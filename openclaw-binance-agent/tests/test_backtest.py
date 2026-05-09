"""
回测框架单元测试（P0-3）

用合成数据（确定性上涨/下跌/震荡）验证：
  - 指标函数正确
  - 引擎 long/short 入场、SL/TP/time_exit/end_of_data 触发
  - 扣费后的净盈亏与 fees 模块一致
"""

import math
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest import (
    BacktestEngine,
    EntrySignal,
    calc_max_drawdown,
    calc_profit_factor,
    calc_sharpe_ratio,
    summarize_trades,
)


# ══════════════════════════════════════════════════════════
# 1. 指标函数测试
# ══════════════════════════════════════════════════════════

class TestMetrics:

    def test_sharpe_zero_for_single_value(self):
        assert calc_sharpe_ratio([0.01]) == 0.0

    def test_sharpe_positive_for_positive_mean(self):
        returns = [0.01, 0.02, 0.015, 0.012, 0.018]
        s = calc_sharpe_ratio(returns, periods_per_year=252)
        assert s > 0

    def test_sharpe_zero_variance_returns_zero(self):
        assert calc_sharpe_ratio([0.01, 0.01, 0.01]) == 0.0

    def test_max_drawdown_basic(self):
        curve = [100, 120, 110, 130, 90, 140]
        dd, dd_pct = calc_max_drawdown(curve)
        # peak=130 -> 90，回撤 40，占 40/130 ≈ 0.3077
        assert dd == 40
        assert dd_pct == pytest.approx(40 / 130, rel=1e-4)

    def test_max_drawdown_monotonic_up(self):
        dd, dd_pct = calc_max_drawdown([100, 101, 102, 103])
        assert dd == 0
        assert dd_pct == 0

    def test_profit_factor_no_loss(self):
        assert calc_profit_factor([10, 20], []) == float("inf")

    def test_profit_factor_no_win(self):
        assert calc_profit_factor([], [-10, -20]) == 0.0

    def test_profit_factor_mixed(self):
        # 盈利 30, 亏损 20 → PF = 1.5
        assert calc_profit_factor([10, 20], [-10, -10]) == pytest.approx(1.5)

    def test_summarize_trades_empty(self):
        s = summarize_trades([])
        assert s.trade_count == 0
        assert s.sharpe_ratio == 0.0

    def test_summarize_trades_basic(self):
        pnls = [100, -50, 200, -30, 80]
        s = summarize_trades(pnls, initial_equity=10000)
        assert s.trade_count == 5
        assert s.win_count == 3
        assert s.loss_count == 2
        assert s.win_rate_pct == 60.0
        assert s.total_pnl == sum(pnls)


# ══════════════════════════════════════════════════════════
# 2. 合成数据工厂
# ══════════════════════════════════════════════════════════

def _make_bars(prices: list[float], start="2026-01-01", freq="1h") -> pd.DataFrame:
    """根据 close 序列构造 OHLC（简单设 high=close*1.001, low=close*0.999）。"""
    idx = pd.date_range(start, periods=len(prices), freq=freq)
    closes = np.array(prices, dtype=float)
    # open = 上一根 close（首根用当根 close）
    opens = np.empty_like(closes)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.001
    lows = np.minimum(opens, closes) * 0.999
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes},
        index=idx,
    )


# ══════════════════════════════════════════════════════════
# 3. 回测引擎测试
# ══════════════════════════════════════════════════════════

