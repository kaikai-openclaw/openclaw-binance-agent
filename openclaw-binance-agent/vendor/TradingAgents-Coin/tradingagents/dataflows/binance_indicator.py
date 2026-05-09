"""Technical indicators for Binance data, using stockstats."""

import os
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Annotated
from stockstats import wrap
from .binance_stock import _symbol_to_binance, _fetch_klines
from .config import get_config


def _load_binance_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Load Binance OHLCV data with caching, filtered up to curr_date."""
    binance_symbol = _symbol_to_binance(symbol)
    config = get_config()
    cache_dir = config.get("data_cache_dir", ".")
    os.makedirs(cache_dir, exist_ok=True)

    today = pd.Timestamp.today()
    start = today - pd.DateOffset(years=5)
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")
    cache_file = os.path.join(cache_dir, f"{binance_symbol}-Binance-data-{start_str}-{end_str}.csv")

    if os.path.exists(cache_file):
        data = pd.read_csv(cache_file)
    else:
        data = _fetch_klines(binance_symbol, start_str, end_str)
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


# Same indicator descriptions as yfinance — keep consistent
_INDICATOR_PARAMS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Overbought/oversold momentum.",
    "boll": "Bollinger Middle: 20 SMA basis.",
    "boll_ub": "Bollinger Upper Band.",
    "boll_lb": "Bollinger Lower Band.",
    "atr": "ATR: Average true range volatility.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index.",
}

# Alias map for common LLM mis-names → valid stockstats indicator names.
# Binance REST API has no technical indicator endpoint; all indicators are
# computed locally from klines via stockstats (<column>_<window>_<type>).
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
    "bollinger": "boll",
    "bollinger_bands": "boll",
    "boll_upper": "boll_ub",
    "boll_lower": "boll_lb",
    "macd_signal": "macds",
    "macd_histogram": "macdh",
    "macd_hist": "macdh",
    "money_flow": "mfi",
    "money_flow_index": "mfi",
}


def _resolve_indicator(indicator: str) -> str:
    """Resolve an indicator name to a valid stockstats key.

    Binance has no technical indicator API — we fetch klines via /api/v3/klines
    and compute indicators locally with stockstats.  The naming convention is
    ``<column>_<window>_<type>`` (e.g. ``close_50_sma``).

    LLM agents sometimes pass non-standard names like ``sma_ema`` or ``50_sma``.
    This function normalises them via an alias table and substring matching.
    """
    if indicator in _INDICATOR_PARAMS:
        return indicator

    key = indicator.lower().strip()

    # 1. Direct alias lookup
    if key in _INDICATOR_ALIASES:
        return _INDICATOR_ALIASES[key]

    # 2. Substring match: input contained in a supported name
    matched = [k for k in _INDICATOR_PARAMS if key in k]
    if matched:
        return matched[0]

    # 3. Reverse substring: a supported name fragment appears in the input
    matched = [k for k in _INDICATOR_PARAMS if k in key]
    if matched:
        return matched[0]

    raise ValueError(
        f"Indicator '{indicator}' not supported. "
        f"Choose from: {list(_INDICATOR_PARAMS.keys())}"
    )


def get_indicators(
    symbol: Annotated[str, "ticker symbol, e.g. BTC-USD, ETHUSDT"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicator values from Binance kline data."""
    indicator = _resolve_indicator(indicator)

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_dt - relativedelta(days=look_back_days)

    data = _load_binance_ohlcv(symbol, curr_date)
    if data.empty:
        return f"No Binance data available for {symbol}"

    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # trigger calculation

    idx = {row["Date"]: row[indicator] for _, row in df.iterrows()}

    lines = []
    dt = curr_dt
    while dt >= before:
        ds = dt.strftime("%Y-%m-%d")
        val = idx.get(ds, "N/A: Not a trading day")
        if pd.isna(val) if not isinstance(val, str) else False:
            val = "N/A"
        lines.append(f"{ds}: {val}")
        dt -= relativedelta(days=1)

    result = f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
    result += "\n".join(lines)
    result += f"\n\n{_INDICATOR_PARAMS.get(indicator, '')}"
    return result
