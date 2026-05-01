import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infra.state_store import StateStore
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule

logging.basicConfig(level=logging.INFO)

# 1. 编写 Mock 分析器函数 (替代实际调用的 TradingAgents)
def mock_analyzer_fn(symbol: str, market_data: dict) -> dict:
    print(f"  [Mock AI] 正在深度分析 {symbol} 的 K线/资金流/社交情绪 ...")
    if symbol == "BTCUSDT":
        return {"rating_score": 8, "signal": "long", "confidence": 85.0}
    elif symbol == "ETHUSDT":
        return {"rating_score": 7, "signal": "long", "confidence": 70.0}
    else:
        # 低于阈值 (默认6分)
        return {"rating_score": 4, "signal": "hold", "confidence": 30.0}

def main():
    print("初始化 StateStore...")
    store = StateStore(db_path="data/test_state.db")
    
    with open("config/schemas/skill2_input.json") as f:
        in_schema = json.load(f)
    with open("config/schemas/skill2_output.json") as f:
        out_schema = json.load(f)
        
    print("实例化 Skill2Analyze...")
    # 注入 TradingAgentsModule，将底层算法 mock 掉，以便快速测试流水线流转
    ta_module = TradingAgentsModule(analyzer=mock_analyzer_fn)
    skill2 = Skill2Analyze(
        state_store=store,
        input_schema=in_schema,
        output_schema=out_schema,
        trading_agents=ta_module,
        rating_threshold=7  # 设置合格阈值
    )
    
    # 2. 伪造 Skill-1 的输出
    s1_out_id = store.save("skill1_collect", {
        "pipeline_run_id": "test-run-002",
        "candidates": [
            {"symbol": "BTCUSDT", "heat_score": 8.5, "source_url": "mock", "collected_at": datetime.now(timezone.utc).isoformat()},
            {"symbol": "ETHUSDT", "heat_score": 8.0, "source_url": "mock", "collected_at": datetime.now(timezone.utc).isoformat()},
            {"symbol": "DOGEUSDT", "heat_score": 9.0, "source_url": "mock", "collected_at": datetime.now(timezone.utc).isoformat()}
        ]
    })
    
    # 构建 Skill-2 的输入状态引用
    s2_in_id = store.save("trigger_skill2", {
        "input_state_id": s1_out_id
    })
    
    print(f"构造了 Skill-1 输出: {s1_out_id}")
    print(f"构造了 Skill-2 输入: {s2_in_id}")
    print("--------------------------------------------------")
    print("运行 Skill2Analyze.execute()...")
    
    # 3. 执行 Skill-2
    out_id = skill2.execute(s2_in_id)
    
    # 4. 打印结果
    result = store.load(out_id)
    print("--------------------------------------------------")
    print("\n✅ Skill-2 运行结果:")
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
