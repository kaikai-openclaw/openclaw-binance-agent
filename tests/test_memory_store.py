"""
MemoryStore 单元测试

验证 record_trade/get_recent_trades round-trip、compute_stats 计算、
save_reflection/get_latest_reflection 存取等核心行为。
"""

from datetime import datetime, timezone

import pytest

from src.infra.memory_store import MemoryStore
from src.models.types import (
    ReflectionLog,
    StrategyStats,
    TradeDirection,
    TradeRecord,
)


@pytest.fixture
def store(tmp_path):
    """创建使用临时数据库的 MemoryStore 实例。"""
    db_path = str(tmp_path / "test_memory.db")
    s = MemoryStore(db_path=db_path)
    yield s
    s.close()


def _make_trade(
    symbol: str = "BTCUSDT",
    direction: TradeDirection = TradeDirection.LONG,
    entry_price: float = 50000.0,
    exit_price: float = 52000.0,
    pnl_amount: float = 200.0,
    hold_duration_hours: float = 4.5,
    rating_score: int = 7,
    position_size_pct: float = 10.0,
    closed_at: datetime | None = None,
    strategy_tag: str = "crypto_oversold_short",
) -> TradeRecord:
    """辅助函数：创建交易记录。"""
    if closed_at is None:
        closed_at = datetime.now(timezone.utc)
    return TradeRecord(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl_amount=pnl_amount,
        hold_duration_hours=hold_duration_hours,
        rating_score=rating_score,
        position_size_pct=position_size_pct,
        closed_at=closed_at,
        strategy_tag=strategy_tag,
    )


