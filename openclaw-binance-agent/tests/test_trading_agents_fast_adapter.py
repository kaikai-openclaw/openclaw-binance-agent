import pytest

from src.integrations import trading_agents_adapter as adapter


def _ticker() -> dict:
    return {
        "symbol": "BTCUSDT",
        "last_price": 80000.0,
        "price_change_pct": 1.2,
        "volume": 1000.0,
        "quote_volume": 100_000_000.0,
        "high_24h": 81000.0,
        "low_24h": 78000.0,
    }


def _market_data() -> dict:
    return {
        "signal_direction": "long",
        "strategy_tag": "crypto_reversal",
        "reversal_score": 62,
        "effective_min_reversal_score": 60,
        "atr_pct": 3.2,
        "atr_filter_pct": 4.5,
        "volatility_action": "half_size",
        "market_regime": {
            "status": "cautious",
            "reason": "BTC实时价修复且1h趋势止跌",
            "breadth_pct_4h": 52.0,
            "breadth_pct_24h": 38.0,
            "major_breadth_pct_4h": 66.7,
            "breadth_sample_size": 110,
            "major_breadth_sample_size": 15,
            "btc_last_close": 79500.0,
            "btc_ema5": 79600.0,
            "btc_ema20": 80200.0,
            "btc_realtime_price": 80100.0,
            "btc_realtime_vs_ema20_pct": -0.12,
            "btc_realtime_recovery": True,
            "btc_1h_ema5": 80020.0,
            "btc_1h_ema20": 79900.0,
            "btc_1h_recovery": True,
            "btc_1h_no_new_low": True,
            "score_adjustment": 15,
        },
    }


def test_fast_analyzer_confirmed_direction_gets_bonus(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())

    def fake_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            '{"signal":"long","confidence":76,"risk_level":"medium",'
            '"veto":false,"veto_reason":"","key_risks":["ATR偏高"],'
            '"confirmation":"BTC 1h修复且方向一致"}'
        )

    monkeypatch.setattr(adapter, "_call_fast_llm", fake_call)

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "long"
    assert result["rating_score"] == 9
    assert result["confidence"] == 76.0
    assert "【市场环境】" in captured["prompt"]
    assert "BTC 1h" in captured["prompt"]
    assert "只能返回 expected_direction 或 hold" in captured["prompt"]


def test_fast_analyzer_hold_is_not_tradeable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())
    monkeypatch.setattr(
        adapter,
        "_call_fast_llm",
        lambda prompt: (
            '{"signal":"hold","confidence":58,"risk_level":"high",'
            '"veto":true,"veto_reason":"BTC 4h弱势","key_risks":["趋势冲突"],'
            '"confirmation":"等待确认"}'
        ),
    )

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
    assert "veto=BTC 4h弱势" in result["comment"]


def test_fast_analyzer_llm_failure_is_not_tradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())

    def fail_call(prompt: str) -> str:
        raise RuntimeError("timeout")

    monkeypatch.setattr(adapter, "_call_fast_llm", fail_call)

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
    assert "降级为hold" in result["comment"]


def test_fast_analyzer_opposite_direction_is_not_tradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_data = _market_data()
    market_data["strategy_tag"] = "crypto_generic"
    market_data["signal_direction"] = "short"
    market_data["signal_score"] = 72
    market_data.pop("reversal_score")

    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())
    monkeypatch.setattr(
        adapter,
        "_call_fast_llm",
        lambda prompt: (
            '{"signal":"long","confidence":80,"risk_level":"medium",'
            '"veto":false,"veto_reason":"","key_risks":[],'
            '"confirmation":"反弹"}'
        ),
    )

    result = adapter.create_fast_analyzer()("ETHUSDT", market_data)

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
    assert "与预期方向 short 相反" in result["comment"]


def test_fast_analyzer_uses_generic_signal_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    market_data = _market_data()
    market_data["strategy_tag"] = "crypto_generic"
    market_data["signal_direction"] = "short"
    market_data["signal_score"] = 72
    market_data.pop("reversal_score")

    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())

    def fake_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            '{"signal":"short","confidence":80,"risk_level":"medium",'
            '"veto":false,"veto_reason":"","key_risks":[],'
            '"confirmation":"方向一致"}'
        )

    monkeypatch.setattr(adapter, "_call_fast_llm", fake_call)

    result = adapter.create_fast_analyzer()("ETHUSDT", market_data)

    assert result["signal"] == "short"
    assert result["rating_score"] == 10
    assert "扫描分=72" in result["comment"]
    assert "通用信号评分: 72/100" in captured["prompt"]


def test_fast_analyzer_veto_true_is_not_tradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())
    monkeypatch.setattr(
        adapter,
        "_call_fast_llm",
        lambda prompt: (
            '{"signal":"long","confidence":82,"risk_level":"high",'
            '"veto":true,"veto_reason":"主流广度冲突","key_risks":["环境冲突"],'
            '"confirmation":"方向一致但风险过高"}'
        ),
    )

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
    assert "veto=主流广度冲突" in result["comment"]


def test_fast_analyzer_string_veto_is_not_tradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())
    monkeypatch.setattr(
        adapter,
        "_call_fast_llm",
        lambda prompt: (
            '{"signal":"long","confidence":82,"risk_level":"medium",'
            '"veto":"true","veto_reason":"字符串否决",'
            '"key_risks":["格式不标准"],"confirmation":"方向一致"}'
        ),
    )

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
    assert "veto=字符串否决" in result["comment"]


def test_fast_analyzer_high_risk_is_not_tradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_get_cached_ticker", lambda symbol: _ticker())
    monkeypatch.setattr(
        adapter,
        "_call_fast_llm",
        lambda prompt: (
            '{"signal":"long","confidence":82,"risk_level":"high",'
            '"veto":false,"veto_reason":"",'
            '"key_risks":["高风险"],"confirmation":"方向一致"}'
        ),
    )

    result = adapter.create_fast_analyzer()("BTCUSDT", _market_data())

    assert result["signal"] == "hold"
    assert result["rating_score"] == adapter.FAST_HOLD_RATING_SCORE
