"""
A股历史数据管理 Skill（Data Provider）

底层数据基础设施，为下游分析/回测 Skill 提供标准化的 A 股历史行情数据。
核心能力：
  1. 本地 SQLite 缓存优先，增量联网拉取
  2. 股票代码强校验（sh./sz./bj. 前缀）
  3. 数据清洗：空值填补、字段标准化、时间正序
  4. 标准化 JSON 输出（status_code + meta_info + data）
  5. 接口防封控：内置延时重试

职责边界：仅负责"找数据、存数据、给数据"，不参与任何业务逻辑分析。

数据源：AkshareClient（akshare 公开接口，前复权/后复权/不复权）
缓存层：与 AkshareClient 共用 src.infra.kline_cache.KlineCache
"""

import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.infra.kline_cache import KlineCache
from src.infra.state_store import StateStore
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────
SYMBOL_PATTERN = re.compile(r"^(sh|sz|bj)\.[0-9]{6}$")

# API 调用间隔（秒），防封控
_API_CALL_INTERVAL = 0.3
_BACKOFF = [1, 2, 4, 8]
_MAX_RETRIES = 3


def _parse_symbol(symbol: str) -> Tuple[str, str]:
    """解析 'sh.600519' → ('sh', '600519')。不合法则抛 ValueError。"""
    if not SYMBOL_PATTERN.match(symbol):
        raise ValueError(
            f"Invalid symbol format. Expected prefix 'sh.', 'sz.', or 'bj.'. "
            f"Received: '{symbol}'"
        )
    parts = symbol.split(".")
    return parts[0], parts[1]


def _exchange_to_akshare_prefix(exchange: str) -> str:
    """sh → sh, sz → sz, bj → bj（akshare 格式）。"""
    return exchange.lower()


