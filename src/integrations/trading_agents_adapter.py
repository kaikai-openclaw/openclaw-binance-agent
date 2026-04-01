"""
TradingAgents 适配器

将 TradingAgents 的 propagate() 输出转换为本系统 Skill-2 所需的格式：
{rating_score: int(1-10), signal: str, confidence: float(0-100)}

支持两种模式：
- fast_mode=True：单次 LLM 调用，约 10-30 秒完成
- fast_mode=False（默认）：完整 TradingAgents 多智能体辩论，约 5-10 分钟
"""

import json
import logging
import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# 适配器在 src/integrations/ 下，项目根目录是父父目录
# trading_agents_adapter.py → integrations/ → src/ → 项目根
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_project_root, ".env"))

log = logging.getLogger(__name__)


# ── 快速分析器（单次 LLM 调用）─────────────────────────────────────────────

def _fetch_binance_ticker(symbol: str) -> Dict[str, Any]:
    """从 Binance fapi 获取单个币种的 24h tick 数据。"""
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
    for d in r.json():
        if d["symbol"] == symbol:
            return {
                "symbol": symbol,
                "last_price": float(d["lastPrice"]),
                "price_change_pct": float(d["priceChangePercent"]),
                "volume": float(d["volume"]),
                "quote_volume": float(d["quoteVolume"]),
                "high_24h": float(d["highPrice"]),
                "low_24h": float(d["lowPrice"]),
            }
    raise ValueError(f"未找到 {symbol} 的市场数据")


