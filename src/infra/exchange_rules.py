"""
Binance 交易规则解析与下单数量规整。

从 exchangeInfo 中提取 LOT_SIZE / MIN_NOTIONAL 规则，用于在策略生成和
真实执行前保证 quantity 与最小名义金额满足交易所约束。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_MIN_NOTIONAL_USDT = Decimal("5")


@dataclass(frozen=True)
class SymbolTradingRule:
    """单个交易对的数量和名义金额规则。"""

    symbol: str
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


TradingRuleProvider = Callable[[str], Optional[SymbolTradingRule]]


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def parse_symbol_trading_rule(symbol_info: Dict[str, Any]) -> Optional[SymbolTradingRule]:
    """从 exchangeInfo 的单个 symbol 条目解析交易规则。"""
    symbol = str(symbol_info.get("symbol", ""))
    if not symbol:
        return None

    filters = symbol_info.get("filters", [])
    lot_size = None
    market_lot_size = None
    notional_filter = None

    for f in filters:
        filter_type = f.get("filterType")
        if filter_type == "LOT_SIZE":
            lot_size = f
        elif filter_type == "MARKET_LOT_SIZE":
            market_lot_size = f
        elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            notional_filter = f

    qty_filter = lot_size or market_lot_size
    if qty_filter is None:
        return None

    step_size = _to_decimal(qty_filter.get("stepSize"), "0")
    min_qty = _to_decimal(qty_filter.get("minQty"), "0")
    if step_size <= 0:
        return None

    min_notional = DEFAULT_MIN_NOTIONAL_USDT
    if notional_filter is not None:
        min_notional = max(
            _to_decimal(notional_filter.get("minNotional"), "0"),
            DEFAULT_MIN_NOTIONAL_USDT,
        )

    return SymbolTradingRule(
        symbol=symbol,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
    )


class BinanceTradingRules:
    """按 symbol 查询的 Binance 交易规则集合。"""

    def __init__(self, rules: Dict[str, SymbolTradingRule]) -> None:
        self._rules = rules

    @classmethod
    def from_exchange_info(cls, exchange_info: Dict[str, Any]) -> "BinanceTradingRules":
        rules: Dict[str, SymbolTradingRule] = {}
        for symbol_info in exchange_info.get("symbols", []):
            rule = parse_symbol_trading_rule(symbol_info)
            if rule is not None:
                rules[rule.symbol] = rule
        return cls(rules)

    def get(self, symbol: str) -> Optional[SymbolTradingRule]:
        return self._rules.get(symbol)


class LazyBinanceTradingRuleProvider:
    """懒加载 exchangeInfo，并缓存解析后的交易规则。"""

    def __init__(self, public_client: Any) -> None:
        self._public_client = public_client
        self._rules: Optional[BinanceTradingRules] = None

    def __call__(self, symbol: str) -> Optional[SymbolTradingRule]:
        if self._rules is None:
            info = self._public_client.get_exchange_info()
            self._rules = BinanceTradingRules.from_exchange_info(info)
        return self._rules.get(symbol)


def floor_quantity_to_step(quantity: float, step_size: Decimal) -> Decimal:
    """按 stepSize 向下取整；stepSize=1 时自然得到整数数量。"""
    raw_qty = Decimal(str(quantity))
    steps = (raw_qty / step_size).to_integral_value(rounding=ROUND_DOWN)
    return steps * step_size


def normalize_order_quantity(
    *,
    symbol: str,
    quantity: float,
    price: float,
    rule: SymbolTradingRule,
) -> Optional[float]:
    """
    将 quantity 按 LOT_SIZE 向下取整，并校验最小数量与最小名义金额。

    返回 None 表示规整后无法满足交易所约束。
    """
    if quantity <= 0 or price <= 0:
        return None

    adjusted = floor_quantity_to_step(quantity, rule.step_size)
    price_decimal = Decimal(str(price))
    if adjusted <= 0 or adjusted < rule.min_qty:
        log.warning(
            "%s 数量 %.12g 按 stepSize=%s 取整后为 %s，低于 minQty=%s",
            symbol,
            quantity,
            rule.step_size,
            adjusted,
            rule.min_qty,
        )
        return None

    notional = adjusted * price_decimal
    min_notional = max(rule.min_notional, DEFAULT_MIN_NOTIONAL_USDT)
    if notional < min_notional:
        log.warning(
            "%s 名义金额 %s 低于最小要求 %s USDT，跳过",
            symbol,
            notional,
            min_notional,
        )
        return None

    return float(adjusted)
