#!/usr/bin/env python3
"""
A股历史 K 线批量预加载

将沪深 A 股日线数据批量拉取并持久化到本地 SQLite 缓存库。
后续所有 Skill 调用 get_klines() 时直接命中本地缓存，零网络开销。

数据源：tencent（默认，纯HTTP稳定） / baostock / akshare

用法:
    python3 preload_klines.py                                    # 全市场（腾讯）
    python3 preload_klines.py --start 2020-01-01                 # 自定义起始
    python3 preload_klines.py --symbols 600519 000001 300750     # 指定个股
    python3 preload_klines.py --skip-existing                    # 断点续传
    python3 preload_klines.py --source baostock                  # 用 baostock
    python3 preload_klines.py --source akshare                   # 用 akshare
"""
import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.kline_cache import KlineCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

_BACKOFF = [2, 4, 8, 16, 30]
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


# ══════════════════════════════════════════════════════════
# 腾讯数据源（纯 HTTP，最稳定）
# ══════════════════════════════════════════════════════════

def _code_to_tencent(code: str) -> str:
    """纯 6 位代码 → 腾讯格式 (sh600519 / sz000001)。"""
    if code.startswith("6"):
        return f"sh{code}"
    elif code.startswith(("0", "3")):
        return f"sz{code}"
    return f"sz{code}"


def tx_get_all_codes() -> List[Tuple[str, str]]:
    """通过腾讯实时行情批量接口获取沪深 A 股代码列表。

    先尝试 akshare 的 stock_info_a_code_name（快），
    失败则用腾讯实时行情逐批探测。
    """
    # 快速路径：akshare 代码列表
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            codes = []
            for _, row in df.iterrows():
                code = str(row.get("code", ""))
                name = str(row.get("name", ""))
                if code and len(code) == 6 and not code.startswith(("8", "9")):
                    codes.append((code, name))
            if codes:
                return codes
    except Exception as e:
        log.warning("akshare 代码列表失败: %s，用腾讯探测", e)

    # 慢速路径：腾讯批量实时行情
    log.info("通过腾讯实时行情获取代码列表...")
    codes = []
    # 生成候选代码范围
    candidates = []
    for i in range(600000, 606000):
        candidates.append(f"sh{i}")
    for i in range(688000, 690000):
        candidates.append(f"sh{i}")
    for i in range(1, 4500):
        candidates.append(f"sz{i:06d}")
    for i in range(300000, 302000):
        candidates.append(f"sz{i}")

    batch_size = 500
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        try:
            r = requests.get(
                f"https://qt.gtimg.cn/q={','.join(batch)}",
                timeout=15, headers=_HEADERS,
            )
            if r.status_code == 200:
                for line in r.text.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    match = re.search(r'"(.+)"', line)
                    if not match:
                        continue
                    f = match.group(1).split("~")
                    if len(f) < 5:
                        continue
                    c = f[2]
                    name = f[1] if len(f) > 1 else ""
                    close = _safe_float(f[3])
                    if not c or len(c) != 6 or not close or close <= 0:
                        continue
                    if c.startswith(("8", "9")):
                        continue
                    codes.append((c, name))
        except Exception as e:
            log.warning("腾讯批量探测失败: %s", e)
        if start > 0 and start % 2000 == 0:
            time.sleep(0.2)

    return codes


