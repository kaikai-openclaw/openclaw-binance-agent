"""
Binance 日内已实现盈亏计算。

RiskController 的日亏损熔断依赖 AccountState.daily_realized_pnl。
本模块从 Binance userTrades 拉取当天真实 realizedPnl，避免生产入口把
daily_realized_pnl 固定为 0 导致熔断失效。
"""

import logging
from datetime import datetime, time, timezone
from typing import Iterable, Optional

from src.infra.binance_fapi import BinanceFapiClient

log = logging.getLogger(__name__)


def utc_day_start_ms(now: Optional[datetime] = None) -> int:
    """返回 UTC 当日 00:00:00 的毫秒时间戳。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return int(day_start.timestamp() * 1000)


def calculate_daily_realized_pnl(
    client: BinanceFapiClient,
    symbols: Iterable[str],
    start_time_ms: Optional[int] = None,
) -> float:
    """
    按指定交易对汇总当天 Binance 已实现盈亏。

    Binance `GET /fapi/v1/userTrades` 需要 symbol 参数，因此这里以当前持仓、
    本轮候选或近期执行涉及的 symbol 集合作为输入。无法覆盖完全未在输入集合
    中出现的历史交易对，调用方应尽量传入当前持仓和本轮执行相关 symbol。
    """
    start_time_ms = start_time_ms or utc_day_start_ms()
    total = 0.0
    for symbol in sorted({s for s in symbols if s}):
        try:
            trades = client.get_user_trades(
                symbol=symbol,
                start_time=start_time_ms,
                limit=1000,
            )
        except Exception as exc:
            log.warning("计算 %s 日内已实现盈亏失败: %s", symbol, exc)
            continue

        for trade in trades:
            total += _to_float(trade.get("realizedPnl"))
    return total


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
