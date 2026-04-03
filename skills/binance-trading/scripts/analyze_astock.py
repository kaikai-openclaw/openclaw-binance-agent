#!/usr/bin/env python3
"""
A 股深度分析（Skill-1A + Skill-2A 入口脚本）

对指定 A 股执行量化筛选 + TradingAgents 深度分析评级。

用法:
    python3 analyze_astock.py 600519
    python3 analyze_astock.py 000001 --fast
    python3 analyze_astock.py --scan            # 全市场扫描
    python3 analyze_astock.py --scan --fast
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
from src.integrations.astock_trading_agents_adapter import (
    create_astock_trading_agents_analyzer,
)
from src.skills.skill1a_collect import Skill1ACollect
from src.skills.skill2a_analyze import Skill2AAnalyze, AStockTradingAgentsModule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="A 股深度分析")
    parser.add_argument("symbol", nargs="?", type=str, help="A 股代码（如 600519 或 SH600519）")
    parser.add_argument("--fast", action="store_true", help="快速 LLM 分析模式")
    parser.add_argument("--scan", action="store_true", help="全市场扫描模式（不指定个股）")
    args = parser.parse_args()

    if not args.symbol and not args.scan:
        parser.error("请指定股票代码或使用 --scan 全市场扫描")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    ta_module = None
    try:
        # ── Skill-1A: A 股数据采集 ──
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
        }
        if args.symbol:
            # 标准化代码
            sym = args.symbol.strip().upper()
            for pfx in ("SH", "SZ", "BJ"):
                if sym.startswith(pfx):
                    sym = sym[len(pfx):]
            sym = sym.replace(".", "")
            trigger_data["target_symbols"] = [sym]
            print(f"📡 Step 1: 收集 {sym} 市场数据...")
        else:
            print("📡 Step 1: 全市场扫描...")

        skill1a = Skill1ACollect(
            state_store=store,
            input_schema=load_schema("skill1a_input.json"),
            output_schema=load_schema("skill1a_output.json"),
            client=client,
        )
        trigger_id = store.save("astock_trigger", trigger_data)
        s1_id = skill1a.execute(trigger_id)
        s1_data = store.load(s1_id)
        candidates = s1_data.get("candidates", [])
        summary = s1_data.get("filter_summary", {})

        if args.scan:
            print(f"   筛选漏斗: {summary.get('total_tickers', '?')} → {summary.get('output_count', 0)} 个候选")

        if not candidates:
            print("⚠️  无符合条件的候选，分析结束")
            return

        for i, c in enumerate(candidates, 1):
            print(f"   {i}. {c['symbol']} {c.get('name','')} "
                  f"(评分:{c['signal_score']}, 方向:{c.get('signal_direction','?')})")

        # ── Skill-2A: 深度分析 ──
        mode_str = "快速模式" if args.fast else "完整模式"
        print(f"\n🔬 Step 2: 深度分析（{mode_str}）...")

        analyzer_fn = create_astock_trading_agents_analyzer(fast_mode=args.fast)
        ta_module = AStockTradingAgentsModule(analyzer=analyzer_fn)

        skill2a = Skill2AAnalyze(
            state_store=store,
            input_schema=load_schema("skill2a_input.json"),
            output_schema=load_schema("skill2a_output.json"),
            trading_agents=ta_module,
            rating_threshold=6,
        )
        s2_input_id = store.save("skill2a_input", {"input_state_id": s1_id})
        s2_id = skill2a.execute(s2_input_id)
        s2_data = store.load(s2_id)

        ratings = s2_data.get("ratings", [])
        failed = s2_data.get("failed_symbols", [])

        if ratings:
            for r in ratings:
                print(f"\n✅ {r['symbol']} 通过评级")
                print(f"   评分: {r['rating_score']}/10 | 信号: {r['signal']} | 置信度: {r['confidence']:.0f}%")
                if r.get("comment"):
                    print(f"   点评: {r['comment'][:300]}")
        else:
            print("\n⚠️  无股票通过评级（阈值 6 分）")

        if failed:
            for f_item in failed:
                print(f"   ❌ {f_item['symbol']}: {f_item['reason']}")

        print(f"\n📊 {s2_data.get('analysis_summary', '')}")

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
