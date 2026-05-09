#!/usr/bin/env python3
"""
A股 K 线增量更新脚本

直接用 DB 已有股票列表，定向补最新数据，跳过股票发现。
用法:
    python3 scripts/incremental_astock_update.py --days 5
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))

from src.infra.kline_cache import KlineCache

# ── 腾讯日线接口 ──────────────────────────────────────────

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

def _code_to_tencent(code: str) -> str:
    if code.startswith("6"):
        return f"sh{code}"
    elif code.startswith(("0", "3")):
        return f"sz{code}"
    return f"sz{code}"

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def tx_fetch_klines(code: str, start_date: str, end_date: str, adjust: str = "qfq"):
    """通过腾讯日线 API 拉取 K 线数据。"""
    import requests, json
    tc_symbol = _code_to_tencent(code)
    adj_key_map = {"qfq": "qfqday", "hfq": "hfqday", "none": "day"}
    adj_param = adjust if adjust in ("qfq", "hfq") else ""
    adj_key = adj_key_map.get(adjust, "qfqday")

    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={tc_symbol},day,{start_date},{end_date},640,{adj_param}")
    try:
        r = requests.get(url, timeout=10, headers=_HEADERS)
        if r.status_code != 200:
            return None
        data = json.loads(r.text)
        inner = data.get("data", {})
        stock_data = next(iter(inner.values()), {}) if inner else {}
        klines = stock_data.get(adj_key, stock_data.get("day", []))
        result = []
        for item in klines:
            date = item[0]
            if start_date <= date <= end_date:
                result.append({
                    "date": date,
                    "open": _safe_float(item[1]),
                    "close": _safe_float(item[2]),
                    "high": _safe_float(item[3]),
                    "low": _safe_float(item[4]),
                    "volume": int(_safe_float(item[5], 0)),
                })
        return result if result else []
    except Exception:
        return None

def get_cached_symbols(db_path: str):
    """从 DB 快速获取所有已缓存股票代码。"""
    conn = sqlite3.connect(db_path)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM kline_cache ORDER BY symbol"
    ).fetchall()]
    conn.close()
    return symbols

def main():
    parser = argparse.ArgumentParser(description="A股 K 线增量更新")
    parser.add_argument("--days", type=int, default=5, help="拉取最近 N 个交易日数据")
    parser.add_argument("--db", type=str, default=None, help="缓存 DB 路径")
    parser.add_argument("--interval", type=float, default=0.1, help="API 间隔秒数")
    args = parser.parse_args()

    db_path = args.db or os.path.join(project_root, "data", "kline_cache.db")
    cache = KlineCache(db_path)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days + 10)).strftime("%Y-%m-%d")

    symbols = get_cached_symbols(db_path)
    print(f"📋 共 {len(symbols)} 只股票")
    print(f"📅 范围: {start_date} ~ {end_date}")
    print(f"⏱  间隔: {args.interval}s")
    print("-" * 60)

    success = skipped = failed = total_rows = 0
    t0 = time.time()

    for i, code in enumerate(symbols, 1):
        # 检查是否已有最新数据
        date_range = cache.get_date_range(code, "qfq")
        if date_range is not None:
            cached_start, cached_end = date_range
            if cached_end >= end_date:
                skipped += 1
                if i % 500 == 0:
                    elapsed = time.time() - t0
                    rate = i / elapsed if elapsed > 0 else 0
                    print(f"  {i}/{len(symbols)} | 成功:{success} 跳过:{skipped} 失败:{failed} | {rate:.1f}个/s")
                continue

        rows = tx_fetch_klines(code, start_date, end_date, "qfq")
        if rows is None:
            failed += 1
            if failed <= 5:
                print(f"  [{i}/{len(symbols)}] ❌ {code} 网络错误")
        elif not rows:
            skipped += 1
        else:
            n = cache.upsert_batch(code, "qfq", rows)
            success += 1
            total_rows += n

        if i > 0:
            time.sleep(args.interval)

        if i % 200 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  {i}/{len(symbols)} | 成功:{success} 跳过:{skipped} 失败:{failed} | {rate:.1f}个/s")

    elapsed = time.time() - t0
    print("-" * 60)
    print(f"✅ 完成！耗时 {elapsed:.0f}s | 成功:{success} 跳过:{skipped} 失败:{failed} | 新增 {total_rows} 行")

if __name__ == "__main__":
    main()
