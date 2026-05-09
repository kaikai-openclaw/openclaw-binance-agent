"""
分步测试 Binance 数据流 — 逐层排查 TradingAgents 卡住的原因。

测试顺序:
  1. Binance API 连通性 (raw HTTP)
  2. binance_client 封装层
  3. binance_stock / binance_indicator / binance_fundamentals / binance_news
  4. interface.py 路由层 (route_to_vendor)
  5. LangChain tool 层 (core_stock_tools 等)
  6. LLM 初始化 (MiniMax)
  7. 单 agent 调用 (market analyst with Binance)
"""

import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_binance_flow")

from dotenv import load_dotenv
load_dotenv()

# ── helpers ──────────────────────────────────────────────────────────────────

def timed(label):
    """Simple context manager to time a block."""
    class Timer:
        def __enter__(self):
            self.t0 = time.time()
            print(f"\n{'='*60}")
            print(f"▶ {label}")
            print(f"{'='*60}")
            return self
        def __exit__(self, *exc):
            elapsed = time.time() - self.t0
            status = "✅ OK" if not exc[0] else f"❌ FAILED ({exc[1]})"
            print(f"  ⏱  {elapsed:.2f}s — {status}")
    return Timer()


SYMBOL = "BTCUSDT"
CURR_DATE = "2026-03-28"
START_DATE = "2026-03-25"
END_DATE = "2026-03-28"


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Raw HTTP — Binance API 连通性
# ═══════════════════════════════════════════════════════════════════════════
def step1_raw_http():
    import requests
    with timed("Step 1: Raw HTTP ping to Binance /api/v3/ping"):
        resp = requests.get("https://api.binance.com/api/v3/ping", timeout=10)
        resp.raise_for_status()
        print(f"  Response: {resp.json()}")

    with timed("Step 1b: Raw kline fetch (3 days BTC)"):
        resp = requests.get("https://api.binance.com/api/v3/klines", params={
            "symbol": SYMBOL, "interval": "1d", "limit": 3,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        print(f"  Got {len(data)} klines, first open time: {data[0][0] if data else 'N/A'}")


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: binance_client 封装层
# ═══════════════════════════════════════════════════════════════════════════
def step2_binance_client():
    from tradingagents.dataflows.binance_client import binance_request
    with timed("Step 2: binance_client.binance_request — /api/v3/ticker/24hr"):
        result = binance_request("/api/v3/ticker/24hr", {"symbol": SYMBOL})
        print(f"  Last price: {result.get('lastPrice')}, 24h vol: {result.get('volume')}")


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: 数据模块层 — stock / indicator / fundamentals / news
# ═══════════════════════════════════════════════════════════════════════════
def step3_data_modules():
    from tradingagents.dataflows.binance_stock import get_stock_data
    from tradingagents.dataflows.binance_indicator import get_indicators
    from tradingagents.dataflows.binance_fundamentals import get_fundamentals
    from tradingagents.dataflows.binance_news import get_news

    with timed("Step 3a: binance_stock.get_stock_data"):
        result = get_stock_data("BTC-USD", START_DATE, END_DATE)
        print(f"  Result length: {len(result)} chars")
        print(f"  First 200 chars:\n{result[:200]}")

    with timed("Step 3b: binance_indicator.get_indicators (RSI)"):
        result = get_indicators("BTC-USD", "rsi", CURR_DATE, 7)
        print(f"  Result length: {len(result)} chars")
        print(f"  First 300 chars:\n{result[:300]}")

    with timed("Step 3c: binance_fundamentals.get_fundamentals"):
        result = get_fundamentals("BTC-USD")
        print(f"  Result length: {len(result)} chars")
        print(f"  First 300 chars:\n{result[:300]}")

    with timed("Step 3d: binance_news.get_news"):
        result = get_news("BTC-USD", START_DATE, END_DATE)
        print(f"  Result length: {len(result)} chars")
        print(f"  First 300 chars:\n{result[:300]}")


# ═══════════════════════════════════════════════════════════════════════════
# Step 4: interface.py 路由层 (route_to_vendor with binance config)
# ═══════════════════════════════════════════════════════════════════════════
def step4_routing():
    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import DEFAULT_CONFIG

    # Force binance as vendor
    config = DEFAULT_CONFIG.copy()
    config["data_vendors"] = {
        "core_stock_apis": "binance",
        "technical_indicators": "binance",
        "fundamental_data": "binance",
        "news_data": "binance",
    }
    set_config(config)

    from tradingagents.dataflows.interface import route_to_vendor

    with timed("Step 4a: route_to_vendor('get_stock_data') via binance"):
        result = route_to_vendor("get_stock_data", "BTC-USD", START_DATE, END_DATE)
        print(f"  Result length: {len(result)} chars")

    with timed("Step 4b: route_to_vendor('get_fundamentals') via binance"):
        result = route_to_vendor("get_fundamentals", "BTC-USD", CURR_DATE)
        print(f"  Result length: {len(result)} chars")

    with timed("Step 4c: route_to_vendor('get_news') via binance"):
        result = route_to_vendor("get_news", "BTC-USD", START_DATE, END_DATE)
        print(f"  Result length: {len(result)} chars")


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: LangChain tool 层
# ═══════════════════════════════════════════════════════════════════════════
def step5_langchain_tools():
    from tradingagents.agents.utils.core_stock_tools import get_stock_data as tool_get_stock
    with timed("Step 5: LangChain @tool get_stock_data"):
        result = tool_get_stock.invoke({
            "symbol": "BTC-USD",
            "start_date": START_DATE,
            "end_date": END_DATE,
        })
        print(f"  Result length: {len(result)} chars")
        print(f"  First 200 chars:\n{result[:200]}")


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: LLM 初始化 (MiniMax)
# ═══════════════════════════════════════════════════════════════════════════
def step6_llm_init():
    from tradingagents.llm_clients import create_llm_client

    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        print("  ⚠️  MINIMAX_API_KEY not set, skipping LLM test")
        return

    with timed("Step 6a: Create MiniMax LLM client"):
        client = create_llm_client(
            provider="minimax",
            model="MiniMax-M2.7-highspeed",
            base_url="https://api.minimaxi.com/v1",
        )
        llm = client.get_llm()
        print(f"  LLM type: {type(llm).__name__}")

    with timed("Step 6b: Simple LLM invoke (should respond in <30s)"):
        resp = llm.invoke("Say 'hello' in one word.")
        print(f"  Response: {resp.content[:200]}")


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    steps = {
        "1": step1_raw_http,
        "2": step2_binance_client,
        "3": step3_data_modules,
        "4": step4_routing,
        "5": step5_langchain_tools,
        "6": step6_llm_init,
    }

    # Run specific step or all
    if len(sys.argv) > 1:
        for s in sys.argv[1:]:
            if s in steps:
                try:
                    steps[s]()
                except Exception as e:
                    logger.error(f"Step {s} failed: {e}", exc_info=True)
            else:
                print(f"Unknown step: {s}. Available: {list(steps.keys())}")
    else:
        for key, fn in steps.items():
            try:
                fn()
            except Exception as e:
                logger.error(f"Step {key} failed: {e}", exc_info=True)
                print(f"\n⛔ Step {key} failed, continuing...\n")

    print(f"\n{'='*60}")
    print("Done. Review output above to identify where things break.")
    print(f"{'='*60}")
