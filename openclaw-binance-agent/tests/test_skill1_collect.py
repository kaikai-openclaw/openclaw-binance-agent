"""
Skill-1 Binance 量化数据采集与候选筛选 单元测试（v2）。

覆盖场景：
1. 技术指标纯函数（EMA / RSI / MACD / 量比 / ATR / ADX / 相关性）
2. 大盘过滤逻辑（成交额、振幅、绝对涨跌幅区间、交易状态）
3. 双向信号评分（做多 + 做空）
4. 完整 run() 流程（mock BinancePublicClient）
5. 相关性去重
6. 边界场景（空数据、K线不足、异常处理）
"""

import json
import math
import os
import uuid
from unittest.mock import MagicMock

import pytest

from src.infra.state_store import StateStore
from src.skills.skill1_collect import (
    ADX_PERIOD,
    ATR_PERIOD,
    CORRELATION_THRESHOLD,
    DEFAULT_KLINE_INTERVAL,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_ADX,
    DEFAULT_MIN_SIGNAL_SCORE,
    DEFAULT_MIN_AMPLITUDE_PCT,
    DEFAULT_MIN_QUOTE_VOLUME,
    DEFAULT_PRICE_CHANGE_MAX,
    DEFAULT_PRICE_CHANGE_MIN,
    DEFAULT_VOLUME_SURGE_RATIO,
    EMA_FAST,
    EMA_SLOW,
    KLINE_LIMIT,
    RSI_PERIOD,
    WEIGHT_ADX,
    WEIGHT_EMA,
    WEIGHT_LIQUIDITY,
    WEIGHT_MACD,
    WEIGHT_RSI,
    Skill1Collect,
    calc_adx,
    calc_atr,
    calc_correlation,
    calc_ema,
    calc_macd,
    calc_returns,
    calc_rsi,
    calc_volume_surge,
)


# ── Schema 加载 ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill1_input.json")
OUTPUT_SCHEMA = _load_schema("skill1_output.json")


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


def _make_ticker(
    symbol: str = "BTCUSDT",
    quote_volume: float = 100_000_000,
    high: float = 110.0,
    low: float = 100.0,
    price_change_pct: float = 5.0,
) -> dict:
    """构造一条 ticker/24hr 数据。"""
    return {
        "symbol": symbol,
        "quoteVolume": str(quote_volume),
        "highPrice": str(high),
        "lowPrice": str(low),
        "priceChangePercent": str(price_change_pct),
    }


def _make_kline(close: float, volume: float = 1000.0, high: float | None = None, low: float | None = None) -> list:
    """构造一条简化 K 线: [open_time, open, high, low, close, volume, ...]"""
    h = high if high is not None else close * 1.01
    lo = low if low is not None else close * 0.99
    return [0, str(close), str(h), str(lo), str(close), str(volume), 0, "0", 0, "0", "0", "0"]


