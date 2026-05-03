#!/usr/bin/env python3
"""
A 股策略回测脚本

对三种策略的评分体系做历史回测，验证权重参数是否有效。

核心问题：
  评分高的股票，后续涨幅是否真的比评分低的好？
  各维度权重分配是否合理？
  入场门槛设多少最优？

回测逻辑：
  1. 从 kline_cache.db 读取所有股票历史数据
  2. 对每个历史时间点（滑动窗口）计算评分
  3. 记录评分后 N 天的实际涨跌幅
  4. 按评分分组统计胜率、平均收益、最大回撤

用法:
    python3 scripts/backtest_astock.py --strategy trend
    python3 scripts/backtest_astock.py --strategy oversold_short
    python3 scripts/backtest_astock.py --strategy oversold_long
    python3 scripts/backtest_astock.py --strategy reversal
    python3 scripts/backtest_astock.py --strategy all
    python3 scripts/backtest_astock.py --strategy trend --start 2023-01-01 --end 2024-12-31
    python3 scripts/backtest_astock.py --strategy trend --sample 500
"""
import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.skills.skill1a_collect import _calc_trend_score
from src.skills.skill1b_oversold import (
    _calc_short_term_score, _calc_long_term_score,
    ST_RSI_THRESHOLD, ST_BIAS_THRESHOLD, ST_CONSECUTIVE_DOWN,
    ST_DROP_PCT, ST_DROP_LOOKBACK,
    LT_RSI_THRESHOLD, LT_BIAS_THRESHOLD, LT_CONSECUTIVE_DOWN,
    LT_DROP_PCT, LT_DROP_LOOKBACK,
)
from src.skills.astock_reversal import _calc_reversal_score

DB_PATH = os.path.join(PROJECT_ROOT, "data", "kline_cache.db")

# 各策略默认持有天数
HOLD_DAYS = {
    "trend":          10,   # 趋势选股：持有 10 个交易日
    "oversold_short":  5,   # 短期超跌：持有 5 个交易日
    "oversold_long":  15,   # 长期超跌：持有 15 个交易日
    "reversal":        8,   # 底部反转：持有 8 个交易日
}

# 评分分组边界
SCORE_BINS   = [0, 20, 30, 40, 50, 60, 75, 101]
SCORE_LABELS = ["0-20", "20-30", "30-40", "40-50", "50-60", "60-75", "75+"]

# 每只股票每隔多少天取一个样本点（避免相邻样本高度相关）
SAMPLE_INTERVAL = 5

# 最少需要多少根 K 线才参与回测
MIN_KLINES = 150


# ══════════════════════════════════════════════════════════
# 数据读取
# ══════════════════════════════════════════════════════════

def get_all_symbols(conn: sqlite3.Connection) -> List[str]:
    """获取缓存中所有个股代码（排除指数 idx_ 前缀）。"""
    cursor = conn.execute(
        "SELECT DISTINCT symbol FROM kline_cache "
        "WHERE symbol NOT LIKE 'idx_%' AND adjust = 'qfq' "
        "ORDER BY symbol"
    )
    return [r[0] for r in cursor.fetchall()]


def get_klines(conn: sqlite3.Connection, symbol: str,
               start_date: str, end_date: str) -> List[dict]:
    """读取指定区间的 K 线，返回 dict 列表。"""
    cursor = conn.execute(
        "SELECT date, open, high, low, close, volume "
        "FROM kline_cache "
        "WHERE symbol = ? AND adjust = 'qfq' AND date >= ? AND date <= ? "
        "ORDER BY date ASC",
        (symbol, start_date, end_date),
    )
    return [
        {"date": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5]}
        for r in cursor.fetchall()
    ]


# ══════════════════════════════════════════════════════════
# 评分函数适配器
# ══════════════════════════════════════════════════════════

def score_trend(klines: List[dict]) -> Optional[float]:
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    volumes = [k["volume"] for k in klines]
    if len(closes) < 60:
        return None
    result = _calc_trend_score(closes, highs, lows, volumes, turnover=0)
    # 趋势策略只做多，方向不对直接排除
    if result["direction"] != "long":
        return None
    return float(result["total_score"])


