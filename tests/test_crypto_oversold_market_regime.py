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