def _call_fast_llm(prompt: str) -> str:
    """单次 LLM API 调用，返回文本响应。优先 MiniMax，其次 Gemini。"""
    minimax_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if minimax_key:
        resp = requests.post(
            "https://api.minimaxi.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {minimax_key}"},
            json={"model": "MiniMax-M2.7-highspeed",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # Fallback to Gemini
    google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if google_key:
        from google.genai import Client
        client = Client(api_key=google_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
        )
        return response.text

    raise ValueError("No LLM API key found (MINIMAX_API_KEY or GOOGLE_API_KEY)")


def _extract_json(text: str) -> dict:
    """
    从 LLM 返回的文本中提取第一个 JSON 对象。

    处理常见情况：
    - 纯 JSON
    - JSON 前后有解释文字
    - markdown code fence 包裹
    - 多行格式化 JSON
    """
    # 先去掉 markdown code fence
    text = re.sub(r"```(?:json)?\s*", "", text)

    # 匹配第一个 { ... } 块（支持嵌套）
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return json.loads(text[start:i + 1])

    raise ValueError(f"未找到有效 JSON 对象: {text[:200]}")


def _clean_llm_text(text: str) -> str:
    """清理 LLM 输出中的 thinking 标签和 markdown 噪音，只保留纯文本摘要。"""
    # 去掉 <think>...</think> 块（含多行）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # 去掉残留的单独标签
    text = re.sub(r"</?think>", "", text)
    # 去掉 markdown code fence
    text = re.sub(r"```(?:json)?\s*", "", text)
    # 压缩连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def create_fast_analyzer() -> callable:
    """
    创建快速分析器（单次 LLM 调用，约 10-30 秒）。

    不依赖 TradingAgents 框架，直接：
    1. 获取 Binance 实时行情
    2. 单次 LLM 调用输出评级

    返回:
        analyzer(symbol, market_data) -> dict
    """

    def analyzer(symbol: str, market_data: dict) -> Dict[str, Any]:
        t0 = time.time()

        # 尝试获取实时行情，失败时用上游 market_data 兜底
        try:
            ticker = _fetch_binance_ticker(symbol)
        except Exception as e:
            log.warning(f"[FastAnalyzer] {symbol} Binance 行情获取失败: {e}, 使用上游 market_data 兜底")
            ticker = {
                "symbol": symbol,
                "last_price": market_data.get("last_price", 0),
                "price_change_pct": market_data.get("price_change_pct", 0),
                "volume": market_data.get("volume", 0),
                "quote_volume": market_data.get("quote_volume_24h", market_data.get("quote_volume", 0)),
                "high_24h": market_data.get("high_24h", 0),
                "low_24h": market_data.get("low_24h", 0),
            }
            if not ticker["last_price"]:
                return {"rating_score": 5, "signal": "hold", "confidence": 30.0,
                        "comment": f"[快速模式] Binance 行情获取失败且无兜底数据: {e}"}

        log.info(f"[FastAnalyzer] {symbol} 行情获取成功: {ticker['last_price']}, "
                 f"24h {ticker['price_change_pct']:+.2f}%")

        prompt = f"""你是加密货币量化分析师。请根据以下 {symbol} 市场数据，给出结构化交易评级。

市场数据：
- 当前价格: {ticker['last_price']} USDT
- 24h 涨跌幅: {ticker['price_change_pct']:+.2f}%
- 24h 成交量: {ticker['quote_volume']/1e6:.1f}M USDT
- 24h 高点: {ticker['high_24h']} / 低点: {ticker['low_24h']}

请直接返回以下 JSON 格式（不解释，只返回 JSON）：
{{"rating_score": <int 1-10>, "signal": "<long|short|hold>", "confidence": <float 0-100>}}

评级标准：rating_score 6分以上为通过。signal 为 long 表示建议做多，short 表示做空，hold 表示观望。
"""

        try:
            raw = _call_fast_llm(prompt)
            result = _extract_json(raw)
            result["comment"] = f"[快速模式] {_clean_llm_text(raw)[:300]}"
            log.info(f"[FastAnalyzer] {symbol} 分析完成，耗时 {time.time()-t0:.1f}s: {result}")
            return result
        except Exception as e:
            log.warning(f"[FastAnalyzer] {symbol} LLM 调用失败: {e}")
            return {"rating_score": 5, "signal": "hold", "confidence": 50.0,
                    "comment": f"[快速模式] 分析失败: {e}"}

    log.info("[FastAnalyzer] 快速分析器已初始化（单次 LLM 调用）")
    return analyzer


# ── 标准 TradingAgents 分析器 ──────────────────────────────────────────────

def create_trading_agents_analyzer(
    llm_provider: str = "minimax",
    deep_think_llm: str = "MiniMax-M2.7-highspeed",
    quick_think_llm: str = "MiniMax-M2.7-highspeed",
    backend_url: Optional[str] = None,
    max_debate_rounds: int = 1,
    fast_mode: bool = False,  # 默认使用完整 TradingAgents 多智能体分析
) -> callable:
    """
    创建可注入 TradingAgentsModule 的 analyzer 回调函数。

    参数:
        llm_provider: LLM 提供商 (openai/google/anthropic/xai/openrouter/ollama/minimax/qwen/zhipu)
        deep_think_llm: 复杂推理模型
        quick_think_llm: 快速任务模型
        backend_url: API 端点（None 则使用各 provider 默认端点，minimax 需传 https://api.minimaxi.com/v1）
        max_debate_rounds: 多空辩论轮数（越多越准但越慢）

    返回:
        analyzer(symbol, market_data) -> dict
    """
    # 快速模式：直接用单次 LLM 调用，不加载完整 TradingAgents 框架
    if fast_mode:
        log.info("[TradingAgentsAdapter] fast_mode=True，使用快速单次 LLM 分析")
        return create_fast_analyzer()

    # 延迟导入，仅在实际使用时才需要 TradingAgents
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG

    # 自动推断 backend_url：仅 minimax 需要自定义端点
    if backend_url is None and llm_provider == "minimax":
        backend_url = "https://api.minimaxi.com/v1"

    config = {
        **DEFAULT_CONFIG,
        "output_language": "Chinese",
        "llm_provider": llm_provider,
        "deep_think_llm": deep_think_llm,
        "quick_think_llm": quick_think_llm,
        "max_debate_rounds": max_debate_rounds,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 100,
        "backend_url": backend_url,
        "data_vendors": {
            "core_stock_apis": "binance",
            "technical_indicators": "binance",
            "fundamental_data": "binance",
            "news_data": "binance",
        },
    }

    ta = TradingAgentsGraph(debug=False, config=config)
    log.info(
        f"TradingAgents 已初始化: provider={llm_provider}, "
        f"model={deep_think_llm}, debate_rounds={max_debate_rounds}"
    )

    # propagate() 超时保护（秒）：防止 LLM/数据源挂住导致进程被 kill
    PROPAGATE_TIMEOUT = 900  # 15分钟

    def analyzer(symbol: str, market_data: dict) -> Dict[str, Any]:
        """调用 TradingAgents 分析单个币种，带超时保护。"""
        # BTCUSDT → BTC-USD（yfinance 加密货币格式）
        ticker = symbol.replace("USDT", "-USD")
        analysis_date = datetime.now().strftime("%Y-%m-%d")

        log.info(f"TradingAgents 分析: {ticker} @ {analysis_date} (超时={PROPAGATE_TIMEOUT}s)")

        # 用线程池包裹 propagate()，超时自动 fallback
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ta.propagate, ticker, analysis_date)
            try:
                final_state, decision = future.result(timeout=PROPAGATE_TIMEOUT)
            except FuturesTimeoutError:
                future.cancel()
                log.warning(
                    f"TradingAgents {ticker} 超时（>{PROPAGATE_TIMEOUT}s），"
                    f"fallback 到快速模式"
                )
                fast = create_fast_analyzer()
                return fast(symbol, market_data)
            except Exception as exc:
                log.warning(
                    f"TradingAgents 完整分析 {ticker} 失败: {exc}, "
                    f"fallback 到快速模式（Binance 数据 + LLM）"
                )
                fast = create_fast_analyzer()
                return fast(symbol, market_data)

        log.info(f"TradingAgents 返回 decision type={type(decision)}, value={repr(decision)[:200]}")
        if final_state:
            ftd = final_state.get("final_trade_decision", "")
            log.info(f"TradingAgents final_trade_decision: {repr(ftd)[:300]}")
            # 如果 decision 为空但 final_trade_decision 有值，用它
            if not decision and ftd:
                decision = ftd
        if not decision:
            raise ValueError("TradingAgents 返回空决策")
        result = _parse_decision(decision)
        # 保留原始决策文本作为摘要点评
        result["comment"] = _clean_llm_text(decision)[:500] if decision else "无分析结果"
        return result

    return analyzer


