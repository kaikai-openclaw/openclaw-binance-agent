#!/usr/bin/env python3
"""
测试 TradingAgents + Binance data_vendors 配置

用单个币种（BTC-USD）快速验证：
1. TradingAgents 能否用 binance 数据源初始化
2. propagate() 能否正常返回决策
"""

import os
import sys
import time
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test_ta_binance")

from datetime import datetime
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
    analysis_date = datetime.now().strftime("%Y-%m-%d")

    config = {
        **DEFAULT_CONFIG,
        "output_language": "Chinese",
        "llm_provider": "google",
        "deep_think_llm": "gemini-2.5-flash",
        "quick_think_llm": "gemini-2.5-flash",
        "backend_url": None,  # Google 不需要 backend_url，用默认端点
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 100,
        "data_vendors": {
            "core_stock_apis": "binance",
            "technical_indicators": "binance",
            "fundamental_data": "binance",
            "news_data": "binance",
        },
    }

    log.info(f"初始化 TradingAgents (data_vendors=binance)...")
    t0 = time.time()
    ta = TradingAgentsGraph(debug=False, config=config)
    log.info(f"初始化完成，耗时 {time.time()-t0:.1f}s")

    log.info(f"开始分析 {ticker} @ {analysis_date} ...")
    t1 = time.time()
    try:
        final_state, decision = ta.propagate(ticker, analysis_date)
        elapsed = time.time() - t1
        log.info(f"分析完成，耗时 {elapsed:.1f}s")
        print("\n" + "=" * 60)
        print(f"  TradingAgents 分析结果: {ticker}")
        print("=" * 60)
        print(f"\n决策:\n{decision}")
        if final_state:
            ftd = final_state.get("final_trade_decision", "")
            if ftd:
                print(f"\nfinal_trade_decision:\n{ftd[:500]}")
        print(f"\n耗时: {elapsed:.1f}s")
        print("=" * 60)
    except Exception as e:
        log.error(f"分析失败: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
