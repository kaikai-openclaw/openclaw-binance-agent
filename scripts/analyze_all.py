#!/usr/bin/env python3
"""深度分析所有候选币种 (Skill-2)"""
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
print("深度分析所有候选币种 (TradingAgents)")
print("=" * 60)

# 1. 初始化
store = StateStore(db_path="data/state_store.db")
with open("config/schemas/skill2_input.json") as f:
    in_schema = json.load(f)
with open("config/schemas/skill2_output.json") as f:
    out_schema = json.load(f)

# 2. 创建真实 analyzer（调用 MiniMax API）
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

# 4. 使用 Skill-1 的输出作为输入
s1_state_id = "79e98edc-94a6-466f-a9d1-350dfe811a1f"

# 先验证 s1 数据是否存在
try:
    s1_data = store.load(s1_state_id)
    candidates = s1_data.get("candidates", [])
    print(f"\n[data] 从 Skill-1 读取到 {len(candidates)} 个候选币种")
except Exception as e:
    print(f"\n[error] 无法读取 Skill-1 数据: {e}")
    print("       重新运行 Skill-1...")
    store.close()
    sys.exit(1)

s2_in_id = store.save("trigger_skill2", {"input_state_id": s1_state_id})

print(f"[data] Skill-1 输出 state_id: {s1_state_id}")
print(f"[data] Skill-2 输入 state_id: {s2_in_id}")
print("\n[run] 开始深度分析 (可能需要 5-15 分钟)...")
print("-" * 60)

t1 = time.time()
try:
    out_id = skill2.execute(s2_in_id)
    elapsed = time.time() - t1
    result = store.load(out_id)

    print("-" * 60)
    print(f"\n[ok] 分析完成! 耗时: {elapsed:.1f}s ({elapsed/60:.1f} 分钟)")

    # 输出结果
    ratings = result.get("ratings", [])
    failed = result.get("failed_symbols", [])
    filtered_count = result.get("filtered_count", 0)
    
    print(f"\n📊 分析结果汇总:")
    print("=" * 60)
    print(f"通过评级 (≥6分): {len(ratings)} 个")
    print(f"未通过评级: {filtered_count} 个")
    print(f"分析失败: {len(failed)} 个")
    
    if ratings:
        print(f"\n✅ 通过评级的币种:")
        print("-" * 60)
        # 按评分排序
        ratings_sorted = sorted(ratings, key=lambda x: x.get('rating_score', 0), reverse=True)
        for i, r in enumerate(ratings_sorted, 1):
            print(f"\n   {i}. {r['symbol']}")
            print(f"      评分: {r['rating_score']}/10")
            print(f"      信号: {r['signal']}")
            print(f"      置信度: {r['confidence']}%")
            if r.get('comment'):
                comment = r['comment'][:200] + "..." if len(r.get('comment', '')) > 200 else r.get('comment', '')
                print(f"      评语: {comment}")
    
    if failed:
        print(f"\n❌ 分析失败的币种:")
        for f_ in failed:
            print(f"   {f_['symbol']}: {f_['reason']}")

except Exception as e:
    elapsed = time.time() - t1
    print(f"\n❌ 分析失败 ({elapsed:.1f}s): {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    ta_module.shutdown()
    store.close()
