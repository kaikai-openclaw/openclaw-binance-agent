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
DEFAULT_MAX_HOLD_HOURS = 24.0

# 默认杠杆倍数
DEFAULT_LEVERAGE = 10

# 止损止盈比例常量（仅在没有 ATR 时的回退路径使用）
STOP_LOSS_PCT = 0.03       # 止损幅度 3%
TAKE_PROFIT_PCT = 0.06     # 止盈幅度 6%（盈亏比 2:1）

# ATR 动态止损默认参数
DEFAULT_ATR_STOP_MULT = 1.5     # 止损距离 = ATR × 1.5
DEFAULT_ATR_TP_MULT = 3.0       # 止盈距离 = ATR × 3.0（盈亏比 2:1）
DEFAULT_MIN_STOP_PCT = 0.005    # 止损距离下限 0.5%（防止极低波动下 SL 贴得过近被秒扫）
DEFAULT_MAX_STOP_PCT = 0.08     # 止损距离上限 8%（防止高波动币种仓位失控）

# 入场区间宽度常量
ENTRY_SPREAD_MIN = 0.01    # 最窄区间（置信度 100% 时）
ENTRY_SPREAD_MAX = 0.05    # 最宽区间（置信度 0% 时）

# 测试路径的标准化基准价（仅在未注入任何 provider 时使用）
TEST_FALLBACK_PRICE = 100.0

