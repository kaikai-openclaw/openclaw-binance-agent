"""
fees.py 单元测试（P0-4）

覆盖：
  - 币圈 maker/taker 费率与滑点
  - A 股佣金 + 印花税 + 过户费 + 最低佣金
  - round-trip 成本占比计算
  - 净盈亏计算与净盈亏比
"""

import pytest

from src.infra.fees import (
    ASTOCK_COMMISSION_RATE,
    ASTOCK_MIN_COMMISSION,
    ASTOCK_STAMP_DUTY_RATE,
    CRYPTO_MAKER_FEE_RATE,
    CRYPTO_TAKER_FEE_RATE,
    apply_fees_to_pnl,
    calc_astock_fee,
    calc_crypto_fee,
    calc_round_trip_cost_pct,
    net_rr_ratio,
)


# ══════════════════════════════════════════════════════════
# 1. 币圈 U 本位合约费率
# ══════════════════════════════════════════════════════════

class TestCryptoFee:

    def test_taker_default(self):
        fee = calc_crypto_fee(10000.0, order_type="taker")
        # 佣金 = 10000 × 0.0005 = 5.0
        # 滑点 = 10000 × 0.0002 = 2.0
        assert fee.commission == pytest.approx(5.0)
        assert fee.slippage_cost == pytest.approx(2.0)
        assert fee.total == pytest.approx(7.0)
        assert fee.tax == 0.0

    def test_maker_no_slippage(self):
        fee = calc_crypto_fee(10000.0, order_type="maker")
        # 佣金 = 10000 × 0.0002 = 2.0, 滑点 = 0
        assert fee.commission == pytest.approx(2.0)
        assert fee.slippage_cost == 0.0
        assert fee.total == pytest.approx(2.0)

    def test_zero_notional(self):
        fee = calc_crypto_fee(0.0)
        assert fee.total == 0.0

    def test_vip_discount(self):
        fee_normal = calc_crypto_fee(10000.0, "taker", vip_discount=0.0)
        fee_vip = calc_crypto_fee(10000.0, "taker", vip_discount=0.5)
        # VIP 50% 折扣：佣金减半；滑点不变
        assert fee_vip.commission == pytest.approx(fee_normal.commission * 0.5)
        assert fee_vip.slippage_cost == fee_normal.slippage_cost


# ══════════════════════════════════════════════════════════
# 2. A 股费率
# ══════════════════════════════════════════════════════════

class TestAStockFee:

    def test_buy_no_stamp_duty(self):
        # 10 万元买入：佣金 25，过户费 3，滑点 100
        fee = calc_astock_fee(100000.0, side="buy")
        assert fee.commission == pytest.approx(25.0)
        assert fee.tax == pytest.approx(100000 * 0.00003)
        assert fee.total > fee.commission  # 含滑点

    def test_sell_has_stamp_duty(self):
        fee_buy = calc_astock_fee(100000.0, side="buy")
        fee_sell = calc_astock_fee(100000.0, side="sell")
        # 卖出比买入多 0.05% 印花税
        diff = fee_sell.tax - fee_buy.tax
        assert diff == pytest.approx(100000 * ASTOCK_STAMP_DUTY_RATE)

    def test_min_commission_triggered(self):
        # 小额交易 1 万元佣金 = 2.5 元 < 最低 5 元
        fee = calc_astock_fee(10000.0, side="buy")
        assert fee.commission == ASTOCK_MIN_COMMISSION

    def test_min_commission_not_triggered(self):
        # 大额交易 100 万元佣金 = 250 元 > 最低 5 元
        fee = calc_astock_fee(1_000_000.0, side="buy")
        assert fee.commission == pytest.approx(1_000_000 * ASTOCK_COMMISSION_RATE)


# ══════════════════════════════════════════════════════════
# 3. Round-trip 成本占比
# ══════════════════════════════════════════════════════════

class TestRoundTripCost:

    def test_crypto_taker_rr(self):
        cost = calc_round_trip_cost_pct("crypto", order_type="taker")
        # 2 × (0.0005 + 0.0002) = 0.0014 = 0.14%
        assert cost == pytest.approx(0.0014)

    def test_crypto_maker_rr(self):
        cost = calc_round_trip_cost_pct("crypto", order_type="maker")
        # 2 × (0.0002 + 0) = 0.0004 = 0.04%
        assert cost == pytest.approx(0.0004)

    def test_astock_rr_reasonable(self):
        # A 股 10 万元 round-trip 成本占比约在 0.3% 左右（含双边滑点 0.2% + 费税 0.1%）
        cost = calc_round_trip_cost_pct("astock", notional=100000.0)
        assert 0.0025 < cost < 0.004

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError):
            calc_round_trip_cost_pct("forex")  # type: ignore


# ══════════════════════════════════════════════════════════
# 4. 净盈亏比与净盈亏
# ══════════════════════════════════════════════════════════

class TestNetPnl:

    def test_net_rr_ratio_crypto_taker(self):
        # 原设计 3%/6% 盈亏比 2:1，币圈 taker 扣费后：
        # net = (0.06 - 0.0014) / (0.03 + 0.0014) = 0.0586 / 0.0314 ≈ 1.866
        ratio = net_rr_ratio(0.03, 0.06, "crypto", "taker")
        assert 1.8 < ratio < 1.95

    def test_net_rr_ratio_below_one_when_tight(self):
        # 止损止盈都只有 0.1%，币圈 taker 0.14% 费率足以让净盈亏比亏本
        ratio = net_rr_ratio(0.001, 0.001, "crypto", "taker")
        assert ratio < 1.0

    def test_apply_fees_to_pnl_crypto(self):
        # 10000 USDT 入场，10500 出场（毛赚 500），taker 单边费 ≈ 7
        net = apply_fees_to_pnl(
            gross_pnl=500.0,
            entry_notional=10000.0,
            exit_notional=10500.0,
            market="crypto",
            order_type="taker",
        )
        # 净 ≈ 500 - 7 - (10500 × 0.0007) ≈ 500 - 7 - 7.35 ≈ 485.65
        assert 480 < net < 490

    def test_apply_fees_to_pnl_astock_sell_gain(self):
        # A 股 10 万买入 10.5 万卖出（毛赚 5000）
        net = apply_fees_to_pnl(
            gross_pnl=5000.0,
            entry_notional=100000.0,
            exit_notional=105000.0,
            market="astock",
        )
        # 扣费后应显著小于 5000（双边佣金 + 印花税 + 过户费 + 滑点）
        assert net < 5000
        # 但也不应离谱（总成本占 <1%）
        assert net > 4500
