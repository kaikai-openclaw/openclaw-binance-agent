#!/usr/bin/env python3
"""
Skill-1A：A 股量化筛选（仅数据采集 + 候选筛选，不执行深度分析）

输出候选列表和 state_id，可用 state_id 手动调用 Skill-2A 深度分析。

用法:
    python3 analyze_astock.py 600519
    python3 analyze_astock.py 000001
    python3 analyze_astock.py --scan            # 全市场扫描
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
from src.skills.skill1a_collect import Skill1ACollect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="A 股量化筛选（Skill-1A）")
    parser.add_argument("symbol", nargs="?", type=str,
                        help="A 股代码（如 600519 或 SH600519）")
    parser.add_argument("--scan", action="store_true",
                        help="全市场扫描模式（不指定个股）")
    args = parser.parse_args()

    if not args.symbol and not args.scan:
        parser.error("请指定股票代码或使用 --scan 全市场扫描")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    try:
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
        }
        if args.symbol:
            sym = args.symbol.strip().upper()
            for pfx in ("SH", "SZ", "BJ"):
                if sym.startswith(pfx):
                    sym = sym[len(pfx):]
            sym = sym.replace(".", "")
            trigger_data["target_symbols"] = [sym]
            print(f"📡 Skill-1A: 收集 {sym} 市场数据...")
        else:
            print("📡 Skill-1A: 全市场扫描...")

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
            print(f"   筛选漏斗: {summary.get('total_tickers', '?')}"
                  f" → {summary.get('output_count', 0)} 个候选")

        if not candidates:
            print("⚠️  无符合条件的候选，筛选结束")
            return

        for i, c in enumerate(candidates, 1):
            ma_detail = c.get('ma_align_detail', '')
            breakout = c.get('breakout_detail', '')
            print(f"   {i}. {c['symbol']} {c.get('name', '')} "
                  f"¥{c.get('close', 0):.2f} | "
                  f"评分:{c['signal_score']} | "
                  f"均线:{c.get('ma_align_score', 0)} "
                  f"MACD:{c.get('macd_score', 0)} "
                  f"ADX:{c.get('adx_score', 0)} "
                  f"量价:{c.get('volume_score', 0)} "
                  f"突破:{c.get('breakout_score', 0)} "
                  f"RSI:{c.get('rsi_score', 0)}")
            details = []
            if ma_detail:
                details.append(ma_detail)
            if breakout:
                details.append(breakout)
            if details:
                print(f"      {' | '.join(details)}")

        # 输出 state_id，供手动调用 Skill-2A
        print(f"\n📋 state_id: {s1_id}")
        print(f"   如需深度分析，运行:")
        print(f"   python3 skills/astock-analysis/scripts/deep_analyze.py {s1_id}")
        print(f"   python3 skills/astock-analysis/scripts/deep_analyze.py {s1_id} --fast")

    except Exception as e:
        print(f"❌ 筛选失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
