"""
加密货币合约插针（Wick/Spike）检测与交易 Skill（双模式）

插针本质：大资金在短时间内定点清洗杠杆仓位（爆多/爆空），价格剧烈偏离后
快速回归，在 K 线上留下极长的上影线或下影线，实体很小。

与超跌/反转的区别：
  超跌 = 持续下跌后的超卖信号（趋势性，持续数小时~数天）
  反转 = 底部构筑完成后的转向确认（需要多根 K 线确认）
  插针 = 单根或极少数 K 线的瞬时异常（事件驱动，分钟级时效）

核心交易逻辑：
  插针 → 流动性猎杀完成 → 杠杆仓位被清洗 → 价格回归均值 → 反向入场
  止损 = 影线尖端（再次跌破说明不是插针而是趋势突破）

## 短期模式（15m K 线）— 捕捉实时插针
  适用场景：插针发生后快速入场，捕捉价格回归
  K 线周期：15m（100 根 ≈ 25 小时）
  核心信号：影线比率极端 + 成交量暴增 + 资金费率极端
  持仓周期：1h ~ 12h

## 长期模式（1h K 线）— 捕捉已确认的插针形态
  适用场景：等 K 线收线确认插针形态后入场，假信号更少
  K 线周期：1h（100 根 ≈ 4 天）
  核心信号：影线比率 + 关键价位触及 + 资金费率 + 价格回归确认
  持仓周期：4h ~ 24h

七维度评分体系（满分 100）：
  1. 影线比率 — 影线长度 / 实体长度，越大越典型
  2. 插针幅度 — 影线尖端偏离收盘价的百分比
  3. 成交量异动 — 插针 K 线成交量 vs 前 N 根均量
  4. 价格回归度 — 收盘价回归到开盘价附近的程度
  5. 关键价位触及 — 布林带外轨、前高/前低、整数关口
  6. 资金费率 — 极端费率 = 杠杆拥挤 = 插针概率高
  7. ATR 相对幅度 — 插针幅度相对于 ATR 的倍数

关键设计：
  - 输出同时包含 candidates（扫描漏斗）和 ratings（兼容 Skill-2 格式）
  - ratings 直接供 Skill-3 消费，跳过 TradingAgents 评级以保证时效性
  - 每个 rating 携带 wick_tip_price，供 Skill-3 作为天然止损位

数据源：BinancePublicClient（K 线优先走本地 SQLite 缓存）
"""

import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill
from src.skills.skill1_collect import (
    calc_ema,
    calc_rsi,
    calc_atr,
    calc_returns,
    calc_correlation,
    KLINE_LIMIT,
    CORRELATION_THRESHOLD,
    RSI_PERIOD,
    EMA_FAST,
    EMA_SLOW,
    ATR_PERIOD,
)

log = logging.getLogger(__name__)


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════
# 共享常量
# ══════════════════════════════════════════════════════════

BOLL_PERIOD = 20
BOLL_STD_MULT = 2.0

# 资金费率阈值
FUNDING_RATE_EXTREME_NEG = -0.001       # -0.1%，极端负值（做多信号）
FUNDING_RATE_EXTREME_POS = 0.001        # +0.1%，极端正值（做空信号）
FUNDING_RATE_VERY_EXTREME = 0.003       # ±0.3%，非常极端

# 影线比率阈值
MIN_SHADOW_RATIO = 2.0                  # 影线 / 实体 ≥ 2 倍才算插针
STRONG_SHADOW_RATIO = 4.0               # ≥ 4 倍为强插针

# 插针幅度阈值（影线尖端偏离收盘价的百分比）
MIN_WICK_DEPTH_PCT = 5.0               # 最小插针深度 5%
STRONG_WICK_DEPTH_PCT = 10.0           # 强插针深度 10%+

# 最低价格过滤（排除低价币精度问题导致的假插针）
MIN_PRICE_FILTER = 0.05

# 成交量异动阈值
VOLUME_SURGE_THRESHOLD = 2.0           # 插针 K 线量 ≥ 2x 前期均量
VOLUME_SURGE_STRONG = 4.0              # 强放量 4x+

