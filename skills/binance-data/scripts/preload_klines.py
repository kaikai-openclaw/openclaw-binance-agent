#!/usr/bin/env python3
"""
Binance 合约 K 线批量预加载

将 U本位合约交易对的历史 K 线数据批量拉取并持久化到本地 SQLite 缓存。
后续所有 Skill 调用 get_klines_cached() 时直接命中本地缓存，零网络开销。

数据源：Binance fapi 公开端点（无需 API Key）

用法:
    python3 preload_klines.py                                    # 全市场 4h
    python3 preload_klines.py --interval 1d                      # 日线
    python3 preload_klines.py --start 2024-01-01                 # 自定义起始
    python3 preload_klines.py --symbols BTCUSDT ETHUSDT SOLUSDT  # 指定交易对
    python3 preload_klines.py --skip-existing                    # 断点续传
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List, Tuple

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_kline_cache import BinanceKlineCache
from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def get_usdt_perpetual_symbols(client: BinancePublicClient) -> List[Tuple[str, str]]:
    """获取所有 USDT 永续合约交易对列表。

    返回: [(symbol, contractType), ...] 如 [("BTCUSDT", "PERPETUAL"), ...]
    """
    info = client.get_exchange_info()
    symbols = []
    for s in info.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
        ):
            symbols.append((s["symbol"], s.get("contractType", "")))
    symbols.sort(key=lambda x: x[0])
    return symbols


def _date_to_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)


def main():
    parser = argparse.ArgumentParser(description="Binance 合约 K 线批量预加载")
    parser.add_argument("--symbols", nargs="*", type=str,
                        help="指定交易对（如 BTCUSDT ETHUSDT）")
    parser.add_argument("--start", type=str, default=None,
                        help="开始日期 YYYY-MM-DD（默认 180 天前）")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--interval", type=str, default="4h",
                        help="K线周期（默认 4h）")
    parser.add_argument("--api-interval", type=float, default=0.2,
                        help="API 调用间隔秒数（默认 0.2）")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已有数据的交易对（断点续传）")
    parser.add_argument("--db", type=str, default=None,
                        help="缓存数据库路径")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    start_ms = _date_to_ms(start_date)
    end_ms = _date_to_ms(end_date) + 86400000 - 1

    db_path = args.db or os.path.join(PROJECT_ROOT, "data", "binance_kline_cache.db")
    cache = BinanceKlineCache(db_path)
    client = BinancePublicClient(rate_limiter=RateLimiter(), kline_cache=cache)

    # 获取交易对列表
    if args.symbols:
        stock_list = [(s.strip().upper(), "") for s in args.symbols]
        print(f"📋 指定 {len(stock_list)} 个交易对")
    else:
        print("📋 获取 USDT 永续合约列表...")
        stock_list = get_usdt_perpetual_symbols(client)
        if not stock_list:
            print("❌ 无法获取交易对列表")
            sys.exit(1)
        print(f"   共 {len(stock_list)} 个交易对")

    print(f"📅 范围: {start_date} ~ {end_date}")
    print(f"📊 周期: {args.interval} | 间隔: {args.api_interval}s")
    print(f"💾 缓存: {db_path}")
    if args.skip_existing:
        print("⏭️  断点续传模式")
    print("-" * 60)

    total = len(stock_list)
    success = 0
    skipped = 0
    failed = 0
    total_rows = 0
    t0 = time.time()

    for i, (symbol, _) in enumerate(stock_list, 1):
        if args.skip_existing:
            if cache.get_row_count(symbol, args.interval) > 0:
                skipped += 1
                if i % 50 == 0:
                    _progress(i, total, success, skipped, failed, total_rows, t0)
                continue

        if i > 1:
            time.sleep(args.api_interval)

        try:
            klines = client.get_klines_range(symbol, args.interval, start_ms, end_ms)
            if not klines:
                skipped += 1
                continue

            n = len(klines)
            success += 1
            total_rows += n

            if i % 20 == 0 or i == total:
                _progress(i, total, success, skipped, failed, total_rows, t0, symbol, n)

        except Exception as e:
            failed += 1
            if failed <= 5 or failed % 10 == 0:
                log.error("[%d/%d] ❌ %s 失败: %s", i, total, symbol, e)

    elapsed = time.time() - t0
    print("=" * 60)
    print(f"✅ 完成 | 成功:{success} 跳过:{skipped} 失败:{failed}")
    print(f"   {total_rows:,} 行 | {elapsed/60:.1f} 分钟 | {db_path}")
    if os.path.exists(db_path):
        print(f"   文件: {os.path.getsize(db_path)/1024/1024:.1f} MB")
    cache.close()


def _progress(i, total, ok, skip, fail, rows, t0, label="", n=0):
    el = time.time() - t0
    spd = ok / el * 60 if el > 0 and ok > 0 else 0
    eta = (total - i) / (i / el) / 60 if el > 0 else 0
    p = [f"[{i}/{total}]"]
    if label:
        p.append(f"{label:14s} +{n:5d}")
    p += [f"✓{ok} ⏭{skip} ✗{fail}", f"{rows:,}行", f"{spd:.0f}/分", f"ETA:{eta:.1f}m"]
    print(f"  {' | '.join(p)}")


if __name__ == "__main__":
    main()