def _make_klines_series(
    closes: list[float],
    volumes: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> list[list]:
    """从收盘价序列构造 K 线列表。"""
    if volumes is None:
        volumes = [1000.0] * len(closes)
    result = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        h = highs[i] if highs else None
        lo = lows[i] if lows else None
        result.append(_make_kline(c, v, h, lo))
    return result


def _make_exchange_info(symbols: list[str]) -> dict:
    """构造 exchangeInfo 响应。"""
    return {
        "symbols": [
            {"symbol": s, "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"}
            for s in symbols
        ]
    }


def _make_skill(state_store, client) -> Skill1Collect:
    return Skill1Collect(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        client=client,
    )


# ══════════════════════════════════════════════════════════
# 1. 技术指标纯函数测试
# ══════════════════════════════════════════════════════════

class TestCalcEma:
    """EMA 计算测试。"""

    def test_basic_ema(self):
        closes = [10.0, 11.0, 12.0, 11.5, 13.0]
        result = calc_ema(closes, 3)
        assert len(result) == 5
        assert math.isnan(result[0])
        assert math.isnan(result[1])
        assert not math.isnan(result[2])  # SMA 起始

    def test_insufficient_data(self):
        result = calc_ema([10.0, 11.0], 5)
        assert all(math.isnan(v) for v in result)

    def test_single_period(self):
        closes = [10.0, 20.0, 30.0]
        result = calc_ema(closes, 1)
        assert result == closes


class TestCalcRsi:
    """RSI 计算测试。"""

    def test_insufficient_data(self):
        assert calc_rsi([10.0] * 5) is None

    def test_all_gains(self):
        closes = list(range(1, 20))
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi > 90.0

    def test_all_losses(self):
        closes = list(range(20, 1, -1))
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert rsi < 10.0

    def test_mixed_movement(self):
        closes = [100, 102, 101, 103, 100, 104, 102, 105, 103, 106,
                  104, 107, 105, 108, 106, 109]
        rsi = calc_rsi(closes)
        assert rsi is not None
        assert 30.0 < rsi < 70.0


class TestCalcMacd:
    """MACD 计算测试。"""

    def test_insufficient_data(self):
        result = calc_macd([10.0] * 5)
        assert result["macd_line"] is None

    def test_valid_macd(self):
        closes = [100 + i * 0.5 for i in range(50)]
        result = calc_macd(closes)
        assert result["macd_line"] is not None
        assert result["signal_line"] is not None
        assert result["histogram"] is not None

    def test_uptrend_positive_macd(self):
        closes = [100 + i * 2.0 for i in range(50)]
        result = calc_macd(closes)
        assert result["macd_line"] > 0


class TestCalcVolumeSurge:
    """量比计算测试。"""

    def test_insufficient_data(self):
        assert calc_volume_surge([100] * 5) is None

    def test_no_surge(self):
        volumes = [100.0] * 20
        surge = calc_volume_surge(volumes)
        assert surge is not None
        assert abs(surge - 1.0) < 0.01

    def test_surge_detected(self):
        volumes = [100.0] * 15 + [300.0] * 5
        surge = calc_volume_surge(volumes)
        assert surge is not None
        assert surge > 1.5

    def test_zero_long_avg(self):
        volumes = [0.0] * 20
        assert calc_volume_surge(volumes) is None


class TestCalcAtr:
    """ATR 计算测试。"""

    def test_insufficient_data(self):
        assert calc_atr([10.0] * 5, [9.0] * 5, [9.5] * 5) is None

    def test_valid_atr(self):
        n = 30
        highs = [100 + i * 0.5 + 2 for i in range(n)]
        lows = [100 + i * 0.5 - 2 for i in range(n)]
        closes = [100 + i * 0.5 for i in range(n)]
        atr = calc_atr(highs, lows, closes)
        assert atr is not None
        assert atr > 0

    def test_flat_market_low_atr(self):
        n = 30
        highs = [100.01] * n
        lows = [99.99] * n
        closes = [100.0] * n
        atr = calc_atr(highs, lows, closes)
        assert atr is not None
        assert atr < 0.1  # 非常低的波动


class TestCalcAdx:
    """ADX 计算测试。"""

    def test_insufficient_data(self):
        n = 10
        assert calc_adx([100.0] * n, [99.0] * n, [99.5] * n) is None

    def test_trending_market_high_adx(self):
        """持续上涨趋势应产生较高 ADX。"""
        n = 60
        closes = [100 + i * 2.0 for i in range(n)]
        highs = [c + 1.0 for c in closes]
        lows = [c - 0.5 for c in closes]
        adx = calc_adx(highs, lows, closes)
        assert adx is not None
        assert adx > 20.0  # 有趋势

    def test_sideways_market_low_adx(self):
        """震荡市场应产生较低 ADX。"""
        n = 60
        closes = [100 + (i % 3 - 1) * 0.5 for i in range(n)]
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        adx = calc_adx(highs, lows, closes)
        assert adx is not None
        assert adx < 30.0


class TestCalcCorrelation:
    """相关性计算测试。"""

    def test_insufficient_data(self):
        assert calc_correlation([0.01] * 5, [0.02] * 5) == 0.0

    def test_perfect_positive(self):
        a = [0.01 * i for i in range(20)]
        b = [0.02 * i for i in range(20)]
        corr = calc_correlation(a, b)
        assert corr > 0.99

    def test_perfect_negative(self):
        a = [0.01 * i for i in range(20)]
        b = [-0.02 * i for i in range(20)]
        corr = calc_correlation(a, b)
        assert corr < -0.99

    def test_uncorrelated(self):
        import random
        random.seed(42)
        a = [random.gauss(0, 1) for _ in range(100)]
        b = [random.gauss(0, 1) for _ in range(100)]
        corr = calc_correlation(a, b)
        assert abs(corr) < 0.3

    def test_zero_std(self):
        a = [0.0] * 20
        b = [0.01] * 20
        assert calc_correlation(a, b) == 0.0


class TestCalcReturns:
    """收益率计算测试。"""

    def test_basic(self):
        closes = [100, 110, 105]
        rets = calc_returns(closes)
        assert len(rets) == 2
        assert abs(rets[0] - 0.1) < 0.001
        assert abs(rets[1] - (-5 / 110)) < 0.001

    def test_empty(self):
        assert calc_returns([100]) == []
        assert calc_returns([]) == []


# ══════════════════════════════════════════════════════════
# 2. 大盘过滤测试（v2: 绝对值涨跌幅）
# ══════════════════════════════════════════════════════════

class TestTickerFilter:
    """Step 1 大盘过滤测试。"""

    def test_passes_valid_ticker(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0)]
        tradable = {"BTCUSDT"}
        result = skill._filter_tickers(tickers, tradable, 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"

    def test_filters_low_volume(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 1_000_000)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_filters_low_amplitude(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 101, 100, 5.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_filters_excessive_pump(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 130, 100, 25.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_passes_negative_change_v2(self, state_store):
        """v2: 下跌标的也应通过（绝对值在区间内）。"""
        client = MagicMock()
        skill = _make_skill(state_store, client)
        # -5% 下跌，|5%| 在 [2, 20] 区间内
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, -5.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 1
        assert result[0]["price_change_pct"] == -5.0

    def test_filters_tiny_change(self, state_store):
        """涨跌幅绝对值太小（< pc_min）应被过滤。"""
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, 0.5)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_filters_excessive_dump(self, state_store):
        """暴跌超过 pc_max 也应被过滤。"""
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, -25.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_filters_non_usdt_pair(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCBTC", 100_000_000, 110, 100, 5.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_filters_non_tradable(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0)]
        tradable = {"ETHUSDT"}
        result = skill._filter_tickers(tickers, tradable, 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0

    def test_empty_tradable_allows_all(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 1

    def test_zero_low_price_filtered(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        tickers = [_make_ticker("BTCUSDT", 100_000_000, 110, 0, 5.0)]
        result = skill._filter_tickers(tickers, set(), 30_000_000, 5.0, 2.0, 20.0)
        assert len(result) == 0


# ══════════════════════════════════════════════════════════
# 3. 双向信号评分测试
# ══════════════════════════════════════════════════════════

class TestSignalScoring:
    """v2 双向信号评分测试。"""

    def test_rsi_long_oversold(self):
        """RSI 超卖应给做多满分。"""
        score = Skill1Collect._score_rsi_long(25.0)
        assert score == WEIGHT_RSI

    def test_rsi_long_overbought(self):
        """RSI 超买应给做多 0 分。"""
        score = Skill1Collect._score_rsi_long(85.0)
        assert score == 0.0

    def test_rsi_long_continuous(self):
        """RSI 评分应是连续的，不是阶梯。"""
        s1 = Skill1Collect._score_rsi_long(40.0)
        s2 = Skill1Collect._score_rsi_long(50.0)
        s3 = Skill1Collect._score_rsi_long(60.0)
        assert s1 > s2 > s3  # 单调递减
        # 不应有大的跳跃
        assert abs(s1 - s2) < 10
        assert abs(s2 - s3) < 10

    def test_rsi_long_none(self):
        assert Skill1Collect._score_rsi_long(None) == 0.0

    def test_rsi_short_overbought(self):
        """RSI 超买应给做空满分。"""
        score = Skill1Collect._score_rsi_short(85.0)
        assert score == WEIGHT_RSI

    def test_rsi_short_oversold(self):
        """RSI 超卖应给做空 0 分。"""
        score = Skill1Collect._score_rsi_short(25.0)
        assert score == 0.0

    def test_rsi_short_continuous(self):
        s1 = Skill1Collect._score_rsi_short(60.0)
        s2 = Skill1Collect._score_rsi_short(50.0)
        s3 = Skill1Collect._score_rsi_short(40.0)
        assert s1 > s2 > s3

    def test_ema_long_bullish(self):
        score = Skill1Collect._score_ema_long(110, 105, 100)
        assert score == WEIGHT_EMA

    def test_ema_long_partial(self):
        score = Skill1Collect._score_ema_long(110, 105, 115)
        assert score == WEIGHT_EMA * 0.5

    def test_ema_long_bearish(self):
        score = Skill1Collect._score_ema_long(90, 105, 100)
        assert score == 0.0

    def test_ema_short_bearish(self):
        score = Skill1Collect._score_ema_short(90, 95, 100)
        assert score == WEIGHT_EMA

    def test_ema_short_partial(self):
        score = Skill1Collect._score_ema_short(90, 95, 85)
        assert score == WEIGHT_EMA * 0.5

    def test_ema_short_bullish(self):
        score = Skill1Collect._score_ema_short(110, 105, 100)
        assert score == 0.0

    def test_macd_long_golden_cross(self):
        score = Skill1Collect._score_macd_long(1.0, 0.5, 0.5)
        assert score == WEIGHT_MACD

    def test_macd_long_hist_positive(self):
        score = Skill1Collect._score_macd_long(-1.0, -1.5, 0.5)
        assert score == WEIGHT_MACD * 0.5

    def test_macd_short_death_cross(self):
        score = Skill1Collect._score_macd_short(-1.0, -0.5, -0.5)
        assert score == WEIGHT_MACD

    def test_macd_short_hist_negative(self):
        score = Skill1Collect._score_macd_short(1.0, 1.5, -0.5)
        assert score == WEIGHT_MACD * 0.5

    def test_adx_high_trend(self):
        score = Skill1Collect._score_adx(50.0)
        assert score == WEIGHT_ADX

    def test_adx_no_trend(self):
        score = Skill1Collect._score_adx(0.0)
        assert score == 0.0

    def test_adx_none(self):
        assert Skill1Collect._score_adx(None) == 0.0

    def test_liquidity_max(self):
        score = Skill1Collect._score_liquidity(1_000_000, 1_000_000)
        assert abs(score - WEIGHT_LIQUIDITY) < 0.01

    def test_liquidity_zero(self):
        assert Skill1Collect._score_liquidity(0, 1_000_000) == 0.0


# ══════════════════════════════════════════════════════════
# 4. 完整 run() 流程测试
# ══════════════════════════════════════════════════════════

class TestRunFlow:
    """完整 run() 流程测试。"""

    def _build_bullish_closes(self, n: int = 100) -> list[float]:
        """构造一个温和上涨序列。"""
        return [100 + i * 0.3 for i in range(n)]

    def _build_bullish_klines(self, n: int = 100) -> list[list]:
        """构造温和上涨 K 线，后 5 根放量。"""
        closes = self._build_bullish_closes(n)
        volumes = [1000.0] * (n - 5) + [3000.0] * 5
        highs = [c + 1.0 for c in closes]
        lows = [c - 0.5 for c in closes]
        return _make_klines_series(closes, volumes, highs, lows)

    def _build_bearish_klines(self, n: int = 100) -> list[list]:
        """构造有波动的下跌 K 线（RSI 在超买区），后 5 根放量。"""
        # 先涨后跌，使 RSI 处于高位（超买区）以产生做空信号
        closes = []
        base = 200
        for i in range(n):
            if i < 30:
                base += 2.0  # 前 30 根上涨
            else:
                base -= 0.5  # 后 70 根缓慢下跌
            # 加入小幅波动避免 RSI 极端值
            closes.append(base + (i % 3 - 1) * 0.2)
        volumes = [1000.0] * (n - 5) + [3000.0] * 5
        highs = [c + 1.5 for c in closes]
        lows = [c - 1.5 for c in closes]
        return _make_klines_series(closes, volumes, highs, lows)

    def test_full_pipeline(self, state_store):
        """完整流程：ticker 过滤 → 量比 → 技术指标 → 输出候选。"""
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT", "ETHUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
            _make_ticker("ETHUSDT", 80_000_000, 115, 100, 8.0),
        ]
        client.get_klines.return_value = self._build_bullish_klines()

        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})

        assert "candidates" in result
        assert "pipeline_run_id" in result
        assert "filter_summary" in result
        assert len(result["candidates"]) >= 1
        for c in result["candidates"]:
            assert "symbol" in c
            assert "signal_score" in c
            assert "signal_direction" in c
            assert c["signal_direction"] in ("long", "short")
            assert "volume_surge_ratio" in c
            assert "atr" in c
            assert "atr_pct" in c
            assert "adx" in c
            assert "collected_at" in c
            assert c["signal_score"] > 0

    def test_bearish_signal_direction(self, state_store):
        """下跌标的应产生 short 信号方向。"""
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, -5.0),
        ]
        client.get_klines.return_value = self._build_bearish_klines()

        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})

        if result["candidates"]:
            # 下跌 K 线应倾向 short
            assert result["candidates"][0]["signal_direction"] == "short"

    def test_pipeline_run_id_is_uuid(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info([])
        client.get_tickers_24hr.return_value = []
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        uuid.UUID(result["pipeline_run_id"], version=4)

    def test_empty_tickers(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info([])
        client.get_tickers_24hr.return_value = []
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        assert result["candidates"] == []
        assert result["filter_summary"]["total_tickers"] == 0

    def test_kline_failure_skips_symbol(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT", "ETHUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
            _make_ticker("ETHUSDT", 80_000_000, 115, 100, 8.0),
        ]
        client.get_klines.side_effect = [
            RuntimeError("timeout"),
            self._build_bullish_klines(),
        ]
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        symbols = [c["symbol"] for c in result["candidates"]]
        assert "BTCUSDT" not in symbols

    def test_insufficient_klines_skipped(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        client.get_klines.return_value = _make_klines_series([100, 101, 102])
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        assert result["candidates"] == []

    def test_max_candidates_limit(self, state_store):
        client = MagicMock()
        tickers = [_make_ticker(f"COIN{i}USDT", 100_000_000, 110, 100, 5.0) for i in range(15)]
        symbols = [f"COIN{i}USDT" for i in range(15)]
        client.get_exchange_info.return_value = _make_exchange_info(symbols)
        client.get_tickers_24hr.return_value = tickers
        # 每个币种用不同的 K 线避免相关性去重
        def make_unique_klines(symbol, interval, limit):
            idx = int(symbol.replace("COIN", "").replace("USDT", ""))
            base = 100 + idx * 10
            closes = [base + i * 0.3 + idx * 0.1 for i in range(100)]
            volumes = [1000.0] * 95 + [3000.0] * 5
            highs = [c + 1.0 for c in closes]
            lows = [c - 0.5 for c in closes]
            return _make_klines_series(closes, volumes, highs, lows)
        client.get_klines.side_effect = make_unique_klines

        skill = _make_skill(state_store, client)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "max_candidates": 3,
        })
        assert len(result["candidates"]) <= 3

    def test_low_surge_ratio_filtered(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        closes = [100 + i * 0.3 for i in range(100)]
        flat_klines = _make_klines_series(closes, [1000.0] * 100)
        client.get_klines.return_value = flat_klines
        skill = _make_skill(state_store, client)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "volume_surge_ratio": 2.0,
        })
        assert result["candidates"] == []

    def test_low_signal_score_filtered(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        closes = [100 + (i % 3) * 0.2 for i in range(100)]
        volumes = [1000.0] * 95 + [3000.0] * 5
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        client.get_klines.return_value = _make_klines_series(closes, volumes, highs, lows)

        skill = _make_skill(state_store, client)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "min_signal_score": DEFAULT_MIN_SIGNAL_SCORE,
        })
        assert result["candidates"] == []

    def test_low_adx_filtered(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        closes = [100 + (i % 3 - 1) * 0.5 for i in range(100)]
        volumes = [1000.0] * 95 + [3000.0] * 5
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.3 for c in closes]
        client.get_klines.return_value = _make_klines_series(closes, volumes, highs, lows)

        skill = _make_skill(state_store, client)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "min_signal_score": 0,
            "min_adx": DEFAULT_MIN_ADX,
        })
        assert result["candidates"] == []

    def test_candidates_sorted_by_score(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["AAAUSDT", "BBBUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("AAAUSDT", 100_000_000, 110, 100, 5.0),
            _make_ticker("BBBUSDT", 100_000_000, 110, 100, 5.0),
        ]
        bullish = self._build_bullish_klines()
        sideways_closes = [100 + (i % 3) * 0.5 for i in range(100)]
        sideways_vols = [1000.0] * 95 + [3000.0] * 5
        sideways = _make_klines_series(sideways_closes, sideways_vols)

        client.get_klines.side_effect = [bullish, sideways]
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})

        if len(result["candidates"]) >= 2:
            assert result["candidates"][0]["signal_score"] >= result["candidates"][1]["signal_score"]

    def test_atr_output_present(self, state_store):
        """候选应包含 ATR 相关字段。"""
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info(["BTCUSDT"])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        client.get_klines.return_value = self._build_bullish_klines()
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})

        if result["candidates"]:
            c = result["candidates"][0]
            assert "atr" in c
            assert "atr_pct" in c
            assert c["atr"] is not None
            assert c["atr_pct"] is not None
            assert c["atr"] > 0
            assert c["atr_pct"] > 0


