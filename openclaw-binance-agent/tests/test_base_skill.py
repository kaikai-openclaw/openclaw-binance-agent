"""
BaseSkill 基类单元测试。

覆盖场景：
1. 标准执行流程（加载输入→校验→执行→校验输出→存储）
2. 无 input_state_id 时使用空字典
3. 输入 Schema 校验失败抛出 SchemaValidationError
4. 输出 Schema 校验失败抛出 SchemaValidationError
5. run() 未实现时抛出 NotImplementedError
6. 执行日志记录（含 state_id、耗时、成功/失败状态）
7. SchemaValidationError 包含错误详情

需求: 6.6, 9.2, 9.3, 9.4, 9.5, 9.6
"""

import logging
import os
import tempfile

import pytest

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill, SchemaValidationError


# ── 测试用 Schema ──────────────────────────────────────────

SIMPLE_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "value": {"type": "integer", "minimum": 0}
    },
    "required": ["value"],
    "additionalProperties": False,
}

SIMPLE_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "result": {"type": "string"}
    },
    "required": ["result"],
    "additionalProperties": False,
}

# 允许空对象的输入 Schema（用于无 input_state_id 场景）
EMPTY_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


# ── 测试用 Skill 子类 ──────────────────────────────────────

class DoubleSkill(BaseSkill):
    """测试用 Skill：将输入 value 翻倍后返回字符串。"""

    def __init__(self, state_store, input_schema=None, output_schema=None):
        super().__init__(
            state_store,
            input_schema or SIMPLE_INPUT_SCHEMA,
            output_schema or SIMPLE_OUTPUT_SCHEMA,
        )
        self.name = "double_skill"

    def run(self, input_data: dict) -> dict:
        val = input_data.get("value", 0)
        return {"result": str(val * 2)}


class BadOutputSkill(BaseSkill):
    """测试用 Skill：返回不符合 output_schema 的数据。"""

    def __init__(self, state_store):
        super().__init__(state_store, EMPTY_INPUT_SCHEMA, SIMPLE_OUTPUT_SCHEMA)
        self.name = "bad_output_skill"

    def run(self, input_data: dict) -> dict:
        return {"result": 12345}  # 应为 string，实际为 int


class ErrorSkill(BaseSkill):
    """测试用 Skill：执行时抛出异常。"""

    def __init__(self, state_store):
        super().__init__(state_store, EMPTY_INPUT_SCHEMA, SIMPLE_OUTPUT_SCHEMA)
        self.name = "error_skill"

    def run(self, input_data: dict) -> dict:
        raise RuntimeError("模拟业务逻辑错误")


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    """创建临时 StateStore 实例。"""
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


# ══════════════════════════════════════════════════════════
# 1. 标准执行流程
# ══════════════════════════════════════════════════════════

class TestStandardExecution:
    """测试 BaseSkill 标准执行流程。"""

    def test_execute_with_input_state_id(self, state_store):
        """有 input_state_id 时，应从 StateStore 加载数据并执行。"""
        # 先存入输入数据
        input_id = state_store.save("upstream", {"value": 5})

        skill = DoubleSkill(state_store)
        output_id = skill.execute(input_state_id=input_id)

        # 验证输出已存入 StateStore
        output_data = state_store.load(output_id)
        assert output_data == {"result": "10"}

    def test_execute_without_input_state_id(self, state_store):
        """无 input_state_id 时，输入为空字典。"""
        skill = DoubleSkill(
            state_store,
            input_schema=EMPTY_INPUT_SCHEMA,
        )
        output_id = skill.execute(input_state_id=None)

        output_data = state_store.load(output_id)
        assert output_data == {"result": "0"}

    def test_execute_returns_valid_state_id(self, state_store):
        """execute() 返回的 state_id 应为有效的 UUID v4 字符串。"""
        import uuid

        input_id = state_store.save("upstream", {"value": 3})
        skill = DoubleSkill(state_store)
        output_id = skill.execute(input_state_id=input_id)

        # 验证是合法 UUID
        parsed = uuid.UUID(output_id, version=4)
        assert str(parsed) == output_id


# ══════════════════════════════════════════════════════════
# 2. Schema 校验失败
# ══════════════════════════════════════════════════════════

