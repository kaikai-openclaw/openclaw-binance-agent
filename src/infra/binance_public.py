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
_TIMEOUT = (5, 45)  # (connect_timeout, read_timeout)，大接口（如 ticker/24hr）响应慢时留足余量

# K 线周期 → 毫秒映射（用于 get_klines_cached 的时效校验）
# 1w/1M 因 UTC 对齐规则特殊（周一起始 / 月份不定长），这里不放入表内，
# 命中时直接退化为"每次联网"策略，保证正确性。
_INTERVAL_MS_MAP: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
}


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
        """带本地缓存 + 时效校验的 K 线获取。

        刷新策略（保证扫描拿到的一定是最新数据）：
          1. 计算当前 UTC 时间所属 K 线周期的 open_time（current_candle_open）。
          2. 若缓存中 max(open_time) < current_candle_open → 缓存陈旧 → 联网拉最新 limit 根并回写。
          3. 若缓存已覆盖到当前正在形成的那根 K 线，且条数足够 → 直接用缓存。
          4. 周期不在 _INTERVAL_MS_MAP 中（1w / 1M 等）→ 退化为"每次联网"。
          5. 无缓存实例 → 退化为普通 get_klines。
          6. 【关键修复】剔除当前未闭合 K 线，确保分析基于已确认的历史数据。
        """
        if not self._kline_cache:
            return self.get_klines(symbol, interval, limit)

        interval_ms = _INTERVAL_MS_MAP.get(interval)
        if interval_ms is None:
            klines = self.get_klines(symbol, interval, limit)
            if klines:
                self._kline_cache.upsert_from_raw(symbol, interval, klines)
            return klines

        now_ms = int(time.time() * 1000)
        current_candle_open = (now_ms // interval_ms) * interval_ms

        time_range = self._kline_cache.get_time_range(symbol, interval)
        max_cached_open = time_range[1] if time_range else None
        fresh = (max_cached_open is not None
                 and max_cached_open >= current_candle_open)

        if fresh:
            cached = self._kline_cache.query_as_lists(symbol, interval, limit)
            if len(cached) >= limit:
                result = cached[-limit:]
                # ── 剔除当前未闭合 K 线 ─────────────────────────────────────
                # 未闭合 K 线的 open_time >= current_candle_open
                # 趋势判断必须基于已确认 K 线，否则 MACD/KDJ 金叉随时变（re-painting）
                while (result and result[-1][0] >= current_candle_open
                       and len(result) > 1):
                    result = result[:-1]
                return result

        klines = self.get_klines(symbol, interval, limit)
        if klines:
            self._kline_cache.upsert_from_raw(symbol, interval, klines)
        # ── 剔除当前未闭合 K 线 ─────────────────────────────────────────────
        while (klines and klines[-1][0] >= current_candle_open
               and len(klines) > 1):
            klines = klines[:-1]
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

    def get_open_interest_hist(
        self, symbol: str, period: str = "1h", limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """获取历史持仓量统计（/futures/data/openInterestHist）。

        参数:
            symbol: 交易对（如 "BTCUSDT"）
            period: 统计周期（5m/15m/30m/1h/2h/4h/6h/12h/1d）
            limit: 返回条数，最大 500

        返回:
            [{"symbol": "BTCUSDT", "sumOpenInterest": "12345.678",
              "sumOpenInterestValue": "123456789.0", "timestamp": 1704067200000}, ...]

        注意: 仅最近 30 天数据可用，无需 API Key。
        """
        try:
            return self._get("/futures/data/openInterestHist", {
                "symbol": symbol,
                "period": period,
                "limit": limit,
            })
        except Exception as exc:
            log.warning("获取 %s 历史持仓量失败: %s", symbol, exc)
            return []

    def get_top_long_short_ratio(
        self, symbol: str, period: str = "1h", limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """获取大户多空比（/futures/data/topLongShortAccountRatio）。

        参数:
            symbol: 交易对
            period: 统计周期（5m/15m/30m/1h/2h/4h/6h/12h/1d）
            limit: 返回条数，最大 500

        返回:
            [{"symbol": "BTCUSDT", "longShortRatio": "1.5",
              "longAccount": "0.6", "shortAccount": "0.4", "timestamp": ...}, ...]
        """
        try:
            return self._get("/futures/data/topLongShortAccountRatio", {
                "symbol": symbol,
                "period": period,
                "limit": limit,
            })
        except Exception as exc:
            log.warning("获取 %s 大户多空比失败: %s", symbol, exc)
            return []
