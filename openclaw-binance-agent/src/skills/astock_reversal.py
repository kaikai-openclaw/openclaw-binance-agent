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
# 权重依据：2021-2026 回测子维度分析（500只股票，115,606个样本点）
# ✅ 有效：KDJ金叉(+0.54%) MACD反转(+0.19%) 前期跌幅(+0.20%)
# ❌ 反效果：价格企稳(-0.66%) 距底部距离(-0.37%) 长下影线(-0.14%) 底部放量(-0.09%) 均线拐头(-0.08%)
W_VOLUME_SURGE = 10    # 底部放量：降权（有信号反而略差，保留作辅助过滤）
W_PRICE_STABLE = 5     # 价格企稳：大幅降权（回测最差维度 -0.66%）
W_MA_TURN = 8          # 均线拐头：降权（-0.08% 轻微反效果）
W_MACD_REVERSAL = 22   # MACD反转：大幅提权（有效 +0.19%，趋势转折核心信号）
W_DIST_BOTTOM = 5      # 距底部距离：降权（-0.37% 反效果）
W_PRIOR_DROP = 18      # 前期跌幅：大幅提权（有效 +0.20%，跌得越深反弹空间越大）
W_TURNOVER = 5         # 换手率：降权（数据未传入实际无效）
W_KDJ_CROSS = 22       # KDJ金叉：大幅提权（最有效维度 +0.54%）
W_SHADOW = 5           # 长下影线：降权（-0.14% 轻微反效果）

