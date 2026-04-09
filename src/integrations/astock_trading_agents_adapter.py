"""
A 股 TradingAgents 适配器

将 TradingAgents 的 propagate() 输出转换为 Skill-2A 所需格式：
{rating_score: int(1-10), signal: str, confidence: float(0-100)}

与 trading_agents_adapter.py 的区别：
  - data_vendors 全部配置为 akshare
  - ticker 格式为 A 股 6 位代码（如 600519）
  - 快速模式使用 akshare 实时行情而非 Binance

TradingAgents-Coin 以 git submodule 形式引入，位于 vendor/TradingAgents-Coin。
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

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_project_root, ".env"))

log = logging.getLogger(__name__)


def _env(key: str, default: str) -> str:
    return os.environ.get(key, "").strip() or default


FAST_LLM_MODEL = _env("FAST_LLM_MODEL", "MiniMax-M2.7-highspeed")
DEFAULT_LLM_PROVIDER = _env("LLM_PROVIDER", "minimax")
DEFAULT_DEEP_THINK_LLM = _env("DEEP_THINK_LLM", "MiniMax-M2.7-highspeed")
DEFAULT_QUICK_THINK_LLM = _env("QUICK_THINK_LLM", "MiniMax-M2.7-highspeed")
DEFAULT_BACKEND_URL = _env("LLM_BACKEND_URL", "")


# ── 快速分析器（单次 LLM）─────────────────────────────────

def _fetch_astock_quote(symbol: str) -> Dict[str, Any]:
    """获取 A 股行情，优先实时行情，fallback 本地缓存日线。"""
    # 方案 A：东方财富实时
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == symbol]
        if not row.empty:
            r = row.iloc[0]
            return {
                "symbol": symbol,
                "name": str(r.get("名称", "")),
                "last_price": float(r.get("最新价", 0)),
                "change_pct": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)),
                "amount": float(r.get("成交额", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "turnover_rate": float(r.get("换手率", 0) or 0),
            }
    except Exception:
        pass

    # 方案 B：本地 SQLite 缓存日线（零网络）
    try:
        from src.infra.kline_cache import KlineCache
        cache = KlineCache()
        rows = cache.query_as_rows(symbol, "qfq", 2)
        cache.close()
        if rows and len(rows) >= 2:
            last, prev = rows[-1], rows[-2]
            close = float(last[4])
            prev_close = float(prev[4])
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0
            return {
                "symbol": symbol, "name": "",
                "last_price": close,
                "change_pct": round(change_pct, 2),
                "volume": float(last[5]),
                "amount": 0.0,
                "high": float(last[2]),
                "low": float(last[3]),
                "turnover_rate": 0.0,
            }
    except Exception:
        pass

    # 方案 C：AkshareClient（自带缓存层）
    try:
        from src.infra.akshare_client import AkshareClient, _symbol_exchange
        client = AkshareClient()
        klines = client.get_klines(symbol, "daily", 2)
        if klines and len(klines) >= 2:
            last, prev = klines[-1], klines[-2]
            close = float(last[4])
            prev_close = float(prev[4])
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0
            return {
                "symbol": symbol, "name": "",
                "last_price": close,
                "change_pct": round(change_pct, 2),
                "volume": float(last[5]),
                "amount": 0.0,
                "high": float(last[2]),
                "low": float(last[3]),
                "turnover_rate": 0.0,
            }
    except Exception as e:
        raise ValueError(f"获取 {symbol} 行情失败（所有接口）: {e}")


def _call_fast_llm(prompt: str, model: Optional[str] = None) -> str:
    """单次 LLM 调用（复用 trading_agents_adapter 的多 provider 逻辑）。"""
    from src.integrations.trading_agents_adapter import _call_fast_llm as _call
    return _call(prompt, model)


def _extract_json(text: str) -> dict:
    from src.integrations.trading_agents_adapter import _extract_json as _extract
    return _extract(text)


def _clean_llm_text(text: str) -> str:
    from src.integrations.trading_agents_adapter import _clean_llm_text as _clean
    return _clean(text)


def create_astock_fast_analyzer() -> callable:
    """A 股快速分析器（单次 LLM 调用）。"""

    def analyzer(symbol: str, market_data: dict) -> Dict[str, Any]:
        t0 = time.time()
        try:
            quote = _fetch_astock_quote(symbol)
        except Exception as e:
            log.warning("[AStockFast] %s 行情获取失败: %s", symbol, e)
            return {"rating_score": 5, "signal": "hold", "confidence": 30.0,
                    "comment": f"[快速模式] 行情获取失败: {e}"}

        name = quote.get("name", symbol)
        prompt = f"""你是一名专业的 A 股量化研究员。请根据以下 {name}({symbol}) 的市场数据，进行量化评估并输出结构化评分。

市场数据：
- 当前价格: {quote['last_price']:.2f} 元
- 涨跌幅: {quote['change_pct']:+.2f}%
- 成交额: {quote['amount']/1e8:.1f} 亿元
- 最高: {quote['high']:.2f} / 最低: {quote['low']:.2f}
- 换手率: {quote['turnover_rate']:.2f}%

请基于以上数据，从技术面和资金面角度进行量化评估。
直接返回以下 JSON 格式（不要解释，只返回 JSON）：
{{"rating_score": <int 1-10>, "signal": "<long|short|hold>", "confidence": <float 0-100>}}

