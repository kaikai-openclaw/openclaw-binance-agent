"""
State_Store 状态存储模块

基于 SQLite 实现全局状态管理，为每次 Skill 输出生成唯一状态 ID（UUID v4）
并持久化存储完整 JSON 数据快照。各 Skill 间通过状态 ID 引用数据，
避免 LLM 上下文窗口膨胀。
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone


class StateNotFoundError(Exception):
    """当指定的 state_id 在 State_Store 中不存在时抛出。"""
    pass


class StateStore:
    """
    全局状态存储，负责 Skill 输出数据的持久化与检索。

    - save(): 存储 Skill 输出数据，返回 UUID v4 状态 ID
    - load(): 根据状态 ID 检索完整数据快照
    - get_latest(): 获取指定 Skill 最近一次成功输出
    """

    # 建表 SQL
    _CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS state_snapshots (
            state_id    TEXT PRIMARY KEY,
            skill_name  TEXT NOT NULL,
            data        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'success'
        )
    """

    # 索引 SQL：按 skill_name + created_at 降序，加速 get_latest 查询
    _CREATE_INDEX_SQL = """
        CREATE INDEX IF NOT EXISTS idx_skill_created
        ON state_snapshots(skill_name, created_at DESC)
    """

    def __init__(self, db_path: str = "data/state_store.db") -> None:
        """
        初始化 StateStore，创建数据库连接并确保表结构存在。

        参数:
            db_path: SQLite 数据库文件路径，默认为 data/state_store.db
        """
        # 确保数据库目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构和索引。"""
        cursor = self._conn.cursor()
        cursor.execute(self._CREATE_TABLE_SQL)
        cursor.execute(self._CREATE_INDEX_SQL)
        self._conn.commit()

    def save(self, skill_name: str, data: dict) -> str:
        """
        存储 Skill 输出数据，返回 UUID v4 状态 ID。

        - 自动生成 state_id（UUID v4）
        - 将 data 序列化为 JSON 字符串存入 SQLite
        - 记录 skill_name 和 ISO 8601 时间戳

        参数:
            skill_name: Skill 名称（如 "skill1_collect"）
            data: 要存储的数据字典

        返回:
            生成的 UUID v4 状态 ID
        """
        state_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        data_json = json.dumps(data, ensure_ascii=False)

        self._conn.execute(
            "INSERT INTO state_snapshots (state_id, skill_name, data, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (state_id, skill_name, data_json, created_at, "success"),
        )
        self._conn.commit()
        return state_id

    def load(self, state_id: str) -> dict:
        """
        根据状态 ID 检索完整数据快照。

        参数:
            state_id: 要检索的状态 ID

        返回:
            存储的数据字典

        异常:
            StateNotFoundError: 当 state_id 不存在时抛出
        """
        cursor = self._conn.execute(
            "SELECT data FROM state_snapshots WHERE state_id = ?",
            (state_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StateNotFoundError(f"状态 ID 不存在: {state_id}")
        return json.loads(row[0])

    def get_latest(self, skill_name: str) -> tuple[str, dict]:
        """
        获取指定 Skill 最近一次成功输出的 (state_id, data)。

        用于故障恢复场景，按 created_at 降序取第一条 status='success' 的记录。

        参数:
            skill_name: Skill 名称

        返回:
            (state_id, data) 元组

        异常:
            StateNotFoundError: 当该 Skill 无成功记录时抛出
        """
        cursor = self._conn.execute(
            "SELECT state_id, data FROM state_snapshots "
            "WHERE skill_name = ? AND status = 'success' "
            "ORDER BY created_at DESC LIMIT 1",
            (skill_name,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StateNotFoundError(
                f"Skill '{skill_name}' 无成功状态记录"
            )
        return row[0], json.loads(row[1])

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
