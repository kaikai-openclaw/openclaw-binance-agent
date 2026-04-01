"""
Skill-3：交易策略制定

基于 Skill-2 输出的评级结果，为每个目标币种生成量化交易计划。
使用固定风险模型计算头寸规模，执行风控预校验并自动裁剪超限头寸。

RiskController 和 account_state_provider 通过构造函数注入，便于测试时 mock。

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

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

# 止损止盈比例常量
STOP_LOSS_PCT = 0.03       # 止损幅度 3%
TAKE_PROFIT_PCT = 0.06     # 止盈幅度 6%（盈亏比 2:1）

# 入场区间宽度常量
ENTRY_SPREAD_MIN = 0.01    # 最窄区间（置信度 100% 时）
ENTRY_SPREAD_MAX = 0.05    # 最宽区间（置信度 0% 时）

# 市场价格提供者类型：接收 symbol，返回当前市场价格
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
    ) -> None:
        """
        初始化 Skill-3。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            risk_controller: 风控拦截层实例
            account_state_provider: 账户状态提供回调（返回 AccountState）
            market_price_provider: 市场价格提供回调（接收 symbol，返回当前价格），
                                   为 None 时使用 100.0 作为标准化基准价格
            risk_ratio: 账户风险比例，默认 0.02（2%）
            max_hold_hours: 默认持仓时间上限（小时），默认 24
            leverage: 默认杠杆倍数，默认 10
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill3_strategy"
        self._risk_controller = risk_controller
        self._account_state_provider = account_state_provider
        self._market_price_provider = market_price_provider
        self._risk_ratio = risk_ratio
        self._max_hold_hours = max_hold_hours
        self._leverage = leverage

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

        # hold 信号不生成交易计划
        if signal == "hold":
            log.info(f"[{self.name}] {symbol} 信号为 hold，跳过")
            return None

        # 确定交易方向
        direction = TradeDirection.LONG if signal == "long" else TradeDirection.SHORT

        # 计算入场价格区间（优先使用真实市场价格）
        entry_price, entry_upper, entry_lower = self._calculate_entry_range(
            confidence, symbol
        )

        # 计算止损和止盈价格
        stop_loss_price, take_profit_price = self._calculate_sl_tp(
            entry_price, direction
        )

        # 需求 3.8：数值参数边界校验
        if entry_price <= 0 or stop_loss_price <= 0 or take_profit_price <= 0:
            log.warning(
                f"[{self.name}] {symbol} 价格参数无效，跳过"
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

        return {
            "symbol": symbol,
            "direction": direction.value,
            "entry_price_upper": round(entry_upper, 8),
            "entry_price_lower": round(entry_lower, 8),
            "position_size_pct": round(position_size_pct, 4),
            "stop_loss_price": round(stop_loss_price, 8),
            "take_profit_price": round(take_profit_price, 8),
            "max_hold_hours": self._max_hold_hours,
        }

    def _calculate_entry_range(
        self, confidence: float, symbol: str = ""
    ) -> tuple[float, float, float]:
        """
        计算入场价格区间。

        优先从 market_price_provider 获取真实市场价格，
        若不可用则回退到 100.0 标准化基准价格。
        区间宽度与置信度成反比：置信度越高，区间越窄。

        参数:
            confidence: 置信度百分比（0-100）
            symbol: 币种符号（用于获取市场价格）

        返回:
            (基准价格, 区间上限, 区间下限)
        """
        # 优先从市场数据获取真实价格
        base_price = None
        if self._market_price_provider is not None and symbol:
            try:
                base_price = self._market_price_provider(symbol)
            except Exception as exc:
                log.warning(
                    f"[{self.name}] 获取 {symbol} 市场价格失败: {exc}，"
                    f"回退到标准化基准价格"
                )

        if base_price is None or base_price <= 0:
            base_price = 100.0  # 标准化基准价格（回退值）

        # 区间宽度：置信度 100% → ENTRY_SPREAD_MIN，置信度 0% → ENTRY_SPREAD_MAX
        spread_pct = ENTRY_SPREAD_MAX - (confidence / 100.0) * (ENTRY_SPREAD_MAX - ENTRY_SPREAD_MIN)
        spread = base_price * spread_pct

        upper = base_price + spread
        lower = base_price - spread

        return base_price, upper, lower

    def _calculate_sl_tp(
        self, entry_price: float, direction: TradeDirection
    ) -> tuple[float, float]:
        """
        计算止损和止盈价格。

        做多：止损 = 入场价 × (1 - STOP_LOSS_PCT)，止盈 = 入场价 × (1 + TAKE_PROFIT_PCT)
        做空：止损 = 入场价 × (1 + STOP_LOSS_PCT)，止盈 = 入场价 × (1 - TAKE_PROFIT_PCT)

        盈亏比 = TAKE_PROFIT_PCT / STOP_LOSS_PCT = 2:1。

        参数:
            entry_price: 入场价格
            direction: 交易方向

        返回:
            (止损价格, 止盈价格)
        """
        if direction == TradeDirection.LONG:
            stop_loss = entry_price * (1 - STOP_LOSS_PCT)
            take_profit = entry_price * (1 + TAKE_PROFIT_PCT)
        else:
            stop_loss = entry_price * (1 + STOP_LOSS_PCT)
            take_profit = entry_price * (1 - TAKE_PROFIT_PCT)

        return stop_loss, take_profit

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
