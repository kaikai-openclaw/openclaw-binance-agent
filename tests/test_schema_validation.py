"""
Schema 校验边界场景单元测试。

覆盖场景：
1. 缺少必填字段
2. 类型错误
3. 值越界
4. 格式错误（date-time、uuid、uri）
5. 模式不匹配（symbol pattern）

需求: 9.4, 9.5
"""

import json
import os
import pytest
from jsonschema import validate, ValidationError, Draft7Validator, FormatChecker

# ── 辅助函数 ──────────────────────────────────────────────

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "schemas")


def _load_schema(name: str) -> dict:
    """加载指定名称的 JSON Schema 文件。"""
    path = os.path.join(SCHEMA_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _assert_invalid(data: dict, schema: dict) -> ValidationError:
    """断言 data 不通过 schema 校验，并返回 ValidationError。"""
    with pytest.raises(ValidationError) as exc_info:
        validate(
            instance=data,
            schema=schema,
            cls=Draft7Validator,
            format_checker=FormatChecker(),
        )
    return exc_info.value


# ── 有效基线数据 ──────────────────────────────────────────

VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"
VALID_DATETIME = "2024-01-15T08:30:00Z"
VALID_URI = "https://example.com/data"


# ══════════════════════════════════════════════════════════
# 1. 缺少必填字段
# ══════════════════════════════════════════════════════════

class TestMissingRequiredFields:
    """测试缺少必填字段时 Schema 校验应拒绝。"""

    def test_skill1_input_missing_trigger_time(self):
        """skill1_input 缺少 trigger_time 应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {"search_keywords": ["BTC", "ETH"]}
        err = _assert_invalid(data, schema)
        assert "trigger_time" in str(err.message)

    def test_skill1_input_missing_search_keywords(self):
        """skill1_input 缺少 search_keywords 应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {"trigger_time": VALID_DATETIME}
        err = _assert_invalid(data, schema)
        assert "search_keywords" in str(err.message)

    def test_skill1_output_missing_state_id(self):
        """skill1_output 缺少 state_id 应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {"candidates": [], "pipeline_run_id": VALID_UUID}
        _assert_invalid(data, schema)

    def test_skill2_input_missing_input_state_id(self):
        """skill2_input 缺少 input_state_id 应被拒绝。"""
        schema = _load_schema("skill2_input.json")
        _assert_invalid({}, schema)

    def test_skill2_output_missing_ratings(self):
        """skill2_output 缺少 ratings 应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {"state_id": VALID_UUID, "filtered_count": 0}
        _assert_invalid(data, schema)

    def test_skill3_output_missing_pipeline_status(self):
        """skill3_output 缺少 pipeline_status 应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {"state_id": VALID_UUID, "trade_plans": []}
        _assert_invalid(data, schema)

    def test_skill4_output_missing_is_paper_mode(self):
        """skill4_output 缺少 is_paper_mode 应被拒绝。"""
        schema = _load_schema("skill4_output.json")
        data = {"state_id": VALID_UUID, "execution_results": []}
        _assert_invalid(data, schema)

    def test_skill1_output_candidate_missing_heat_score(self):
        """skill1_output 候选项缺少 heat_score 应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                # heat_score 缺失
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_skill2_output_rating_missing_signal(self):
        """skill2_output 评级项缺少 signal 应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 8,
                # signal 缺失
                "confidence": 75.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 2. 类型错误
# ══════════════════════════════════════════════════════════

class TestTypeErrors:
    """测试字段类型错误时 Schema 校验应拒绝。"""

    def test_rating_score_string_instead_of_integer(self):
        """rating_score 传入字符串而非整数应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": "high",  # 应为 integer
                "signal": "long",
                "confidence": 80.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_rating_score_float_instead_of_integer(self):
        """rating_score 传入浮点数而非整数应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 7.5,  # 应为 integer，不是 number
                "signal": "long",
                "confidence": 80.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_heat_score_string_instead_of_number(self):
        """heat_score 传入字符串而非数字应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": "hot",  # 应为 number
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_is_paper_mode_string_instead_of_boolean(self):
        """is_paper_mode 传入字符串而非布尔值应被拒绝。"""
        schema = _load_schema("skill4_output.json")
        data = {
            "state_id": VALID_UUID,
            "execution_results": [],
            "is_paper_mode": "true",  # 应为 boolean
        }
        _assert_invalid(data, schema)

    def test_filtered_count_string_instead_of_integer(self):
        """filtered_count 传入字符串而非整数应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [],
            "filtered_count": "zero",  # 应为 integer
        }
        _assert_invalid(data, schema)

    def test_search_keywords_string_instead_of_array(self):
        """search_keywords 传入字符串而非数组应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {
            "trigger_time": VALID_DATETIME,
            "search_keywords": "BTC",  # 应为 array
        }
        _assert_invalid(data, schema)

    def test_candidates_object_instead_of_array(self):
        """candidates 传入对象而非数组应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": {"symbol": "BTCUSDT"},  # 应为 array
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 3. 值越界
# ══════════════════════════════════════════════════════════

class TestValueOutOfRange:
    """测试数值超出允许范围时 Schema 校验应拒绝。"""

    def test_rating_score_below_minimum(self):
        """rating_score 为 0（低于最小值 1）应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 0,  # 最小值为 1
                "signal": "long",
                "confidence": 50.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_rating_score_above_maximum(self):
        """rating_score 为 11（超过最大值 10）应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 11,  # 最大值为 10
                "signal": "short",
                "confidence": 50.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_heat_score_below_minimum(self):
        """heat_score 为 -1（低于最小值 0）应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": -1,  # 最小值为 0
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_heat_score_above_maximum(self):
        """heat_score 为 101（超过最大值 100）应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": 101,  # 最大值为 100
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_confidence_below_minimum(self):
        """confidence 为 -5（低于最小值 0）应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 5,
                "signal": "hold",
                "confidence": -5,  # 最小值为 0
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_confidence_above_maximum(self):
        """confidence 为 150（超过最大值 100）应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 5,
                "signal": "hold",
                "confidence": 150,  # 最大值为 100
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_position_size_pct_zero(self):
        """position_size_pct 为 0（exclusiveMinimum: 0）应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "ETHUSDT",
                "direction": "long",
                "entry_price_upper": 2000.0,
                "entry_price_lower": 1900.0,
                "position_size_pct": 0,  # exclusiveMinimum: 0
                "stop_loss_price": 1850.0,
                "take_profit_price": 2200.0,
                "max_hold_hours": 24.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_position_size_pct_above_maximum(self):
        """position_size_pct 为 25（超过最大值 20）应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "ETHUSDT",
                "direction": "long",
                "entry_price_upper": 2000.0,
                "entry_price_lower": 1900.0,
                "position_size_pct": 25,  # 最大值为 20
                "stop_loss_price": 1850.0,
                "take_profit_price": 2200.0,
                "max_hold_hours": 24.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_entry_price_zero(self):
        """entry_price_upper 为 0（exclusiveMinimum: 0）应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "ETHUSDT",
                "direction": "short",
                "entry_price_upper": 0,  # exclusiveMinimum: 0
                "entry_price_lower": 1900.0,
                "position_size_pct": 10.0,
                "stop_loss_price": 2100.0,
                "take_profit_price": 1700.0,
                "max_hold_hours": 12.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_negative_entry_price(self):
        """entry_price_lower 为负数应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "ETHUSDT",
                "direction": "long",
                "entry_price_upper": 2000.0,
                "entry_price_lower": -100.0,  # 负数
                "position_size_pct": 10.0,
                "stop_loss_price": 1850.0,
                "take_profit_price": 2200.0,
                "max_hold_hours": 24.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_filtered_count_negative(self):
        """filtered_count 为 -1（低于最小值 0）应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [],
            "filtered_count": -1,  # 最小值为 0
        }
        _assert_invalid(data, schema)

    def test_search_keywords_empty_array(self):
        """search_keywords 为空数组（minItems: 1）应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {
            "trigger_time": VALID_DATETIME,
            "search_keywords": [],  # minItems: 1
        }
        _assert_invalid(data, schema)

    def test_win_rate_above_maximum(self):
        """evolution.win_rate 超过 100 应被拒绝。"""
        schema = _load_schema("skill5_output.json")
        data = {
            "state_id": VALID_UUID,
            "account_summary": {
                "total_balance": 10000,
                "available_margin": 5000,
                "unrealized_pnl": 0,
                "daily_realized_pnl": 0,
                "is_paper_mode": False,
            },
            "positions": [],
            "evolution": {
                "win_rate": 110,  # 最大值为 100
                "avg_pnl_ratio": 1.5,
                "trade_count": 10,
                "adjustment_applied": False,
            },
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 4. 格式错误（date-time、uuid、uri）
# ══════════════════════════════════════════════════════════

class TestFormatErrors:
    """测试格式不合法时 Schema 校验应拒绝。"""

    def test_trigger_time_invalid_datetime(self):
        """trigger_time 格式不合法应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {
            "trigger_time": "not-a-datetime",  # 非法 date-time
            "search_keywords": ["BTC"],
        }
        err = _assert_invalid(data, schema)
        assert "date-time" in str(err.message) or "format" in str(err.schema_path)

    def test_trigger_time_partial_date(self):
        """trigger_time 仅有日期部分（缺少时间）应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {
            "trigger_time": "2024-01-15",  # 缺少时间部分
            "search_keywords": ["BTC"],
        }
        _assert_invalid(data, schema)

    def test_state_id_invalid_uuid(self):
        """state_id 非法 UUID 格式应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": "not-a-uuid",  # 非法 UUID
            "candidates": [],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_pipeline_run_id_invalid_uuid(self):
        """pipeline_run_id 非法 UUID 格式应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [],
            "pipeline_run_id": "12345",  # 非法 UUID
        }
        _assert_invalid(data, schema)

    def test_collected_at_invalid_datetime(self):
        """候选项 collected_at 格式不合法应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": 80,
                "source_url": VALID_URI,
                "collected_at": "yesterday",  # 非法 date-time
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_source_url_invalid_uri(self):
        """候选项 source_url 非法 URI 格式应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": 80,
                "source_url": "not a url",  # 非法 URI
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_input_state_id_invalid_uuid(self):
        """skill2_input 的 input_state_id 非法 UUID 应被拒绝。"""
        schema = _load_schema("skill2_input.json")
        data = {"input_state_id": "xyz-not-uuid"}
        _assert_invalid(data, schema)

    def test_executed_at_invalid_datetime(self):
        """skill4_output 执行结果的 executed_at 格式不合法应被拒绝。"""
        schema = _load_schema("skill4_output.json")
        data = {
            "state_id": VALID_UUID,
            "execution_results": [{
                "order_id": "ORD001",
                "symbol": "BTCUSDT",
                "direction": "long",
                "status": "filled",
                "executed_at": "2024/01/15 08:30",  # 非法 date-time
            }],
            "is_paper_mode": False,
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 5. 模式不匹配（symbol pattern）
# ══════════════════════════════════════════════════════════

class TestPatternMismatch:
    """测试 symbol 不符合 ^[A-Z]{2,10}USDT$ 模式时应被拒绝。"""

    def test_symbol_lowercase(self):
        """小写 symbol 应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "btcusdt",  # 小写
                "heat_score": 80,
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_symbol_missing_usdt_suffix(self):
        """symbol 缺少 USDT 后缀应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTC",  # 缺少 USDT
                "rating_score": 8,
                "signal": "long",
                "confidence": 75.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_symbol_single_char_prefix(self):
        """symbol 前缀仅 1 个字符（低于最少 2 个）应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "XUSDT",  # 前缀仅 1 个字符
                "direction": "long",
                "entry_price_upper": 100.0,
                "entry_price_lower": 90.0,
                "position_size_pct": 5.0,
                "stop_loss_price": 85.0,
                "take_profit_price": 120.0,
                "max_hold_hours": 24.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_symbol_too_long_prefix(self):
        """symbol 前缀超过 10 个字符应被拒绝。"""
        schema = _load_schema("skill4_output.json")
        data = {
            "state_id": VALID_UUID,
            "execution_results": [{
                "order_id": "ORD001",
                "symbol": "ABCDEFGHIJKUSDT",  # 前缀 11 个字符
                "direction": "short",
                "status": "filled",
                "executed_at": VALID_DATETIME,
            }],
            "is_paper_mode": False,
        }
        _assert_invalid(data, schema)

    def test_symbol_with_numbers(self):
        """symbol 包含数字应被拒绝（pattern 仅允许大写字母）。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTC123USDT",  # 包含数字
                "heat_score": 50,
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)

    def test_symbol_with_special_chars(self):
        """symbol 包含特殊字符应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTC-USDT",  # 包含连字符
                "rating_score": 7,
                "signal": "long",
                "confidence": 60.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 6. 枚举值不匹配