def tx_fetch_klines(code: str, start_date: str, end_date: str,
                    adjust: str) -> Optional[List[Dict[str, Any]]]:
    """通过腾讯日线 API 拉取 K 线数据。

    接口: web.ifzq.gtimg.cn/appstock/app/fqkline/get
    返回格式: [date, open, close, high, low, volume]
    注意: 腾讯接口单次最多返回约 640 根 K 线，超长区间需分段拉取。
    """
    tc_symbol = _code_to_tencent(code)
    adj_key_map = {"qfq": "qfqday", "hfq": "hfqday", "none": "day"}
    adj_param = adjust if adjust in ("qfq", "hfq") else ""
    adj_key = adj_key_map.get(adjust, "qfqday")

    all_rows = []
    # 分段拉取（每段 600 天）
    seg_start = datetime.strptime(start_date, "%Y-%m-%d")
    seg_end_final = datetime.strptime(end_date, "%Y-%m-%d")

    while seg_start <= seg_end_final:
        seg_end = min(seg_start + timedelta(days=600), seg_end_final)
        s_str = seg_start.strftime("%Y-%m-%d")
        e_str = seg_end.strftime("%Y-%m-%d")

        rows = _tx_fetch_segment(tc_symbol, s_str, e_str, adj_key, adj_param)
        if rows is None:
            return None  # 全部重试失败
        all_rows.extend(rows)

        seg_start = seg_end + timedelta(days=1)

    return all_rows


