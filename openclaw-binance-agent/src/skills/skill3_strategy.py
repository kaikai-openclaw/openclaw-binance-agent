"""
Skill-3：交易策略制定

基于 Skill-2 输出的评级结果，为每个目标币种生成量化交易计划。
使用固定风险模型计算头寸规模，执行风控预校验并自动裁剪超限头寸。

RiskController 和 account_state_provider 通过构造函数注入，便于测试时 mock。

止损止盈策略（P0-1 改造后）:
  - 优先使用 ATR 动态止损：rating 透传的 atr_pct（来自 Skill-1）→ 止损距离 = atr_pct × atr_stop_mult
    盈亏比固定 2:1（atr_tp_mult / atr_stop_mult）
  - 止损距离会 clip 到 [min_stop_pct, max_stop_pct]，避免极端波动或极低波动下的病态止损
  - 若 ATR 推导出的原始止损距离超过 max_stop_pct，视为波动过大并跳过，不强行截断进场
  - 回退路径：无 atr_pct 时沿用旧的固定百分比（3%/6%）并记录 warning

价格来源（P0-2 改造后）:
  - 生产路径（require_market_price=True）：market_price_provider 必须返回正数，否则该币种被跳过
  - 测试路径（require_market_price=False，默认）：provider 缺失/失败时回退到 TEST_FALLBACK_PRICE=100.0
    仅用于单元测试，日志会明确标注

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.infra.exchange_rules import (
    TradingRuleProvider,
    normalize_order_quantity,
)
from src.infra.fees import (
    CRYPTO_MAKER_FEE_RATE,
    CRYPTO_TAKER_FEE_RATE,
    calc_round_trip_cost_pct,
)
from src.infra.memory_store import MemoryStore
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    OrderRequest,
    PipelineStatus,
    TradeDirection,
    calculate_position_size,
)
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# 默认风险比例（2%）
DEFAULT_RISK_RATIO = 0.02

# 默认持仓时间上限（小时）
DEFAULT_MAX_HOLD_HOURS = 12.0  # 1h策略实际平均持仓<1h，统一缩短让时间衰减止盈真正生效

# 默认杠杆倍数
DEFAULT_LEVERAGE = 10
DEFAULT_SHORT_LEVERAGE = 5

# 止损止盈比例常量（仅在没有 ATR 时的回退路径使用）
STOP_LOSS_PCT = 0.03  # 止损幅度 3%
TAKE_PROFIT_PCT = 0.06  # 止盈幅度 6%（盈亏比 2:1）

# ATR 动态止损默认参数
DEFAULT_ATR_STOP_MULT = 1.5  # 止损距离 = ATR × 1.5
DEFAULT_ATR_TP_MULT = 2.3  # 止盈距离 = ATR × 2.3（盈亏比 1.53:1）
DEFAULT_MIN_STOP_PCT = 0.005  # 止损距离下限 0.5%（防止极低波动下 SL 贴得过近被秒扫）
DEFAULT_MAX_STOP_PCT = (
    0.07  # 止损距离上限 7%（10x 杠杆下保证金最多亏 70%，留 30% 安全边际防滑点强平）
)
DEFAULT_MAX_STOP_USDT = 0.03  # 做空硬顶止损距离 3%（币价 × 3%，防止趋势反转时亏损无限）
DEFAULT_SHORT_TRAILING_ACTIVATION_MULT = (
    0.8  # 做空移动止损激活系数（从1.5收紧至0.8，更快锁利）
)
DEFAULT_SHORT_TRAILING_ACTIVATION_MULT_HV = 1.2  # 做空高波动移动止损激活系数

# 入场区间宽度常量
ENTRY_SPREAD_MIN = 0.01  # 最窄区间（置信度 100% 时）
ENTRY_SPREAD_MAX = 0.05  # 最宽区间（置信度 0% 时）

# 测试路径的标准化基准价（仅在未注入任何 provider 时使用）
TEST_FALLBACK_PRICE = 100.0

# P0-4：扣费后净盈亏比的最低阈值；低于此值的交易直接拒绝
DEFAULT_MIN_NET_RR_RATIO = 1.2
DEFAULT_TRAILING_STOP_RATIO = 0.5
DEFAULT_TRAILING_ACTIVATION_MULT = (
    1.0  # 常规币 trailing stop 激活距离 = 止损距离 × 此值
)
DEFAULT_TRAILING_ACTIVATION_MULT_HV = 1.5  # 高波动币 trailing stop 激活距离乘数
DEFAULT_HIGH_VOL_TP_MULT = 3.0  # 高波动币止盈乘数（与常规币保持同等盈亏比，不再放大）

# 市场价格提供者类型：接收 symbol，返回当前市场价格（None 或 <=0 表示不可用）
MarketPriceProvider = Callable[[str], Optional[float]]

# 账户状态提供者类型：无参数调用，返回 AccountState
AccountStateProvider = Callable[[], AccountState]


class Skill3Strategy(BaseSkill):
    """
    交易策略制定 Skill。

    从 State_Store 读取 Skill-2 输出的评级结果，
    为每个目标币种生成交易计划（方向、入场区间、头寸规模、止损、止盈、持仓上限），
    执行风控预校验并自动裁剪超限头寸。

    需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        risk_controller: RiskController,
        account_state_provider: AccountStateProvider,
        market_price_provider: Optional[MarketPriceProvider] = None,
        risk_ratio: float = DEFAULT_RISK_RATIO,
        max_hold_hours: float = DEFAULT_MAX_HOLD_HOURS,
        leverage: int = DEFAULT_LEVERAGE,
        short_leverage: int = DEFAULT_SHORT_LEVERAGE,
        require_market_price: bool = True,
        atr_stop_mult: float = DEFAULT_ATR_STOP_MULT,
        atr_tp_mult: float = DEFAULT_ATR_TP_MULT,
        min_stop_pct: float = DEFAULT_MIN_STOP_PCT,
        max_stop_pct: float = DEFAULT_MAX_STOP_PCT,
        fee_market: str = "crypto",
        fee_order_type: str = "taker",
        fee_vip_discount: float = 0.0,
        min_net_rr_ratio: float = DEFAULT_MIN_NET_RR_RATIO,
        trading_rule_provider: Optional[TradingRuleProvider] = None,
        memory_store: Optional[MemoryStore] = None,
        max_trades: int = 4,
        trailing_stop_ratio: float = DEFAULT_TRAILING_STOP_RATIO,
        trailing_activation_mult: float = DEFAULT_TRAILING_ACTIVATION_MULT,
        trailing_activation_mult_hv: float = DEFAULT_TRAILING_ACTIVATION_MULT_HV,
        high_vol_tp_mult: float = DEFAULT_HIGH_VOL_TP_MULT,
        max_position_pct: float = 20.0,
        max_margin_usdt: Optional[float] = None,
    ) -> None:
        """
        初始化 Skill-3。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            risk_controller: 风控拦截层实例
            account_state_provider: 账户状态提供回调（返回 AccountState）
            market_price_provider: 市场价格提供回调（接收 symbol，返回当前价格）
                - None 或返回 None/0/负数 时的行为由 require_market_price 决定
            risk_ratio: 账户风险比例，默认 0.02（2%）
            max_hold_hours: 默认持仓时间上限（小时），默认 24
            leverage: 默认杠杆倍数（做多），默认 10
            short_leverage: 做空杠杆倍数，默认 5
            require_market_price: 是否要求必须拿到真实市场价（P0-2）
                - True（生产路径，默认）：provider 返回无效值时跳过该币种
                - False（测试路径）：回退到 TEST_FALLBACK_PRICE=100.0，
                  仅用于单元测试，生产环境禁止使用
            atr_stop_mult: ATR 止损乘数（止损距离 = atr_pct × atr_stop_mult）
            atr_tp_mult: ATR 止盈乘数（止盈距离 = atr_pct × atr_tp_mult），
                atr_tp_mult / atr_stop_mult 即实际盈亏比
            min_stop_pct: 止损距离相对入场价的下限（防止 SL 贴得过近）
            max_stop_pct: 止损距离相对入场价的上限（防止高波动币种仓位失控）
            fee_market: 费率市场类型，"crypto"（默认）或 "astock"
            fee_order_type: crypto 下单类型，"taker"（默认，更保守）或 "maker"
            fee_vip_discount: crypto VIP 费率折扣（0-1）
            min_net_rr_ratio: 扣费后净盈亏比下限（P0-4）；低于此值的交易直接拒绝
            trading_rule_provider: Binance 交易规则提供者；提供时按 LOT_SIZE 规整 quantity，
                并校验最小名义金额（至少 5 USDT）
            memory_store: 可选，注入后每轮从最新反思日志动态读取
                risk_ratio，无需重启即可感知 Skill-5 的进化结果
            max_trades: 单轮最多交易数量，默认 4
            max_position_pct: 单笔持仓占账户资金的上限百分比，默认 20%。
                1h 等高频策略建议设为 2-5% 以控制总敞口。
            max_margin_usdt: 单笔保证金绝对金额上限（USDT）。设置后优先于
                max_position_pct 生效，适合固定每笔风险金额的场景。
                例如设为 10.0 则每笔保证金不超过 10 USDT（名义价值不超过
                10 × 杠杆 USDT）。None 表示不限制（默认）。
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill3_strategy"
        self._risk_controller = risk_controller
        self._account_state_provider = account_state_provider
        self._market_price_provider = market_price_provider
        self._risk_ratio = risk_ratio
        self._max_hold_hours = max_hold_hours
        self._leverage = leverage
        self._short_leverage = short_leverage
        self._require_market_price = require_market_price
        self._atr_stop_mult = atr_stop_mult
        self._atr_tp_mult = atr_tp_mult
        self._min_stop_pct = min_stop_pct
        self._max_stop_pct = max_stop_pct
        self._fee_market = fee_market
        self._fee_order_type = fee_order_type
        self._fee_vip_discount = fee_vip_discount
        self._min_net_rr_ratio = min_net_rr_ratio
        self._max_trades = max_trades
        self._trailing_stop_ratio = trailing_stop_ratio
        self._trailing_activation_mult = trailing_activation_mult
        self._trailing_activation_mult_hv = trailing_activation_mult_hv
        self._high_vol_tp_mult = high_vol_tp_mult
        self._trading_rule_provider = trading_rule_provider
        self._memory_store = memory_store
        self._max_position_pct = max_position_pct
        self._max_margin_usdt = max_margin_usdt

        # 预计算 round-trip 成本占比（每次下单一致，无需每笔重算）
        try:
            self._round_trip_cost_pct = calc_round_trip_cost_pct(
                market=fee_market,
                order_type=fee_order_type,
                vip_discount=fee_vip_discount,
            )
        except ValueError:
            log.warning(f"[{self.name}] fee_market={fee_market} 未知，禁用费用建模")
            self._round_trip_cost_pct = 0.0

    def run(self, input_data: dict) -> dict:
        """
        执行交易策略制定。

        流程:
        1. 从 State_Store 读取评级结果（通过 input_state_id）
        2. 处理空评级列表场景
        3. 获取当前账户状态
        4. 为每个目标币种生成交易计划
        5. 执行风控预校验并裁剪超限头寸
        6. 组装输出

        参数:
            input_data: 经 Schema 校验的输入，包含 input_state_id

        返回:
            符合 skill3_output.json Schema 的输出字典
        """
        input_state_id = input_data["input_state_id"]

        # 热更新：每轮从 MemoryStore 读取最新进化参数，无需重启
        if self._memory_store is not None:
            _, evolved_risk = self._memory_store.get_evolved_params(
                default_risk_ratio=self._risk_ratio,
            )
            effective_risk_ratio = evolved_risk
            if evolved_risk != self._risk_ratio:
                log.info(
                    f"[{self.name}] 热更新 risk_ratio: "
                    f"{self._risk_ratio} → {evolved_risk}（来自 MemoryStore）"
                )
        else:
            effective_risk_ratio = self._risk_ratio

        # 步骤 1：从 State_Store 读取评级结果
        upstream_data = self.state_store.load(input_state_id)
        ratings = upstream_data.get("ratings", [])

        log.info(
            f"[{self.name}] 读取到 {len(ratings)} 个目标币种，"
            f"input_state_id={input_state_id}"
        )

        # 步骤 2：空评级列表 → 标记"本轮无交易机会"
        if not ratings:
            log.info(f"[{self.name}] 本轮无交易机会（评级列表为空）")
            return {
                "state_id": str(uuid.uuid4()),
                "trade_plans": [],
                "rejected_symbols": [],
                "pipeline_status": PipelineStatus.NO_OPPORTUNITY.value,
            }

        # 步骤 3：获取当前账户状态
        account = self._account_state_provider()

        # 步骤 3.5：按评分+置信度排序，只取前 max_trades 个
        if len(ratings) > self._max_trades:
            ratings_sorted = sorted(
                ratings,
                key=lambda r: r.get("rating_score", 0),
                reverse=True,
            )
            skipped = ratings_sorted[self._max_trades :]
            ratings = ratings_sorted[: self._max_trades]
            skipped_symbols = [r.get("symbol", "") for r in skipped]
            log.info(
                f"[{self.name}] 评级通过 {len(ratings) + len(skipped)} 个，"
                f"只取评分最高的 {self._max_trades} 个，"
                f"跳过: {', '.join(skipped_symbols)}"
            )

        # 步骤 4 & 5：为每个目标币种生成交易计划（使用热更新后的 risk_ratio）
        trade_plans: List[Dict[str, Any]] = []
        rejected_symbols: List[Dict[str, str]] = []
        for rating in ratings:
            plan, rejection_reason = self._generate_trade_plan(
                rating, account, effective_risk_ratio
            )
            if plan is not None:
                trade_plans.append(plan)
            elif rejection_reason:
                rejected_symbols.append(
                    {
                        "symbol": rating.get("symbol", ""),
                        "reason": rejection_reason,
                        "rating_score": rating.get("rating_score", 0),
                        "signal": rating.get("signal", ""),
                    }
                )

        # 判断 pipeline_status
        if trade_plans:
            pipeline_status = PipelineStatus.HAS_TRADES.value
        else:
            pipeline_status = PipelineStatus.NO_OPPORTUNITY.value
            log.info(f"[{self.name}] 所有交易计划均未通过风控预校验")

        output = {
            "state_id": str(uuid.uuid4()),
            "trade_plans": trade_plans,
            "rejected_symbols": rejected_symbols,
            "pipeline_status": pipeline_status,
        }

        log.info(
            f"[{self.name}] 策略制定完成: "
            f"输入={len(ratings)}, 输出={len(trade_plans)}, "
            f"状态={pipeline_status}"
        )

        return output

    def _generate_trade_plan(
        self,
        rating: Dict[str, Any],
        account: AccountState,
        risk_ratio: Optional[float] = None,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        为单个目标币种生成交易计划。

        包含：方向推导、入场区间计算、头寸规模计算、止损止盈设定、
        风控预校验与超限裁剪。

        参数:
            rating: 评级结果字典（symbol, rating_score, signal, confidence）
            account: 当前账户状态
            risk_ratio: 本轮有效风险比例；None 时回退到 self._risk_ratio（构造时注入值）

        返回:
            (交易计划字典, None) 或 (None, 拒绝原因字符串)（信号为 hold 或风控拒绝时）
        """
        effective_risk = risk_ratio if risk_ratio is not None else self._risk_ratio
        symbol = rating.get("symbol", "")
        signal = rating.get("signal", "")
        confidence = rating.get("confidence", 0.0)
        atr_pct = rating.get(
            "atr_pct"
        )  # Skill-1/Skill-2 透传的 ATR 百分比（SL/TP 用，ATR 20 周期，更平滑）
        atr_filter_pct = rating.get(
            "atr_filter_pct"
        )  # 高波动过滤专用 ATR（ATR 14 周期，保留短期敏感度）
        wick_tip_price = rating.get(
            "wick_tip_price"
        )  # 插针 Skill 透传的影线尖端价（可选）
        rating_score = rating.get("rating_score", 0)  # 评级分数，用于判断是否临界通过

        # hold 信号时使用扫描层的预期方向（LLM 不确定但不反对）
        # 只有反方向才在 Skill2 被降级为 0 分拦截
        if signal == "hold":
            # 从上游候选数据推断方向：有 oversold/reversal → long，有 overbought → short
            inferred = rating.get("signal_direction", "")
            if inferred in ("long", "short"):
                signal = inferred
                log.info(f"[{self.name}] {symbol} LLM=hold，使用扫描预期方向 {signal}")
            else:
                log.info(f"[{self.name}] {symbol} 信号为 hold 且无预期方向，跳过")
                return None, "信号为 hold 且无预期方向"

        # 确定交易方向
        direction = TradeDirection.LONG if signal == "long" else TradeDirection.SHORT
        effective_leverage = (
            self._short_leverage if direction == TradeDirection.SHORT else self._leverage
        )

        # 计算入场价格区间（P0-2：生产路径 fail-fast）
        entry_range = self._calculate_entry_range(confidence, symbol)
        if entry_range is None:
            log.warning(
                f"[{self.name}] {symbol} 市场价格不可用（require_market_price=True），跳过"
            )
            return None, "市场价格不可用"
        entry_price, entry_upper, entry_lower, price_source = entry_range

        # 插针策略有 wick_tip_price 作为天然止损位，不依赖 ATR，跳过波动率检查
        # 高波动过滤优先使用 atr_filter_pct（ATR 14 周期），回退到 atr_pct（ATR 20 周期）
        effective_atr_skip = atr_filter_pct if atr_filter_pct is not None else atr_pct
        if not wick_tip_price and self._should_skip_for_excessive_volatility(
            effective_atr_skip, symbol, direction=direction
        ):
            return None, "ATR 波动过大"

        # 计算止损和止盈价格（优先级：wick_tip > ATR 动态 > 固定百分比）
        stop_loss_price, take_profit_price, sl_source = self._calculate_sl_tp(
            entry_price,
            direction,
            atr_pct,
            symbol,
            wick_tip_price=wick_tip_price,
        )

        # 需求 3.8：数值参数边界校验
        if entry_price <= 0 or stop_loss_price <= 0 or take_profit_price <= 0:
            log.warning(f"[{self.name}] {symbol} 价格参数无效，跳过")
            return None, "价格参数无效"

        # P0-4：扣费后净盈亏比守门
        sl_dist_pct = abs(entry_price - stop_loss_price) / entry_price
        tp_dist_pct = abs(take_profit_price - entry_price) / entry_price
        cost_pct = self._round_trip_cost_pct
        net_sl = sl_dist_pct + cost_pct
        net_tp = tp_dist_pct - cost_pct
        net_rr = (net_tp / net_sl) if net_sl > 0 else 0.0
        if net_tp <= 0 or net_rr < self._min_net_rr_ratio:
            log.warning(
                f"[{self.name}] {symbol} 扣费后净盈亏比 {net_rr:.2f} "
                f"低于阈值 {self._min_net_rr_ratio:.2f}（TP={tp_dist_pct:.4f} "
                f"SL={sl_dist_pct:.4f} cost={cost_pct:.4f}），跳过"
            )
            return None, f"净盈亏比 {net_rr:.2f} 低于阈值 {self._min_net_rr_ratio:.2f}"

        # 需求 3.2：使用固定风险模型计算头寸规模（使用热更新后的 effective_risk）
        try:
            position_size = calculate_position_size(
                account_balance=account.total_balance,
                risk_ratio=effective_risk,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
            )
        except ValueError as e:
            log.warning(f"[{self.name}] {symbol} 头寸规模计算失败: {e}")
            return None, f"头寸规模计算失败: {e}"

        # 转换为头寸规模百分比
        position_value = position_size * entry_price
        position_size_pct = (position_value / account.total_balance) * 100

        # ── 高波动/临界评分降仓（先执行，再 cap） ────────────────────────────
        # 高波动：ATR 14周期 > 4% 时，仓位减半（10x 杠杆下波动过大）
        # 注意：此逻辑必须在 cap 检查之前执行，否则高波动币种会被 cap 直接截断
        # 优先使用 atr_filter_pct（ATR 14 周期），因为它是真正传到 plan 的值；
        # atr_pct（ATR 20 周期）在某些评级里是 None，导致降仓失效
        effective_atr = atr_filter_pct if atr_filter_pct is not None else atr_pct
        if effective_atr is not None and effective_atr > 4.0:
            position_size_pct *= 0.5
            position_size = (
                position_size_pct / 100.0 * account.total_balance
            ) / entry_price
            position_value = position_size * entry_price
            log.info(
                f"[{self.name}] {symbol} ATR%={effective_atr:.2f}>4% 高波动，仓位降至 {position_size_pct:.2f}%"
            )
        # 临界评分：评级 6 分刚好及格，仓位减半控制风险
        elif rating_score == 6:
            position_size_pct *= 0.5
            position_size = (
                position_size_pct / 100.0 * account.total_balance
            ) / entry_price
            position_value = position_size * entry_price
            log.info(
                f"[{self.name}] {symbol} 评级分=6（临界），仓位降至 {position_size_pct:.2f}%"
            )

        # P2-8: 交割周仓位减半
        delivery_week = rating.get("delivery_week", False)
        if delivery_week:
            position_size_pct *= 0.5
            position_size = (
                position_size_pct / 100.0 * account.total_balance
            ) / entry_price
            position_value = position_size * entry_price
            log.info(
                f"[{self.name}] {symbol} 处于季度交割周，仓位降至 {position_size_pct:.2f}%"
            )

        # 需求 3.4 & 3.5：风控预校验 — 单笔持仓不超过 max_position_pct
        # cap 作为最终硬上限，在 ATR/评分降仓之后执行
        cap = self._max_position_pct
        if position_size_pct > cap:
            log.info(
                f"[{self.name}] {symbol} 头寸规模 {position_size_pct:.2f}% "
                f"超过 {cap:.0f}% 上限，裁剪至 {cap:.0f}%"
            )
            position_size_pct = cap
            position_size = (account.total_balance * cap / 100.0) / entry_price
            position_value = position_size * entry_price

        # 固定保证金金额上限：max_margin_usdt 优先于 max_position_pct
        if self._max_margin_usdt is not None and self._max_margin_usdt > 0:
            margin = position_value / effective_leverage
            if margin > self._max_margin_usdt:
                log.info(
                    f"[{self.name}] {symbol} 保证金 {margin:.2f} USDT "
                    f"超过上限 {self._max_margin_usdt:.2f} USDT，裁剪"
                )
                position_size = (self._max_margin_usdt * effective_leverage) / entry_price
                position_value = position_size * entry_price
                position_size_pct = (position_value / account.total_balance) * 100

        normalized_position_size = self._normalize_quantity_for_exchange(
            symbol=symbol,
            quantity=position_size,
            price=entry_price,
        )
        if normalized_position_size is None:
            log.warning(
                f"[{self.name}] {symbol} 头寸数量不满足交易所 LOT_SIZE/minNotional，跳过"
            )
            return None, "不满足交易所 LOT_SIZE/minNotional"
        if normalized_position_size != position_size:
            log.info(
                f"[{self.name}] {symbol} quantity 按交易所规则规整: "
                f"{position_size:.12g} -> {normalized_position_size:.12g}"
            )
            position_size = normalized_position_size
            position_value = position_size * entry_price
            position_size_pct = (position_value / account.total_balance) * 100

        # 需求 3.4：风控预校验 — 使用 RiskController 校验
        order_request = OrderRequest(
            symbol=symbol,
            direction=direction,
            price=entry_price,
            quantity=position_size,
            leverage=effective_leverage,
        )
        validation = self._risk_controller.validate_order(order_request, account)

        if not validation.passed:
            # 需求 3.5：尝试裁剪头寸至合规范围
            adjusted_plan = self._try_adjust_position(
                symbol,
                direction,
                entry_price,
                position_size,
                position_size_pct,
                account,
            )
            if adjusted_plan is not None:
                position_size = adjusted_plan["quantity"]
                position_size_pct = adjusted_plan["pct"]
                normalized_position_size = self._normalize_quantity_for_exchange(
                    symbol=symbol,
                    quantity=position_size,
                    price=entry_price,
                )
                if normalized_position_size is None:
                    log.warning(
                        f"[{self.name}] {symbol} 裁剪后数量不满足交易所规则，跳过"
                    )
                    return None, "裁剪后数量不满足交易所规则"
                position_size = normalized_position_size
                position_value = position_size * entry_price
                position_size_pct = (position_value / account.total_balance) * 100
                log.info(
                    f"[{self.name}] {symbol} 头寸已裁剪至 {position_size_pct:.2f}%"
                )
            else:
                log.warning(
                    f"[{self.name}] {symbol} 风控预校验失败且无法裁剪: "
                    f"{validation.reason}"
                )
                return None, validation.reason

        # 需求 3.8：最终边界校验
        if position_size_pct <= 0:
            log.warning(f"[{self.name}] {symbol} 头寸规模为零，跳过")
            return None, "头寸规模为零"

        plan = {
            "symbol": symbol,
            "direction": direction.value,
            "entry_price_upper": round(entry_upper, 8),
            "entry_price_lower": round(entry_lower, 8),
            "position_size_pct": round(position_size_pct, 4),
            "stop_loss_price": round(stop_loss_price, 8),
            "take_profit_price": round(take_profit_price, 8),
            "max_hold_hours": self._max_hold_hours,
        }
        plan["quantity"] = position_size
        plan["notional_value"] = round(position_size * entry_price, 8)
        # 审计字段：标注止损/价格来源 + 费率成本估算，便于回测归因（schema 可选字段）
        plan["stop_loss_source"] = sl_source
        plan["price_source"] = price_source
        plan["round_trip_cost_pct"] = round(cost_pct, 6)
        plan["net_rr_ratio"] = round(net_rr, 4)
        plan["strategy_tag"] = rating.get("strategy_tag") or "crypto_generic"

        # 优化的高波动率追踪止盈逻辑 (Trailing Stop 强化)
        # P0-2: 做空使用更快的移动止损激活系数（0.8 vs 1.0），更快锁利
        is_high_vol = atr_pct is not None and atr_pct >= 5.0
        if direction == TradeDirection.SHORT:
            activation_multiplier = (
                DEFAULT_SHORT_TRAILING_ACTIVATION_MULT_HV
                if is_high_vol
                else DEFAULT_SHORT_TRAILING_ACTIVATION_MULT
            )
        else:
            activation_multiplier = (
                self._trailing_activation_mult_hv
                if is_high_vol
                else self._trailing_activation_mult
            )
        activation_dist = abs(entry_price - stop_loss_price) * activation_multiplier

        # 移动止损激活价格：价格必须先向有利方向移动到 activation_price 才激活
        # 做空(LONG)：有利方向是上涨，activation_price 在入场价上方
        # 做空(SHORT)：有利方向是下跌，activation_price 在入场价下方
        if direction == TradeDirection.SHORT:
            activation_price = entry_price - activation_dist
        else:
            activation_price = entry_price + activation_dist

        plan["trailing_stop"] = {
            "activation_price": round(activation_price, 8),
            # Binance callbackRate 精度为 1 位小数（步进 0.1%），源头即规整
            "trail_pct": round(sl_dist_pct * self._trailing_stop_ratio * 100, 1),
        }
        return plan, None

    def _calculate_entry_range(
        self, confidence: float, symbol: str = ""
    ) -> Optional[tuple[float, float, float, str]]:
        """
        计算入场价格区间。

        行为（P0-2 改造）：
          - 优先从 market_price_provider 获取真实市场价格
          - provider 注入且返回正数 → source="market"
          - provider 未注入 / 返回无效值：
              - require_market_price=True（生产路径）→ 返回 None，调用方跳过该币种
              - require_market_price=False（测试路径）→ 回退 TEST_FALLBACK_PRICE，source="test_fallback"

        区间宽度与置信度成反比：置信度越高，区间越窄。

        参数:
            confidence: 置信度百分比（0-100）
            symbol: 币种符号（用于获取市场价格）

        返回:
            (基准价格, 区间上限, 区间下限, price_source) 或 None（生产路径且价格不可用）
        """
        base_price: Optional[float] = None
        if self._market_price_provider is not None and symbol:
            try:
                raw = self._market_price_provider(symbol)
                if raw is not None and raw > 0:
                    base_price = float(raw)
            except Exception as exc:
                log.warning(f"[{self.name}] 获取 {symbol} 市场价格失败: {exc}")

        price_source: str
        if base_price is not None:
            price_source = "market"
        else:
            if self._require_market_price:
                # P0-2：生产路径禁止魔数回退，返回 None 由上游跳过
                return None
            base_price = TEST_FALLBACK_PRICE
            price_source = "test_fallback"
            log.debug(
                f"[{self.name}] {symbol} 使用测试回退价格 {TEST_FALLBACK_PRICE} "
                f"(require_market_price=False)"
            )

        spread_pct = ENTRY_SPREAD_MAX - (confidence / 100.0) * (
            ENTRY_SPREAD_MAX - ENTRY_SPREAD_MIN
        )
        spread = base_price * spread_pct

        upper = base_price + spread
        lower = base_price - spread

        return base_price, upper, lower, price_source

    def _calculate_sl_tp(
        self,
        entry_price: float,
        direction: TradeDirection,
        atr_pct: Optional[float] = None,
        symbol: str = "",
        wick_tip_price: Optional[float] = None,
    ) -> tuple[float, float, str]:
        """
        计算止损和止盈价格（优先级：wick_tip > ATR 动态 > 固定百分比）。

        最优路径（插针尖端）：
            - 止损 = 影线尖端 × (1 ± 0.5% 缓冲)
            - 止盈 = 入场价 ± 止损距离 × (atr_tp_mult / atr_stop_mult)
            - 影线尖端是天然止损位：再次跌破说明不是插针而是趋势突破

        次优路径（ATR 动态）：
            - 止损距离 pct = clip(atr_pct / 100 × atr_stop_mult, min_stop_pct, max_stop_pct)
            - 止盈距离 pct = 止损距离 × (atr_tp_mult / atr_stop_mult)

        回退路径（无 atr_pct）：
            - 沿用 STOP_LOSS_PCT=3% / TAKE_PROFIT_PCT=6%

        做多：止损 = 入场价 × (1 - sl_pct)，止盈 = 入场价 × (1 + tp_pct)
        做空：止损 = 入场价 × (1 + sl_pct)，止盈 = 入场价 × (1 - tp_pct)

        返回:
            (止损价格, 止盈价格, 来源标记) 来源 ∈ {"wick_tip", "atr", "fixed"}
        """
        # 最优路径：插针尖端止损
        if wick_tip_price is not None and wick_tip_price > 0:
            stop_mult, tp_mult = self._get_dynamic_multipliers(
                atr_pct if atr_pct else 0
            )
            wick_buffer = 0.005  # 0.5% 缓冲，防止精确到尖端被扫
            if direction == TradeDirection.LONG and wick_tip_price < entry_price:
                stop_loss = wick_tip_price * (1 - wick_buffer)
                sl_dist_pct = (entry_price - stop_loss) / entry_price
                # clip 止损距离到合理范围
                sl_dist_pct = max(
                    self._min_stop_pct, min(self._max_stop_pct, sl_dist_pct)
                )
                stop_loss = entry_price * (1 - sl_dist_pct)
                tp_dist_pct = sl_dist_pct * (tp_mult / stop_mult)
                take_profit = entry_price * (1 + tp_dist_pct)
                log.info(
                    f"[{self.name}] {symbol} 插针尖端止损: tip={wick_tip_price:.8g}, "
                    f"sl={stop_loss:.8g}({sl_dist_pct:.4f}), tp={take_profit:.8g}"
                )
                return stop_loss, take_profit, "wick_tip"
            elif direction == TradeDirection.SHORT and wick_tip_price > entry_price:
                stop_loss = wick_tip_price * (1 + wick_buffer)
                sl_dist_pct = (stop_loss - entry_price) / entry_price
                sl_dist_pct = max(
                    self._min_stop_pct, min(self._max_stop_pct, sl_dist_pct)
                )
                stop_loss = entry_price * (1 + sl_dist_pct)
                tp_dist_pct = sl_dist_pct * (tp_mult / stop_mult)
                take_profit = entry_price * (1 - tp_dist_pct)
                log.info(
                    f"[{self.name}] {symbol} 插针尖端止损: tip={wick_tip_price:.8g}, "
                    f"sl={stop_loss:.8g}({sl_dist_pct:.4f}), tp={take_profit:.8g}"
                )
                return stop_loss, take_profit, "wick_tip"
            # wick_tip_price 方向不匹配，回退到 ATR/固定
            log.debug(
                f"[{self.name}] {symbol} wick_tip_price={wick_tip_price} "
                f"与方向 {direction.value} 不匹配，回退到 ATR"
            )

        # 次优路径：ATR 动态止损
        if atr_pct is not None and atr_pct > 0:
            atr_ratio = float(atr_pct) / 100.0
            stop_mult, tp_mult = self._get_dynamic_multipliers(float(atr_pct))
            raw_sl_pct = atr_ratio * stop_mult
            sl_pct = max(self._min_stop_pct, min(self._max_stop_pct, raw_sl_pct))
            tp_pct = sl_pct * (tp_mult / stop_mult)
            source = "atr"
            log.debug(
                f"[{self.name}] {symbol} ATR 止损: atr_pct={atr_pct:.2f}%, "
                f"mult={stop_mult:.1f}, sl={sl_pct:.4f}, tp={tp_pct:.4f}"
            )
        else:
            sl_pct = STOP_LOSS_PCT
            tp_pct = TAKE_PROFIT_PCT
            source = "fixed"
            log.warning(
                f"[{self.name}] {symbol} 缺少 ATR 信息，止损回退到固定 "
                f"{STOP_LOSS_PCT * 100:.1f}%/{TAKE_PROFIT_PCT * 100:.1f}% — "
                f"建议上游透传 atr_pct 以启用波动率自适应止损"
            )

        if direction == TradeDirection.LONG:
            stop_loss = entry_price * (1 - sl_pct)
            take_profit = entry_price * (1 + tp_pct)
        else:
            stop_loss = entry_price * (1 + sl_pct)
            take_profit = entry_price * (1 - tp_pct)

        return stop_loss, take_profit, source

    def _should_skip_for_excessive_volatility(
        self,
        atr_pct: Optional[float],
        symbol: str,
        direction: Optional[TradeDirection] = None,
    ) -> bool:
        """
        ATR 原始止损距离超过系统上限时跳过交易。

        这类币种即使把止损硬截到 max_stop_pct，也很容易被正常波动扫损；
        与其放大单笔尾部风险，不如本轮不生成交易计划。

        注意：SHORT 策略使用更严格的 3% 阈值（DEFAULT_MAX_STOP_USDT），
        LONG 策略使用 7% 阈值（_max_stop_pct）。
        这是因为做空时向下波动通常更剧烈，需要更紧的止损保护。

        P0-1 改造：做空增加硬顶止损距离（entry_price × 3%），防止趋势反转时亏损无限。
        """
        if atr_pct is None or atr_pct <= 0:
            return False

        stop_mult, _ = self._get_dynamic_multipliers(float(atr_pct))
        raw_sl_pct = (float(atr_pct) / 100.0) * stop_mult
        if direction == TradeDirection.SHORT and raw_sl_pct > DEFAULT_MAX_STOP_USDT:
            log.warning(
                f"[{self.name}] {symbol} 做空 ATR 波动过大，跳过交易: "
                f"atr_pct={atr_pct:.2f}%, raw_sl={raw_sl_pct:.2%}, "
                f"做空硬顶={DEFAULT_MAX_STOP_USDT:.2%}"
            )
            return True
        if raw_sl_pct <= self._max_stop_pct:
            return False

        log.warning(
            f"[{self.name}] {symbol} ATR 波动过大，跳过交易: "
            f"atr_pct={atr_pct:.2f}%, raw_sl={raw_sl_pct:.2%}, "
            f"max_stop={self._max_stop_pct:.2%}"
        )
        return True

    def _get_dynamic_multipliers(self, atr_pct: float) -> tuple[float, float]:
        """对于高波动币种（ATR > 5%），使用配置的高波动止盈乘数。

        止损乘数不再放大——max_stop_pct (7%) 会自动 clip 过大的止损距离。
        """
        if atr_pct >= 5.0:
            return self._atr_stop_mult, max(self._atr_tp_mult, self._high_vol_tp_mult)
        return self._atr_stop_mult, self._atr_tp_mult

    def _normalize_quantity_for_exchange(
        self,
        symbol: str,
        quantity: float,
        price: float,
    ) -> Optional[float]:
        """
        按 Binance 交易规则规整 quantity。

        未注入 provider 时保持旧行为，便于测试和离线回测；生产入口会注入
        exchangeInfo 规则，保证真实下单数量满足 LOT_SIZE 与最小名义金额。
        """
        if self._trading_rule_provider is None:
            return quantity

        try:
            rule = self._trading_rule_provider(symbol)
        except Exception as exc:
            log.warning(f"[{self.name}] 获取 {symbol} 交易规则失败: {exc}")
            return None

        if rule is None:
            log.warning(f"[{self.name}] {symbol} 缺少交易规则，跳过")
            return None

        return normalize_order_quantity(
            symbol=symbol,
            quantity=quantity,
            price=price,
            rule=rule,
        )

    def _try_adjust_position(
        self,
        symbol: str,
        direction: TradeDirection,
        entry_price: float,
        current_quantity: float,
        current_pct: float,
        account: AccountState,
    ) -> Optional[Dict[str, Any]]:
        """
        尝试逐步裁剪头寸规模直到通过风控校验。

        每次裁剪 10%，最多尝试 10 次。

        参数:
            symbol: 币种符号
            direction: 交易方向
            entry_price: 入场价格
            current_quantity: 当前头寸数量
            current_pct: 当前头寸百分比
            account: 账户状态

        返回:
            裁剪后的 {"quantity": float, "pct": float}，或 None（无法裁剪至合规）
        """
        existing_value = self._risk_controller._get_position_value(symbol, account)
        if existing_value > 0:
            return None

        effective_lev = (
            self._short_leverage if direction == TradeDirection.SHORT else self._leverage
        )
        quantity = current_quantity
        pct = current_pct

        for _ in range(10):
            # 每次裁剪 10%
            quantity *= 0.9
            pct *= 0.9

            if pct <= 0.01:
                # 头寸太小，放弃
                return None

            order_request = OrderRequest(
                symbol=symbol,
                direction=direction,
                price=entry_price,
                quantity=quantity,
                leverage=effective_lev,
            )
            validation = self._risk_controller.validate_order(order_request, account)

            if validation.passed:
                return {"quantity": quantity, "pct": pct}

        return None
