"""
市场熔断器 — BTC 急跌时自动收紧持仓保护

当 BTC 在短时间内出现急跌时，分级触发保护动作：
  Level 0：正常，无动作
  Level 1：BTC 1h 内跌幅 > 3%  → 收紧止损 15% + 减仓 25%
  Level 2：BTC 1h 内跌幅 > 5%  → 减仓 50%，止损收紧 40%
  Level 3：BTC 1h 内跌幅 > 8%  → 全部强平，停止开新仓（切换 Paper Mode）

同时检查短期快速下跌（最近1根1h K线跌幅 > 2%）作为补充预警。

内部所有计算使用 decimal 形式（-0.10 表示 -10%），
结果返回时乘以 100 转换为百分比形式（-10.0%）供外部显示。

Level 3 触发后会持久化标记到 db_path（如果提供），重启后可检测。
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class CircuitLevel(IntEnum):
    NORMAL = 0  # 正常
    TIGHTEN = 1  # 收紧止损 + 减仓
    REDUCE = 2  # 减仓 + 收紧止损
    CLOSE_ALL = 3  # 全部强平


@dataclass
class CircuitBreakerResult:
    level: CircuitLevel
    btc_price: float
    btc_1h_return_pct: float  # 1h 回报率（百分比形式）
    tighten_ratio: float  # 止损收紧比例（0=不收紧）
    reduce_ratio: float  # 减仓比例（0=不减仓）
    short_term_drop: bool  # 是否检测到短期快速下跌


class CircuitBreaker:
    # BTC 1h K 线回看窗口
    BTC_LOOKBACK_BARS = 6  # 6 × 1h = 6h
    # 短期 K 线（用于检测快速下跌）
    SHORT_TERM_BARS = 1  # 1 × 1h = 1h
    # 分级阈值（decimal 形式）
    THRESHOLD_TIGHTEN = -0.03  # -3%
    THRESHOLD_REDUCE = -0.05  # -5%
    THRESHOLD_CLOSE = -0.08  # -8%
    # 短期快速下跌阈值
    SHORT_TERM_DROP_THRESHOLD = -0.02  # -2%

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._current_level: CircuitLevel = CircuitLevel.NORMAL
        self._db_path: Optional[Path] = Path(db_path) if db_path else None
        self._load_persisted_level()

    def _load_persisted_level(self) -> None:
        if self._db_path is None:
            return
        marker = self._db_path / "circuit_breaker_level3.json"
        if marker.exists():
            try:
                data = json.loads(marker.read_text())
                level = CircuitLevel(data.get("level", 0))
                if level >= CircuitLevel.CLOSE_ALL:
                    self._current_level = level
                    log.warning(
                        "[CircuitBreaker] 检测到上次 Level 3 熔断标记，"
                        "当前级别设为 CLOSE_ALL，重启后继续处理"
                    )
            except Exception as exc:
                log.warning("[CircuitBreaker] 读取熔断标记失败: %s", exc)

    def _persist_level3(self) -> None:
        if self._db_path is None:
            return
        try:
            self._db_path.mkdir(parents=True, exist_ok=True)
            marker = self._db_path / "circuit_breaker_level3.json"
            marker.write_text(
                json.dumps(
                    {
                        "level": CircuitLevel.CLOSE_ALL,
                        "timestamp": time.time(),
                    }
                )
            )
            log.info("[CircuitBreaker] Level 3 熔断已持久化")
        except Exception as exc:
            log.warning("[CircuitBreaker] 持久化 Level 3 失败: %s", exc)

    def check(self, btc_price: float) -> CircuitBreakerResult:
        """
        通过单点价格评估熔断级别（已废弃，仅保留兼容接口）。
        建议使用 check_from_klines() 获取更精确的结果。

        Args:
            btc_price: BTC 当前价格（USDT）

        Returns:
            CircuitBreakerResult
        """
        raise NotImplementedError(
            "check() 已废弃，请使用 check_from_klines() 进行熔断检测"
        )

    def check_from_klines(
        self,
        fetch_btc_klines: Callable[[str, str, int], list],
    ) -> CircuitBreakerResult:
        """
        从 K 线计算 BTC 回报并评估熔断级别。

        使用 6 根 1h K 线（6h）计算主级别，
        同时用最近 1 根 1h K 线检测短期快速下跌。

        Args:
            fetch_btc_klines: 接收 (symbol, interval, limit)，返回 K 线列表的函数
        """
        try:
            klines = fetch_btc_klines("BTCUSDT", "1h", self.BTC_LOOKBACK_BARS)
            if not klines or len(klines) < self.BTC_LOOKBACK_BARS:
                with self._lock:
                    self._current_level = CircuitLevel.NORMAL
                return CircuitBreakerResult(
                    level=CircuitLevel.NORMAL,
                    btc_price=0.0,
                    btc_1h_return_pct=0.0,
                    tighten_ratio=0.0,
                    reduce_ratio=0.0,
                    short_term_drop=False,
                )

            current_price = float(klines[-1][4])
            past_price = float(klines[-self.BTC_LOOKBACK_BARS][4])
            short_term_price = float(klines[-2][4]) if len(klines) >= 2 else past_price

            if past_price <= 0 or current_price <= 0:
                log.warning(
                    "[CircuitBreaker] BTC价格异常(past=%.4f, current=%.4f)，跳过",
                    past_price,
                    current_price,
                )
                with self._lock:
                    self._current_level = CircuitLevel.NORMAL
                return CircuitBreakerResult(
                    level=CircuitLevel.NORMAL,
                    btc_price=current_price if current_price > 0 else 0.0,
                    btc_1h_return_pct=0.0,
                    tighten_ratio=0.0,
                    reduce_ratio=0.0,
                    short_term_drop=False,
                )

            btc_1h_return = (current_price - past_price) / past_price
            short_term_return = (current_price - short_term_price) / short_term_price
            short_term_drop = short_term_return < self.SHORT_TERM_DROP_THRESHOLD

            if short_term_drop:
                log.warning(
                    "[CircuitBreaker] BTC 短期快速下跌检测: %.2f%%（阈值 %.2f%%）",
                    short_term_return * 100,
                    self.SHORT_TERM_DROP_THRESHOLD * 100,
                )

        except Exception as exc:
            log.warning("[CircuitBreaker] BTC K线失败: %s", exc)
            with self._lock:
                self._current_level = CircuitLevel.NORMAL
            return CircuitBreakerResult(
                level=CircuitLevel.NORMAL,
                btc_price=0.0,
                btc_1h_return_pct=0.0,
                tighten_ratio=0.0,
                reduce_ratio=0.0,
                short_term_drop=False,
            )

        with self._lock:
            if btc_1h_return < self.THRESHOLD_CLOSE:
                level = CircuitLevel.CLOSE_ALL
                tighten_ratio = 0.4
                reduce_ratio = 1.0
            elif btc_1h_return < self.THRESHOLD_REDUCE:
                level = CircuitLevel.REDUCE
                tighten_ratio = 0.4
                reduce_ratio = 0.5
            elif btc_1h_return < self.THRESHOLD_TIGHTEN:
                level = CircuitLevel.TIGHTEN
                tighten_ratio = 0.15
                reduce_ratio = 0.25
            else:
                level = CircuitLevel.NORMAL
                tighten_ratio = 0.0
                reduce_ratio = 0.0

            self._current_level = level
            if level >= CircuitLevel.CLOSE_ALL:
                self._persist_level3()
            return CircuitBreakerResult(
                level=level,
                btc_price=current_price,
                btc_1h_return_pct=round(btc_1h_return * 100, 2),
                tighten_ratio=tighten_ratio,
                reduce_ratio=reduce_ratio,
                short_term_drop=short_term_drop,
            )

    @property
    def current_level(self) -> CircuitLevel:
        return self._current_level

    def reset(self, force: bool = False) -> None:
        """
        重置熔断状态。

        Args:
            force: 为 True 时强制重置所有级别包括 Level 3；False 时只重置 Level 1/2。
        """
        with self._lock:
            if self._current_level < CircuitLevel.CLOSE_ALL:
                self._current_level = CircuitLevel.NORMAL
                log.info("[CircuitBreaker] 熔断状态已重置")
            elif force and self._current_level >= CircuitLevel.CLOSE_ALL:
                self._current_level = CircuitLevel.NORMAL
                self._clear_persisted_level()
                log.info("[CircuitBreaker] Level 3 熔断状态已重置（force=True）")

    def _clear_persisted_level(self) -> None:
        if self._db_path is None:
            return
        marker = self._db_path / "circuit_breaker_level3.json"
        if marker.exists():
            try:
                marker.unlink()
                log.info("[CircuitBreaker] 熔断持久化标记已清除")
            except Exception as exc:
                log.warning("[CircuitBreaker] 清除熔断标记失败: %s", exc)