def score_oversold_short(klines: List[dict]) -> Optional[float]:
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    volumes = [k["volume"] for k in klines]
    if len(closes) < 30:
        return None
    result = _calc_short_term_score(
        closes, highs, lows, volumes,
        ST_RSI_THRESHOLD, ST_BIAS_THRESHOLD, ST_CONSECUTIVE_DOWN,
        ST_DROP_PCT, ST_DROP_LOOKBACK,
    )
    return float(result["oversold_score"])


def score_oversold_long(klines: List[dict]) -> Optional[float]:
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    volumes = [k["volume"] for k in klines]
    if len(closes) < 60:
        return None
    result = _calc_long_term_score(
        closes, highs, lows, volumes,
        LT_RSI_THRESHOLD, LT_BIAS_THRESHOLD, LT_CONSECUTIVE_DOWN,
        LT_DROP_PCT, LT_DROP_LOOKBACK,
    )
    return float(result["oversold_score"])


def score_reversal(klines: List[dict]) -> Optional[float]:
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    opens   = [k["open"]   for k in klines]
    volumes = [k["volume"] for k in klines]
    if len(closes) < 60:
        return None
    result = _calc_reversal_score(closes, highs, lows, opens, volumes, turnover=0)
    return float(result["total_score"])


def score_reversal_detail(klines: List[dict]) -> Optional[dict]:
    """返回 reversal 各子维度评分，用于分析哪个维度最有预测力。"""
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    opens   = [k["open"]   for k in klines]
    volumes = [k["volume"] for k in klines]
    if len(closes) < 60:
        return None
    return _calc_reversal_score(closes, highs, lows, opens, volumes, turnover=0)


SCORE_FN = {
    "trend":          score_trend,
    "oversold_short": score_oversold_short,
    "oversold_long":  score_oversold_long,
    "reversal":       score_reversal,
}


# ══════════════════════════════════════════════════════════
# 回测核心
# ══════════════════════════════════════════════════════════

def backtest_symbol(conn: sqlite3.Connection, symbol: str,
                    strategy: str, start_date: str, end_date: str,
                    hold_days: int) -> List[dict]:
    """对单只股票做滑动窗口回测，返回样本点列表。"""
    score_fn = SCORE_FN[strategy]
    # 多拉一段历史用于计算指标（最多 200 根）
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                   - timedelta(days=300)).strftime("%Y-%m-%d")
    all_klines = get_klines(conn, symbol, fetch_start, end_date)

    if len(all_klines) < MIN_KLINES + hold_days:
        return []

    # 找到 start_date 在 all_klines 中的位置
    start_idx = 0
    for i, k in enumerate(all_klines):
        if k["date"] >= start_date:
            start_idx = i
            break

    results = []
    i = start_idx
    while i < len(all_klines) - hold_days:
        # 用 i 之前的数据计算评分（最多取 200 根）
        window = all_klines[max(0, i - 200): i + 1]
        if len(window) < 30:
            i += SAMPLE_INTERVAL
            continue

        try:
            score = score_fn(window)
        except Exception:
            i += SAMPLE_INTERVAL
            continue

        if score is None:
            i += SAMPLE_INTERVAL
            continue

        # 计算持有 hold_days 后的涨跌幅
        entry_close = all_klines[i]["close"]
        exit_close  = all_klines[i + hold_days]["close"]
        if entry_close <= 0:
            i += SAMPLE_INTERVAL
            continue

        ret_pct = (exit_close - entry_close) / entry_close * 100

        # 计算持仓期间最大回撤
        period = all_klines[i: i + hold_days + 1]
        peak = entry_close
        max_drawdown = 0.0
        for k in period[1:]:
            if k["close"] > peak:
                peak = k["close"]
            dd = (k["close"] - peak) / peak * 100
            if dd < max_drawdown:
                max_drawdown = dd

        results.append({
            "symbol":       symbol,
            "date":         all_klines[i]["date"],
            "score":        round(score),
            "ret_pct":      round(ret_pct, 2),
            "max_drawdown": round(max_drawdown, 2),
            "win":          ret_pct > 0,
        })
        i += SAMPLE_INTERVAL

    return results


