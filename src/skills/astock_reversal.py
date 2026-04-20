"""
A 股底部放量反转筛选 Skill

筛选经历持续下跌后、在底部区域出现放量企稳并开始反转的股票。
与超跌反弹（Skill-1B）的区别：超跌是"接飞刀"，反转是"确认转向"。

核心逻辑：
  下跌趋势 → 底部缩量筑底 → 突然放量（大资金进场）→ 价格企稳不再创新低
  → 均线拐头 → MACD 金叉 → 反转确认

九维度评分体系（满分 100）：
  1. 底部放量（20 分）— 核心信号，近期量能 vs 前期地量
  2. 价格企稳（15 分）— 不再创新低 + 波动收窄
  3. 均线拐头（15 分）— MA5 上穿 MA10 或 MA10 拐头向上
  4. MACD 反转信号（12 分）— 零轴下方金叉 or 底背离
  5. 距底部距离（10 分）— 距近期最低点 3%-10% 最佳
  6. 前期跌幅深度（8 分）— 跌得越深反转空间越大
  7. 换手率异常（8 分）— 底部换手率突然放大
  8. KDJ 低位金叉（7 分）— 超卖区金叉确认
  9. 长下影线（5 分）— 下方有强支撑

适用场景：波段交易（5 天 ~ 3 周），捕捉底部反转的第一波上涨。
风险提示：底部反转可能是"假突破"，必须设止损（跌破近期低点即止损）。

数据源：AkshareClient（K 线优先走本地 SQLite 缓存）
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill
from src.skills.skill1_collect import (
    calc_ema,
    calc_rsi,
    calc_macd,
    calc_atr,
    calc_returns,
    calc_correlation,
    KLINE_LIMIT,
    CORRELATION_THRESHOLD,
    RSI_PERIOD,
    ATR_PERIOD,
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 默认参数
# ══════════════════════════════════════════════════════════

DEFAULT_MIN_AMOUNT = 80_000_000        # 最低成交额 8000 万（底部股成交额偏低）
DEFAULT_MIN_PRICE = 3.0
DEFAULT_MIN_KLINES = 60
DEFAULT_MIN_SCORE = 40
DEFAULT_MAX_CANDIDATES = 20

# 排除关键词
_EXCLUDE_KEYWORDS = {"ST", "*ST", "退", "B股", "PT"}

# ── 评分权重（满分 100）──────────────────────────────────
W_VOLUME_SURGE = 20    # 底部放量（核心信号）
W_PRICE_STABLE = 15    # 价格企稳
W_MA_TURN = 15         # 均线拐头
W_MACD_REVERSAL = 12   # MACD 反转信号
W_DIST_BOTTOM = 10     # 距底部距离
W_PRIOR_DROP = 8       # 前期跌幅深度
W_TURNOVER = 8         # 换手率异常
W_KDJ_CROSS = 7        # KDJ 低位金叉
W_SHADOW = 5           # 长下影线

# ── 参数阈值 ──────────────────────────────────────────────
VOLUME_SURGE_THRESHOLD = 2.0           # 放量倍数阈值（近 3 日均量 / 前 15 日均量）
VOLUME_SURGE_STRONG = 3.0              # 强放量
PRICE_STABLE_DAYS = 5                  # 企稳观察窗口
DROP_LOOKBACK = 30                     # 前期跌幅回看天数
BOTTOM_LOOKBACK = 20                   # 近期最低点回看天数
DIST_BOTTOM_IDEAL_MIN = 3.0            # 距底部理想距离下限（%）
DIST_BOTTOM_IDEAL_MAX = 10.0           # 距底部理想距离上限（%）
SHADOW_RATIO_THRESHOLD = 2.0           # 下影线长度 / 实体长度 ≥ 2 倍

# KDJ 参数
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3


class AStockReversalSkill(BaseSkill):
    """A 股底部放量反转筛选 Skill。"""

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "astock_reversal"
        self._client = client

    def run(self, input_data: dict) -> dict:
        min_amount = input_data.get("min_amount", DEFAULT_MIN_AMOUNT)
        min_price = input_data.get("min_price", DEFAULT_MIN_PRICE)
        min_klines = input_data.get("min_klines", DEFAULT_MIN_KLINES)
        min_score = input_data.get("min_score", DEFAULT_MIN_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")
        exclude_kcb = input_data.get("exclude_kcb", False)

        pipeline_run_id = str(uuid.uuid4())

        all_tickers = self._client.get_spot_all()
        total_count = len(all_tickers)

        if target_symbols:
            pool = _build_target_pool(all_tickers, target_symbols)
            if not pool and hasattr(self._client, "get_spot_by_hist"):
                pool = self._client.get_spot_by_hist(target_symbols)
        else:
            pool = _base_filter(all_tickers, min_amount, min_price)

        # 排除科创板（688 开头）
        if exclude_kcb and not target_symbols:
            before = len(pool)
            pool = [p for p in pool if not p["symbol"].startswith("688")]
            log.info("[reversal] 排除科创板: %d → %d", before, len(pool))

        log.info("[reversal] Step1: %d/%d 通过基础过滤", len(pool), total_count)

        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        for item in pool:
            symbol = item["symbol"]
            try:
                klines = self._client.get_klines(symbol, "daily", KLINE_LIMIT)
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                opens = [float(k[1]) for k in klines]
                volumes = [float(k[5]) for k in klines]

                result = _calc_reversal_score(
                    closes, highs, lows, opens, volumes,
                    item.get("turnover", 0),
                )

                if result["total_score"] < min_score and not target_symbols:
                    continue

                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                last_close = closes[-1]
                atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None

                scored.append({
                    "symbol": symbol,
                    "name": item.get("name", ""),
                    "close": last_close,
                    "amount": item.get("amount", 0),
                    "change_pct": item.get("change_pct", 0),
                    "reversal_score": result["total_score"],
                    "volume_surge_score": result["volume_surge_score"],
                    "volume_surge_ratio": result["volume_surge_ratio"],
                    "price_stable_score": result["price_stable_score"],
                    "ma_turn_score": result["ma_turn_score"],
                    "ma_turn_detail": result["ma_turn_detail"],
                    "macd_reversal_score": result["macd_reversal_score"],
                    "macd_detail": result["macd_detail"],
                    "dist_bottom_pct": result["dist_bottom_pct"],
                    "dist_bottom_score": result["dist_bottom_score"],
                    "prior_drop_pct": result["prior_drop_pct"],
                    "prior_drop_score": result["prior_drop_score"],
                    "turnover_score": result["turnover_score"],
                    "kdj_score": result["kdj_score"],
                    "shadow_score": result["shadow_score"],
                    "signal_details": result["signal_details"],
                    "atr_pct": atr_pct,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[reversal] %s 分析失败: %s", symbol, exc)

        scored.sort(key=lambda x: x["reversal_score"], reverse=True)
        candidates = _deduplicate(scored, returns_map, max_cands)

        log.info("[reversal] 完成: pool=%d, scored=%d, output=%d",
                 len(pool), len(scored), len(candidates))

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
            "filter_summary": {
                "total_tickers": total_count,
                "after_base_filter": len(pool),
                "after_reversal_filter": len(scored),
                "output_count": len(candidates),
            },
        }


# ══════════════════════════════════════════════════════════
# 九维度反转评分
# ══════════════════════════════════════════════════════════

def _calc_reversal_score(
    closes: List[float], highs: List[float], lows: List[float],
    opens: List[float], volumes: List[float], turnover: float,
) -> dict:
    signals = []
    last_close = closes[-1]

    # ── 1. 底部放量（20 分）──
    # 核心信号：近 3 日均量 vs 前 15 日均量（排除最近 3 日）
    vol_surge_ratio = 0.0
    vol_surge_score = 0.0
    if len(volumes) >= 20:
        recent_avg = sum(volumes[-3:]) / 3
        base_avg = sum(volumes[-18:-3]) / 15
        if base_avg > 0:
            vol_surge_ratio = recent_avg / base_avg
            if vol_surge_ratio >= VOLUME_SURGE_STRONG:
                vol_surge_score = W_VOLUME_SURGE
                signals.append(f"强放量{vol_surge_ratio:.1f}x")
            elif vol_surge_ratio >= VOLUME_SURGE_THRESHOLD:
                vol_surge_score = W_VOLUME_SURGE * (vol_surge_ratio - 1.0) / (VOLUME_SURGE_STRONG - 1.0)
                signals.append(f"放量{vol_surge_ratio:.1f}x")

    # ── 2. 价格企稳（15 分）──
    # 近 5 日不再创新低 + 波动收窄
    price_stable_score = 0.0
    if len(closes) >= BOTTOM_LOOKBACK + PRICE_STABLE_DAYS:
        recent_low = min(lows[-PRICE_STABLE_DAYS:])
        prior_low = min(lows[-(BOTTOM_LOOKBACK + PRICE_STABLE_DAYS):-PRICE_STABLE_DAYS])
        # 近 5 日最低价高于前期最低价 = 不再创新低
        if recent_low >= prior_low * 0.99:  # 允许 1% 误差
            price_stable_score += 8.0
            signals.append("不再创新低")
        # 近 5 日振幅收窄（ATR 缩小）
        recent_range = max(highs[-PRICE_STABLE_DAYS:]) - min(lows[-PRICE_STABLE_DAYS:])
        prior_range = max(highs[-15:-5]) - min(lows[-15:-5]) if len(highs) >= 15 else recent_range * 2
        if prior_range > 0 and recent_range < prior_range * 0.7:
            price_stable_score += 7.0
            signals.append("波动收窄")
    price_stable_score = min(price_stable_score, W_PRICE_STABLE)

    # ── 3. 均线拐头（15 分）──
    ma_turn_score, ma_turn_detail = _score_ma_turn(closes)
    if ma_turn_detail:
        signals.append(ma_turn_detail)

    # ── 4. MACD 反转信号（12 分）──
    macd_score, macd_detail = _score_macd_reversal(closes)
    if macd_detail:
        signals.append(macd_detail)

    # ── 5. 距底部距离（10 分）──
    dist_bottom_pct = None
    dist_score = 0.0
    if len(lows) >= BOTTOM_LOOKBACK:
        bottom = min(lows[-BOTTOM_LOOKBACK:])
        if bottom > 0:
            dist_bottom_pct = (last_close - bottom) / bottom * 100
            # 3%-10% 是理想区间（刚离开底部，还有空间）
            if DIST_BOTTOM_IDEAL_MIN <= dist_bottom_pct <= DIST_BOTTOM_IDEAL_MAX:
                dist_score = W_DIST_BOTTOM
                signals.append(f"距底部{dist_bottom_pct:.1f}%(理想)")
            elif 0 < dist_bottom_pct < DIST_BOTTOM_IDEAL_MIN:
                dist_score = W_DIST_BOTTOM * 0.5  # 太近，可能还没企稳
            elif DIST_BOTTOM_IDEAL_MAX < dist_bottom_pct <= 20:
                dist_score = W_DIST_BOTTOM * 0.3  # 稍远，但还行
            # > 20% 说明已经涨了一段，不算底部反转

    # ── 6. 前期跌幅深度（8 分）──
    prior_drop_pct = None
    prior_drop_score = 0.0
    if len(closes) >= DROP_LOOKBACK + 1:
        base = max(closes[-(DROP_LOOKBACK + 1):-PRICE_STABLE_DAYS])  # 前期高点
        if base > 0:
            prior_drop_pct = (last_close - base) / base * 100
            if prior_drop_pct < -20:
                prior_drop_score = W_PRIOR_DROP
                signals.append(f"前期跌{prior_drop_pct:.1f}%")
            elif prior_drop_pct < -10:
                prior_drop_score = W_PRIOR_DROP * 0.6

    # ── 7. 换手率异常（8 分）──
    turnover_score = 0.0
    if turnover and turnover >= 3.0:
        if turnover <= 15.0:
            turnover_score = W_TURNOVER
            signals.append(f"换手率{turnover:.1f}%")
        elif turnover <= 25.0:
            turnover_score = W_TURNOVER * 0.5  # 过高可能是出货

    # ── 8. KDJ 低位金叉（7 分）──
    kdj_score = _score_kdj_golden_cross(closes, highs, lows)
    if kdj_score > 0:
        signals.append("KDJ低位金叉")

    # ── 9. 长下影线（5 分）──
    shadow_score = _score_lower_shadow(closes, opens, highs, lows)
    if shadow_score > 0:
        signals.append("长下影线")

    total = (vol_surge_score + price_stable_score + ma_turn_score +
             macd_score + dist_score + prior_drop_score +
             turnover_score + kdj_score + shadow_score)

    return {
        "total_score": round(total),
        "volume_surge_score": round(vol_surge_score),
        "volume_surge_ratio": round(vol_surge_ratio, 2),
        "price_stable_score": round(price_stable_score),
        "ma_turn_score": round(ma_turn_score),
        "ma_turn_detail": ma_turn_detail,
        "macd_reversal_score": round(macd_score),
        "macd_detail": macd_detail,
        "dist_bottom_pct": round(dist_bottom_pct, 2) if dist_bottom_pct is not None else None,
        "dist_bottom_score": round(dist_score),
        "prior_drop_pct": round(prior_drop_pct, 2) if prior_drop_pct is not None else None,
        "prior_drop_score": round(prior_drop_score),
        "turnover_score": round(turnover_score),
        "kdj_score": round(kdj_score),
        "shadow_score": round(shadow_score),
        "signal_details": " | ".join(signals) if signals else "无反转信号",
    }


# ══════════════════════════════════════════════════════════
# 子维度评分函数
# ══════════════════════════════════════════════════════════

def _score_ma_turn(closes: List[float]) -> tuple:
    """均线拐头评分（满分 15）。

    检测短期均线从下降转为上升：
    - MA5 上穿 MA10（金叉）：15 分
    - MA5 拐头向上（但还在 MA10 下方）：10 分
    - MA10 拐头向上：7 分
    """
    if len(closes) < 15:
        return 0.0, ""

    ma5_now = sum(closes[-5:]) / 5
    ma5_3d = sum(closes[-8:-3]) / 5
    ma10_now = sum(closes[-10:]) / 10
    ma10_3d = sum(closes[-13:-3]) / 10

    # MA5 上穿 MA10（金叉）
    if ma5_now > ma10_now and ma5_3d <= ma10_3d:
        return W_MA_TURN, "MA5上穿MA10(金叉)"

    # MA5 拐头向上
    if ma5_now > ma5_3d and ma5_3d < sum(closes[-11:-6]) / 5:
        if ma5_now > ma10_now:
            return 12.0, "MA5拐头向上(在MA10上方)"
        return 10.0, "MA5拐头向上"

    # MA10 拐头向上
    if ma10_now > ma10_3d and ma10_3d < sum(closes[-16:-6]) / 10:
        return 7.0, "MA10拐头向上"

    return 0.0, ""


def _score_macd_reversal(closes: List[float]) -> tuple:
    """MACD 反转信号评分（满分 12）。

    - 零轴下方金叉（MACD 线上穿信号线，且都在零轴下方）：12 分
    - MACD 底背离（价格新低但 MACD 未新低）：10 分
    - 柱状图由负转正：6 分
    """
    macd = calc_macd(closes)
    ml = macd.get("macd_line")
    sl = macd.get("signal_line")
    hist = macd.get("histogram")

    if ml is None or sl is None or hist is None:
        return 0.0, ""

    # 零轴下方金叉
    if ml < 0 and sl < 0 and ml > sl and hist > 0:
        return W_MACD_REVERSAL, "MACD零轴下方金叉"

    # 底背离检测
    if _check_macd_divergence(closes):
        return 10.0, "MACD底背离"

    # 柱状图由负转正
    if hist > 0 and ml < 0:
        return 6.0, "MACD柱状图转正"

    return 0.0, ""


def _check_macd_divergence(closes: List[float], lookback: int = 40) -> bool:
    """检测 MACD 底背离。"""
    macd_data = calc_macd(closes)
    if macd_data.get("histogram") is None or len(closes) < lookback + 10:
        return False
    recent = closes[-lookback:]
    base_idx = len(closes) - lookback
    min_idx = min(range(len(recent)), key=lambda i: recent[i])
    prev_min_idx = None
    for i in range(max(0, min_idx - 5) - 1, -1, -1):
        if prev_min_idx is None or recent[i] < recent[prev_min_idx]:
            prev_min_idx = i
    if prev_min_idx is None or recent[min_idx] >= recent[prev_min_idx]:
        return False
    h1 = calc_macd(closes[:base_idx + prev_min_idx + 1]).get("histogram")
    h2 = calc_macd(closes[:base_idx + min_idx + 1]).get("histogram")
    return h1 is not None and h2 is not None and h2 > h1


def _score_kdj_golden_cross(
    closes: List[float], highs: List[float], lows: List[float],
) -> float:
    """KDJ 低位金叉评分（满分 7）。

    J 值从负值区域上穿 0 线，或 K 上穿 D 且都在 30 以下。
    """
    if len(closes) < KDJ_PERIOD + KDJ_M1 + KDJ_M2 + 3:
        return 0.0

    # 计算最近两天的 KDJ
    def _calc_kdj(c, h, l):
        rsvs = []
        for i in range(KDJ_PERIOD - 1, len(c)):
            hh = max(h[i - KDJ_PERIOD + 1: i + 1])
            ll = min(l[i - KDJ_PERIOD + 1: i + 1])
            rsvs.append(50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100)
        if not rsvs:
            return None, None, None
        k = d = rsvs[0]
        for rsv in rsvs[1:]:
            k = (k * (KDJ_M1 - 1) + rsv) / KDJ_M1
            d = (d * (KDJ_M2 - 1) + k) / KDJ_M2
        j = 3 * k - 2 * d
        return k, d, j

    k_now, d_now, j_now = _calc_kdj(closes, highs, lows)
    k_prev, d_prev, j_prev = _calc_kdj(closes[:-1], highs[:-1], lows[:-1])

    if k_now is None or k_prev is None:
        return 0.0

    # K 上穿 D 且都在 30 以下（低位金叉）
    if k_now > d_now and k_prev <= d_prev and k_now < 50:
        return W_KDJ_CROSS

    # J 值从负值上穿 0
    if j_now is not None and j_prev is not None:
        if j_now > 0 and j_prev < 0:
            return W_KDJ_CROSS * 0.7

    return 0.0


def _score_lower_shadow(
    closes: List[float], opens: List[float],
    highs: List[float], lows: List[float],
) -> float:
    """长下影线评分（满分 5）。

    近 3 天内出现长下影线 = 下方有强支撑。
    下影线长度 / 实体长度 ≥ 2 倍。
    """
    for i in range(-3, 0):
        if i >= -len(closes):
            c, o, h, l = closes[i], opens[i], highs[i], lows[i]
            body = abs(c - o)
            lower_shadow = min(c, o) - l
            if body > 0 and lower_shadow >= body * SHADOW_RATIO_THRESHOLD:
                return W_SHADOW
            # 十字星也算（实体极小但下影线长）
            if body < (h - l) * 0.1 and lower_shadow > (h - l) * 0.5:
                return W_SHADOW * 0.7
    return 0.0


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def _build_target_pool(all_tickers, target_symbols):
    normalized = set()
    for s in target_symbols:
        s = s.strip().upper()
        for pfx in ("SH", "SZ", "BJ"):
            if s.startswith(pfx) and len(s) > 2:
                s = s[len(pfx):]
        s = s.replace(".", "")
        normalized.add(s)
    return [
        {**t, "symbol": (t.get("symbol", "")[-6:] if len(t.get("symbol", "")) > 6 else t.get("symbol", ""))}
        for t in all_tickers
        if (t.get("symbol", "")[-6:] if len(t.get("symbol", "")) > 6 else t.get("symbol", "")) in normalized
    ]


def _base_filter(tickers, min_amount, min_price):
    result = []
    for t in tickers:
        raw_symbol = t.get("symbol", "")
        name = t.get("name", "")
        symbol = raw_symbol[-6:] if len(raw_symbol) > 6 else raw_symbol
        if any(kw in name for kw in _EXCLUDE_KEYWORDS):
            continue
        if symbol.startswith(("8", "9")):
            continue
        close = t.get("close")
        if close is None or close <= 0 or close < min_price:
            continue
        amount = t.get("amount")
        if amount is None or amount < min_amount:
            continue
        # 排除一字板
        h, l, o = t.get("high"), t.get("low"), t.get("open")
        if h is not None and l is not None and o is not None and h == l == o and h > 0:
            continue
        result.append({**t, "symbol": symbol})
    return result


def _deduplicate(scored, returns_map, max_cands):
    selected, selected_returns = [], []
    for item in scored:
        if len(selected) >= max_cands:
            break
        rets = returns_map.get(item["symbol"], [])
        if not any(calc_correlation(rets, sr) > CORRELATION_THRESHOLD for sr in selected_returns):
            selected.append(item)
            selected_returns.append(rets)
    return selected
