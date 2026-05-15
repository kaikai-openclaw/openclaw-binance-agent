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


class TrimmedCurrentCandleClient(DummyClient):
    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        rows = super().get_klines(symbol, interval, limit)
        if symbol in self.symbol_klines and limit == 3 and len(rows) >= 3:
            return rows[-3:-1]
        return rows


class BTCRealtimeRecoveryClient(DummyClient):
    def __init__(
        self,
        btc_4h_klines: List[list],
        btc_1h_klines: List[list],
        symbol_klines: dict[str, List[list]],
        btc_last_price: float,
    ) -> None:
        super().__init__(btc_4h_klines, symbol_klines)
        self.btc_1h_klines = btc_1h_klines
        self.btc_last_price = btc_last_price

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        if symbol == "BTCUSDT" and interval == "1h":
            return self.btc_1h_klines[-limit:]
        return super().get_klines(symbol, interval, limit)


class BTCTrimmedRealtimeRecoveryClient(BTCRealtimeRecoveryClient):
    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        if symbol == "BTCUSDT" and interval == "1h":
            rows = self.btc_1h_klines[-limit:]
            return rows[:-1] if len(rows) > 1 else rows
        return super().get_klines(symbol, interval, limit)


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


def test_market_regime_downgrades_weak_btc_when_realtime_and_1h_recover() -> None:
    closes = [120.0 - i * 0.2 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    tickers.append({
        "symbol": "BTCUSDT",
        "priceChangePercent": "0.5",
        "quoteVolume": "1000000000",
        "lastPrice": "106.0",
    })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        BTCRealtimeRecoveryClient(
            _make_klines(closes),
            _make_klines([100.0 + i * 0.2 for i in range(20)]),
            klines_by_symbol,
            btc_last_price=106.0,
        ),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["score_adjustment"] == 15
    assert result["btc_regime_downgraded_from_blocked"] is True
    assert result["btc_1h_recovery"] is True


def test_market_regime_keeps_weak_btc_blocked_when_1h_not_recovered() -> None:
    closes = [120.0 - i * 0.2 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    tickers.append({
        "symbol": "BTCUSDT",
        "priceChangePercent": "0.5",
        "quoteVolume": "1000000000",
        "lastPrice": "106.0",
    })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        BTCRealtimeRecoveryClient(
            _make_klines(closes),
            _make_klines([110.0 - i * 0.2 for i in range(20)]),
            klines_by_symbol,
            btc_last_price=106.0,
        ),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert result["btc_realtime_recovery"] is True
    assert result["btc_1h_recovery"] is False


def test_market_regime_low_breadth_blocks_even_when_btc_realtime_recovers() -> None:
    closes = [120.0 - i * 0.2 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(30, 70)
    tickers.append({
        "symbol": "BTCUSDT",
        "priceChangePercent": "0.5",
        "quoteVolume": "1000000000",
        "lastPrice": "106.0",
    })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        BTCRealtimeRecoveryClient(
            _make_klines(closes),
            _make_klines([100.0 + i * 0.2 for i in range(20)]),
            klines_by_symbol,
            btc_last_price=106.0,
        ),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["breadth_pct_4h"] < 35.0
    assert result["btc_1h_recovery"] is True
    assert result["score_adjustment"] == 20


def test_market_regime_uses_20_closed_1h_klines_after_current_trim() -> None:
    closes = [120.0 - i * 0.2 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    tickers.append({
        "symbol": "BTCUSDT",
        "priceChangePercent": "0.5",
        "quoteVolume": "1000000000",
        "lastPrice": "106.0",
    })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        BTCTrimmedRealtimeRecoveryClient(
            _make_klines(closes),
            _make_klines([100.0 + i * 0.2 for i in range(21)]),
            klines_by_symbol,
            btc_last_price=106.0,
        ),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["btc_1h_ema20"] is not None


def test_market_regime_blocks_low_market_breadth_when_btc_drops() -> None:
    # BTC 最近一根 4h 收阴但 EMA 未弱到阻断，广度 < 35% → blocked
    # 用缓慢上涨序列让 EMA 通过，但最后两根做成收阴
    closes = [100.0 + i * 0.5 for i in range(78)] + [138.0, 136.0]
    tickers, klines_by_symbol = _make_breadth_fixture(30, 70)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert "4h上涨广度" in result["reason"]
    assert result["breadth_pct_4h"] == 30.0
    assert result["breadth_pct"] == 30.0
    assert result["score_adjustment"] == 20


def test_market_regime_cautious_low_breadth_when_btc_reversal_up() -> None:
    # BTC 最近一根 4h 收阳（closes 递增），广度 < 35% → cautious（反转初期豁免）
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(30, 70)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert "反转初期" in result["reason"]
    assert result["breadth_pct_4h"] == 30.0
    assert result["score_adjustment"] == 20


def test_market_regime_hard_blocks_below_20_pct_breadth_standalone() -> None:
    """广度 < 20% 自由落体硬阻断"""
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(15, 85)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert result["breadth_pct_4h"] == 15.0
    assert "自由落体" in result["reason"]


def test_market_regime_hard_blocks_below_20_pct_breadth_btc_weak_recovery() -> None:
    """广度 < 20% 即使 BTC 实时+1h 恢复仍硬阻断"""
    closes = [120.0 - i * 0.2 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(15, 85)
    tickers.append({
        "symbol": "BTCUSDT",
        "priceChangePercent": "0.5",
        "quoteVolume": "1000000000",
        "lastPrice": "106.0",
    })
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        BTCRealtimeRecoveryClient(
            _make_klines(closes),
            _make_klines([100.0 + i * 0.2 for i in range(20)]),
            klines_by_symbol,
            btc_last_price=106.0,
        ),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "blocked"
    assert result["breadth_pct_4h"] < 20.0
    assert result["btc_regime_downgraded_from_blocked"] is False


def test_market_regime_cautious_at_20_pct_breadth_boundary() -> None:
    """广度正好 20% 属于 20-35% 层级，cautious"""
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(20, 80)
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        DummyClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "cautious"
    assert result["breadth_pct_4h"] == 20.0
    assert result["score_adjustment"] == 20


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


def test_market_regime_fetches_extra_kline_for_closed_4h_breadth() -> None:
    closes = [100.0 + i * 0.1 for i in range(80)]
    tickers, klines_by_symbol = _make_breadth_fixture(60, 40)
    for symbol, klines in list(klines_by_symbol.items()):
        last_close = float(klines[-1][4])
        klines_by_symbol[symbol] = klines + _make_klines([last_close])[-1:]
    skill = ShortTermReversalSkill(
        None,
        {},
        {},
        TrimmedCurrentCandleClient(_make_klines(closes), klines_by_symbol),
    )

    result = skill._get_market_regime({}, tickers=tickers)

    assert result["status"] == "enabled"
    assert result["breadth_sample_size"] == 100
    assert result["breadth_pct_4h"] == 60.0


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


def test_4h_vol_1h_confirm_constants():
    from src.skills.crypto_reversal import (
        VOL_1H_CONFIRM_BONUS,
        VOL_1H_CONFIRM_STRONG,
        VOL_1H_CONFIRM_STRONG_BONUS,
        VOL_1H_CONFIRM_THRESHOLD,
    )

    assert VOL_1H_CONFIRM_THRESHOLD == 1.5
    assert VOL_1H_CONFIRM_STRONG == 2.0
    assert VOL_1H_CONFIRM_BONUS == 3
    assert VOL_1H_CONFIRM_STRONG_BONUS == 5


class VolConfirmClient(DummyClient):
    """4h klines 低成交量，1h klines 高成交量 → vol_intraday_strength > 2.0。"""

    def __init__(self, btc_klines, symbol_klines=None, exchange_symbols=None):
        super().__init__(btc_klines, symbol_klines, exchange_symbols)
        self._1h_klines = None

    def set_1h_klines(self, klines):
        self._1h_klines = klines

    def get_klines(self, symbol, interval, limit):
        if interval == "1h" and self._1h_klines is not None:
            return self._1h_klines[-limit:]
        return super().get_klines(symbol, interval, limit)

    def get_tickers_24hr(self):
        return [{
            "symbol": "TESTUSDT",
            "priceChangePercent": "2.0",
            "quoteVolume": "25000000",
            "lastPrice": "110.0",
        }]

    def get_exchange_info(self):
        return {
            "symbols": [{
                "symbol": "TESTUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            }]
        }

    def get_funding_rates_all(self):
        return []


def test_4h_vol_1h_confirm_adds_bonus(monkeypatch) -> None:
    """vol_intraday_strength >= 2.0 时，评分增加 VOL_1H_CONFIRM_STRONG_BONUS。"""
    closes = [100.0 + i * 0.1 for i in range(80)]
    klines_4h = _make_klines(closes)
    # 4h klines 的 volume=1000，1h klines 的 volume=3000
    # vol_1h_recent = avg(3000*4) = 3000, vol_4h_hist = avg(1000*7) ≈ 1000
    # vol_intraday_strength = 3000 / 1000 = 3.0 > 2.0 → +5 分
    klines_1h = _make_klines([100.0] * 20)
    for k in klines_1h:
        k[5] = 3000.0  # 3x of 4h volume

    client = VolConfirmClient(klines_4h, {"TESTUSDT": klines_4h})
    client.set_1h_klines(klines_1h)
    skill = ShortTermReversalSkill(None, {}, {}, client)

    captured_score: dict = {}

    def fake_calc(*args, **kwargs):
        result = {
            "reversal_score": 60,
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
        captured_score.update(result)
        return result

    monkeypatch.setattr("src.skills.crypto_reversal.calc_reversal_score", fake_calc)

    result = skill.run({
        "ignore_market_regime": True,
        "min_reversal_score": 55,
    })

    # 60 + 5 (strong vol confirm) = 65, should pass min_score=55
    assert len(result["candidates"]) >= 1
    c = result["candidates"][0]
    assert c["vol_1h_confirm_bonus"] == 5
