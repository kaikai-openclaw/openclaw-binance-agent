#!/usr/bin/env python3
"""
加密货币合约超跌反弹扫描 CLI

支持短期（4h）和长期（1d）两种模式。

用法:
    python3 scan_oversold.py                        # 默认短期模式
    python3 scan_oversold.py --mode short           # 短期超跌（4h）
    python3 scan_oversold.py --mode long            # 长期超跌（1d）
    python3 scan_oversold.py --mode long --symbols BTC,ETH,SOL
    python3 scan_oversold.py --mode short --min-score 30
    python3 scan_oversold.py --json
"""
import argparse
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from datetime import datetime, timezone
from src.infra.binance_kline_cache import BinanceKlineCache
from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.skills.crypto_oversold import ShortTermOversoldSkill, LongTermOversoldSkill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="加密货币合约超跌反弹扫描")
    parser.add_argument("--mode", type=str, default="short",
                        choices=["short", "long"],
                        help="扫描模式：short=短期4h超跌, long=长期1d超跌（默认 short）")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定币种，逗号分隔（如 BTC,ETH,SOL）")
    parser.add_argument("--min-score", type=int, default=25,
                        help="最低超跌评分（默认 25）")
    parser.add_argument("--max-candidates", type=int, default=20,
                        help="最大输出数量（默认 20）")
    parser.add_argument("--json", action="store_true",
                        help="输出原始 JSON")
    args = parser.parse_args()

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)

    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    cache = BinanceKlineCache(os.path.join(db_dir, "binance_kline_cache.db"))
    client = BinancePublicClient(rate_limiter=RateLimiter(), kline_cache=cache)

    in_schema = load_schema("crypto_oversold_input.json")
    out_schema = load_schema("crypto_oversold_output.json")

    if args.mode == "long":
        skill = LongTermOversoldSkill(store, in_schema, out_schema, client)
        mode_label = "长期超跌（1d 日线）"
    else:
        skill = ShortTermOversoldSkill(store, in_schema, out_schema, client)
        mode_label = "短期超跌（4h）"

    try:
        input_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "min_oversold_score": args.min_score,
            "max_candidates": args.max_candidates,
        }
        if args.symbols:
            input_data["target_symbols"] = [
                s.strip().upper() for s in args.symbols.split(",") if s.strip()
            ]

        if not args.json:
            print(f"📡 {mode_label} — 正在扫描超跌反弹候选...")
        result = skill.run(input_data)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        summary = result["filter_summary"]
        print(f"\n📊 筛选漏斗:")
        print(f"   全部交易对: {summary['total_tickers']}")
        print(f"   基础过滤后: {summary['after_base_filter']}")
        print(f"   超跌信号后: {summary['after_oversold_filter']}")
        print(f"   最终输出:   {summary['output_count']}")

        candidates = result["candidates"]
        if not candidates:
            print("\n⚠️  当前市场无符合条件的超跌反弹候选")
            return

        print(f"\n🎯 {mode_label} 候选 ({len(candidates)} 个):\n")
        for i, c in enumerate(candidates, 1):
            rsi_s = f"{c['rsi']:.1f}" if c.get("rsi") is not None else "N/A"
            bias_s = f"{c['bias_20']:.1f}%" if c.get("bias_20") is not None else "N/A"
            fr_s = f"{c['funding_rate']:.3f}%" if c.get("funding_rate") is not None else "N/A"
            dd_s = f"{c['distance_from_high_pct']:.1f}%" if c.get("distance_from_high_pct") is not None else "N/A"
            drop_s = f"{c['drop_pct']:.1f}%" if c.get("drop_pct") is not None else "N/A"

            print(f"  {i:2d}. {c['symbol']:14s} 评分:{c['oversold_score']:3d}/100")
            print(f"      价格: {c['close']:.4g} | 24h: {c['price_change_pct']:+.2f}% | "
                  f"成交额: {c['quote_volume_24h']:,.0f} USDT")
            print(f"      RSI: {rsi_s} | BIAS: {bias_s} | "
                  f"连跌: {c['consecutive_down']}根 | 累跌: {drop_s}")
            print(f"      费率: {fr_s} | 距高点: {dd_s} | "
                  f"BOLL破: {'✓' if c['below_boll_lower'] else '✗'} | "
                  f"MACD背离: {'✓' if c['macd_divergence'] else '✗'}")
            print(f"      信号: {c['signal_details']}")
            print()

    except Exception as e:
        print(f"❌ 扫描失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()
        cache.close()


if __name__ == "__main__":
    main()
