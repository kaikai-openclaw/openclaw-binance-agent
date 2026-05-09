"""AKShare A股 OHLCV 行情数据获取模块。

通过 AKShare 的 stock_zh_a_hist 接口获取沪深京A股日线数据（东方财富数据源）。
支持上海（60xxxx）、深圳（00xxxx/30xxxx）、北京（8xxxxx/9xxxxx）股票。
"""

import os
import logging
import time
import pandas as pd
from datetime import datetime
from typing import Annotated
from .config import get_config

logger = logging.getLogger(__name__)


class AKShareError(Exception):
    """AKShare 通用异常。"""
    pass


class AKShareInvalidSymbolError(AKShareError):
    """股票代码无效时抛出。"""
    pass


class AKShareRateLimitError(AKShareError):
    """触发频率限制时抛出。"""
    pass


def _ensure_akshare():
    """延迟导入 akshare，避免硬依赖。"""
    try:
        import akshare as ak
        return ak
    except ImportError:
        raise AKShareError("akshare 未安装，请执行: pip install akshare")


def _symbol_to_akshare(symbol: str) -> str:
    """将各种格式的股票代码转换为纯6位A股代码。

    支持输入: 600519, SH600519, 600519.SH, sh600519, 000001.SZ, sz000001
    输出: 600519, 000001 等纯数字代码
    """
    s = symbol.strip().upper()
    # 去除交易所前缀/后缀
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
        if s.endswith("." + prefix):
            s = s[: -(len(prefix) + 1)]
    s = s.replace(".", "")
    if not s.isdigit() or len(s) != 6:
        raise AKShareInvalidSymbolError(
            f"'{symbol}' 不是有效的A股代码，应为6位数字，如 600519、000001、300750"
        )
    return s


def _symbol_to_em(symbol: str) -> str:
    """将6位股票代码转换为东方财富格式（带交易所前缀）。

    规则: 6开头 -> SH, 0/3开头 -> SZ, 8/9开头 -> BJ
    """
    code = _symbol_to_akshare(symbol)
    if code.startswith("6"):
        return f"SH{code}"
    elif code.startswith(("0", "3")):
        return f"SZ{code}"
    elif code.startswith(("8", "9")):
        return f"BJ{code}"
    return code


