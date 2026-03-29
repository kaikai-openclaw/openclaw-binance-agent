"""
Skill-1 信息收集与候选筛选 单元测试。

覆盖场景：
1. 正常流程：searcher + fetcher 返回有效数据
2. 防幻觉过滤：缺少 symbol / heat_score 的记录被过滤
3. 重试逻辑：失败后重试，最多 3 次
4. 重试耗尽后抛出异常
5. fetcher 返回 None 时跳过
6. 空搜索结果
7. source_url 和 collected_at 标注
8. heat_score 范围裁剪

需求: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

import json
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.infra.state_store import StateStore
from src.skills.skill1_collect import (
    MAX_RETRIES,
    Skill1Collect,
)


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill1_input.json")
OUTPUT_SCHEMA = _load_schema("skill1_output.json")


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


def _make_skill(state_store, searcher, fetcher):
    """创建 Skill1Collect 实例的辅助函数。"""
    return Skill1Collect(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        searcher=searcher,
        fetcher=fetcher,
    )


# ══════════════════════════════════════════════════════════
# 1. 正常执行流程
# ══════════════════════════════════════════════════════════

class TestNormalExecution:
    """测试正常执行流程。"""

    def test_basic_collect(self, state_store):
        """searcher 和 fetcher 正常返回时，应输出有效候选列表。"""
        searcher = MagicMock(return_value=[
            {"url": "https://example.com/btc"},
            {"url": "https://example.com/eth"},
        ])
        fetcher = MagicMock(side_effect=[
            {"symbol": "BTCUSDT", "heat_score": 85.0},
            {"symbol": "ETHUSDT", "heat_score": 72.5},
        ])

        skill = _make_skill(state_store, searcher, fetcher)
        input_data = {
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["crypto", "热点"],
        }

        result = skill.run(input_data)

        assert len(result["candidates"]) == 2
        assert result["candidates"][0]["symbol"] == "BTCUSDT"
        assert result["candidates"][0]["heat_score"] == 85.0
        assert result["candidates"][0]["source_url"] == "https://example.com/btc"
        assert result["candidates"][1]["symbol"] == "ETHUSDT"

    def test_pipeline_run_id_is_uuid(self, state_store):
        """pipeline_run_id 应为有效 UUID。"""
        import uuid

        searcher = MagicMock(return_value=[])
        fetcher = MagicMock()

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        uuid.UUID(result["pipeline_run_id"], version=4)


# ══════════════════════════════════════════════════════════
# 2. 防幻觉过滤
# ══════════════════════════════════════════════════════════

class TestAntiHallucination:
    """测试防幻觉约束。"""

    def test_missing_symbol_filtered(self, state_store):
        """缺少 symbol 的记录应被过滤。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"heat_score": 50.0})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 0

    def test_empty_symbol_filtered(self, state_store):
        """空字符串 symbol 应被过滤。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"symbol": "", "heat_score": 50.0})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 0

    def test_missing_heat_score_filtered(self, state_store):
        """缺少 heat_score 的记录应被过滤。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT"})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 0

    def test_non_numeric_heat_score_filtered(self, state_store):
        """非数值 heat_score 应被过滤。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT", "heat_score": "high"})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 0

    def test_heat_score_clamped_to_range(self, state_store):
        """heat_score 超出 [0, 100] 范围时应被裁剪。"""
        searcher = MagicMock(return_value=[
            {"url": "https://example.com/a"},
            {"url": "https://example.com/b"},
        ])
        fetcher = MagicMock(side_effect=[
            {"symbol": "BTCUSDT", "heat_score": 150.0},
            {"symbol": "ETHUSDT", "heat_score": -10.0},
        ])

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert result["candidates"][0]["heat_score"] == 100.0
        assert result["candidates"][1]["heat_score"] == 0.0


# ══════════════════════════════════════════════════════════
# 3. 来源标注
# ══════════════════════════════════════════════════════════

class TestSourceAnnotation:
    """测试 source_url 和 collected_at 标注。"""

    def test_source_url_annotated(self, state_store):
        """每条候选记录应包含正确的 source_url。"""
        searcher = MagicMock(return_value=[
            {"url": "https://example.com/data"},
        ])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT", "heat_score": 80.0})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert result["candidates"][0]["source_url"] == "https://example.com/data"

    def test_collected_at_is_iso8601(self, state_store):
        """collected_at 应为有效的 ISO 8601 时间戳。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT", "heat_score": 80.0})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        collected_at = result["candidates"][0]["collected_at"]
        # 应能被 datetime.fromisoformat 解析
        dt = datetime.fromisoformat(collected_at)
        assert dt is not None


