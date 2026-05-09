import os

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    # Supported providers: openai, anthropic, google, xai, ollama, openrouter,
    #                      minimax, qwen, zhipu
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    "backend_url": "https://api.openai.com/v1",
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # 数据源配置
    # 分类级别配置（每个分类的默认数据源）
    "data_vendors": {
        "core_stock_apis": "yfinance",       # 可选: alpha_vantage, yfinance, binance, akshare
        "technical_indicators": "yfinance",  # 可选: alpha_vantage, yfinance, binance, akshare
        "fundamental_data": "yfinance",      # 可选: alpha_vantage, yfinance, binance, akshare
        "news_data": "yfinance",             # 可选: alpha_vantage, yfinance, binance, akshare
    },
    # 工具级别配置（优先级高于分类级别）
    "tool_vendors": {
        # 示例: "get_stock_data": "akshare",  # 覆盖分类默认值
    },
}
