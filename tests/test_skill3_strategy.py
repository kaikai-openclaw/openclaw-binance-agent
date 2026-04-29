"""
Skill-3 交易策略制定 单元测试。

覆盖场景：
1. 正常流程：读取评级 → 生成交易计划 → 风控预校验 → 输出
2. 空评级列表 → pipeline_status = "no_opportunity"
3. hold 信号跳过
4. 头寸规模超限自动裁剪至 20%
5. 风控预校验失败后尝试裁剪
6. 风控预校验失败且无法裁剪
7. 做多/做空方向止损止盈计算
8. 数值参数边界校验
9. 多币种混合场景
10. 自定义风险比例和持仓上限

需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import json
import os
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.infra.exchange_rules import SymbolTradingRule
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    PipelineStatus,
    TradeDirection,
    ValidationResult,
)
from src.skills.skill3_strategy import (
    DEFAULT_LEVERAGE,
    DEFAULT_MAX_HOLD_HOURS,
    DEFAULT_RISK_RATIO,
    Skill3Strategy,
)


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill3_input.json")
OUTPUT_SCHEMA = _load_schema("skill3_output.json")


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


def _make_account(
    total_balance: float = 10000.0,
    available_margin: float = 8000.0,
    daily_realized_pnl: float = 0.0,
    positions: list | None = None,
) -> AccountState:
    """构造 AccountState 的辅助函数。"""
    return AccountState(
        total_balance=total_balance,
        available_margin=available_margin,
        daily_realized_pnl=daily_realized_pnl,
        positions=positions or [],
    )


def _make_upstream_data(ratings: list) -> dict:
    """构造 Skill-2 输出数据。"""
    return {
        "state_id": str(uuid.uuid4()),
        "ratings": ratings,
        "filtered_count": 0,
    }


def _make_skill(
    state_store,
    risk_controller=None,
    account=None,
    risk_ratio=DEFAULT_RISK_RATIO,
    max_hold_hours=DEFAULT_MAX_HOLD_HOURS,
    leverage=DEFAULT_LEVERAGE,
    trading_rule_provider=None,
) -> Skill3Strategy:
    """创建 Skill3Strategy 实例的辅助函数。"""
    if risk_controller is None:
        risk_controller = RiskController()
    if account is None:
        account = _make_account()

    return Skill3Strategy(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        risk_controller=risk_controller,
        account_state_provider=lambda: account,
        risk_ratio=risk_ratio,
        max_hold_hours=max_hold_hours,
        leverage=leverage,
        trading_rule_provider=trading_rule_provider,
    )


# ══════════════════════════════════════════════════════════
# 1. 正常执行流程
# ══════════════════════════════════════════════════════════

class TestNormalExecution:
    """测试正常执行流程。"""

    def test_basic_long_trade_plan(self, state_store):
        """做多信号应生成有效的交易计划。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert len(result["trade_plans"]) == 1
        plan = result["trade_plans"][0]
        assert plan["symbol"] == "BTCUSDT"
        assert plan["direction"] == "long"
        assert plan["entry_price_upper"] > plan["entry_price_lower"]
        assert plan["position_size_pct"] > 0
        assert plan["position_size_pct"] <= 20.0
        assert plan["stop_loss_price"] > 0
        assert plan["take_profit_price"] > 0
        assert plan["max_hold_hours"] == DEFAULT_MAX_HOLD_HOURS
        assert result["pipeline_status"] == "has_trades"

    def test_basic_short_trade_plan(self, state_store):
        """做空信号应生成有效的交易计划。"""
        upstream = _make_upstream_data([
            {"symbol": "ETHUSDT", "rating_score": 7, "signal": "short", "confidence": 70.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert len(result["trade_plans"]) == 1
        plan = result["trade_plans"][0]
        assert plan["symbol"] == "ETHUSDT"
        assert plan["direction"] == "short"
        # 做空：止损 > 入场价，止盈 < 入场价
        assert plan["stop_loss_price"] > plan["entry_price_lower"]
        assert plan["take_profit_price"] < plan["entry_price_lower"]

    def test_state_id_is_uuid(self, state_store):
        """输出的 state_id 应为有效 UUID。"""
        upstream = _make_upstream_data([])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        uuid.UUID(result["state_id"], version=4)

    def test_skill_name(self, state_store):
        """Skill 名称应为 skill3_strategy。"""
        skill = _make_skill(state_store)
        assert skill.name == "skill3_strategy"

    def test_quantity_is_floored_by_lot_size(self, state_store):
        """stepSize=1 的币种应输出整数 quantity。"""
        upstream = _make_upstream_data([
            {"symbol": "APEUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)
        account = _make_account(total_balance=1234.0, available_margin=1000.0)
        rule = SymbolTradingRule(
            symbol="APEUSDT",
            step_size=Decimal("1"),
            min_qty=Decimal("1"),
            min_notional=Decimal("5"),
        )

        skill = _make_skill(
            state_store,
            account=account,
            trading_rule_provider=lambda symbol: rule if symbol == "APEUSDT" else None,
        )
        result = skill.run({"input_state_id": state_id})

        plan = result["trade_plans"][0]
        assert plan["quantity"] == 2.0
        assert plan["notional_value"] == 200.0

    def test_quantity_below_min_notional_is_rejected(self, state_store):
        """规整后名义金额低于 5 USDT 时不应生成交易计划。"""
        upstream = _make_upstream_data([
            {"symbol": "PULUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)
        account = _make_account(total_balance=20.0, available_margin=20.0)
        rule = SymbolTradingRule(
            symbol="PULUSDT",
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

        skill = _make_skill(
            state_store,
            account=account,
            trading_rule_provider=lambda symbol: rule if symbol == "PULUSDT" else None,
        )
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"] == []
        assert result["pipeline_status"] == PipelineStatus.NO_OPPORTUNITY.value

    def test_long_sl_tp_direction(self, state_store):
        """做多方向：止损 < 入场价，止盈 > 入场价。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        plan = result["trade_plans"][0]
        # 入场基准价 = 100，做多止损 = 97，止盈 = 106
        assert plan["stop_loss_price"] < plan["entry_price_lower"]
        assert plan["take_profit_price"] > plan["entry_price_upper"]

    def test_short_sl_tp_direction(self, state_store):
        """做空方向：止损 > 入场价，止盈 < 入场价。"""
        upstream = _make_upstream_data([
            {"symbol": "ETHUSDT", "rating_score": 7, "signal": "short", "confidence": 70.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        plan = result["trade_plans"][0]
        assert plan["stop_loss_price"] > plan["entry_price_upper"]
        assert plan["take_profit_price"] < plan["entry_price_lower"]


# ══════════════════════════════════════════════════════════
# 2. 空评级列表场景
# ══════════════════════════════════════════════════════════

class TestEmptyRatings:
    """测试空评级列表场景。"""

    def test_empty_ratings_no_opportunity(self, state_store):
        """空评级列表应标记为 no_opportunity。"""
        upstream = _make_upstream_data([])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"] == []
        assert result["pipeline_status"] == "no_opportunity"

    def test_upstream_no_ratings_key(self, state_store):
        """上游数据缺少 ratings 键时应返回 no_opportunity。"""
        upstream = {"state_id": str(uuid.uuid4()), "filtered_count": 0}
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"] == []
        assert result["pipeline_status"] == "no_opportunity"

    def test_all_hold_signals_no_opportunity(self, state_store):
        """所有信号都是 hold 时应标记为 no_opportunity。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "hold", "confidence": 80.0},
            {"symbol": "ETHUSDT", "rating_score": 7, "signal": "hold", "confidence": 70.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"] == []
        assert result["pipeline_status"] == "no_opportunity"


# ══════════════════════════════════════════════════════════
# 3. 风控预校验与头寸裁剪
# ══════════════════════════════════════════════════════════

class TestRiskPrecheck:
    """测试风控预校验与头寸裁剪。"""

    def test_position_size_capped_at_20_pct(self, state_store):
        """头寸规模百分比不应超过 20%。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 9, "signal": "long", "confidence": 90.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        if result["trade_plans"]:
            assert result["trade_plans"][0]["position_size_pct"] <= 20.0

    def test_risk_controller_rejection_with_adjustment(self, state_store):
        """风控拒绝后应尝试裁剪头寸。"""
        rc = MagicMock(spec=RiskController)
        # 前几次拒绝，裁剪后通过
        rc.validate_order.side_effect = [
            ValidationResult(passed=False, reason="单笔保证金超限"),
            ValidationResult(passed=True),
        ]

        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store, risk_controller=rc)
        result = skill.run({"input_state_id": state_id})

        assert len(result["trade_plans"]) == 1
        assert result["pipeline_status"] == "has_trades"

    def test_risk_controller_rejection_no_adjustment(self, state_store):
        """风控持续拒绝且无法裁剪时应跳过该币种。"""
        rc = MagicMock(spec=RiskController)
        # 始终拒绝（如止损冷却期）
        rc.validate_order.return_value = ValidationResult(
            passed=False, reason="止损冷却期内禁止同方向开仓"
        )

        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store, risk_controller=rc)
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"] == []
        assert result["pipeline_status"] == "no_opportunity"

    def test_all_positive_values_in_plan(self, state_store):
        """交易计划中所有数值字段应为正数。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        for plan in result["trade_plans"]:
            assert plan["entry_price_upper"] > 0
            assert plan["entry_price_lower"] > 0
            assert plan["position_size_pct"] > 0
            assert plan["stop_loss_price"] > 0
            assert plan["take_profit_price"] > 0
            assert plan["max_hold_hours"] > 0


# ══════════════════════════════════════════════════════════
# 4. 多币种混合场景
# ══════════════════════════════════════════════════════════

class TestMultipleCoins:
    """测试多币种混合场景。"""

    def test_mixed_signals(self, state_store):
        """混合信号（long/short/hold）应正确处理。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 9, "signal": "long", "confidence": 90.0},
            {"symbol": "ETHUSDT", "rating_score": 7, "signal": "hold", "confidence": 60.0},
            {"symbol": "SOLUSDT", "rating_score": 8, "signal": "short", "confidence": 75.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        # ETHUSDT 是 hold 应被跳过
        symbols = [p["symbol"] for p in result["trade_plans"]]
        assert "BTCUSDT" in symbols
        assert "SOLUSDT" in symbols
        assert "ETHUSDT" not in symbols
        assert result["pipeline_status"] == "has_trades"

    def test_multiple_long_plans(self, state_store):
        """多个做多信号应各自生成独立的交易计划。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 9, "signal": "long", "confidence": 90.0},
            {"symbol": "ETHUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        assert len(result["trade_plans"]) == 2
        assert result["trade_plans"][0]["direction"] == "long"
        assert result["trade_plans"][1]["direction"] == "long"


# ══════════════════════════════════════════════════════════
# 5. 自定义参数
# ══════════════════════════════════════════════════════════

class TestCustomParameters:
    """测试自定义参数。"""

    def test_custom_max_hold_hours(self, state_store):
        """自定义持仓时间上限应生效。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store, max_hold_hours=48.0)
        result = skill.run({"input_state_id": state_id})

        assert result["trade_plans"][0]["max_hold_hours"] == 48.0

    def test_custom_risk_ratio(self, state_store):
        """自定义风险比例应影响头寸规模。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        # 较小的风险比例应产生较小的头寸
        skill_small = _make_skill(state_store, risk_ratio=0.01)
        result_small = skill_small.run({"input_state_id": state_id})

        skill_large = _make_skill(state_store, risk_ratio=0.05)
        result_large = skill_large.run({"input_state_id": state_id})

        if result_small["trade_plans"] and result_large["trade_plans"]:
            assert (
                result_small["trade_plans"][0]["position_size_pct"]
                <= result_large["trade_plans"][0]["position_size_pct"]
            )


# ══════════════════════════════════════════════════════════
# 6. Schema 集成验证
# ══════════════════════════════════════════════════════════

class TestSchemaIntegration:
    """测试通过 BaseSkill.execute() 的完整 Schema 校验流程。"""

    def test_execute_with_valid_input(self, state_store):
        """通过 execute() 执行应通过 Schema 校验。"""
        # 准备上游数据
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 80.0},
        ])
        upstream_state_id = state_store.save("skill2_analyze", upstream)

        # 准备 Skill-3 输入数据（符合 skill3_input.json Schema）
        input_data = {"input_state_id": upstream_state_id}
        input_state_id = state_store.save("skill3_input", input_data)

        skill = _make_skill(state_store)
        output_state_id = skill.execute(input_state_id)

        # 验证输出已存入 State_Store
        output = state_store.load(output_state_id)
        assert "trade_plans" in output
        assert "pipeline_status" in output

    def test_execute_empty_ratings(self, state_store):
        """空评级列表通过 execute() 应输出 no_opportunity。"""
        upstream = _make_upstream_data([])
        upstream_state_id = state_store.save("skill2_analyze", upstream)

        input_data = {"input_state_id": upstream_state_id}
        input_state_id = state_store.save("skill3_input", input_data)

        skill = _make_skill(state_store)
        output_state_id = skill.execute(input_state_id)

        output = state_store.load(output_state_id)
        assert output["pipeline_status"] == "no_opportunity"
        assert output["trade_plans"] == []


# ══════════════════════════════════════════════════════════
# 7. 入场区间计算
# ══════════════════════════════════════════════════════════

class TestEntryRange:
    """测试入场价格区间计算。"""

    def test_high_confidence_narrow_range(self, state_store):
        """高置信度应产生较窄的入场区间。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 9, "signal": "long", "confidence": 95.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        plan = result["trade_plans"][0]
        spread_high = plan["entry_price_upper"] - plan["entry_price_lower"]

        # 低置信度
        upstream2 = _make_upstream_data([
            {"symbol": "ETHUSDT", "rating_score": 7, "signal": "long", "confidence": 20.0},
        ])
        state_id2 = state_store.save("skill2_analyze", upstream2)

        result2 = skill.run({"input_state_id": state_id2})
        plan2 = result2["trade_plans"][0]
        spread_low = plan2["entry_price_upper"] - plan2["entry_price_lower"]

        # 高置信度区间应更窄
        assert spread_high < spread_low

    def test_entry_upper_greater_than_lower(self, state_store):
        """入场区间上限应大于下限。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "rating_score": 8, "signal": "long", "confidence": 50.0},
        ])
        state_id = state_store.save("skill2_analyze", upstream)

        skill = _make_skill(state_store)
        result = skill.run({"input_state_id": state_id})

        plan = result["trade_plans"][0]
        assert plan["entry_price_upper"] > plan["entry_price_lower"]
