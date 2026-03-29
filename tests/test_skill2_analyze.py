"""
Skill-2 深度分析与评级 单元测试。

覆盖场景：
1. TradingAgentsModule 超时控制（30 秒）
2. TradingAgentsModule 正常分析
3. TradingAgentsModule 错误透传
4. Skill2Analyze 正常流程：读取候选 → 分析 → 过滤 → 输出
5. 评级过滤阈值（默认 6 分）
6. 分析超时时跳过该币种
7. 分析错误时跳过该币种，继续处理剩余
8. 空候选列表
9. 无效分析结果被跳过
10. 自定义阈值

需求: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""

import json
import os
import time
import uuid
from unittest.mock import MagicMock

import pytest

from src.infra.state_store import StateStore
from src.skills.skill2_analyze import (
    ANALYSIS_TIMEOUT,
    DEFAULT_RATING_THRESHOLD,
    Skill2Analyze,
    TradingAgentsModule,
)


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill2_input.json")
OUTPUT_SCHEMA = _load_schema("skill2_output.json")


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


def _make_upstream_data(candidates):
    """构造 Skill-1 输出数据并存入 State_Store，返回 state_id。"""
    return {
        "state_id": str(uuid.uuid4()),
        "candidates": candidates,
        "pipeline_run_id": str(uuid.uuid4()),
    }


def _make_skill(state_store, analyzer_fn, rating_threshold=DEFAULT_RATING_THRESHOLD):
    """创建 Skill2Analyze 实例的辅助函数。"""
    trading_agents = TradingAgentsModule(analyzer=analyzer_fn)
    return Skill2Analyze(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        trading_agents=trading_agents,
        rating_threshold=rating_threshold,
    )


# ══════════════════════════════════════════════════════════
# 1. TradingAgentsModule 单元测试
# ══════════════════════════════════════════════════════════

class TestTradingAgentsModule:
    """测试 TradingAgentsModule 封装。"""

    def test_normal_analysis(self):
        """正常分析应返回 analyzer 的结果。"""
        analyzer = MagicMock(return_value={
            "rating_score": 8,
            "signal": "long",
            "confidence": 85.0,
        })
        module = TradingAgentsModule(analyzer=analyzer)

        result = module.analyze("BTCUSDT", {"symbol": "BTCUSDT"})

        assert result["rating_score"] == 8
        assert result["signal"] == "long"
        assert result["confidence"] == 85.0
        analyzer.assert_called_once_with("BTCUSDT", {"symbol": "BTCUSDT"})

    def test_timeout_raises_timeout_error(self):
        """分析超时应抛出 TimeoutError。"""
        def slow_analyzer(symbol, data):
            time.sleep(ANALYSIS_TIMEOUT + 5)
            return {}

        module = TradingAgentsModule(analyzer=slow_analyzer)

        # 使用较短超时来加速测试（通过 monkey-patch）
        import src.skills.skill2_analyze as mod
        original_timeout = mod.ANALYSIS_TIMEOUT
        mod.ANALYSIS_TIMEOUT = 1  # 1 秒超时
        try:
            module_fast = TradingAgentsModule(analyzer=slow_analyzer)
            with pytest.raises(TimeoutError, match="分析超时"):
                module_fast.analyze("BTCUSDT", {})
        finally:
            mod.ANALYSIS_TIMEOUT = original_timeout

    def test_analyzer_error_propagates(self):
        """analyzer 内部错误应透传。"""
        analyzer = MagicMock(side_effect=RuntimeError("API 错误"))
        module = TradingAgentsModule(analyzer=analyzer)

        with pytest.raises(RuntimeError, match="API 错误"):
            module.analyze("BTCUSDT", {})


# ══════════════════════════════════════════════════════════
# 2. Skill2Analyze 正常执行流程
# ══════════════════════════════════════════════════════════

class TestNormalExecution:
    """测试正常执行流程。"""

    def test_basic_analyze(self, state_store):
        """正常分析应输出有效评级列表。"""
        # 准备上游数据
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "ETHUSDT", "heat_score": 72.5,
             "source_url": "https://example.com/eth",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(side_effect=[
            {"rating_score": 8, "signal": "long", "confidence": 85.0},
            {"rating_score": 7, "signal": "short", "confidence": 60.0},
        ])

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert len(result["ratings"]) == 2
        assert result["ratings"][0]["symbol"] == "BTCUSDT"
        assert result["ratings"][0]["rating_score"] == 8
        assert result["ratings"][0]["signal"] == "long"
        assert result["ratings"][1]["symbol"] == "ETHUSDT"
        assert result["filtered_count"] == 0

    def test_state_id_is_uuid(self, state_store):
        """输出的 state_id 应为有效 UUID。"""
        upstream = _make_upstream_data([])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock()
        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        uuid.UUID(result["state_id"], version=4)

    def test_skill_name(self, state_store):
        """Skill 名称应为 skill2_analyze。"""
        analyzer = MagicMock()
        skill = _make_skill(state_store, analyzer)
        assert skill.name == "skill2_analyze"


# ══════════════════════════════════════════════════════════
# 3. 评级过滤
# ══════════════════════════════════════════════════════════

class TestRatingFilter:
    """测试评级过滤逻辑。"""

    def test_filter_below_threshold(self, state_store):
        """评级分低于阈值的币种应被过滤。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "ETHUSDT", "heat_score": 72.5,
             "source_url": "https://example.com/eth",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "SOLUSDT", "heat_score": 60.0,
             "source_url": "https://example.com/sol",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(side_effect=[
            {"rating_score": 8, "signal": "long", "confidence": 85.0},
            {"rating_score": 5, "signal": "hold", "confidence": 40.0},  # 低于阈值
            {"rating_score": 3, "signal": "short", "confidence": 20.0},  # 低于阈值
        ])

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert len(result["ratings"]) == 1
        assert result["ratings"][0]["symbol"] == "BTCUSDT"
        assert result["filtered_count"] == 2

    def test_threshold_boundary_included(self, state_store):
        """评级分等于阈值的币种应被保留。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 6, "signal": "hold", "confidence": 50.0,
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert len(result["ratings"]) == 1
        assert result["filtered_count"] == 0

    def test_custom_threshold(self, state_store):
        """自定义阈值应生效。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 7, "signal": "long", "confidence": 70.0,
        })

        # 阈值设为 8，rating_score=7 应被过滤
        skill = _make_skill(state_store, analyzer, rating_threshold=8)
        result = skill.run({"input_state_id": state_id})

        assert len(result["ratings"]) == 0
        assert result["filtered_count"] == 1

    def test_all_filtered(self, state_store):
        """所有币种都低于阈值时，ratings 应为空列表。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "ETHUSDT", "heat_score": 72.5,
             "source_url": "https://example.com/eth",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(side_effect=[
            {"rating_score": 3, "signal": "hold", "confidence": 20.0},
            {"rating_score": 4, "signal": "hold", "confidence": 30.0},
        ])

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
        assert result["filtered_count"] == 2


# ══════════════════════════════════════════════════════════
# 4. 错误处理
# ══════════════════════════════════════════════════════════

class TestErrorHandling:
    """测试错误处理逻辑。"""

    def test_timeout_skips_symbol(self, state_store):
        """分析超时时应跳过该币种，继续处理剩余。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "ETHUSDT", "heat_score": 72.5,
             "source_url": "https://example.com/eth",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        # 直接注入一个会抛 TimeoutError 的 TradingAgentsModule mock
        trading_agents_mock = MagicMock()
        trading_agents_mock.analyze.side_effect = [
            TimeoutError("BTCUSDT 分析超时"),
            {"rating_score": 7, "signal": "long", "confidence": 70.0},
        ]

        skill = Skill2Analyze(
            state_store=state_store,
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            trading_agents=trading_agents_mock,
        )
        result = skill.run({"input_state_id": state_id})

        # BTCUSDT 超时被跳过，只有 ETHUSDT
        assert len(result["ratings"]) == 1
        assert result["ratings"][0]["symbol"] == "ETHUSDT"

    def test_error_skips_symbol(self, state_store):
        """分析错误时应跳过该币种，继续处理剩余。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
            {"symbol": "ETHUSDT", "heat_score": 72.5,
             "source_url": "https://example.com/eth",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        trading_agents_mock = MagicMock()
        trading_agents_mock.analyze.side_effect = [
            RuntimeError("API 错误"),
            {"rating_score": 8, "signal": "short", "confidence": 90.0},
        ]

        skill = Skill2Analyze(
            state_store=state_store,
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            trading_agents=trading_agents_mock,
        )
        result = skill.run({"input_state_id": state_id})

        assert len(result["ratings"]) == 1
        assert result["ratings"][0]["symbol"] == "ETHUSDT"

    def test_all_fail_returns_empty(self, state_store):
        """所有币种分析都失败时，ratings 应为空。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        trading_agents_mock = MagicMock()
        trading_agents_mock.analyze.side_effect = RuntimeError("全部失败")

        skill = Skill2Analyze(
            state_store=state_store,
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            trading_agents=trading_agents_mock,
        )
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
        assert result["filtered_count"] == 0


