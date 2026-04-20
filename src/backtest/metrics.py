"""
回测核心指标（P0-3）

包括：
  - Sharpe Ratio（年化，假设 bar-level 收益率）
  - 最大回撤（基于净值曲线）
  - Calmar Ratio（年化收益 / 最大回撤）
  - 胜率、盈亏比、盈利因子
  - 净胜率（扣费后）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence


# 交易日 / 常用周期年化因子（按 bar 类型选择）
ANNUALIZATION_FACTORS = {
    "1h": 24 * 365,
    "4h": 6 * 365,
    "1d": 365,
    "1d_astock": 252,   # A 股交易日
}


@dataclass
class TradeStats:
    """回测汇总指标。"""
    trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    total_pnl: float
    avg_pnl: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    calmar_ratio: float


def calc_sharpe_ratio(
    returns: Sequence[float],
    periods_per_year: float = 365.0,
    risk_free_rate: float = 0.0,
) -> float:
    """
    Sharpe 比率（annualized）。

    returns: 每个周期（bar 或每笔交易）的收益率序列
    periods_per_year: 年化因子，如日线 A 股 252，日线 crypto 365，1h crypto 8760
    risk_free_rate: 年化无风险利率
    """
    n = len(returns)
    if n < 2:
        return 0.0

    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0

    excess = mean - risk_free_rate / periods_per_year
    return (excess / std) * math.sqrt(periods_per_year)


def calc_max_drawdown(equity_curve: Sequence[float]) -> tuple[float, float]:
    """
    最大回撤（绝对值）与最大回撤比例。

    返回:
        (max_drawdown_value, max_drawdown_pct)，均为非负数。
        若 equity_curve 为空或单点，返回 (0, 0)。
    """
    if len(equity_curve) < 2:
        return 0.0, 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
            if peak != 0:
                max_dd_pct = dd / peak
    return max_dd, max_dd_pct


def calc_profit_factor(
    winning_pnls: Iterable[float],
    losing_pnls: Iterable[float],
) -> float:
    """
    盈利因子 = 总盈利 / 总亏损绝对值。

    无亏损时返回 inf；无盈利时返回 0.0。
    """
    total_win = sum(p for p in winning_pnls if p > 0)
    total_loss = sum(abs(p) for p in losing_pnls if p < 0)
    if total_loss == 0:
        return float("inf") if total_win > 0 else 0.0
    return total_win / total_loss


def summarize_trades(
    pnl_amounts: Sequence[float],
    initial_equity: float = 10000.0,
    periods_per_year: float = 365.0,
) -> TradeStats:
    """
    对一组交易的 pnl 金额序列做汇总统计。

    参数:
        pnl_amounts: 每笔交易的净盈亏金额（按时间顺序）
        initial_equity: 初始资金，用于构建净值曲线
        periods_per_year: Sharpe 年化因子；对于"每笔交易"的序列，常用值 = 年均交易笔数
    """
    n = len(pnl_amounts)
    if n == 0:
        return TradeStats(
            trade_count=0, win_count=0, loss_count=0, win_rate_pct=0.0,
            avg_win=0.0, avg_loss=0.0, profit_factor=0.0,
            total_pnl=0.0, avg_pnl=0.0,
            sharpe_ratio=0.0, max_drawdown=0.0, max_drawdown_pct=0.0,
            calmar_ratio=0.0,
        )

    wins = [p for p in pnl_amounts if p > 0]
    losses = [p for p in pnl_amounts if p < 0]
    total = sum(pnl_amounts)
    avg = total / n

    equity = [initial_equity]
    for p in pnl_amounts:
        equity.append(equity[-1] + p)
    returns = [
        (equity[i + 1] - equity[i]) / equity[i] if equity[i] != 0 else 0.0
        for i in range(len(equity) - 1)
    ]

    sharpe = calc_sharpe_ratio(returns, periods_per_year=periods_per_year)
    max_dd, max_dd_pct = calc_max_drawdown(equity)

    total_return = (equity[-1] - initial_equity) / initial_equity
    # Calmar 用简化定义：回测窗口总收益率 / max_dd_pct（窗口内）
    calmar = (total_return / max_dd_pct) if max_dd_pct > 0 else (
        float("inf") if total_return > 0 else 0.0
    )

    return TradeStats(
        trade_count=n,
        win_count=len(wins),
        loss_count=len(losses),
        win_rate_pct=len(wins) / n * 100.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=calc_profit_factor(wins, losses),
        total_pnl=total,
        avg_pnl=avg,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        calmar_ratio=calmar,
    )