# ATR 相对幅度阈值
ATR_WICK_MULT_MIN = 1.5               # 插针幅度 ≥ 1.5 倍 ATR
ATR_WICK_MULT_STRONG = 3.0            # 强插针 ≥ 3 倍 ATR

# ══════════════════════════════════════════════════════════
# 短期插针参数（15m K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "15m"
ST_MIN_KLINES = 50
ST_LOOKBACK_CANDLES = 3                 # 检查最近 3 根 K 线是否有插针
ST_VOLUME_BASE_WINDOW = 20             # 量能基准窗口 20 根 15m

# 短期评分权重（满分 100）— 侧重即时信号
ST_W_SHADOW_RATIO = 25      # 影线比率（核心）
ST_W_WICK_DEPTH = 18        # 插针幅度
ST_W_VOLUME_SURGE = 15      # 成交量异动
ST_W_PRICE_REVERT = 10      # 价格回归度
ST_W_KEY_LEVEL = 10         # 关键价位触及
ST_W_FUNDING = 12           # 资金费率
ST_W_ATR_MULT = 10          # ATR 相对幅度

# ══════════════════════════════════════════════════════════
# 长期插针参数（1h K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1h"
LT_MIN_KLINES = 60
LT_LOOKBACK_CANDLES = 3                 # 检查最近 3 根 K 线
LT_VOLUME_BASE_WINDOW = 24             # 量能基准窗口 24 根 1h

# 长期评分权重（满分 100）— 侧重确认信号
LT_W_SHADOW_RATIO = 20      # 影线比率
LT_W_WICK_DEPTH = 15        # 插针幅度
LT_W_VOLUME_SURGE = 12      # 成交量异动
LT_W_PRICE_REVERT = 8       # 价格回归度
LT_W_KEY_LEVEL = 15         # 关键价位触及（长期更重要）
LT_W_FUNDING = 15           # 资金费率（长期更重要）
LT_W_ATR_MULT = 15          # ATR 相对幅度（长期更重要）

DEFAULT_MIN_QUOTE_VOLUME = 10_000_000
DEFAULT_MIN_WICK_SCORE = 35
DEFAULT_MAX_CANDIDATES = 15
DEFAULT_MAX_SPREAD_PCT = 0.25
DEFAULT_MAX_ABS_FUNDING_RATE = 0.01

# 插针评级映射：wick_score → rating_score（0~10）
# 插针 Skill 直接输出 ratings，跳过 Skill-2
WICK_SCORE_TO_RATING = [
    (80, 9),   # wick_score ≥ 80 → rating 9
    (65, 8),   # wick_score ≥ 65 → rating 8
    (50, 7),   # wick_score ≥ 50 → rating 7
    (35, 6),   # wick_score ≥ 35 → rating 6
    (0, 5),    # 兜底
]

# 插针置信度映射：wick_score → confidence（0~100）
WICK_SCORE_TO_CONFIDENCE = [
    (80, 85.0),
    (65, 70.0),
    (50, 55.0),
    (35, 40.0),
    (0, 25.0),
]


# ══════════════════════════════════════════════════════════
# 插针形态检测（纯函数）
# ══════════════════════════════════════════════════════════

