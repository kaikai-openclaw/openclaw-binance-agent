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
from typing import Any, Dict, List, Optional

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
        rate_limiter: Optional[RateLimiter] = None,
        proxy: Optional[str] = None,
        kline_cache: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or RateLimiter()
        self._kline_cache = kline_cache  # BinanceKlineCache 实例，可选
        self._session = requests.Session()
        import os
        _proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if _proxy:
            self._session.proxies = {"http": _proxy, "https": _proxy}

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """带限流和退避重试的 GET 请求。"""
        url = f"{self.base_url}{path}"
        last_err: Optional[Exception] = None

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

    def get_exchange_info(self) -> Dict[str, Any]:
        """获取交易所信息，用于过滤可交易状态的交易对。"""
        return self._get("/fapi/v1/exchangeInfo")

    def get_tickers_24hr(self) -> List[Dict[str, Any]]:
        """获取所有交易对的 24 小时行情。"""
        return self._get("/fapi/v1/ticker/24hr")

    def get_klines(
        self, symbol: str, interval: str = "4h", limit: int = 100
    ) -> List[list]:
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

    def get_klines_cached(
        self, symbol: str, interval: str = "4h", limit: int = 100,
    ) -> List[list]:
        """带本地缓存的 K 线获取。

        优先查本地 SQLite 缓存，命中则零网络请求。
        缓存不足时联网拉取并回写缓存。
        无缓存实例时退化为普通 get_klines。
        """
        if not self._kline_cache:
            return self.get_klines(symbol, interval, limit)

        # 先查缓存
        cached = self._kline_cache.query_as_lists(symbol, interval, limit)
        if len(cached) >= limit:
            return cached[-limit:]

        # 缓存不足，联网拉取
        klines = self.get_klines(symbol, interval, limit)
        if klines:
            self._kline_cache.upsert_from_raw(symbol, interval, klines)
        return klines

    def get_klines_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> List[list]:
        """按时间范围拉取 K 线，自动分页（每次最多 1500 条）。

        用于历史数据批量拉取，结果自动写入缓存。
        """
        all_klines: List[list] = []
        current_start = start_time

        while current_start < end_time:
            klines = self._get("/fapi/v1/klines", {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_time,
                "limit": 1500,
            })
            if not klines:
                break

            all_klines.extend(klines)

            # 下一页起始 = 最后一条的 open_time + 1ms
            last_open_time = int(klines[-1][0])
            if last_open_time <= current_start:
                break  # 防止死循环
            current_start = last_open_time + 1

            if len(klines) < 1500:
                break  # 已到末尾

        # 写入缓存
        if all_klines and self._kline_cache:
            self._kline_cache.upsert_from_raw(symbol, interval, all_klines)

        return all_klines

    def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取最新资金费率（premiumIndex 端点）。

        返回: {"symbol": "BTCUSDT", "lastFundingRate": "0.00010000", ...}
        极端负费率（< -0.1%）= 空头拥挤，反弹概率高。
        """
        try:
            return self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        except Exception as exc:
            log.warning("获取 %s 资金费率失败: %s", symbol, exc)
            return None

    def get_funding_rates_all(self) -> List[Dict[str, Any]]:
        """获取所有交易对的最新资金费率。"""
        try:
            return self._get("/fapi/v1/premiumIndex")
        except Exception as exc:
            log.warning("获取全市场资金费率失败: %s", exc)
            return []

    def get_open_interest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取当前持仓量。

        返回: {"symbol": "BTCUSDT", "openInterest": "12345.678", "time": ...}
        """
        try:
            return self._get("/fapi/v1/openInterest", {"symbol": symbol})
        except Exception as exc:
            log.warning("获取 %s 持仓量失败: %s", symbol, exc)
            return None
