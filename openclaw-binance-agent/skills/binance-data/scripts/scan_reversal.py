#!/usr/bin/env python3
"""
加密货币合约底部放量反转扫描 CLI

与超跌反弹（scan_oversold.py）的区别：
  超跌反弹 = "接飞刀"，在暴跌过程中抄底
  底部反转 = "确认转向"，等底部构筑完成后入场

支持短期（4h）和长期（1d）两种模式。

用法:
    python3 scan_reversal.py                        # 默认短期模式
    python3 scan_reversal.py --mode short           # 短期反转（4h）
    python3 scan_reversal.py --mode long            # 长期反转（1d）
    python3 scan_reversal.py --mode long --symbols BTC,ETH,SOL
    python3 scan_reversal.py --mode short --min-score 25
    python3 scan_reversal.py --json
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
from src.skills.crypto_reversal import ShortTermReversalSkill, LongTermReversalSkill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="加密货币合约底部放量反转扫描")
    parser.add_argument("--mode", type=str, default="short",
                        choices=["short", "long"],
                        help="扫描模式：short=短期4h反转, long=长期1d反转（默认 short）")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定币种，逗号分隔（如 BTC,ETH,SOL）")
    parser.add_argument("--min-score", type=int, default=None,
                        help="最低反转评分（默认使用各周期策略内置值，4h=55）")
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

    in_schema = load_schema("crypto_reversal_input.json")
    out_schema = load_schema("crypto_reversal_output.json")

    if args.mode == "long":
        skill = LongTermReversalSkill(store, in_schema, out_schema, client)
        mode_label = "长期反转（1d 日线）"
    else:
        skill = ShortTermReversalSkill(store, in_schema, out_schema, client)
        mode_label = "短期反转（4h）"

    try:
        input_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "max_candidates": args.max_candidates,
        }
        if args.min_score is not None:
            input_data["min_reversal_score"] = args.min_score
        if args.symbols:
            input_data["target_symbols"] = [
                s.strip().upper() for s in args.symbols.split(",") if s.strip()
            ]

        print(f"📡 {mode_label} — 正在扫描底部反转候选...")
        result = skill.run(input_data)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        summary = result["filter_summary"]
        print(f"\n📊 筛选漏斗:")
        print(f"   全部交易对: {summary['total_tickers']}")
        print(f"   基础过滤后: {summary['after_base_filter']}")
        print(f"   反转信号后: {summary['after_reversal_filter']}")
        print(f"   最终输出:   {summary['output_count']}")

        candidates = result["candidates"]
        if not candidates:
            print("\n⚠️  当前市场无符合条件的底部反转候选")
            return

        print(f"\n🔄 {mode_label} 候选 ({len(candidates)} 个):\n")
        for i, c in enumerate(candidates, 1):
            fr_s = f"{c['funding_rate']:.3f}%" if c.get("funding_rate") is not None else "N/A"
            dist_s = f"{c['dist_bottom_pct']:.1f}%" if c.get("dist_bottom_pct") is not None else "N/A"
            drop_s = f"{c['prior_drop_pct']:.1f}%" if c.get("prior_drop_pct") is not None else "N/A"

            print(f"  {i:2d}. {c['symbol']:14s} 评分:{c['reversal_score']:3d}/100")
            print(f"      价格: {c['close']:.4g} | 24h: {c['price_change_pct']:+.2f}% | "
                  f"成交额: {c['quote_volume_24h']:,.0f} USDT")
            print(f"      放量: {c['volume_surge_ratio']:.1f}x({c['volume_surge_score']}分) | "
                  f"企稳: {c['price_stable_score']}分 | "
                  f"均线: {c['ma_turn_detail'] or '-'}({c['ma_turn_score']}分)")
            print(f"      费率: {fr_s}({c['funding_reversal_score']}分) | "
                  f"MACD: {c['macd_detail'] or '-'}({c['macd_reversal_score']}分) | "
                  f"距底部: {dist_s}({c['dist_bottom_score']}分)")
            print(f"      前期跌: {drop_s}({c['prior_drop_score']}分) | "
                  f"KDJ: {c['kdj_score']}分 | 下影线: {c['shadow_score']}分")
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