class TestSchemaValidation:
    """测试 Schema 校验失败场景。"""

    def test_input_schema_validation_failure(self, state_store):
        """输入数据不符合 input_schema 时应抛出 SchemaValidationError。"""
        # 存入不合法的输入数据（value 为负数）
        input_id = state_store.save("upstream", {"value": -1})

        skill = DoubleSkill(state_store)
        with pytest.raises(SchemaValidationError) as exc_info:
            skill.execute(input_state_id=input_id)

        assert len(exc_info.value.errors) > 0

    def test_input_missing_required_field(self, state_store):
        """输入数据缺少必填字段时应抛出 SchemaValidationError。"""
        input_id = state_store.save("upstream", {"other": "data"})

        skill = DoubleSkill(state_store)
        with pytest.raises(SchemaValidationError):
            skill.execute(input_state_id=input_id)

    def test_output_schema_validation_failure(self, state_store):
        """输出数据不符合 output_schema 时应抛出 SchemaValidationError。"""
        skill = BadOutputSkill(state_store)
        with pytest.raises(SchemaValidationError) as exc_info:
            skill.execute()

        assert "输出数据" in str(exc_info.value)

    def test_schema_validation_error_contains_details(self, state_store):
        """SchemaValidationError 应包含具体的校验错误信息。"""
        input_id = state_store.save("upstream", {"value": "not_an_int"})

        skill = DoubleSkill(state_store)
        with pytest.raises(SchemaValidationError) as exc_info:
            skill.execute(input_state_id=input_id)

        assert isinstance(exc_info.value.errors, list)
        assert len(exc_info.value.errors) > 0


# ══════════════════════════════════════════════════════════
# 3. run() 未实现
# ══════════════════════════════════════════════════════════

class TestNotImplemented:
    """测试 run() 未实现时的行为。"""

    def test_base_skill_run_raises_not_implemented(self, state_store):
        """直接调用 BaseSkill.run() 应抛出 NotImplementedError。"""
        skill = BaseSkill(state_store, EMPTY_INPUT_SCHEMA, SIMPLE_OUTPUT_SCHEMA)
        with pytest.raises(NotImplementedError):
            skill.run({})

    def test_execute_with_unimplemented_run(self, state_store):
        """execute() 调用未实现的 run() 应抛出 NotImplementedError。"""
        skill = BaseSkill(state_store, EMPTY_INPUT_SCHEMA, SIMPLE_OUTPUT_SCHEMA)
        with pytest.raises(NotImplementedError):
            skill.execute()


# ══════════════════════════════════════════════════════════
# 4. 执行日志记录
# ══════════════════════════════════════════════════════════

class TestExecutionLogging:
    """测试执行前后日志记录。"""

    def test_success_logs_contain_required_fields(self, state_store, caplog):
        """成功执行时日志应包含 state_id、耗时和 success 状态。"""
        input_id = state_store.save("upstream", {"value": 7})
        skill = DoubleSkill(state_store)

        with caplog.at_level(logging.INFO, logger="src.skills.base"):
            output_id = skill.execute(input_state_id=input_id)

        # 检查开始日志
        start_logs = [r for r in caplog.records if "开始执行" in r.message]
        assert len(start_logs) == 1
        assert "input_state_id=" in start_logs[0].message

        # 检查完成日志
        end_logs = [r for r in caplog.records if "执行完成" in r.message]
        assert len(end_logs) == 1
        assert output_id in end_logs[0].message
        assert "耗时=" in end_logs[0].message
        assert "success" in end_logs[0].message

    def test_failure_logs_contain_required_fields(self, state_store, caplog):
        """失败执行时日志应包含耗时和 failed 状态。"""
        skill = ErrorSkill(state_store)

        with caplog.at_level(logging.ERROR, logger="src.skills.base"):
            with pytest.raises(RuntimeError):
                skill.execute()

        error_logs = [r for r in caplog.records if "执行失败" in r.message]
        assert len(error_logs) == 1
        assert "耗时=" in error_logs[0].message
        assert "failed" in error_logs[0].message

    def test_schema_failure_logs(self, state_store, caplog):
        """Schema 校验失败时日志应包含 failed 状态和原因。"""
        skill = BadOutputSkill(state_store)

        with caplog.at_level(logging.ERROR, logger="src.skills.base"):
            with pytest.raises(SchemaValidationError):
                skill.execute()

        error_logs = [r for r in caplog.records if "执行失败" in r.message]
        assert len(error_logs) == 1
        assert "Schema校验失败" in error_logs[0].message


# ══════════════════════════════════════════════════════════
# 5. SchemaValidationError 异常
# ══════════════════════════════════════════════════════════

class TestSchemaValidationErrorException:
    """测试 SchemaValidationError 异常类。"""

    def test_error_message(self):
        """SchemaValidationError 应包含错误消息。"""
        err = SchemaValidationError("测试错误")
        assert str(err) == "测试错误"
        assert err.errors == []

    def test_error_with_details(self):
        """SchemaValidationError 应支持传入错误详情列表。"""
        details = ["字段 A 缺失", "字段 B 类型错误"]
        err = SchemaValidationError("校验失败", errors=details)
        assert err.errors == details

    def test_is_exception(self):
        """SchemaValidationError 应为 Exception 子类。"""
        assert issubclass(SchemaValidationError, Exception)
