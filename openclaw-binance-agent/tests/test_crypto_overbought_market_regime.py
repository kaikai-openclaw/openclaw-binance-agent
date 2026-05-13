from src.skills.crypto_overbought import ShortTermOverboughtSkill


class FakeStateStore:
    pass


class StrongTrendClient:
    def get_tickers_24hr(self):
        return [{"symbol": "BTCUSDT"}]

    def get_exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}

    def get_funding_rates_all(self):
        return []

    def get_klines(self, symbol, interval, limit):
        if symbol != "BTCUSDT":
            return []
        klines = []
        price = 100.0
        bars = 80 if interval == "4h" else 20
        step = 1.2 if interval == "4h" else 2.5
        for _ in range(bars):
            open_price = price
            close_price = price + step
            klines.append([0, open_price, close_price + 1, open_price - 1, close_price])
            price = close_price
        return klines


class BullMarketClient(StrongTrendClient):
    """BTC 在 MA200 以上（牛市结构），4h EMA 非强趋势（不触发 blocked）。"""

    def get_klines(self, symbol, interval, limit):
        if symbol != "BTCUSDT":
            return []
        klines = []
        price = 100.0
        if interval == "4h":
            # 震荡上涨，EMA5 ≈ EMA20，不触发 4h 强趋势 blocked
            for i in range(80):
                step = 0.5 if i % 3 == 0 else -0.3
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
        else:
            # 250 根日线，缓慢上涨确保 close > MA200
            for i in range(250):
                step = 0.05
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
        return klines


class BearMarketClient(StrongTrendClient):
    """BTC 在 MA200 以下（熊市结构），4h EMA 非强趋势。"""

    def get_klines(self, symbol, interval, limit):
        if symbol != "BTCUSDT":
            return []
        klines = []
        if interval == "4h":
            # 震荡，EMA5 ≈ EMA20，不触发 blocked
            price = 100.0
            for i in range(80):
                step = 0.3 if i % 2 == 0 else -0.3
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
        else:
            # 250 根日线从高点下跌，MA200 在高位，close < MA200
            price = 200.0
            for i in range(250):
                step = -0.3
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
        return klines


class UnknownMarketClient(StrongTrendClient):
    def get_klines(self, symbol, interval, limit):
        return []


class LowScoreTargetClient:
    def get_tickers_24hr(self):
        return [{
            "symbol": "ETHUSDT",
            "quoteVolume": "50000000",
            "priceChangePercent": "1",
            "lastPrice": "100",
        }]

    def get_exchange_info(self):
        return {
            "symbols": [{
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            }]
        }

    def get_funding_rates_all(self):
        return [{"symbol": "ETHUSDT", "lastFundingRate": "0.0001"}]

    def get_klines(self, symbol, interval, limit):
        if interval == "1h":
            return [[0, 100.0, 101.0, 99.0, 100.0] for _ in range(20)]
        return [[0, 100.0, 101.0, 99.0, 100.0] for _ in range(80)]


class MissingLastPriceClient(LowScoreTargetClient):
    def get_tickers_24hr(self):
        return [{
            "symbol": "ETHUSDT",
            "quoteVolume": "50000000",
            "priceChangePercent": "8",
        }]


def test_overbought_scan_blocks_btc_strong_uptrend():
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=StrongTrendClient(),
    )

    result = skill.run({})

    assert result["candidates"] == []
    assert result["market_regime"]["status"] == "blocked"
    assert "强趋势上涨" in result["market_regime"]["reason"]


def test_overbought_scan_blocks_unknown_market_state():
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=UnknownMarketClient(),
    )

    result = skill.run({})

    assert result["candidates"] == []
    assert result["market_regime"]["status"] == "unknown"
    assert result["filter_summary"]["output_count"] == 0


def test_target_symbols_do_not_bypass_min_score():
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=LowScoreTargetClient(),
    )

    result = skill.run({
        "ignore_market_regime": True,
        "target_symbols": ["ETHUSDT"],
    })

    assert result["candidates"] == []
    assert result["filter_summary"]["after_overbought_filter"] == 0


