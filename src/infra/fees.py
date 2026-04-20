"""
手续费与滑点建模（P0-4）

统一封装 Binance U 本位合约和 A 股的交易成本计算，供：
  - Skill-3 策略制定：计算净盈亏比，拒绝费后亏本交易
  - Skill-5 展示进化：统计净胜率 / 净盈亏比（扣除手续费 + 滑点）
  - 回测框架：对历史信号模拟真实交易成本

市场成本参考（2026-04 现值，需定期 review）:
  - Binance U 本位合约（普通账户，无 VIP）:
      Maker 0.02%（0.0002），Taker 0.05%（0.0005）
      滑点 taker 假设 0.02%（0.0002，深度币种），maker 假设 0
  - A 股：
      佣金 双边 0.025%（0.00025），最低 5 元
      印花税 卖出单边 0.05%（0.0005）
      过户费 沪市双边 0.00341%（≈ 0.00003，近似忽略或简化）

所有常量集中，支持通过配置或参数覆盖，便于 VIP 折扣或券商特殊费率场景。
"""

from dataclasses import dataclass
from typing import Literal, Optional


# ============================================================
# 市场费率常量
# ============================================================

# --- Binance U 本位合约 ---
CRYPTO_MAKER_FEE_RATE = 0.0002      # 0.02%
CRYPTO_TAKER_FEE_RATE = 0.0005      # 0.05%
CRYPTO_DEFAULT_SLIPPAGE = 0.0002    # 0.02%（taker 入场/出场典型 1-tick 成本）

# --- A 股（沪深） ---
ASTOCK_COMMISSION_RATE = 0.00025    # 双边佣金 0.025%
ASTOCK_MIN_COMMISSION = 5.0         # 佣金最低 5 元/笔
ASTOCK_STAMP_DUTY_RATE = 0.0005     # 卖出印花税 0.05%（买入免）
ASTOCK_TRANSFER_FEE_RATE = 0.00003  # 过户费约 0.003%（仅沪市；简化为双边统一）
ASTOCK_DEFAULT_SLIPPAGE = 0.001     # 0.1%（主板撮合滑点典型估值；小票更高）


MarketType = Literal["crypto", "astock"]
CryptoOrderType = Literal["maker", "taker"]
AStockSide = Literal["buy", "sell"]


@dataclass
class FeeBreakdown:
    """单笔订单费用细目。"""
    commission: float     # 佣金/手续费
    tax: float            # 印花税/过户费等
    slippage_cost: float  # 估算的滑点成本（相对名义值）
    total: float          # 合计

    @property
    def rate_on_notional(self) -> float:
        """相对名义成交额的成本占比。"""
        # 防御：调用方需保证 notional 非零
        return self.total


def calc_crypto_fee(
    notional: float,
    order_type: CryptoOrderType = "taker",
    maker_rate: float = CRYPTO_MAKER_FEE_RATE,
    taker_rate: float = CRYPTO_TAKER_FEE_RATE,
    slippage: float = CRYPTO_DEFAULT_SLIPPAGE,
    vip_discount: float = 0.0,
) -> FeeBreakdown:
    """
    计算 Binance U 本位合约单边成本（金额）。

    参数:
        notional: 名义成交额（数量 × 价格，USDT）
        order_type: "maker" 或 "taker"
        maker_rate / taker_rate: 可覆盖默认费率
        slippage: 滑点比例（仅 taker 实际发生；maker 挂单无滑点）
        vip_discount: VIP 折扣系数（0-1），最终费率 = 原费率 × (1 - discount)

    返回:
        FeeBreakdown（total 为金额）
    """
    if notional <= 0:
        return FeeBreakdown(0.0, 0.0, 0.0, 0.0)

    rate = maker_rate if order_type == "maker" else taker_rate
    rate *= max(0.0, 1.0 - vip_discount)
    commission = notional * rate
    slip = notional * slippage if order_type == "taker" else 0.0
    return FeeBreakdown(
        commission=commission,
        tax=0.0,
        slippage_cost=slip,
        total=commission + slip,
    )


def calc_astock_fee(
    notional: float,
    side: AStockSide,
    commission_rate: float = ASTOCK_COMMISSION_RATE,
    min_commission: float = ASTOCK_MIN_COMMISSION,
    stamp_duty: float = ASTOCK_STAMP_DUTY_RATE,
    transfer_fee: float = ASTOCK_TRANSFER_FEE_RATE,
    slippage: float = ASTOCK_DEFAULT_SLIPPAGE,
) -> FeeBreakdown:
    """
    计算 A 股单边成本（金额）。

    参数:
        notional: 名义成交额（元）
        side: "buy"（免印花）或 "sell"（含印花税 0.05%）
        commission_rate / min_commission: 佣金率与最低佣金
        stamp_duty: 印花税率（仅卖出）
        transfer_fee: 过户费率（双边，简化统一）
        slippage: 滑点比例

    返回:
        FeeBreakdown
    """
    if notional <= 0:
        return FeeBreakdown(0.0, 0.0, 0.0, 0.0)

    commission = max(notional * commission_rate, min_commission)
    tax = notional * transfer_fee
    if side == "sell":
        tax += notional * stamp_duty
    slip = notional * slippage
    return FeeBreakdown(
        commission=commission,
        tax=tax,
        slippage_cost=slip,
        total=commission + tax + slip,
    )


