#!/usr/bin/env python3
"""快速分析 PUMPUSDT - Skill1收集 + Skill2深度分析"""
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer

print("=" * 60)
print("快速分析 PUMPUSDT")
print("=" * 60)

# 初始化
store = StateStore(db_path="data/state_store.db")
limiter = RateLimiter()
client = BinancePublicClient(rate_limiter=limiter)

# Skill-1 配置
with open("config/schemas/skill1_input.json") as f:
    in_schema = json.load(f)
with open("config/schemas/skill1_output.json") as f:
    out_schema = json.load(f)

# 执行 Skill-1（仅 PUMPUSDT）
print("\n📡 [Skill-1] 收集 PUMPUSDT 数据...")
skill1 = Skill1Collect(state_store=store, input_schema=in_schema, output_schema=out_schema, client=client)
trigger_data = {"trigger_time": datetime.now(timezone.utc).isoformat()}
trigger_id = store.save("skill1_trigger", trigger_data)
s1_out_id = skill1.execute(trigger_id)
s1_result = store.load(s1_out_id)

candidates = s1_result.get("candidates", [])
print(f"   候选: {[c['symbol'] for c in candidates]}")

# 构造 Skill-2 输入
s2_in_id = store.save("trigger_skill2", {"input_state_id": s1_out_id})

# Skill-2 配置
with open("config/schemas/skill2_input.json") as f:
    s2_in_schema = json.load(f)
with open("config/schemas/skill2_output.json") as f:
    s2_out_schema = json.load(f)

# 初始化 TradingAgents（快速模式）
print("\n🤖 [Skill-2] 初始化 TradingAgents (max_debate_rounds=0)...")
analyzer = create_trading_agents_analyzer(max_debate_rounds=0)
ta_module = TradingAgentsModule(analyzer=analyzer)
skill2 = Skill2Analyze(state_store=store, input_schema=s2_in_schema, output_schema=s2_out_schema,
                       trading_agents=ta_module, rating_threshold=7)

print("🔍 [Skill-2] 开始深度分析...")
t0 = time.time()
out_id = skill2.execute(s2_in_id)
result = store.load(out_id)
elapsed = time.time() - t0

print(f"\n{'=' * 60}")
print(f"✅ 分析完成，耗时: {elapsed:.1f}s")
print(f"{'=' * 60}")

ratings = result.get("ratings", [])
failed = result.get("failed_symbols", [])

if ratings:
    for r in ratings:
        print(f"\n🎯 {r['symbol']}")
        print(f"   评级分数: {r['rating_score']}/10")
        print(f"   信号: {r['signal']}")
        print(f"   置信度: {r['confidence']}")
        print(f"   简评: {r.get('summary', 'N/A')}")
        if r.get('reasons'):
            print(f"   理由: {'; '.join(r['reasons'])}")
else:
    print("\n⚠️ 无通过评级的候选")

if failed:
    print(f"\n❌ 失败: {[f['symbol'] for f in failed]}")

ta_module.shutdown()
store.close()
