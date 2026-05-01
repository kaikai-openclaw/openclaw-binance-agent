"""
Risk_Controller 风控拦截层模块

作为独立拦截层运行，所有交易指令在到达 Binance_Fapi_Client 之前
必须通过其校验。硬编码四大风控常量，不可配置。

功能：
- validate_order(): 单笔保证金、单币持仓、止损冷却期校验
- check_daily_loss(): 日亏损 ≥ 5% 检测
- execute_degradation(): 取消挂单、停止实盘、告警、切换 Paper Mode
- is_paper_mode(): 查询当前模式
- record_stop_loss(): 记录止损事件，启动冷却期（持久化到 SQLite）
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models.types import (
    AccountState,
    OrderRequest,
    ValidationResult,
)

log = logging.getLogger(__name__)


class RiskController:
    """
    风控拦截层。

    所有交易指令在到达 Binance_Fapi_Client 之前必须通过此层校验。
    四大风控常量为硬编码，不可通过配置修改。
    止损冷却记录持久化到 SQLite，进程重启后冷却期不丢失。
    """

    # 硬编码常量（不可配置）
    MAX_SINGLE_MARGIN_RATIO = 0.35    # 单笔保证金 <= 总资金 35%
    MAX_SINGLE_COIN_RATIO = 0.40      # 单币累计持仓 <= 总资金 40%
    DAILY_LOSS_THRESHOLD = 0.05       # 日亏损阈值 5%
    STOP_LOSS_COOLDOWN_HOURS = 24     # 止损后同方向冷却期（小时）

    _CREATE_COOLDOWN_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS stop_loss_cooldowns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            direction   TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            strategy_tag TEXT NOT NULL DEFAULT ''
        )
    """

    _CREATE_COOLDOWN_INDEX_SQL = """
        CREATE INDEX IF NOT EXISTS idx_cooldown_recorded_at
        ON stop_loss_cooldowns(recorded_at DESC)
    """

    _CREATE_RUNTIME_STATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS risk_runtime_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    # 在线迁移：为旧表补充 strategy_tag 列
    _ADD_COOLDOWN_STRATEGY_TAG_SQL = (
        "ALTER TABLE stop_loss_cooldowns "
        "ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT ''"
    )

    def __init__(self, db_path: Optional[str] = None) -> None:
        """
        初始化 RiskController。

        参数:
            db_path: SQLite 数据库路径。为 None 时使用内存列表（兼容旧行为/测试）。
                     传入路径时止损冷却记录持久化到 SQLite。
        """
        # 模拟盘模式标志；db_path 模式会从 risk_runtime_state 恢复。
        self._paper_mode: bool = False
        # 按策略独立的 Paper Mode：strategy_tag → bool
        self._strategy_paper_modes: dict[str, bool] = {}

        # 持久化模式
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        # 线程本地存储：每个线程用自己的 SQLite 连接，避免跨线程访问冲突
        self._thread_local = threading.local()

        # 内存回退（无 db_path 时使用，兼容旧行为和测试）
        self._stop_loss_records: list[tuple[str, str, str, datetime]] = []

        if db_path is not None:
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            # 主线程连接存入 thread_local，worker 线程通过 _get_conn() 各自创建
            self._thread_local.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._thread_local.conn.execute("PRAGMA journal_mode=WAL")
            self._thread_local.conn.execute(self._CREATE_COOLDOWN_TABLE_SQL)
            self._thread_local.conn.execute(self._CREATE_COOLDOWN_INDEX_SQL)
            self._thread_local.conn.execute(self._CREATE_RUNTIME_STATE_TABLE_SQL)
            self._migrate_cooldown_strategy_tag(self._thread_local.conn)
            self._thread_local.conn.commit()
            self._conn = self._thread_local.conn  # 保留引用供 __del__ 使用
            self._paper_mode = self._load_paper_mode()
            self._strategy_paper_modes = self._load_strategy_paper_modes()

    def _get_conn(self) -> Optional[sqlite3.Connection]:
        """
        返回当前线程的 SQLite 连接。

        主线程在 __init__ 中已初始化连接；worker 线程首次调用时
        在该线程内创建新连接（延迟创建，复用同线程多次调用）。
        """
        if self._db_path is None:
            return None
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            return conn
        # Worker 线程首次调用，创建属于该线程的独立连接并初始化表结构
        new_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        new_conn.execute("PRAGMA journal_mode=WAL")
        new_conn.execute(self._CREATE_COOLDOWN_TABLE_SQL)
        new_conn.execute(self._CREATE_COOLDOWN_INDEX_SQL)
        new_conn.execute(self._CREATE_RUNTIME_STATE_TABLE_SQL)
        self._migrate_cooldown_strategy_tag(new_conn)
        new_conn.commit()
        self._thread_local.conn = new_conn
        return new_conn

    def validate_order(
        self, order: OrderRequest, account: AccountState,
        strategy_tag: str = "",
    ) -> ValidationResult:
        """
        对单笔订单执行全部风控断言校验。

        校验项：
        1. 单笔保证金 <= 总资金 × 35%
        2. 单币累计持仓 <= 总资金 × 40%
        3. 止损冷却期内禁止同方向开仓（按 strategy_tag 隔离）
        4. 该策略是否处于 Paper Mode

        任一断言失败即拒绝订单。
        """
        # 断言 0：策略级 Paper Mode 检查
        if strategy_tag and self.is_strategy_paper_mode(strategy_tag):
            reason = (
                f"策略 {strategy_tag} 处于模拟盘模式，拒绝实盘下单"
            )
            log.warning(f"风控拒绝: {reason}")
            return ValidationResult(passed=False, reason=reason)

        total_balance = account.total_balance

        # 断言 1：单笔保证金 <= 总资金 20%
        single_margin = order.quantity * order.price / order.leverage
        margin_limit = total_balance * self.MAX_SINGLE_MARGIN_RATIO
        if single_margin > margin_limit:
            reason = (
                f"单笔保证金 {single_margin:.2f} 超过限额 {margin_limit:.2f}"
                f"（总资金 {total_balance:.2f} × {self.MAX_SINGLE_MARGIN_RATIO:.0%}）"
            )
            log.warning(f"风控拒绝: {reason}")
            return ValidationResult(passed=False, reason=reason)

        # 断言 2：单币累计持仓 <= 总资金 30%
        existing_value = self._get_position_value(order.symbol, account)
        new_order_value = order.quantity * order.price
        new_total = existing_value + new_order_value
        coin_limit = total_balance * self.MAX_SINGLE_COIN_RATIO
        if new_total > coin_limit:
            reason = (
                f"单币累计持仓 {new_total:.2f} 超过限额 {coin_limit:.2f}"
                f"（总资金 {total_balance:.2f} × {self.MAX_SINGLE_COIN_RATIO:.0%}）"
            )
            log.warning(f"风控拒绝: {reason}")
            return ValidationResult(passed=False, reason=reason)

        # 断言 3：止损冷却期检查（按策略隔离）
        direction_str = (
            order.direction.value
            if hasattr(order.direction, "value")
            else str(order.direction)
        )
        if self._is_in_cooldown(order.symbol, direction_str, strategy_tag):
            reason = (
                f"{order.symbol} {direction_str} 处于止损冷却期"
                f"（{self.STOP_LOSS_COOLDOWN_HOURS} 小时内禁止同方向开仓）"
                + (f"，策略={strategy_tag}" if strategy_tag else "")
            )
            log.warning(f"风控拒绝: {reason}")
            return ValidationResult(passed=False, reason=reason)

        return ValidationResult(passed=True)

    def check_daily_loss(self, account: AccountState) -> bool:
        """
        检查当日累计已实现亏损是否触及 5% 阈值。

        返回 True 表示需要降级（亏损已达阈值）。
        """
        if account.total_balance <= 0:
            return False

        daily_pnl = account.daily_realized_pnl
        # 仅在亏损时检查（daily_pnl 为负数）
        if daily_pnl >= 0:
            return False

        loss_ratio = abs(daily_pnl) / account.total_balance
        return loss_ratio >= self.DAILY_LOSS_THRESHOLD

    def execute_degradation(
        self, account: AccountState, binance_client=None,
        strategy_tag: str = "",
    ) -> None:
        """
        执行降级流程：
        1. 取消所有未成交挂单（仅全局降级时）
        2. 停止实盘下单
        3. 发出告警通知
        4. 切换至 Paper_Trading_Mode（按策略或全局）
        """
        loss_ratio = (
            abs(account.daily_realized_pnl) / account.total_balance
            if account.total_balance > 0
            else 0
        )

        # 步骤 1：取消所有未成交挂单（仅全局降级时执行）
        if not strategy_tag and binance_client is not None:
            try:
                cancelled = binance_client.cancel_all_orders()
                log.info(f"降级流程: 已取消 {cancelled} 笔挂单")
            except Exception as e:
                log.error(f"降级流程: 取消挂单失败 - {e}")

        tag_info = f"策略={strategy_tag}" if strategy_tag else "全局"

        # 步骤 2 & 3：告警
        log.critical(
            f"风控降级触发 [{tag_info}]: 日亏损达 {loss_ratio:.2%}，"
            f"已触及 {self.DAILY_LOSS_THRESHOLD:.0%} 阈值，"
            f"降级至模拟盘"
        )

        # 步骤 4：切换至 Paper_Trading_Mode
        if strategy_tag:
            self.enable_strategy_paper_mode(
                strategy_tag, f"daily_loss_degradation_{strategy_tag}"
            )
        else:
            self.enable_paper_mode("daily_loss_degradation")

        log.warning(
            f"风控降级完成 [{tag_info}]: 日亏损 {loss_ratio:.2%}，"
            f"当前模式=Paper_Trading"
        )

    def is_paper_mode(self) -> bool:
        """返回当前是否处于模拟盘模式。"""
        return self._paper_mode

    def enable_paper_mode(self, reason: str = "manual") -> None:
        """切换到模拟盘模式，并在持久化模式下保存运行时状态。"""
        self._paper_mode = True
        self._persist_runtime_state("paper_mode", "true", reason)
        log.warning(f"Paper Mode 已启用: reason={reason}")

    def disable_paper_mode(self, reason: str = "manual") -> None:
        """显式恢复实盘模式；仅供人工确认后调用。"""
        self._paper_mode = False
        self._persist_runtime_state("paper_mode", "false", reason)
        log.warning(f"Paper Mode 已关闭: reason={reason}")

    # ── 策略级 Paper Mode ─────────────────────────────────

    def is_strategy_paper_mode(self, strategy_tag: str) -> bool:
        """检查指定策略是否处于模拟盘模式。全局 Paper Mode 优先。"""
        if self._paper_mode:
            return True
        return self._strategy_paper_modes.get(strategy_tag, False)

    def enable_strategy_paper_mode(self, strategy_tag: str, reason: str = "manual") -> None:
        """将指定策略切换到模拟盘模式。"""
        self._strategy_paper_modes[strategy_tag] = True
        key = f"paper_mode:{strategy_tag}"
        self._persist_runtime_state(key, "true", reason)
        log.warning(f"策略 Paper Mode 已启用: strategy={strategy_tag}, reason={reason}")

    def disable_strategy_paper_mode(self, strategy_tag: str, reason: str = "manual") -> None:
        """将指定策略恢复实盘模式。"""
        self._strategy_paper_modes[strategy_tag] = False
        key = f"paper_mode:{strategy_tag}"
        self._persist_runtime_state(key, "false", reason)
        log.warning(f"策略 Paper Mode 已关闭: strategy={strategy_tag}, reason={reason}")

    def record_stop_loss(self, symbol: str, direction: str, strategy_tag: str = "") -> None:
        """
        记录某币种某方向的止损事件，启动 24 小时冷却期。

        持久化到 SQLite（若已配置），同时写入内存列表作为回退。
        冷却期按 (symbol, direction, strategy_tag) 隔离，不同策略互不影响。

        参数:
            symbol: 币种交易对符号，如 "BTCUSDT"
            direction: 交易方向，"long" 或 "short"
            strategy_tag: 策略标签，如 "crypto_oversold_long"
        """
        now = datetime.now(timezone.utc)

        # 内存记录（兼容无 db_path 场景）
        self._stop_loss_records.append((symbol, direction, strategy_tag, now))

        conn = self._get_conn()
        if conn is not None:
            conn.execute(
                "INSERT INTO stop_loss_cooldowns "
                "(symbol, direction, strategy_tag, recorded_at) VALUES (?, ?, ?, ?)",
                (symbol, direction, strategy_tag, now.isoformat()),
            )
            conn.commit()

        log.info(
            f"止损记录: {symbol} {direction} strategy={strategy_tag} "
            f"于 {now.isoformat()}，冷却期 {self.STOP_LOSS_COOLDOWN_HOURS} 小时"
        )

    # ----------------------------------------------------------------
    # 内部辅助方法
    # ----------------------------------------------------------------

    def _get_position_value(
        self, symbol: str, account: AccountState
    ) -> float:
        """
        获取指定币种的现有持仓价值。

        从 account.positions 中查找匹配 symbol 的持仓，
        累加其持仓价值（quantity × entry_price 或 quantity × current_price）。
        支持 dict 和具有属性的对象两种格式。
        """
        total_value = 0.0
        for pos in account.positions:
            # 兼容 dict 和 dataclass/对象
            if isinstance(pos, dict):
                pos_symbol = pos.get("symbol", "")
                quantity = pos.get("quantity", 0)
                price = pos.get("entry_price", 0) or pos.get("current_price", 0)
            else:
                pos_symbol = getattr(pos, "symbol", "")
                quantity = getattr(pos, "quantity", 0)
                price = getattr(pos, "entry_price", 0) or getattr(
                    pos, "current_price", 0
                )

            if pos_symbol == symbol:
                total_value += abs(quantity) * price

        return total_value

    def _is_in_cooldown(self, symbol: str, direction: str, strategy_tag: str = "") -> bool:
        """
        检查指定币种、方向和策略是否处于止损冷却期内。

        冷却期按 (symbol, direction, strategy_tag) 隔离。
        优先查询 SQLite（若已配置），否则回退到内存列表。
        """
        now = datetime.now(timezone.utc)

        conn = self._get_conn()
        if conn is not None:
            cutoff = (now - timedelta(hours=self.STOP_LOSS_COOLDOWN_HOURS)).isoformat()
            cursor = conn.execute(
                "SELECT 1 FROM stop_loss_cooldowns "
                "WHERE symbol = ? AND direction = ? AND strategy_tag = ? "
                "AND recorded_at > ? LIMIT 1",
                (symbol, direction, strategy_tag, cutoff),
            )
            return cursor.fetchone() is not None

        # 内存回退
        cooldown_delta = timedelta(hours=self.STOP_LOSS_COOLDOWN_HOURS)
        for rec_symbol, rec_direction, rec_tag, rec_time in self._stop_loss_records:
            if (
                rec_symbol == symbol
                and rec_direction == direction
                and rec_tag == strategy_tag
            ):
                # 兼容 naive datetime（旧测试用 datetime.now() 无时区）
                if rec_time.tzinfo is None:
                    elapsed = datetime.now() - rec_time
                else:
                    elapsed = now - rec_time
                if elapsed < cooldown_delta:
                    return True

        return False

    def _load_paper_mode(self) -> bool:
        """从 SQLite 运行时状态恢复 Paper Mode。"""
        conn = self._get_conn()
        if conn is None:
            return self._paper_mode
        cursor = conn.execute(
            "SELECT value FROM risk_runtime_state WHERE key = ?",
            ("paper_mode",),
        )
        row = cursor.fetchone()
        return row is not None and row[0].lower() == "true"

    def _persist_runtime_state(self, key: str, value: str, reason: str) -> None:
        """保存风控运行时状态；无数据库时保持内存行为。"""
        conn = self._get_conn()
        if conn is None:
            return
        updated_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO risk_runtime_state (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value, updated_at),
        )
        conn.commit()
        log.info(f"风控运行时状态已保存: {key}={value}, reason={reason}")

    def _load_strategy_paper_modes(self) -> dict[str, bool]:
        """从 SQLite 运行时状态恢复所有策略级 Paper Mode。"""
        modes: dict[str, bool] = {}
        conn = self._get_conn()
        if conn is None:
            return modes
        cursor = conn.execute(
            "SELECT key, value FROM risk_runtime_state WHERE key LIKE 'paper_mode:%'",
        )
        for key, value in cursor.fetchall():
            # key 格式: "paper_mode:crypto_oversold_long"
            strategy_tag = key.split(":", 1)[1] if ":" in key else ""
            if strategy_tag:
                modes[strategy_tag] = value.lower() == "true"
        return modes

    @staticmethod
    def _migrate_cooldown_strategy_tag(conn: sqlite3.Connection) -> None:
        """在线迁移：为旧 stop_loss_cooldowns 表补充 strategy_tag 列。"""
        cursor = conn.execute("PRAGMA table_info(stop_loss_cooldowns)")
        columns = {row[1] for row in cursor.fetchall()}
        if "strategy_tag" not in columns:
            conn.execute(
                "ALTER TABLE stop_loss_cooldowns "
                "ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        """关闭数据库连接（若有）。"""
        if self._conn is not None:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
