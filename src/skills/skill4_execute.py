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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, List, Optional

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.exchange_rules import (
    TradingRuleProvider,
    normalize_order_quantity,
    normalize_order_price,
)
from src.infra.fees import calc_crypto_fee
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

# 多品种并发执行最大线程数（monitor_until_close=True 时生效）
DEFAULT_MAX_CONCURRENT_TRADES = 8

# 已有持仓缺少策略计划时的保护性止损/止盈（与 Skill-3 固定回退保持一致）
EXISTING_POSITION_STOP_LOSS_PCT = 0.03
EXISTING_POSITION_TAKE_PROFIT_PCT = 0.06

# 已有保护单触发价允许的相对误差，超过则撤单重挂
PROTECTION_PRICE_TOLERANCE_PCT = 0.001

# 开仓后短暂确认成交；超时未成交则撤单，避免后续裸仓成交
DEFAULT_ENTRY_CONFIRM_TIMEOUT = 15.0
ENTRY_CONFIRM_POLL_INTERVAL = 2.0

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
        trading_rule_provider: Optional[TradingRuleProvider] = None,
        monitor_until_close: bool = False,
        entry_confirm_timeout: float = DEFAULT_ENTRY_CONFIRM_TIMEOUT,
        max_concurrent_trades: int = DEFAULT_MAX_CONCURRENT_TRADES,
        fee_order_type: str = "taker",
        fee_vip_discount: float = 0.0,
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
            trading_rule_provider: Binance 交易规则提供者；提供时执行前按 LOT_SIZE
                兜底规整 quantity，并按 PRICE_FILTER.tickSize 规整触发价
            monitor_until_close: 是否在本轮阻塞监控到平仓；定时任务默认 False，
                由下一轮任务继续管理已有持仓
            entry_confirm_timeout: 非阻塞模式下等待入场成交的最长秒数
            max_concurrent_trades: monitor_until_close=True 时的最大并发线程数，
                默认 8；非阻塞模式单次调用极短，并发收益有限，保持默认即可
            fee_order_type: 平仓订单类型，"taker"（市价单，默认）或 "maker"（限价单），
                用于计算平仓手续费
            fee_vip_discount: Binance VIP 费率折扣系数（0-1）
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill4_execute"
        self._binance_client = binance_client
        self._risk_controller = risk_controller
        self._account_state_provider = account_state_provider
        self._poll_interval = poll_interval
        self._leverage = leverage
        self._trading_rule_provider = trading_rule_provider
        self._monitor_until_close = monitor_until_close
        self._entry_confirm_timeout = entry_confirm_timeout
        self._max_concurrent_trades = max_concurrent_trades
        self._fee_order_type = fee_order_type
        self._fee_vip_discount = fee_vip_discount

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

        execution_results: List[dict] = []

        # 步骤 2：日亏损检查（需求 4.11）
        account = self._account_state_provider()
        if self._risk_controller.check_daily_loss(account):
            log.warning(f"[{self.name}] 日亏损触及阈值，执行降级")
            self._risk_controller.execute_degradation(
                account, binance_client=self._binance_client
            )

        # 清理服务端触发后遗留的孤儿条件单，避免下一次触发反向开仓。
        self._cleanup_orphan_algo_orders(account)

        # 对已经存在的实盘持仓补齐交易所侧保护，避免无 SL/TP 裸奔。
        self._protect_existing_positions(account)

        # 步骤 3：并发执行交易计划
        # monitor_until_close=True 时每笔交易可能阻塞数小时，必须并发；
        # monitor_until_close=False 时单次调用极短，并发收益有限但无害。
        execution_results = self._execute_plans_concurrent(trade_plans)

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

    def _execute_plans_concurrent(self, trade_plans: List[dict]) -> List[dict]:
        """
        并发执行所有交易计划，结果按原始 plan 顺序返回。

        使用 ThreadPoolExecutor 同时监控多个品种，解决
        monitor_until_close=True 时串行阻塞的问题。
        单个品种抛出未捕获异常时，记录错误并返回 EXECUTION_FAILED 结果，
        不影响其他品种的执行。
        """
        if not trade_plans:
            return []

        # 单品种时无需创建线程池，直接串行
        if len(trade_plans) == 1:
            return [self._execute_single_trade(trade_plans[0])]

        results: List[dict] = [None] * len(trade_plans)  # type: ignore[list-item]
        workers = min(self._max_concurrent_trades, len(trade_plans))

        log.info(
            f"[{self.name}] 并发执行 {len(trade_plans)} 笔计划，"
            f"线程数={workers}"
        )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skill4") as pool:
            # 以 index 为 key 保留原始顺序
            future_to_idx = {
                pool.submit(self._execute_single_trade, plan): idx
                for idx, plan in enumerate(trade_plans)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                plan = trade_plans[idx]
                symbol = plan.get("symbol", f"plan[{idx}]")
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    log.error(
                        f"[{self.name}] {symbol} 并发执行异常（已隔离）: {exc}",
                        exc_info=True,
                    )
                    results[idx] = self._make_result(
                        symbol=symbol,
                        direction=plan.get("direction", "unknown"),
                        status=OrderStatus.EXECUTION_FAILED.value,
                        executed_at=datetime.now(timezone.utc).isoformat(),
                        reason=f"concurrent_exception: {exc}",
                        strategy_tag=plan.get("strategy_tag", "unknown"),
                    )

        return results

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
        normalized_entry_price = self._normalize_price_for_exchange(symbol, entry_price)
        if normalized_entry_price is not None:
            entry_price = normalized_entry_price
        position_size_pct = plan.get("position_size_pct", 0)
        stop_loss_price = plan.get("stop_loss_price", 0)
        take_profit_price = plan.get("take_profit_price", 0)
        max_hold_hours = plan.get("max_hold_hours", 24)
        strategy_tag = plan.get("strategy_tag", "unknown")
        trailing_stop = plan.get("trailing_stop") or {}

        now_str = datetime.now(timezone.utc).isoformat()

        # 获取账户状态
        account = self._account_state_provider()

        # 优先使用 Skill-3 已按交易所规则规整的 quantity；兼容旧状态则回退到百分比计算。
        quantity = plan.get("quantity") or (
            account.total_balance * position_size_pct / 100
        ) / entry_price
        if quantity <= 0:
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.EXECUTION_FAILED.value,
                executed_at=now_str,
                reason="数量计算为零",
                strategy_tag=strategy_tag,
            )

        normalized_quantity = self._normalize_quantity_for_exchange(
            symbol=symbol,
            quantity=quantity,
            price=entry_price,
        )
        if normalized_quantity is None:
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.REJECTED_BY_RISK.value,
                executed_at=now_str,
                reason="数量不满足 Binance LOT_SIZE 或最小名义金额要求",
                strategy_tag=strategy_tag,
            )
        if normalized_quantity != quantity:
            log.info(
                f"[{self.name}] {symbol} quantity 执行前规整: "
                f"{quantity:.12g} -> {normalized_quantity:.12g}"
            )
            quantity = normalized_quantity

        # 需求 4.2：风控校验
        order_request = OrderRequest(
            symbol=symbol,
            direction=direction,
            price=entry_price,
            quantity=quantity,
            leverage=self._leverage,
        )
        validation = self._risk_controller.validate_order(
            order_request, account, strategy_tag=strategy_tag,
        )

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
                strategy_tag=strategy_tag,
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
                strategy_tag=strategy_tag,
            )

        if not self._ensure_symbol_leverage(symbol):
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=OrderStatus.EXECUTION_FAILED.value,
                executed_at=now_str,
                reason=f"设置 {symbol} 杠杆为 {self._leverage}x 失败",
                strategy_tag=strategy_tag,
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
                strategy_tag=strategy_tag,
            )

        if not self._monitor_until_close:
            entry_result = self._confirm_entry_and_place_protection(
                symbol=symbol,
                direction=direction,
                close_side="SELL" if direction == TradeDirection.LONG else "BUY",
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                planned_quantity=quantity,
                order_id=order_result.order_id,
                trailing_stop=trailing_stop,
            )
            return self._make_result(
                symbol=symbol,
                direction=direction_str,
                status=entry_result.get("status", OrderStatus.EXECUTION_FAILED.value),
                executed_at=datetime.now(timezone.utc).isoformat(),
                executed_price=entry_result.get("entry_price", entry_price),
                executed_quantity=entry_result.get("quantity", 0.0),
                fee=0.0,
                order_id=order_result.order_id,
                reason=entry_result.get("reason", ""),
                entry_price=entry_result.get("entry_price", entry_price),
                position_size_pct=position_size_pct,
                strategy_tag=strategy_tag,
            )

        # 可选旧行为：阻塞轮询直到平仓
        close_result = self._monitor_position(
            symbol=symbol,
            direction=direction,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            max_hold_hours=max_hold_hours,
            quantity=quantity,
            order_id=order_result.order_id,
            trailing_stop=trailing_stop,
            strategy_tag=strategy_tag,
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
            strategy_tag=strategy_tag,
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
        trailing_stop: dict | None = None,
        strategy_tag: str = "",
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
        # 服务端条件单是否已挂载（入场成交后才挂）
        server_sl_tp_placed = False
        trailing_stop = trailing_stop or {}
        trailing_active = False
        best_price = 0.0

        while True:
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
                    self._cancel_algo_orders_safe(symbol)
                    return self._close_position(
                        symbol, close_side, quantity, "monitor_error", start_time
                    )
                # 检查是否超时
                elapsed = time.monotonic() - start_time
                if elapsed >= max_hold_seconds:
                    if not position_opened:
                        self._cancel_entry_order(symbol)
                        self._cancel_algo_orders_safe(symbol)
                        return self._make_monitor_result(
                            status=OrderStatus.EXECUTION_FAILED.value,
                            close_price=0.0,
                            fee=0.0,
                            reason="entry_not_filled_timeout",
                            start_time=start_time,
                        )
                    self._cancel_algo_orders_safe(symbol)
                    return self._close_position(
                        symbol, close_side, quantity, "timeout", start_time
                    )
                if self._poll_interval > 0:
                    time.sleep(self._poll_interval)
                continue

            current_price = pos_risk.mark_price
            position_amt = abs(pos_risk.position_amt)

            # 入场成交检测：首次观测到持仓后挂服务端止损/止盈单
            if position_amt > 0 and not position_opened:
                position_opened = True
                # 挂保护单前先清理已有同方向保护条件单，避免 -4130 冲突
                self._cancel_conflicting_protection_orders(symbol, close_side)
                server_sl_tp_placed = self._place_server_sl_tp(
                    symbol=symbol,
                    close_side=close_side,
                    quantity=position_amt,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    trailing_stop=trailing_stop or {},
                )
            elif position_amt > 0:
                position_opened = True

            # 持仓为 0：要区分“未成交”与“已开仓后被平”
            if position_amt == 0:
                if position_opened:
                    # 持仓被清零（可能是服务端条件单触发），清理残留条件单
                    self._cancel_algo_orders_safe(symbol)
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
                        self._cancel_algo_orders_safe(symbol)
                        return self._make_monitor_result(
                            status=OrderStatus.EXECUTION_FAILED.value,
                            close_price=0.0,
                            fee=0.0,
                            reason="entry_not_filled_timeout",
                            start_time=start_time,
                        )
                    if self._poll_interval > 0:
                        time.sleep(self._poll_interval)
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
                    symbol, direction.value, strategy_tag=strategy_tag
                )
                self._cancel_algo_orders_safe(symbol)
                return self._close_position(
                    symbol, close_side, position_amt, "stop_loss", start_time
                )

            # 需求 4.6：止盈检查
            if self._should_take_profit(direction, current_price, take_profit_price):
                log.info(
                    f"[{self.name}] {symbol} 触发止盈: "
                    f"当前价={current_price}, 止盈价={take_profit_price}"
                )
                self._cancel_algo_orders_safe(symbol)
                return self._close_position(
                    symbol, close_side, position_amt, "take_profit", start_time
                )

            trailing_result = self._check_trailing_stop(
                direction=direction,
                current_price=current_price,
                trailing_stop=trailing_stop,
                trailing_active=trailing_active,
                best_price=best_price,
            )
            trailing_active = trailing_result["active"]
            best_price = trailing_result["best_price"]
            if trailing_result["triggered"]:
                log.info(
                    f"[{self.name}] {symbol} 触发移动止损: "
                    f"current={current_price}, trail={trailing_result['trail_price']}"
                )
                self._cancel_algo_orders_safe(symbol)
                return self._close_position(
                    symbol, close_side, position_amt, "trailing_stop", start_time
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
                    self._cancel_algo_orders_safe(symbol)
                    return self._make_monitor_result(
                        status=OrderStatus.EXECUTION_FAILED.value,
                        close_price=0.0,
                        fee=0.0,
                        reason="entry_not_filled_timeout",
                        start_time=start_time,
                    )
                self._cancel_algo_orders_safe(symbol)
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
                    self._cancel_algo_orders_safe(symbol)
                    return self._make_monitor_result(
                        status=OrderStatus.EXECUTION_FAILED.value,
                        close_price=0.0,
                        fee=0.0,
                        reason="daily_loss_before_fill",
                        start_time=start_time,
                    )
                self._cancel_algo_orders_safe(symbol)
                return self._close_position(
                    symbol,
                    close_side,
                    position_amt,
                    "daily_loss_degradation",
                    start_time,
                )

            # 首轮立即检查，后续再按 poll_interval 等待，缩短成交后的裸仓窗口。
            if self._poll_interval > 0:
                time.sleep(self._poll_interval)

    def _confirm_entry_and_place_protection(
        self,
        symbol: str,
        direction: TradeDirection,
        close_side: str,
        stop_loss_price: float,
        take_profit_price: float,
        planned_quantity: float,
        order_id: str,
        trailing_stop: Optional[dict] = None,
    ) -> dict:
        """
        非阻塞执行模式：短暂确认入场成交，挂好服务端保护后返回。

        若限价单在短时间内未成交，则主动撤单，避免本轮结束后订单才成交而
        暴露无保护仓位。

        若 trailing_stop 含有效配置（trail_pct > 0），在 SL/TP 挂单后
        同步向 Binance 提交 TRAILING_STOP_MARKET 条件单，使移动止损
        在非阻塞生产模式下也能由服务端执行（进程崩溃不丢失保护）。
        """
        start_time = time.monotonic()
        while True:
            try:
                pos_risk = self._binance_client.get_position_risk(symbol)
            except Exception as exc:
                log.warning(f"[{self.name}] {symbol} 确认入场成交失败: {exc}")
                pos_risk = None

            if pos_risk is not None:
                position_amt = abs(pos_risk.position_amt)
                if position_amt > 0:
                    # 入场成交后、挂保护单前：检测并清理已有同方向保护条件单，
                    # 避免 closePosition=True 重复挂载触发 Binance -4130 冲突
                    self._cancel_conflicting_protection_orders(symbol, close_side)
                    protection_placed = self._place_server_sl_tp(
                        symbol=symbol,
                        close_side=close_side,
                        quantity=position_amt,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        trailing_stop=trailing_stop or {},
                    )
                    if not protection_placed:
                        log.critical(
                            f"[{self.name}] {symbol} 入场已成交但服务端保护单全部挂载失败，"
                            f"立即平仓以避免裸仓"
                        )
                        close_result = self._close_position(
                            symbol=symbol,
                            side=close_side,
                            quantity=position_amt,
                            reason="protection_failed",
                            start_time=start_time,
                        )
                        return {
                            "status": close_result.get(
                                "status", OrderStatus.EXECUTION_FAILED.value
                            ),
                            "reason": (
                                "protection_failed_closed"
                                if close_result.get("status") == OrderStatus.FILLED.value
                                else "protection_failed_close_failed"
                            ),
                            "entry_price": pos_risk.entry_price,
                            "quantity": position_amt,
                        }
                    return {
                        "status": OrderStatus.OPEN.value,
                        "reason": "entry_filled_protection_placed",
                        "entry_price": pos_risk.entry_price,
                        "quantity": position_amt,
                    }

            elapsed = time.monotonic() - start_time
            if elapsed >= self._entry_confirm_timeout:
                if self._is_order_open(symbol, order_id):
                    self._cancel_entry_order(symbol)
                    return {
                        "status": OrderStatus.EXECUTION_FAILED.value,
                        "reason": "entry_not_filled_quick_timeout",
                        "entry_price": 0.0,
                        "quantity": 0.0,
                    }
                return {
                    "status": OrderStatus.EXECUTION_FAILED.value,
                    "reason": "entry_order_not_open_no_position",
                    "entry_price": 0.0,
                    "quantity": 0.0,
                }

            wait = min(
                ENTRY_CONFIRM_POLL_INTERVAL,
                self._entry_confirm_timeout - elapsed,
            )
            if wait > 0:
                time.sleep(wait)

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
            "status": status.lower() if isinstance(status, str) else status,
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

    @staticmethod
    def _check_trailing_stop(
        direction: TradeDirection,
        current_price: float,
        trailing_stop: dict,
        trailing_active: bool,
        best_price: float,
    ) -> dict:
        activation_price = float(trailing_stop.get("activation_price") or 0)
        trail_pct = float(trailing_stop.get("trail_pct") or 0)
        if activation_price <= 0 or trail_pct <= 0 or current_price <= 0:
            return {
                "active": trailing_active,
                "best_price": best_price,
                "triggered": False,
                "trail_price": 0.0,
            }

        if direction == TradeDirection.LONG:
            active = trailing_active or current_price >= activation_price
            best = max(best_price or current_price, current_price) if active else best_price
            trail_price = best * (1 - trail_pct / 100) if active else 0.0
            triggered = active and current_price <= trail_price
        else:
            active = trailing_active or current_price <= activation_price
            best = min(best_price or current_price, current_price) if active else best_price
            trail_price = best * (1 + trail_pct / 100) if active else 0.0
            triggered = active and current_price >= trail_price

        return {
            "active": active,
            "best_price": best,
            "triggered": triggered,
            "trail_price": trail_price,
        }

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
            # 以平仓名义金额计算真实手续费（taker 市价单）
            close_notional = quantity * result.price if result.price > 0 else 0.0
            close_fee = calc_crypto_fee(
                notional=close_notional,
                order_type=self._fee_order_type,
                vip_discount=self._fee_vip_discount,
            ).total
            log.info(
                f"[{self.name}] {symbol} 平仓成功: "
                f"原因={reason}, 价格={result.price}, 手续费={close_fee:.4f} USDT"
            )
            return self._make_monitor_result(
                status=OrderStatus.FILLED.value,
                close_price=result.price,
                fee=close_fee,
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

    def _place_server_sl_tp(
        self,
        symbol: str,
        close_side: str,
        quantity: float,
        stop_loss_price: float,
        take_profit_price: float,
        trailing_stop: Optional[dict] = None,
    ) -> bool:
        """
        在 Binance 服务端挂止损 + 止盈 + 移动止损条件单（三重保护）。

        仅在入场成交后调用，使用 closePosition=True 让 Binance 按当前仓位
        全平，避免另一侧残留保护单在仓位已平后反向开仓。
        任一条件单挂载失败不影响另一张，本地轮询作为兜底。

        若 trailing_stop 含有效的 trail_pct（> 0），则同步挂载
        TRAILING_STOP_MARKET 移动止损单，使非阻塞模式也能保护盈利。

        返回:
            True 表示至少一张条件单挂载成功
        """
        success = False
        stop_loss_price = self._normalize_price_for_exchange(
            symbol,
            stop_loss_price,
        )
        take_profit_price = self._normalize_price_for_exchange(
            symbol,
            take_profit_price,
        )
        if stop_loss_price is None or take_profit_price is None:
            log.warning(f"[{self.name}] {symbol} 服务端保护单价格规整失败，跳过挂载")
            return False

        # 挂止损单 (STOP_MARKET)
        try:
            sl_result = self._binance_client.place_stop_market_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                stop_price=stop_loss_price,
                close_position=True,
            )
            log.info(
                f"[{self.name}] {symbol} 服务端止损单已挂载: "
                f"triggerPrice={stop_loss_price}, closePosition=true, "
                f"algoId={sl_result.order_id}"
            )
            success = True
        except Exception as exc:
            log.warning(
                f"[{self.name}] {symbol} 服务端止损单挂载失败: {exc}，"
                f"将依赖本地轮询兜底"
            )

        # 挂止盈单 (TAKE_PROFIT_MARKET)
        try:
            tp_result = self._binance_client.place_take_profit_market_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                stop_price=take_profit_price,
                close_position=True,
            )
            log.info(
                f"[{self.name}] {symbol} 服务端止盈单已挂载: "
                f"triggerPrice={take_profit_price}, closePosition=true, "
                f"algoId={tp_result.order_id}"
            )
            success = True
        except Exception as exc:
            log.warning(
                f"[{self.name}] {symbol} 服务端止盈单挂载失败: {exc}，"
                f"将依赖本地轮询兜底"
            )

        # 挂移动止损单 (TRAILING_STOP_MARKET)
        if trailing_stop:
            self._place_server_trailing_stop(
                symbol=symbol,
                close_side=close_side,
                quantity=quantity,
                trailing_stop=trailing_stop,
            )

        return success

    def _place_server_trailing_stop(
        self,
        symbol: str,
        close_side: str,
        quantity: float,
        trailing_stop: dict,
    ) -> None:
        """
        向 Binance 提交 TRAILING_STOP_MARKET 移动止损条件单。

        参数:
            symbol: 交易对符号
            close_side: 平仓方向（"SELL" 或 "BUY"）
            quantity: 持仓数量
            trailing_stop: 移动止损配置字典，包含:
                - trail_pct: 回调比例（%），必填，0 < trail_pct <= 5.0
                - activation_price: 激活价格（可选），达到此价格才开始追踪
        """
        trail_pct = float(trailing_stop.get("trail_pct") or 0)
        activation_price = float(trailing_stop.get("activation_price") or 0)

        # Binance callbackRate 范围 0.1% ~ 5.0%，精度为 1 位小数（步进 0.1）
        if trail_pct <= 0:
            return
        callback_rate = round(max(0.1, min(trail_pct, 5.0)), 1)
        if callback_rate != trail_pct:
            log.warning(
                f"[{self.name}] {symbol} trail_pct={trail_pct} 超出 Binance "
                f"允许范围 [0.1, 5.0]，自动裁剪为 {callback_rate}"
            )

        # 对激活价格进行 tickSize 规整
        normalized_activation = (
            self._normalize_price_for_exchange(symbol, activation_price)
            if activation_price > 0
            else None
        )

        try:
            result = self._binance_client.place_trailing_stop_market_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                callback_rate=callback_rate,
                activation_price=normalized_activation,
                close_position=True,
            )
            log.info(
                f"[{self.name}] {symbol} 服务端移动止损单已挂载: "
                f"callbackRate={callback_rate}%, "
                f"activationPrice={normalized_activation}, "
                f"algoId={result.order_id}"
            )
        except Exception as exc:
            # 激活价可能已被市场价穿过（-2021），降级为不带激活价重试
            if normalized_activation is not None:
                log.warning(
                    f"[{self.name}] {symbol} 带激活价挂载失败: {exc}，"
                    f"降级为不带激活价重试（立即追踪）"
                )
                try:
                    result = self._binance_client.place_trailing_stop_market_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=quantity,
                        callback_rate=callback_rate,
                        activation_price=None,
                        close_position=True,
                    )
                    log.info(
                        f"[{self.name}] {symbol} 服务端移动止损单已挂载（无激活价）: "
                        f"callbackRate={callback_rate}%, algoId={result.order_id}"
                    )
                except Exception as retry_exc:
                    log.warning(
                        f"[{self.name}] {symbol} 移动止损单重试仍失败: {retry_exc}，"
                        f"将依赖本地轮询兜底"
                    )
            else:
                log.warning(
                    f"[{self.name}] {symbol} 服务端移动止损单挂载失败: {exc}，"
                    f"将依赖本地轮询兜底"
                )

    def _protect_existing_positions(self, account: AccountState) -> None:
        """
        为执行前已存在的实盘持仓补齐服务端止损/止盈。

        这些持仓不是本轮 Skill-4 开出来的，不会进入本地监控循环；如果没有
        Binance 服务端条件单保护，定时任务重启或网络异常时会暴露裸仓风险。
        """
        if self._risk_controller.is_paper_mode():
            return

        for raw_pos in account.positions:
            symbol = raw_pos.get("symbol", "") if isinstance(raw_pos, dict) else ""
            if not symbol:
                continue

            try:
                pos_risk = self._binance_client.get_position_risk(symbol)
            except Exception as exc:
                log.warning(f"[{self.name}] {symbol} 查询已有持仓失败: {exc}")
                continue

            position_amt = pos_risk.position_amt
            quantity = abs(position_amt)
            entry_price = pos_risk.entry_price
            current_price = pos_risk.mark_price
            if quantity <= 0 or entry_price <= 0 or current_price <= 0:
                continue

            direction = TradeDirection.LONG if position_amt > 0 else TradeDirection.SHORT
            close_side = "SELL" if direction == TradeDirection.LONG else "BUY"

            self._ensure_symbol_leverage(symbol)

            stop_loss_price, take_profit_price = self._calculate_existing_sl_tp(
                entry_price,
                direction,
            )

            if self._should_stop_loss(direction, current_price, stop_loss_price):
                log.warning(
                    f"[{self.name}] {symbol} 已有持仓触发保护性止损: "
                    f"当前价={current_price}, 止损价={stop_loss_price}"
                )
                self._risk_controller.record_stop_loss(symbol, direction.value, strategy_tag="existing_position")
                self._cancel_algo_orders_safe(symbol)
                self._close_position(
                    symbol, close_side, quantity, "existing_stop_loss", time.monotonic()
                )
                continue

            if self._should_take_profit(direction, current_price, take_profit_price):
                log.info(
                    f"[{self.name}] {symbol} 已有持仓触发保护性止盈: "
                    f"当前价={current_price}, 止盈价={take_profit_price}"
                )
                self._cancel_algo_orders_safe(symbol)
                self._close_position(
                    symbol, close_side, quantity, "existing_take_profit", time.monotonic()
                )
                continue

            try:
                algo_orders = self._binance_client.get_open_algo_orders(symbol)
            except Exception as exc:
                log.warning(f"[{self.name}] {symbol} 查询 Algo 条件单失败: {exc}")
                algo_orders = []

            valid_sl_count = self._valid_algo_order_count(
                algo_orders,
                close_side,
                "STOP_MARKET",
                stop_loss_price,
                quantity,
            )
            valid_tp_count = self._valid_algo_order_count(
                algo_orders,
                close_side,
                "TAKE_PROFIT_MARKET",
                take_profit_price,
                quantity,
            )
            protection_order_count = self._protection_algo_order_count(
                algo_orders,
                close_side,
            )
            if (
                valid_sl_count == 1
                and valid_tp_count == 1
                and protection_order_count == 2
            ):
                continue

            has_sl = valid_sl_count > 0
            has_tp = valid_tp_count > 0
            if protection_order_count > 0:
                log.warning(
                    f"[{self.name}] {symbol} 已有保护单触发价不匹配，撤销后重挂"
                )
                self._cancel_algo_orders_safe(symbol)
                has_sl = False
                has_tp = False

            log.warning(
                f"[{self.name}] {symbol} 已有持仓缺少服务端保护单，"
                f"补挂 SL={stop_loss_price}, TP={take_profit_price}, qty={quantity}"
            )
            self._place_missing_existing_protection(
                symbol=symbol,
                close_side=close_side,
                quantity=quantity,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                place_sl=not has_sl,
                place_tp=not has_tp,
            )

    def _cleanup_orphan_algo_orders(self, account: AccountState) -> None:
        """
        清理无对应持仓的服务端保护条件单。

        非阻塞生产路径下，止损/止盈由 Binance 服务端触发；另一侧条件单
        可能在本地进程未监控时残留。每轮执行开始时按当前持仓扫描一次，
        对无持仓币种的保护单整组撤销，避免后续触发反向开仓。
        """
        if self._risk_controller.is_paper_mode() or account.is_paper_mode:
            return

        active_symbols = self._active_position_symbols(account)
        try:
            algo_orders = self._binance_client.get_open_algo_orders()
        except Exception as exc:
            log.warning(f"[{self.name}] 查询全量 Algo 条件单失败: {exc}")
            return

        orphan_symbols: set[str] = set()
        for order in algo_orders:
            symbol = str(order.get("symbol", ""))
            if not symbol or symbol in active_symbols:
                continue
            if self._looks_like_protection_algo_order(order):
                orphan_symbols.add(symbol)

        for symbol in sorted(orphan_symbols):
            log.warning(f"[{self.name}] {symbol} 无持仓但存在保护条件单，清理残留")
            self._cancel_algo_orders_safe(symbol)

    @staticmethod
    def _active_position_symbols(account: AccountState) -> set[str]:
        """从账户状态提取当前有持仓的 symbol。"""
        symbols: set[str] = set()
        for pos in account.positions or []:
            if isinstance(pos, dict):
                symbol = str(pos.get("symbol", ""))
                quantity = abs(float(pos.get("quantity", 0) or 0))
            else:
                symbol = str(getattr(pos, "symbol", ""))
                quantity = abs(float(getattr(pos, "quantity", 0) or 0))
            if symbol and quantity > 0:
                symbols.add(symbol)
        return symbols

    def _ensure_symbol_leverage(self, symbol: str) -> bool:
        """将交易所侧 symbol 杠杆同步为 Skill-4 目标杠杆。"""
        try:
            self._binance_client.set_leverage(symbol, self._leverage)
            log.info(f"[{self.name}] {symbol} 杠杆已同步为 {self._leverage}x")
            return True
        except Exception as exc:
            log.warning(
                f"[{self.name}] {symbol} 设置杠杆 {self._leverage}x 失败: {exc}"
            )
            return False

    @staticmethod
    def _calculate_existing_sl_tp(
        entry_price: float,
        direction: TradeDirection,
    ) -> tuple[float, float]:
        """已有持仓没有对应策略计划时，使用固定 3%/6% 保护。"""
        if direction == TradeDirection.LONG:
            return (
                entry_price * (1 - EXISTING_POSITION_STOP_LOSS_PCT),
                entry_price * (1 + EXISTING_POSITION_TAKE_PROFIT_PCT),
            )
        return (
            entry_price * (1 + EXISTING_POSITION_STOP_LOSS_PCT),
            entry_price * (1 - EXISTING_POSITION_TAKE_PROFIT_PCT),
        )

    def _valid_algo_order_count(
        self,
        orders: list,
        side: str,
        order_type: str,
        expected_trigger_price: float,
        expected_quantity: float,
    ) -> int:
        count = 0
        for order in orders:
            if (
                str(order.get("side", "")).upper() == side
                and self._algo_order_type_matches(order, order_type)
                and self._trigger_price_matches(order, expected_trigger_price)
                and self._quantity_matches(order, expected_quantity)
            ):
                count += 1
        return count

    def _protection_algo_order_count(self, orders: list, side: str) -> int:
        count = 0
        for order in orders:
            if (
                str(order.get("side", "")).upper() == side
                and self._looks_like_protection_algo_order(order)
            ):
                count += 1
        return count

    @staticmethod
    def _algo_order_type_matches(order: dict, expected_type: str) -> bool:
        raw_type = (
            order.get("type")
            or order.get("origType")
            or order.get("orderType")
        )
        if raw_type:
            return str(raw_type).upper() == expected_type

        # Binance Algo Service open-order responses may omit STOP_MARKET /
        # TAKE_PROFIT_MARKET and only expose algoType plus triggerPrice.
        return (
            str(order.get("algoType", "")).upper() == "CONDITIONAL"
            and bool(order.get("triggerPrice") or order.get("stopPrice"))
        )

    @staticmethod
    def _looks_like_protection_algo_order(order: dict) -> bool:
        # 包含 TRAILING_STOP_MARKET，以便孤儿单清理也能覆盖移动止损残留单
        protection_types = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
        raw_type = (
            order.get("type")
            or order.get("origType")
            or order.get("orderType")
        )
        if raw_type:
            return str(raw_type).upper() in protection_types

        return (
            str(order.get("algoType", "")).upper() == "CONDITIONAL"
            and bool(
                order.get("triggerPrice")
                or order.get("stopPrice")
                or order.get("callbackRate")  # TRAILING_STOP_MARKET 标识字段
            )
        )

    @staticmethod
    def _trigger_price_matches(order: dict, expected_price: float) -> bool:
        if expected_price <= 0:
            return False

        raw_price = (
            order.get("triggerPrice")
            or order.get("stopPrice")
            or order.get("activatePrice")
        )
        try:
            actual_price = float(raw_price)
        except (TypeError, ValueError):
            return False

        diff_pct = abs(actual_price - expected_price) / expected_price
        return diff_pct <= PROTECTION_PRICE_TOLERANCE_PCT

    @staticmethod
    def _quantity_matches(order: dict, expected_quantity: float) -> bool:
        # closePosition 条件单由交易所按当前仓位全平，返回 quantity=0。
        # 这类保护单天然匹配当前持仓数量，不能按 quantity 字段判为不匹配。
        if order.get("closePosition") is True:
            return True

        raw_quantity = (
            order.get("quantity")
            or order.get("origQty")
            or order.get("origQuantity")
        )
        if raw_quantity in (None, ""):
            return True
        try:
            actual_quantity = abs(float(raw_quantity))
        except (TypeError, ValueError):
            return False
        if expected_quantity <= 0:
            return False
        diff_pct = abs(actual_quantity - expected_quantity) / expected_quantity
        return diff_pct <= PROTECTION_PRICE_TOLERANCE_PCT

    def _place_missing_existing_protection(
        self,
        symbol: str,
        close_side: str,
        quantity: float,
        stop_loss_price: float,
        take_profit_price: float,
        place_sl: bool,
        place_tp: bool,
    ) -> None:
        stop_loss_price = self._normalize_price_for_exchange(
            symbol,
            stop_loss_price,
        )
        take_profit_price = self._normalize_price_for_exchange(
            symbol,
            take_profit_price,
        )
        if stop_loss_price is None or take_profit_price is None:
            log.warning(f"[{self.name}] {symbol} 补挂保护单价格规整失败，跳过")
            return

        if place_sl:
            try:
                self._binance_client.place_stop_market_order(
                    symbol=symbol,
                    side=close_side,
                    quantity=quantity,
                    stop_price=stop_loss_price,
                    close_position=True,
                )
            except Exception as exc:
                log.warning(f"[{self.name}] {symbol} 补挂已有持仓止损失败: {exc}")

        if place_tp:
            try:
                self._binance_client.place_take_profit_market_order(
                    symbol=symbol,
                    side=close_side,
                    quantity=quantity,
                    stop_price=take_profit_price,
                    close_position=True,
                )
            except Exception as exc:
                log.warning(f"[{self.name}] {symbol} 补挂已有持仓止盈失败: {exc}")

    def _normalize_quantity_for_exchange(
        self,
        symbol: str,
        quantity: float,
        price: float,
    ) -> Optional[float]:
        """执行前按 Binance 交易规则兜底规整开仓数量。"""
        if self._trading_rule_provider is None:
            return quantity

        try:
            rule = self._trading_rule_provider(symbol)
        except Exception as exc:
            log.warning(f"[{self.name}] 获取 {symbol} 交易规则失败: {exc}")
            return None

        if rule is None:
            log.warning(f"[{self.name}] {symbol} 缺少交易规则，拒绝执行")
            return None

        return normalize_order_quantity(
            symbol=symbol,
            quantity=quantity,
            price=price,
            rule=rule,
        )

    def _normalize_price_for_exchange(
        self,
        symbol: str,
        price: float,
    ) -> Optional[float]:
        """执行前按 Binance PRICE_FILTER.tickSize 规整价格。"""
        if self._trading_rule_provider is None:
            return price

        try:
            rule = self._trading_rule_provider(symbol)
        except Exception as exc:
            log.warning(f"[{self.name}] 获取 {symbol} 交易规则失败: {exc}")
            return None

        if rule is None:
            log.warning(f"[{self.name}] {symbol} 缺少交易规则，拒绝执行")
            return None

        return normalize_order_price(
            symbol=symbol,
            price=price,
            rule=rule,
        )

    def _cancel_conflicting_protection_orders(
        self, symbol: str, close_side: str
    ) -> None:
        """
        在挂载新保护单前，检测并撤销已有的同方向 closePosition 条件单。

        Binance 不允许同一 symbol 同方向同时存在多张 closePosition=True 的
        STOP_MARKET / TAKE_PROFIT_MARKET 条件单，重复挂载会触发 -4130 错误。
        本方法在 open_position 成交后、_place_server_sl_tp 调用前执行，
        确保旧保护单已清理，避免死锁。
        """
        try:
            algo_orders = self._binance_client.get_open_algo_orders(symbol)
        except Exception as exc:
            log.warning(
                f"[{self.name}] {symbol} 查询已有保护条件单失败，跳过冲突检测: {exc}"
            )
            return

        conflicting = [
            o for o in algo_orders
            if (
                str(o.get("side", "")).upper() == close_side.upper()
                and self._looks_like_protection_algo_order(o)
            )
        ]
        if not conflicting:
            return

        log.warning(
            f"[{self.name}] {symbol} 检测到 {len(conflicting)} 张已有 "
            f"{close_side} 方向保护条件单，挂载前先撤销以避免 -4130 冲突"
        )
        self._cancel_algo_orders_safe(symbol)

    def _cancel_algo_orders_safe(self, symbol: str) -> None:
        """安全清理指定币种的所有 Algo 条件单，防止残留。"""
        try:
            self._binance_client.cancel_all_algo_orders(symbol=symbol)
            log.info(f"[{self.name}] {symbol} 已清理 Algo 条件单")
        except Exception as exc:
            log.warning(f"[{self.name}] {symbol} 清理 Algo 条件单失败: {exc}")

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
        strategy_tag: str = "",
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
            "status": status.lower() if isinstance(status, str) else status,
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
        if strategy_tag:
            result["strategy_tag"] = strategy_tag
        return result