# ══════════════════════════════════════════════════════════
# 5. 边界场景
# ══════════════════════════════════════════════════════════

class TestEdgeCases:
    """测试边界场景。"""

    def test_empty_candidates(self, state_store):
        """候选列表为空时，ratings 应为空。"""
        upstream = _make_upstream_data([])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock()
        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
        assert result["filtered_count"] == 0
        analyzer.assert_not_called()

    def test_candidate_without_symbol_skipped(self, state_store):
        """候选记录缺少 symbol 时应跳过。"""
        upstream = _make_upstream_data([
            {"heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock()
        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
        analyzer.assert_not_called()

    def test_invalid_rating_score_skipped(self, state_store):
        """rating_score 超出 [1,10] 范围时应跳过。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 15, "signal": "long", "confidence": 85.0,
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []

    def test_invalid_signal_skipped(self, state_store):
        """无效 signal 值应跳过。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 8, "signal": "invalid_signal", "confidence": 85.0,
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []

    def test_missing_fields_in_result_skipped(self, state_store):
        """分析结果缺少必要字段时应跳过。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        # 缺少 confidence
        analyzer = MagicMock(return_value={
            "rating_score": 8, "signal": "long",
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []

    def test_confidence_clamped(self, state_store):
        """confidence 超出 [0, 100] 范围时应被裁剪。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 8, "signal": "long", "confidence": 150.0,
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"][0]["confidence"] == 100.0

    def test_upstream_no_candidates_key(self, state_store):
        """上游数据缺少 candidates 键时应返回空列表。"""
        upstream = {"state_id": str(uuid.uuid4()), "pipeline_run_id": str(uuid.uuid4())}
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock()
        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
        assert result["filtered_count"] == 0

    def test_float_rating_score_skipped(self, state_store):
        """rating_score 为浮点数时应跳过（需要整数）。"""
        upstream = _make_upstream_data([
            {"symbol": "BTCUSDT", "heat_score": 85.0,
             "source_url": "https://example.com/btc",
             "collected_at": "2025-01-01T00:00:00Z"},
        ])
        state_id = state_store.save("skill1_collect", upstream)

        analyzer = MagicMock(return_value={
            "rating_score": 7.5, "signal": "long", "confidence": 85.0,
        })

        skill = _make_skill(state_store, analyzer)
        result = skill.run({"input_state_id": state_id})

        assert result["ratings"] == []