# ══════════════════════════════════════════════════════════
# 统计分析
# ══════════════════════════════════════════════════════════

def analyze_results(samples: List[dict], strategy: str, hold_days: int) -> None:
    """按评分分组统计，输出回测报告。"""
    if not samples:
        print("  ⚠️  无有效样本")
        return

    total = len(samples)
    all_rets = [s["ret_pct"] for s in samples]
    overall_win = sum(1 for s in samples if s["win"]) / total * 100
    overall_avg = sum(all_rets) / total

    print(f"\n{'='*65}")
    print(f"  策略: {strategy}  |  持有: {hold_days} 个交易日  |  样本: {total:,} 个")
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

    # 评分有效性验证：高分组 vs 低分组
    if len(group_stats) >= 2:
        lo_g  = group_stats[0]
        hi_g  = group_stats[-1]
        diff  = hi_g[3] - lo_g[3]
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

    # 按年份分层，看不同市场环境下的表现
    if samples and "date" in samples[0]:
        print(f"\n  📅 按年份分层（验证市场环境影响）:")
        print(f"  {'年份':>6} {'样本':>7} {'胜率':>7} {'均收益':>9}  评分≥40子集")
        print(f"  {'-'*55}")
        years = sorted(set(s["date"][:4] for s in samples))
        for yr in years:
            yr_all  = [s for s in samples if s["date"].startswith(yr)]
            yr_high = [s for s in yr_all  if s["score"] >= 40]
            if not yr_all:
                continue
            wr_all  = sum(1 for s in yr_all  if s["win"]) / len(yr_all)  * 100
            avg_all = sum(s["ret_pct"] for s in yr_all)  / len(yr_all)
            if yr_high:
                avg_h = sum(s["ret_pct"] for s in yr_high) / len(yr_high)
                wr_h  = sum(1 for s in yr_high if s["win"]) / len(yr_high) * 100
                high_str = f"  胜率{wr_h:.0f}% 均收益{avg_h:+.2f}% (n={len(yr_high)})"
            else:
                high_str = "  无样本"
            print(f"  {yr:>6} {len(yr_all):>7,} {wr_all:>6.1f}% {avg_all:>+8.2f}%{high_str}")

    print()


