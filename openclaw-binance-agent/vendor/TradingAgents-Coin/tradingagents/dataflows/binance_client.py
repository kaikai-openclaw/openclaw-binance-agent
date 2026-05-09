"""Binance API client with retry logic and caching."""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime
from .config import get_config

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"


class BinanceRateLimitError(Exception):
    """Raised when Binance API returns a rate limit error (HTTP 429 or 418)."""
    pass


class BinanceInvalidSymbolError(Exception):
    """Raised when the symbol is not found on Binance."""
    pass


def binance_request(endpoint: str, params: dict = None, max_retries: int = 3, base_delay: float = 2.0) -> dict | list:
    """Make a Binance API request with exponential backoff on rate limits.

    - 400 (bad request / invalid symbol): raises BinanceInvalidSymbolError immediately (no retry).
    - 429/418 (rate limit): retries with backoff, then raises BinanceRateLimitError.
    - Other HTTP errors: retries with backoff, then raises.
    - Connection errors: retries with backoff, then raises.
    """
    url = f"{BINANCE_BASE_URL}{endpoint}"
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)

            # Invalid symbol / bad request — no point retrying
            if resp.status_code == 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                msg = body.get("msg", resp.text)
                raise BinanceInvalidSymbolError(
                    f"Binance rejected request ({resp.status_code}): {msg}"
                )

            # Rate limit — retry with backoff
            if resp.status_code in (429, 418):
                raise BinanceRateLimitError(f"Rate limited: {resp.status_code}")

            resp.raise_for_status()
            return resp.json()

        except (BinanceInvalidSymbolError, BinanceRateLimitError):
            raise  # propagate without retry

        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Binance request error: {e}, retrying in {delay:.0f}s")
                time.sleep(delay)
            else:
                raise


def binance_futures_request(endpoint: str, params: dict = None, max_retries: int = 3, base_delay: float = 2.0) -> dict | list:
    """Make a Binance Futures API request. Same error handling as binance_request."""
    url = f"{BINANCE_FUTURES_BASE_URL}{endpoint}"
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)

            if resp.status_code == 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                msg = body.get("msg", resp.text)
                raise BinanceInvalidSymbolError(
                    f"Binance Futures rejected request ({resp.status_code}): {msg}"
                )

            if resp.status_code in (429, 418):
                raise BinanceRateLimitError(f"Futures rate limited: {resp.status_code}")

            resp.raise_for_status()
            return resp.json()

        except (BinanceInvalidSymbolError, BinanceRateLimitError):
            raise

        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Binance Futures request error: {e}, retrying in {delay:.0f}s")
                time.sleep(delay)
            else:
                raise