class SkillDataProvider(BaseSkill):
    """
    A 股历史数据管理 Skill。

    接收下游 Skill 的数据请求（股票代码、日期区间、复权方式），
    优先查本地缓存，缺失部分增量联网拉取，返回标准化 JSON。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        client: Any,
        cache_db_path: str = "data/kline_cache.db",
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill_data_provider"
        self._client = client
        self._cache = KlineCache(cache_db_path)
        self._last_api_call: float = 0.0

    def run(self, input_data: dict) -> dict:
        """主入口：解析请求 → 缓存检索 → 增量拉取 → 清洗 → 返回。"""
        symbol_raw = input_data.get("symbol", "")
        start_date = input_data.get("start_date", "")
        end_date = input_data.get("end_date", "")
        frequency = input_data.get("frequency", "daily")
        adjust = input_data.get("adjust", "qfq")

        # ── Step 1: 参数校验 ──
        try:
            exchange, code = _parse_symbol(symbol_raw)
        except ValueError as e:
            return self._error_response(400, str(e), symbol_raw, frequency, adjust)

        if not self._validate_date(start_date) or not self._validate_date(end_date):
            return self._error_response(
                400,
                f"Invalid date format. Expected YYYY-MM-DD. start={start_date}, end={end_date}",
                symbol_raw, frequency, adjust,
            )

        if start_date > end_date:
            return self._error_response(
                400,
                f"start_date ({start_date}) must be <= end_date ({end_date})",
                symbol_raw, frequency, adjust,
            )

        # ── Step 2: 缓存检索 ──
        # 缓存 key 使用纯 6 位代码（与 AkshareClient 一致）
        cached_rows = self._cache.query(code, adjust, start_date, end_date)
        cached_dates = {r["date"] for r in cached_rows}

        cache_range = self._cache.get_date_range(code, adjust)
        fully_cached = (
            cache_range is not None
            and cache_range[0] <= start_date
            and cache_range[1] >= end_date
        )

        data_source = "local_cache"

        if fully_cached and cached_rows:
            # 全命中缓存
            log.info("[data_provider] %s 全命中缓存, %d 行", symbol_raw, len(cached_rows))
        else:
            # ── Step 3: 增量联网拉取 ──
            api_rows = self._fetch_from_api(exchange, code, start_date, end_date, adjust)

            if api_rows is None:
                # API 完全失败，降级返回已有缓存
                if cached_rows:
                    log.warning("[data_provider] %s API失败，降级返回缓存数据", symbol_raw)
                    data_source = "local_cache"
                else:
                    return self._error_response(
                        404,
                        f"No data available for {symbol_raw} in [{start_date}, {end_date}]",
                        symbol_raw, frequency, adjust,
                    )
            else:
                # 写入缓存
                new_rows = [r for r in api_rows if r["date"] not in cached_dates]
                if new_rows:
                    self._cache.upsert_batch(code, adjust, new_rows)
                    log.info("[data_provider] %s 缓存新增 %d 行", symbol_raw, len(new_rows))

                # 重新从缓存读取完整数据（保证一致性）
                cached_rows = self._cache.query(code, adjust, start_date, end_date)

                if not cached_rows and not api_rows:
                    return self._error_response(
                        404,
                        f"No data available for {symbol_raw} in [{start_date}, {end_date}]",
                        symbol_raw, frequency, adjust,
                    )

                # 如果缓存为空但 API 有数据（可能日期范围不匹配），直接用 API 数据
                if not cached_rows and api_rows:
                    cached_rows = sorted(api_rows, key=lambda r: r["date"])

                data_source = "mixed" if cache_range else "api"

        # ── Step 4: 数据清洗与标准化 ──
        clean_data = self._clean_and_validate(cached_rows)

        if not clean_data:
            return self._error_response(
                404,
                f"No valid data for {symbol_raw} after cleaning",
                symbol_raw, frequency, adjust,
            )

        # 尝试获取股票名称
        stock_name = self._get_stock_name(code)

        return {
            "status_code": 200,
            "message": "success",
            "meta_info": {
                "symbol": symbol_raw,
                "name": stock_name,
                "frequency": frequency,
                "adjust": adjust,
                "data_source": data_source,
                "row_count": len(clean_data),
            },
            "data": clean_data,
        }

    # ── API 拉取 ─────────────────────────────────────────

    def _fetch_from_api(
        self, exchange: str, code: str, start_date: str, end_date: str, adjust: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """通过 AkshareClient 拉取 K 线数据，带重试和防封控。"""
        ak_symbol = f"{exchange}{code}"
        adjust_map = {"qfq": "qfq", "hfq": "hfq", "none": ""}

        for attempt in range(_MAX_RETRIES):
            try:
                # 防封控：控制调用间隔
                self._throttle()

                ak = self._client._ak
                # 优先东方财富 hist（支持 adjust 参数）
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust=adjust_map.get(adjust, "qfq"),
                )

                if df is not None and not df.empty:
                    return self._df_to_standard_rows(df)

                # fallback: 腾讯日线
                df = ak.stock_zh_a_hist_tx(
                    symbol=ak_symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust_map.get(adjust, "qfq"),
                )
                if df is not None and not df.empty:
                    return self._df_to_standard_rows(df)

                return []

            except Exception as e:
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning(
                    "[data_provider] API第%d次失败(%s): %s, %ds后重试",
                    attempt + 1, code, e, wait,
                )
                time.sleep(wait)

        log.error("[data_provider] API全部失败: %s", code)
        return None

    def _throttle(self) -> None:
        """API 调用间隔控制。"""
        now = time.monotonic()
        elapsed = now - self._last_api_call
        if elapsed < _API_CALL_INTERVAL:
            time.sleep(_API_CALL_INTERVAL - elapsed)
        self._last_api_call = time.monotonic()

    @staticmethod
    def _df_to_standard_rows(df) -> List[Dict[str, Any]]:
        """将 akshare DataFrame 转为标准化行列表。"""
        # 统一列名映射
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
        }
        df = df.rename(columns=col_map)

        rows = []
        for _, r in df.iterrows():
            date_val = r.get("date", "")
            if hasattr(date_val, "strftime"):
                date_val = date_val.strftime("%Y-%m-%d")
            else:
                date_val = str(date_val)[:10]

            open_v = _safe_float(r.get("open"))
            high_v = _safe_float(r.get("high"))
            low_v = _safe_float(r.get("low"))
            close_v = _safe_float(r.get("close"))
            volume_v = _safe_int(r.get("volume"))
            amount_v = _safe_float(r.get("amount")) or 0.0

            if close_v is None or close_v <= 0:
                continue

            rows.append({
                "date": date_val,
                "open": open_v or close_v,
                "high": high_v or close_v,
                "low": low_v or close_v,
                "close": close_v,
                "volume": volume_v or 0,
                "amount": amount_v,
            })
        return rows

    # ── 数据清洗 ─────────────────────────────────────────

    @staticmethod
    def _clean_and_validate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清洗数据：去重、排序、校验 close 在 [low, high] 区间、volume >= 0。"""
        seen_dates = set()
        clean = []
        for r in rows:
            d = r["date"]
            if d in seen_dates:
                continue
            seen_dates.add(d)

            # 校验 close 在 [low, high]
            if r["high"] > 0 and r["low"] > 0:
                if r["close"] > r["high"]:
                    r["high"] = r["close"]
                if r["close"] < r["low"]:
                    r["low"] = r["close"]

            # volume 不得为负
            if r["volume"] < 0:
                r["volume"] = 0

            clean.append(r)

        # 按日期正序
        clean.sort(key=lambda x: x["date"])
        return clean

    # ── 辅助方法 ─────────────────────────────────────────

    def _get_stock_name(self, code: str) -> str:
        """尽力获取股票名称，失败返回空字符串。"""
        try:
            ak = self._client._ak
            info = ak.stock_individual_info_em(symbol=code)
            if info is not None and not info.empty:
                name_row = info[info["item"] == "股票简称"]
                if not name_row.empty:
                    return str(name_row.iloc[0]["value"])
        except Exception:
            pass
        return ""

    @staticmethod
    def _validate_date(date_str: str) -> bool:
        """校验日期格式 YYYY-MM-DD。"""
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    @staticmethod
    def _error_response(
        status_code: int, message: str, symbol: str,
        frequency: str = "daily", adjust: str = "qfq",
    ) -> dict:
        return {
            "status_code": status_code,
            "message": message,
            "meta_info": {
                "symbol": symbol,
                "name": "",
                "frequency": frequency,
                "adjust": adjust,
                "data_source": "none",
                "row_count": 0,
            },
            "data": [],
        }

    def close(self) -> None:
        """关闭缓存连接。"""
        self._cache.close()


# ── 工具函数 ─────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
        return v if not math.isnan(v) else None
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None