def _tx_fetch_segment(tc_symbol: str, start: str, end: str,
                      adj_key: str, adj_param: str) -> Optional[List[Dict]]:
    """拉取单段腾讯日线数据，带重试。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={tc_symbol},day,{start},{end},640,{adj_param}")

    for attempt in range(4):
        try:
            r = requests.get(url, timeout=15, headers=_HEADERS)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")

            data = json.loads(r.text)
            # 响应结构: {"code":0,"data":{"sh600519":{"qfqday":[[...],...],...}}}
            inner = data.get("data", {})
            # 取第一个 key（股票代码）
            stock_data = next(iter(inner.values()), {}) if inner else {}
            klines = stock_data.get(adj_key, [])
            if not klines:
                # fallback: 尝试 "day" key
                klines = stock_data.get("day", [])

            rows = []
            for k in klines:
                # k: [date, open, close, high, low, volume]
                if len(k) < 6:
                    continue
                close = _safe_float(k[2])
                if close is None or close <= 0:
                    continue
                rows.append({
                    "date": k[0],
                    "open": _safe_float(k[1]) or close,
                    "high": _safe_float(k[3]) or close,
                    "low": _safe_float(k[4]) or close,
                    "close": close,
                    "volume": _safe_int(k[5]) or 0,
                    "amount": 0.0,
                })
            return rows

        except Exception as e:
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            log.warning("[tx] %s 第%d次失败: %s, %ds后重试",
                        tc_symbol, attempt + 1, e, wait)
            time.sleep(wait)

    return None


# ══════════════════════════════════════════════════════════
# Baostock 数据源
# ══════════════════════════════════════════════════════════

def _code_to_bs(code: str) -> str:
    if code.startswith("6"):
        return f"sh.{code}"
    elif code.startswith(("0", "3")):
        return f"sz.{code}"
    return f"sz.{code}"


def bs_login():
    import socket
    import baostock as bs
    import baostock.common.context as ctx
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    try:
        lg = bs.login()
        if hasattr(ctx, "default_socket") and ctx.default_socket is not None:
            ctx.default_socket.settimeout(30)
        if lg.error_code != "0":
            raise ConnectionError(f"baostock login: {lg.error_code} {lg.error_msg}")
        return bs
    finally:
        socket.setdefaulttimeout(old_timeout)


def bs_get_all_codes(bs_mod) -> List[Tuple[str, str]]:
    today = datetime.now().strftime("%Y-%m-%d")
    rs = bs_mod.query_all_stock(day=today)
    if rs.error_code != "0":
        raise RuntimeError(f"query_all_stock: {rs.error_code} {rs.error_msg}")
    codes = []
    while rs.next():
        row = rs.get_row_data()
        bs_code = row[0]
        name = row[2] if len(row) > 2 else ""
        pure = bs_code.split(".")[-1] if "." in bs_code else bs_code
        if not pure.isdigit() or len(pure) != 6 or pure.startswith(("8", "9")):
            continue
        if bs_code.startswith("sh.") and pure.startswith("0"):
            continue
        codes.append((pure, name))
    return codes


def bs_fetch_klines(bs_mod, code: str, start_date: str, end_date: str,
                    adjust: str) -> Optional[List[Dict[str, Any]]]:
    adj_map = {"qfq": "2", "hfq": "1", "none": "3"}
    bs_symbol = _code_to_bs(code)
    for attempt in range(3):
        try:
            rs = bs_mod.query_history_k_data_plus(
                bs_symbol, "date,open,high,low,close,volume,amount",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag=adj_map.get(adjust, "2"))
            if rs.error_code != "0":
                return []
            rows = []
            while rs.next():
                r = rs.get_row_data()
                close = _safe_float(r[4])
                if close is None or close <= 0:
                    continue
                rows.append({"date": r[0], "open": _safe_float(r[1]) or close,
                    "high": _safe_float(r[2]) or close, "low": _safe_float(r[3]) or close,
                    "close": close, "volume": _safe_int(r[5]) or 0,
                    "amount": _safe_float(r[6]) or 0.0})
            return rows
        except Exception as e:
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            log.warning("[bs] %s 第%d次: %s, %ds后重试", code, attempt+1, e, wait)
            time.sleep(wait)
    return None


# ══════════════════════════════════════════════════════════
# Akshare 数据源
# ══════════════════════════════════════════════════════════

_AK_COL_MAP = {"日期": "date", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}


def _ensure_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        print("❌ akshare 未安装: pip install akshare")
        sys.exit(1)


def ak_get_all_codes(ak) -> List[Tuple[str, str]]:
    for attempt in range(3):
        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                codes = [(str(r.get("code","")), str(r.get("name","")))
                         for _, r in df.iterrows()
                         if str(r.get("code","")).isdigit() and len(str(r.get("code",""))) == 6
                         and not str(r.get("code","")).startswith(("8","9"))]
                if codes:
                    return codes
        except Exception as e:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF)-1)])
    return []


def ak_fetch_klines(ak, code: str, start_date: str, end_date: str,
                    adjust: str) -> Optional[List[Dict[str, Any]]]:
    adj_map = {"qfq": "qfq", "hfq": "hfq", "none": ""}
    for attempt in range(len(_BACKOFF)):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                start_date=start_date.replace("-",""),
                end_date=end_date.replace("-",""),
                adjust=adj_map.get(adjust, "qfq"))
            if df is None or df.empty:
                return []
            df = df.rename(columns=_AK_COL_MAP)
            rows = []
            for _, r in df.iterrows():
                d = r.get("date","")
                d = d.strftime("%Y-%m-%d") if hasattr(d,"strftime") else str(d)[:10]
                close = _safe_float(r.get("close"))
                if close is None or close <= 0:
                    continue
                rows.append({"date": d, "open": _safe_float(r.get("open")) or close,
                    "high": _safe_float(r.get("high")) or close,
                    "low": _safe_float(r.get("low")) or close,
                    "close": close, "volume": _safe_int(r.get("volume")) or 0,
                    "amount": _safe_float(r.get("amount")) or 0.0})
            return rows
        except Exception as e:
            time.sleep(_BACKOFF[attempt])
    return None


# ══════════════════════════════════════════════════════════
# 工具函数 & 主流程
# ══════════════════════════════════════════════════════════

def _safe_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if not math.isnan(v) else None
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="A股历史K线批量预加载")
    parser.add_argument("--symbols", nargs="*", type=str,
                        help="指定股票代码（如 600519 000001）")
    parser.add_argument("--start", type=str, default=None,
                        help="开始日期 YYYY-MM-DD（默认 2 年前）")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--adjust", type=str, default="qfq",
                        choices=["qfq", "hfq", "none"],
                        help="复权方式（默认 qfq）")
    parser.add_argument("--interval", type=float, default=0.15,
                        help="API 调用间隔秒数（默认 0.15）")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已有数据的股票（断点续传）")
    parser.add_argument("--source", type=str, default="tencent",
                        choices=["tencent", "baostock", "akshare"],
                        help="数据源（默认 tencent，纯HTTP最稳定）")
    parser.add_argument("--db", type=str, default=None,
                        help="缓存数据库路径")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    db_path = args.db or os.path.join(PROJECT_ROOT, "data", "kline_cache.db")
    cache = KlineCache(db_path)

    source = args.source
    bs_mod = None
    ak_mod = None

    # 初始化数据源
    if source == "baostock":
        print("📡 数据源: baostock")
        try:
            bs_mod = bs_login()
            print("   登录成功")
        except Exception as e:
            print(f"⚠️  baostock 失败: {e}，切换到 tencent")
            source = "tencent"
    elif source == "akshare":
        print("📡 数据源: akshare")
        ak_mod = _ensure_akshare()

    if source == "tencent":
        print("📡 数据源: 腾讯日线（纯HTTP，稳定）")

    # 获取股票列表
    if args.symbols:
        stock_list = [(s.strip(), "") for s in args.symbols]
        print(f"📋 指定 {len(stock_list)} 只股票")
    else:
        print("📋 获取代码列表...")
        if source == "baostock":
            stock_list = bs_get_all_codes(bs_mod)
        elif source == "akshare":
            stock_list = ak_get_all_codes(ak_mod)
        else:
            stock_list = tx_get_all_codes()

        if not stock_list:
            print("❌ 无法获取股票列表")
            sys.exit(1)
        print(f"   共 {len(stock_list)} 只股票")

    print(f"📅 范围: {start_date} ~ {end_date}")
    print(f"📊 复权: {args.adjust} | 间隔: {args.interval}s")
    print(f"💾 缓存: {db_path}")
    if args.skip_existing:
        print("⏭️  断点续传模式")
    print("-" * 60)

    total = len(stock_list)
    success = 0
    skipped = 0
    failed = 0
    total_rows = 0
    t0 = time.time()

    for i, (code, name) in enumerate(stock_list, 1):
        label = f"{code} {name}" if name else code

        if args.skip_existing:
            if cache.get_row_count(code, args.adjust) > 0:
                skipped += 1
                if i % 200 == 0:
                    _progress(i, total, success, skipped, failed, total_rows, t0)
                continue

        if i > 1:
            time.sleep(args.interval)

        # 拉取
        if source == "tencent":
            rows = tx_fetch_klines(code, start_date, end_date, args.adjust)
        elif source == "baostock":
            rows = bs_fetch_klines(bs_mod, code, start_date, end_date, args.adjust)
        else:
            rows = ak_fetch_klines(ak_mod, code, start_date, end_date, args.adjust)

        if rows is None:
            failed += 1
            if failed <= 5 or failed % 20 == 0:
                log.error("[%d/%d] ❌ %s 失败（累计 %d）", i, total, label, failed)
            continue
        if not rows:
            skipped += 1
            continue

        n = cache.upsert_batch(code, args.adjust, rows)
        success += 1
        total_rows += n

        if i % 100 == 0 or i == total:
            _progress(i, total, success, skipped, failed, total_rows, t0, label, n)

    elapsed = time.time() - t0
    print("=" * 60)
    print(f"✅ 完成 | 成功:{success} 跳过:{skipped} 失败:{failed}")
    print(f"   {total_rows:,} 行 | {elapsed/60:.1f} 分钟 | {db_path}")
    if os.path.exists(db_path):
        print(f"   文件: {os.path.getsize(db_path)/1024/1024:.1f} MB")
    if bs_mod:
        bs_mod.logout()
    cache.close()


def _progress(i, total, ok, skip, fail, rows, t0, label="", n=0):
    el = time.time() - t0
    spd = ok / el * 60 if el > 0 and ok > 0 else 0
    eta = (total - i) / (i / el) / 60 if el > 0 else 0
    p = [f"[{i}/{total}]"]
    if label:
        p.append(f"{label:14s} +{n:5d}")
    p += [f"✓{ok} ⏭{skip} ✗{fail}", f"{rows:,}行", f"{spd:.0f}/分", f"ETA:{eta:.1f}m"]
    print(f"  {' | '.join(p)}")


if __name__ == "__main__":
    main()