# ── 参数阈值 ──────────────────────────────────────────────
VOLUME_SURGE_THRESHOLD = 2.0           # 放量倍数阈值（近 3 日均量 / 前 15 日均量）
VOLUME_SURGE_STRONG = 3.0              # 强放量
PRICE_STABLE_DAYS = 5                  # 企稳观察窗口
DROP_LOOKBACK = 60                     # 前期跌幅回看天数 (A股底部较长，从30改为60天)
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
        # ── 大盘环境过滤（前置检查）──
        skip_regime = input_data.get("skip_market_regime", False)
        if not skip_regime:
            try:
                from src.infra.market_regime import get_regime_filter
                regime_filter = get_regime_filter(client=self._client)
                regime = regime_filter.get_current_regime()

                if not regime["allow_reversal"]:
                    log.warning(
                        "[%s] 大盘熊市且未企稳，底部反转策略暂停。"
                        "trend=%s chg5d=%.1f%% reason=%s",
                        self.name, regime["trend"], regime["chg5d"], regime["reason"],
                    )
                    return {
                        "state_id": str(uuid.uuid4()),
                        "candidates": [],
                        "pipeline_run_id": str(uuid.uuid4()),
                        "filter_summary": {
                            "total_tickers": 0,
                            "after_base_filter": 0,
                            "after_reversal_filter": 0,
                            "output_count": 0,
                            "skipped_reason": "market_regime_bear",
                            "market_trend": regime["trend"],
                            "market_reason": regime["reason"],
                        },
                    }

                # 熊市时提高评分门槛（input_data 未显式指定时才覆盖）
                if "min_score" not in input_data and regime["trend"] == "bear":
                    bumped = DEFAULT_MIN_SCORE + 15
                    log.info("[%s] 大盘偏弱，底部反转门槛提升至 %d（原 %d）",
                             self.name, bumped, DEFAULT_MIN_SCORE)
                    input_data = {**input_data, "min_score": bumped}

            except Exception as e:
                log.warning("[%s] 大盘环境检查失败，降级继续运行: %s", self.name, e)

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

                # 计算辅助分析指标
                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                last_close = closes[-1]
                atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None
                rsi_val = calc_rsi(closes, RSI_PERIOD)

                scored.append({
                    "symbol": symbol,
                    "name": item.get("name", ""),
                    "close": last_close,
                    "amount": item.get("amount", 0),
                    "change_pct": item.get("change_pct", 0),
                    "signal_direction": "long",  # 底部反转策略固定为看多
                    "rsi": round(rsi_val, 2) if rsi_val else None,
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

def _find_valid_bottom(
    lows: List[float], closes: List[float], volumes: List[float],
    lookback: int = 20,
) -> tuple:
    """判断近期最低点是否构成有效底部。

    有效底部需满足：
    1. 在最低点附近（±2%）停留了至少 3 个交易日（时间维度：筑底）
    2. 底部区域有成交量放大（量能承接，不是无量阴跌）

    Returns:
        (bottom_price, is_valid, days_at_bottom, vol_confirm)
        - bottom_price:    近期最低价
        - is_valid:        是否构成有效底部（停留≥3天）
        - days_at_bottom:  在底部区域停留的天数
        - vol_confirm:     底部是否有量能确认（底部均量≥整体均量×1.2）
    """
    if len(lows) < lookback:
        return None, False, 0, False

    window_lows = lows[-lookback:]
    bottom = min(window_lows)
    if bottom <= 0:
        return None, False, 0, False

    # 底部区域：最低点上方 2% 以内
    bottom_threshold = bottom * 1.02

    # 统计在底部区域停留的天数
    days_at_bottom = sum(1 for lo in window_lows if lo <= bottom_threshold)

    # 底部区域的成交量 vs 整体均量
    vol_confirm = False
    if len(volumes) >= lookback:
        vol_window = volumes[-lookback:]
        bottom_indices = [i for i, lo in enumerate(window_lows) if lo <= bottom_threshold]
        if bottom_indices:
            bottom_vols = [vol_window[i] for i in bottom_indices]
            base_avg = sum(vol_window) / len(vol_window)
            bottom_avg = sum(bottom_vols) / len(bottom_vols)
            vol_confirm = base_avg > 0 and bottom_avg >= base_avg * 1.2

    is_valid = days_at_bottom >= 3

    return bottom, is_valid, days_at_bottom, vol_confirm


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
            
            # A股防雷：放量必须伴随阳线（收盘价>开盘价），巨量阴线可能是恐慌盘杀跌
            is_positive_line = closes[-1] > opens[-1]
            
            if vol_surge_ratio >= VOLUME_SURGE_STRONG:
                if is_positive_line:
                    vol_surge_score = W_VOLUME_SURGE
                    signals.append(f"强放量阳线{vol_surge_ratio:.1f}x")
                else:
                    # 巨量大阴线，极度危险，得分减半且标注警告
                    vol_surge_score = W_VOLUME_SURGE * 0.5
                    signals.append(f"巨量阴线警告({vol_surge_ratio:.1f}x)")
            elif vol_surge_ratio >= VOLUME_SURGE_THRESHOLD:
                if is_positive_line:
                    vol_surge_score = W_VOLUME_SURGE * (vol_surge_ratio - 1.0) / (VOLUME_SURGE_STRONG - 1.0)
                    signals.append(f"温和放量阳线{vol_surge_ratio:.1f}x")
                else:
                    vol_surge_score = (W_VOLUME_SURGE * (vol_surge_ratio - 1.0) / (VOLUME_SURGE_STRONG - 1.0)) * 0.5
                    signals.append(f"放量阴线{vol_surge_ratio:.1f}x")

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

    # ── 5. 距底部距离（8 分）──
    # 改进：底部必须经过有效性验证（筑底时间 + 量能承接），
    # 且当前价格必须处于从底部向上离开的状态，而非仍在下跌途中。
    dist_bottom_pct = None
    dist_score = 0.0
    if len(lows) >= BOTTOM_LOOKBACK:
        bottom, is_valid, days_at_bottom, vol_confirm = _find_valid_bottom(
            lows, closes, volumes, BOTTOM_LOOKBACK
        )
        if bottom and bottom > 0:
            dist_bottom_pct = (last_close - bottom) / bottom * 100
            if is_valid:
                # 必须是从底部向上离开（近 3 日收盘价在上涨）
                is_rising = (len(closes) >= 3
                             and closes[-1] > closes[-3]
                             and last_close > bottom)
                if is_rising and DIST_BOTTOM_IDEAL_MIN <= dist_bottom_pct <= DIST_BOTTOM_IDEAL_MAX:
                    base = float(W_DIST_BOTTOM)
                    if vol_confirm:
                        base = min(base * 1.2, W_DIST_BOTTOM)   # 底部有量能确认
                    if days_at_bottom >= 5:
                        base = min(base * 1.1, W_DIST_BOTTOM)   # 筑底时间越长越可靠
                    dist_score = base
                    signals.append(
                        f"有效底部反弹{dist_bottom_pct:.1f}%"
                        f"(筑底{days_at_bottom}天{'·量确认' if vol_confirm else ''})"
                    )
                elif is_rising and 0 < dist_bottom_pct < DIST_BOTTOM_IDEAL_MIN:
                    dist_score = W_DIST_BOTTOM * 0.4   # 刚离底，尚未确认
                elif dist_bottom_pct > DIST_BOTTOM_IDEAL_MAX:
                    dist_score = 0.0                   # 已涨太多，不算底部反转
                # is_rising=False 时得 0 分（还在下跌途中）
            else:
                # 底部无效（停留时间不足）→ 0 分，但记录信号供参考
                if dist_bottom_pct is not None and 0 < dist_bottom_pct <= DIST_BOTTOM_IDEAL_MAX:
                    signals.append(f"底部未确认(仅停留{days_at_bottom}天)")
        else:
            bottom = None

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
        # amount=0 表示接口未返回真实数据（腾讯接口常返回0），此时放行
        if amount is not None and amount != 0 and amount < min_amount:
            continue
        # 排除一字板（需 amount>0，排除盘前数据 equal to yesterday close）
        h, l, o = t.get("high"), t.get("low"), t.get("open")
        if amount and h is not None and l is not None and o is not None and h == l == o and h > 0:
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
