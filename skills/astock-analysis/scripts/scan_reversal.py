#!/usr/bin/env python3
"""
A 股底部放量反转扫描

用法:
    python3 scan_reversal.py --scan                    # 全市场扫描
    python3 scan_reversal.py 600519                    # 指定个股
    python3 scan_reversal.py --scan --min-score 50     # 调整评分门槛
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
from src.infra.akshare_client import AkshareClient
from src.infra.state_store import StateStore
from src.skills.astock_reversal import AStockReversalSkill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="A 股底部放量反转扫描")
    parser.add_argument("symbols", nargs="*", type=str, help="A 股代码")
    parser.add_argument("--scan", action="store_true", help="全市场扫描")
    parser.add_argument("--min-score", type=int, default=40, help="最低评分（默认 40）")
    parser.add_argument("--max", type=int, default=20, help="最大输出数量")
    args = parser.parse_args()

    if not args.symbols and not args.scan:
        parser.error("请指定股票代码或使用 --scan")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    # 用空 schema 跳过校验（直接调用 run）
    skill = AStockReversalSkill(store, {"type": "object"}, {"type": "object"}, client)

    try:
        input_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "min_score": args.min_score,
            "max_candidates": args.max,
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


if __name__ == "__main__":
    main()
