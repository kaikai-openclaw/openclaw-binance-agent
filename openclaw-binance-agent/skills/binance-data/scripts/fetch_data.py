#!/usr/bin/env python3
"""
Binance 合约历史 K 线数据服务 CLI

用法:
    python3 fetch_data.py BTCUSDT --start 2024-01-01 --end 2024-06-30
    python3 fetch_data.py ETHUSDT --start 2024-01-01 --end 2024-06-30 --interval 1d
    python3 fetch_data.py BTCUSDT --start 2024-01-01 --end 2024-06-30 --json
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime

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
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def _date_to_ms(date_str: str) -> int:
    """YYYY-MM-DD → 毫秒时间戳。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    """毫秒时间戳 → YYYY-MM-DD HH:MM。"""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def main():
    parser = argparse.ArgumentParser(description="Binance 合约历史 K 线数据服务")
    parser.add_argument("symbol", type=str, help="交易对（如 BTCUSDT, ETHUSDT）")
    parser.add_argument("--start", type=str, required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--interval", type=str, default="4h",
                        help="K线周期（默认 4h）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "binance_kline_cache.db")

    cache = BinanceKlineCache(db_path)
    client = BinancePublicClient(rate_limiter=RateLimiter(), kline_cache=cache)

    try:
        start_ms = _date_to_ms(args.start)
        end_ms = _date_to_ms(args.end) + 86400000 - 1  # 包含结束日当天

        # 先查缓存
        cached = cache.query(symbol, args.interval, start_ms, end_ms)
        data_source = "local_cache"

        if not cached:
            # 缓存无数据，联网拉取
            data_source = "api"
            raw = client.get_klines_range(symbol, args.interval, start_ms, end_ms)
            cached = cache.query(symbol, args.interval, start_ms, end_ms)
        elif len(cached) < 10:
            # 缓存数据太少，可能不完整，补拉
            data_source = "mixed"
            client.get_klines_range(symbol, args.interval, start_ms, end_ms)
            cached = cache.query(symbol, args.interval, start_ms, end_ms)

        result = {
            "status_code": 200,
            "message": "success",
            "meta_info": {
                "symbol": symbol,
                "interval": args.interval,
                "data_source": data_source,
                "row_count": len(cached),
            },
            "data": cached,
        }

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        # 人类可读输出
        meta = result["meta_info"]
        data = result["data"]

        if not data:
            print(f"⚠️  {symbol} 在 {args.start} ~ {args.end} 无数据")
            sys.exit(1)

        print(f"📊 {symbol}  [{args.start} ~ {args.end}]  "
              f"周期={args.interval}  来源={data_source}")
        print(f"   共 {meta['row_count']} 条记录")
        print("-" * 90)
        print(f"  {'时间':20s} {'开盘':>12s} {'最高':>12s} {'最低':>12s} "
              f"{'收盘':>12s} {'成交量':>14s}")
        print("-" * 90)

        show_rows = data
        truncated = False
        if len(data) > 20:
            show_rows = data[:10] + data[-10:]
            truncated = True

        for i, row in enumerate(show_rows):
            if truncated and i == 10:
                print(f"  {'... 省略中间 ' + str(len(data) - 20) + ' 行 ...':^90s}")
            t = _ms_to_date(row["open_time"])
            print(f"  {t:20s} {row['open']:12.2f} {row['high']:12.2f} "
                  f"{row['low']:12.2f} {row['close']:12.2f} "
                  f"{row['volume']:14.2f}")

        print("-" * 90)

    except Exception as e:
        print(f"❌ 数据获取失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cache.close()


if __name__ == "__main__":
    main()
