"""
Memory_Store 长期记忆库模块

基于 SQLite 实现历史交易归因数据存储，支持策略自我进化的反思与调优。
存储已平仓交易的核心数据和策略调优反思日志。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from typing import List, Optional

from src.models.types import (
    ReflectionLog,
    StrategyStats,
    TradeDirection,
    TradeRecord,
)


class MemoryStore:
    """
    Agent 长期记忆库，存储历史交易归因数据用于策略自我进化。

    - record_trade(): 存储一笔已平仓交易的核心数据
    - get_recent_trades(): 获取最近 N 笔交易记录，按平仓时间倒序
    - compute_stats(): 计算策略胜率和平均盈亏比
    - save_reflection(): 存储策略调优建议至反思日志
    - get_latest_reflection(): 获取最新的反思日志
    """

    # 交易记录表建表 SQL
    _CREATE_TRADES_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS trade_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            entry_price     REAL NOT NULL,
            exit_price      REAL NOT NULL,
            pnl_amount      REAL NOT NULL,
            hold_duration_hours REAL NOT NULL,
            rating_score    INTEGER NOT NULL,
            position_size_pct REAL NOT NULL,
            closed_at       TEXT NOT NULL,
            strategy_tag    TEXT NOT NULL DEFAULT 'unknown'
        )
    """

    # 交易记录索引：按平仓时间倒序，加速 get_recent_trades 查询
    _CREATE_TRADES_INDEX_SQL = """
        CREATE INDEX IF NOT EXISTS idx_trades_closed_at
        ON trade_records(closed_at DESC)
    """

    # 反思日志表建表 SQL（含 strategy_tag 用于按策略独立进化）
    _CREATE_REFLECTIONS_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS reflection_logs (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at                  TEXT NOT NULL,
            win_rate                    REAL NOT NULL,
            avg_pnl_ratio               REAL NOT NULL,
            suggested_rating_threshold  INTEGER NOT NULL,
            suggested_risk_ratio        REAL NOT NULL,
            reasoning                   TEXT NOT NULL,
            strategy_tag                TEXT NOT NULL DEFAULT ''
        )
    """

    # 反思日志索引：按创建时间倒序，加速 get_latest_reflection 查询
    _CREATE_REFLECTIONS_INDEX_SQL = """
        CREATE INDEX IF NOT EXISTS idx_reflections_created_at
        ON reflection_logs(created_at DESC)
    """

    _CREATE_TRADE_SYNC_KEYS_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS trade_sync_keys (
            sync_key        TEXT PRIMARY KEY,
            trade_record_id INTEGER,
            created_at      TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str = "data/memory_store.db") -> None:
        """
        初始化 MemoryStore，创建数据库连接并确保表结构存在。

        参数:
            db_path: SQLite 数据库文件路径，默认为 data/memory_store.db
        """
        # 确保数据库目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构和索引。"""
        cursor = self._conn.cursor()
        cursor.execute(self._CREATE_TRADES_TABLE_SQL)
        cursor.execute(self._CREATE_TRADES_INDEX_SQL)
        cursor.execute(self._CREATE_REFLECTIONS_TABLE_SQL)
        cursor.execute(self._CREATE_REFLECTIONS_INDEX_SQL)
        cursor.execute(self._CREATE_TRADE_SYNC_KEYS_TABLE_SQL)
        self._ensure_trade_records_strategy_tag(cursor)
        self._ensure_reflection_logs_strategy_tag(cursor)
        self._conn.commit()

    @staticmethod
    def _has_column(cursor, table_name: str, column_name: str) -> bool:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return any(row[1] == column_name for row in cursor.fetchall())

    def _ensure_trade_records_strategy_tag(self, cursor) -> None:
        if not self._has_column(cursor, "trade_records", "strategy_tag"):
            cursor.execute(
                "ALTER TABLE trade_records "
                "ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT 'unknown'"
            )

    def _ensure_reflection_logs_strategy_tag(self, cursor) -> None:
        if not self._has_column(cursor, "reflection_logs", "strategy_tag"):
            cursor.execute(
                "ALTER TABLE reflection_logs "
                "ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT ''"
            )

    def record_trade(self, trade: TradeRecord) -> None:
        """
        存储一笔已平仓交易的核心数据。

        参数:
            trade: 交易记录数据对象
        """
        self._conn.execute(
            "INSERT INTO trade_records "
            "(symbol, direction, entry_price, exit_price, pnl_amount, "
            "hold_duration_hours, rating_score, position_size_pct, closed_at, strategy_tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade.symbol,
                trade.direction.value,
                trade.entry_price,
                trade.exit_price,
                trade.pnl_amount,
                trade.hold_duration_hours,
                trade.rating_score,
                trade.position_size_pct,
                trade.closed_at.isoformat(),
                trade.strategy_tag,
            ),
        )
        self._conn.commit()

    def record_trade_once(self, trade: TradeRecord, sync_key: str) -> bool:
        """
        幂等存储一笔外部同步来的已平仓交易。

        参数:
            trade: 交易记录数据对象
            sync_key: 外部成交的唯一键，如 binance_user_trade:BTCUSDT:123

        返回:
            True 表示本次新写入，False 表示该 sync_key 已同步过。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO trade_sync_keys "
                "(sync_key, trade_record_id, created_at) VALUES (?, ?, ?)",
                (sync_key, None, now),
            )
            if cursor.rowcount == 0:
                return False

            trade_cursor = self._conn.execute(
                "INSERT INTO trade_records "
                "(symbol, direction, entry_price, exit_price, pnl_amount, "
                "hold_duration_hours, rating_score, position_size_pct, closed_at, strategy_tag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade.symbol,
                    trade.direction.value,
                    trade.entry_price,
                    trade.exit_price,
                    trade.pnl_amount,
                    trade.hold_duration_hours,
                    trade.rating_score,
                    trade.position_size_pct,
                    trade.closed_at.isoformat(),
                    trade.strategy_tag,
                ),
            )
            self._conn.execute(
                "UPDATE trade_sync_keys SET trade_record_id = ? WHERE sync_key = ?",
                (trade_cursor.lastrowid, sync_key),
            )
            return True

    def get_recent_trades(self, limit: int = 50) -> List[TradeRecord]:
        """
        获取最近 N 笔交易记录，按平仓时间倒序。

        参数:
            limit: 返回的最大记录数，默认 50

        返回:
            交易记录列表，按平仓时间倒序排列
        """
        cursor = self._conn.execute(
            "SELECT symbol, direction, entry_price, exit_price, pnl_amount, "
            "hold_duration_hours, rating_score, position_size_pct, closed_at, strategy_tag "
            "FROM trade_records ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        return [
            TradeRecord(
                symbol=row[0],
                direction=TradeDirection(row[1]),
                entry_price=row[2],
                exit_price=row[3],
                pnl_amount=row[4],
                hold_duration_hours=row[5],
                rating_score=row[6],
                position_size_pct=row[7],
                closed_at=datetime.fromisoformat(row[8]),
                strategy_tag=row[9],
            )
            for row in rows
        ]

    def get_recent_trades_by_strategy(
        self, strategy_tag: str, limit: int = 50
    ) -> List[TradeRecord]:
        """获取指定策略最近 N 笔交易记录，按平仓时间倒序。"""
        cursor = self._conn.execute(
            "SELECT symbol, direction, entry_price, exit_price, pnl_amount, "
            "hold_duration_hours, rating_score, position_size_pct, closed_at, strategy_tag "
            "FROM trade_records WHERE strategy_tag = ? "
            "ORDER BY closed_at DESC LIMIT ?",
            (strategy_tag, limit),
        )
        rows = cursor.fetchall()
        return [
            TradeRecord(
                symbol=row[0],
                direction=TradeDirection(row[1]),
                entry_price=row[2],
                exit_price=row[3],
                pnl_amount=row[4],
                hold_duration_hours=row[5],
                rating_score=row[6],
                position_size_pct=row[7],
                closed_at=datetime.fromisoformat(row[8]),
                strategy_tag=row[9],
            )
            for row in rows
        ]

    def compute_stats_by_strategy(
        self,
        trades: List[TradeRecord],
    ) -> dict[str, StrategyStats]:
        """按策略标签分别计算胜率和平均盈亏。"""
        grouped: dict[str, List[TradeRecord]] = {}
        for trade in trades:
            grouped.setdefault(trade.strategy_tag or "unknown", []).append(trade)
        return {
            strategy_tag: self.compute_stats(strategy_trades)
            for strategy_tag, strategy_trades in grouped.items()
        }

    def compute_stats(self, trades: List[TradeRecord]) -> StrategyStats:
        """
        计算策略胜率和平均盈亏比。

        胜率 = 盈利笔数 / 总笔数 × 100
        平均盈亏比 = 总盈亏金额 / 总笔数

        参数:
            trades: 交易记录列表

        返回:
            策略统计数据对象
        """
        total = len(trades)
        if total == 0:
            return StrategyStats(
                win_rate=0.0,
                avg_pnl_ratio=0.0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
            )

        winning = [t for t in trades if t.pnl_amount > 0]
        losing = [t for t in trades if t.pnl_amount <= 0]
        win_rate = len(winning) / total * 100
        avg_pnl_ratio = sum(t.pnl_amount for t in trades) / total

        return StrategyStats(
            win_rate=win_rate,
            avg_pnl_ratio=avg_pnl_ratio,
            total_trades=total,
            winning_trades=len(winning),
            losing_trades=len(losing),
        )

    def save_reflection(self, reflection: ReflectionLog) -> None:
        """
        存储策略调优建议至反思日志。

        参数:
            reflection: 反思日志数据对象
        """
        strategy_tag = getattr(reflection, "strategy_tag", "") or ""
        self._conn.execute(
            "INSERT INTO reflection_logs "
            "(created_at, win_rate, avg_pnl_ratio, suggested_rating_threshold, "
            "suggested_risk_ratio, reasoning, strategy_tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                reflection.created_at.isoformat(),
                reflection.win_rate,
                reflection.avg_pnl_ratio,
                reflection.suggested_rating_threshold,
                reflection.suggested_risk_ratio,
                reflection.reasoning,
                strategy_tag,
            ),
        )
        self._conn.commit()

    def get_latest_reflection(self, strategy_tag: str = "") -> Optional[ReflectionLog]:
        """
        获取最新的反思日志。

        参数:
            strategy_tag: 策略标签，空字符串表示全局（兼容旧行为）

        返回:
            最新的反思日志对象，若无记录则返回 None
        """
        cursor = self._conn.execute(
            "SELECT created_at, win_rate, avg_pnl_ratio, "
            "suggested_rating_threshold, suggested_risk_ratio, reasoning, "
            "strategy_tag "
            "FROM reflection_logs WHERE strategy_tag = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (strategy_tag,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return ReflectionLog(
            created_at=datetime.fromisoformat(row[0]),
            win_rate=row[1],
            avg_pnl_ratio=row[2],
            suggested_rating_threshold=row[3],
            suggested_risk_ratio=row[4],
            reasoning=row[5],
            strategy_tag=row[6] if len(row) > 6 else "",
        )

    def get_evolved_params(
        self,
        default_rating_threshold: int = 6,
        default_risk_ratio: float = 0.02,
        strategy_tag: str = "",
    ) -> tuple[int, float]:
        """
        获取进化后的策略参数，供 Pipeline 编排层注入 Skill-2/3。

        从最新反思日志读取建议参数，若无记录则返回默认值。
        支持按 strategy_tag 查询特定策略的参数。

        参数:
            default_rating_threshold: 默认评级过滤阈值
            default_risk_ratio: 默认风险比例
            strategy_tag: 策略标签，空字符串表示全局（兼容旧行为）

        返回:
            (rating_threshold, risk_ratio) 元组
        """
        reflection = self.get_latest_reflection(strategy_tag=strategy_tag)
        if reflection is None:
            return default_rating_threshold, default_risk_ratio
        return (
            reflection.suggested_rating_threshold,
            reflection.suggested_risk_ratio,
        )

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
