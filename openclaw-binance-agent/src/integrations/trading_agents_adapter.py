"""
TradingAgents 适配器

将 TradingAgents 的 propagate() 输出转换为本系统 Skill-2 所需的格式：
{rating_score: int(1-10), signal: str, confidence: float(0-100)}

TradingAgents-Coin 以 git submodule 形式引入，位于 vendor/TradingAgents-Coin，
通过 `pip install -e vendor/TradingAgents-Coin` 以 editable 模式安装。

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
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# 适配器在 src/integrations/ 下，项目根目录是父父目录
# trading_agents_adapter.py → integrations/ → src/ → 项目根
_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
load_dotenv(os.path.join(_project_root, ".env"))

# 24h ticker 缓存配置
TICKER_CACHE_TTL_SECONDS = 180  # 3 分钟缓存
TICKER_CACHE_DB = os.path.join(_project_root, "data", "binance_kline_cache.db")
FAST_HOLD_RATING_SCORE = 4

log = logging.getLogger(__name__)


# ── 模型配置（可通过 .env 覆盖）──────────────────────────────────────────────
# 环境变量优先，函数参数次之，最后用默认值
def _env(key: str, default: str) -> str:
    """读取环境变量，空字符串视为未设置。"""
    return os.environ.get(key, "").strip() or default


# 快速模式使用的模型
FAST_LLM_MODEL = _env("FAST_LLM_MODEL", "MiniMax-M2.7-highspeed")

# TradingAgents 完整模式使用的模型
DEFAULT_LLM_PROVIDER = _env("LLM_PROVIDER", "minimax")
DEFAULT_DEEP_THINK_LLM = _env("DEEP_THINK_LLM", "MiniMax-M2.7-highspeed")
DEFAULT_QUICK_THINK_LLM = _env("QUICK_THINK_LLM", "MiniMax-M2.7-highspeed")
DEFAULT_BACKEND_URL = _env("LLM_BACKEND_URL", "")


# ── 24h Ticker 缓存 ───────────────────────────────────────────────────────


def _get_cached_ticker(symbol: str) -> Optional[Dict[str, Any]]:
    """从本地缓存获取 24h ticker 数据（TTL=3分钟）。"""
    try:
        conn = sqlite3.connect(TICKER_CACHE_DB, timeout=5)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_price, price_change_pct, volume, quote_volume, high_24h, low_24h, update_time "
            "FROM ticker_24h_cache WHERE symbol = ?",
            (symbol,),
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        (
            last_price,
            price_change_pct,
            volume,
            quote_volume,
            high_24h,
            low_24h,
            update_time,
        ) = row
        age_seconds = time.time() - (update_time / 1000)
        if age_seconds > TICKER_CACHE_TTL_SECONDS:
            return None
        return {
            "symbol": symbol,
            "last_price": last_price,
            "price_change_pct": price_change_pct,
            "volume": volume,
            "quote_volume": quote_volume,
            "high_24h": high_24h,
            "low_24h": low_24h,
        }
    except Exception:
        return None


def _save_ticker_to_cache(ticker: Dict[str, Any]) -> None:
    """将 24h ticker 数据写入本地缓存。"""
    try:
        conn = sqlite3.connect(TICKER_CACHE_DB, timeout=5)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO ticker_24h_cache "
            "(symbol, last_price, price_change_pct, volume, quote_volume, high_24h, low_24h, update_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker["symbol"],
                ticker["last_price"],
                ticker["price_change_pct"],
                ticker["volume"],
                ticker["quote_volume"],
                ticker["high_24h"],
                ticker["low_24h"],
                int(time.time() * 1000),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def preload_ticker_cache() -> bool:
    """预加载全市场 USDT 交易对 24h ticker 数据到缓存。"""
    log_func = logging.getLogger(__name__)
    try:
        log_func.info("开始预加载 24h ticker 缓存...")
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=30)
        all_tickers = r.json()
        usdt_tickers = [t for t in all_tickers if t["symbol"].endswith("USDT")]
        log_func.info(f"获取到 {len(usdt_tickers)} 个 USDT 交易对")

        conn = sqlite3.connect(TICKER_CACHE_DB, timeout=10)
        cursor = conn.cursor()
        for t in usdt_tickers:
            try:
                cursor.execute(
                    "INSERT OR REPLACE INTO ticker_24h_cache "
                    "(symbol, last_price, price_change_pct, volume, quote_volume, high_24h, low_24h, update_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        t["symbol"],
                        float(t["lastPrice"]),
                        float(t["priceChangePercent"]),
                        float(t["volume"]),
                        float(t["quoteVolume"]),
                        float(t["highPrice"]),
                        float(t["lowPrice"]),
                        int(time.time() * 1000),
                    ),
                )
            except Exception:
                pass
        conn.commit()
        conn.close()
        log_func.info(
            f"预加载完成: {len(usdt_tickers)} 个 USDT 交易对 ticker 数据已写入缓存"
        )
        return True
    except Exception as e:
        log_func.warning(f"预加载 ticker 缓存失败: {e}")
        return False


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


def _build_strategy_guide(market_data: dict) -> str:
    """根据扫描层数据判断策略类型，生成专属评估指引。"""
    direction = market_data.get("signal_direction", "")

    if market_data.get("oversold_score") is not None:
        return (
            "策略类型：超跌反弹做多\n"
            "交易逻辑：币种经历深度下跌后出现超卖信号，预期均值回归反弹。\n"
            "核心确认信号（满足越多分数越高）：\n"
            "  1. RSI 处于超卖区（< 30 强信号，30-40 中等）\n"
            "  2. 资金费率为负值（空头拥挤，反弹动力）\n"
            "  3. MACD 底背离或 KDJ 低位金叉（动能转向）\n"
            "  4. 布林带跌破下轨（极端超卖）\n"
            "  5. 连续下跌多根 K 线（恐慌释放充分）\n"
            "评分指引：\n"
            "  - RSI 超卖 + 任意一个其他信号 → 给 long\n"
            "  - 资金费率极端负值（< -0.1%）本身就是强信号 → 给 long\n"
            "  - 仅轻微超卖且无其他确认 → 给 hold\n"
            "风险因素（出现则降分）：\n"
            "  - 大盘（BTC）处于下跌趋势，系统性风险高\n"
            "  - 跌幅过深（>40%），可能是基本面恶化而非超卖"
        )

    if market_data.get("overbought_score") is not None:
        return (
            "策略类型：超买摸顶做空\n"
            "交易逻辑：币种短期暴涨后出现超买信号，预期回调。\n"
            "核心确认信号（满足越多分数越高）：\n"
            "  1. RSI 处于超买区（> 80 强信号，75-80 中等）\n"
            "  2. 量价背离（价格创新高但成交量萎缩，上涨动能衰竭）\n"
            "  3. MACD 顶背离或 KDJ 高位死叉\n"
            "  4. 连续上涨多根 K 线（涨幅过快需要回调）\n"
            "  5. 资金费率极端正值（多头拥挤，做空有费率收益）\n"
            "评分指引：\n"
            "  - RSI 超买 + 量价背离 → 给 short\n"
            "  - 连续暴涨 + 任意一个超买信号 → 给 short\n"
            "  - 仅轻微超买且趋势仍强 → 给 hold\n"
            "风险因素（出现则降分）：\n"
            "  - 轧空风险：成交量低但持仓量高，容易被逼空\n"
            "  - 强势趋势中的回调做空胜率极低\n"
            "  - 价格已从高点回落超过 5%，做空最佳时机已过"
        )

    if market_data.get("reversal_score") is not None:
        return (
            "策略类型：底部趋势反转做多\n"
            "交易逻辑：币种在底部区域出现趋势反转信号，预期新一轮上涨。\n"
            "核心确认信号（满足越多分数越高）：\n"
            "  1. 底部放量（成交量突增 ≥ 3x，资金入场）— 最重要\n"
            "  2. 价格企稳（不再创新低 + 波动收窄）\n"
            "  3. 均线拐头向上（EMA5 上穿 EMA10 金叉，或 EMA5 拐头）\n"
            "  4. 资金费率从负值回归正常（空头平仓信号）\n"
            "  5. MACD 零轴下方金叉或底背离\n"
            "评分指引：\n"
            "  - 放量 + 企稳 + 任意一个其他信号 → 给 long\n"
            "  - 仅放量但企稳不明显 → 给 long（放量是最核心信号）\n"
            "  - 无放量 → 给 hold\n"
            "风险因素（出现则降分）：\n"
            "  - 大盘（BTC）处于恐慌下跌，系统性风险高\n"
            "  - 跌幅过深（>50%），可能是基本面恶化"
        )

    # 通用兜底
    dir_text = (
        "做多"
        if direction == "long"
        else "做空"
        if direction == "short"
        else "未知方向"
    )
    return (
        f"策略类型：通用量化筛选（{dir_text}）\n"
        f"请综合技术指标和量化信号评估该方向的胜率和风险。"
    )


def _call_fast_llm(prompt: str, model: Optional[str] = None) -> str:
    """单次 LLM API 调用，返回文本响应。

    根据 LLM_PROVIDER 环境变量选择调用路径，支持：
    - minimax（默认）
    - zhipu（智谱 GLM）
    - google（Gemini）
    - 其他 OpenAI 兼容 provider（openai/qwen/xai/openrouter/ollama）

    Args:
        prompt: 提示词
        model: 模型名称，None 则使用 FAST_LLM_MODEL 环境变量配置
    """
    model = model or FAST_LLM_MODEL
    provider = DEFAULT_LLM_PROVIDER

    # ── OpenAI 兼容接口（minimax / zhipu / openai / qwen / xai / openrouter / ollama）
    # 这些 provider 都走统一的 chat/completions 端点
    _OPENAI_COMPAT_PROVIDERS = {
        "minimax": ("MINIMAX_API_KEY", "https://api.minimaxi.com/v1"),
        "zhipu": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4"),
        "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
        "qwen": ("QWEN_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "xai": ("XAI_API_KEY", "https://api.x.ai/v1"),
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        "ollama": (None, "http://localhost:11434/v1"),
    }

    if provider in _OPENAI_COMPAT_PROVIDERS:
        env_key, default_url = _OPENAI_COMPAT_PROVIDERS[provider]
        api_key = os.environ.get(env_key, "").strip() if env_key else ""
        base_url = DEFAULT_BACKEND_URL or default_url

        if env_key and not api_key:
            raise ValueError(f"快速模式需要 {env_key}，但未设置")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 注意：reasoning/thinking 模型（MiniMax-M2/M2.7、DeepSeek-R1 等）
        # 的输出会先写 <think>...</think> 推理块再给答案，占用 tokens。
        # 默认 max_tokens 常为 1024，对 reasoning 模型会导致答案被截断在思考
        # 块内（表现为解析 JSON 失败），因此统一提升到 2048。
        # MiniMax API 不认 "minimax/" 前缀，需要剥离
        api_model = (
            model.split("/", 1)[1] if provider == "minimax" and "/" in model else model
        )

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": api_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                "temperature": 0.3,
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # ── Google Gemini（独立 SDK）
    if provider == "google":
        google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not google_key:
            raise ValueError("快速模式需要 GOOGLE_API_KEY，但未设置")
        from google.genai import Client
        from google.genai import types as genai_types

        client = Client(api_key=google_key)
        # Google SDK 不接受 "google/" 前缀，直接剥离
        sdk_model = model.split("/", 1)[1] if model.startswith("google/") else model
        response = client.models.generate_content(
            model=sdk_model,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
        )
        return response.text

    # ── Anthropic（独立 SDK）
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ValueError("快速模式需要 ANTHROPIC_API_KEY，但未设置")
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    raise ValueError(f"快速模式不支持的 LLM_PROVIDER: {provider}")


def _extract_json(text: str) -> dict:
    """
    从 LLM 返回的文本中提取第一个 JSON 对象。

    处理常见情况：
    - 纯 JSON
    - JSON 前后有解释文字
    - markdown code fence 包裹
    - 多行格式化 JSON
    - reasoning 模型的 <think>...</think> 推理块（含未闭合情况）
    """
    original = text

    # 1. 剥离已闭合的 <think>...</think> 块（含多行）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 2. 未闭合的 <think> 块：从 <think> 位置起整段删除
    # （发生于 reasoning 模型思考过长占满 max_tokens 被截断）
    m = re.search(r"<think>", text, flags=re.IGNORECASE)
    if m:
        text = text[: m.start()]

    # 3. 去掉 markdown code fence
    text = re.sub(r"```(?:json)?\s*", "", text)

    # 4. 扫描第一个平衡的 { ... } 块（支持嵌套）
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
                return json.loads(text[start : i + 1])

    # 区分错误原因，给出可操作的错误信息
    if "<think>" in original.lower():
        raise ValueError(
            "LLM 响应被截断在 <think> 思考块内，未输出 JSON。"
            "建议提高 max_tokens 或切换到非 reasoning 模型。原始前 200 字: "
            f"{original[:200]}"
        )
    raise ValueError(f"未找到有效 JSON 对象: {original[:200]}")


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


def _fmt_prompt_value(value: Any, suffix: str = "") -> str:
    """格式化 prompt 字段，缺失值统一展示为 N/A。"""
    if value is None or value == "":
        return "N/A"
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"{value}{suffix}"


def _build_fast_market_context(market_data: Dict[str, Any]) -> str:
    """构造 fast 模式使用的市场环境上下文。"""
    regime = market_data.get("market_regime") or {}
    score_adjustment = regime.get("score_adjustment")
    if score_adjustment is None:
        score_adjustment = market_data.get("market_score_adjustment")
    lines = [
        f"- 市场状态: {_fmt_prompt_value(regime.get('status') or market_data.get('market_regime_status'))}",
        f"- 市场原因: {_fmt_prompt_value(regime.get('reason'))}",
        f"- 4h广度: {_fmt_prompt_value(regime.get('breadth_pct_4h'), '%')} / "
        f"24h广度: {_fmt_prompt_value(regime.get('breadth_pct_24h'), '%')} / "
        f"主流4h广度: {_fmt_prompt_value(regime.get('major_breadth_pct_4h'), '%')}",
        f"- 广度样本: {_fmt_prompt_value(regime.get('breadth_sample_size'))} / "
        f"主流样本: {_fmt_prompt_value(regime.get('major_breadth_sample_size'))}",
        f"- BTC 4h: close={_fmt_prompt_value(regime.get('btc_last_close'))}, "
        f"EMA5={_fmt_prompt_value(regime.get('btc_ema5'))}, "
        f"EMA20={_fmt_prompt_value(regime.get('btc_ema20'))}",
        f"- BTC 实时: price={_fmt_prompt_value(regime.get('btc_realtime_price'))}, "
        f"距EMA20={_fmt_prompt_value(regime.get('btc_realtime_vs_ema20_pct'), '%')}, "
        f"实时修复={_fmt_prompt_value(regime.get('btc_realtime_recovery'))}",
        f"- BTC 1h: EMA5={_fmt_prompt_value(regime.get('btc_1h_ema5'))}, "
        f"EMA20={_fmt_prompt_value(regime.get('btc_1h_ema20'))}, "
        f"1h修复={_fmt_prompt_value(regime.get('btc_1h_recovery'))}, "
        f"无新低={_fmt_prompt_value(regime.get('btc_1h_no_new_low'))}",
        f"- 市场门槛调整: {_fmt_prompt_value(score_adjustment)}",
        f"- 有效扫描门槛: oversold={_fmt_prompt_value(market_data.get('effective_min_oversold_score'))}, "
        f"reversal={_fmt_prompt_value(market_data.get('effective_min_reversal_score'))}, "
        f"overbought={_fmt_prompt_value(market_data.get('effective_min_overbought_score'))}",
        f"- 波动率动作: {_fmt_prompt_value(market_data.get('volatility_action'))}, "
        f"ATR过滤={_fmt_prompt_value(market_data.get('atr_filter_pct'), '%')}",
    ]
    return "\n".join(lines)


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

        # 1. 尝试从缓存获取 24h ticker（TTL=3分钟）
        ticker = _get_cached_ticker(symbol)
        cache_hit = ticker is not None

        # 2. 缓存未命中，尝试调用 Binance API
        if ticker is None:
            try:
                ticker = _fetch_binance_ticker(symbol)
                if ticker:
                    _save_ticker_to_cache(ticker)
            except Exception as e:
                log.warning(
                    f"[FastAnalyzer] {symbol} Binance 行情获取失败: {e}, 使用上游 market_data 兜底"
                )
                ticker = {
                    "symbol": symbol,
                    "last_price": market_data.get("last_price", 0),
                    "price_change_pct": market_data.get("price_change_pct", 0),
                    "volume": market_data.get("volume", 0),
                    "quote_volume": market_data.get(
                        "quote_volume_24h", market_data.get("quote_volume", 0)
                    ),
                    "high_24h": market_data.get("high_24h", 0),
                    "low_24h": market_data.get("low_24h", 0),
                }
                if not ticker["last_price"]:
                    return {
                        "rating_score": FAST_HOLD_RATING_SCORE,
                        "signal": "hold",
                        "confidence": 30.0,
                        "comment": f"[快速模式] Binance 行情获取失败且无兜底数据: {e}",
                    }

        log.info(
            f"[FastAnalyzer] {symbol} 行情获取成功{' (缓存)' if cache_hit else ''}: {ticker['last_price']}, "
            f"24h {ticker['price_change_pct']:+.2f}%"
        )

        signal_dir_map = {
            "long": "抄底做多 (Long)",
            "short": "摸顶做空 (Short)",
            "hold": "观望 (Hold)",
        }

        expected_dir = market_data.get("signal_direction", "")
        direction_text = (
            signal_dir_map.get(expected_dir, expected_dir) if expected_dir else "未知"
        )

        deep_indicators = ""
        if market_data.get("rsi") is not None:
            deep_indicators += f"- RSI: {market_data['rsi']}\n"
        if market_data.get("bias_pct") is not None:
            deep_indicators += f"- 均线乖离率 (BIAS20): {market_data['bias_pct']}%\n"
        if market_data.get("atr_pct") is not None:
            deep_indicators += f"- ATR 波动率: {market_data['atr_pct']}%\n"
        if market_data.get("kdj_j") is not None:
            deep_indicators += f"- KDJ J值: {market_data['kdj_j']}\n"
        if market_data.get("funding_rate") is not None:
            deep_indicators += f"- 资金费率: {market_data['funding_rate']}%\n"
        if market_data.get("macd_divergence") is not None:
            deep_indicators += (
                f"- MACD背离: {'是' if market_data['macd_divergence'] else '否'}\n"
            )
        if market_data.get("volume_surge") is not None:
            deep_indicators += f"- 放量倍数: {market_data['volume_surge']}x\n"
        elif market_data.get("volume_surge_ratio") is not None:
            deep_indicators += f"- 放量倍数: {market_data['volume_surge_ratio']}x\n"
        if market_data.get("below_boll_lower"):
            deep_indicators += "- 布林带: 跌破下轨\n"
        if market_data.get("above_boll_upper"):
            deep_indicators += "- 布林带: 突破上轨\n"
        if (
            market_data.get("consecutive_down") is not None
            and market_data["consecutive_down"] > 0
        ):
            deep_indicators += f"- 连续下跌: {market_data['consecutive_down']}根\n"
        if (
            market_data.get("consecutive_up") is not None
            and market_data["consecutive_up"] > 0
        ):
            deep_indicators += f"- 连续上涨: {market_data['consecutive_up']}根\n"
        if market_data.get("drop_pct") is not None:
            deep_indicators += f"- 近期累计跌幅: {market_data['drop_pct']}%\n"
        if market_data.get("rally_pct") is not None:
            deep_indicators += f"- 近期累计涨幅: {market_data['rally_pct']}%\n"
        if market_data.get("distance_from_high_pct") is not None:
            deep_indicators += (
                f"- 距近期高点: {market_data['distance_from_high_pct']}%\n"
            )
        if market_data.get("rise_from_low_pct") is not None:
            deep_indicators += (
                f"- 距近期低点涨幅: {market_data['rise_from_low_pct']}%\n"
            )

        # 反转扫描层子维度详情（均线、企稳、MACD 等）
        reversal_details = ""
        if market_data.get("ma_turn_detail"):
            reversal_details += f"- 均线状态: {market_data['ma_turn_detail']}（{market_data.get('ma_turn_score', 0)}分）\n"
        if market_data.get("price_stable_score") is not None:
            reversal_details += (
                f"- 价格企稳评分: {market_data['price_stable_score']}分\n"
            )
        if (
            market_data.get("funding_reversal_score") is not None
            and market_data["funding_reversal_score"] > 0
        ):
            reversal_details += (
                f"- 费率回归评分: {market_data['funding_reversal_score']}分\n"
            )
        if market_data.get("macd_detail"):
            reversal_details += f"- MACD状态: {market_data['macd_detail']}（{market_data.get('macd_reversal_score', 0)}分）\n"
        if market_data.get("dist_bottom_pct") is not None:
            reversal_details += f"- 距底部距离: {market_data['dist_bottom_pct']}%\n"
        if market_data.get("prior_drop_pct") is not None:
            reversal_details += f"- 前期跌幅: {market_data['prior_drop_pct']}%\n"
        if market_data.get("kdj_score") is not None and market_data["kdj_score"] > 0:
            reversal_details += f"- KDJ低位金叉: 是（{market_data['kdj_score']}分）\n"
        if (
            market_data.get("shadow_score") is not None
            and market_data["shadow_score"] > 0
        ):
            reversal_details += f"- 长下影线: 是（{market_data['shadow_score']}分）\n"

        # 扫描层综合评分和信号摘要
        scan_summary = ""
        if market_data.get("signal_score") is not None:
            scan_summary += f"- 通用信号评分: {market_data['signal_score']}/100\n"
        for score_key in ["oversold_score", "overbought_score", "reversal_score"]:
            if market_data.get(score_key) is not None:
                scan_summary += f"- 量化筛选评分: {market_data[score_key]}/100\n"
        if market_data.get("signal_details"):
            scan_summary += f"- 触发信号: {market_data['signal_details']}\n"

        # 根据策略类型生成专属评估指引
        strategy_guide = _build_strategy_guide(market_data)
        market_context = _build_fast_market_context(market_data)

        prompt = f"""你是一名专业的加密货币合约风控分析师。量化系统已经筛出候选信号，你的任务不是重新寻找交易方向，而是复核该信号能否放行。

