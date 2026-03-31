import os
import sys

# 必须加载环境变量
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val.strip()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

print("初始化 TradingAgents 框架...")
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-2.5-flash"
config["quick_think_llm"] = "gemini-2.5-flash"
config["max_debate_rounds"] = 0  # 彻底关掉多轮辩论
config["max_risk_discuss_rounds"] = 0 # 关掉风控辩论
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance"
}

ta = TradingAgentsGraph(debug=True, config=config)
print("正在执行 ta.propagate('BTC-USD')，这可能需要很长时间，甚至死循环...")
try:
    result, decision = ta.propagate("BTC-USD", "2026-03-30")
    print("\n✅ 终于跑完了！决策：")
    print(decision)
except Exception as e:
    print(f"\n❌ 执行崩溃: {e}")
