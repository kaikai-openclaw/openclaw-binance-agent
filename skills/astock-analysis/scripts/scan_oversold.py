#!/usr/bin/env python3
"""
Skill-1B：A 股超跌反弹筛选

多维度超跌信号检测，输出候选列表和 state_id。
可用 state_id 手动调用 Skill-2A 深度分析。

用法:
    python3 scan_oversold.py --scan                    # 全市场扫描
    python3 scan_oversold.py 600519                    # 指定个股
    python3 scan_oversold.py 600519 000001 300750      # 多个个股
    python3 scan_oversold.py --scan --rsi 30           # 自定义 RSI 阈值
    python3 scan_oversold.py --scan --bias -8          # 自定义乖离率阈值
    python3 scan_oversold.py --scan --min-score 40     # 降低评分门槛
    python3 scan_oversold.py --scan --volume-confirm   # 要求底部放量确认
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
from src.skills.skill1b_oversold import Skill1BOversold

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="A 股超跌反弹筛选（Skill-1B）")
    parser.add_argument("symbols", nargs="*", type=str,
                        help="A 股代码（如 600519 SH000001）")
    parser.add_argument("--scan", action="store_true",
                        help="全市场扫描模式")
    parser.add_argument("--rsi", type=float, default=25,
                        help="RSI 超跌阈值（默认 35）")
    parser.add_argument("--bias", type=float, default=-6,
                        help="20日乖离率阈值（默认 -6%%）")
    parser.add_argument("--drop", type=float, default=-8,
                        help="近N日累计跌幅阈值（默认 -8%%）")
    parser.add_argument("--drop-days", type=int, default=10,
                        help="累计跌幅回看天数（默认 10）")
    parser.add_argument("--min-score", type=int, default=25,
                        help="超跌综合评分最低阈值（默认 25）")
    parser.add_argument("--max", type=int, default=30,
                        help="最大输出数量（默认 30）")
    parser.add_argument("--volume-confirm", action="store_true",
                        help="要求底部放量确认")
    parser.add_argument("--prefilter", type=float, default=0,
                        help="当日跌幅预筛阈值（默认 0 禁用，设 -3 启用当日跌幅过滤）")
    args = parser.parse_args()

    if not args.symbols and not args.scan:
        parser.error("请指定股票代码或使用 --scan 全市场扫描")

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    try:
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "rsi_threshold": args.rsi,
            "bias_threshold": args.bias,
            "drop_pct_threshold": args.drop,
            "drop_lookback_days": args.drop_days,
            "min_oversold_score": args.min_score,
            "max_candidates": args.max,
            "require_volume_confirm": args.volume_confirm,
            "prefilter_change_pct": args.prefilter,
        }

        if args.symbols:
            syms = []
            for s in args.symbols:
                s = s.strip().upper()
                for pfx in ("SH", "SZ", "BJ"):
                    if s.startswith(pfx):
                        s = s[len(pfx):]
                s = s.replace(".", "")
                syms.append(s)
            trigger_data["target_symbols"] = syms
            print(f"📡 Skill-1B 超跌反弹筛选: {', '.join(syms)}")
        else:
            print("📡 Skill-1B 超跌反弹筛选: 全市场扫描...")

        print(f"   参数: RSI<{args.rsi} | BIAS<{args.bias}% | "
              f"跌幅<{args.drop}%/{args.drop_days}日 | 评分≥{args.min_score} | "
              f"当日跌幅<{args.prefilter}%")

        skill1b = Skill1BOversold(
            state_store=store,
            input_schema=load_schema("skill1b_input.json"),
            output_schema=load_schema("skill1b_output.json"),
            client=client,
        )
        trigger_id = store.save("oversold_trigger", trigger_data)
        s1_id = skill1b.execute(trigger_id)
        s1_data = store.load(s1_id)
        candidates = s1_data.get("candidates", [])
        summary = s1_data.get("filter_summary", {})

        if args.scan:
            print(f"\n   筛选漏斗: {summary.get('total_tickers', '?')}"
                  f" → 基础过滤 {summary.get('after_base_filter', '?')}"
                  f" → 超跌信号 {summary.get('after_oversold_filter', '?')}"
                  f" → 输出 {summary.get('output_count', 0)}")

        if not candidates:
            print("\n⚠️  无符合条件的超跌候选")
            return

        print(f"\n🔻 超跌反弹候选（{len(candidates)} 只）:")
        print("-" * 90)
        for i, c in enumerate(candidates, 1):
            print(f"  {i:2d}. {c['symbol']} {c.get('name', ''):8s} "
                  f"¥{c['close']:8.2f} | "
                  f"评分:{c['oversold_score']:3d} | "
                  f"RSI:{c['rsi'] or '-':>5} | "
                  f"BIAS:{c['bias_20'] or '-':>6}% | "
                  f"连跌:{c['consecutive_down']}天")
            details = c.get("signal_details", "")
            if details:
                print(f"      信号: {details}")
        print("-" * 90)

        # 输出 state_id
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
