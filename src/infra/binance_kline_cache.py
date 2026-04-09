"""
Binance 合约 K 线本地缓存模块

基于 SQLite 实现 U本位合约 K 线数据的本地持久化缓存。
按 (symbol, interval, open_time) 三元组唯一索引，支持区间查询和批量写入。
供 BinancePublicClient / Skill1Collect / TradingAgents 共用。

与 A 股 KlineCache 设计对齐：
- 缓存优先，仅拉缺失段
- WAL 模式，高并发读写
- 支持 dict 和 list 两种格式输出
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS binance_kline_cache (
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    close_time  INTEGER NOT NULL,
    quote_volume REAL   NOT NULL DEFAULT 0,
    trades      INTEGER NOT NULL DEFAULT 0,
    taker_buy_volume    REAL NOT NULL DEFAULT 0,
    taker_buy_quote_vol REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, interval, open_time)
)
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_bkc_symbol_interval_time
ON binance_kline_cache(symbol, interval, open_time)
"""


class BinanceKlineCache:
    """Binance U本位合约 K 线本地 SQLite 缓存。"""

    def __init__(self, db_path: str = "data/binance_kline_cache.db") -> None:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.commit()

    # ── 查询 ──────────────────────────────────────────────

    def query(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询缓存中指定区间的 K 线数据，按 open_time 正序返回。

        Args:
            symbol: 交易对，如 BTCUSDT
            interval: K 线周期，如 4h, 1d
            start_time: 起始时间戳 (ms)，可选
            end_time: 结束时间戳 (ms)，可选
            limit: 返回条数限制，0 表示不限
        """
        sql = (
            "SELECT open_time, open, high, low, close, volume, "
            "close_time, quote_volume, trades, taker_buy_volume, taker_buy_quote_vol "
            "FROM binance_kline_cache "
            "WHERE symbol = ? AND interval = ?"
        )
        params: list = [symbol, interval]

        if start_time is not None:
            sql += " AND open_time >= ?"
            params.append(start_time)
        if end_time is not None:
            sql += " AND open_time <= ?"
            params.append(end_time)

        sql += " ORDER BY open_time ASC"

        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        cursor = self._conn.execute(sql, params)
        return [
            {
                "open_time": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "close_time": row[6],
                "quote_volume": row[7],
                "trades": row[8],
                "taker_buy_volume": row[9],
                "taker_buy_quote_vol": row[10],
            }
            for row in cursor.fetchall()
        ]

    def query_as_lists(
        self, symbol: str, interval: str, limit: int = 100,
    ) -> List[list]:
        """查询最近 limit 条数据，返回 Binance 原始格式兼容的 list。

        返回: [[open_time, open, high, low, close, volume, close_time, ...], ...]
        与 BinancePublicClient.get_klines() 返回格式一致，方便下游无缝切换。
        """
        cursor = self._conn.execute(
            "SELECT open_time, open, high, low, close, volume, "
            "close_time, quote_volume, trades, taker_buy_volume, taker_buy_quote_vol "
            "FROM binance_kline_cache "
            "WHERE symbol = ? AND interval = ? "
            "ORDER BY open_time DESC LIMIT ?",
            (symbol, interval, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()  # 正序
        # 转为 Binance 原始格式: 值转字符串
        return [
            [
                r[0],           # open_time (int ms)
                str(r[1]),      # open
                str(r[2]),      # high
                str(r[3]),      # low
                str(r[4]),      # close
                str(r[5]),      # volume
                r[6],           # close_time (int ms)
                str(r[7]),      # quote_volume
                r[8],           # trades
                str(r[9]),      # taker_buy_volume
                str(r[10]),     # taker_buy_quote_vol
                "0",            # ignore
            ]
            for r in rows
        ]

    # ── 元数据查询 ────────────────────────────────────────

    def get_row_count(self, symbol: str, interval: str) -> int:
        """返回缓存中该交易对的总行数。"""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM binance_kline_cache "
            "WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
        return cursor.fetchone()[0]

    def get_time_range(
        self, symbol: str, interval: str,
    ) -> Optional[Tuple[int, int]]:
        """返回缓存中该交易对的 (最早open_time, 最晚open_time)，无数据返回 None。"""
        cursor = self._conn.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM binance_kline_cache "
            "WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
        row = cursor.fetchone()
        if row and row[0] is not None and row[1] is not None:
            return (row[0], row[1])
        return None

    def get_cached_symbols(self, interval: str = "4h") -> List[str]:
        """返回缓存中已有数据的所有交易对列表。"""
        cursor = self._conn.execute(
            "SELECT DISTINCT symbol FROM binance_kline_cache WHERE interval = ?",
            (interval,),
        )
        return [row[0] for row in cursor.fetchall()]

    # ── 写入 ──────────────────────────────────────────────

    def upsert_batch(
        self, symbol: str, interval: str, rows: List[Dict[str, Any]],
    ) -> int:
        """批量写入/更新缓存（dict 格式），返回写入行数。"""
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO binance_kline_cache "
            "(symbol, interval, open_time, open, high, low, close, volume, "
            "close_time, quote_volume, trades, taker_buy_volume, taker_buy_quote_vol) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    symbol, interval,
                    r["open_time"], r["open"], r["high"], r["low"], r["close"],
                    r["volume"], r["close_time"],
                    r.get("quote_volume", 0),
                    r.get("trades", 0),
                    r.get("taker_buy_volume", 0),
                    r.get("taker_buy_quote_vol", 0),
                )
                for r in rows
            ],
        )
        self._conn.commit()
        return len(rows)

    def upsert_from_raw(
        self, symbol: str, interval: str, klines: List[list],
    ) -> int:
        """从 Binance 原始 K 线数组批量写入。

        klines 格式: [[open_time, open, high, low, close, volume, close_time, ...], ...]
        """
        if not klines:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO binance_kline_cache "
            "(symbol, interval, open_time, open, high, low, close, volume, "
            "close_time, quote_volume, trades, taker_buy_volume, taker_buy_quote_vol) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    symbol, interval,
                    int(k[0]),
                    float(k[1]),   # open
                    float(k[2]),   # high
                    float(k[3]),   # low
                    float(k[4]),   # close
                    float(k[5]),   # volume
                    int(k[6]),     # close_time
                    float(k[7]) if len(k) > 7 else 0,   # quote_volume
                    int(k[8]) if len(k) > 8 else 0,     # trades
                    float(k[9]) if len(k) > 9 else 0,   # taker_buy_volume
                    float(k[10]) if len(k) > 10 else 0,  # taker_buy_quote_vol
                )
                for k in klines
                if len(k) >= 6 and _safe_float(k[4]) and _safe_float(k[4]) > 0
            ],
        )
        self._conn.commit()
        return len(klines)

    # ── 生命周期 ──────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()


def _safe_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
