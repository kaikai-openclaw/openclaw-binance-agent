"""AKShare A股技术指标计算模块。

通过 AKShare 获取日线数据后，使用 stockstats 本地计算技术指标。
与 binance_indicator.py 保持相同的指标体系和输出格式。
"""

import os
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Annotated
from stockstats import wrap
from .akshare_stock import _symbol_to_akshare, _ensure_akshare, _fetch_hist, _date_to_akshare
from .config import get_config


def _load_akshare_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """加载A股 OHLCV 数据（带缓存），并过滤到 curr_date 之前的数据。

    获取近5年的日线数据用于技术指标计算，确保长周期指标（如200日均线）有足够数据。
    """
    ak = _ensure_akshare()
    code = _symbol_to_akshare(symbol)
    config = get_config()
    cache_dir = config.get("data_cache_dir", ".")
    os.makedirs(cache_dir, exist_ok=True)

    today = pd.Timestamp.today()
    start = today - pd.DateOffset(years=5)
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")
    cache_file = os.path.join(cache_dir, f"{code}-AKShare-data-{start_str}-{end_str}.csv")

    if os.path.exists(cache_file):
        data = pd.read_csv(cache_file)
    else:
        data = _fetch_hist(ak, code, start_str, end_str)
        if not data.empty:
            data.to_csv(cache_file, index=False)

    if data.empty:
        return data

    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()
    data = data[data["Date"] <= pd.to_datetime(curr_date)]
    return data


# 支持的技术指标及其说明
_INDICATOR_PARAMS = {
    "close_50_sma": "50日简单移动平均线：中期趋势指标。",
    "close_200_sma": "200日简单移动平均线：长期趋势基准。",
    "close_10_ema": "10日指数移动平均线：短期灵敏均线。",
    "macd": "MACD：通过EMA差值衡量动量。",
    "macds": "MACD信号线：MACD的EMA平滑。",
    "macdh": "MACD柱状图：MACD与信号线的差值。",
    "rsi": "RSI相对强弱指标：超买/超卖动量。",
    "boll": "布林带中轨：20日SMA基准线。",
    "boll_ub": "布林带上轨。",
    "boll_lb": "布林带下轨。",
    "atr": "ATR平均真实波幅：波动率指标。",
    "vwma": "VWMA成交量加权移动平均线。",
    "mfi": "MFI资金流量指标。",
}

# 别名映射：将常见的非标准名称映射到 stockstats 标准指标名
_INDICATOR_ALIASES = {
    "sma": "close_50_sma",
    "ema": "close_10_ema",
    "50_sma": "close_50_sma",
    "200_sma": "close_200_sma",
    "10_ema": "close_10_ema",
    "sma_50": "close_50_sma",
    "sma_200": "close_200_sma",
    "ema_10": "close_10_ema",
    "sma_ema": "close_50_sma",
    "ema_sma": "close_10_ema",
    "moving_average": "close_50_sma",
    "均线": "close_50_sma",
    "bollinger": "boll",
    "bollinger_bands": "boll",
    "布林": "boll",
    "布林带": "boll",
    "boll_upper": "boll_ub",
    "boll_lower": "boll_lb",
    "macd_signal": "macds",
    "macd_histogram": "macdh",
    "macd_hist": "macdh",
    "money_flow": "mfi",
    "money_flow_index": "mfi",
    "资金流": "mfi",
}


def _resolve_indicator(indicator: str) -> str:
    """将指标名称解析为 stockstats 可识别的标准名称。

    支持三级匹配：
    1. 直接匹配已知指标
    2. 别名表查找
    3. 子串模糊匹配
    """
    if indicator in _INDICATOR_PARAMS:
        return indicator

    key = indicator.lower().strip()

    # 1. 别名查找
    if key in _INDICATOR_ALIASES:
        return _INDICATOR_ALIASES[key]

    # 2. 子串匹配：输入包含在某个已知指标名中
    matched = [k for k in _INDICATOR_PARAMS if key in k]
    if matched:
        return matched[0]

    # 3. 反向子串：某个已知指标名片段出现在输入中
    matched = [k for k in _INDICATOR_PARAMS if k in key]
    if matched:
        return matched[0]

    raise ValueError(
        f"不支持的指标 '{indicator}'，可选: {list(_INDICATOR_PARAMS.keys())}"
    )


def get_indicators(
    symbol: Annotated[str, "A股股票代码，如 600519、000001"],
    indicator: Annotated[str, "技术指标名称"],
    curr_date: Annotated[str, "当前交易日期，格式 YYYY-mm-dd"],
    look_back_days: Annotated[int, "回看天数"],
) -> str:
    """获取A股技术指标数据。

    从 AKShare 加载日线数据，通过 stockstats 本地计算指标值。
    """
    indicator = _resolve_indicator(indicator)

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_dt - relativedelta(days=look_back_days)

    data = _load_akshare_ohlcv(symbol, curr_date)
    if data.empty:
        return f"无法获取 {symbol} 的A股数据"

    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # 触发 stockstats 计算

    idx = {row["Date"]: row[indicator] for _, row in df.iterrows()}

    lines = []
    dt = curr_dt
    while dt >= before:
        ds = dt.strftime("%Y-%m-%d")
        val = idx.get(ds, "N/A: 非交易日")
        if pd.isna(val) if not isinstance(val, str) else False:
            val = "N/A"
        lines.append(f"{ds}: {val}")
        dt -= relativedelta(days=1)

    result = f"## {indicator} 指标值，区间 {before.strftime('%Y-%m-%d')} 至 {curr_date}:\n\n"
    result += "\n".join(lines)
    result += f"\n\n{_INDICATOR_PARAMS.get(indicator, '')}"
    return result