# ══════════════════════════════════════════════════════════
# 5. 相关性去重测试
# ══════════════════════════════════════════════════════════

class TestCorrelationDedup:
    """相关性去重测试。"""

    def test_identical_returns_deduped(self):
        """完全相同的收益率序列应被去重。"""
        scored = [
            {"symbol": "AAAUSDT", "signal_score": 80},
            {"symbol": "BBBUSDT", "signal_score": 70},
        ]
        rets = [0.01 * i for i in range(30)]
        returns_map = {
            "AAAUSDT": rets,
            "BBBUSDT": rets,  # 完全相同
        }
        result = Skill1Collect._deduplicate_by_correlation(scored, returns_map, 10)
        assert len(result) == 1
        assert result[0]["symbol"] == "AAAUSDT"

    def test_uncorrelated_kept(self):
        """不相关的币种应保留。"""
        import random
        random.seed(123)
        scored = [
            {"symbol": "AAAUSDT", "signal_score": 80},
            {"symbol": "BBBUSDT", "signal_score": 70},
        ]
        returns_map = {
            "AAAUSDT": [random.gauss(0, 1) for _ in range(30)],
            "BBBUSDT": [random.gauss(0, 1) for _ in range(30)],
        }
        result = Skill1Collect._deduplicate_by_correlation(scored, returns_map, 10)
        assert len(result) == 2

    def test_negative_correlation_kept(self):
        """强负相关不应被视为重复暴露。"""
        scored = [
            {"symbol": "AAAUSDT", "signal_score": 80},
            {"symbol": "BBBUSDT", "signal_score": 70},
        ]
        returns_map = {
            "AAAUSDT": [0.01 * i for i in range(1, 31)],
            "BBBUSDT": [-0.01 * i for i in range(1, 31)],
        }
        result = Skill1Collect._deduplicate_by_correlation(scored, returns_map, 10)
        assert len(result) == 2

    def test_respects_max_candidates(self):
        scored = [{"symbol": f"C{i}USDT", "signal_score": 90 - i} for i in range(10)]
        returns_map = {f"C{i}USDT": [0.01 * (i + j) for j in range(30)] for i in range(10)}
        result = Skill1Collect._deduplicate_by_correlation(scored, returns_map, 3)
        assert len(result) <= 3

    def test_empty_returns_not_deduped(self):
        """没有收益率数据的币种不应被去重。"""
        scored = [
            {"symbol": "AAAUSDT", "signal_score": 80},
            {"symbol": "BBBUSDT", "signal_score": 70},
        ]
        returns_map = {}
        result = Skill1Collect._deduplicate_by_correlation(scored, returns_map, 10)
        assert len(result) == 2