def test_4h_confirmation_requires_momentum_signal():
    structural_only = ShortTermOverboughtSkill._build_4h_confirmation(
        closes=[100.0] * 20,
        highs=[101.0] * 20,
        lows=[99.0] * 20,
        klines_1h=[],
        current_price=100.0,
        rsi_1h=None,
        rsi_1h_trend=None,
        macd_divergence=True,
        rsi_divergence=False,
        volume_divergence=True,
        kdj_dead_cross=False,
        drawdown_from_high=None,
    )
    passed = ShortTermOverboughtSkill._build_4h_confirmation(
        closes=[100.0] * 19 + [99.0],
        highs=[101.0] * 20,
        lows=[99.0] * 20,
        klines_1h=[
            [0, 102.0, 103.0, 100.0, 102.0],
            [0, 101.0, 102.0, 99.5, 100.5],
            [0, 100.0, 100.5, 98.5, 99.0],
        ],
        current_price=99.0,
        rsi_1h=72.0,
        rsi_1h_trend=-2.0,
        macd_divergence=True,
        rsi_divergence=False,
        volume_divergence=False,
        kdj_dead_cross=False,
        drawdown_from_high=-3.0,
    )

    assert structural_only["signal_count"] == 2
    assert structural_only["momentum_count"] == 0
    assert structural_only["passed"] is False
    assert passed["passed"] is True
    assert passed["momentum_count"] >= 1
    assert passed["strong"] is True


def test_missing_last_price_fails_closed():
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=MissingLastPriceClient(),
    )

    result = skill.run({
        "ignore_market_regime": True,
        "target_symbols": ["ETHUSDT"],
        "min_overbought_score": 0,
    })

    assert result["candidates"] == []
    assert result["filter_summary"]["after_overbought_filter"] == 0


def test_market_regime_raises_threshold_when_btc_above_ma200():
    """BTC 日线 close > MA200 时，score_adjustment = 15，门槛提高但不阻断。"""
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=BullMarketClient(),
    )

    regime = skill._get_market_regime({})

    assert regime["status"] == "enabled"
    assert regime["btc_above_ma200"] is True
    assert regime["score_adjustment"] == 15
    assert regime["ma200"] is not None
    assert regime["ma200"] > 0


def test_market_regime_no_adjustment_when_btc_below_ma200():
    """BTC 日线 close < MA200 时，score_adjustment = 0。"""
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=BearMarketClient(),
    )

    regime = skill._get_market_regime({})

    assert regime["status"] == "enabled"
    assert regime["btc_above_ma200"] is False
    assert regime["score_adjustment"] == 0


def test_market_regime_ma200_stacks_with_5d_rally():
    """BTC > MA200 + 5 日涨幅 > 8% 时，score_adjustment = 25（15 + 10），且为 cautious。"""

    class BullRallyClient(BullMarketClient):
        """BTC 在 MA200 以上，且最近 5 天暴涨。"""

        def get_klines(self, symbol, interval, limit):
            if symbol != "BTCUSDT":
                return []
            if interval == "4h":
                klines = []
                price = 100.0
                for i in range(80):
                    step = 0.5 if i % 3 == 0 else -0.3
                    close_price = price + step
                    klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                    price = close_price
                return klines
            # 1d: 前 245 天缓涨，最后 5 天暴涨
            klines = []
            price = 100.0
            for i in range(245):
                step = 0.05
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
            # 最后 5 天暴涨 30%（远超 8% 阈值）
            for i in range(5):
                step = price * 0.06
                close_price = price + step
                klines.append([0, price, close_price + 0.5, price - 0.5, close_price])
                price = close_price
            return klines

    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=BullRallyClient(),
    )

    regime = skill._get_market_regime({})

    assert regime["status"] == "cautious"
    assert regime["btc_above_ma200"] is True
    assert regime["score_adjustment"] == 25  # 15 (MA200) + 10 (5d rally)
    assert "强势上涨" in regime["reason"]


def test_market_regime_ma200_insufficient_data_skips():
    """日线数据不足 200 根时跳过 MA200 检查，score_adjustment = 0。"""
    skill = ShortTermOverboughtSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=StrongTrendClient(),  # 只返回 20 根日线
    )

    regime = skill._get_market_regime({})

    assert regime["status"] in ("enabled", "blocked", "cautious", "unknown")
    # StrongTrendClient 日线只有 20 根，不够 200 根，MA200 跳过
    assert regime.get("btc_above_ma200", False) is False