def _parse_decision(decision: str) -> Dict[str, Any]:
    """将 TradingAgents 文本决策解析为结构化评级（支持中英文输出）。"""
    d = decision.lower()

    # 中文关键词
    if "强烈买入" in decision or "强烈推荐买入" in decision:
        return {"rating_score": 9, "signal": "long", "confidence": 85.0}
    elif "买入" in decision or "做多" in decision:
        return {"rating_score": 7, "signal": "long", "confidence": 70.0}
    elif "强烈卖出" in decision or "强烈推荐卖出" in decision:
        return {"rating_score": 9, "signal": "short", "confidence": 85.0}
    elif "卖出" in decision or "做空" in decision:
        return {"rating_score": 7, "signal": "short", "confidence": 70.0}
    elif "持有" in decision or "观望" in decision or "中性" in decision:
        return {"rating_score": 5, "signal": "hold", "confidence": 50.0}
    # 英文关键词
    elif "strong buy" in d or "strongly recommend buying" in d:
        return {"rating_score": 9, "signal": "long", "confidence": 85.0}
    elif "buy" in d or "long" in d:
        return {"rating_score": 7, "signal": "long", "confidence": 70.0}
    elif "strong sell" in d or "strongly recommend selling" in d:
        return {"rating_score": 9, "signal": "short", "confidence": 85.0}
    elif "sell" in d or "short" in d:
        return {"rating_score": 7, "signal": "short", "confidence": 70.0}
    elif "hold" in d or "neutral" in d:
        return {"rating_score": 5, "signal": "hold", "confidence": 50.0}
    else:
        return {"rating_score": 4, "signal": "hold", "confidence": 30.0}
