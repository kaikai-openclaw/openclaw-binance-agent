from typing import Any, List

from src.skills.crypto_reversal import ShortTermReversalSkill


class DummyClient:
    def __init__(
        self,
        btc_klines: List[list],
        symbol_klines: dict[str, List[list]] | None = None,
        exchange_symbols: list[dict] | None = None,
    ) -> None:
        self.btc_klines = btc_klines
        self.symbol_klines = symbol_klines or {}
        self.exchange_symbols = exchange_symbols

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        if symbol in self.symbol_klines:
            return self.symbol_klines[symbol][-limit:]
        return self.btc_klines[-limit:]

    def get_exchange_info(self) -> dict:
        if self.exchange_symbols is not None:
            return {"symbols": self.exchange_symbols}
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                }
                for symbol in self.symbol_klines
            ]
        }


class TargetChaseClient(DummyClient):
    def get_tickers_24hr(self) -> list[dict]:
        return [{
            "symbol": "HIGHUSDT",
            "priceChangePercent": "20.0",
            "quoteVolume": "25000000",
            "lastPrice": "100.0",
        }]

    def get_exchange_info(self) -> dict:
        return {
            "symbols": [{
                "symbol": "HIGHUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            }]
        }

    def get_funding_rates_all(self) -> list:
        return []


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


def _make_breadth_fixture(up_count: int, down_count: int) -> tuple[list, dict[str, List[list]]]:
    tickers = []
    klines_by_symbol = {}
    for idx in range(up_count):
        symbol = f"UP{idx}USDT"
        tickers.append({
            "symbol": symbol,
            "priceChangePercent": "1.0",
            "quoteVolume": "25000000",
        })
        klines_by_symbol[symbol] = _make_klines([100.0, 101.0])
    for idx in range(down_count):
        symbol = f"DOWN{idx}USDT"
        tickers.append({
            "symbol": symbol,
            "priceChangePercent": "-1.0",
            "quoteVolume": "25000000",
        })
        klines_by_symbol[symbol] = _make_klines([100.0, 99.0])
    return tickers, klines_by_symbol


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
        {
            "symbol": f"COIN{i}USDT",
            "priceChangePercent": "1.0",
            "quoteVolume": "25000000",
        }
        for i in range(100)
    ]

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert "BTC 4h 短期趋势偏弱" in result["reason"]


def test_market_regime_blocks_low_market_breadth() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(30, 70)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert "4h上涨广度" in result["reason"]
    assert result["breadth_pct_4h"] == 30.0
    assert result["breadth_pct"] == 30.0


def test_market_regime_cautious_for_borderline_4h_breadth() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(40, 60)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["breadth_status"] == "cautious"
    assert result["score_adjustment"] == 10
    assert result["breadth_pct_4h"] == 40.0


def test_market_regime_uses_4h_breadth_over_24h_breadth() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    for ticker in tickers:
        ticker["priceChangePercent"] = "-1.0"
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "enabled"
    assert result["breadth_pct_4h"] == 60.0
    assert result["breadth_pct_24h"] == 0.0


def test_market_regime_ignores_low_volume_breadth_symbols() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(50, 0)
    low_volume_tickers, low_volume_klines = _make_breadth_fixture(0, 50)
    for ticker in low_volume_tickers:
        ticker["quoteVolume"] = "1000000"
    tickers.extend(low_volume_tickers)
    klines_by_symbol.update(low_volume_klines)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "enabled"
    assert result["breadth_sample_size"] == 50
    assert result["breadth_pct_4h"] == 100.0


def test_market_regime_ignores_non_tradable_breadth_symbols() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(50, 0)
    invalid_tickers, invalid_klines = _make_breadth_fixture(0, 50)
    tickers.extend(invalid_tickers)
    klines_by_symbol.update(invalid_klines)
    exchange_symbols = []
    for ticker in tickers:
        symbol = ticker["symbol"]
        if symbol.startswith("UP"):
            exchange_symbols.append({
                "symbol": symbol,
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            })
        else:
            exchange_symbols.append({
                "symbol": symbol,
                "status": "BREAK",
                "contractType": "CURRENT_QUARTER",
                "quoteAsset": "USDT",
            })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol, exchange_symbols),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "enabled"
    assert result["breadth_sample_size"] == 50
    assert result["breadth_pct_24h"] == 100.0
    assert result["breadth_pct_4h"] == 100.0


def test_market_regime_blocks_insufficient_breadth_sample() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(10, 10)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert "样本不足" in result["reason"]


def test_market_regime_cautious_when_major_breadth_is_weak() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    major_symbols = ["ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    for symbol in major_symbols:
        tickers.append({
            "symbol": symbol,
            "priceChangePercent": "-1.0",
            "quoteVolume": "25000000",
        })
        klines_by_symbol[symbol] = _make_klines([100.0, 99.0])
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["major_breadth_pct_4h"] == 0.0
    assert result["score_adjustment"] == 10


def test_market_regime_cautious_not_blocked_when_both_breadths_are_weak() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(40, 60)
    major_symbols = ["ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    for symbol in major_symbols:
        tickers.append({
            "symbol": symbol,
            "priceChangePercent": "-1.0",
            "quoteVolume": "25000000",
        })
        klines_by_symbol[symbol] = _make_klines([100.0, 99.0])
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["breadth_pct_4h"] < 45.0
    assert result["major_breadth_pct_4h"] == 0.0
    assert result["score_adjustment"] == 15


def test_market_regime_blocks_insufficient_btc_klines() -> None:
    skill = ShortTermReversalSkill(None, {}, {}, DummyClient(_make_klines([100.0] * 10)))

    result = skill._get_market_regime({}, tickers=[])

    assert result["status"] == "blocked"
    assert result["reason"] == "insufficient_market_klines"


def test_target_symbols_do_not_bypass_chasing_filter(monkeypatch) -> None:
    klines = _make_klines([100.0] * 80)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        TargetChaseClient(klines, {"HIGHUSDT": klines}),
    )

    def fake_calc_reversal_score(*args: Any, **kwargs: Any) -> dict:
        return {
            "reversal_score": 100,
            "volume_surge_score": 0,
            "volume_surge_ratio": 1.0,
            "price_stable_score": 0,
            "ma_turn_score": 0,
            "ma_turn_detail": "",
            "funding_reversal_score": 0,
            "funding_rate": None,
            "macd_reversal_score": 0,
            "macd_detail": "",
            "dist_bottom_pct": 5.0,
            "dist_bottom_score": 35,
            "prior_drop_pct": -10.0,
            "prior_drop_score": 18,
            "kdj_score": 25,
            "shadow_score": 0,
            "signal_details": "test",
        }

    monkeypatch.setattr(
        "src.skills.crypto_reversal.calc_reversal_score",
        fake_calc_reversal_score,
    )

    result = skill.run({
        "ignore_market_regime": True,
        "target_symbols": ["HIGHUSDT"],
        "min_reversal_score": 55,
    })

    assert result["candidates"] == []
    assert result["filter_summary"]["after_reversal_filter"] == 0


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
