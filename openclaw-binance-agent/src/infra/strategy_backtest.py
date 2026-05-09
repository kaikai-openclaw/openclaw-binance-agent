"""
最小策略回测工具。

用于对候选信号做快速、可重复的胜率/盈亏比检查。它不是完整撮合系统，
但能在策略上线前回答最基本的问题：同一类信号在给定持仓窗口内是否
具备正期望。
"""

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class BacktestTrade:
    symbol: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    outcome: str


@dataclass
class BacktestSummary:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_pnl_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float


def backtest_long_only_candidates(
    candidates: Iterable[dict],
    klines_by_symbol: dict[str, list[list]],
    hold_bars: int = 6,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
) -> tuple[BacktestSummary, list[BacktestTrade]]:
    """
    对候选信号做简单 long-only 前向持有回测。

    参数:
        candidates: 候选列表，至少包含 symbol。
        klines_by_symbol: 每个 symbol 的后续 K 线，kline[4] 为收盘价。
        hold_bars: 最大持有 K 线根数。
        stop_loss_pct / take_profit_pct: 可选止损/止盈百分比，正数输入。
    """
    trades: list[BacktestTrade] = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", ""))
        klines = klines_by_symbol.get(symbol, [])
        if not symbol or len(klines) < 2:
            continue
        entry_price = _to_float(klines[0][4])
        if entry_price <= 0:
            continue
        exit_price = _resolve_exit_price(
            entry_price,
            klines[1: hold_bars + 1],
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        if exit_price <= 0:
            continue
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        trades.append(
            BacktestTrade(
                symbol=symbol,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl_pct=round(pnl_pct, 4),
                outcome="win" if pnl_pct > 0 else "loss",
            )
        )
    return summarize_trades(trades), trades


def summarize_trades(trades: list[BacktestTrade]) -> BacktestSummary:
    if not trades:
        return BacktestSummary(
            trade_count=0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            avg_pnl_pct=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            expectancy_pct=0.0,
        )
    wins = [t.pnl_pct for t in trades if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in trades if t.pnl_pct <= 0]
    avg_pnl = sum(t.pnl_pct for t in trades) / len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return BacktestSummary(
        trade_count=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=round(len(wins) / len(trades) * 100, 4),
        avg_pnl_pct=round(avg_pnl, 4),
        avg_win_pct=round(avg_win, 4),
        avg_loss_pct=round(avg_loss, 4),
        expectancy_pct=round(avg_pnl, 4),
    )


def _resolve_exit_price(
    entry_price: float,
    future_klines: list[list],
    stop_loss_pct: Optional[float],
    take_profit_pct: Optional[float],
) -> float:
    if not future_klines:
        return 0.0
    stop_price = (
        entry_price * (1 - abs(stop_loss_pct) / 100)
        if stop_loss_pct
        else None
    )
    take_profit_price = (
        entry_price * (1 + abs(take_profit_pct) / 100)
        if take_profit_pct
        else None
    )
    for kline in future_klines:
        high = _to_float(kline[2])
        low = _to_float(kline[3])
        if stop_price is not None and low <= stop_price:
            return stop_price
        if take_profit_price is not None and high >= take_profit_price:
            return take_profit_price
    return _to_float(future_klines[-1][4])


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