def calc_round_trip_cost_pct(
    market: MarketType,
    notional: Optional[float] = None,
    order_type: CryptoOrderType = "taker",
    maker_rate: float = CRYPTO_MAKER_FEE_RATE,
    taker_rate: float = CRYPTO_TAKER_FEE_RATE,
    crypto_slippage: float = CRYPTO_DEFAULT_SLIPPAGE,
    astock_slippage: float = ASTOCK_DEFAULT_SLIPPAGE,
    vip_discount: float = 0.0,
) -> float:
    """
    估算"入场 + 出场"一次完整往返的总成本占名义金额的比例（P0-4 的核心）。

    用于 Skill-3 计算净盈亏比：
        净盈亏比 = (止盈距离 - round_trip_cost) / (止损距离 + round_trip_cost)

    参数:
        market: "crypto" 或 "astock"
        notional: A 股场景下用于判断是否触发最低佣金 5 元；
                  crypto 场景忽略（按比例计算）
        order_type: crypto 的订单类型（两边默认都是 taker）
        vip_discount: crypto VIP 折扣

    返回:
        round-trip 成本占名义金额的比例（如 0.0014 = 0.14%）
    """
    if market == "crypto":
        rate = maker_rate if order_type == "maker" else taker_rate
        rate *= max(0.0, 1.0 - vip_discount)
        slippage_leg = crypto_slippage if order_type == "taker" else 0.0
        # 开仓 + 平仓各一次
        return 2 * (rate + slippage_leg)

    if market == "astock":
        notional_for_calc = notional if (notional and notional > 0) else 100_000.0
        buy = calc_astock_fee(notional_for_calc, "buy", slippage=astock_slippage)
        sell = calc_astock_fee(notional_for_calc, "sell", slippage=astock_slippage)
        return (buy.total + sell.total) / notional_for_calc

    raise ValueError(f"未知市场类型: {market}")


def apply_fees_to_pnl(
    gross_pnl: float,
    entry_notional: float,
    exit_notional: float,
    market: MarketType,
    order_type: CryptoOrderType = "taker",
    vip_discount: float = 0.0,
) -> float:
    """
    给"毛盈亏"扣除完整往返手续费与滑点，返回净盈亏。

    用于 Skill-5 / 回测统计净胜率和净盈亏比。

    参数:
        gross_pnl: 毛盈亏（已考虑方向）
        entry_notional: 入场名义金额（|quantity × entry_price|）
        exit_notional: 出场名义金额（|quantity × exit_price|）

    返回:
        扣费后净盈亏
    """
    if market == "crypto":
        fee_in = calc_crypto_fee(entry_notional, order_type, vip_discount=vip_discount).total
        fee_out = calc_crypto_fee(exit_notional, order_type, vip_discount=vip_discount).total
    elif market == "astock":
        fee_in = calc_astock_fee(entry_notional, "buy").total
        fee_out = calc_astock_fee(exit_notional, "sell").total
    else:
        raise ValueError(f"未知市场类型: {market}")
    return gross_pnl - fee_in - fee_out


def net_rr_ratio(
    sl_distance_pct: float,
    tp_distance_pct: float,
    market: MarketType,
    order_type: CryptoOrderType = "taker",
    vip_discount: float = 0.0,
) -> float:
    """
    给定止损/止盈距离（相对入场价的比例），返回扣费后的净盈亏比 (TP - cost) / (SL + cost)。

    如果返回 < 1.0，说明盈亏比扣费后不划算，调用方应当拒绝该交易。
    如果返回 <= 0，说明止盈扣费后仍为亏损（极窄止盈 + 高费用）。

    参数:
        sl_distance_pct: 止损距离占入场价的比例（正数，如 0.03）
        tp_distance_pct: 止盈距离占入场价的比例（正数，如 0.06）
    """
    cost = calc_round_trip_cost_pct(
        market=market, order_type=order_type, vip_discount=vip_discount
    )
    net_tp = tp_distance_pct - cost
    net_sl = sl_distance_pct + cost
    if net_sl <= 0:
        return 0.0
    return net_tp / net_sl
