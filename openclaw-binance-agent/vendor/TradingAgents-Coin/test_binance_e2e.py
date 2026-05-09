"""
端到端测试: MiniMax + Binance 数据流
精简配置，只跑 market analyst，最小化 agent 数量以定位卡住的环节。
"""

import os
import sys
import time
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e")

# 设置全局超时 — 5 分钟后自动退出
TIMEOUT = 300

def timeout_handler(signum, frame):
    logger.error(f"⛔ Global timeout ({TIMEOUT}s) reached — process is hanging!")
    sys.exit(1)

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(TIMEOUT)

from dotenv import load_dotenv
load_dotenv()

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = {
    **DEFAULT_CONFIG,
    "llm_provider": "minimax",
    "deep_think_llm": "MiniMax-M2.7-highspeed",
    "quick_think_llm": "MiniMax-M2.7-highspeed",
    "backend_url": "https://api.minimaxi.com/v1",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "output_language": "Chinese",
    # 全部走 Binance
    "data_vendors": {
        "core_stock_apis": "binance",
        "technical_indicators": "binance",
        "fundamental_data": "binance",
        "news_data": "binance",
    },
}

print(f"{'='*60}")
print("E2E Test: MiniMax + Binance, analysts=['market', 'news']")
print(f"{'='*60}")

t0 = time.time()
print("\n▶ Initializing TradingAgentsGraph...")
graph = TradingAgentsGraph(
    selected_analysts=["market", "news"],
    debug=True,
    config=config,
)
print(f"  ✅ Graph initialized in {time.time()-t0:.1f}s")

print("\n▶ Running propagate('BTC-USD', '2026-03-28')...")
t1 = time.time()
try:
    final_state, decision = graph.propagate("BTC-USD", "2026-03-28")
    elapsed = time.time() - t1
    print(f"\n{'='*60}")
    print(f"✅ Completed in {elapsed:.1f}s")
    print(f"Decision: {decision}")
    print(f"{'='*60}")
    if "final_trade_decision" in final_state:
        print(f"\nFinal trade decision (first 500 chars):")
        print(final_state["final_trade_decision"][:500])
except Exception as e:
    elapsed = time.time() - t1
    logger.error(f"❌ Failed after {elapsed:.1f}s: {e}", exc_info=True)
    sys.exit(1)
