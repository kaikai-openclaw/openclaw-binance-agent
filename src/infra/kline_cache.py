"""
A股 K 线本地缓存模块

基于 SQLite 实现 K 线数据的本地持久化缓存。
按 (symbol, adjust, date) 三元组唯一索引，支持区间查询和批量写入。
供 AkshareClient 和 SkillDataProvider 共用。
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_CACHE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS kline_cache (
    symbol      TEXT NOT NULL,
    adjust      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      INTEGER NOT NULL,
    amount      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, adjust, date)
)
"""
_CACHE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_kline_symbol_adjust_date
ON kline_cache(symbol, adjust, date)
"""


class KlineCache:
    """A 股 K 线本地 SQLite 缓存。"""

    def __init__(self, db_path: str = "data/kline_cache.db") -> None:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CACHE_CREATE_SQL)
        self._conn.execute(_CACHE_INDEX_SQL)
        self._conn.commit()

    def query(
        self, symbol: str, adjust: str, start_date: str, end_date: str,
    ) -> List[Dict[str, Any]]:
        """查询缓存中指定区间的 K 线数据，按日期正序返回。"""
        cursor = self._conn.execute(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM kline_cache "
            "WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC",
            (symbol, adjust, start_date, end_date),
        )
        return [
            {
                "date": row[0], "open": row[1], "high": row[2],
                "low": row[3], "close": row[4],
                "volume": int(row[5]), "amount": row[6],
            }
            for row in cursor.fetchall()
        ]

    def query_as_rows(
        self, symbol: str, adjust: str, limit: int,
    ) -> List[List]:
        """查询最近 limit 条数据，返回 Skill-1A/1B 兼容的行格式
        [[date, open, high, low, close, volume], ...]。"""
        cursor = self._conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM kline_cache "
            "WHERE symbol = ? AND adjust = ? "
            "ORDER BY date DESC LIMIT ?",
            (symbol, adjust, limit),
        )
        rows = cursor.fetchall()
        # 反转为正序
        rows.reverse()
        return [list(r) for r in rows]

    def query_last_dicts(
        self, symbol: str, adjust: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """查询最近 limit 条数据（含 amount 字段），正序返回 dict 列表。

        用于实时行情 spot 在非交易时段（盘前/休市）的 amount 字段补齐。
        """
        cursor = self._conn.execute(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM kline_cache "
            "WHERE symbol = ? AND adjust = ? "
            "ORDER BY date DESC LIMIT ?",
            (symbol, adjust, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()
        return [
            {
                "date": r[0], "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": int(r[5]), "amount": r[6],
            }
            for r in rows
        ]

    def get_row_count(self, symbol: str, adjust: str) -> int:
        """返回缓存中该股票的总行数。"""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM kline_cache WHERE symbol = ? AND adjust = ?",
            (symbol, adjust),
        )
        return cursor.fetchone()[0]

    def get_date_range(
        self, symbol: str, adjust: str,
    ) -> Optional[Tuple[str, str]]:
        """返回缓存中该股票的 (最早日期, 最晚日期)，无数据返回 None。"""
        cursor = self._conn.execute(
            "SELECT MIN(date), MAX(date) FROM kline_cache "
            "WHERE symbol = ? AND adjust = ?",
            (symbol, adjust),
        )
        row = cursor.fetchone()
        if row and row[0] and row[1]:
            return (row[0], row[1])
        return None

    def upsert_batch(
        self, symbol: str, adjust: str, rows: List[Dict[str, Any]],
    ) -> int:
        """批量写入/更新缓存，返回写入行数。"""
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO kline_cache "
            "(symbol, adjust, date, open, high, low, close, volume, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    symbol, adjust,
                    r["date"], r["open"], r["high"], r["low"], r["close"],
                    r["volume"], r.get("amount", 0),
                )
                for r in rows
            ],
        )
        self._conn.commit()
        return len(rows)

    def upsert_from_list_rows(
        self, symbol: str, adjust: str, rows: List[List],
    ) -> int:
        """从 [[date, open, high, low, close, volume, amount?], ...] 格式批量写入。

        第 7 列 amount 可选，兼容老调用：只传 6 列时 amount 会被写为 0。
        """
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO kline_cache "
            "(symbol, adjust, date, open, high, low, close, volume, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    symbol, adjust, r[0], r[1], r[2], r[3], r[4], r[5],
                    float(r[6]) if len(r) >= 7 else 0.0,
                )
                for r in rows if len(r) >= 6
            ],
        )
        self._conn.commit()
        return len(rows)

    def close(self) -> None:
        self._conn.close()
