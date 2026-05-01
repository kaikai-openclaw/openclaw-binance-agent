#!/usr/bin/env python3
"""深度分析 BLURUSDT (Skill-2)"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

# 设置 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infra.state_store import StateStore
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule

print("=" * 60)
print("深度分析 BLURUSDT (TradingAgents)")
print("=" * 60)

# 1. 初始化
store = StateStore(db_path="data/state_store.db")
with open("config/schemas/skill2_input.json") as f:
    in_schema = json.load(f)
with open("config/schemas/skill2_output.json") as f:
    out_schema = json.load(f)

# 2. 创建真实 analyzer
print("\n[init] 初始化 TradingAgents (MiniMax)...")
t0 = time.time()
analyzer = create_trading_agents_analyzer(max_debate_rounds=0)
print(f"   初始化耗时: {time.time() - t0:.1f}s")

# 3. 组装 Skill2
ta_module = TradingAgentsModule(analyzer=analyzer)
skill2 = Skill2Analyze(
    state_store=store,
    input_schema=in_schema,
    output_schema=out_schema,
    trading_agents=ta_module,
    rating_threshold=6,
)

# 4. 伪造 Skill-1 输出（只测 BLURUSDT）
now = datetime.now(timezone.utc).isoformat()
s1_out_id = store.save(
    "skill1_collect",
    {
        "pipeline_run_id": "blur-analysis",
        "candidates": [
            {
                "symbol": "BLURUSDT",
                "heat_score": 9.0,
                "source_url": "https://binance.com",
                "collected_at": now,
            },
        ],
    },
)
s2_in_id = store.save("trigger_skill2", {"input_state_id": s1_out_id})

print(f"\n[data] Skill-1 输出 state_id: {s1_out_id}")
print(f"[data] Skill-2 输入 state_id: {s2_in_id}")
print("\n[run] 开始深度分析 BLURUSDT (可能需要 2-5 分钟)...")
print("-" * 60)

t1 = time.time()
try:
    out_id = skill2.execute(s2_in_id)
    elapsed = time.time() - t1
    result = store.load(out_id)

    print("-" * 60)
    print(f"\n[ok] 分析完成! 耗时: {elapsed:.1f}s")

    # 输出结果
    ratings = result.get("ratings", [])
    failed = result.get("failed_symbols", [])
    
    print(f"\n📊 分析结果:")
    print("=" * 60)
    
    if failed:
        print(f"❌ 失败: {len(failed)} 个")
        for f_ in failed:
            print(f"   {f_['symbol']}: {f_['reason']}")
    
    if ratings:
        for r in ratings:
            print(f"\n✅ {r['symbol']}")
            print(f"   评分: {r['rating_score']}/10")
            print(f"   信号: {r['signal']}")
            print(f"   置信度: {r['confidence']}%")
            if r.get('comment'):
                print(f"   评语: {r['comment']}")
    else:
        print("\n⚠️ 未通过评级阈值 (6分)")
        print("   建议: 选择其他候选币种")

except Exception as e:
    elapsed = time.time() - t1
    print(f"\n❌ 分析失败 ({elapsed:.1f}s): {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    ta_module.shutdown()
    store.close()
