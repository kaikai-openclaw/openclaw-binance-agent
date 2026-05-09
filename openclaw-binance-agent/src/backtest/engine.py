"""
Bar-by-bar 回测引擎（P0-3 最小可用版）

设计原则：
  - 一次只持一单（单品种回测）
  - 信号生成器只看"截止到当前 bar"的历史，避免 look-ahead bias
  - 入场按下一根 bar 的 open 价撮合（模拟真实下一个交易机会）
  - 持仓期间每根 bar 用 high/low 判断是否触发 SL/TP（取较先触发者）
  - 持仓期超过 max_hold_bars 则按当根 close 平仓
  - 使用 src.infra.fees 扣除真实手续费 + 滑点

不实现（故意）：
  - 多品种组合、资金分配
  - 分批建仓/加减仓
  - 事件驱动撮合引擎
  - slippage 模型进阶（已有默认值）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

import pandas as pd

from src.infra.fees import apply_fees_to_pnl
from src.backtest.metrics import TradeStats, summarize_trades


Direction = Literal["long", "short"]


@dataclass
class EntrySignal:
    """入场信号。"""
    direction: Direction
    # 以"入场价"为基准的止损/止盈距离（比例，正数）
    stop_loss_pct: float
    take_profit_pct: float


@dataclass
class Trade:
    """一笔已平仓交易。"""
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: Direction
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    net_pnl: float
    bars_held: int
    exit_reason: Literal["tp", "sl", "time_exit", "end_of_data"]


@dataclass
class BacktestResult:
    trades: list[Trade]
    stats: TradeStats
    equity_curve: list[float]

    def to_summary_dict(self) -> dict:
        s = self.stats
        return {
            "trade_count": s.trade_count,
            "win_rate_pct": round(s.win_rate_pct, 2),
            "profit_factor": round(s.profit_factor, 3)
                if s.profit_factor != float("inf") else "inf",
            "total_pnl": round(s.total_pnl, 2),
            "avg_pnl": round(s.avg_pnl, 4),
            "sharpe_ratio": round(s.sharpe_ratio, 3),
            "max_drawdown": round(s.max_drawdown, 2),
            "max_drawdown_pct": round(s.max_drawdown_pct * 100, 2),
            "calmar_ratio": round(s.calmar_ratio, 3)
                if s.calmar_ratio != float("inf") else "inf",
        }


# 信号生成器：输入历史 DataFrame（到当前 bar 为止，含当前 bar），输出 EntrySignal 或 None
SignalGenerator = Callable[[pd.DataFrame], Optional[EntrySignal]]


class BacktestEngine:
    """最小可用 bar-by-bar 回测器。"""

    def __init__(
        self,
        fee_market: Literal["crypto", "astock"] = "crypto",
        fee_order_type: Literal["taker", "maker"] = "taker",
        fee_vip_discount: float = 0.0,
        initial_equity: float = 10000.0,
        position_notional_pct: float = 1.0,
        max_hold_bars: int = 24,
        periods_per_year: float = 365.0,
    ) -> None:
        """
        参数:
            fee_market: 市场类型，决定费率公式
            fee_order_type: crypto 下单类型
            fee_vip_discount: VIP 折扣（0-1）
            initial_equity: 初始资金
            position_notional_pct: 每笔下单占总权益比例（0-1），
                最小版：全仓单品种
            max_hold_bars: 最大持仓 bar 数（触发时间止损）
            periods_per_year: Sharpe 年化因子（按 bar 周期决定）
        """
        if not 0 < position_notional_pct <= 1:
            raise ValueError("position_notional_pct 必须在 (0, 1] 范围")
        self.fee_market = fee_market
        self.fee_order_type = fee_order_type
        self.fee_vip_discount = fee_vip_discount
        self.initial_equity = initial_equity
        self.position_notional_pct = position_notional_pct
        self.max_hold_bars = max_hold_bars
        self.periods_per_year = periods_per_year

    def run(
        self,
        bars: pd.DataFrame,
        signal_generator: SignalGenerator,
    ) -> BacktestResult:
        """
        在给定 K 线 DataFrame 上回放信号生成器。

        参数:
            bars: 必须含列 [open, high, low, close]，index 可为 DatetimeIndex 或 RangeIndex
            signal_generator: 输入历史切片（至当前 bar），输出 EntrySignal 或 None

        返回:
            BacktestResult
        """
        self._validate_bars(bars)

        bars = bars.reset_index(drop=False)
        time_col = bars.columns[0]  # reset_index 生成的索引列（timestamp 或 int）

        trades: list[Trade] = []
        equity = self.initial_equity
        equity_curve: list[float] = [equity]

        open_pos: Optional[dict] = None  # {direction, entry_time, entry_price, sl, tp, qty, bars_held}

        n = len(bars)
        for i in range(n):
            row = bars.iloc[i]
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            ts = row[time_col]

            # 1. 若有持仓，先检查出场
            if open_pos is not None:
                open_pos["bars_held"] += 1
                exit_price, reason = self._check_exit(open_pos, high, low)

                # 时间止损
                if exit_price is None and open_pos["bars_held"] >= self.max_hold_bars:
                    exit_price = close
                    reason = "time_exit"

                # 数据末尾强制平仓
                if exit_price is None and i == n - 1:
                    exit_price = close
                    reason = "end_of_data"

                if exit_price is not None:
                    trade = self._close_position(open_pos, ts, exit_price, reason)
                    trades.append(trade)
                    equity += trade.net_pnl
                    equity_curve.append(equity)
                    open_pos = None

            # 2. 若无持仓，在"当前 bar 收盘后"基于 signal_generator 判断下一根 bar 入场
            if open_pos is None and i < n - 1:
                history_slice = bars.iloc[: i + 1]
                signal = signal_generator(history_slice)
                if signal is not None:
                    next_bar = bars.iloc[i + 1]
                    entry_price = float(next_bar["open"])
                    if entry_price > 0:
                        notional = equity * self.position_notional_pct
                        quantity = notional / entry_price
                        if signal.direction == "long":
                            sl = entry_price * (1 - signal.stop_loss_pct)
                            tp = entry_price * (1 + signal.take_profit_pct)
                        else:
                            sl = entry_price * (1 + signal.stop_loss_pct)
                            tp = entry_price * (1 - signal.take_profit_pct)
                        open_pos = {
                            "direction": signal.direction,
                            "entry_time": next_bar[time_col],
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp": tp,
                            "quantity": quantity,
                            "bars_held": 0,
                        }

        stats = summarize_trades(
            [t.net_pnl for t in trades],
            initial_equity=self.initial_equity,
            periods_per_year=self.periods_per_year,
        )
        return BacktestResult(trades=trades, stats=stats, equity_curve=equity_curve)

    # ------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------

    @staticmethod
    def _validate_bars(bars: pd.DataFrame) -> None:
        required = {"open", "high", "low", "close"}
        missing = required - set(bars.columns)
        if missing:
            raise ValueError(f"bars 缺少必要列: {sorted(missing)}")
        if len(bars) < 2:
            raise ValueError("bars 至少需要 2 行数据")

    @staticmethod
    def _check_exit(
        pos: dict, high: float, low: float
    ) -> tuple[Optional[float], Optional[str]]:
        """
        检查 SL/TP 触发。遵守"保守原则"——当 bar 同时覆盖 SL 和 TP 时按 SL 优先（更悲观）。
        """
        if pos["direction"] == "long":
            hit_sl = low <= pos["sl"]
            hit_tp = high >= pos["tp"]
            if hit_sl:
                return pos["sl"], "sl"
            if hit_tp:
                return pos["tp"], "tp"
        else:  # short
            hit_sl = high >= pos["sl"]
            hit_tp = low <= pos["tp"]
            if hit_sl:
                return pos["sl"], "sl"
            if hit_tp:
                return pos["tp"], "tp"
        return None, None

    def _close_position(
        self,
        pos: dict,
        exit_time,
        exit_price: float,
        reason: str,
    ) -> Trade:
        entry_price = pos["entry_price"]
        qty = pos["quantity"]
        if pos["direction"] == "long":
            gross = (exit_price - entry_price) * qty
        else:
            gross = (entry_price - exit_price) * qty

        net = apply_fees_to_pnl(
            gross_pnl=gross,
            entry_notional=qty * entry_price,
            exit_notional=qty * exit_price,
            market=self.fee_market,
            order_type=self.fee_order_type,
            vip_discount=self.fee_vip_discount,
        )

        return Trade(
            entry_time=pos["entry_time"],
            exit_time=exit_time,
            direction=pos["direction"],
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=qty,
            gross_pnl=gross,
            net_pnl=net,
            bars_held=pos["bars_held"],
            exit_reason=reason,  # type: ignore[arg-type]
        )
