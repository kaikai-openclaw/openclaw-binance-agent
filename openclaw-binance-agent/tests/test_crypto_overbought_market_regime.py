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
