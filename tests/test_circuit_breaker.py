"""
CircuitBreaker 单元测试

测试熔断器的核心功能：
- Level 检测 (NORMAL, TIGHTEN, REDUCE, CLOSE_ALL)
- 短期急跌检测 (short_term_drop)
- 状态持久化
- 重置功能
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.infra.circuit_breaker import CircuitBreaker, CircuitLevel


def _make_klines(base_price: float, changes: list[float]) -> list:
    """
    生成模拟 K 线数据。

    Args:
        base_price: 起始价格
        changes: 每个周期的价格变化率 (e.g., [-0.01, 0.005] 表示 -1%, +0.5%)

    Returns:
        K 线列表，每条 [open_time, open, high, low, close, volume, ...]
    """
    klines = []
    price = base_price
    for i, change in enumerate(changes):
        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * 1.001
        low_price = min(open_price, close_price) * 0.999
        klines.append(
            [
                i * 3600 * 1000,
                str(open_price),
                str(high_price),
                str(low_price),
                str(close_price),
                "1000.0",
            ]
        )
        price = close_price
    return klines


class TestCircuitBreakerLevels:
    """熔断级别检测测试。"""

    def test_normal_level_no_drop(self):
        """无明显跌幅时应为 NORMAL。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.001, 0.001, 0.001, 0.001, 0.001, 0.001])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.NORMAL
        assert result.tighten_ratio == 0.0
        assert result.reduce_ratio == 0.0

    def test_tighten_level_small_drop(self):
        """跌幅 -3% ~ -5% 应触发 TIGHTEN。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.04])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.TIGHTEN
        assert result.tighten_ratio == 0.15
        assert result.reduce_ratio == 0.25

    def test_reduce_level_medium_drop(self):
        """跌幅 -5% ~ -8% 应触发 REDUCE。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.06])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.REDUCE
        assert result.tighten_ratio == 0.4
        assert result.reduce_ratio == 0.5

    def test_close_all_level_severe_drop(self):
        """跌幅 > -8% 应触发 CLOSE_ALL。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.10])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.CLOSE_ALL
        assert result.tighten_ratio == 0.4
        assert result.reduce_ratio == 1.0

    def test_btc_1h_return_pct_format(self):
        """返回的 btc_1h_return_pct 应为百分比形式。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.05])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.btc_1h_return_pct == pytest.approx(-5.0, rel=0.1)


class TestCircuitBreakerShortTermDrop:
    """短期急跌检测测试。"""

    def test_short_term_drop_detected(self):
        """最近一根 K 线跌幅 > 2% 应触发 short_term_drop。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.03])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.short_term_drop is True

    def test_short_term_drop_not_detected(self):
        """最近一根 K 线跌幅 < 2% 不应触发 short_term_drop。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [0.0, 0.0, 0.0, 0.0, 0.0, -0.01])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.short_term_drop is False


class TestCircuitBreakerPersistence:
    """状态持久化测试。"""

    def test_persists_level_tighten_and_above(self, tmp_path: Path):
        """Level >= TIGHTEN (1, 2, 3) 都应被持久化。"""
        for level_value, level_name in [
            (1, "TIGHTEN"),
            (2, "REDUCE"),
            (3, "CLOSE_ALL"),
        ]:
            cb = CircuitBreaker(db_path=str(tmp_path / f"test_{level_name}"))
            drop = -0.04 if level_value == 1 else -0.06 if level_value == 2 else -0.10
            klines = _make_klines(80000, [0.0] * 5 + [drop])
            cb.check_from_klines(lambda s, i, l, kl=klines: kl)

            marker = tmp_path / f"test_{level_name}" / "circuit_breaker_level.json"
            assert marker.exists(), f"{level_name} should be persisted"

            data = json.loads(marker.read_text())
            assert data["level"] == level_value

    def test_loads_persisted_level_on_init(self, tmp_path: Path):
        """重启后应正确加载持久化的级别。"""
        db_path = tmp_path / "persist_load"
        db_path.mkdir()

        klines = _make_klines(80000, [0.0] * 5 + [-0.10])
        cb1 = CircuitBreaker(db_path=str(db_path))
        cb1.check_from_klines(lambda s, i, l: klines)
        assert cb1.current_level == CircuitLevel.CLOSE_ALL

        cb2 = CircuitBreaker(db_path=str(db_path))
        assert cb2.current_level == CircuitLevel.CLOSE_ALL

    def test_normal_not_persisted(self, tmp_path: Path):
        """NORMAL 级别不应持久化。"""
        cb = CircuitBreaker(db_path=str(tmp_path))
        klines = _make_klines(80000, [0.001] * 6)
        cb.check_from_klines(lambda s, i, l: klines)

        marker = tmp_path / "circuit_breaker_level.json"
        assert not marker.exists()


class TestCircuitBreakerReset:
    """重置功能测试。"""

    def test_reset_clears_level(self, tmp_path: Path):
        """reset() 应清除当前级别。"""
        cb = CircuitBreaker(db_path=str(tmp_path))
        klines = _make_klines(80000, [0.0] * 5 + [-0.10])
        cb.check_from_klines(lambda s, i, l: klines)
        assert cb.current_level == CircuitLevel.CLOSE_ALL

        cb.reset(force=True)
        assert cb.current_level == CircuitLevel.NORMAL

    def test_reset_clears_persistence(self, tmp_path: Path):
        """reset(force=True) 应同时清除持久化文件。"""
        cb = CircuitBreaker(db_path=str(tmp_path))
        klines = _make_klines(80000, [0.0] * 5 + [-0.10])
        cb.check_from_klines(lambda s, i, l: klines)

        marker = tmp_path / "circuit_breaker_level.json"
        assert marker.exists()

        cb.reset(force=True)
        assert not marker.exists()


class TestCircuitBreakerEdgeCases:
    """边界情况测试。"""

    def test_insufficient_klines(self):
        """K 线不足时应返回 NORMAL。"""
        cb = CircuitBreaker()
        klines = _make_klines(80000, [-0.10])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.NORMAL

    def test_zero_price_handled(self):
        """价格为 0 时应返回 NORMAL 而不崩溃。"""
        cb = CircuitBreaker()
        klines = _make_klines(0, [-0.10])
        result = cb.check_from_klines(lambda s, i, l: klines)
        assert result.level == CircuitLevel.NORMAL
