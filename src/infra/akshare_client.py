"""
AKShare A股公开市场数据客户端

封装 akshare 常用 A 股接口，用于 Skill-1A 量化数据采集。
数据源优先级（按稳定性排序）：
  实时行情: 腾讯 qt.gtimg.cn → 新浪 spot → 东方财富 spot_em
  K 线:     腾讯 hist_tx → 新浪 daily → 东方财富 hist

集成指数退避重试，兼容 Skill-1 的 client 协议。
"""

import logging
import re
import time
import requests as _requests
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_BACKOFF = [1, 2, 4, 8]
_MAX_RETRIES = 4


def _ensure_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        raise RuntimeError("akshare 未安装，请执行: pip install akshare")


def _normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
        if s.endswith("." + prefix):
            s = s[: -(len(prefix) + 1)]
    s = s.replace(".", "")
    if not s.isdigit() or len(s) != 6:
        raise ValueError(f"'{symbol}' 不是有效的 A 股代码（应为 6 位数字）")
    return s


def _symbol_exchange(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    elif code.startswith(("0", "3")):
        return "SZ"
    elif code.startswith(("8", "9")):
        return "BJ"
    return ""


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class AkshareClient:
    """A 股公开数据客户端，供 Skill-1A 注入使用。"""

    def __init__(self) -> None:
        self._ak = _ensure_akshare()

    def _retry(self, fn, desc: str = "akshare"):
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                return fn()
            except Exception as e:
                last_err = e
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning("[AkshareClient] %s 第%d次失败: %s, %ds后重试",
                            desc, attempt + 1, e, wait)
                time.sleep(wait)
        raise last_err

    def _try(self, fn, desc: str = "akshare"):
        """单次尝试，失败即抛出（用于有 fallback 的场景）。"""
        try:
            return fn()
        except Exception as e:
            log.warning("[AkshareClient] %s 失败: %s", desc, e)
            raise

    # ══════════════════════════════════════════════════════
    # 实时行情：腾讯 → 新浪 → 东方财富
    # ══════════════════════════════════════════════════════

    def get_spot_all(self) -> List[Dict[str, Any]]:
        """获取沪深 A 股全量实时行情。优先级：腾讯 → 新浪 → 东方财富。"""
        for name, fn in [
            ("腾讯", self._get_spot_tencent),
            ("新浪", self._get_spot_sina),
            ("东方财富", self._get_spot_em),
        ]:
            try:
                result = fn()
                if result:
                    log.info("[AkshareClient] 实时行情(%s): %d 只", name, len(result))
                    return result
            except Exception as e:
                log.warning("[AkshareClient] 实时行情(%s)失败: %s", name, e)
        log.warning("[AkshareClient] 所有实时行情接口均不可用")
        return []

    def _get_spot_tencent(self) -> List[Dict[str, Any]]:
        """腾讯 qt.gtimg.cn 批量实时行情。"""
        codes, code_name_map = self._get_code_list()
        results = []
        batch_size = 500
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            r = _requests.get(
                f"https://qt.gtimg.cn/q={','.join(batch)}",
                timeout=15, headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                results.extend(self._parse_tencent(r.text, code_name_map))
            if i > 0 and i % 2000 == 0:
                time.sleep(0.1)
        return results

    def _get_spot_sina(self) -> List[Dict[str, Any]]:
        """新浪 stock_zh_a_spot 实时行情。"""
        df = self._try(lambda: self._ak.stock_zh_a_spot(), "spot_sina")
        if df is None or df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            code_raw = str(row.get("代码", ""))
            code = code_raw[-6:] if len(code_raw) >= 6 else code_raw
            close = _safe_float(row.get("最新价"))
            prev_close = _safe_float(row.get("昨收"))
            if not close or close <= 0:
                continue
            high = _safe_float(row.get("最高")) or close
            low = _safe_float(row.get("最低")) or close
            change_pct = _safe_float(row.get("涨跌幅"))
            if change_pct is None and prev_close and prev_close > 0:
                change_pct = round((close - prev_close) / prev_close * 100, 2)
            amp = round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else None
            results.append(self._make_spot(code, str(row.get("名称", "")),
                close, change_pct, high, low,
                _safe_float(row.get("今开")), _safe_float(row.get("成交量")),
                _safe_float(row.get("成交额")), None, amp))
        return results

    def _get_spot_em(self) -> List[Dict[str, Any]]:
        """东方财富 stock_zh_a_spot_em 实时行情（最不稳定）。"""
        df = self._try(lambda: self._ak.stock_zh_a_spot_em(), "spot_em")
        if df is None or df.empty:
            return []
        col_map = {
            "代码": "symbol", "名称": "name", "最新价": "close",
            "涨跌幅": "change_pct", "成交量": "volume", "成交额": "amount",
            "最高": "high", "最低": "low", "今开": "open",
            "换手率": "turnover_rate", "振幅": "amplitude_pct",
            "市盈率-动态": "pe", "总市值": "total_mv",
        }
        df = df.rename(columns=col_map)
        keep = [v for v in col_map.values() if v in df.columns]
        df = df[keep]
        for col in ["close", "change_pct", "volume", "amount", "high", "low",
                     "open", "turnover_rate", "amplitude_pct", "pe", "total_mv"]:
            if col in df.columns:
                df[col] = df[col].apply(_safe_float)
        return df.to_dict(orient="records")

    def get_spot_by_hist(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """通过日线数据为指定个股构造行情快照（终极 fallback）。
        优先腾讯日线，失败用新浪，最后东方财富。
        """
        results = []
        end = datetime.now()
        start = end - timedelta(days=10)
        for sym in symbols:
            code = _normalize_symbol(sym)
            df = self._get_klines_any(code, 10)
            if df is None or df.empty or len(df) < 2:
                continue
            last, prev = df.iloc[-1], df.iloc[-2]
            close = _safe_float(last.get("close"))
            prev_close = _safe_float(prev.get("close"))
            if not close or not prev_close or prev_close == 0:
                continue
            high = _safe_float(last.get("high")) or close
            low = _safe_float(last.get("low")) or close
            change_pct = round((close - prev_close) / prev_close * 100, 2)
            amp = round((high - low) / prev_close * 100, 2)
            results.append(self._make_spot(code, "", close, change_pct, high, low,
                _safe_float(last.get("open")), _safe_float(last.get("volume")),
                _safe_float(last.get("amount")), _safe_float(last.get("turnover_rate")), amp))
        return results

    # ══════════════════════════════════════════════════════
    # K 线：腾讯 → 新浪 → 东方财富
    # ══════════════════════════════════════════════════════

    def get_klines(self, symbol: str, period: str = "daily", limit: int = 100) -> List[List]:
        """获取日线 K 线（前复权）。优先级：腾讯 → 新浪 → 东方财富。"""
        code = _normalize_symbol(symbol)
        df = self._get_klines_any(code, limit)
        if df is not None and not df.empty:
            return self._df_to_rows(df, limit)
        return []

    def _get_klines_any(self, code: str, limit: int):
        """按优先级尝试所有 K 线数据源，返回第一个成功的 DataFrame。"""
        for name, fn in [
            ("腾讯", lambda: self._klines_tx(code, limit)),
            ("新浪", lambda: self._klines_sina(code, limit)),
            ("东方财富", lambda: self._klines_em(code, limit)),
        ]:
            try:
                df = fn()
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                log.warning("[AkshareClient] K线(%s/%s)失败: %s", name, code, e)
        return None

    def _klines_tx(self, code: str, limit: int):
        """腾讯日线。"""
        exchange = _symbol_exchange(code).lower()
        end = datetime.now()
        start = end - timedelta(days=int(limit * 2))
        df = self._try(
            lambda: self._ak.stock_zh_a_hist_tx(
                symbol=f"{exchange}{code}",
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                adjust="qfq",
            ), f"klines_tx({code})")
        if df is not None and "volume" not in df.columns and "amount" in df.columns:
            df = df.rename(columns={"amount": "volume"})
        return df

    def _klines_sina(self, code: str, limit: int):
        """新浪日线。"""
        exchange = _symbol_exchange(code).lower()
        return self._try(
            lambda: self._ak.stock_zh_a_daily(symbol=f"{exchange}{code}", adjust="qfq"),
            f"klines_sina({code})")

    def _klines_em(self, code: str, limit: int):
        """东方财富日线（最不稳定）。"""
        end = datetime.now()
        start = end - timedelta(days=int(limit * 2))
        df = self._try(
            lambda: self._ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq"),
            f"klines_em({code})")
        if df is not None:
            col_map = {"日期": "date", "开盘": "open", "收盘": "close",
                       "最高": "high", "最低": "low", "成交量": "volume"}
            df = df.rename(columns=col_map)
        return df

    # ══════════════════════════════════════════════════════
    # 辅助方法
    # ══════════════════════════════════════════════════════

    def _get_code_list(self):
        """获取 A 股代码列表（腾讯格式），失败则用代码范围生成。"""
        codes, name_map = [], {}
        try:
            info_df = self._ak.stock_info_a_code_name()
            if info_df is not None and not info_df.empty:
                for _, row in info_df.iterrows():
                    code = str(row.get("code", ""))
                    name = str(row.get("name", ""))
                    if not code or len(code) != 6:
                        continue
                    ex = _symbol_exchange(code).lower()
                    if ex:
                        codes.append(f"{ex}{code}")
                        name_map[code] = name
        except Exception as e:
            log.warning("[AkshareClient] 获取股票列表失败: %s, 使用代码范围", e)
        if not codes:
            codes = self._generate_code_range()
        return codes, name_map

    @staticmethod
    def _generate_code_range() -> List[str]:
        codes = []
        for i in range(600000, 606000):
            codes.append(f"sh{i}")
        for i in range(688000, 690000):
            codes.append(f"sh{i}")
        for i in range(1, 4000):
            codes.append(f"sz{i:06d}")
        for i in range(300000, 302000):
            codes.append(f"sz{i}")
        return codes

    @staticmethod
    def _parse_tencent(text: str, name_map: dict) -> List[Dict[str, Any]]:
        """解析腾讯 qt.gtimg.cn 响应。"""
        results = []
        for line in text.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            match = re.search(r'"(.+)"', line)
            if not match:
                continue
            f = match.group(1).split("~")
            if len(f) < 50:
                continue
            code = f[2]
            close = _safe_float(f[3])
            prev_close = _safe_float(f[4])
            if not close or close <= 0:
                continue
            high = _safe_float(f[33]) or close
            low = _safe_float(f[34]) or close
            amount = _safe_float(f[37])
            if amount:
                amount *= 10000
            change_pct = _safe_float(f[32])
            if change_pct is None and prev_close and prev_close > 0:
                change_pct = round((close - prev_close) / prev_close * 100, 2)
            amp = round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else None
            results.append({
                "symbol": code, "name": f[1] if len(f) > 1 else name_map.get(code, ""),
                "close": close, "change_pct": change_pct,
                "volume": _safe_float(f[36]) or 0, "amount": amount or 0,
                "high": high, "low": low, "open": _safe_float(f[5]) or close,
                "turnover_rate": _safe_float(f[38]),
                "amplitude_pct": amp,
                "pe": _safe_float(f[39]) if len(f) > 39 else None,
                "total_mv": None,
            })
        return results

    @staticmethod
    def _make_spot(code, name, close, change_pct, high, low, open_p, volume, amount, turnover, amp):
        return {
            "symbol": code, "name": name, "close": close,
            "change_pct": change_pct, "volume": volume or 0, "amount": amount or 0,
            "high": high or close, "low": low or close, "open": open_p or close,
            "turnover_rate": turnover, "amplitude_pct": amp,
            "pe": None, "total_mv": None,
        }

    @staticmethod
    def _df_to_rows(df, limit: int) -> List[List]:
        df = df.tail(limit)
        return [[str(r.get("date", "")), float(r.get("open", 0)), float(r.get("high", 0)),
                 float(r.get("low", 0)), float(r.get("close", 0)), float(r.get("volume", 0))]
                for _, r in df.iterrows()]