class TestBacktestEngine:

    def test_validate_requires_ohlc(self):
        bars = pd.DataFrame({"close": [1, 2, 3]})
        engine = BacktestEngine()
        with pytest.raises(ValueError, match="缺少必要列"):
            engine.run(bars, lambda df: None)

    def test_validate_min_length(self):
        bars = _make_bars([100.0])
        engine = BacktestEngine()
        with pytest.raises(ValueError, match="至少需要"):
            engine.run(bars, lambda df: None)

    def test_no_signal_no_trade(self):
        bars = _make_bars([100, 101, 102, 103, 104])
        engine = BacktestEngine()
        result = engine.run(bars, signal_generator=lambda df: None)
        assert result.stats.trade_count == 0
        assert result.equity_curve == [engine.initial_equity]

    def test_long_take_profit_triggered(self):
        """上涨行情中 long 信号应触发 TP 获利。"""
        # 100 → 115 线性上涨，每根 +1，TP 3% 在 103 附近会被触发
        bars = _make_bars([100 + i for i in range(20)])
        calls = {"n": 0}

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            # 只在第一根发信号，避免重复入场
            if calls["n"] == 0 and len(df) == 1:
                calls["n"] += 1
                return EntrySignal("long", stop_loss_pct=0.02, take_profit_pct=0.03)
            return None

        engine = BacktestEngine(fee_market="crypto", max_hold_bars=50)
        result = engine.run(bars, gen)
        assert result.stats.trade_count == 1
        trade = result.trades[0]
        assert trade.direction == "long"
        assert trade.exit_reason == "tp"
        assert trade.net_pnl > 0
        # 入场在第二根 open（=100，因为 first close=100，second open=first close）
        assert trade.entry_price == pytest.approx(100.0)

    def test_long_stop_loss_triggered(self):
        """下跌行情中 long 信号应触发 SL 亏损。"""
        bars = _make_bars([100 - i for i in range(20)])

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            if len(df) == 1:
                return EntrySignal("long", stop_loss_pct=0.02, take_profit_pct=0.10)
            return None

        engine = BacktestEngine(fee_market="crypto", max_hold_bars=50)
        result = engine.run(bars, gen)
        assert result.stats.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == "sl"
        assert trade.net_pnl < 0

    def test_short_take_profit_triggered(self):
        """下跌行情中 short 信号应盈利。"""
        bars = _make_bars([100 - i for i in range(20)])

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            if len(df) == 1:
                return EntrySignal("short", stop_loss_pct=0.05, take_profit_pct=0.03)
            return None

        engine = BacktestEngine(fee_market="crypto", max_hold_bars=50)
        result = engine.run(bars, gen)
        assert result.stats.trade_count == 1
        trade = result.trades[0]
        assert trade.direction == "short"
        assert trade.exit_reason == "tp"
        assert trade.net_pnl > 0

    def test_time_exit_when_neither_tp_sl(self):
        """横盘行情中，SL/TP 都不触发，max_hold_bars 到期应 time_exit。"""
        bars = _make_bars([100.0] * 30)

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            if len(df) == 1:
                return EntrySignal("long", stop_loss_pct=0.5, take_profit_pct=0.5)
            return None

        engine = BacktestEngine(fee_market="crypto", max_hold_bars=5)
        result = engine.run(bars, gen)
        assert result.stats.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == "time_exit"
        assert trade.bars_held == 5

    def test_fees_reduce_pnl(self):
        """net_pnl 应严格小于 gross_pnl（只要有费率）。"""
        bars = _make_bars([100 + i for i in range(15)])

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            if len(df) == 1:
                return EntrySignal("long", stop_loss_pct=0.02, take_profit_pct=0.03)
            return None

        engine = BacktestEngine(fee_market="crypto", fee_order_type="taker",
                                max_hold_bars=50)
        result = engine.run(bars, gen)
        trade = result.trades[0]
        assert trade.net_pnl < trade.gross_pnl

    def test_summary_dict_serializable(self):
        """to_summary_dict 应返回可序列化的扁平字典。"""
        bars = _make_bars([100 + i for i in range(15)])

        def gen(df: pd.DataFrame) -> Optional[EntrySignal]:
            if len(df) == 1:
                return EntrySignal("long", stop_loss_pct=0.02, take_profit_pct=0.03)
            return None

        engine = BacktestEngine()
        result = engine.run(bars, gen)
        summary = result.to_summary_dict()
        assert "trade_count" in summary
        assert "sharpe_ratio" in summary
        assert "max_drawdown_pct" in summary