# ══════════════════════════════════════════════════════════

class TestEnumMismatch:
    """测试枚举值不在允许列表中时应被拒绝。"""

    def test_signal_invalid_value(self):
        """signal 传入非法枚举值应被拒绝。"""
        schema = _load_schema("skill2_output.json")
        data = {
            "state_id": VALID_UUID,
            "ratings": [{
                "symbol": "BTCUSDT",
                "rating_score": 7,
                "signal": "buy",  # 应为 long/short/hold
                "confidence": 60.0,
            }],
            "filtered_count": 0,
        }
        _assert_invalid(data, schema)

    def test_direction_invalid_value(self):
        """direction 传入非法枚举值应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [{
                "symbol": "ETHUSDT",
                "direction": "buy",  # 应为 long/short
                "entry_price_upper": 2000.0,
                "entry_price_lower": 1900.0,
                "position_size_pct": 10.0,
                "stop_loss_price": 1850.0,
                "take_profit_price": 2200.0,
                "max_hold_hours": 24.0,
            }],
            "pipeline_status": "has_trades",
        }
        _assert_invalid(data, schema)

    def test_pipeline_status_invalid_value(self):
        """pipeline_status 传入非法枚举值应被拒绝。"""
        schema = _load_schema("skill3_output.json")
        data = {
            "state_id": VALID_UUID,
            "trade_plans": [],
            "pipeline_status": "pending",  # 应为 has_trades/no_opportunity
        }
        _assert_invalid(data, schema)

    def test_order_status_invalid_value(self):
        """execution_results.status 传入非法枚举值应被拒绝。"""
        schema = _load_schema("skill4_output.json")
        data = {
            "state_id": VALID_UUID,
            "execution_results": [{
                "order_id": "ORD001",
                "symbol": "BTCUSDT",
                "direction": "long",
                "status": "pending",  # 应为 filled/rejected_by_risk/execution_failed/paper_trade
                "executed_at": VALID_DATETIME,
            }],
            "is_paper_mode": False,
        }
        _assert_invalid(data, schema)


# ══════════════════════════════════════════════════════════
# 7. additionalProperties 禁止额外字段
# ══════════════════════════════════════════════════════════

class TestAdditionalProperties:
    """测试传入额外字段时 Schema 校验应拒绝。"""

    def test_skill1_input_extra_field(self):
        """skill1_input 传入额外字段应被拒绝。"""
        schema = _load_schema("skill1_input.json")
        data = {
            "trigger_time": VALID_DATETIME,
            "search_keywords": ["BTC"],
            "extra_field": "not_allowed",  # 额外字段
        }
        _assert_invalid(data, schema)

    def test_skill1_output_candidate_extra_field(self):
        """skill1_output 候选项传入额外字段应被拒绝。"""
        schema = _load_schema("skill1_output.json")
        data = {
            "state_id": VALID_UUID,
            "candidates": [{
                "symbol": "BTCUSDT",
                "heat_score": 80,
                "source_url": VALID_URI,
                "collected_at": VALID_DATETIME,
                "extra": True,  # 额外字段
            }],
            "pipeline_run_id": VALID_UUID,
        }
        _assert_invalid(data, schema)
