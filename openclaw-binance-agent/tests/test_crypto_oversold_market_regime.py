from src.skills.crypto_oversold import ShortTermOversoldSkill


class FakeStateStore:
    pass


class FakeClient:
    def get_tickers_24hr(self):
        return [{"symbol": "BTCUSDT"}]

    def get_exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}

    def get_premium_index(self):
        return []

    def get_funding_rates_all(self):
        return []

    def get_klines(self, symbol, interval, limit):
        if symbol == "BTCUSDT":
            klines = []
            price = 100.0
            for _ in range(70):
                price -= 1
                klines.append([0, 0, price + 1, price - 1, price])
            return klines
        return []


class WeakTrendClient(FakeClient):
    def get_klines(self, symbol, interval, limit):
        if symbol == "BTCUSDT":
            klines = []
            price = 120.0
            for _ in range(70):
                price -= 0.2
                klines.append([0, price + 0.1, price + 1, price - 1, price])
            return klines
        return []


class UnknownMarketClient(FakeClient):
    def get_klines(self, symbol, interval, limit):
        return []


class QualityFilterClient:
    def get_tickers_24hr(self):
        return [{
            "symbol": "ETHUSDT",
            "quoteVolume": "50000000",
            "priceChangePercent": "-5",
            "bidPrice": "99",
            "askPrice": "101",
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
        price = 100.0
        klines = []
        for _ in range(80):
            klines.append([0, 0, price + 1, price - 1, price])
            price += 0.1
        return klines


class ChasingClient(QualityFilterClient):
    def get_tickers_24hr(self):
        return [{
            "symbol": "ETHUSDT",
            "quoteVolume": "50000000",
            "priceChangePercent": "15",
            "bidPrice": "99",
            "askPrice": "99.1",
            "lastPrice": "100",
        }]


def test_oversold_scan_blocks_waterfall_market():
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=FakeClient(),
    )

    result = skill.run({})

    assert result["candidates"] == []
    assert result["market_regime"]["status"] == "blocked"
    assert result["filter_summary"]["output_count"] == 0


def test_oversold_scan_blocks_weak_btc_trend():
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=WeakTrendClient(),
    )

    result = skill.run({})

    assert result["candidates"] == []
    assert result["market_regime"]["status"] == "blocked"
    assert "weak trend" in result["market_regime"]["reason"]


def test_oversold_scan_blocks_unknown_market_state():
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=UnknownMarketClient(),
    )

    result = skill.run({})

    assert result["candidates"] == []
    assert result["market_regime"]["status"] == "unknown"
    assert result["filter_summary"]["output_count"] == 0


def test_oversold_scan_filters_wide_spread_symbols():
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=QualityFilterClient(),
    )

    result = skill.run({
        "ignore_market_regime": True,
        "max_spread_pct": 0.1,
    })

    assert result["candidates"] == []
    assert result["filter_summary"]["after_oversold_filter"] == 0


def test_target_symbols_do_not_bypass_chasing_filter():
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=ChasingClient(),
    )

    result = skill.run({
        "ignore_market_regime": True,
        "target_symbols": ["ETHUSDT"],
    })

    assert result["candidates"] == []
    assert result["filter_summary"]["after_oversold_filter"] == 0


def test_4h_confirmation_requires_momentum_signal():
    structural_only = ShortTermOversoldSkill._build_4h_confirmation(
        current_price=96.0,
        lows=[90.0, 95.0],
        klines_1h=[],
        rsi_1h=None,
        rsi_1h_trend=None,
        support_distance_pct=2.0,
        panic_selling_detected=False,
    )
    passed = ShortTermOversoldSkill._build_4h_confirmation(
        current_price=101.0,
        lows=[90.0, 95.0],
        klines_1h=[
            [0, 0, 99.0, 90.0, 95.0],
            [0, 0, 100.0, 95.0, 98.0],
            [0, 0, 100.5, 98.0, 100.0],
        ],
        rsi_1h=35.0,
        rsi_1h_trend=1.0,
        support_distance_pct=2.0,
        panic_selling_detected=False,
    )

    assert structural_only["signal_count"] == 2
    assert structural_only["momentum_count"] == 0
    assert structural_only["passed"] is False
    assert passed["passed"] is True
    assert passed["momentum_count"] >= 1
    assert passed["strong"] is True