def _akshare_retry(fn, max_retries=3, base_delay=2.0):
    """带指数退避的重试包装器，用于处理频率限制和网络连接问题。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err_str = str(e).lower()
            # 频率限制或网络连接问题都需要重试
            retryable = any(kw in err_str for kw in (
                "rate", "limit", "频繁", "ip", "连接",
                "connection", "timeout", "remote", "disconnected",
            ))
            if retryable:
                last_err = AKShareRateLimitError(str(e))
                delay = base_delay * (2 ** attempt)
                logger.warning(f"AKShare 请求失败，{delay}秒后重试（第{attempt + 1}次）: {type(e).__name__}")
                time.sleep(delay)
                continue
            raise
    raise last_err


def _date_to_akshare(date_str: str) -> str:
    """将 YYYY-MM-DD 格式转换为 AKShare 所需的 YYYYMMDD 格式。"""
    return date_str.replace("-", "")


def get_stock_data(
    symbol: Annotated[str, "A股股票代码，如 600519、000001、300750"],
    start_date: Annotated[str, "开始日期，格式 yyyy-mm-dd"],
    end_date: Annotated[str, "结束日期，格式 yyyy-mm-dd"],
) -> str:
    """获取A股日线 OHLCV 数据，优先走本地 SQLite 缓存。

    数据链路：SQLite 缓存 → CSV 文件缓存 → akshare 联网拉取。
    联网拉取的数据会自动回写 SQLite 缓存，供全局复用。
    """
    ak = _ensure_akshare()
    code = _symbol_to_akshare(symbol)
    config = get_config()
    cache_dir = config.get("data_cache_dir", ".")
    os.makedirs(cache_dir, exist_ok=True)

    # ── 优先查 SQLite 缓存（与 AkshareClient / SkillDataProvider 共享）──
    sqlite_data = _query_sqlite_cache(code, start_date, end_date)
    if sqlite_data is not None and not sqlite_data.empty:
        logger.info(f"[akshare_stock] SQLite 缓存命中 {code}: {len(sqlite_data)} 行")
        data = sqlite_data
    else:
        # ── fallback: CSV 文件缓存 ──
        cache_file = os.path.join(cache_dir, f"{code}-AKShare-{start_date}-{end_date}.csv")
        if os.path.exists(cache_file):
            data = pd.read_csv(cache_file)
        else:
            data = _fetch_hist(ak, code, start_date, end_date)
            if data.empty:
                return f"未找到 '{symbol}'（{code}）在 {start_date} 至 {end_date} 期间的数据"
            data.to_csv(cache_file, index=False)

        # 回写 SQLite 缓存（供其他 Skill 复用）
        _write_sqlite_cache(code, data)

    csv_string = data.to_csv(index=False)
    header = f"# A股日线数据: {code}，区间 {start_date} 至 {end_date}\n"
    header += f"# 总记录数: {len(data)}\n"
    header += f"# 数据获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def _query_sqlite_cache(code: str, start_date: str, end_date: str):
    """查询本地 SQLite K 线缓存，返回 DataFrame 或 None。"""
    try:
        from src.infra.kline_cache import KlineCache
        cache = KlineCache()
        rows = cache.query(code, "qfq", start_date, end_date)
        cache.close()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        # 转为 TradingAgents 标准列名
        col_map = {"date": "Date", "open": "Open", "high": "High",
                   "low": "Low", "close": "Close", "volume": "Volume"}
        df = df.rename(columns=col_map)
        keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[keep]
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
        return df
    except Exception as e:
        logger.debug(f"[akshare_stock] SQLite 缓存查询失败: {e}")
        return None


def _write_sqlite_cache(code: str, df: pd.DataFrame):
    """将 DataFrame 回写到 SQLite 缓存。"""
    try:
        from src.infra.kline_cache import KlineCache
        cache = KlineCache()
        # 反向映射列名
        col_map = {"Date": "date", "Open": "open", "High": "high",
                   "Low": "low", "Close": "close", "Volume": "volume"}
        tmp = df.rename(columns=col_map)
        rows = []
        for _, r in tmp.iterrows():
            date_val = r.get("date", "")
            if hasattr(date_val, "strftime"):
                date_val = date_val.strftime("%Y-%m-%d")
            else:
                date_val = str(date_val)[:10]
            close = r.get("close")
            if close is None or float(close) <= 0:
                continue
            rows.append({
                "date": date_val,
                "open": float(r.get("open", close)),
                "high": float(r.get("high", close)),
                "low": float(r.get("low", close)),
                "close": float(close),
                "volume": int(float(r.get("volume", 0))),
                "amount": 0.0,
            })
        if rows:
            cache.upsert_batch(code, "qfq", rows)
            logger.info(f"[akshare_stock] SQLite 缓存回写 {code}: {len(rows)} 行")
        cache.close()
    except Exception as e:
        logger.debug(f"[akshare_stock] SQLite 缓存回写失败: {e}")


def _fetch_hist(ak, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取日线数据并标准化列名。

    数据源优先级（按稳定性）：腾讯 → 新浪 → 东方财富。
    统一转换为英文列名以兼容系统其他模块。
    """
    df = None

    # 方案 1：腾讯日线（最稳定）
    try:
        exchange = "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj"
        tx_symbol = f"{exchange}{code}"
        df = ak.stock_zh_a_hist_tx(
            symbol=tx_symbol,
            start_date=start_date,  # YYYY-MM-DD 格式
            end_date=end_date,
            adjust="qfq",
        )
        if df is not None and not df.empty:
            logger.info(f"[akshare_stock] 腾讯日线 {code}: {len(df)} 行")
            return _normalize_tx_df(df)
    except Exception as e:
        logger.warning(f"[akshare_stock] 腾讯日线 {code} 失败: {e}")

    # 方案 2：新浪日线
    try:
        exchange = "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj"
        sina_symbol = f"{exchange}{code}"
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")
        if df is not None and not df.empty:
            # 按日期范围过滤
            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start_date) & (df["date"] <= end_date)
            df = df[mask]
            if not df.empty:
                logger.info(f"[akshare_stock] 新浪日线 {code}: {len(df)} 行")
                return _normalize_sina_df(df)
    except Exception as e:
        logger.warning(f"[akshare_stock] 新浪日线 {code} 失败: {e}")

    # 方案 3：东方财富日线（最不稳定）
    try:
        df = _akshare_retry(lambda: ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=_date_to_akshare(start_date),
            end_date=_date_to_akshare(end_date),
            adjust="qfq",
        ))
    except AKShareRateLimitError:
        raise
    except Exception as e:
        if "不存在" in str(e) or "无数据" in str(e):
            raise AKShareInvalidSymbolError(f"股票代码 {code} 无效或无数据: {e}")
        raise AKShareError(f"获取 {code} 行情数据失败: {e}")

    if df is None or df.empty:
        return pd.DataFrame()

    return _normalize_em_df(df)


def _normalize_em_df(df: pd.DataFrame) -> pd.DataFrame:
    """标准化东方财富 DataFrame 列名。"""
    col_map = {
        "日期": "Date", "开盘": "Open", "收盘": "Close",
        "最高": "High", "最低": "Low", "成交量": "Volume",
        "成交额": "Amount", "振幅": "Amplitude",
        "涨跌幅": "Change_Pct", "涨跌额": "Change", "换手率": "Turnover",
    }
    df = df.rename(columns=col_map)
    keep_cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    return df


def _normalize_tx_df(df: pd.DataFrame) -> pd.DataFrame:
    """标准化腾讯 DataFrame 列名。"""
    col_map = {"date": "Date", "open": "Open", "close": "Close",
               "high": "High", "low": "Low", "amount": "Volume"}
    df = df.rename(columns=col_map)
    # 腾讯可能没有 volume 列，amount 实际是成交量
    if "Volume" not in df.columns and "volume" in df.columns:
        df = df.rename(columns={"volume": "Volume"})
    keep_cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    return df


def _normalize_sina_df(df: pd.DataFrame) -> pd.DataFrame:
    """标准化新浪 DataFrame 列名。"""
    col_map = {"date": "Date", "open": "Open", "close": "Close",
               "high": "High", "low": "Low", "volume": "Volume"}
    df = df.rename(columns=col_map)
    keep_cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    return df
