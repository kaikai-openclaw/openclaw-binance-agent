"""End-to-end test: run TradingAgents with MiniMax provider."""

from dotenv import load_dotenv
load_dotenv()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = {
    **DEFAULT_CONFIG,
    "llm_provider": "minimax",
    "deep_think_llm":"MiniMax-M2.7-highspeed",
    "quick_think_llm": "MiniMax-M2.7-highspeed",
    "backend_url": "https://api.minimaxi.com/v1",
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "output_language": "Chinese",
}

print("Initializing TradingAgentsGraph with MiniMax...")
graph = TradingAgentsGraph(
    selected_analysts=["market", "news"],
    config=config,
)

print("Running analysis for SPY...")
final_state, decision = graph.propagate("SPY", "2026-03-28")

print(f"\n{'='*60}")
print(f"Decision: {decision}")
print(f"{'='*60}")
print(f"\nFinal trade decision (excerpt):\n{final_state['final_trade_decision'][:500]}")
