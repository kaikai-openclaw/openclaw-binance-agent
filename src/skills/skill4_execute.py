"""
Skill-4：自动交易执行与风控

从 State_Store 读取 Skill-3 输出的交易计划，对每笔交易调用 Risk_Controller 校验，
通过 Binance_Fapi_Client 提交限价订单，并实现持仓监控（止损/止盈/超时平仓）、
日亏损检查与 Paper Mode 降级。

BinanceFapiClient 和 RiskController 通过构造函数注入，便于测试时 mock。
持仓监控轮询间隔可配置（默认 30 秒），测试时可设为 0。

需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.11, 4.12, 4.13
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    OrderRequest,
    OrderStatus,
    TradeDirection,
)
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# 默认轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 30

# 默认杠杆倍数
DEFAULT_LEVERAGE = 10

# 账户状态提供者类型
AccountStateProvider = Callable[[], AccountState]


class Skill4Execute(BaseSkill):
    """
    自动交易执行 Skill。

    从 State_Store 读取交易计划，对每笔交易执行风控校验，
    通过 Binance_Fapi_Client 提交限价订单，并轮询监控持仓状态。

    需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.11, 4.12, 4.13
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        binance_client: BinanceFapiClient,
        risk_controller: RiskController,
        account_state_provider: AccountStateProvider,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        leverage: int = DEFAULT_LEVERAGE,
    ) -> None:
        """
        初始化 Skill-4。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            binance_client: Binance 合约客户端（注入）
            risk_controller: 风控拦截层（注入）
            account_state_provider: 账户状态提供回调
            poll_interval: 持仓监控轮询间隔（秒），默认 30，测试时可设为 0
            leverage: 默认杠杆倍数
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill4_execute"
        self._binance_client = binance_client
        self._risk_controller = risk_controller
        self._account_state_provider = account_state_provider
        self._poll_interval = poll_interval
        self._leverage = leverage

    def run(self, input_data: dict) -> dict:
        """
        执行自动交易。

        流程:
        1. 从 State_Store 读取交易计划（通过 input_state_id）
        2. 检查日亏损，必要时执行降级
        3. 对每笔交易执行风控校验 → 下单 → 监控持仓
        4. 组装执行结果输出

        参数:
            input_data: 经 Schema 校验的输入，包含 input_state_id

        返回:
            符合 skill4_output.json Schema 的输出字典
        """
        input_state_id = input_data["input_state_id"]

        # 步骤 1：从 State_Store 读取交易计划
        upstream_data = self.state_store.load(input_state_id)
        trade_plans = upstream_data.get("trade_plans", [])

        log.info(
            f"[{self.name}] 读取到 {len(trade_plans)} 笔交易计划，"
            f"input_state_id={input_state_id}"
        )

        execution_results: list[dict] = []

        # 步骤 2：日亏损检查（需求 4.11）
        account = self._account_state_provider()
        if self._risk_controller.check_daily_loss(account):
            log.warning(f"[{self.name}] 日亏损触及阈值，执行降级")
            self._risk_controller.execute_degradation(
                account, binance_client=self._binance_client
            )

        # 步骤 3：逐笔执行交易计划
        for plan in trade_plans:
            result = self._execute_single_trade(plan)
            execution_results.append(result)

        is_paper = self._risk_controller.is_paper_mode()

        output = {
            "state_id": str(uuid.uuid4()),
            "execution_results": execution_results,
            "is_paper_mode": is_paper,
        }

        log.info(
            f"[{self.name}] 执行完成: "
            f"总计={len(execution_results)}, "
            f"paper_mode={is_paper}"
        )

        return output

    def _execute_single_trade(self, plan: dict) -> dict:
        """
        执行单笔交易计划：风控校验 → 下单 → 持仓监控。

        参数:
            plan: 交易计划字典（来自 Skill-3 输出）

        返回:
            执行结果字典（符合 skill4_output.json 中 execution_results 项的 Schema）
        """
        symbol = plan.get("symbol", "")
        direction_str = plan.get("direction", "long")
        direction = TradeDirection(direction_str)
        entry_price = (
            plan.get("entry_price_upper", 0) + plan.get("entry_price_lower", 0)
        ) / 2
        position_size_pct = plan.get("position_size_pct", 0)
        stop_loss_price = plan.get("stop_loss_price", 0)
        take_profit_price = plan.get("take_profit_price", 0)
        max_hold_hours = plan.get("max_hold_hours", 24)

        now_str = datetime.now(timezone.utc).isoformat()

        # 获取账户状态
        account = self._account_state_provider()

        # 计算下单数量
        quantity = (account.total_balance * position_size_pct / 100) / entry_price
        if quantity <= 0:
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.EXECUTION_FAILED.value,
                executed_at=now_str,
                reason="数量计算为零",
            )

        # 需求 4.2：风控校验
        order_request = OrderRequest(
            symbol=symbol,
            direction=direction,
            price=entry_price,
            quantity=quantity,
            leverage=self._leverage,
        )
        validation = self._risk_controller.validate_order(order_request, account)

        if not validation.passed:
            log.warning(
                f"[{self.name}] {symbol} 风控拒绝: {validation.reason}"
            )
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.REJECTED_BY_RISK.value,
                executed_at=now_str,
                reason=validation.reason,
            )

        # 需求 4.12：Paper Mode 下不提交真实订单
        if self._risk_controller.is_paper_mode():
            log.info(f"[{self.name}] {symbol} Paper Mode，模拟下单")
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.PAPER_TRADE.value,
                executed_at=now_str,
                executed_price=entry_price,
                executed_quantity=quantity,
                fee=0.0,
                order_id=f"paper_{uuid.uuid4().hex[:12]}",
            )

        # 需求 4.3：提交限价订单
        side = "BUY" if direction == TradeDirection.LONG else "SELL"
        try:
            order_result = self._binance_client.place_limit_order(
                symbol=symbol,
                side=side,
                price=entry_price,
                quantity=quantity,
            )
        except Exception as exc:
            log.error(f"[{self.name}] {symbol} 下单失败: {exc}")
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.EXECUTION_FAILED.value,
                executed_at=now_str,
                reason=str(exc),
            )

        # 需求 4.4：轮询持仓监控
        close_result = self._monitor_position(
            symbol=symbol,
            direction=direction,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            max_hold_hours=max_hold_hours,
            quantity=quantity,
            order_id=order_result.order_id,
        )

        executed_at = datetime.now(timezone.utc).isoformat()

        return self._make_result(
            symbol=symbol,
            direction=direction_str,
            status=close_result.get("status", OrderStatus.EXECUTION_FAILED.value),
            executed_at=executed_at,
            executed_price=close_result.get("close_price", 0.0),
            executed_quantity=(
                order_result.quantity
                if close_result.get("status") == OrderStatus.FILLED.value
                else 0.0
            ),
            fee=close_result.get("fee", 0.0),
            order_id=order_result.order_id,
            reason=close_result.get("reason", ""),
            entry_price=entry_price,
            exit_price=close_result.get("close_price", 0.0),
            pnl_amount=self._calculate_pnl_amount(
                direction=direction,
                entry_price=entry_price,
                exit_price=close_result.get("close_price", 0.0),
                quantity=order_result.quantity,
                status=close_result.get("status", ""),
            ),
            hold_duration_hours=close_result.get("hold_duration_hours", 0.0),
            position_size_pct=position_size_pct,
        )

    def _monitor_position(
        self,
        symbol: str,
        direction: TradeDirection,
        stop_loss_price: float,
        take_profit_price: float,
        max_hold_hours: float,
        quantity: float,
        order_id: str,
    ) -> dict:
        """
        轮询监控持仓，检查止损/止盈/超时平仓条件。

        需求 4.4: 每 poll_interval 秒轮询一次
        需求 4.5: 未实现亏损触及止损 → 市价平仓
        需求 4.6: 未实现盈利触及止盈 → 市价平仓
        需求 4.7: 持仓超时 → 市价平仓

        参数:
            symbol: 交易对符号
            direction: 交易方向
            stop_loss_price: 止损价格
            take_profit_price: 止盈价格
            max_hold_hours: 持仓时间上限（小时）
            quantity: 持仓数量

        返回:
            {
                "status": str,
                "close_price": float,
                "fee": float,
                "reason": str,
                "hold_duration_hours": float,
            }
        """
        start_time = time.monotonic()
        max_hold_seconds = max_hold_hours * 3600
        close_side = "SELL" if direction == TradeDirection.LONG else "BUY"
        consecutive_errors = 0
        max_consecutive_errors = 10  # 连续错误上限，防止无限循环
        position_opened = False

        while True:
            # 轮询间隔
            if self._poll_interval > 0:
                time.sleep(self._poll_interval)

            # 获取持仓风险信息
            try:
                pos_risk = self._binance_client.get_position_risk(symbol)
                consecutive_errors = 0  # 成功后重置错误计数
            except Exception as exc:
                consecutive_errors += 1
                log.warning(
                    f"[{self.name}] {symbol} 获取持仓信息失败 "
                    f"({consecutive_errors}/{max_consecutive_errors}): {exc}"
                )
                # 连续错误达到上限，强制超时平仓
                if consecutive_errors >= max_consecutive_errors:
                    log.error(
                        f"[{self.name}] {symbol} 连续 {max_consecutive_errors} 次"
                        f"获取持仓失败，强制平仓"
                    )
                    if not position_opened:
                        self._cancel_entry_order(symbol)
                        return self._make_monitor_result(
                            status=OrderStatus.EXECUTION_FAILED.value,
                            close_price=0.0,
                            fee=0.0,
                            reason="entry_order_unconfirmed",
                            start_time=start_time,
                        )
                    return self._close_position(
                        symbol, close_side, quantity, "monitor_error", start_time
                    )
                # 检查是否超时
                elapsed = time.monotonic() - start_time
                if elapsed >= max_hold_seconds:
                    if not position_opened:
                        self._cancel_entry_order(symbol)
                        return self._make_monitor_result(
                            status=OrderStatus.EXECUTION_FAILED.value,
                            close_price=0.0,
                            fee=0.0,
                            reason="entry_not_filled_timeout",
                            start_time=start_time,
                        )
                    return self._close_position(
                        symbol, close_side, quantity, "timeout", start_time
                    )
                continue

            current_price = pos_risk.mark_price
            position_amt = abs(pos_risk.position_amt)
            if position_amt > 0:
                position_opened = True

            # 持仓为 0：要区分“未成交”与“已开仓后被平”
            if position_amt == 0:
                if position_opened:
                    log.info(f"[{self.name}] {symbol} 持仓已清零")
                    return self._make_monitor_result(
                        status=OrderStatus.FILLED.value,
                        close_price=current_price,
                        fee=0.0,
                        reason="external_close",
                        start_time=start_time,
                    )

                # 尚未观测到持仓，若入场单仍在挂单则继续等待
                if self._is_order_open(symbol, order_id):
                    elapsed = time.monotonic() - start_time
                    if elapsed >= max_hold_seconds:
                        self._cancel_entry_order(symbol)
                        return self._make_monitor_result(
                            status=OrderStatus.EXECUTION_FAILED.value,
                            close_price=0.0,
                            fee=0.0,
                            reason="entry_not_filled_timeout",
                            start_time=start_time,
                        )
                    continue

                # 入场单已不在挂单且从未持仓，视为入场失败（被撤单/拒单/失效）
                return self._make_monitor_result(
                    status=OrderStatus.EXECUTION_FAILED.value,
                    close_price=0.0,
                    fee=0.0,
                    reason="entry_order_not_open_no_position",
                    start_time=start_time,
                )

            # 需求 4.5：止损检查
            if self._should_stop_loss(direction, current_price, stop_loss_price):
                log.warning(
                    f"[{self.name}] {symbol} 触发止损: "
                    f"当前价={current_price}, 止损价={stop_loss_price}"
                )
                # 记录止损事件，启动冷却期
                self._risk_controller.record_stop_loss(
                    symbol, direction.value
                )
                return self._close_position(
                    symbol, close_side, position_amt, "stop_loss", start_time
                )

            # 需求 4.6：止盈检查
            if self._should_take_profit(direction, current_price, take_profit_price):
                log.info(
                    f"[{self.name}] {symbol} 触发止盈: "
                    f"当前价={current_price}, 止盈价={take_profit_price}"
                )
                return self._close_position(
                    symbol, close_side, position_amt, "take_profit", start_time
                )

            # 需求 4.7：超时检查
            elapsed = time.monotonic() - start_time
            if elapsed >= max_hold_seconds:
                log.info(
                    f"[{self.name}] {symbol} 持仓超时: "
                    f"已持有 {elapsed / 3600:.2f} 小时"
                )
                if not position_opened:
                    self._cancel_entry_order(symbol)
                    return self._make_monitor_result(
                        status=OrderStatus.EXECUTION_FAILED.value,
                        close_price=0.0,
                        fee=0.0,
                        reason="entry_not_filled_timeout",
                        start_time=start_time,
                    )
                return self._close_position(
                    symbol, close_side, position_amt, "timeout", start_time
                )

            # 日亏损检查（需求 4.11）
            account = self._account_state_provider()
            if self._risk_controller.check_daily_loss(account):
                log.warning(
                    f"[{self.name}] 监控中日亏损触及阈值，执行降级并平仓"
                )
                self._risk_controller.execute_degradation(
                    account, binance_client=self._binance_client
                )
                if not position_opened:
                    self._cancel_entry_order(symbol)
                    return self._make_monitor_result(
                        status=OrderStatus.EXECUTION_FAILED.value,
                        close_price=0.0,
                        fee=0.0,
                        reason="daily_loss_before_fill",
                        start_time=start_time,
                    )
                return self._close_position(
                    symbol,
                    close_side,
                    position_amt,
                    "daily_loss_degradation",
                    start_time,
                )

    def _make_monitor_result(
        self,
        status: str,
        close_price: float,
        fee: float,
        reason: str,
        start_time: float,
    ) -> dict:
        """统一构造监控结果并补充持仓时长。"""
        elapsed_hours = max(0.0, (time.monotonic() - start_time) / 3600.0)
        return {
            "status": status,
            "close_price": close_price,
            "fee": fee,
            "reason": reason,
            "hold_duration_hours": elapsed_hours,
        }

    @staticmethod
    def _calculate_pnl_amount(
        direction: TradeDirection,
        entry_price: float,
        exit_price: float,
        quantity: float,
        status: str,
    ) -> float:
        """计算交易盈亏金额。"""
        if status != OrderStatus.FILLED.value:
            return 0.0
        if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
            return 0.0
        if direction == TradeDirection.LONG:
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity

    def _should_stop_loss(
        self,
        direction: TradeDirection,
        current_price: float,
        stop_loss_price: float,
    ) -> bool:
        """
        判断是否触发止损。

        做多：当前价 <= 止损价 → 止损
        做空：当前价 >= 止损价 → 止损
        """
        if direction == TradeDirection.LONG:
            return current_price <= stop_loss_price
        else:
            return current_price >= stop_loss_price

    def _should_take_profit(
        self,
        direction: TradeDirection,
        current_price: float,
        take_profit_price: float,
    ) -> bool:
        """
        判断是否触发止盈。

        做多：当前价 >= 止盈价 → 止盈
        做空：当前价 <= 止盈价 → 止盈
        """
        if direction == TradeDirection.LONG:
            return current_price >= take_profit_price
        else:
            return current_price <= take_profit_price

    def _close_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reason: str,
        start_time: float,
    ) -> dict:
        """
        提交市价平仓订单。

        参数:
            symbol: 交易对符号
            side: 平仓方向（"BUY" 或 "SELL"）
            quantity: 平仓数量
            reason: 平仓原因

        返回:
            {
                "status": str,
                "close_price": float,
                "fee": float,
                "reason": str,
                "hold_duration_hours": float,
            }
        """
        try:
            result = self._binance_client.place_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
            )
            log.info(
                f"[{self.name}] {symbol} 平仓成功: "
                f"原因={reason}, 价格={result.price}"
            )
            return self._make_monitor_result(
                status=OrderStatus.FILLED.value,
                close_price=result.price,
                fee=0.0,
                reason=reason,
                start_time=start_time,
            )
        except Exception as exc:
            log.error(f"[{self.name}] {symbol} 平仓失败: {exc}")
            return self._make_monitor_result(
                status=OrderStatus.EXECUTION_FAILED.value,
                close_price=0.0,
                fee=0.0,
                reason=f"{reason}_failed",
                start_time=start_time,
            )

    def _is_order_open(self, symbol: str, order_id: str) -> bool:
        """查询入场限价单是否仍在挂单。"""
        try:
            orders = self._binance_client.get_open_orders(symbol)
            order_id_str = str(order_id)
            for order in orders:
                if str(order.get("orderId", "")) == order_id_str:
                    return True
            return False
        except Exception as exc:
            # 无法确认挂单状态时保守等待，避免误判失败
            log.warning(f"[{self.name}] {symbol} 查询挂单状态失败: {exc}")
            return True

    def _cancel_entry_order(self, symbol: str) -> None:
        """在入场未成交超时场景主动撤销挂单。"""
        try:
            self._binance_client.cancel_all_orders(symbol=symbol)
        except Exception as exc:
            log.warning(f"[{self.name}] {symbol} 撤销入场挂单失败: {exc}")

    @staticmethod
    def _make_result(
        symbol: str,
        direction: str,
        status: str,
        executed_at: str,
        executed_price: float = 0.0,
        executed_quantity: float = 0.0,
        fee: float = 0.0,
        order_id: str = "",
        reason: str = "",
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        pnl_amount: float = 0.0,
        hold_duration_hours: float = 0.0,
        position_size_pct: float = 0.0,
    ) -> dict:
        """
        构造单笔执行结果字典。

        参数:
            symbol: 交易对符号
            direction: 交易方向
            status: 订单状态
            executed_at: 成交时间戳
            executed_price: 成交价格
            executed_quantity: 成交数量
            fee: 手续费
            order_id: 订单 ID
            reason: 附加原因说明
            entry_price: 入场价格
            exit_price: 平仓价格
            pnl_amount: 盈亏金额
            hold_duration_hours: 持仓时长（小时）
            position_size_pct: 头寸规模百分比

        返回:
            符合 skill4_output.json 中 execution_results 项 Schema 的字典
        """
        result: dict = {
            "order_id": order_id or f"none_{uuid.uuid4().hex[:8]}",
            "symbol": symbol,
            "direction": direction,
            "status": status,
            "executed_at": executed_at,
        }
        # 仅在有值时添加可选字段（Schema 中非 required）
        if executed_price > 0:
            result["executed_price"] = executed_price
        if executed_quantity > 0:
            result["executed_quantity"] = executed_quantity
        if fee >= 0:
            result["fee"] = fee
        if reason:
            result["reason"] = reason
        if entry_price > 0:
            result["entry_price"] = entry_price
        if exit_price > 0:
            result["exit_price"] = exit_price
        result["pnl_amount"] = pnl_amount
        if hold_duration_hours > 0:
            result["hold_duration_hours"] = hold_duration_hours
        if position_size_pct > 0:
            result["position_size_pct"] = position_size_pct
        return result
