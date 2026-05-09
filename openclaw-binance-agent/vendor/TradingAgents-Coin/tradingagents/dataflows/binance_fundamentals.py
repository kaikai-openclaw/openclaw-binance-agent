"""Binance crypto fundamentals — market data that substitutes traditional financial statements.

Crypto assets don't have balance sheets, income statements, or insider transactions.
These functions return crypto-specific equivalents or clear 'not applicable' messages.
"""

from datetime import datetime
from typing import Annotated
from .binance_client import binance_request, binance_futures_request, BinanceInvalidSymbolError
from .binance_stock import _symbol_to_binance

import logging
logger = logging.getLogger(__name__)


def _request_with_futures_fallback(spot_endpoint: str, futures_endpoint: str, params: dict) -> dict | list:
    """Try spot API first; if symbol not found, fall back to futures."""
    try:
        return binance_request(spot_endpoint, params)
    except BinanceInvalidSymbolError:
        logger.info(f"Symbol {params.get('symbol')} not on spot, trying futures for {futures_endpoint}")
        return binance_futures_request(futures_endpoint, params)


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol, e.g. BTC-USD, ETHUSDT"],
    curr_date: Annotated[str, "current date (not used)"] = None,
) -> str:
    """Get crypto market fundamentals from Binance (24h ticker + exchange info)."""
    try:
        binance_symbol = _symbol_to_binance(ticker)

        ticker_24h = _request_with_futures_fallback(
            "/api/v3/ticker/24hr", "/fapi/v1/ticker/24hr", {"symbol": binance_symbol}
        )
        info = _request_with_futures_fallback(
            "/api/v3/exchangeInfo", "/fapi/v1/exchangeInfo", {"symbol": binance_symbol}
        )

        symbol_info = {}
        if info and "symbols" in info and info["symbols"]:
            symbol_info = info["symbols"][0]

        fields = [
            ("Symbol", binance_symbol),
            ("Status", symbol_info.get("status")),
            ("Base Asset", symbol_info.get("baseAsset")),
            ("Quote Asset", symbol_info.get("quoteAsset")),
            ("Last Price", ticker_24h.get("lastPrice")),
            ("24h Price Change", ticker_24h.get("priceChange")),
            ("24h Price Change %", ticker_24h.get("priceChangePercent")),
            ("24h High", ticker_24h.get("highPrice")),
            ("24h Low", ticker_24h.get("lowPrice")),
            ("24h Volume (Base)", ticker_24h.get("volume")),
            ("24h Volume (Quote)", ticker_24h.get("quoteVolume")),
            ("Weighted Avg Price", ticker_24h.get("weightedAvgPrice")),
            ("Bid Price", ticker_24h.get("bidPrice")),
            ("Ask Price", ticker_24h.get("askPrice")),
            ("Open Price", ticker_24h.get("openPrice")),
            ("Number of Trades (24h)", ticker_24h.get("count")),
        ]

        lines = [f"{label}: {value}" for label, value in fields if value is not None]
        header = f"# Crypto Fundamentals for {binance_symbol}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + "\n".join(lines)

    except BinanceInvalidSymbolError:
        raise
    except Exception as e:
        return f"Error retrieving Binance fundamentals for {ticker}: {str(e)}"


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "frequency (not applicable for crypto)"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Crypto assets don't have balance sheets. Returns market depth as a proxy."""
    try:
        binance_symbol = _symbol_to_binance(ticker)
        depth = _request_with_futures_fallback(
            "/api/v3/depth", "/fapi/v1/depth", {"symbol": binance_symbol, "limit": 10}
        )

        lines = [f"# Order Book Depth for {binance_symbol} (balance sheet not applicable for crypto)\n"]
        lines.append("## Top 10 Bids (Buy Orders):")
        for price, qty in depth.get("bids", []):
            lines.append(f"  Price: {price}, Quantity: {qty}")
        lines.append("\n## Top 10 Asks (Sell Orders):")
        for price, qty in depth.get("asks", []):
            lines.append(f"  Price: {price}, Quantity: {qty}")
        return "\n".join(lines)

    except BinanceInvalidSymbolError:
        raise
    except Exception as e:
        return f"Error retrieving order book for {ticker}: {str(e)}"


def get_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "frequency (not applicable for crypto)"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Crypto assets don't have cash flow statements. Returns recent trade flow as a proxy."""
    try:
        binance_symbol = _symbol_to_binance(ticker)
        trades = _request_with_futures_fallback(
            "/api/v3/trades", "/fapi/v1/trades", {"symbol": binance_symbol, "limit": 20}
        )

        lines = [f"# Recent Trade Flow for {binance_symbol} (cash flow not applicable for crypto)\n"]
        for t in trades:
            ts = datetime.fromtimestamp(t["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            side = "SELL" if t.get("isBuyerMaker") else "BUY"
            lines.append(f"  {ts} | {side} | Price: {t['price']} | Qty: {t['qty']}")
        return "\n".join(lines)

    except BinanceInvalidSymbolError:
        raise
    except Exception as e:
        return f"Error retrieving trade flow for {ticker}: {str(e)}"


def get_income_statement(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "frequency (not applicable for crypto)"] = "quarterly",
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Crypto assets don't have income statements. Returns 24h trading summary as a proxy."""
    return get_fundamentals(ticker, curr_date)


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """Crypto assets don't have insider transactions in the traditional sense."""
    binance_symbol = _symbol_to_binance(ticker)
    return (
        f"# Insider Transactions for {binance_symbol}\n\n"
        "Insider transaction data is not applicable for cryptocurrency assets.\n"
        "Consider monitoring large wallet movements (whale tracking) via on-chain analytics instead."
    )
