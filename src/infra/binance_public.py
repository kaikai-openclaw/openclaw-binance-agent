"""
Binance 公开市场数据客户端

封装 Binance 现货/合约公开 API（无需签名），用于 Skill-1 量化数据采集。
端点：
  - GET /fapi/v1/ticker/24hr — 24小时行情
  - GET /fapi/v1/klines — K线数据
  - GET /fapi/v1/exchangeInfo — 交易对信息（交易状态过滤）

集成 RateLimiter 限流，指数退避重试。
"""

import logging
import time
from typing import Any

import requests

from src.infra.rate_limiter import RateLimiter

log = logging.getLogger(__name__)

# 退避序列（秒）
_BACKOFF = [1, 2, 4, 8, 16]
_MAX_RETRIES = 5
_TIMEOUT = 15


class BinancePublicClient:
    """
    Binance U本位合约公开数据客户端（无需 API Key）。

    仅调用公开端点，不涉及任何签名或敏感信息。
    """

    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        rate_limiter: RateLimiter | None = None,
        proxy: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or RateLimiter()
        self._session = requests.Session()
        import os
        _proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if _proxy:
            self._session.proxies = {"http": _proxy, "https": _proxy}

    def _get(self, path: str, params: dict | None = None) -> Any:
        """带限流和退避重试的 GET 请求。"""
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                self.rate_limiter.acquire()
                resp = self._session.get(url, params=params or {}, timeout=_TIMEOUT)
                if resp.status_code == 429:
                    self.rate_limiter.pause(30)
                    time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_err = exc
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning("公开API %s 第%d次失败: %s, 等待%ds", path, attempt + 1, exc, wait)
                time.sleep(wait)

        raise last_err  # type: ignore[misc]

    # ── 公开端点 ──────────────────────────────────────────

    def get_exchange_info(self) -> dict[str, Any]:
        """获取交易所信息，用于过滤可交易状态的交易对。"""
        return self._get("/fapi/v1/exchangeInfo")

    def get_tickers_24hr(self) -> list[dict[str, Any]]:
        """获取所有交易对的 24 小时行情。"""
        return self._get("/fapi/v1/ticker/24hr")

    def get_klines(
        self, symbol: str, interval: str = "4h", limit: int = 100
    ) -> list[list]:
        """
        获取 K 线数据。

        参数:
            symbol: 交易对（如 "BTCUSDT"）
            interval: K线周期（1m/5m/15m/1h/4h/1d）
            limit: 返回条数，最大 1500

        返回:
            K线数组，每条: [open_time, open, high, low, close, volume, ...]
        """
        return self._get("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
