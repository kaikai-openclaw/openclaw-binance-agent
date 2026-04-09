#!/usr/bin/env python3
"""
A 股超跌交易计划生成器

基于 Skill-1B 超跌筛选结果，生成量化交易计划（入场/止损/止盈/仓位）。

用法:
    # 先扫描超跌候选，再生成交易计划（一步到位）
    python3 make_trade_plan.py --scan --mode short
    python3 make_trade_plan.py --scan --mode long

    # 指定个股
    python3 make_trade_plan.py --scan --mode short --symbols 600519 000001

    # 指定总资金
    python3 make_trade_plan.py --scan --mode short --capital 500000

    # 输出 JSON
    python3 make_trade_plan.py --scan --mode short --json
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
from src.skills.skill1b_oversold import ShortTermAStockOversold, LongTermAStockOversold
from src.skills.astock_trade_plan import generate_trade_plans, format_trade_plan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="A 股超跌交易计划生成器")
    parser.add_argument("--scan", action="store_true", required=True,
                        help="扫描超跌候选并生成交易计划")
    parser.add_argument("--mode", type=str, default="short",
                        choices=["short", "long"],
                        help="策略模式：short=短期反弹, long=长期蓄能（默认 short）")
    parser.add_argument("--symbols", nargs="*", type=str,
                        help="指定股票代码")
    parser.add_argument("--capital", type=float, default=100000,
                        help="总资金（元，默认 10 万）")
    parser.add_argument("--existing-pos", type=float, default=0,
                        help="已有持仓占比（%%，默认 0）")
    parser.add_argument("--json", action="store_true",
                        help="输出原始 JSON")
    args = parser.parse_args()

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    mode_label = "短期超跌反弹" if args.mode == "short" else "长期超跌蓄能"

    try:
        # Step 1: 超跌筛选
        print(f"📡 Step 1: {mode_label}筛选...")
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
        }
        if args.symbols:
            syms = []
            for s in args.symbols:
                s = s.strip().upper().replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "")
                syms.append(s)
            trigger_data["target_symbols"] = syms

        in_schema = load_schema("skill1b_input.json")
        out_schema = load_schema("skill1b_output.json")

        if args.mode == "long":
            skill = LongTermAStockOversold(store, in_schema, out_schema, client)
        else:
            skill = ShortTermAStockOversold(store, in_schema, out_schema, client)

        result = skill.run(trigger_data)
        candidates = result.get("candidates", [])
        summary = result.get("filter_summary", {})

        print(f"   漏斗: {summary.get('total_tickers', '?')} → "
              f"{summary.get('after_base_filter', '?')} → "
              f"{summary.get('after_oversold_filter', '?')} → "
              f"{summary.get('output_count', 0)}")

        if not candidates:
            print(f"\n⚠️  无符合条件的{mode_label}候选")
            return

        # Step 2: 生成交易计划
        print(f"\n📋 Step 2: 生成交易计划（资金 ¥{args.capital:,.0f}）...")
        plan_result = generate_trade_plans(
            candidates,
            mode=args.mode,
            total_capital=args.capital,
            existing_position_pct=args.existing_pos,
        )

        if args.json:
            print(json.dumps(plan_result, ensure_ascii=False, indent=2))
            return

        plans = plan_result["trade_plans"]
        plan_summary = plan_result["summary"]

        if not plans:
            print(f"\n⚠️  无符合入场条件的交易计划（评分不足）")
            return

        print(f"   生成 {len(plans)} 个交易计划 | "
              f"总仓位: {plan_summary['total_position_pct']}%\n")

        for plan in plans:
            print(format_trade_plan(plan))
            print()

        # 风险提示
        print(f"{'═' * 60}")
        print(f"  ⚠️  风险提示")
        print(f"{'─' * 60}")
        print(f"  • 超跌反弹是左侧交易（接飞刀），严格执行止损")
        print(f"  • T+1 交易：当天买入次日才能卖出")
        print(f"  • 止损触发后同股票 {5} 个交易日内不重复开仓")
        print(f"  • 单只仓位 ≤ 20%，总仓位 ≤ 80%")
        if args.mode == "long":
            print(f"  • 长期策略建议分批建仓，不要一次性满仓")
        print(f"{'═' * 60}")

    except Exception as e:
        print(f"❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
