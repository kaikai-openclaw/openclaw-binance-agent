#!/usr/bin/env python3
"""
A 股底部放量反转扫描

用法:
    python3 scan_reversal.py --scan                    # 全市场扫描
    python3 scan_reversal.py 600519                    # 指定个股
    python3 scan_reversal.py --scan --min-score 50     # 调整评分门槛
    python3 scan_reversal.py --scan --from-cache       # 纯本地缓存扫描（无网络）
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from datetime import datetime, timezone
from typing import Any, Dict, List
from src.infra.akshare_client import AkshareClient
from src.infra.state_store import StateStore
from src.skills.astock_reversal import AStockReversalSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class CacheOnlyClient:
    """本地缓存优先客户端。

    get_spot_all()  — 从 kline_cache.db 最近一根日线构造行情快照，
                      并尝试通过新浪行情接口批量补充股票名称
    get_klines()    — 直接读 kline_cache.db，不联网
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._spot_cache: List[Dict[str, Any]] = []

    def _fetch_names_sina(self, symbols: List[str]) -> Dict[str, str]:
        """通过新浪行情接口批量拉取股票名称，分批请求，失败静默返回空。"""
        import requests, re
        name_map: Dict[str, str] = {}
        batch_size = 200

        def _exchange(code: str) -> str:
            if code.startswith("6"):
                return "sh"
            return "sz"

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            query = ",".join(f"{_exchange(s)}{s}" for s in batch)
            try:
                r = requests.get(
                    f"http://hq.sinajs.cn/list={query}",
                    timeout=10,
                    headers={"Referer": "http://finance.sina.com.cn"},
                )
                if r.status_code != 200:
                    continue
                for line in r.text.split(";"):
                    m = re.search(r'hq_str_(?:sh|sz)(\d{6})="([^,]+)', line)
                    if m:
                        name_map[m.group(1)] = m.group(2)
            except Exception as e:
                log.debug("[CacheOnlyClient] 名称拉取失败(batch %d): %s", i, e)
        return name_map

    def get_spot_all(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self._spot_cache and not force_refresh:
            return self._spot_cache

        log.info("[CacheOnlyClient] 从本地缓存构造全市场行情快照...")
        cursor = self._conn.execute(
            """
            SELECT k.symbol, k.open, k.high, k.low, k.close, k.volume, k.amount,
                   p.close AS prev_close
            FROM kline_cache k
            LEFT JOIN kline_cache p
              ON p.symbol = k.symbol AND p.adjust = k.adjust
              AND p.date = (
                  SELECT MAX(date) FROM kline_cache
                  WHERE symbol = k.symbol AND adjust = k.adjust AND date < k.date
              )
            WHERE k.adjust = 'qfq'
              AND k.date = (
                  SELECT MAX(date) FROM kline_cache
                  WHERE symbol = k.symbol AND adjust = 'qfq'
              )
              AND k.symbol NOT LIKE 'idx_%'
            """,
        )
        rows = cursor.fetchall()
        results = []
        all_symbols = []
        for r in rows:
            symbol, open_, high, low, close, volume, amount, prev_close = r
            if not close or close <= 0:
                continue
            change_pct = None
            if prev_close and prev_close > 0:
                change_pct = round((close - prev_close) / prev_close * 100, 2)
            amp = round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else None
            results.append({
                "symbol": symbol,
                "name": "",
                "close": close,
                "change_pct": change_pct,
                "volume": volume or 0,
                "amount": amount or 0,
                "high": high or close,
                "low": low or close,
                "open": open_ or close,
                "turnover": None,
                "amplitude_pct": amp,
                "pe": None,
                "total_mv": None,
            })
            all_symbols.append(symbol)

        # 尝试补充股票名称
        log.info("[CacheOnlyClient] 正在拉取股票名称...")
        name_map = self._fetch_names_sina(all_symbols)
        if name_map:
            for item in results:
                item["name"] = name_map.get(item["symbol"], "")
            log.info("[CacheOnlyClient] 名称补充完成: %d/%d", len(name_map), len(results))
        else:
            log.warning("[CacheOnlyClient] 名称拉取失败，名称列将为空")

        log.info("[CacheOnlyClient] 行情快照构造完成: %d 只", len(results))
        self._spot_cache = results
        return results

    def get_klines(self, symbol: str, period: str = "daily", limit: int = 100) -> List[List]:
        cursor = self._conn.execute(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM kline_cache "
            "WHERE symbol = ? AND adjust = 'qfq' "
            "ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()
        # 返回 7 列：[date, open, high, low, close, volume, amount]
        return [list(r) for r in rows]

    def close(self):
        self._conn.close()


def main():
    parser = argparse.ArgumentParser(description="A 股底部放量反转扫描")
    parser.add_argument("symbols", nargs="*", type=str, help="A 股代码")
    parser.add_argument("--scan", action="store_true", help="全市场扫描")
    parser.add_argument("--min-score", type=int, default=40, help="最低评分（默认 40）")
    parser.add_argument("--max", type=int, default=20, help="最大输出数量")
    parser.add_argument("--exclude-kcb", action="store_true", help="排除科创板（688）")
    parser.add_argument("--from-cache", action="store_true", help="纯本地缓存扫描，不联网")
    args = parser.parse_args()

    if not args.symbols and not args.scan:
        parser.error("请指定股票代码或使用 --scan")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))

    if args.from_cache:
        cache_db = os.path.join(db_dir, "kline_cache.db")
        client = CacheOnlyClient(db_path=cache_db)
        print(f"📦 使用本地缓存模式（kline_cache.db，最新数据: 2026-04-30）")
    else:
        client = AkshareClient()

    # 用空 schema 跳过校验（直接调用 run）
    skill = AStockReversalSkill(store, {"type": "object"}, {"type": "object"}, client)

    try:
        input_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "min_score": args.min_score,
            "max_candidates": args.max,
            "exclude_kcb": args.exclude_kcb,
            # 缓存模式下跳过大盘环境检查（无法联网拉指数数据）
            "skip_market_regime": args.from_cache,
        }
        if args.symbols:
            syms = [s.strip().upper().replace("SH","").replace("SZ","").replace("BJ","").replace(".","")
                    for s in args.symbols]
            input_data["target_symbols"] = syms
            print(f"📡 底部放量反转分析: {', '.join(syms)}")
        else:
            print("📡 底部放量反转: 全市场扫描...")

        result = skill.run(input_data)
        candidates = result["candidates"]
        summary = result["filter_summary"]

        print(f"\n   漏斗: {summary['total_tickers']} → {summary['after_base_filter']}"
              f" → {summary['after_reversal_filter']} → {summary['output_count']}")

        if not candidates:
            print("\n⚠️  无符合条件的底部反转候选")
            return

        print(f"\n🔄 底部放量反转候选（{len(candidates)} 只）:")
        print("-" * 95)
        for i, c in enumerate(candidates, 1):
            print(f"  {i:2d}. {c['symbol']} {c.get('name',''):8s} "
                  f"¥{c['close']:8.2f} | "
                  f"评分:{c['reversal_score']:3d} | "
                  f"放量:{c.get('volume_surge_ratio',0):.1f}x "
                  f"企稳:{c.get('price_stable_score',0)} "
                  f"均线:{c.get('ma_turn_score',0)} "
                  f"MACD:{c.get('macd_reversal_score',0)} "
                  f"距底:{c.get('dist_bottom_pct','?')}%")
            details = c.get("signal_details", "")
            if details:
                print(f"      信号: {details}")
        print("-" * 95)

    except Exception as e:
        print(f"❌ 扫描失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()
        if args.from_cache:
            client.close()


if __name__ == "__main__":
    main()
