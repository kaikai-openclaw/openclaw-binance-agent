"""
Skill-4 自动交易执行 单元测试。

覆盖场景：
1. 正常流程：读取交易计划 → 风控校验 → 下单 → 持仓监控 → 输出
2. 风控拒绝 → status = rejected_by_risk
3. Paper Mode → status = paper_trade
4. 止损触发 → 市价平仓
5. 止盈触发 → 市价平仓
6. 超时平仓
7. 日亏损降级 → 切换 Paper Mode
8. 下单异常 → status = execution_failed
9. 空交易计划
10. 多笔交易混合场景

需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.11, 4.12, 4.13
"""

import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from src.infra.binance_fapi import BinanceFapiClient, OrderResult, PositionRisk
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    OrderStatus,
    TradeDirection,
    ValidationResult,
)
from src.skills.skill4_execute import Skill4Execute


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill4_input.json")
OUTPUT_SCHEMA = _load_schema("skill4_output.json")


# ── 辅助函数 ──────────────────────────────────────────────

def _make_account(
    total_balance: float = 10000.0,
    available_margin: float = 8000.0,
    daily_realized_pnl: float = 0.0,
    positions: list | None = None,
    is_paper_mode: bool = False,
) -> AccountState:
    """构造 AccountState。"""
    return AccountState(
        total_balance=total_balance,
        available_margin=available_margin,
        daily_realized_pnl=daily_realized_pnl,
        positions=positions or [],
        is_paper_mode=is_paper_mode,
    )


def _make_trade_plan(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_price_upper: float = 101.0,
    entry_price_lower: float = 99.0,
    position_size_pct: float = 10.0,
    stop_loss_price: float = 97.0,
    take_profit_price: float = 106.0,
    max_hold_hours: float = 24.0,
) -> dict:
    """构造单笔交易计划。"""
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price_upper": entry_price_upper,
        "entry_price_lower": entry_price_lower,
        "position_size_pct": position_size_pct,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price,
        "max_hold_hours": max_hold_hours,
    }


def _make_upstream_data(trade_plans: list) -> dict:
    """构造 Skill-3 输出数据。"""
    return {
        "state_id": str(uuid.uuid4()),
        "trade_plans": trade_plans,
        "pipeline_status": "has_trades" if trade_plans else "no_opportunity",
    }


def _make_order_result(
    order_id: str = "12345",
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: float = 100.0,
    quantity: float = 10.0,
    status: str = "NEW",
) -> OrderResult:
    """构造 OrderResult。"""
    return OrderResult(
        order_id=order_id,
        symbol=symbol,
        side=side,
        price=price,
        quantity=quantity,
        status=status,
    )


def _make_position_risk(
    symbol: str = "BTCUSDT",
    position_amt: float = 10.0,
    entry_price: float = 100.0,
    mark_price: float = 100.0,
    unrealized_pnl: float = 0.0,
) -> PositionRisk:
    """构造 PositionRisk。"""
    return PositionRisk(
        symbol=symbol,
        position_amt=position_amt,
        entry_price=entry_price,
        mark_price=mark_price,
        unrealized_pnl=unrealized_pnl,
        liquidation_price=80.0,
        leverage=10,
    )


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


@pytest.fixture
def mock_binance():
    """创建 mock BinanceFapiClient。"""
    client = MagicMock(spec=BinanceFapiClient)
    client.place_limit_order.return_value = _make_order_result()
    client.place_market_order.return_value = _make_order_result(
        price=100.0, status="FILLED"
    )
    client.place_stop_market_order.return_value = _make_order_result(
        order_id="algo_sl_001", status="NEW"
    )
    client.place_take_profit_market_order.return_value = _make_order_result(
        order_id="algo_tp_001", status="NEW"
    )
    client.get_open_orders.return_value = []
    client.cancel_all_orders.return_value = 1
    client.cancel_all_algo_orders.return_value = 1
    return client


@pytest.fixture
def mock_risk_controller():
    """创建 mock RiskController。"""
    rc = MagicMock(spec=RiskController)
    rc.validate_order.return_value = ValidationResult(passed=True)
    rc.check_daily_loss.return_value = False
    rc.is_paper_mode.return_value = False
    return rc


def _make_skill(
    state_store,
    binance_client,
    risk_controller,
    account=None,
    poll_interval=0,
) -> Skill4Execute:
    """创建 Skill4Execute 实例。"""
    if account is None:
        account = _make_account()

    return Skill4Execute(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        binance_client=binance_client,
        risk_controller=risk_controller,
        account_state_provider=lambda: account,
        poll_interval=poll_interval,
    )