说明：rating_score 为综合评分（1最低10最高），signal 为趋势方向判断，confidence 为置信度百分比。
"""
        try:
            raw = _call_fast_llm(prompt)
            cleaned = _clean_llm_text(raw)
            result = _extract_json(cleaned)
            result["comment"] = f"[快速模式] {cleaned[:300]}"
            log.info("[AStockFast] %s 完成, %.1fs: score=%s",
                     symbol, time.time() - t0, result.get("rating_score"))
            return result
        except Exception as e:
            log.warning("[AStockFast] %s LLM 失败: %s", symbol, e)
            return {"rating_score": 5, "signal": "hold", "confidence": 50.0,
                    "comment": f"[快速模式] 分析失败: {e}"}

    return analyzer


# ── 标准 TradingAgents 分析器（A 股版）──────────────────────

def create_astock_trading_agents_analyzer(
    llm_provider: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    backend_url: Optional[str] = None,
    max_debate_rounds: int = 1,
    fast_mode: bool = False,
) -> callable:
    """
    创建 A 股版 TradingAgents analyzer 回调。

    关键区别：data_vendors 全部配置为 akshare，
    使 TradingAgents 通过 akshare 获取 A 股数据。
    """
    llm_provider = llm_provider or DEFAULT_LLM_PROVIDER
    deep_think_llm = deep_think_llm or DEFAULT_DEEP_THINK_LLM
    quick_think_llm = quick_think_llm or DEFAULT_QUICK_THINK_LLM
    if backend_url is None:
        backend_url = DEFAULT_BACKEND_URL or None

    log.info("[AStockTA] 配置: provider=%s, deep=%s, fast_mode=%s",
             llm_provider, deep_think_llm, fast_mode)

    if fast_mode:
        return create_astock_fast_analyzer()

    # 延迟导入 TradingAgents
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG

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
        # 核心区别：全部使用 akshare 数据源
        "data_vendors": {
            "core_stock_apis": "akshare",
            "technical_indicators": "akshare",
            "fundamental_data": "akshare",
            "news_data": "akshare",
        },
    }

    ta = TradingAgentsGraph(debug=False, config=config)
    log.info("[AStockTA] TradingAgents 已初始化 (akshare 数据源)")

    PROPAGATE_TIMEOUT = 900  # 15 分钟

    def analyzer(symbol: str, market_data: dict) -> Dict[str, Any]:
        """调用 TradingAgents 分析 A 股，带超时保护。"""
        # A 股代码直接传入（TradingAgents akshare 模块接受 6 位代码）
        analysis_date = datetime.now().strftime("%Y-%m-%d")
        log.info("[AStockTA] 分析: %s @ %s", symbol, analysis_date)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ta.propagate, symbol, analysis_date)
            try:
                final_state, decision = future.result(timeout=PROPAGATE_TIMEOUT)
            except FuturesTimeoutError:
                future.cancel()
                log.warning("[AStockTA] %s 超时, fallback 快速模式", symbol)
                fast = create_astock_fast_analyzer()
                return fast(symbol, market_data)
            except Exception as exc:
                log.warning("[AStockTA] %s 失败: %s, fallback 快速模式", symbol, exc)
                fast = create_astock_fast_analyzer()
                return fast(symbol, market_data)

        if final_state:
            ftd = final_state.get("final_trade_decision", "")
            if not decision and ftd:
                decision = ftd
        if not decision:
            raise ValueError("TradingAgents 返回空决策")

        result = _parse_decision(decision)
        # 优先保存完整分析报告（final_trade_decision），而非压缩后的信号词
        full_report = ""
        if final_state:
            full_report = final_state.get("final_trade_decision", "")
        if full_report:
            result["comment"] = _clean_llm_text(full_report)[:2000]
        else:
            result["comment"] = _clean_llm_text(decision)[:500] if decision else "无分析结果"
        # 保存完整分析报告到磁盘
        if final_state:
            from src.infra.report_store import save_analysis_report
            save_analysis_report(final_state, symbol, market="astock")
        return result

    return analyzer


def _parse_decision(decision: str) -> Dict[str, Any]:
    """将 TradingAgents 文本决策解析为结构化评级（支持中英文）。"""
    d = decision.lower()

    if "强烈买入" in decision or "强烈推荐买入" in decision:
        return {"rating_score": 9, "signal": "long", "confidence": 85.0}
    elif "买入" in decision or "做多" in decision or "看多" in decision:
        return {"rating_score": 7, "signal": "long", "confidence": 70.0}
    elif "强烈卖出" in decision or "强烈推荐卖出" in decision:
        return {"rating_score": 9, "signal": "short", "confidence": 85.0}
    elif "卖出" in decision or "做空" in decision or "看空" in decision:
        return {"rating_score": 7, "signal": "short", "confidence": 70.0}
    elif "持有" in decision or "观望" in decision or "中性" in decision:
        return {"rating_score": 5, "signal": "hold", "confidence": 50.0}
    elif "strong buy" in d:
        return {"rating_score": 9, "signal": "long", "confidence": 85.0}
    elif "buy" in d or "long" in d:
        return {"rating_score": 7, "signal": "long", "confidence": 70.0}
    elif "strong sell" in d:
        return {"rating_score": 9, "signal": "short", "confidence": 85.0}
    elif "sell" in d or "short" in d:
        return {"rating_score": 7, "signal": "short", "confidence": 70.0}
    elif "hold" in d or "neutral" in d:
        return {"rating_score": 5, "signal": "hold", "confidence": 50.0}
    else:
        return {"rating_score": 4, "signal": "hold", "confidence": 30.0}