# P0-4：扣费后净盈亏比的最低阈值；低于此值的交易直接拒绝
DEFAULT_MIN_NET_RR_RATIO = 1.2

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
        require_market_price: bool = False,
        atr_stop_mult: float = DEFAULT_ATR_STOP_MULT,
        atr_tp_mult: float = DEFAULT_ATR_TP_MULT,
        min_stop_pct: float = DEFAULT_MIN_STOP_PCT,
        max_stop_pct: float = DEFAULT_MAX_STOP_PCT,
        fee_market: str = "crypto",
        fee_order_type: str = "taker",
        fee_vip_discount: float = 0.0,
        min_net_rr_ratio: float = DEFAULT_MIN_NET_RR_RATIO,
        trading_rule_provider: Optional[TradingRuleProvider] = None,
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
            leverage: 默认杠杆倍数，默认 10
            require_market_price: 是否要求必须拿到真实市场价（P0-2）
                - True（生产路径）：provider 返回无效值时跳过该币种
                - False（测试路径，默认）：回退到 TEST_FALLBACK_PRICE=100.0
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
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill3_strategy"
        self._risk_controller = risk_controller
        self._account_state_provider = account_state_provider
        self._market_price_provider = market_price_provider
        self._risk_ratio = risk_ratio
        self._max_hold_hours = max_hold_hours
        self._leverage = leverage
        self._require_market_price = require_market_price
        self._atr_stop_mult = atr_stop_mult
        self._atr_tp_mult = atr_tp_mult
        self._min_stop_pct = min_stop_pct
        self._max_stop_pct = max_stop_pct
        self._fee_market = fee_market
        self._fee_order_type = fee_order_type
        self._fee_vip_discount = fee_vip_discount
        self._min_net_rr_ratio = min_net_rr_ratio
        self._trading_rule_provider = trading_rule_provider

        # 预计算 round-trip 成本占比（每次下单一致，无需每笔重算）
        try:
            self._round_trip_cost_pct = calc_round_trip_cost_pct(
                market=fee_market,
                order_type=fee_order_type,
                vip_discount=fee_vip_discount,
            )
        except ValueError:
            log.warning(
                f"[{self.name}] fee_market={fee_market} 未知，禁用费用建模"
            )
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
                "pipeline_status": PipelineStatus.NO_OPPORTUNITY.value,
            }

        # 步骤 3：获取当前账户状态
        account = self._account_state_provider()

        # 步骤 4 & 5：为每个目标币种生成交易计划
        trade_plans: List[Dict[str, Any]] = []
        for rating in ratings:
            plan = self._generate_trade_plan(rating, account)
            if plan is not None:
                trade_plans.append(plan)

        # 判断 pipeline_status
        if trade_plans:
            pipeline_status = PipelineStatus.HAS_TRADES.value
        else:
            pipeline_status = PipelineStatus.NO_OPPORTUNITY.value
            log.info(f"[{self.name}] 所有交易计划均未通过风控预校验")

        output = {
            "state_id": str(uuid.uuid4()),
            "trade_plans": trade_plans,
            "pipeline_status": pipeline_status,
        }

        log.info(
            f"[{self.name}] 策略制定完成: "
            f"输入={len(ratings)}, 输出={len(trade_plans)}, "
            f"状态={pipeline_status}"
        )

        return output

    def _generate_trade_plan(
        self, rating: Dict[str, Any], account: AccountState
    ) -> Optional[Dict[str, Any]]:
        """
        为单个目标币种生成交易计划。

        包含：方向推导、入场区间计算、头寸规模计算、止损止盈设定、
        风控预校验与超限裁剪。

        参数:
            rating: 评级结果字典（symbol, rating_score, signal, confidence）
            account: 当前账户状态

        返回:
            交易计划字典，或 None（信号为 hold 或风控拒绝时）
        """
        symbol = rating.get("symbol", "")
        signal = rating.get("signal", "")
        confidence = rating.get("confidence", 0.0)
        atr_pct = rating.get("atr_pct")  # Skill-1/Skill-2 透传的 ATR 百分比（可选）

        # hold 信号不生成交易计划
        if signal == "hold":
            log.info(f"[{self.name}] {symbol} 信号为 hold，跳过")
            return None

        # 确定交易方向
        direction = TradeDirection.LONG if signal == "long" else TradeDirection.SHORT

        # 计算入场价格区间（P0-2：生产路径 fail-fast）
        entry_range = self._calculate_entry_range(confidence, symbol)
        if entry_range is None:
            log.warning(
                f"[{self.name}] {symbol} 市场价格不可用（require_market_price=True），跳过"
            )
            return None
        entry_price, entry_upper, entry_lower, price_source = entry_range

        if self._should_skip_for_excessive_volatility(atr_pct, symbol):
            return None

        # 计算止损和止盈价格（P0-1：优先使用 ATR 动态）
        stop_loss_price, take_profit_price, sl_source = self._calculate_sl_tp(
            entry_price, direction, atr_pct, symbol
        )

        # 需求 3.8：数值参数边界校验
        if entry_price <= 0 or stop_loss_price <= 0 or take_profit_price <= 0:
            log.warning(
                f"[{self.name}] {symbol} 价格参数无效，跳过"
            )
            return None

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
            return None

        # 需求 3.2：使用固定风险模型计算头寸规模
        try:
            position_size = calculate_position_size(
                account_balance=account.total_balance,
                risk_ratio=self._risk_ratio,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
            )
        except ValueError as e:
            log.warning(
                f"[{self.name}] {symbol} 头寸规模计算失败: {e}"
            )
            return None

        # 转换为头寸规模百分比
        position_value = position_size * entry_price
        position_size_pct = (position_value / account.total_balance) * 100

        # 需求 3.4 & 3.5：风控预校验 — 单笔保证金不超过 20%
        if position_size_pct > 20.0:
            log.info(
                f"[{self.name}] {symbol} 头寸规模 {position_size_pct:.2f}% "
                f"超过 20% 上限，裁剪至 20%"
            )
            position_size_pct = 20.0
            position_size = (account.total_balance * 0.20) / entry_price

        normalized_position_size = self._normalize_quantity_for_exchange(
            symbol=symbol,
            quantity=position_size,
            price=entry_price,
        )
        if normalized_position_size is None:
            log.warning(
                f"[{self.name}] {symbol} 头寸数量不满足交易所 LOT_SIZE/minNotional，跳过"
            )
            return None
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
            leverage=self._leverage,
        )
        validation = self._risk_controller.validate_order(order_request, account)

        if not validation.passed:
            # 需求 3.5：尝试裁剪头寸至合规范围
            adjusted_plan = self._try_adjust_position(
                symbol, direction, entry_price, position_size,
                position_size_pct, account
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
                    return None
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
                return None

        # 需求 3.8：最终边界校验
        if position_size_pct <= 0:
            log.warning(f"[{self.name}] {symbol} 头寸规模为零，跳过")
            return None

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
        return plan

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
                log.warning(
                    f"[{self.name}] 获取 {symbol} 市场价格失败: {exc}"
                )

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

        spread_pct = ENTRY_SPREAD_MAX - (confidence / 100.0) * (ENTRY_SPREAD_MAX - ENTRY_SPREAD_MIN)
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
    ) -> tuple[float, float, str]:
        """
        计算止损和止盈价格（P0-1：ATR 动态 > 固定百分比）。

        优先路径（ATR 动态）：
            - 止损距离 pct = clip(atr_pct / 100 × atr_stop_mult, min_stop_pct, max_stop_pct)
            - 止盈距离 pct = 止损距离 × (atr_tp_mult / atr_stop_mult)
            - 盈亏比 = atr_tp_mult / atr_stop_mult（默认 2:1）

        回退路径（无 atr_pct）：
            - 沿用 STOP_LOSS_PCT=3% / TAKE_PROFIT_PCT=6%
            - 打 warning 提示 ATR 缺失

        做多：止损 = 入场价 × (1 - sl_pct)，止盈 = 入场价 × (1 + tp_pct)
        做空：止损 = 入场价 × (1 + sl_pct)，止盈 = 入场价 × (1 - tp_pct)

        返回:
            (止损价格, 止盈价格, 来源标记) 来源 ∈ {"atr", "fixed"}
        """
        if atr_pct is not None and atr_pct > 0:
            atr_ratio = float(atr_pct) / 100.0
            raw_sl_pct = atr_ratio * self._atr_stop_mult
            sl_pct = max(self._min_stop_pct, min(self._max_stop_pct, raw_sl_pct))
            tp_pct = sl_pct * (self._atr_tp_mult / self._atr_stop_mult)
            source = "atr"
            log.debug(
                f"[{self.name}] {symbol} ATR 止损: atr_pct={atr_pct:.2f}%, "
                f"sl={sl_pct:.4f}, tp={tp_pct:.4f}"
            )
        else:
            sl_pct = STOP_LOSS_PCT
            tp_pct = TAKE_PROFIT_PCT
            source = "fixed"
            log.warning(
                f"[{self.name}] {symbol} 缺少 ATR 信息，止损回退到固定 "
                f"{STOP_LOSS_PCT*100:.1f}%/{TAKE_PROFIT_PCT*100:.1f}% — "
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
    ) -> bool:
        """
        ATR 原始止损距离超过系统上限时跳过交易。

        这类币种即使把止损硬截到 max_stop_pct，也很容易被正常波动扫损；
        与其放大单笔尾部风险，不如本轮不生成交易计划。
        """
        if atr_pct is None or atr_pct <= 0:
            return False

        raw_sl_pct = (float(atr_pct) / 100.0) * self._atr_stop_mult
        if raw_sl_pct <= self._max_stop_pct:
            return False

        log.warning(
            f"[{self.name}] {symbol} ATR 波动过大，跳过交易: "
            f"atr_pct={atr_pct:.2f}%, raw_sl={raw_sl_pct:.2%}, "
            f"max_stop={self._max_stop_pct:.2%}"
        )
        return True

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
                leverage=self._leverage,
            )
            validation = self._risk_controller.validate_order(
                order_request, account
            )

            if validation.passed:
                return {"quantity": quantity, "pct": pct}

        return None