请只基于输入数据判断，不要假设外部新闻或未提供信息。数据不足、市场环境冲突、BTC趋势不支持、波动率异常、信号质量临界时，必须倾向 hold。

【策略背景】
{strategy_guide}

【量化预期方向】
- expected_direction: {expected_dir or "N/A"}
- 方向含义: {direction_text}

【实时行情】
- 当前价格: {ticker["last_price"]} USDT
- 24h 涨跌幅: {ticker["price_change_pct"]:+.2f}%
- 24h 成交量: {ticker["quote_volume"] / 1e6:.1f}M USDT
- 24h 高点: {ticker["high_24h"]} / 低点: {ticker["low_24h"]}

【技术指标】
{deep_indicators if deep_indicators else "暂无"}

【市场环境】
{market_context}

【量化筛选结果】
{scan_summary if scan_summary else "暂无"}

{f"【反转子维度详情】{chr(10)}{reversal_details}" if reversal_details else ""}

硬性规则：
1. 只能返回 expected_direction 或 hold。不要输出与 expected_direction 相反的方向。
2. 只有当数据同时支持 expected_direction、市场环境不冲突、风险可控时，才返回 expected_direction。
3. 如果 expected_direction=long，但你认为更适合 short，返回 hold。
4. 如果 expected_direction=short，但你认为更适合 long，返回 hold。
5. 判断 BTC 与市场广度时必须区分方向：做多需要 BTC 1h/实时修复和广度支持；做空时 BTC 1h/实时修复、主流4h广度偏强属于追空风险。
6. 如果扫描分只略高于有效门槛，且存在任一中高风险，返回 hold。
7. 如果 ATR/波动率动作提示高风险，必须在 key_risks 中说明；风险不可控时返回 hold。
8. 如果数据缺失影响判断，返回 hold。