def analyze_reversal_dimensions(conn, symbols, start_date, end_date, hold_days):
    """分析 reversal 各子维度与收益的相关性，找出真正有预测力的维度。"""
    from src.skills.astock_reversal import _calc_reversal_score

    DIMS = [
        ("volume_surge_score",  "底部放量",   10),
        ("price_stable_score",  "价格企稳",    5),
        ("ma_turn_score",       "均线拐头",    8),
        ("macd_reversal_score", "MACD反转",   22),
        ("dist_bottom_score",   "距底部距离",  5),
        ("prior_drop_score",    "前期跌幅",   18),
        ("turnover_score",      "换手率",      5),
        ("kdj_score",           "KDJ金叉",    22),
        ("shadow_score",        "长下影线",    5),
    ]

    print(f"\n{'='*65}")
    print(f"  reversal 子维度预测力分析（各维度得分 vs 持有{hold_days}日收益）")
    print(f"{'='*65}")
    print(f"  {'维度':>10} {'权重':>4} {'有信号均收益':>12} {'无信号均收益':>12} {'差值':>8} {'有效?':>6}")
    print(f"  {'-'*55}")

    random.seed(42)
    sample_syms = random.sample(symbols, min(200, len(symbols)))

    dim_data = {d[0]: {"with": [], "without": []} for d in DIMS}

    for symbol in sample_syms:
        fetch_start = (datetime.strptime(start_date, "%Y-%m-%d")
                       - timedelta(days=300)).strftime("%Y-%m-%d")
        klines = get_klines(conn, symbol, fetch_start, end_date)
        if len(klines) < MIN_KLINES + hold_days:
            continue

        start_idx = next((i for i, k in enumerate(klines) if k["date"] >= start_date), 0)
        i = start_idx
        while i < len(klines) - hold_days:
            window = klines[max(0, i - 200): i + 1]
            if len(window) < 60:
                i += SAMPLE_INTERVAL; continue
            try:
                c = [k["close"] for k in window]; h = [k["high"] for k in window]
                l = [k["low"] for k in window];   o = [k["open"] for k in window]
                v = [k["volume"] for k in window]
                result = _calc_reversal_score(c, h, l, o, v, turnover=0)
            except Exception:
                i += SAMPLE_INTERVAL; continue

            entry = klines[i]["close"]
            exit_ = klines[i + hold_days]["close"]
            if entry <= 0:
                i += SAMPLE_INTERVAL; continue
            ret = (exit_ - entry) / entry * 100

            for dim_key, _, _ in DIMS:
                score = result.get(dim_key, 0)
                if score and score > 0:
                    dim_data[dim_key]["with"].append(ret)
                else:
                    dim_data[dim_key]["without"].append(ret)
            i += SAMPLE_INTERVAL

    # 调试：输出各维度样本量
    total_pts = sum(len(v["with"]) + len(v["without"]) for v in dim_data.values())
    first_dim = list(dim_data.values())[0]
    print(f"  [调试] 总样本点: {total_pts//len(DIMS):,}  "
          f"有信号样本示例(底部放量): {len(dim_data['volume_surge_score']['with']):,}  "
          f"无信号: {len(dim_data['volume_surge_score']['without']):,}")

    for dim_key, dim_name, weight in DIMS:
        w_rets  = dim_data[dim_key]["with"]
        wo_rets = dim_data[dim_key]["without"]
        if not wo_rets:
            print(f"  {dim_name:>10} {weight:>4}  {'N/A':>12}  {'N/A':>12}  {'N/A':>8}  {'?':>6}")
            continue
        w_avg  = sum(w_rets)  / len(w_rets)  if w_rets  else float('nan')
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
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="A 股策略回测 — 验证评分权重是否有效")
    parser.add_argument("--strategy", type=str, default="all",
                        choices=["trend", "oversold_short", "oversold_long", "reversal", "all"],
                        help="回测策略（默认 all）")
    parser.add_argument("--start", type=str, default="2022-01-01",
                        help="回测开始日期（默认 2022-01-01）")
    parser.add_argument("--end", type=str, default=None,
                        help="回测结束日期（默认今天）")
    parser.add_argument("--sample", type=int, default=300,
                        help="随机抽取股票数量（默认 300，0=全量）")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子（默认 42，保证可复现）")
    args = parser.parse_args()

    end_date   = args.end or datetime.now().strftime("%Y-%m-%d")
    strategies = (["trend", "oversold_short", "oversold_long", "reversal"]
                  if args.strategy == "all" else [args.strategy])

    print(f"📊 A 股策略回测")
    print(f"   策略: {', '.join(strategies)}")
    print(f"   区间: {args.start} ~ {end_date}")
    print(f"   数据库: {DB_PATH}")

    if not os.path.exists(DB_PATH):
        print(f"❌ 缓存数据库不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    all_symbols = get_all_symbols(conn)
    print(f"   可用股票: {len(all_symbols):,} 只")

    if args.sample > 0 and args.sample < len(all_symbols):
        random.seed(args.seed)
        symbols = random.sample(all_symbols, args.sample)
        print(f"   随机抽样: {len(symbols)} 只（seed={args.seed}）")
    else:
        symbols = all_symbols
        print(f"   全量回测: {len(symbols)} 只")

    for strategy in strategies:
        hold_days = HOLD_DAYS[strategy]
        print(f"\n⏳ 回测策略: {strategy}（持有 {hold_days} 日）...")

        all_samples = []
        for idx, symbol in enumerate(symbols):
            try:
                s = backtest_symbol(conn, symbol, strategy,
                                    args.start, end_date, hold_days)
                all_samples.extend(s)
            except Exception:
                pass
            if (idx + 1) % 50 == 0:
                print(f"   进度: {idx+1}/{len(symbols)}，样本: {len(all_samples):,}")

        print(f"   完成，共 {len(all_samples):,} 个样本点")
        analyze_results(all_samples, strategy, hold_days)

        # reversal 策略额外做子维度分析
        if strategy == "reversal" and all_samples:
            analyze_reversal_dimensions(conn, symbols, args.start, end_date, hold_days)

    conn.close()


if __name__ == "__main__":
    main()
