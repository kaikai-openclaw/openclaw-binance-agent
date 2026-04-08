#!/usr/bin/env python3
"""
候选币种筛选（仅 Skill-1，OpenClaw skill 调用入口）

从全市场量化筛选候选币种，输出评分排名。不执行交易。

用法:
    python3 collect_candidates.py
    python3 collect_candidates.py --symbols ONT,BTC,SOL
"""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from datetime import datetime, timezone
from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.skills.skill1_collect import Skill1Collect


def main():
    parser = argparse.ArgumentParser(description="候选币种筛选")
    parser.add_argument("--symbols", type=str, default="", help="指定币种，逗号分隔")
    args = parser.parse_args()

    db_path = os.path.join(PROJECT_ROOT, "data", "state_store.db")
    store = StateStore(db_path=db_path)
    client = BinancePublicClient(rate_limiter=RateLimiter())

    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, "skill1_input.json")) as f:
        in_schema = json.load(f)
    with open(os.path.join(schema_dir, "skill1_output.json")) as f:
        out_schema = json.load(f)

    skill1 = Skill1Collect(
        state_store=store, input_schema=in_schema,
        output_schema=out_schema, client=client,
    )

    trigger_data = {"trigger_time": datetime.now(timezone.utc).isoformat()}
    if args.symbols:
        trigger_data["target_symbols"] = [s.strip() for s in args.symbols.split(",") if s.strip()]

    try:
        print("📡 正在筛选候选币种...")
        trigger_id = store.save("skill1_trigger", trigger_data)
        out_id = skill1.execute(trigger_id)
        result = store.load(out_id)

        summary = result.get("filter_summary", {})
        print(f"\n📊 筛选漏斗:")
        print(f"   全部交易对: {summary.get('total_tickers', '?')}")
        print(f"   大盘过滤后: {summary.get('after_base_filter', '?')}")
        print(f"   信号过滤后: {summary.get('after_signal_filter', '?')}")
        print(f"   最终输出:   {summary.get('output_count', '?')}")

        candidates = result.get("candidates", [])
        if not candidates:
            print("\n⚠️  当前市场无符合条件的候选币种")
        else:
            print(f"\n🎯 候选币种 ({len(candidates)} 个):\n")
            for i, c in enumerate(candidates, 1):
                rsi_str = f"{c['rsi']:.1f}" if c.get("rsi") is not None else "N/A"
                print(f"  {i}. {c['symbol']}")
                print(f"     评分: {c['signal_score']}/100 | 方向: {c.get('signal_direction','?')} | "
                      f"成交额: {c['quote_volume_24h']:,.0f} USDT")
                print(f"     涨幅: {c['price_change_pct']:+.2f}% | 量比: {c['volume_surge_ratio']:.2f}x | "
                      f"RSI: {rsi_str} | ADX: {c.get('adx', 'N/A')}")

        print(f"\n💡 state_id: {out_id}")

    except Exception as e:
        print(f"❌ 筛选失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
