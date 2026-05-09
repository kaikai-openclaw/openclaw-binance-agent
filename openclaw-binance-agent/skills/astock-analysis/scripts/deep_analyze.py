#!/usr/bin/env python3
"""
Skill-2A：A 股深度分析

两种调用方式：
  1. 传 state_id（接 Skill-1A/1B 输出）
  2. 直接传股票代码（独立调用）

用法:
    # 独立调用：直接传股票代码
    python3 deep_analyze.py 600519
    python3 deep_analyze.py 600519 000001 300750 --fast

    # 接上游：传 state_id
    python3 deep_analyze.py --state-id <state_id>
    python3 deep_analyze.py --state-id <state_id> --fast
    python3 deep_analyze.py --state-id <state_id> --threshold 7
"""
import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.state_store import StateStore
from src.integrations.astock_trading_agents_adapter import (
    create_astock_trading_agents_analyzer,
)
from src.skills.skill2a_analyze import Skill2AAnalyze, AStockTradingAgentsModule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def _build_candidates_from_symbols(symbols: list, store: StateStore) -> str:
    """从股票代码列表构造候选数据并存入 StateStore，返回 state_id。"""
    candidates = []
    for sym in symbols:
        sym = sym.strip().upper()
        for pfx in ("SH", "SZ", "BJ"):
            if sym.startswith(pfx):
                sym = sym[len(pfx):]
        sym = sym.replace(".", "")
        if not sym.isdigit() or len(sym) != 6:
            print(f"⚠️  跳过无效代码: {sym}")
            continue
        candidates.append({
            "symbol": sym,
            "name": "",
            "signal_score": 0,
            "signal_direction": "",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })

    if not candidates:
        print("❌ 无有效股票代码")
        sys.exit(1)

    data = {
        "state_id": str(uuid.uuid4()),
        "candidates": candidates,
        "pipeline_run_id": str(uuid.uuid4()),
        "filter_summary": {
            "total_tickers": len(candidates),
            "after_base_filter": len(candidates),
            "after_signal_filter": len(candidates),
            "output_count": len(candidates),
        },
    }
    return store.save("direct_analyze_input", data)


def main():
    parser = argparse.ArgumentParser(description="A 股深度分析（Skill-2A）")
    parser.add_argument("symbols", nargs="*", type=str,
                        help="股票代码（如 600519 000001），直接独立分析")
    parser.add_argument("--state-id", type=str, default=None,
                        help="Skill-1A/1B 输出的 state_id（接上游模式）")
    parser.add_argument("--fast", action="store_true",
                        help="快速 LLM 分析模式（单次调用，10-30秒/股）")
    parser.add_argument("--threshold", type=int, default=6,
                        help="评级通过阈值（默认 6）")
    args = parser.parse_args()

    if not args.symbols and not args.state_id:
        parser.error("请指定股票代码或 --state-id")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))

    ta_module = None
    try:
        # 确定 state_id
        if args.state_id:
            state_id = args.state_id
            s1_data = store.load(state_id)
        else:
            state_id = _build_candidates_from_symbols(args.symbols, store)
            s1_data = store.load(state_id)

        candidates = s1_data.get("candidates", [])
        if not candidates:
            print("⚠️  无候选数据")
            return

        print(f"📋 {len(candidates)} 个候选:")
        for i, c in enumerate(candidates, 1):
            score = c.get("signal_score") or c.get("oversold_score") or "—"
            print(f"   {i}. {c['symbol']} {c.get('name', '')} (评分:{score})")

        mode_str = "快速模式" if args.fast else "完整模式"
        print(f"\n🔬 深度分析（{mode_str}，阈值 {args.threshold} 分）...")

        analyzer_fn = create_astock_trading_agents_analyzer(fast_mode=args.fast)
        ta_module = AStockTradingAgentsModule(analyzer=analyzer_fn)

        skill2a = Skill2AAnalyze(
            state_store=store,
            input_schema=load_schema("skill2a_input.json"),
            output_schema=load_schema("skill2a_output.json"),
            trading_agents=ta_module,
            rating_threshold=args.threshold,
        )
        s2_input_id = store.save("skill2a_input", {"input_state_id": state_id})
        s2_id = skill2a.execute(s2_input_id)
        s2_data = store.load(s2_id)

        ratings = s2_data.get("ratings", [])
        failed = s2_data.get("failed_symbols", [])

        if ratings:
            print(f"\n{'='*60}")
            for r in ratings:
                print(f"✅ {r['symbol']}  评分:{r['rating_score']}/10 | "
                      f"信号:{r['signal']} | 置信度:{r['confidence']:.0f}%")
                if r.get("comment"):
                    print(f"   {r['comment'][:300]}")
            print(f"{'='*60}")
        else:
            print(f"\n⚠️  无股票通过评级（阈值 {args.threshold} 分）")

        if failed:
            print(f"\n失败 {len(failed)} 只:")
            for f_item in failed:
                print(f"   ❌ {f_item['symbol']}: {f_item['reason']}")

        print(f"\n📊 {s2_data.get('analysis_summary', '')}")
        print(f"📋 state_id: {s2_id}")

    except Exception as e:
        print(f"❌ 分析失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()
        if ta_module:
            ta_module.shutdown()


if __name__ == "__main__":
    main()
