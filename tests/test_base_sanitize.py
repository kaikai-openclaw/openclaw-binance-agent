import sys
import os

# 确保能找到 src 模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.skills.base import BaseSkill, SchemaValidationError
from src.infra.state_store import StateStore

class DummyStateStore(StateStore):
    def __init__(self):
        self.data = {}
    def save(self, name, state):
        self.data["test_id"] = state
        return "test_id"
    def load(self, state_id):
        return self.data.get(state_id, {})

class DummySkill(BaseSkill):
    def __init__(self, store):
        in_schema = {
            "type": "object",
            "properties": {
                "price": {"type": "number"},
                "is_active": {"type": "boolean"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"}
                    },
                    "additionalProperties": False
                }
            },
            "additionalProperties": False,
            "required": ["price"]
        }
        out_schema = {
            "type": "object",
            "properties": {
                "result": {"type": "number"}
            },
            "additionalProperties": False
        }
        super().__init__(store, in_schema, out_schema)

    def run(self, input_data):
        return {
            "result": input_data["price"] * 2,
            "unexpected_output": "this should be removed"
        }

def test_sanitize():
    store = DummyStateStore()
    skill = DummySkill(store)
    
    # 模拟带有错误类型和冗余字段的数据
    dirty_input = {
        "price": "10.5",  # 应该是 number
        "is_active": "true", # 应该是 boolean
        "extra_field": "remove me", # additionalProperties: False
        "nested": {
            "count": "5", # 应该是 integer
            "bad_field": "remove me too"
        }
    }
    
    store.data["test_id"] = dirty_input
    
    try:
        # 执行流程：应该会经历清洗 -> 验证 -> 运行 -> 输出清洗 -> 验证
        out_id = skill.execute("test_id")
        out_data = store.load(out_id)
        
        print("Test passed!")
        print("Cleaned Input inside run() price type:", type(out_data["result"])) 
        print("Final Output Data:", out_data)
        
        # 断言清洗结果
        assert "unexpected_output" not in out_data, "Output wasn't sanitized"
        assert out_data["result"] == 21.0, "Result math is wrong (10.5 * 2)"
        
    except SchemaValidationError as e:
        print("Test failed with validation error:", e.errors)
        sys.exit(1)

if __name__ == "__main__":
    test_sanitize()