# ══════════════════════════════════════════════════════════
# 1. 正常执行流程
# ══════════════════════════════════════════════════════════

class TestNormalExecution:
    """测试正常执行流程。"""

    def test_basic_long_execution(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做多交易应正常执行并返回 filled 状态。"""
        # 设置持仓监控：第一次轮询即触发止盈
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0, status="FILLED"
        )

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert len(result["execution_results"]) == 1
        assert result["execution_results"][0]["status"] == "filled"
        assert result["execution_results"][0]["symbol"] == "BTCUSDT"
        assert result["is_paper_mode"] is False

    def test_basic_short_execution(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做空交易应正常执行。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=90.0, position_amt=-10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=90.0, status="FILLED"
        )

        plan = _make_trade_plan(
            direction="short",
            stop_loss_price=103.0,
            take_profit_price=94.0,
        )
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        assert result["execution_results"][0]["direction"] == "short"

    def test_state_id_is_uuid(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """输出的 state_id 应为有效 UUID。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0
        )

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        uuid.UUID(result["state_id"], version=4)

    def test_skill_name(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """Skill 名称应为 skill4_execute。"""
        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        assert skill.name == "skill4_execute"


# ══════════════════════════════════════════════════════════
# 2. 风控拒绝
# ══════════════════════════════════════════════════════════

class TestRiskRejection:
    """测试风控拒绝场景。"""

    def test_risk_rejected_order(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """风控拒绝的订单应标记为 rejected_by_risk。"""
        mock_risk_controller.validate_order.return_value = ValidationResult(
            passed=False, reason="单笔保证金超限"
        )

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "rejected_by_risk"
        # 不应调用下单接口
        mock_binance.place_limit_order.assert_not_called()

    def test_cooldown_rejection(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """止损冷却期内的订单应被拒绝。"""
        mock_risk_controller.validate_order.return_value = ValidationResult(
            passed=False, reason="止损冷却期内禁止同方向开仓"
        )

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "rejected_by_risk"


# ══════════════════════════════════════════════════════════
# 3. Paper Mode
# ══════════════════════════════════════════════════════════

class TestPaperMode:
    """测试 Paper Mode 场景。"""

    def test_paper_mode_order(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """Paper Mode 下订单应标记为 paper_trade。"""
        mock_risk_controller.is_paper_mode.return_value = True

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "paper_trade"
        assert result["is_paper_mode"] is True
        # Paper Mode 不应调用真实下单接口
        mock_binance.place_limit_order.assert_not_called()

    def test_paper_mode_has_order_id(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """Paper Mode 订单应有 paper_ 前缀的 order_id。"""
        mock_risk_controller.is_paper_mode.return_value = True

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["order_id"].startswith("paper_")

    def test_paper_mode_has_price_and_quantity(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """Paper Mode 订单应包含模拟的价格和数量。"""
        mock_risk_controller.is_paper_mode.return_value = True

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        r = result["execution_results"][0]
        assert r["executed_price"] > 0
        assert r["executed_quantity"] > 0


# ══════════════════════════════════════════════════════════
# 4. 止损触发
# ══════════════════════════════════════════════════════════

class TestStopLoss:
    """测试止损触发场景。"""

    def test_long_stop_loss(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做多持仓价格跌至止损价应触发平仓。"""
        # 当前价 95 < 止损价 97
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=95.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=95.0, status="FILLED"
        )

        plan = _make_trade_plan(stop_loss_price=97.0, take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        # 应调用市价平仓
        mock_binance.place_market_order.assert_called()
        # 应记录止损事件
        mock_risk_controller.record_stop_loss.assert_called_once_with(
            "BTCUSDT", "long"
        )

    def test_short_stop_loss(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做空持仓价格涨至止损价应触发平仓。"""
        # 当前价 105 > 止损价 103
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=105.0, position_amt=-10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=105.0, status="FILLED"
        )

        plan = _make_trade_plan(
            direction="short",
            stop_loss_price=103.0,
            take_profit_price=94.0,
        )
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        mock_risk_controller.record_stop_loss.assert_called_once_with(
            "BTCUSDT", "short"
        )


# ══════════════════════════════════════════════════════════
# 5. 止盈触发
# ══════════════════════════════════════════════════════════

class TestTakeProfit:
    """测试止盈触发场景。"""

    def test_long_take_profit(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做多持仓价格涨至止盈价应触发平仓。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0, status="FILLED"
        )

        plan = _make_trade_plan(take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        # 止盈不应记录止损事件
        mock_risk_controller.record_stop_loss.assert_not_called()

    def test_short_take_profit(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做空持仓价格跌至止盈价应触发平仓。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=90.0, position_amt=-10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=90.0, status="FILLED"
        )

        plan = _make_trade_plan(
            direction="short",
            stop_loss_price=103.0,
            take_profit_price=94.0,
        )
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"


# ══════════════════════════════════════════════════════════
# 6. 超时平仓
# ══════════════════════════════════════════════════════════

class TestTimeout:
    """测试超时平仓场景。"""

    def test_timeout_close(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """持仓超时应触发市价平仓。"""
        # 价格在止损和止盈之间，不触发止损/止盈
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=100.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=100.0, status="FILLED"
        )

        # max_hold_hours=0 使得立即超时
        plan = _make_trade_plan(max_hold_hours=0.0001)  # 约 0.36 秒
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller,
            poll_interval=0,
        )
        result = skill.run({"input_state_id": state_id})

        # 应该触发超时平仓（或止损/止盈，取决于价格）
        assert result["execution_results"][0]["status"] == "filled"


# ══════════════════════════════════════════════════════════
# 7. 日亏损降级
# ══════════════════════════════════════════════════════════

class TestDailyLossDegradation:
    """测试日亏损降级场景。"""

    def test_daily_loss_triggers_paper_mode(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """日亏损触及阈值应切换至 Paper Mode。"""
        # 初始检查触发降级
        mock_risk_controller.check_daily_loss.return_value = True
        # 降级后 is_paper_mode 返回 True
        mock_risk_controller.is_paper_mode.return_value = True

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        # 应执行降级
        mock_risk_controller.execute_degradation.assert_called_once()
        # 后续订单应为 paper_trade
        assert result["execution_results"][0]["status"] == "paper_trade"
        assert result["is_paper_mode"] is True


# ══════════════════════════════════════════════════════════
# 8. 下单异常
# ══════════════════════════════════════════════════════════

class TestOrderFailure:
    """测试下单异常场景。"""

    def test_order_exception(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """下单抛出异常应标记为 execution_failed。"""
        mock_binance.place_limit_order.side_effect = Exception("网络超时")

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "execution_failed"

    def test_close_exception_should_be_execution_failed(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """开仓后平仓失败不应误标为 filled。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=10.0
        )
        mock_binance.place_market_order.side_effect = Exception("平仓网络错误")

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "execution_failed"


# ══════════════════════════════════════════════════════════
# 9. 空交易计划
# ══════════════════════════════════════════════════════════

class TestEmptyPlans:
    """测试空交易计划场景。"""

    def test_empty_trade_plans(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """空交易计划应返回空执行结果。"""
        upstream = _make_upstream_data([])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"] == []
        assert result["is_paper_mode"] is False


# ══════════════════════════════════════════════════════════
# 10. 多笔交易混合场景
# ══════════════════════════════════════════════════════════

class TestMultipleTrades:
    """测试多笔交易混合场景。"""

    def test_mixed_results(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """多笔交易应各自独立执行并返回结果。"""
        # 第一笔通过风控，第二笔被拒绝
        mock_risk_controller.validate_order.side_effect = [
            ValidationResult(passed=True),
            ValidationResult(passed=False, reason="单币累计持仓超限"),
        ]
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0
        )

        plans = [
            _make_trade_plan(symbol="BTCUSDT"),
            _make_trade_plan(symbol="ETHUSDT"),
        ]
        upstream = _make_upstream_data(plans)
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert len(result["execution_results"]) == 2
        assert result["execution_results"][0]["status"] == "filled"
        assert result["execution_results"][1]["status"] == "rejected_by_risk"


# ══════════════════════════════════════════════════════════
# 11. 持仓已被外部平仓
# ══════════════════════════════════════════════════════════

class TestExternalClose:
    """测试持仓被外部平仓的场景。"""

    def test_position_already_closed(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """首轮即无持仓且无挂单时应识别为入场失败。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=100.0, position_amt=0.0
        )
        mock_binance.get_open_orders.return_value = []

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "execution_failed"
        # 未成交超时/失效场景不会触发市价平仓
        mock_binance.place_market_order.assert_not_called()

    def test_position_closed_after_opened(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """先观测到持仓后变为 0，应识别为外部平仓并标记 filled。"""
        mock_binance.get_position_risk.side_effect = [
            _make_position_risk(mark_price=100.0, position_amt=10.0),
            _make_position_risk(mark_price=101.0, position_amt=0.0),
        ]

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(
            state_store, mock_binance, mock_risk_controller
        )
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        mock_binance.place_market_order.assert_not_called()


# ══════════════════════════════════════════════════════════
# 12. 服务端止损/止盈条件单管理
# ══════════════════════════════════════════════════════════

class TestServerSideSlTp:
    """测试服务端止损/止盈条件单的挂载与清理。"""

    def test_sl_tp_placed_after_entry_fill(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """入场成交后应挂载服务端止损 + 止盈条件单，数量等于实际持仓。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0, status="FILLED"
        )

        plan = _make_trade_plan(stop_loss_price=97.0, take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        skill.run({"input_state_id": state_id})

        # 应挂止损单，数量 = 实际持仓 10.0
        mock_binance.place_stop_market_order.assert_called_once_with(
            symbol="BTCUSDT", side="SELL", quantity=10.0, stop_price=97.0
        )
        # 应挂止盈单
        mock_binance.place_take_profit_market_order.assert_called_once_with(
            symbol="BTCUSDT", side="SELL", quantity=10.0, stop_price=106.0
        )

    def test_sl_tp_uses_actual_position_amt(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """条件单数量应使用实际持仓数量，而非计划数量。"""
        # 实际持仓 8.5（可能因精度裁剪与计划不同）
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=8.5
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0, status="FILLED"
        )

        plan = _make_trade_plan(stop_loss_price=97.0, take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        skill.run({"input_state_id": state_id})

        # 数量应为 8.5 而非计划中的计算值
        mock_binance.place_stop_market_order.assert_called_once()
        call_args = mock_binance.place_stop_market_order.call_args
        assert call_args.kwargs.get("quantity") == 8.5 or call_args[1].get("quantity") == 8.5

    def test_algo_orders_cleaned_on_stop_loss(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """本地轮询触发止损时应清理服务端条件单。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=95.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=95.0, status="FILLED"
        )

        plan = _make_trade_plan(stop_loss_price=97.0, take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        skill.run({"input_state_id": state_id})

        mock_binance.cancel_all_algo_orders.assert_called_with(symbol="BTCUSDT")

    def test_algo_orders_cleaned_on_external_close(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """持仓被外部平仓（服务端条件单触发）后应清理残留条件单。"""
        mock_binance.get_position_risk.side_effect = [
            _make_position_risk(mark_price=100.0, position_amt=10.0),
            _make_position_risk(mark_price=101.0, position_amt=0.0),
        ]

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "filled"
        mock_binance.cancel_all_algo_orders.assert_called_with(symbol="BTCUSDT")

    def test_sl_tp_failure_does_not_block_execution(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """服务端条件单挂载失败不应阻断交易执行，本地轮询兜底。"""
        mock_binance.place_stop_market_order.side_effect = Exception("Algo API 异常")
        mock_binance.place_take_profit_market_order.side_effect = Exception("Algo API 异常")
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=110.0, position_amt=10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=110.0, status="FILLED"
        )

        plan = _make_trade_plan(take_profit_price=106.0)
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        result = skill.run({"input_state_id": state_id})

        # 即使条件单失败，本地轮询仍应正常触发止盈
        assert result["execution_results"][0]["status"] == "filled"

    def test_no_sl_tp_before_entry_fill(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """入场未成交时不应挂条件单。"""
        # 持仓为 0 且无挂单 → 入场失败
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=100.0, position_amt=0.0
        )
        mock_binance.get_open_orders.return_value = []

        upstream = _make_upstream_data([_make_trade_plan()])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        result = skill.run({"input_state_id": state_id})

        assert result["execution_results"][0]["status"] == "execution_failed"
        mock_binance.place_stop_market_order.assert_not_called()
        mock_binance.place_take_profit_market_order.assert_not_called()

    def test_short_direction_sl_tp_side(
        self, state_store, mock_binance, mock_risk_controller
    ):
        """做空交易的条件单平仓方向应为 BUY。"""
        mock_binance.get_position_risk.return_value = _make_position_risk(
            mark_price=90.0, position_amt=-10.0
        )
        mock_binance.place_market_order.return_value = _make_order_result(
            price=90.0, status="FILLED"
        )

        plan = _make_trade_plan(
            direction="short", stop_loss_price=103.0, take_profit_price=94.0
        )
        upstream = _make_upstream_data([plan])
        state_id = state_store.save("skill3_strategy", upstream)

        skill = _make_skill(state_store, mock_binance, mock_risk_controller)
        skill.run({"input_state_id": state_id})

        mock_binance.place_stop_market_order.assert_called_once_with(
            symbol="BTCUSDT", side="BUY", quantity=10.0, stop_price=103.0
        )
        mock_binance.place_take_profit_market_order.assert_called_once_with(
            symbol="BTCUSDT", side="BUY", quantity=10.0, stop_price=94.0
        )
