"""Binance OHLCV (Kline) data fetching — spot market with futures fallback."""

import os
import logging
import pandas as pd
from datetime import datetime
from typing import Annotated
from .binance_client import (
    binance_request,
    binance_futures_request,
    BinanceInvalidSymbolError,
)
from .config import get_config

logger = logging.getLogger(__name__)


def _symbol_to_binance(symbol: str) -> str:
    """Convert common ticker formats to Binance symbol.

    Accepts: BTC-USD, BTCUSD, BTCUSDT, BTC -> BTCUSDT
    """
    s = symbol.upper().replace("-", "")
    if s.endswith("USD") and not s.endswith("USDT"):
        s = s + "T"
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s


def _date_to_ms(date_str: str) -> int:
    """Convert YYYY-MM-DD to millisecond timestamp."""
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)


def get_stock_data(
    symbol: Annotated[str, "ticker symbol, e.g. BTC-USD, ETHUSDT, SOL, HYPE"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch OHLCV kline data from Binance (spot, then futures fallback), with CSV caching."""
    binance_symbol = _symbol_to_binance(symbol)
    config = get_config()
    cache_dir = config.get("data_cache_dir", ".")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{binance_symbol}-Binance-{start_date}-{end_date}.csv")

    if os.path.exists(cache_file):
        data = pd.read_csv(cache_file)
    else:
        data = _fetch_klines(binance_symbol, start_date, end_date)
        if data.empty:
            return f"No data found for '{symbol}' ({binance_symbol}) between {start_date} and {end_date}"
        data.to_csv(cache_file, index=False)

    csv_string = data.to_csv(index=False)
    header = f"# Binance kline data for {binance_symbol} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def _fetch_klines(binance_symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch daily klines from Binance spot; if symbol not found, try futures."""
    try:
        return _fetch_klines_from(binance_request, "/api/v3/klines", binance_symbol, start_date, end_date)
    except BinanceInvalidSymbolError:
        logger.info(f"{binance_symbol} not on spot market, trying futures")
        return _fetch_klines_from(binance_futures_request, "/fapi/v1/klines", binance_symbol, start_date, end_date)


def _fetch_klines_from(request_fn, endpoint: str, binance_symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch all daily klines using the given request function and endpoint, paginating as needed."""
    all_rows = []
    start_ms = _date_to_ms(start_date)
    end_ms = _date_to_ms(end_date)

    while start_ms < end_ms:
        params = {
            "symbol": binance_symbol,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        klines = request_fn(endpoint, params=params)
        if not klines:
            break
        all_rows.extend(klines)
        start_ms = klines[-1][0] + 1
        if len(klines) < 1000:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "Date", "Open", "High", "Low", "Close", "Volume",
        "Close_time", "Quote_volume", "Trades",
        "Taker_buy_base", "Taker_buy_quote", "Ignore",
    ])
    df["Date"] = pd.to_datetime(df["Date"], unit="ms")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    return df