# ══════════════════════════════════════════════════════════
# 4. 重试逻辑
# ══════════════════════════════════════════════════════════

class TestRetryLogic:
    """测试重试逻辑。"""

    @patch("src.skills.skill1_collect.time.sleep")
    def test_searcher_retry_on_failure(self, mock_sleep, state_store):
        """searcher 失败后应重试，成功后返回结果。"""
        searcher = MagicMock(side_effect=[
            RuntimeError("网络错误"),
            [{"url": "https://example.com/a"}],
        ])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT", "heat_score": 80.0})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 1
        assert searcher.call_count == 2
        mock_sleep.assert_called_once_with(60)

    @patch("src.skills.skill1_collect.time.sleep")
    def test_fetcher_retry_on_failure(self, mock_sleep, state_store):
        """fetcher 失败后应重试，成功后返回结果。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(side_effect=[
            RuntimeError("超时"),
            {"symbol": "BTCUSDT", "heat_score": 80.0},
        ])

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 1
        assert fetcher.call_count == 2

    @patch("src.skills.skill1_collect.time.sleep")
    def test_searcher_max_retries_exceeded(self, mock_sleep, state_store):
        """searcher 重试 3 次后仍失败应抛出异常。"""
        searcher = MagicMock(side_effect=RuntimeError("持续失败"))
        fetcher = MagicMock()

        skill = _make_skill(state_store, searcher, fetcher)

        with pytest.raises(RuntimeError, match="持续失败"):
            skill.run({
                "trigger_time": "2025-01-01T00:00:00Z",
                "search_keywords": ["test"],
            })

        assert searcher.call_count == MAX_RETRIES
        # 重试 2 次（第 1 次失败后等待，第 2 次失败后等待，第 3 次失败后不等待）
        assert mock_sleep.call_count == MAX_RETRIES - 1


# ══════════════════════════════════════════════════════════
# 5. 边界场景
# ══════════════════════════════════════════════════════════

class TestEdgeCases:
    """测试边界场景。"""

    def test_empty_search_results(self, state_store):
        """搜索结果为空时，候选列表应为空。"""
        searcher = MagicMock(return_value=[])
        fetcher = MagicMock()

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert result["candidates"] == []
        fetcher.assert_not_called()

    def test_fetcher_returns_none(self, state_store):
        """fetcher 返回 None 时应跳过该记录。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value=None)

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 0

    def test_search_result_without_url_skipped(self, state_store):
        """搜索结果缺少 url 字段时应跳过。"""
        searcher = MagicMock(return_value=[{"title": "no url"}])
        fetcher = MagicMock()

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert result["candidates"] == []
        fetcher.assert_not_called()

    def test_skill_name_is_correct(self, state_store):
        """Skill 名称应为 skill1_collect。"""
        searcher = MagicMock()
        fetcher = MagicMock()
        skill = _make_skill(state_store, searcher, fetcher)
        assert skill.name == "skill1_collect"

    def test_integer_heat_score_accepted(self, state_store):
        """整数类型的 heat_score 应被接受。"""
        searcher = MagicMock(return_value=[{"url": "https://example.com/a"}])
        fetcher = MagicMock(return_value={"symbol": "BTCUSDT", "heat_score": 75})

        skill = _make_skill(state_store, searcher, fetcher)
        result = skill.run({
            "trigger_time": "2025-01-01T00:00:00Z",
            "search_keywords": ["test"],
        })

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["heat_score"] == 75.0