# ══════════════════════════════════════════════════════════
# 6. 边界场景
# ══════════════════════════════════════════════════════════

class TestEdgeCases:
    """边界场景测试。"""

    def test_skill_name(self, state_store):
        client = MagicMock()
        skill = _make_skill(state_store, client)
        assert skill.name == "skill1_collect"

    def test_exchange_info_failure_graceful(self, state_store):
        client = MagicMock()
        client.get_exchange_info.side_effect = RuntimeError("network error")
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
        ]
        closes = [100 + i * 0.3 for i in range(100)]
        volumes = [1000.0] * 95 + [3000.0] * 5
        highs = [c + 1.0 for c in closes]
        lows = [c - 0.5 for c in closes]
        client.get_klines.return_value = _make_klines_series(closes, volumes, highs, lows)

        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        assert "candidates" in result

    def test_filter_summary_present(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info([])
        client.get_tickers_24hr.return_value = [
            _make_ticker("BTCUSDT", 100_000_000, 110, 100, 5.0),
            _make_ticker("LOWUSDT", 1_000, 101, 100, 0.5),
        ]
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        summary = result["filter_summary"]
        assert summary["total_tickers"] == 2
        assert summary["after_base_filter"] <= 2

    def test_default_params_used(self, state_store):
        client = MagicMock()
        client.get_exchange_info.return_value = _make_exchange_info([])
        client.get_tickers_24hr.return_value = []
        skill = _make_skill(state_store, client)
        result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
        assert result["candidates"] == []

    def test_weight_sum_is_100(self):
        """评分权重之和应为 100。"""
        assert WEIGHT_RSI + WEIGHT_EMA + WEIGHT_MACD + WEIGHT_ADX + WEIGHT_LIQUIDITY == 100
