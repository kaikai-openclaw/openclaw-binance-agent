from src.infra.strategy_backtest import backtest_long_only_candidates


def test_backtest_long_only_candidates_calculates_basic_stats():
    candidates = [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]
    klines = {
        "BTCUSDT": [
            [0, 0, 100, 100, 100],
            [0, 0, 106, 99, 105],
        ],
        "ETHUSDT": [
            [0, 0, 100, 100, 100],
            [0, 0, 101, 94, 95],
        ],
    }

    summary, trades = backtest_long_only_candidates(
        candidates,
        klines,
        hold_bars=1,
    )

    assert len(trades) == 2
    assert summary.trade_count == 2
    assert summary.win_count == 1
    assert summary.loss_count == 1
    assert summary.win_rate == 50.0
    assert summary.avg_pnl_pct == 0.0


def test_backtest_applies_stop_loss_and_take_profit():
    candidates = [{"symbol": "BTCUSDT"}]
    klines = {
        "BTCUSDT": [
            [0, 0, 100, 100, 100],
            [0, 0, 110, 98, 109],
        ],
    }

    summary, trades = backtest_long_only_candidates(
        candidates,
        klines,
        hold_bars=1,
        stop_loss_pct=5,
        take_profit_pct=6,
    )

    assert trades[0].exit_price == 106.0
    assert trades[0].pnl_pct == 6.0
    assert summary.expectancy_pct == 6.0
