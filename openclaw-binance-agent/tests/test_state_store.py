"""
StateStore 单元测试

验证 save/load round-trip、get_latest、StateNotFoundError 等核心行为。
"""

import os
import tempfile
import uuid

import pytest

from src.infra.state_store import StateNotFoundError, StateStore


@pytest.fixture
def store(tmp_path):
    """创建使用临时数据库的 StateStore 实例。"""
    db_path = str(tmp_path / "test_state.db")
    s = StateStore(db_path=db_path)
    yield s
    s.close()


class TestSaveAndLoad:
    """测试 save() 和 load() 的基本存取功能。"""

    def test_save_returns_uuid4(self, store):
        """save() 应返回合法的 UUID v4 字符串。"""
        state_id = store.save("skill1", {"key": "value"})
        # 验证 UUID v4 格式
        parsed = uuid.UUID(state_id, version=4)
        assert str(parsed) == state_id

    def test_round_trip_simple_dict(self, store):
        """存入简单字典后 load 应返回完全一致的数据。"""
        data = {"symbol": "BTCUSDT", "score": 85}
        state_id = store.save("skill1", data)
        loaded = store.load(state_id)
        assert loaded == data

    def test_round_trip_nested_dict(self, store):
        """存入嵌套字典后 load 应返回完全一致的数据。"""
        data = {
            "candidates": [
                {"symbol": "BTCUSDT", "heat_score": 90},
                {"symbol": "ETHUSDT", "heat_score": 75},
            ],
            "metadata": {"source": "test", "count": 2},
        }
        state_id = store.save("skill1", data)
        loaded = store.load(state_id)
        assert loaded == data

    def test_round_trip_empty_dict(self, store):
        """存入空字典后 load 应返回空字典。"""
        state_id = store.save("skill1", {})
        loaded = store.load(state_id)
        assert loaded == {}

    def test_round_trip_unicode_data(self, store):
        """存入包含中文的数据后 load 应正确还原。"""
        data = {"描述": "比特币合约", "状态": "成功"}
        state_id = store.save("skill1", data)
        loaded = store.load(state_id)
        assert loaded == data

    def test_multiple_saves_unique_ids(self, store):
        """多次 save 应生成不同的 state_id。"""
        ids = [store.save("skill1", {"i": i}) for i in range(10)]
        assert len(set(ids)) == 10


class TestLoadNotFound:
    """测试 load() 对不存在的 state_id 的处理。"""

    def test_load_nonexistent_raises(self, store):
        """load 不存在的 state_id 应抛出 StateNotFoundError。"""
        with pytest.raises(StateNotFoundError):
            store.load("nonexistent-id")

    def test_load_random_uuid_raises(self, store):
        """load 随机 UUID 应抛出 StateNotFoundError。"""
        with pytest.raises(StateNotFoundError):
            store.load(str(uuid.uuid4()))


class TestGetLatest:
    """测试 get_latest() 方法。"""

    def test_get_latest_returns_most_recent(self, store):
        """get_latest 应返回最近一次成功存储的数据。"""
        store.save("skill1", {"version": 1})
        store.save("skill1", {"version": 2})
        sid3 = store.save("skill1", {"version": 3})

        state_id, data = store.get_latest("skill1")
        assert state_id == sid3
        assert data == {"version": 3}

    def test_get_latest_filters_by_skill_name(self, store):
        """get_latest 应只返回指定 skill_name 的记录。"""
        store.save("skill1", {"from": "skill1"})
        sid2 = store.save("skill2", {"from": "skill2"})

        state_id, data = store.get_latest("skill2")
        assert state_id == sid2
        assert data == {"from": "skill2"}

    def test_get_latest_no_records_raises(self, store):
        """当指定 Skill 无记录时应抛出 StateNotFoundError。"""
        with pytest.raises(StateNotFoundError):
            store.get_latest("nonexistent_skill")

    def test_get_latest_empty_store_raises(self, store):
        """空数据库调用 get_latest 应抛出 StateNotFoundError。"""
        with pytest.raises(StateNotFoundError):
            store.get_latest("skill1")


class TestDatabaseInit:
    """测试数据库初始化行为。"""

    def test_creates_directory_if_not_exists(self, tmp_path):
        """如果数据库目录不存在，应自动创建。"""
        db_path = str(tmp_path / "subdir" / "nested" / "test.db")
        s = StateStore(db_path=db_path)
        assert os.path.exists(db_path)
        s.close()
