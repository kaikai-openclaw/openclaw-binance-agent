#!/usr/bin/env python3
"""
指定币种深度分析（Skill-1 + Skill-2，OpenClaw skill 调用入口）

对指定币种执行量化筛选 + 深度分析评级。

用法:
    python3 analyze_symbol.py BTCUSDT
    python3 analyze_symbol.py SOLUSDT --fast
"""
import argparse
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from datetime import datetime, timezone
from src.infra.binance_public import BinancePublicClient
from src.infra.memory_store import MemoryStore
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="币种深度分析")
    parser.add_argument("symbol", type=str, help="币种符号（如 BTCUSDT 或 BTC）")
    parser.add_argument("--fast", action="store_true", help="快速 LLM 分析模式")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    db_dir = os.path.join(PROJECT_ROOT, "data")
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    memory_store = MemoryStore(db_path=os.path.join(db_dir, "trading_state.db"))
    client = BinancePublicClient(rate_limiter=RateLimiter())

    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")

    def load_schema(name):
        with open(os.path.join(schema_dir, name)) as f:
            return json.load(f)

    try:
        # ── Skill-1: 指定币种模式 ──
        print(f"📡 Step 1: 收集 {symbol} 市场数据...")
        skill1 = Skill1Collect(
            state_store=store,
            input_schema=load_schema("skill1_input.json"),
            output_schema=load_schema("skill1_output.json"),
            client=client,
        )
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "target_symbols": [symbol.replace("USDT", "")],
        }
        trigger_id = store.save("analyze_trigger", trigger_data)
        s1_id = skill1.execute(trigger_id)
        s1_data = store.load(s1_id)
        candidates = s1_data.get("candidates", [])

        if not candidates:
            print(f"⚠️  {symbol} 未通过技术指标筛选（信号评分或 ADX 不足）")
            return

        c = candidates[0]
        print(f"   评分: {c['signal_score']}/100 | 方向: {c.get('signal_direction','?')} | "
              f"RSI: {c.get('rsi', 'N/A')} | ADX: {c.get('adx', 'N/A')}")

        # ── Skill-2: 深度分析 ──
        mode_str = "快速模式" if args.fast else "完整模式"
        print(f"\n🔬 Step 2: 深度分析（{mode_str}）...")

        rating_threshold, _ = memory_store.get_evolved_params()
        analyzer_fn = create_trading_agents_analyzer(fast_mode=args.fast)
        ta_module = TradingAgentsModule(analyzer=analyzer_fn)

        skill2 = Skill2Analyze(
            state_store=store,
            input_schema=load_schema("skill2_input.json"),
            output_schema=load_schema("skill2_output.json"),
            trading_agents=ta_module,
            rating_threshold=rating_threshold,
        )
        s2_input_id = store.save("skill2_input", {"input_state_id": s1_id})
        s2_id = skill2.execute(s2_input_id)
        s2_data = store.load(s2_id)

        ratings = s2_data.get("ratings", [])
        failed = s2_data.get("failed_symbols", [])

        if ratings:
            for r in ratings:
                print(f"\n✅ {r['symbol']} 通过评级")
                print(f"   评分: {r['rating_score']}/10 | 信号: {r['signal']} | 置信度: {r['confidence']:.0f}%")
                if r.get("comment"):
                    # 截断过长的评论
                    comment = r["comment"][:300]
                    print(f"   点评: {comment}")
        else:
            print(f"\n⚠️  {symbol} 未通过评级（阈值 {rating_threshold} 分）")

        if failed:
            for f_item in failed:
                print(f"   ❌ {f_item['symbol']}: {f_item['reason']}")

    except Exception as e:
        print(f"❌ 分析失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()
        memory_store.close()
        ta_ref = locals().get("ta_module")
        if ta_ref:
            ta_ref.shutdown()


if __name__ == "__main__":
    main()
    