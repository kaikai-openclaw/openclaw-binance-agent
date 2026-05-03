#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安合约 4h / 1h 策略回测脚本

对六种策略的评分体系做历史回测，验证权重参数是否有效：
  oversold_4h   : 短期超跌反弹（4h K线，做多）
  oversold_1h   : 超短期超跌反弹（1h K线，做多）
  overbought_4h : 短期超买做空（4h K线，做空）
  overbought_1h : 超短期超买做空（1h K线，做空）
  reversal_4h   : 底部放量反转（4h K线，做多）
  reversal_1h   : 底部放量反转（1h K线，做多）

用法:
    python3 scripts/backtest_crypto.py --strategy oversold_4h
    python3 scripts/backtest_crypto.py --strategy all
    python3 scripts/backtest_crypto.py --strategy oversold_4h --start 2024-01-01
    python3 scripts/backtest_crypto.py --strategy oversold_4h --sample 200
    python3 scripts/backtest_crypto.py --strategy oversold_4h --optimize
    python3 scripts/backtest_crypto.py --strategy oversold_4h --dim-analysis
"""
import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.skills.crypto_oversold import (
    calc_oversold_score,
    ST_RSI_THRESHOLD, ST_BIAS_THRESHOLD, ST_CONSECUTIVE_DOWN,
    ST_DROP_PCT, ST_DROP_LOOKBACK, ST_DRAWDOWN_THRESHOLD, ST_DRAWDOWN_LOOKBACK,
    ST_VOL_SURGE_THRESHOLD,
    ST_W_RSI, ST_W_BIAS, ST_W_DROP, ST_W_BOLL, ST_W_MACD_DIV, ST_W_KDJ,
    ST_W_FUNDING, ST_W_DRAWDOWN, ST_W_VOLUME,
)
from src.skills.crypto_overbought import (
    calc_overbought_score,
    ST_RSI_THRESHOLD as OB_ST_RSI,
    ST_BIAS_THRESHOLD as OB_ST_BIAS,
    ST_CONSECUTIVE_UP as OB_ST_CONSEC,
    ST_RALLY_PCT as OB_ST_RALLY_PCT,
    ST_RALLY_LOOKBACK as OB_ST_RALLY_LB,
    ST_RISE_LOOKBACK as OB_ST_RISE_LB,
    ST_W_RSI as OB_ST_W_RSI, ST_W_FUNDING as OB_ST_W_FUND,
    ST_W_BIAS as OB_ST_W_BIAS, ST_W_VOL_DIV as OB_ST_W_VDIV,
    ST_W_BOLL as OB_ST_W_BOLL, ST_W_RALLY as OB_ST_W_RALLY,
    ST_W_KDJ as OB_ST_W_KDJ, ST_W_MACD_DIV as OB_ST_W_MACD,
    ST_W_SHADOW as OB_ST_W_SHAD, ST_W_SQUEEZE_RISK as OB_ST_W_SQZ,
    H1_RSI_THRESHOLD as OB_H1_RSI,
    H1_BIAS_THRESHOLD as OB_H1_BIAS,
    H1_CONSECUTIVE_UP as OB_H1_CONSEC,
    H1_RALLY_PCT as OB_H1_RALLY_PCT,
    H1_RALLY_LOOKBACK as OB_H1_RALLY_LB,
    H1_RISE_LOOKBACK as OB_H1_RISE_LB,
    H1_W_RSI as OB_H1_W_RSI, H1_W_FUNDING as OB_H1_W_FUND,
    H1_W_BIAS as OB_H1_W_BIAS, H1_W_VOL_DIV as OB_H1_W_VDIV,
    H1_W_BOLL as OB_H1_W_BOLL, H1_W_RALLY as OB_H1_W_RALLY,
    H1_W_KDJ as OB_H1_W_KDJ, H1_W_MACD_DIV as OB_H1_W_MACD,
    H1_W_SHADOW as OB_H1_W_SHAD, H1_W_SQUEEZE_RISK as OB_H1_W_SQZ,
)
from src.skills.crypto_reversal import (
    calc_reversal_score,
    ST_BOTTOM_LOOKBACK as REV_ST_BOT_LB,
    ST_PRICE_STABLE_WINDOW as REV_ST_STABLE_W,
    ST_DROP_LOOKBACK as REV_ST_DROP_LB,
    ST_VOLUME_SURGE_THRESHOLD as REV_ST_VOL_T,
    ST_VOLUME_SURGE_STRONG as REV_ST_VOL_S,
    ST_DIST_BOTTOM_IDEAL_MIN as REV_ST_DIST_MIN,
    ST_DIST_BOTTOM_IDEAL_MAX as REV_ST_DIST_MAX,
    ST_SHADOW_RATIO_THRESHOLD as REV_ST_SHAD,
    ST_W_VOLUME_SURGE as REV_ST_W_VOL, ST_W_PRICE_STABLE as REV_ST_W_STAB,
    ST_W_MA_TURN as REV_ST_W_MA, ST_W_FUNDING as REV_ST_W_FUND,
    ST_W_MACD_REVERSAL as REV_ST_W_MACD, ST_W_DIST_BOTTOM as REV_ST_W_DIST,
    ST_W_PRIOR_DROP as REV_ST_W_DROP, ST_W_KDJ_CROSS as REV_ST_W_KDJ,
    ST_W_SHADOW as REV_ST_W_SHAD,
    H1_BOTTOM_LOOKBACK as REV_H1_BOT_LB,
    H1_PRICE_STABLE_WINDOW as REV_H1_STABLE_W,
    H1_DROP_LOOKBACK as REV_H1_DROP_LB,
    H1_VOLUME_SURGE_THRESHOLD as REV_H1_VOL_T,
    H1_VOLUME_SURGE_STRONG as REV_H1_VOL_S,
    H1_DIST_BOTTOM_IDEAL_MIN as REV_H1_DIST_MIN,
    H1_DIST_BOTTOM_IDEAL_MAX as REV_H1_DIST_MAX,
    H1_SHADOW_RATIO_THRESHOLD as REV_H1_SHAD,
    H1_W_VOLUME_SURGE as REV_H1_W_VOL, H1_W_PRICE_STABLE as REV_H1_W_STAB,
    H1_W_MA_TURN as REV_H1_W_MA, H1_W_FUNDING as REV_H1_W_FUND,
    H1_W_MACD_REVERSAL as REV_H1_W_MACD, H1_W_DIST_BOTTOM as REV_H1_W_DIST,
    H1_W_PRIOR_DROP as REV_H1_W_DROP, H1_W_KDJ_CROSS as REV_H1_W_KDJ,
    H1_W_SHADOW as REV_H1_W_SHAD,
)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "binance_kline_cache.db")

# ── 各策略默认持有 K 线根数 ──────────────────────────────
# 超跌/超买：信号时效短，持有 1 天
# 反转：需要趋势确认，持有 3 天（4h）/ 1 天（1h）
HOLD_BARS = {
    "oversold_4h":   6,    # 4h x 6  = 1 天
    "oversold_1h":   12,   # 1h x 12 = 12 小时
    "overbought_4h": 6,    # 4h x 6  = 1 天
    "overbought_1h": 12,   # 1h x 12 = 12 小时
    "reversal_4h":   18,   # 4h x 18 = 3 天
    "reversal_1h":   24,   # 1h x 24 = 1 天
}

INTERVALS = {
    "oversold_4h":   "4h",
    "oversold_1h":   "1h",
    "overbought_4h": "4h",
    "overbought_1h": "1h",
    "reversal_4h":   "4h",
    "reversal_1h":   "1h",
}

# 做多策略：收益 = (exit - entry) / entry
# 做空策略：收益 = (entry - exit) / entry
DIRECTION = {
    "oversold_4h":   "long",
    "oversold_1h":   "long",
    "overbought_4h": "short",
    "overbought_1h": "short",
    "reversal_4h":   "long",
    "reversal_1h":   "long",
}

SCORE_BINS   = [0, 20, 30, 40, 50, 60, 75, 101]
SCORE_LABELS = ["0-20", "20-30", "30-40", "40-50", "50-60", "60-75", "75+"]

# 每隔多少根 K 线取一个样本点（避免相邻样本高度相关）
SAMPLE_INTERVAL = {"4h": 6, "1h": 12}

MIN_KLINES = 120  # 最少需要多少根 K 线才参与回测


# ══════════════════════════════════════════════════════════
# 数据读取
# ══════════════════════════════════════════════════════════

def _interval_ms(interval: str) -> int:
    """K 线周期转毫秒。"""
    return {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "15m": 900_000}.get(interval, 3_600_000)


def get_all_symbols(conn: sqlite3.Connection, interval: str) -> List[str]:
    """获取缓存中指定周期的所有交易对。"""
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM binance_kline_cache WHERE interval=? ORDER BY symbol",
        (interval,),
    )
    return [r[0] for r in cur.fetchall()]


def get_klines(
    conn: sqlite3.Connection, symbol: str, interval: str,
    start_ms: int, end_ms: int,
) -> List[dict]:
    """读取指定区间的 K 线，返回 dict 列表。"""
    cur = conn.execute(
        "SELECT open_time,open,high,low,close,volume "
        "FROM binance_kline_cache "
        "WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=? "
        "ORDER BY open_time ASC",
        (symbol, interval, start_ms, end_ms),
    )
    return [
        {"open_time": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5]}
        for r in cur.fetchall()
    ]


def ms_from_date(date_str: str) -> int:
    """日期字符串转 UTC 毫秒时间戳。"""
    from datetime import timezone
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ══════════════════════════════════════════════════════════
# 评分函数适配器（纯函数，不依赖网络/资金费率）
# ══════════════════════════════════════════════════════════

def score_oversold_4h(klines: List[dict]) -> Optional[float]:
    if len(klines) < 60:
        return None
    c = [k["close"] for k in klines]
    h = [k["high"]  for k in klines]
    lo = [k["low"]  for k in klines]
    v = [k["volume"] for k in klines]
    r = calc_oversold_score(
        c, h, lo, v,
        ST_RSI_THRESHOLD, ST_BIAS_THRESHOLD, ST_CONSECUTIVE_DOWN,
        ST_DROP_PCT, ST_DROP_LOOKBACK,
        ST_DRAWDOWN_THRESHOLD, ST_DRAWDOWN_LOOKBACK,
        ST_VOL_SURGE_THRESHOLD,
        None,  # funding_rate — 回测无法获取历史资金费率，置 None
        {"rsi": ST_W_RSI, "bias": ST_W_BIAS, "drop": ST_W_DROP,
         "boll": ST_W_BOLL, "macd_div": ST_W_MACD_DIV, "kdj": ST_W_KDJ,
         "funding": ST_W_FUNDING, "drawdown": ST_W_DRAWDOWN, "volume": ST_W_VOLUME},
    )
    return float(r["oversold_score"])


def score_oversold_1h(klines: List[dict]) -> Optional[float]:
    """1h 超跌：复用 4h 参数（阈值相同，仅周期不同）。"""
    return score_oversold_4h(klines)


def score_overbought_4h(klines: List[dict]) -> Optional[float]:
    if len(klines) < 60:
        return None
    c  = [k["close"]  for k in klines]
    h  = [k["high"]   for k in klines]
    lo = [k["low"]    for k in klines]
    o  = [k["open"]   for k in klines]
    v  = [k["volume"] for k in klines]
    r = calc_overbought_score(
        c, h, lo, o, v,
        None,   # funding_rate
        None,   # oi_value
        1e8,    # quote_volume_24h — 给大值避免轧空扣分干扰
        OB_ST_RSI, OB_ST_BIAS, OB_ST_CONSEC,
        OB_ST_RALLY_PCT, OB_ST_RALLY_LB, OB_ST_RISE_LB,
        {"rsi": OB_ST_W_RSI, "funding": OB_ST_W_FUND,
         "bias": OB_ST_W_BIAS, "vol_div": OB_ST_W_VDIV,
         "boll": OB_ST_W_BOLL, "rally": OB_ST_W_RALLY,
         "kdj": OB_ST_W_KDJ, "macd_div": OB_ST_W_MACD,
         "shadow": OB_ST_W_SHAD, "squeeze_risk": OB_ST_W_SQZ},
    )
    return float(r["overbought_score"])


def score_overbought_1h(klines: List[dict]) -> Optional[float]:
    if len(klines) < 60:
        return None
    c  = [k["close"]  for k in klines]
    h  = [k["high"]   for k in klines]
    lo = [k["low"]    for k in klines]
    o  = [k["open"]   for k in klines]
    v  = [k["volume"] for k in klines]
    r = calc_overbought_score(
        c, h, lo, o, v,
        None, None, 1e8,
        OB_H1_RSI, OB_H1_BIAS, OB_H1_CONSEC,
        OB_H1_RALLY_PCT, OB_H1_RALLY_LB, OB_H1_RISE_LB,
        {"rsi": OB_H1_W_RSI, "funding": OB_H1_W_FUND,
         "bias": OB_H1_W_BIAS, "vol_div": OB_H1_W_VDIV,
         "boll": OB_H1_W_BOLL, "rally": OB_H1_W_RALLY,
         "kdj": OB_H1_W_KDJ, "macd_div": OB_H1_W_MACD,
         "shadow": OB_H1_W_SHAD, "squeeze_risk": OB_H1_W_SQZ},
    )
    return float(r["overbought_score"])


def score_reversal_4h(klines: List[dict]) -> Optional[float]:
    if len(klines) < 80:
        return None
    c  = [k["close"]  for k in klines]
    h  = [k["high"]   for k in klines]
    lo = [k["low"]    for k in klines]
    o  = [k["open"]   for k in klines]
    v  = [k["volume"] for k in klines]
    r = calc_reversal_score(
        c, h, lo, o, v,
        None,  # funding_rate
        REV_ST_BOT_LB, REV_ST_STABLE_W, REV_ST_DROP_LB,
        REV_ST_VOL_T, REV_ST_VOL_S,
        REV_ST_DIST_MIN, REV_ST_DIST_MAX, REV_ST_SHAD,
        {"volume_surge": REV_ST_W_VOL, "price_stable": REV_ST_W_STAB,
         "ma_turn": REV_ST_W_MA, "funding": REV_ST_W_FUND,
         "macd_reversal": REV_ST_W_MACD, "dist_bottom": REV_ST_W_DIST,
         "prior_drop": REV_ST_W_DROP, "kdj_cross": REV_ST_W_KDJ,
         "shadow": REV_ST_W_SHAD},
    )
    return float(r["reversal_score"])


def score_reversal_1h(klines: List[dict]) -> Optional[float]:
    if len(klines) < 80:
        return None
    c  = [k["close"]  for k in klines]
    h  = [k["high"]   for k in klines]
    lo = [k["low"]    for k in klines]
    o  = [k["open"]   for k in klines]
    v  = [k["volume"] for k in klines]
    r = calc_reversal_score(
        c, h, lo, o, v,
        None,
        REV_H1_BOT_LB, REV_H1_STABLE_W, REV_H1_DROP_LB,
        REV_H1_VOL_T, REV_H1_VOL_S,
        REV_H1_DIST_MIN, REV_H1_DIST_MAX, REV_H1_SHAD,
        {"volume_surge": REV_H1_W_VOL, "price_stable": REV_H1_W_STAB,
         "ma_turn": REV_H1_W_MA, "funding": REV_H1_W_FUND,
         "macd_reversal": REV_H1_W_MACD, "dist_bottom": REV_H1_W_DIST,
         "prior_drop": REV_H1_W_DROP, "kdj_cross": REV_H1_W_KDJ,
         "shadow": REV_H1_W_SHAD},
    )
    return float(r["reversal_score"])


SCORE_FN = {
    "oversold_4h":   score_oversold_4h,
    "oversold_1h":   score_oversold_1h,
    "overbought_4h": score_overbought_4h,
    "overbought_1h": score_overbought_1h,
    "reversal_4h":   score_reversal_4h,
    "reversal_1h":   score_reversal_1h,
}


# ══════════════════════════════════════════════════════════
# 回测核心
# ══════════════════════════════════════════════════════════

def backtest_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    strategy: str,
    start_ms: int,
    end_ms: int,
    hold_bars: int,
) -> List[dict]:
    """对单个交易对做滑动窗口回测，返回样本点列表。"""
    interval  = INTERVALS[strategy]
    score_fn  = SCORE_FN[strategy]
    direction = DIRECTION[strategy]

    # 多拉 200 根历史用于计算指标
    fetch_start = start_ms - 200 * _interval_ms(interval)
    klines = get_klines(conn, symbol, interval, fetch_start, end_ms)

    if len(klines) < MIN_KLINES + hold_bars:
        return []

    # 找到 start_ms 在 klines 中的位置
    start_idx = 0
    for i, k in enumerate(klines):
        if k["open_time"] >= start_ms:
            start_idx = i
            break

    results = []
    step = SAMPLE_INTERVAL[interval]
    i = start_idx

    while i < len(klines) - hold_bars:
        window = klines[max(0, i - 200): i + 1]
        if len(window) < 60:
            i += step
            continue

        try:
            score = score_fn(window)
        except Exception:
            i += step
            continue

        if score is None:
            i += step
            continue

        entry_close = klines[i]["close"]
        exit_close  = klines[i + hold_bars]["close"]
        if entry_close <= 0:
            i += step
            continue

        # 收益率（做多/做空方向）
        if direction == "long":
            ret_pct = (exit_close - entry_close) / entry_close * 100
        else:
            ret_pct = (entry_close - exit_close) / entry_close * 100

        # 持仓期间最大不利波动（MFE 反向 = 最大回撤）
        period = klines[i: i + hold_bars + 1]
        peak = entry_close
        max_drawdown = 0.0
        for k in period[1:]:
            if direction == "long":
                if k["close"] > peak:
                    peak = k["close"]
                dd = (k["close"] - peak) / peak * 100
            else:
                if k["close"] < peak:
                    peak = k["close"]
                dd = (peak - k["close"]) / peak * 100
            if dd < max_drawdown:
                max_drawdown = dd

        from datetime import timezone as _tz
        ts = datetime.fromtimestamp(klines[i]["open_time"] / 1000, tz=_tz.utc).strftime("%Y-%m-%d")
        results.append({
            "symbol":       symbol,
            "date":         ts,
            "score":        round(score),
            "ret_pct":      round(ret_pct, 2),
            "max_drawdown": round(max_drawdown, 2),
            "win":          ret_pct > 0,
        })
        i += step

    return results


# ══════════════════════════════════════════════════════════
# 统计分析
# ══════════════════════════════════════════════════════════

def analyze_results(samples: List[dict], strategy: str, hold_bars: int) -> None:
    """按评分分组统计，输出回测报告。"""
    if not samples:
        print("  ⚠️  无有效样本")
        return

    total    = len(samples)
    all_rets = [s["ret_pct"] for s in samples]
    overall_win = sum(1 for s in samples if s["win"]) / total * 100
    overall_avg = sum(all_rets) / total
    direction   = DIRECTION.get(strategy, "long")

    print(f"\n{'='*65}")
    print(f"  策略: {strategy}  |  持有: {hold_bars} 根K线  |  样本: {total:,}  |  方向: {direction}")
    print(f"  整体胜率: {overall_win:.1f}%  |  平均收益: {overall_avg:+.2f}%")
    print(f"{'='*65}")
    print(f"  {'评分区间':>8} {'样本数':>7} {'胜率':>7} {'均收益':>9} "
          f"{'中位收益':>9} {'均回撤':>9} {'IR':>8}")
    print(f"  {'-'*60}")

    group_stats = []
    for i, label in enumerate(SCORE_LABELS):
        lo = SCORE_BINS[i]
        hi = SCORE_BINS[i + 1]
        grp = [s for s in samples if lo <= s["score"] < hi]
        if not grp:
            continue
        n    = len(grp)
        rets = [s["ret_pct"] for s in grp]
        dds  = [s["max_drawdown"] for s in grp]
        wr   = sum(1 for s in grp if s["win"]) / n * 100
        avg  = sum(rets) / n
        med  = sorted(rets)[n // 2]
        add  = sum(dds) / n
        std  = (sum((r - avg) ** 2 for r in rets) / (n - 1)) ** 0.5 if n > 1 else 1
        ir   = avg / std if std > 0 else 0
        group_stats.append((label, n, wr, avg, med, add, ir))
        print(f"  {label:>8} {n:>7,} {wr:>6.1f}% {avg:>+8.2f}% "
              f"{med:>+8.2f}% {add:>+8.2f}% {ir:>8.3f}")

    print(f"  {'-'*60}")

    # 评分有效性验证
    if len(group_stats) >= 2:
        lo_g = group_stats[0]
        hi_g = group_stats[-1]
        diff = hi_g[3] - lo_g[3]
        print(f"\n  📊 评分有效性: 高分组 {hi_g[3]:+.2f}% vs 低分组 {lo_g[3]:+.2f}% → 差值 {diff:+.2f}%")
        if diff > 1.0:
            print("  ✅ 评分有效：高分组显著跑赢低分组")
        elif diff > 0:
            print("  ⚠️  评分弱有效：高分组略优，但差距不显著")
        else:
            print("  ❌ 评分无效：高分组未跑赢低分组，权重需要重新调整")

    # 最优入场门槛建议
    print(f"\n  📋 建议入场门槛（胜率>55% 且均收益>0 且样本≥20）:")
    found = False
    for label, n, wr, avg, med, add, ir in group_stats:
        if wr > 55 and avg > 0 and n >= 20:
            print(f"     评分≥{label.split('-')[0]}  胜率{wr:.1f}%  均收益{avg:+.2f}%  样本{n}")
            found = True
    if not found:
        print("     无满足条件的门槛（样本可能不足，建议增加 --sample）")

    # 按年份分层
    if samples and "date" in samples[0]:
        print(f"\n  📅 按年份分层（验证不同市场环境）:")
        print(f"  {'年份':>6} {'样本':>7} {'胜率':>7} {'均收益':>9}  评分≥40子集")
        print(f"  {'-'*55}")
        years = sorted(set(s["date"][:4] for s in samples))
        for yr in years:
            yr_all  = [s for s in samples if s["date"].startswith(yr)]
            yr_high = [s for s in yr_all  if s["score"] >= 40]
            if not yr_all:
                continue
            wr_all  = sum(1 for s in yr_all if s["win"]) / len(yr_all) * 100
            avg_all = sum(s["ret_pct"] for s in yr_all) / len(yr_all)
            if yr_high:
                avg_h = sum(s["ret_pct"] for s in yr_high) / len(yr_high)
                wr_h  = sum(1 for s in yr_high if s["win"]) / len(yr_high) * 100
                high_str = f"  胜率{wr_h:.0f}% 均收益{avg_h:+.2f}% (n={len(yr_high)})"
            else:
                high_str = "  无样本"
            print(f"  {yr:>6} {len(yr_all):>7,} {wr_all:>6.1f}% {avg_all:>+8.2f}%{high_str}")

    print()


# ══════════════════════════════════════════════════════════
# 子维度预测力分析
# ══════════════════════════════════════════════════════════

def analyze_dimensions(
    conn: sqlite3.Connection,
    symbols: List[str],
    strategy: str,
    start_ms: int,
    end_ms: int,
    hold_bars: int,
) -> None:
    """分析各子维度与收益的相关性，找出真正有预测力的维度。"""
    interval  = INTERVALS[strategy]
    direction = DIRECTION[strategy]

    # 各策略的子维度定义
    if strategy in ("oversold_4h", "oversold_1h"):
        dims = [
            ("rsi",      "RSI超卖",    ST_W_RSI),
            ("bias",     "BIAS乖离",   ST_W_BIAS),
            ("drop",     "连续杀跌",   ST_W_DROP),
            ("boll",     "布林下轨",   ST_W_BOLL),
            ("macd_div", "MACD背离",   ST_W_MACD_DIV),
            ("kdj",      "KDJ极值",    ST_W_KDJ),
            ("funding",  "资金费率",   ST_W_FUNDING),
            ("drawdown", "距高点回撤", ST_W_DRAWDOWN),
            ("volume",   "底部放量",   ST_W_VOLUME),
        ]
        def get_dim_scores(klines):
            c  = [k["close"]  for k in klines]
            h  = [k["high"]   for k in klines]
            lo = [k["low"]    for k in klines]
            v  = [k["volume"] for k in klines]
            r = calc_oversold_score(
                c, h, lo, v,
                ST_RSI_THRESHOLD, ST_BIAS_THRESHOLD, ST_CONSECUTIVE_DOWN,
                ST_DROP_PCT, ST_DROP_LOOKBACK,
                ST_DRAWDOWN_THRESHOLD, ST_DRAWDOWN_LOOKBACK,
                ST_VOL_SURGE_THRESHOLD, None,
                {"rsi": ST_W_RSI, "bias": ST_W_BIAS, "drop": ST_W_DROP,
                 "boll": ST_W_BOLL, "macd_div": ST_W_MACD_DIV, "kdj": ST_W_KDJ,
                 "funding": ST_W_FUNDING, "drawdown": ST_W_DRAWDOWN, "volume": ST_W_VOLUME},
            )
            # 用各指标原始值判断是否触发
            return {
                "rsi":      1 if (r["rsi"] is not None and r["rsi"] < ST_RSI_THRESHOLD) else 0,
                "bias":     1 if (r["bias_20"] is not None and r["bias_20"] < ST_BIAS_THRESHOLD) else 0,
                "drop":     1 if r["consecutive_down"] >= ST_CONSECUTIVE_DOWN else 0,
                "boll":     1 if r["below_boll_lower"] else 0,
                "macd_div": 1 if r["macd_divergence"] else 0,
                "kdj":      1 if (r["kdj_j"] is not None and r["kdj_j"] < 0) else 0,
                "funding":  0,  # 回测无历史资金费率
                "drawdown": 1 if (r["distance_from_high_pct"] is not None
                                  and r["distance_from_high_pct"] < ST_DRAWDOWN_THRESHOLD) else 0,
                "volume":   1 if (r["volume_surge"] is not None
                                  and r["volume_surge"] >= ST_VOL_SURGE_THRESHOLD) else 0,
            }

    elif strategy in ("reversal_4h", "reversal_1h"):
        bot_lb    = REV_ST_BOT_LB    if "4h" in strategy else REV_H1_BOT_LB
        stable_w  = REV_ST_STABLE_W  if "4h" in strategy else REV_H1_STABLE_W
        drop_lb   = REV_ST_DROP_LB   if "4h" in strategy else REV_H1_DROP_LB
        vol_t     = REV_ST_VOL_T     if "4h" in strategy else REV_H1_VOL_T
        vol_s     = REV_ST_VOL_S     if "4h" in strategy else REV_H1_VOL_S
        dist_min  = REV_ST_DIST_MIN  if "4h" in strategy else REV_H1_DIST_MIN
        dist_max  = REV_ST_DIST_MAX  if "4h" in strategy else REV_H1_DIST_MAX
        shad      = REV_ST_SHAD      if "4h" in strategy else REV_H1_SHAD
        w_vol     = REV_ST_W_VOL     if "4h" in strategy else REV_H1_W_VOL
        w_stab    = REV_ST_W_STAB    if "4h" in strategy else REV_H1_W_STAB
        w_ma      = REV_ST_W_MA      if "4h" in strategy else REV_H1_W_MA
        w_fund    = REV_ST_W_FUND    if "4h" in strategy else REV_H1_W_FUND
        w_macd    = REV_ST_W_MACD    if "4h" in strategy else REV_H1_W_MACD
        w_dist    = REV_ST_W_DIST    if "4h" in strategy else REV_H1_W_DIST
        w_drop    = REV_ST_W_DROP    if "4h" in strategy else REV_H1_W_DROP
        w_kdj     = REV_ST_W_KDJ     if "4h" in strategy else REV_H1_W_KDJ
        w_shad    = REV_ST_W_SHAD    if "4h" in strategy else REV_H1_W_SHAD
        dims = [
            ("volume_surge",   "底部放量",   w_vol),
            ("price_stable",   "价格企稳",   w_stab),
            ("ma_turn",        "均线拐头",   w_ma),
            ("funding",        "资金费率",   w_fund),
            ("macd_reversal",  "MACD反转",   w_macd),
            ("dist_bottom",    "距底部距离", w_dist),
            ("prior_drop",     "前期跌幅",   w_drop),
            ("kdj_cross",      "KDJ金叉",    w_kdj),
            ("shadow",         "长下影线",   w_shad),
        ]
        def get_dim_scores(klines):
            c  = [k["close"]  for k in klines]
            h  = [k["high"]   for k in klines]
            lo = [k["low"]    for k in klines]
            o  = [k["open"]   for k in klines]
            v  = [k["volume"] for k in klines]
            r = calc_reversal_score(
                c, h, lo, o, v, None,
                bot_lb, stable_w, drop_lb, vol_t, vol_s,
                dist_min, dist_max, shad,
                {"volume_surge": w_vol, "price_stable": w_stab, "ma_turn": w_ma,
                 "funding": w_fund, "macd_reversal": w_macd, "dist_bottom": w_dist,
                 "prior_drop": w_drop, "kdj_cross": w_kdj, "shadow": w_shad},
            )
            return {
                "volume_surge":  1 if r["volume_surge_score"] > 0 else 0,
                "price_stable":  1 if r["price_stable_score"] > 0 else 0,
                "ma_turn":       1 if r["ma_turn_score"] > 0 else 0,
                "funding":       0,
                "macd_reversal": 1 if r["macd_reversal_score"] > 0 else 0,
                "dist_bottom":   1 if r["dist_bottom_score"] > 0 else 0,
                "prior_drop":    1 if r["prior_drop_score"] > 0 else 0,
                "kdj_cross":     1 if r["kdj_score"] > 0 else 0,
                "shadow":        1 if r["shadow_score"] > 0 else 0,
            }
    else:
        print(f"  ⚠️  {strategy} 暂不支持子维度分析")
        return

    print(f"\n{'='*65}")
    print(f"  {strategy} 子维度预测力分析（有信号 vs 无信号 持有{hold_bars}根K线收益）")
    print(f"{'='*65}")
    print(f"  {'维度':>10} {'权重':>4} {'有信号均收益':>12} {'无信号均收益':>12} {'差值':>8} {'有效?':>6}")
    print(f"  {'-'*55}")

    random.seed(42)
    sample_syms = random.sample(symbols, min(150, len(symbols)))
    dim_data: Dict[str, Dict] = {d[0]: {"with": [], "without": []} for d in dims}

    for symbol in sample_syms:
        fetch_start = start_ms - 200 * _interval_ms(interval)
        klines = get_klines(conn, symbol, interval, fetch_start, end_ms)
        if len(klines) < MIN_KLINES + hold_bars:
            continue

        start_idx = next((i for i, k in enumerate(klines) if k["open_time"] >= start_ms), 0)
        step = SAMPLE_INTERVAL[interval]
        i = start_idx
        while i < len(klines) - hold_bars:
            window = klines[max(0, i - 200): i + 1]
            if len(window) < 80:
                i += step
                continue
            try:
                dim_scores = get_dim_scores(window)
            except Exception:
                i += step
                continue

            entry = klines[i]["close"]
            exit_ = klines[i + hold_bars]["close"]
            if entry <= 0:
                i += step
                continue
            ret = (exit_ - entry) / entry * 100 if direction == "long" \
                  else (entry - exit_) / entry * 100

            for dim_key, _, _ in dims:
                if dim_scores.get(dim_key, 0) > 0:
                    dim_data[dim_key]["with"].append(ret)
                else:
                    dim_data[dim_key]["without"].append(ret)
            i += step

    for dim_key, dim_name, weight in dims:
        w_rets  = dim_data[dim_key]["with"]
        wo_rets = dim_data[dim_key]["without"]
        if not wo_rets:
            print(f"  {dim_name:>10} {weight:>4}  {'N/A':>12}  {'N/A':>12}  {'N/A':>8}  {'?':>6}")
            continue
        w_avg  = sum(w_rets)  / len(w_rets)  if w_rets  else float("nan")
        wo_avg = sum(wo_rets) / len(wo_rets)
        if not w_rets:
            print(f"  {dim_name:>10} {weight:>4}  {'无信号':>12}  {wo_avg:>+10.2f}%  {'N/A':>8}  {'?':>6}")
            continue
        diff  = w_avg - wo_avg
        valid = "✅" if diff > 0.3 else ("⚠️" if diff > 0 else "❌")
        print(f"  {dim_name:>10} {weight:>4}  "
              f"{w_avg:>+10.2f}%  {wo_avg:>+10.2f}%  {diff:>+6.2f}%  {valid}"
              f"  (n={len(w_rets):,})")

    print(f"  {'-'*55}")
    print(f"  差值>0.3% = 有效维度，差值<0 = 反效果维度（建议降权或移除）\n")


# ══════════════════════════════════════════════════════════
# 参数优化（网格搜索入场门槛 + 持有时间）
# ══════════════════════════════════════════════════════════

def optimize_params(samples: List[dict], strategy: str) -> None:
    """网格搜索最优入场门槛和持有时间（基于已有样本，不重新计算评分）。

    注意：这里只优化入场门槛（score_threshold），
    持有时间优化需要重新跑回测，此处仅做门槛优化。
    """
    if not samples:
        print("  ⚠️  无样本可优化")
        return

    direction = DIRECTION.get(strategy, "long")
    print(f"\n{'='*65}")
    print(f"  参数优化：{strategy}  入场门槛网格搜索")
    print(f"{'='*65}")
    print(f"  {'门槛':>6} {'样本':>7} {'胜率':>7} {'均收益':>9} {'中位':>9} {'IR':>8} {'综合评分':>9}")
    print(f"  {'-'*60}")

    best_score = -999
    best_thresh = 0
    results = []

    for thresh in range(10, 80, 5):
        grp = [s for s in samples if s["score"] >= thresh]
        if len(grp) < 15:
            continue
        n    = len(grp)
        rets = [s["ret_pct"] for s in grp]
        wr   = sum(1 for s in grp if s["win"]) / n * 100
        avg  = sum(rets) / n
        med  = sorted(rets)[n // 2]
        std  = (sum((r - avg) ** 2 for r in rets) / (n - 1)) ** 0.5 if n > 1 else 1
        ir   = avg / std if std > 0 else 0
        # 综合评分：胜率 * 均收益 * IR（三者都要好）
        composite = (wr / 100) * max(0, avg) * max(0, ir)
        results.append((thresh, n, wr, avg, med, ir, composite))
        marker = " ◀ 当前最优" if composite > best_score and avg > 0 and wr > 50 else ""
        if composite > best_score and avg > 0 and wr > 50:
            best_score = composite
            best_thresh = thresh
        print(f"  {thresh:>6} {n:>7,} {wr:>6.1f}% {avg:>+8.2f}% {med:>+8.2f}% {ir:>8.3f} {composite:>9.4f}{marker}")

    print(f"\n  🎯 建议入场门槛: 评分 ≥ {best_thresh}  (综合评分最优)")
    print(f"     当前代码默认门槛: oversold=25, overbought=40, reversal=55")
    print()


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="币安合约 4h/1h 策略回测 — 验证评分权重是否有效"
    )
    parser.add_argument(
        "--strategy", type=str, default="all",
        choices=["oversold_4h", "oversold_1h",
                 "overbought_4h", "overbought_1h",
                 "reversal_4h", "reversal_1h", "all"],
        help="回测策略（默认 all）",
    )
    parser.add_argument(
        "--start", type=str, default="2025-11-01",
        help="回测开始日期（默认 2025-11-01，与缓存数据对齐）",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="回测结束日期（默认今天）",
    )
    parser.add_argument(
        "--sample", type=int, default=200,
        help="随机抽取交易对数量（默认 200，0=全量）",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="随机种子（默认 42，保证可复现）",
    )
    parser.add_argument(
        "--hold", type=int, default=None,
        help="覆盖默认持有 K 线根数（可选）",
    )
    parser.add_argument(
        "--optimize", action="store_true",
        help="开启入场门槛网格优化",
    )
    parser.add_argument(
        "--dim-analysis", action="store_true",
        help="开启子维度预测力分析",
    )
    args = parser.parse_args()

    from datetime import timezone as _tz
    end_date   = args.end or datetime.now(_tz.utc).strftime("%Y-%m-%d")
    start_ms   = ms_from_date(args.start)
    end_ms     = ms_from_date(end_date) + 86_400_000  # 包含当天

    strategies = (
        ["oversold_4h", "oversold_1h",
         "overbought_4h", "overbought_1h",
         "reversal_4h", "reversal_1h"]
        if args.strategy == "all"
        else [args.strategy]
    )

    print(f"📊 币安合约策略回测")
    print(f"   策略: {', '.join(strategies)}")
    print(f"   区间: {args.start} ~ {end_date}")
    print(f"   数据库: {DB_PATH}")

    if not os.path.exists(DB_PATH):
        print(f"❌ 缓存数据库不存在: {DB_PATH}")
        print("   请先运行 cron 任务让系统缓存 K 线数据")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    for strategy in strategies:
        interval  = INTERVALS[strategy]
        hold_bars = args.hold if args.hold else HOLD_BARS[strategy]

        all_symbols = get_all_symbols(conn, interval)
        print(f"\n   [{strategy}] 可用交易对: {len(all_symbols):,} 个 (interval={interval})")

        if args.sample > 0 and args.sample < len(all_symbols):
            random.seed(args.seed)
            symbols = random.sample(all_symbols, args.sample)
            print(f"   随机抽样: {len(symbols)} 个 (seed={args.seed})")
        else:
            symbols = all_symbols
            print(f"   全量回测: {len(symbols)} 个")

        print(f"\n⏳ 回测策略: {strategy}（持有 {hold_bars} 根K线）...")

        all_samples = []
        for idx, symbol in enumerate(symbols):
            try:
                s = backtest_symbol(conn, symbol, strategy, start_ms, end_ms, hold_bars)
                all_samples.extend(s)
            except Exception as e:
                pass
            if (idx + 1) % 50 == 0:
                print(f"   进度: {idx+1}/{len(symbols)}，样本: {len(all_samples):,}")

        print(f"   完成，共 {len(all_samples):,} 个样本点")
        analyze_results(all_samples, strategy, hold_bars)

        if args.optimize and all_samples:
            optimize_params(all_samples, strategy)

        if args.dim_analysis and all_samples:
            analyze_dimensions(conn, symbols, strategy, start_ms, end_ms, hold_bars)

    conn.close()


if __name__ == "__main__":
    main()
