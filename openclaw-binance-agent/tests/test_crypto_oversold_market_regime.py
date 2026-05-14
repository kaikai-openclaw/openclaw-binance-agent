from src.skills.crypto_oversold import ShortTermOversoldSkill


class FakeStateStore:
    pass


class FakeClient:
    def get_tickers_24hr(self):
        return [{"symbol": "BTCUSDT"}]

    def get_exchange_info(self):
        return {
            "symbols": [{
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            }]
        }

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


def _make_breadth_fixture(up_count, down_count):
    tickers = []
    klines_by_symbol = {}
    symbols_info = []
    for idx in range(up_count):
        symbol = f"UP{idx}USDT"
        tickers.append({
            "symbol": symbol,
            "quoteVolume": "25000000",
            "priceChangePercent": "1",
        })
        klines_by_symbol[symbol] = [
            [0, 0, 101.0, 99.0, 100.0],
            [0, 0, 102.0, 100.0, 101.0],
        ]
        symbols_info.append({
            "symbol": symbol,
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
        })
    for idx in range(down_count):
        symbol = f"DN{idx}USDT"
        tickers.append({
            "symbol": symbol,
            "quoteVolume": "25000000",
            "priceChangePercent": "-1",
        })
        klines_by_symbol[symbol] = [
            [0, 0, 102.0, 100.0, 101.0],
            [0, 0, 101.0, 99.0, 100.0],
        ]
        symbols_info.append({
            "symbol": symbol,
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
        })
    return tickers, klines_by_symbol, symbols_info


class BreadthClient:
    def __init__(self, btc_closes, up_count=60, down_count=40):
        self.tickers, self.klines_by_symbol, self.symbols_info = (
            _make_breadth_fixture(up_count, down_count)
        )
        self.btc_klines = [
            [0, close, close + 1.0, close - 1.0, close] for close in btc_closes
        ]

    def get_tickers_24hr(self):
        return self.tickers

    def get_exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                },
                *self.symbols_info,
            ]
        }

    def get_funding_rates_all(self):
        return []

    def get_klines(self, symbol, interval, limit):
        if symbol == "BTCUSDT":
            return self.btc_klines
        return self.klines_by_symbol.get(symbol, [])


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


def test_market_regime_cautious_for_soft_btc_weak_trend():
    btc_closes = [100.0] * 60 + [
        99.8,
        99.7,
        99.6,
        99.5,
        99.4,
        99.3,
        99.2,
        99.1,
        99.0,
        98.9,
    ]
    client = BreadthClient(btc_closes, up_count=60, down_count=40)
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=client,
    )

    result = skill._get_market_regime(
        {},
        tickers=client.get_tickers_24hr(),
        tradable=skill._get_tradable_symbols(),
    )

    assert result["status"] == "cautious"
    assert "BTC 4h weak trend" in result["reason"]
    assert result["score_adjustment"] == 10


def test_market_regime_blocks_low_4h_breadth():
    client = BreadthClient([100.0] * 70, up_count=30, down_count=70)
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=client,
    )

    result = skill._get_market_regime(
        {},
        tickers=client.get_tickers_24hr(),
        tradable=skill._get_tradable_symbols(),
    )

    assert result["status"] == "blocked"
    assert result["breadth_pct_4h"] == 30.0
    assert "4h上涨广度" in result["reason"]


def test_market_regime_cautious_for_borderline_4h_breadth():
    client = BreadthClient([100.0] * 70, up_count=40, down_count=60)
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=client,
    )

    result = skill._get_market_regime(
        {},
        tickers=client.get_tickers_24hr(),
        tradable=skill._get_tradable_symbols(),
    )

    assert result["status"] == "cautious"
    assert result["breadth_pct_4h"] == 40.0
    assert result["score_adjustment"] == 10


def test_market_regime_fetches_breadth_when_tickers_not_supplied():
    client = BreadthClient([100.0] * 70, up_count=60, down_count=40)
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=client,
    )

    result = skill._get_market_regime({})

    assert result["status"] == "enabled"
    assert result["breadth_pct_4h"] == 60.0


def test_market_regime_can_disable_breadth_for_non_4h_modes():
    client = BreadthClient([100.0] * 70, up_count=10, down_count=90)
    skill = ShortTermOversoldSkill(
        state_store=FakeStateStore(),
        input_schema={},
        output_schema={},
        client=client,
    )

    result = skill._get_market_regime({}, use_market_breadth=False)

    assert result["status"] == "enabled"
    assert result["breadth_status"] == "not_applicable"


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