直接返回 JSON（不要解释）：
{{
  "signal": "<{expected_dir or 'long|short'}|hold>",
  "confidence": <0-100>,
  "risk_level": "<low|medium|high>",
  "veto": <true|false>,
  "veto_reason": "<如果 veto=true，说明阻断原因；否则为空字符串>",
  "key_risks": ["<主要风险，最多3条>"],
  "confirmation": "<放行或观望的核心依据>"
}}
"""

        # 从扫描层评分映射 rating_score（0-100 → 1-10）
        # 回测优化后映射（v2）：与各策略有效门槛对齐
        #   oversold_4h/1h 有效门槛 = 40，overbought_4h/1h 有效门槛 = 30，reversal_4h/1h 有效门槛 = 60
        #   扫描层 40 分 → 映射 6 分（Skill-2 门槛）→ LLM 确认后 7 分通过下单
        #   旧映射：45→6（扫描层 40-44 分的有效信号被 Skill-2 过滤掉）
        #   新映射：40→6, 50→7, 60→8, 70→9
        scan_score = None
        for key in ["oversold_score", "overbought_score", "reversal_score", "signal_score"]:
            val = market_data.get(key)
            if val is not None:
                scan_score = val
                break
        if scan_score is None:
            scan_score = 50

        if scan_score >= 70:
            mapped_rating = 9
        elif scan_score >= 60:
            mapped_rating = 8
        elif scan_score >= 50:
            mapped_rating = 7
        elif scan_score >= 40:
            mapped_rating = (
                6  # 与回测有效门槛对齐（oversold≥40, reversal≥60 → 此处≥40进入候选池）
            )
        elif scan_score >= 30:
            mapped_rating = 5
        elif scan_score >= 20:
            mapped_rating = 4
        else:
            mapped_rating = max(1, round(scan_score / 10))

        try:
            raw = _call_fast_llm(prompt)
            cleaned = _clean_llm_text(raw)
            result = _extract_json(cleaned)
            llm_signal = result.get("signal", "hold")
            if llm_signal not in {"long", "short", "hold"}:
                llm_signal = "hold"
            veto = result.get("veto")
            if veto is True:
                llm_signal = "hold"
            if expected_dir in {"long", "short"} and llm_signal not in {
                expected_dir,
                "hold",
            }:
                result["veto_reason"] = (
                    f"LLM输出方向 {llm_signal} 与预期方向 {expected_dir} 相反"
                )
                llm_signal = "hold"
            # LLM 明确确认扫描方向才允许通过；hold/无效输出一律降级。
            bonus = 1 if expected_dir and llm_signal == expected_dir else 0
            rating_score = (
                FAST_HOLD_RATING_SCORE
                if llm_signal == "hold"
                else min(10, mapped_rating + bonus)
            )
            confidence = result.get("confidence")
            if not isinstance(confidence, (int, float)):
                confidence = float(scan_score)
            result["signal"] = llm_signal
            result["rating_score"] = rating_score
            result["confidence"] = max(0.0, min(100.0, float(confidence)))
            risk_level = result.get("risk_level", "unknown")
            veto_reason = result.get("veto_reason", "")
            result["comment"] = (
                f"[快速模式] 扫描分={scan_score} LLM={llm_signal} risk={risk_level}"
                + (f" +1确认" if bonus else "")
                + (f" veto={veto_reason}" if veto_reason else "")
            )
            log.info(
                f"[FastAnalyzer] {symbol} 完成 {time.time() - t0:.1f}s: "
                f"score={result['rating_score']}(扫描{scan_score}{'+1' if bonus else ''}) "
                f"signal={llm_signal}"
            )
            return result
        except Exception as e:
            log.warning(f"[FastAnalyzer] {symbol} LLM 调用失败: {e}")
            return {
                "rating_score": FAST_HOLD_RATING_SCORE,
                "signal": "hold",
                "confidence": min(50.0, float(scan_score)),
                "comment": f"[快速模式] LLM失败，降级为hold: 扫描分={scan_score}: {e}",
            }

    log.info("[FastAnalyzer] 快速分析器已初始化（单次 LLM 调用）")
    return analyzer


# ── 标准 TradingAgents 分析器 ──────────────────────────────────────────────


def create_trading_agents_analyzer(
    llm_provider: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    backend_url: Optional[str] = None,
    max_debate_rounds: int = 1,
    fast_mode: bool = False,  # 默认使用完整 TradingAgents 多智能体分析
) -> callable:
    """
    创建可注入 TradingAgentsModule 的 analyzer 回调函数。

    所有模型参数支持三级配置（优先级从高到低）：
    1. 函数参数显式传入
    2. 环境变量（.env 文件）
    3. 内置默认值

    环境变量:
        LLM_PROVIDER:    LLM 提供商 (默认 minimax)
        DEEP_THINK_LLM:  复杂推理模型 (默认 MiniMax-M2.7-highspeed)
        QUICK_THINK_LLM: 快速任务模型 (默认 MiniMax-M2.7-highspeed)
        LLM_BACKEND_URL: API 端点 (minimax 自动推断)
        FAST_LLM_MODEL:  快速模式使用的模型

    参数:
        llm_provider: LLM 提供商，None 则读取环境变量
        deep_think_llm: 复杂推理模型，None 则读取环境变量
        quick_think_llm: 快速任务模型，None 则读取环境变量
        backend_url: API 端点，None 则自动推断
        max_debate_rounds: 多空辩论轮数（越多越准但越慢）
        fast_mode: True 则使用单次 LLM 快速分析

    返回:
        analyzer(symbol, market_data) -> dict
    """
    # 解析配置：函数参数 > 环境变量 > 默认值
    llm_provider = llm_provider or DEFAULT_LLM_PROVIDER
    deep_think_llm = deep_think_llm or DEFAULT_DEEP_THINK_LLM
    quick_think_llm = quick_think_llm or DEFAULT_QUICK_THINK_LLM
    if backend_url is None:
        backend_url = DEFAULT_BACKEND_URL or None
    log.info(
        f"[TradingAgentsAdapter] 配置解析完成: provider={llm_provider}, "
        f"deep={deep_think_llm}, quick={quick_think_llm}, "
        f"backend={backend_url}, fast_mode={fast_mode}"
    )

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

        log.info(
            f"TradingAgents 分析: {ticker} @ {analysis_date} (超时={PROPAGATE_TIMEOUT}s)"
        )

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

        log.info(
            f"TradingAgents 返回 decision type={type(decision)}, value={repr(decision)[:200]}"
        )
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
        result["comment"] = (
            _clean_llm_text(decision)[:500] if decision else "无分析结果"
        )
        # 保存完整分析报告到磁盘
        if final_state:
            from src.infra.report_store import save_analysis_report

            save_analysis_report(final_state, symbol, market="crypto")
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
