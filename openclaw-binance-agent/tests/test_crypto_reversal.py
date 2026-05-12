from typing import Any, List

from src.skills.crypto_reversal import ShortTermReversalSkill


class DummyClient:
    def __init__(self, btc_klines: List[list]) -> None:
        self.btc_klines = btc_klines

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        return self.btc_klines[-limit:]


def _make_klines(closes: List[float]) -> List[list]:
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            [
                idx * 14_400_000,
                close,
                close * 1.01,
                close * 0.99,
                close,
                1000.0,
            ]
        )
    return rows


def test_reversal_4h_default_min_score_is_55(monkeypatch) -> None:
    skill = ShortTermReversalSkill(None, {}, {}, DummyClient(_make_klines([100.0] * 80)))
    captured: dict[str, Any] = {}

    def fake_run_scan(input_data: dict, *args: Any, **kwargs: Any) -> dict:
        captured.update(input_data)
        return {"candidates": [], "filter_summary": {}}

    monkeypatch.setattr(skill, "_run_scan", fake_run_scan)

    skill.run({"trigger_time": "2026-05-12T00:00:00Z"})

    assert captured["min_reversal_score"] == 55


def test_market_regime_blocks_weak_btc_4h_trend() -> None:
    closes = [120.0 - i * 0.2 for i in range(80)]
    skill = ShortTermReversalSkill(None, {}, {}, DummyClient(_make_klines(closes)))
    tickers = [
        {"symbol": f"COIN{i}USDT", "priceChangePercent": "1.0"}
        for i in range(100)
    ]

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert "BTC 4h 短期趋势偏弱" in result["reason"]


def test_market_regime_blocks_low_market_breadth() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    skill = ShortTermReversalSkill(None, {}, {}, DummyClient(_make_klines(closes)))
    tickers = [
        {"symbol": f"UP{i}USDT", "priceChangePercent": "1.0"}
        for i in range(40)
    ] + [
        {"symbol": f"DOWN{i}USDT", "priceChangePercent": "-1.0"}
        for i in range(60)
    ]

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert "上涨广度" in result["reason"]


def test_market_regime_blocks_insufficient_btc_klines() -> None:
    skill = ShortTermReversalSkill(None, {}, {}, DummyClient(_make_klines([100.0] * 10)))

    result = skill._get_market_regime({}, tickers=[])

    assert result["status"] == "blocked"
    assert result["reason"] == "insufficient_market_klines"


def test_4h_confirmation_requires_ideal_distance() -> None:
    closes = [100.0 + i * 0.2 for i in range(30)]
    highs = [x * 1.01 for x in closes]
    lows = [x * 0.99 for x in closes]

    result = ShortTermReversalSkill._build_4h_confirmation(
        closes=closes,
        highs=highs,
        lows=lows,
        current_price=max(highs[-3:-1]) * 1.01,
        dist_bottom_pct=2.5,
        kdj_score=25,
        rsi_1h=None,
    )

    assert result["passed"] is False
    assert result["cond_kdj"] is True
    assert result["cond_dist"] is False


def test_4h_confirmation_accepts_kdj_breakout() -> None:
    closes = [100.0 + i * 0.2 for i in range(30)]
    highs = [x * 1.01 for x in closes]
    lows = [x * 0.99 for x in closes]

    result = ShortTermReversalSkill._build_4h_confirmation(
        closes=closes,
        highs=highs,
        lows=lows,
        current_price=max(highs[-3:-1]) * 1.01,
        dist_bottom_pct=6.0,
        kdj_score=25,
        rsi_1h=None,
    )

    assert result["passed"] is True
    assert result["breakout"] is True
