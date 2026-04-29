"""
Binance 服务端成交同步测试。
"""

from datetime import datetime, timezone

from src.infra.memory_store import MemoryStore
from src.infra.trade_sync import BinanceTradeSyncer
from src.models.types import TradeDirection


class FakeBinanceClient:
    def __init__(self, trades_by_symbol):
        self.trades_by_symbol = trades_by_symbol
        self.calls = []

    def get_user_trades(self, symbol, start_time=None, end_time=None, limit=1000):
        self.calls.append((symbol, start_time, end_time, limit))
        return self.trades_by_symbol.get(symbol, [])


def test_sync_closed_long_trade_from_realized_pnl(tmp_path):
    """SELL 平仓且 realizedPnl 非零时，应同步为做多平仓记录。"""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        client = FakeBinanceClient({
            "BTCUSDT": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 1001,
                    "side": "SELL",
                    "price": "110",
                    "qty": "2",
                    "realizedPnl": "20",
                    "time": 1710000000000,
                },
            ]
        })
        syncer = BinanceTradeSyncer(client, store)

        synced = syncer.sync_closed_trades(
            ["BTCUSDT"],
            metadata_by_symbol={
                "BTCUSDT": {"rating_score": 8, "position_size_pct": 12.5}
            },
        )

        assert synced == 1
        trades = store.get_recent_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.symbol == "BTCUSDT"
        assert trade.direction == TradeDirection.LONG
        assert trade.entry_price == 100.0
        assert trade.exit_price == 110.0
        assert trade.pnl_amount == 20.0
        assert trade.rating_score == 8
        assert trade.position_size_pct == 12.5
        assert trade.closed_at == datetime.fromtimestamp(
            1710000000000 / 1000,
            tz=timezone.utc,
        )
    finally:
        store.close()


def test_sync_closed_trade_is_idempotent_by_order_id(tmp_path):
    """同一个 Binance orderId 重复同步时，只写入一次 MemoryStore。"""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        client = FakeBinanceClient({
            "ETHUSDT": [
                {
                    "symbol": "ETHUSDT",
                    "orderId": 2001,
                    "side": "BUY",
                    "price": "90",
                    "qty": "1",
                    "realizedPnl": "10",
                    "time": 1710000000000,
                },
            ]
        })
        syncer = BinanceTradeSyncer(client, store)

        assert syncer.sync_closed_trades(["ETHUSDT"]) == 1
        assert syncer.sync_closed_trades(["ETHUSDT"]) == 0

        trades = store.get_recent_trades()
        assert len(trades) == 1
        assert trades[0].direction == TradeDirection.SHORT
        assert trades[0].entry_price == 100.0
        assert trades[0].exit_price == 90.0
    finally:
        store.close()


def test_sync_groups_partial_fills_by_order_id(tmp_path):
    """同一个平仓订单的多笔成交应聚合成一条交易记录。"""
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        client = FakeBinanceClient({
            "SOLUSDT": [
                {
                    "symbol": "SOLUSDT",
                    "orderId": 3001,
                    "side": "SELL",
                    "price": "110",
                    "qty": "1",
                    "realizedPnl": "10",
                    "time": 1710000000000,
                },
                {
                    "symbol": "SOLUSDT",
                    "orderId": 3001,
                    "side": "SELL",
                    "price": "112",
                    "qty": "1",
                    "realizedPnl": "12",
                    "time": 1710000001000,
                },
            ]
        })
        syncer = BinanceTradeSyncer(client, store)

        assert syncer.sync_closed_trades(["SOLUSDT"]) == 1
        trade = store.get_recent_trades()[0]
        assert trade.exit_price == 111.0
        assert trade.entry_price == 100.0
        assert trade.pnl_amount == 22.0
    finally:
        store.close()
