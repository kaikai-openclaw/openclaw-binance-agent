#!/usr/bin/env python3
"""
加密货币合约超买做空扫描 CLI

筛选短期涨幅过大、多头过度拥挤的币种，寻找高胜率做空机会。
支持短期（4h）和长期（1d）两种模式。

用法:
    python3 scan_overbought.py                        # 默认短期模式
    python3 scan_overbought.py --mode short           # 短期超买（4h）
    python3 scan_overbought.py --mode long            # 长期超买（1d）
    python3 scan_overbought.py --mode long --symbols BTC,ETH,SOL
    python3 scan_overbought.py --mode short --min-score 30
    python3 scan_overbought.py --json
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
from src.skills.crypto_overbought import ShortTermOverboughtSkill, LongTermOverboughtSkill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="加密货币合约超买做空扫描")
    parser.add_argument("--mode", type=str, default="short",
                        choices=["short", "long"],
                        help="扫描模式：short=短期4h超买, long=长期1d超买（默认 short）")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定币种，逗号分隔（如 BTC,ETH,SOL）")
    parser.add_argument("--min-score", type=int, default=25,
                        help="最低超买评分（默认 25）")
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

    in_schema = load_schema("crypto_overbought_input.json")
    out_schema = load_schema("crypto_overbought_output.json")

    if args.mode == "long":
        skill = LongTermOverboughtSkill(store, in_schema, out_schema, client)
        mode_label = "长期超买做空（1d 日线）"
    else:
        skill = ShortTermOverboughtSkill(store, in_schema, out_schema, client)
        mode_label = "短期超买做空（4h）"

    try:
        input_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "min_overbought_score": args.min_score,
            "max_candidates": args.max_candidates,
        }
        if args.symbols:
            input_data["target_symbols"] = [
                s.strip().upper() for s in args.symbols.split(",") if s.strip()
            ]

        print(f"📡 {mode_label} — 正在扫描超买做空候选...")
        result = skill.run(input_data)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        summary = result["filter_summary"]
        print(f"\n📊 筛选漏斗:")
        print(f"   全部交易对: {summary['total_tickers']}")
        print(f"   基础过滤后: {summary['after_base_filter']}")
        print(f"   超买信号后: {summary['after_overbought_filter']}")
        print(f"   最终输出:   {summary['output_count']}")

        candidates = result["candidates"]
        if not candidates:
            print("\n⚠️  当前市场无符合条件的超买做空候选")
            return

        print(f"\n🔻 {mode_label} 候选 ({len(candidates)} 个):\n")
        for i, c in enumerate(candidates, 1):
            rsi_s = f"{c['rsi']:.1f}" if c.get("rsi") is not None else "N/A"
            bias_s = f"{c['bias_20']:.1f}%" if c.get("bias_20") is not None else "N/A"
            fr_s = f"{c['funding_rate']:.3f}%" if c.get("funding_rate") is not None else "N/A"
            rally_s = f"{c['rally_pct']:.1f}%" if c.get("rally_pct") is not None else "N/A"
            rise_s = f"{c['rise_from_low_pct']:.1f}%" if c.get("rise_from_low_pct") is not None else "N/A"
            squeeze = " ⚠️轧空" if c.get("squeeze_risk") else ""

            print(f"  {i:2d}. {c['symbol']:14s} 评分:{c['overbought_score']:3d}/100{squeeze}")
            print(f"      价格: {c['close']:.4g} | 24h: {c['price_change_pct']:+.2f}% | "
                  f"成交额: {c['quote_volume_24h']:,.0f} USDT")
            print(f"      RSI: {rsi_s} | BIAS: {bias_s} | "
                  f"连涨: {c['consecutive_up']}根 | 累涨: {rally_s}")
            print(f"      费率: {fr_s} | 距低点涨: {rise_s} | "
                  f"BOLL破: {'✓' if c['above_boll_upper'] else '✗'} | "
                  f"MACD背离: {'✓' if c['macd_divergence'] else '✗'} | "
                  f"量价背离: {'✓' if c['volume_divergence'] else '✗'}")
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
