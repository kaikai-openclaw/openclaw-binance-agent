import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "binance-trading"
    / "scripts"
    / "run_oversold_cron.py"
)
spec = importlib.util.spec_from_file_location("run_oversold_cron", SCRIPT_PATH)
cron = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cron)


class FakePosition:
    def __init__(self, raw):
        self.symbol = raw["symbol"]
        self.position_amt = float(raw["positionAmt"])
        self.entry_price = float(raw["entryPrice"])
        self.unrealized_pnl = float(raw["unRealizedProfit"])
        self.leverage = int(raw["leverage"])
        self.raw = raw


def test_build_position_snapshots_includes_pnl_leverage_and_margin_pct():
    positions = [
        FakePosition(
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.001",
                "entryPrice": "76750",
                "markPrice": "77230",
                "unRealizedProfit": "0.48",
                "notional": "77.23",
                "positionInitialMargin": "7.723",
                "leverage": "10",
                "liquidationPrice": "69000",
            }
        )
    ]

    result = cron.build_position_snapshots(
        total_balance=319.14,
        positions=positions,
        source_map={"BTCUSDT": "超跌short"},
    )

    assert result[0]["symbol"] == "BTCUSDT"
    assert result[0]["source"] == "超跌short"
    assert result[0]["direction"] == "long"
    assert result[0]["price_change_pct"] == 0.6254
    assert result[0]["margin_pct_of_equity"] == 2.4199
    assert result[0]["leverage"] == 10
    assert result[0]["roi_on_margin_pct"] == 6.2152


def test_build_protection_report_flags_duplicate_orders():
    positions = [
        {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_price": 76750.0,
        }
    ]
    orders = [
        {
            "symbol": "BTCUSDT",
            "type": "STOP_MARKET",
            "side": "SELL",
            "triggerPrice": "74447.5",
            "quantity": "0.001",
            "closePosition": "true",
            "algoId": "1",
        },
        {
            "symbol": "BTCUSDT",
            "type": "STOP_MARKET",
            "side": "SELL",
            "triggerPrice": "74447.5",
            "quantity": "0.001",
            "closePosition": "true",
            "algoId": "2",
        },
        {
            "symbol": "BTCUSDT",
            "type": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "triggerPrice": "81355",
            "quantity": "0.001",
            "closePosition": "true",
            "algoId": "3",
        },
    ]

    result = cron.build_protection_report(positions, orders)

    health = result["health"]["BTCUSDT"]
    assert health["has_stop_loss"] is True
    assert health["has_take_profit"] is True
    assert health["stop_loss_count"] == 2
    assert health["take_profit_count"] == 1
    assert health["duplicate_protection_orders"] == 1
    assert health["status"] == "warning"


def test_render_markdown_keeps_fixed_report_sections():
    report = {
        "mode": "4h",
        "status": "success",
        "scan": {
            "filter_summary": {
                "total_tickers": 609,
                "after_base_filter": 146,
                "after_oversold_filter": 2,
                "output_count": 2,
            },
            "candidates": [],
        },
        "analysis": {
            "analyzed_count": 2,
            "passed_count": 0,
            "rating_threshold": 6,
            "ratings": [],
        },
        "decision": {
            "action": "no_trade",
            "reason": "无币种通过 6 分评级门槛",
            "executed_count": 0,
            "risk_blocked_count": 0,
            "execution_failed_count": 0,
        },
        "triggered_trades": {
            "this_run": [],
            "closed_since_last_run_count": 0,
        },
        "positions": [],
        "new_positions": [],
        "rejected_symbols": [],
        "protection_orders": {"health": {}},
        "account": {
            "paper_mode": False,
            "total_balance": 319.14,
            "available_margin": 299.24,
            "total_unrealized_pnl": 3.99,
            "total_position_margin": 23.88,
            "total_position_margin_pct": 7.5,
            "daily_loss_pct": 0.0,
        },
        "risk": {
            "single_trade_margin_limit_pct": 20,
            "single_symbol_position_limit_pct": 30,
            "daily_loss_stop_pct": 5,
            "risk_status": "normal",
        },
        "warnings": [],
        "errors": [],
    }

    output = cron.render_markdown(report)

    assert "超跌交易报告" in output
    assert "扫描结果:" in output
    assert "分析评级:" in output
    assert "已触发/已执行交易:" in output