def detect_wick(
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Optional[Dict[str, Any]]:
    """
    检测单根 K 线是否为插针形态。

    下插针（做多信号）：下影线 >> 实体 + 上影线
    上插针（做空信号）：上影线 >> 实体 + 下影线

    过滤条件（避免十字星/微波动误判）：
      - 影线深度（影线长度/收盘价）必须 ≥ 1%
      - 影线必须占总振幅的 50% 以上
      - shadow_ratio 封顶 20（避免极端值干扰评分）

    返回:
        插针信息字典，或 None（非插针形态）。
    """
    total_range = high - low
    if total_range <= 0:
        return None

    body = abs(close - open_price)
    upper_shadow = high - max(open_price, close)
    lower_shadow = min(open_price, close) - low

    # 硬门槛：影线深度必须 ≥ 5%（排除正常波动，只捕捉极端插针）
    min_depth_abs = close * 0.05 if close > 0 else 0
    if lower_shadow < min_depth_abs and upper_shadow < min_depth_abs:
        return None

    # 影线必须占总振幅的 50% 以上（真正的插针，影线是主体）
    min_shadow_pct_of_range = total_range * 0.50

    # 分母下限：用总振幅的 10%，避免十字星的微小实体导致 ratio 爆炸
    body_floor = max(body, total_range * 0.10)

    # shadow_ratio 封顶，避免极端值
    max_ratio = 20.0

    # 下插针检测
    if (lower_shadow >= body_floor * MIN_SHADOW_RATIO
            and lower_shadow > upper_shadow * 1.5
            and lower_shadow >= min_shadow_pct_of_range
            and lower_shadow >= min_depth_abs):
        raw_ratio = lower_shadow / body_floor
        return {
            "type": "lower_wick",
            "direction": "long",
            "shadow_ratio": round(min(raw_ratio, max_ratio), 2),
            "wick_depth_pct": round(lower_shadow / close * 100, 4) if close > 0 else 0,
            "body_ratio": round(body / total_range, 4),
            "wick_tip_price": low,
            "shadow_length": lower_shadow,
        }

    # 上插针检测
    if (upper_shadow >= body_floor * MIN_SHADOW_RATIO
            and upper_shadow > lower_shadow * 1.5
            and upper_shadow >= min_shadow_pct_of_range
            and upper_shadow >= min_depth_abs):
        raw_ratio = upper_shadow / body_floor
        return {
            "type": "upper_wick",
            "direction": "short",
            "shadow_ratio": round(min(raw_ratio, max_ratio), 2),
            "wick_depth_pct": round(upper_shadow / close * 100, 4) if close > 0 else 0,
            "body_ratio": round(body / total_range, 4),
            "wick_tip_price": high,
            "shadow_length": upper_shadow,
        }

    return None


def detect_wick_in_recent(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    lookback: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    在最近 N 根 K 线中检测最强的插针形态，并验证插针后价格确认。

    插针后确认规则：
      - 下插针（做多）：插针之后的 K 线收盘价必须 ≥ 插针 K 线收盘价（价格企稳或反弹）
      - 上插针（做空）：插针之后的 K 线收盘价必须 ≤ 插针 K 线收盘价
      - 最后一根 K 线的插针不要求确认（刚发生，还没有后续 K 线）

    返回最强的已确认插针，或 None。
    """
    best: Optional[Dict[str, Any]] = None
    n = len(closes)
    start = max(0, n - lookback)

    for i in range(start, n):
        wick = detect_wick(opens[i], highs[i], lows[i], closes[i])
        if wick is None:
            continue

        # 插针后价格确认（最后一根不要求，因为还没有后续 K 线）
        if i < n - 1:
            wick_close = closes[i]
            latest_close = closes[-1]
            if wick["direction"] == "long" and latest_close < wick_close * 0.995:
                # 插针后价格继续下跌，不是有效插针
                continue
            if wick["direction"] == "short" and latest_close > wick_close * 1.005:
                # 插针后价格继续上涨，不是有效插针
                continue

        if best is None or wick["shadow_ratio"] > best["shadow_ratio"]:
            best = wick
            best["candle_index"] = i - n  # 负索引，-1 = 最后一根

    return best


# ══════════════════════════════════════════════════════════
# 七维度评分函数（纯函数）
# ══════════════════════════════════════════════════════════

def calc_wick_score(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    funding_rate: Optional[float],
    lookback_candles: int,
    volume_base_window: int,
    weights: Dict[str, float],
) -> Optional[Dict[str, Any]]:
    """
    计算插针综合评分（满分 100）。

    先检测最近 N 根 K 线是否存在插针形态，不存在则返回 None。
    存在则计算七维度评分。

    返回:
        评分结果字典，或 None（无插针形态）。
    """
    # Step 0: 检测插针形态
    wick = detect_wick_in_recent(opens, highs, lows, closes, lookback_candles)
    if wick is None:
        return None

    signals: List[str] = []
    score = 0.0
    w = weights
    last_close = closes[-1]

    # ── 1. 影线比率评分 ──
    shadow_ratio = wick["shadow_ratio"]
    shadow_score = 0.0
    if shadow_ratio >= STRONG_SHADOW_RATIO:
        shadow_score = w["shadow_ratio"]
        signals.append(f"强插针比率{shadow_ratio:.1f}x")
    elif shadow_ratio >= MIN_SHADOW_RATIO:
        shadow_score = w["shadow_ratio"] * (
            (shadow_ratio - MIN_SHADOW_RATIO)
            / (STRONG_SHADOW_RATIO - MIN_SHADOW_RATIO)
        )
        signals.append(f"插针比率{shadow_ratio:.1f}x")
    score += shadow_score

    # ── 2. 插针幅度评分 ──
    wick_depth = wick["wick_depth_pct"]
    depth_score = 0.0
    # 硬门槛：深度不到 MIN_WICK_DEPTH_PCT 不给分
    if wick_depth >= STRONG_WICK_DEPTH_PCT:
        depth_score = w["wick_depth"]
        signals.append(f"深度{wick_depth:.1f}%")
    elif wick_depth >= MIN_WICK_DEPTH_PCT:
        depth_score = w["wick_depth"] * (
            (wick_depth - MIN_WICK_DEPTH_PCT)
            / (STRONG_WICK_DEPTH_PCT - MIN_WICK_DEPTH_PCT)
        )
        signals.append(f"深度{wick_depth:.1f}%")
    # depth < MIN_WICK_DEPTH_PCT → 0 分，不 append 信号
    score += depth_score

    # ── 3. 成交量异动评分 ──
    vol_surge = _calc_volume_surge(volumes, volume_base_window)
    vol_score = 0.0
    if vol_surge is not None and vol_surge >= VOLUME_SURGE_THRESHOLD:
        if vol_surge >= VOLUME_SURGE_STRONG:
            vol_score = w["volume_surge"]
            signals.append(f"暴量{vol_surge:.1f}x")
        else:
            vol_score = w["volume_surge"] * (
                (vol_surge - VOLUME_SURGE_THRESHOLD)
                / (VOLUME_SURGE_STRONG - VOLUME_SURGE_THRESHOLD)
            )
            signals.append(f"放量{vol_surge:.1f}x")
    # vol_surge < VOLUME_SURGE_THRESHOLD → 0 分
    score += vol_score

    # ── 4. 价格回归度评分 ──
    body_ratio = wick["body_ratio"]
    revert_score = 0.0
    # body_ratio 越小 = 实体越小 = 价格回归越充分
    if body_ratio < 0.15:
        revert_score = w["price_revert"]
        signals.append("完全回归")
    elif body_ratio < 0.30:
        revert_score = w["price_revert"] * 0.7
        signals.append("大部分回归")
    elif body_ratio < 0.45:
        revert_score = w["price_revert"] * 0.3
    score += revert_score

    # ── 5. 关键价位触及评分 ──
    key_level_score = _score_key_level_touch(
        closes, highs, lows, wick, w["key_level"]
    )
    if key_level_score > 0:
        signals.append("触及关键价位")
    score += key_level_score

    # ── 6. 资金费率评分 ──
    fr_display = None
    funding_score = 0.0
    if funding_rate is not None:
        fr_display = round(funding_rate * 100, 4)
        funding_score = _score_funding_rate(
            funding_rate, wick["direction"], w["funding"]
        )
        if funding_score > 0:
            signals.append(f"费率{fr_display:.3f}%")
    score += funding_score

    # ── 7. ATR 相对幅度评分 ──
    atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
    atr_mult_score = 0.0
    atr_mult = None
    if atr_val and atr_val > 0:
        atr_mult = wick["shadow_length"] / atr_val
        if atr_mult >= ATR_WICK_MULT_STRONG:
            atr_mult_score = w["atr_mult"]
            signals.append(f"ATR×{atr_mult:.1f}")
        elif atr_mult >= ATR_WICK_MULT_MIN:
            atr_mult_score = w["atr_mult"] * (
                (atr_mult - ATR_WICK_MULT_MIN)
                / (ATR_WICK_MULT_STRONG - ATR_WICK_MULT_MIN)
            )
            signals.append(f"ATR×{atr_mult:.1f}")
    score += atr_mult_score

    # ATR 百分比（透传给 Skill-3）
    atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None

    return {
        "wick_type": wick["type"],
        "direction": wick["direction"],
        "shadow_ratio": shadow_ratio,
        "wick_depth_pct": round(wick_depth, 2),
        "body_ratio": round(body_ratio, 4),
        "wick_tip_price": wick["wick_tip_price"],
        "volume_surge": round(vol_surge, 2) if vol_surge is not None else None,
        "funding_rate": fr_display,
        "atr_pct": atr_pct,
        "atr_wick_mult": round(atr_mult, 2) if atr_mult is not None else None,
        "wick_score": round(min(score, 100)),
        "signal_details": " | ".join(signals) if signals else "无插针信号",
        "candle_index": wick.get("candle_index", -1),
    }


# ══════════════════════════════════════════════════════════
# 子维度评分函数
# ══════════════════════════════════════════════════════════

def _calc_volume_surge(
    volumes: List[float], base_window: int,
) -> Optional[float]:
    """计算最后一根 K 线的量比：最后一根 / 前 N 根均量。"""
    if len(volumes) < base_window + 1:
        return None
    base_avg = sum(volumes[-(base_window + 1):-1]) / base_window
    if base_avg <= 0:
        return None
    return volumes[-1] / base_avg


def _score_key_level_touch(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    wick: Dict[str, Any],
    max_score: float,
) -> float:
    """
    关键价位触及评分。

    检测插针尖端是否触及：
    1. 布林带外轨（上插针触上轨，下插针触下轨）
    2. 近期高低点（前 50 根 K 线的极值）
    3. 整数关口（价格的整数/半整数位）
    """
    if len(closes) < BOLL_PERIOD:
        return 0.0

    tip_price = wick["wick_tip_price"]
    last_close = closes[-1]
    score = 0.0

    # 1. 布林带外轨
    window = closes[-BOLL_PERIOD:]
    ma = sum(window) / BOLL_PERIOD
    variance = sum((x - ma) ** 2 for x in window) / BOLL_PERIOD
    std = math.sqrt(variance)
    boll_upper = ma + BOLL_STD_MULT * std
    boll_lower = ma - BOLL_STD_MULT * std

    if wick["direction"] == "long" and tip_price <= boll_lower:
        score += max_score * 0.45
    elif wick["direction"] == "short" and tip_price >= boll_upper:
        score += max_score * 0.45

    # 2. 近期高低点（前 50 根，排除最近 3 根）
    lookback = min(50, len(closes) - 3)
    if lookback > 10:
        prior_highs = highs[-(lookback + 3):-3]
        prior_lows = lows[-(lookback + 3):-3]
        recent_high = max(prior_highs) if prior_highs else 0
        recent_low = min(prior_lows) if prior_lows else float("inf")

        # 下插针触及前期低点附近（±1%）
        if wick["direction"] == "long" and recent_low > 0:
            if abs(tip_price - recent_low) / recent_low < 0.01:
                score += max_score * 0.35

        # 上插针触及前期高点附近（±1%）
        if wick["direction"] == "short" and recent_high > 0:
            if abs(tip_price - recent_high) / recent_high < 0.01:
                score += max_score * 0.35

    # 3. 整数关口（心理价位）
    if last_close > 0:
        # 计算合适的整数关口步长
        magnitude = 10 ** max(0, int(math.log10(last_close)) - 1)
        rounded = round(tip_price / magnitude) * magnitude
        if abs(tip_price - rounded) / last_close < 0.005:
            score += max_score * 0.20

    return min(score, max_score)


def _score_funding_rate(
    funding_rate: float,
    wick_direction: str,
    max_score: float,
) -> float:
    """
    资金费率评分。

    下插针（做多）+ 极端负费率 = 空头拥挤，爆空后反弹
    上插针（做空）+ 极端正费率 = 多头拥挤，爆多后回落
    """
    if wick_direction == "long":
        # 做多信号：费率越负越好
        if funding_rate <= -FUNDING_RATE_VERY_EXTREME:
            return max_score
        if funding_rate <= -FUNDING_RATE_EXTREME_NEG:
            return max_score * min(1.0, abs(funding_rate) / FUNDING_RATE_VERY_EXTREME)
        if funding_rate < 0:
            return max_score * 0.3
        return 0.0
    else:
        # 做空信号：费率越正越好
        if funding_rate >= FUNDING_RATE_VERY_EXTREME:
            return max_score
        if funding_rate >= FUNDING_RATE_EXTREME_POS:
            return max_score * min(1.0, funding_rate / FUNDING_RATE_VERY_EXTREME)
        if funding_rate > 0:
            return max_score * 0.3
        return 0.0


def _map_wick_score(wick_score: int, mapping: list) -> Any:
    """通用映射：wick_score → 目标值。"""
    for threshold, value in mapping:
        if wick_score >= threshold:
            return value
    return mapping[-1][1]


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════

class _CryptoWickBase(BaseSkill):
    """插针检测共享基类，封装基础过滤、资金费率获取、去重、ratings 生成。"""

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self._client = client

    def _build_funding_map(self) -> Dict[str, float]:
        fr_map: Dict[str, float] = {}
        try:
            data = self._client.get_funding_rates_all()
            for item in data:
                sym = item.get("symbol", "")
                rate_str = item.get("lastFundingRate", "")
                if sym and rate_str:
                    try:
                        fr_map[sym] = float(rate_str)
                    except (ValueError, TypeError):
                        pass
        except Exception as exc:
            log.warning("[%s] 获取资金费率失败: %s", self.name, exc)
        return fr_map

    def _get_tradable_symbols(self) -> set:
        try:
            info = self._client.get_exchange_info()
            return {
                s["symbol"]
                for s in info.get("symbols", [])
                if s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
            }
        except Exception as exc:
            log.warning("[%s] 获取交易对信息失败: %s", self.name, exc)
            return set()

    @staticmethod
    def _build_target_pool(tickers, target_symbols):
        normalized = set()
        for s in target_symbols:
            s = s.strip().upper()
            if not s.endswith("USDT"):
                s += "USDT"
            normalized.add(s)
        return [t for t in tickers if t.get("symbol", "") in normalized]

    @staticmethod
    def _base_filter(tickers, tradable, min_qv):
        exclude_bases = {"USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP"}
        result = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in tradable:
                continue
            base = sym.replace("USDT", "")
            if base in exclude_bases or not re.match(r'^[A-Z0-9]{2,15}$', base):
                continue
            qv = float(t.get("quoteVolume", 0))
            if qv < min_qv:
                continue
            # 排除低价币（精度问题导致假插针）
            last_price = float(t.get("lastPrice", 0))
            if last_price < MIN_PRICE_FILTER:
                continue
            t["quoteVolume"] = qv
            t["priceChangePercent"] = float(t.get("priceChangePercent", 0))
            result.append(t)
        return result

    @staticmethod
    def _deduplicate(scored, returns_map, max_cands):
        selected, selected_returns = [], []
        for item in scored:
            if len(selected) >= max_cands:
                break
            rets = returns_map.get(item["symbol"], [])
            if not any(
                calc_correlation(rets, sr) > CORRELATION_THRESHOLD
                for sr in selected_returns
            ):
                selected.append(item)
                selected_returns.append(rets)
        return selected

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        if hasattr(self._client, "get_klines_cached"):
            return self._client.get_klines_cached(symbol, interval, limit)
        return self._client.get_klines(symbol, interval, limit)

    @staticmethod
    def _quality_filter_reason(
        ticker: dict,
        funding_rate: Optional[float],
        max_spread_pct: float,
        max_abs_funding_rate: float,
    ) -> Optional[str]:
        bid = _safe_float(ticker.get("bidPrice"))
        ask = _safe_float(ticker.get("askPrice"))
        if bid and ask and bid > 0 and ask > bid:
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100
            if spread_pct > max_spread_pct:
                return f"spread_too_wide:{spread_pct:.4f}%"

        if funding_rate is not None and abs(funding_rate) > max_abs_funding_rate:
            return f"funding_rate_too_extreme:{funding_rate:.6f}"
        return None

    @staticmethod
    def _build_ratings_from_candidates(
        candidates: List[dict],
        strategy_tag: str,
    ) -> List[dict]:
        """
        将插针候选转换为 Skill-2 兼容的 ratings 格式。

        这是插针 Skill 跳过 TradingAgents 评级的关键：
        直接输出 ratings，让 Skill-3 无缝消费。
        """
        ratings = []
        for c in candidates:
            wick_score = c.get("wick_score", 0)
            rating_score = _map_wick_score(wick_score, WICK_SCORE_TO_RATING)
            confidence = _map_wick_score(wick_score, WICK_SCORE_TO_CONFIDENCE)

            ratings.append({
                "symbol": c["symbol"],
                "rating_score": rating_score,
                "signal": c.get("direction", "long"),
                "confidence": confidence,
                "comment": (
                    f"插针检测: {c.get('wick_type', '')} "
                    f"影线比率{c.get('shadow_ratio', 0):.1f}x "
                    f"深度{c.get('wick_depth_pct', 0):.1f}% "
                    f"评分{wick_score}/100 | "
                    f"{c.get('signal_details', '')}"
                ),
                "atr_pct": c.get("atr_pct"),
                "strategy_tag": strategy_tag,
                # 插针专属字段（Skill-3 可选消费）
                "wick_tip_price": c.get("wick_tip_price"),
            })
        return ratings

    def _run_scan(
        self,
        input_data: dict,
        interval: str,
        min_klines: int,
        lookback_candles: int,
        volume_base_window: int,
        weights: Dict[str, float],
    ) -> dict:
        """通用扫描流程，短期/长期共用。"""
        min_qv = input_data.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
        min_score = input_data.get("min_wick_score", DEFAULT_MIN_WICK_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        max_spread_pct = input_data.get("max_spread_pct", DEFAULT_MAX_SPREAD_PCT)
        max_abs_funding_rate = input_data.get(
            "max_abs_funding_rate", DEFAULT_MAX_ABS_FUNDING_RATE,
        )
        target_symbols = input_data.get("target_symbols")
        # 方向过滤：仅保留指定方向的插针（"long" = 下插针做多，"short" = 上插针做空，None = 不过滤）
        direction_filter = input_data.get("direction_filter")

        pipeline_run_id = str(uuid.uuid4())

        tickers = self._client.get_tickers_24hr()
        total_count = len(tickers)
        funding_map = self._build_funding_map()
        tradable = self._get_tradable_symbols()

        if target_symbols:
            pool = self._build_target_pool(tickers, target_symbols)
        else:
            pool = self._base_filter(tickers, tradable, min_qv)

        log.info("[%s] Step1: %d/%d 通过基础过滤", self.name, len(pool), total_count)

        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        for item in pool:
            symbol = item["symbol"]
            try:
                fr = funding_map.get(symbol)

                # 质量过滤（点差、费率极端）
                quality_reason = self._quality_filter_reason(
                    item, fr, max_spread_pct, max_abs_funding_rate,
                )
                if quality_reason:
                    log.debug("[%s] %s 质量过滤: %s", self.name, symbol, quality_reason)
                    continue

                kline_need = max(KLINE_LIMIT, volume_base_window + lookback_candles + 30)
                klines = self._fetch_klines(symbol, interval, kline_need)
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                opens = [float(k[1]) for k in klines]
                volumes = [float(k[5]) for k in klines]

                result = calc_wick_score(
                    opens, highs, lows, closes, volumes,
                    fr,
                    lookback_candles,
                    volume_base_window,
                    weights,
                )

                if result is None:
                    continue

                # 方向过滤
                if direction_filter and result["direction"] != direction_filter:
                    continue

                if result["wick_score"] < min_score and not target_symbols:
                    continue

                returns_map[symbol] = calc_returns(closes)
                last_close = closes[-1]

                scored.append({
                    "symbol": symbol,
                    "close": last_close,
                    "quote_volume_24h": item.get("quoteVolume", 0),
                    "price_change_pct": item.get("priceChangePercent", 0),
                    "wick_type": result["wick_type"],
                    "direction": result["direction"],
                    "shadow_ratio": result["shadow_ratio"],
                    "wick_depth_pct": result["wick_depth_pct"],
                    "body_ratio": result["body_ratio"],
                    "wick_tip_price": result["wick_tip_price"],
                    "volume_surge": result["volume_surge"],
                    "funding_rate": result["funding_rate"],
                    "atr_pct": result["atr_pct"],
                    "atr_wick_mult": result["atr_wick_mult"],
                    "wick_score": result["wick_score"],
                    "signal_details": result["signal_details"],
                    "signal_direction": result["direction"],
                    "strategy_tag": self.name,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["wick_score"], reverse=True)
        candidates = self._deduplicate(scored, returns_map, max_cands)

        # 生成 Skill-2 兼容的 ratings（跳过 TradingAgents）
        ratings = self._build_ratings_from_candidates(candidates, self.name)

        log.info(
            "[%s] 完成: pool=%d, 检测到插针=%d, 输出=%d, ratings=%d",
            self.name, len(pool), len(scored), len(candidates), len(ratings),
        )

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "ratings": ratings,
            "pipeline_run_id": pipeline_run_id,
            "filter_summary": {
                "total_tickers": total_count,
                "after_base_filter": len(pool),
                "after_wick_filter": len(scored),
                "output_count": len(candidates),
            },
        }


# ══════════════════════════════════════════════════════════
# 短期插针 Skill（15m）
# ══════════════════════════════════════════════════════════

class ShortTermWickSkill(_CryptoWickBase):
    """短期插针检测（15m K 线）。

    捕捉实时插针事件，适合快速入场。
    核心信号：影线比率极端 + 成交量暴增 + 资金费率极端。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_wick_short"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(
            input_data,
            interval=ST_INTERVAL,
            min_klines=ST_MIN_KLINES,
            lookback_candles=ST_LOOKBACK_CANDLES,
            volume_base_window=ST_VOLUME_BASE_WINDOW,
            weights={
                "shadow_ratio": ST_W_SHADOW_RATIO,
                "wick_depth": ST_W_WICK_DEPTH,
                "volume_surge": ST_W_VOLUME_SURGE,
                "price_revert": ST_W_PRICE_REVERT,
                "key_level": ST_W_KEY_LEVEL,
                "funding": ST_W_FUNDING,
                "atr_mult": ST_W_ATR_MULT,
            },
        )


# ══════════════════════════════════════════════════════════
# 长期插针 Skill（1h）
# ══════════════════════════════════════════════════════════

class LongTermWickSkill(_CryptoWickBase):
    """长期插针检测（1h K 线）。

    等 K 线收线确认插针形态后入场，假信号更少。
    核心信号：影线比率 + 关键价位触及 + 资金费率 + ATR 相对幅度。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_wick_long"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(
            input_data,
            interval=LT_INTERVAL,
            min_klines=LT_MIN_KLINES,
            lookback_candles=LT_LOOKBACK_CANDLES,
            volume_base_window=LT_VOLUME_BASE_WINDOW,
            weights={
                "shadow_ratio": LT_W_SHADOW_RATIO,
                "wick_depth": LT_W_WICK_DEPTH,
                "volume_surge": LT_W_VOLUME_SURGE,
                "price_revert": LT_W_PRICE_REVERT,
                "key_level": LT_W_KEY_LEVEL,
                "funding": LT_W_FUNDING,
                "atr_mult": LT_W_ATR_MULT,
            },
        )


# 向后兼容
CryptoWickSkill = ShortTermWickSkill