class TestRecordAndGetTrades:
    """测试 record_trade() 和 get_recent_trades() 的存取功能。"""

    def test_record_and_retrieve_single_trade(self, store):
        """存入一笔交易后应能正确取回。"""
        trade = _make_trade()
        store.record_trade(trade)
        trades = store.get_recent_trades(limit=10)
        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == trade.symbol
        assert t.direction == trade.direction
        assert t.entry_price == trade.entry_price
        assert t.exit_price == trade.exit_price
        assert t.pnl_amount == trade.pnl_amount
        assert t.hold_duration_hours == trade.hold_duration_hours
        assert t.rating_score == trade.rating_score
        assert t.position_size_pct == trade.position_size_pct

    def test_get_recent_trades_ordered_by_closed_at_desc(self, store):
        """get_recent_trades 应按平仓时间倒序返回。"""
        t1 = _make_trade(closed_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        t2 = _make_trade(closed_at=datetime(2024, 1, 3, tzinfo=timezone.utc))
        t3 = _make_trade(closed_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
        store.record_trade(t1)
        store.record_trade(t2)
        store.record_trade(t3)

        trades = store.get_recent_trades(limit=10)
        assert len(trades) == 3
        # 最新的排在前面
        assert trades[0].closed_at >= trades[1].closed_at
        assert trades[1].closed_at >= trades[2].closed_at

    def test_get_recent_trades_respects_limit(self, store):
        """get_recent_trades 应遵守 limit 参数。"""
        for i in range(10):
            store.record_trade(_make_trade(pnl_amount=float(i)))
        trades = store.get_recent_trades(limit=3)
        assert len(trades) == 3

    def test_get_recent_trades_empty_store(self, store):
        """空数据库应返回空列表。"""
        trades = store.get_recent_trades()
        assert trades == []

    def test_record_trade_short_direction(self, store):
        """做空方向的交易应正确存取。"""
        trade = _make_trade(direction=TradeDirection.SHORT, pnl_amount=-100.0)
        store.record_trade(trade)
        trades = store.get_recent_trades(limit=1)
        assert trades[0].direction == TradeDirection.SHORT
        assert trades[0].pnl_amount == -100.0

    def test_record_trade_once_is_idempotent(self, store):
        """相同 sync_key 的外部成交只应写入一次。"""
        trade = _make_trade()

        assert store.record_trade_once(trade, "binance_user_order:BTCUSDT:1") is True
        assert store.record_trade_once(trade, "binance_user_order:BTCUSDT:1") is False

        trades = store.get_recent_trades(limit=10)
        assert len(trades) == 1


class TestComputeStats:
    """测试 compute_stats() 策略统计计算。"""

    def test_compute_stats_empty_list(self, store):
        """空列表应返回零值统计。"""
        stats = store.compute_stats([])
        assert stats.win_rate == 0.0
        assert stats.avg_pnl_ratio == 0.0
        assert stats.total_trades == 0
        assert stats.winning_trades == 0
        assert stats.losing_trades == 0

    def test_compute_stats_all_winning(self, store):
        """全部盈利时胜率应为 100%。"""
        trades = [_make_trade(pnl_amount=100.0) for _ in range(5)]
        stats = store.compute_stats(trades)
        assert stats.win_rate == 100.0
        assert stats.total_trades == 5
        assert stats.winning_trades == 5
        assert stats.losing_trades == 0

    def test_compute_stats_all_losing(self, store):
        """全部亏损时胜率应为 0%。"""
        trades = [_make_trade(pnl_amount=-50.0) for _ in range(4)]
        stats = store.compute_stats(trades)
        assert stats.win_rate == 0.0
        assert stats.total_trades == 4
        assert stats.winning_trades == 0
        assert stats.losing_trades == 4

    def test_compute_stats_mixed(self, store):
        """混合盈亏时胜率和平均盈亏比应正确计算。"""
        trades = [
            _make_trade(pnl_amount=100.0),
            _make_trade(pnl_amount=200.0),
            _make_trade(pnl_amount=-50.0),
            _make_trade(pnl_amount=-150.0),
        ]
        stats = store.compute_stats(trades)
        assert stats.win_rate == 50.0  # 2/4
        assert stats.total_trades == 4
        assert stats.winning_trades == 2
        assert stats.losing_trades == 2
        # 平均盈亏比 = (100 + 200 - 50 - 150) / 4 = 25.0
        assert stats.avg_pnl_ratio == 25.0

    def test_compute_stats_zero_pnl_counts_as_losing(self, store):
        """盈亏为零的交易应计入亏损笔数（pnl_amount <= 0）。"""
        trades = [_make_trade(pnl_amount=0.0)]
        stats = store.compute_stats(trades)
        assert stats.winning_trades == 0
        assert stats.losing_trades == 1

    def test_compute_stats_by_strategy(self, store):
        """应按 strategy_tag 分别统计胜率。"""
        trades = [
            _make_trade(pnl_amount=100.0, strategy_tag="trend"),
            _make_trade(pnl_amount=-50.0, strategy_tag="trend"),
            _make_trade(pnl_amount=30.0, strategy_tag="oversold"),
        ]

        stats = store.compute_stats_by_strategy(trades)

        assert stats["trend"].win_rate == 50.0
        assert stats["trend"].total_trades == 2
        assert stats["oversold"].win_rate == 100.0
        assert stats["oversold"].total_trades == 1


class TestReflectionLog:
    """测试 save_reflection() 和 get_latest_reflection() 的存取功能。"""

    def test_save_and_get_reflection(self, store):
        """存入反思日志后应能正确取回。"""
        reflection = ReflectionLog(
            created_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            win_rate=35.0,
            avg_pnl_ratio=-10.5,
            suggested_rating_threshold=7,
            suggested_risk_ratio=0.015,
            reasoning="胜率低于 40%，建议提高评级阈值",
        )
        store.save_reflection(reflection)
        latest = store.get_latest_reflection()

        assert latest is not None
        assert latest.win_rate == 35.0
        assert latest.avg_pnl_ratio == -10.5
        assert latest.suggested_rating_threshold == 7
        assert latest.suggested_risk_ratio == 0.015
        assert latest.reasoning == "胜率低于 40%，建议提高评级阈值"

    def test_get_latest_reflection_returns_most_recent(self, store):
        """get_latest_reflection 应返回最新的反思日志。"""
        r1 = ReflectionLog(
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            win_rate=30.0,
            avg_pnl_ratio=-5.0,
            suggested_rating_threshold=7,
            suggested_risk_ratio=0.01,
            reasoning="旧日志",
        )
        r2 = ReflectionLog(
            created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            win_rate=45.0,
            avg_pnl_ratio=15.0,
            suggested_rating_threshold=6,
            suggested_risk_ratio=0.02,
            reasoning="新日志",
        )
        store.save_reflection(r1)
        store.save_reflection(r2)

        latest = store.get_latest_reflection()
        assert latest is not None
        assert latest.reasoning == "新日志"
        assert latest.win_rate == 45.0

    def test_get_latest_reflection_empty_store(self, store):
        """空数据库应返回 None。"""
        assert store.get_latest_reflection() is None


class TestDatabaseInit:
    """测试数据库初始化行为。"""

    def test_creates_directory_if_not_exists(self, tmp_path):
        """如果数据库目录不存在，应自动创建。"""
        import os
        db_path = str(tmp_path / "subdir" / "nested" / "memory.db")
        s = MemoryStore(db_path=db_path)
        assert os.path.exists(db_path)
        s.close()
