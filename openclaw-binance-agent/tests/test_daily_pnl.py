from datetime import datetime, timezone

from src.infra.daily_pnl import calculate_daily_realized_pnl, utc_day_start_ms


class FakeClient:
    def __init__(self, trades_by_symbol):
        self.trades_by_symbol = trades_by_symbol
        self.calls = []

    def get_user_trades(self, symbol, start_time=None, end_time=None, limit=1000):
        self.calls.append((symbol, start_time, end_time, limit))
        return self.trades_by_symbol.get(symbol, [])


def test_utc_day_start_ms_uses_current_utc_day():
    now = datetime(2026, 4, 29, 16, 30, tzinfo=timezone.utc)

    result = utc_day_start_ms(now)

    assert result == int(datetime(2026, 4, 29, tzinfo=timezone.utc).timestamp() * 1000)


def test_calculate_daily_realized_pnl_sums_user_trade_realized_pnl():
    client = FakeClient({
        "BTCUSDT": [
            {"realizedPnl": "12.5"},
            {"realizedPnl": "-2.5"},
        ],
        "ETHUSDT": [
            {"realizedPnl": "-5"},
            {"realizedPnl": "0"},
        ],
    })

    result = calculate_daily_realized_pnl(
        client,
        ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        start_time_ms=123,
    )

    assert result == 5.0
    assert client.calls == [
        ("BTCUSDT", 123, None, 1000),
        ("ETHUSDT", 123, None, 1000),
    ]
