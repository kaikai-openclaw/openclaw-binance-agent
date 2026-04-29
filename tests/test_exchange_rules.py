from decimal import Decimal

from src.infra.exchange_rules import (
    BinanceTradingRules,
    SymbolTradingRule,
    normalize_order_quantity,
)


def test_step_size_one_floors_to_integer_quantity():
    rule = SymbolTradingRule(
        symbol="APEUSDT",
        step_size=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("5"),
    )

    quantity = normalize_order_quantity(
        symbol="APEUSDT",
        quantity=12.987,
        price=1.25,
        rule=rule,
    )

    assert quantity == 12.0


def test_min_notional_below_five_is_rejected():
    rule = SymbolTradingRule(
        symbol="PULUSDT",
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )

    quantity = normalize_order_quantity(
        symbol="PULUSDT",
        quantity=10.0,
        price=0.4,
        rule=rule,
    )

    assert quantity is None


def test_parse_rules_from_exchange_info_uses_lot_size_and_notional():
    rules = BinanceTradingRules.from_exchange_info({
        "symbols": [{
            "symbol": "DAMUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            ],
        }]
    })

    rule = rules.get("DAMUSDT")

    assert rule is not None
    assert rule.step_size == Decimal("1")
    assert rule.min_notional == Decimal("5")
