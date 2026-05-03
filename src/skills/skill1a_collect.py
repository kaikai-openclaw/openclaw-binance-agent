"""
Skill-1A：A 股趋势选股（v2 重设计）

针对 A 股市场特性的趋势选股系统，核心理念：
  "趋势为王，量价配合，均线确认"

与 v1 的关键改进：
  1. 降低成交额门槛（5亿→1亿），覆盖中小盘趋势启动股
  2. 去掉振幅和涨跌幅下限，不再排除缩量窄幅整理和慢牛
  3. 新增均线多头排列检测（MA5/10/20/60，A 股趋势交易的基础）
  4. 新增突破前高/平台检测（箱体突破是 A 股最经典的趋势启动信号）
  5. 新增换手率评估（比成交额更能反映真实活跃度）
  6. 重新分配权重：均线排列 > MACD 持续性 > ADX > 量价配合 > 突破确认

四步筛选流水线：
  1. 基础过滤 — 排除 ST/退市/北交所/低价股/一字板/流动性枯竭
  2. K 线数据获取 + 均线计算
  3. 多因子趋势评分（满分 100）：
     - 均线多头排列（25 分）— A 股趋势交易的基石
     - MACD 持续性（20 分）— 趋势动量确认
     - ADX 趋势强度（15 分）— 区分趋势和震荡
     - 量价配合（15 分）— 放量上涨 + 换手率健康
     - 突破确认（15 分）— 突破前高/20日高点
     - RSI 趋势区间（10 分）— 处于 50-80 强势区间
  4. 相关性去重

数据源：AkshareClient（akshare 公开接口，K 线优先走本地 SQLite 缓存）
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
    calc_adx,
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

# ══════════════════════════════════════════════════════════
# 默认参数（针对 A 股趋势选股调优）
# ══════════════════════════════════════════════════════════

DEFAULT_MIN_AMOUNT = 100_000_000       # 最低成交额 1 亿（覆盖中小盘趋势启动股）
DEFAULT_MIN_PRICE = 3.0                # 最低股价
DEFAULT_MIN_KLINES = 60                # 最低 K 线数量（约 3 个月，排除次新股）
DEFAULT_MIN_SIGNAL_SCORE = 50          # 最低信号评分
DEFAULT_MAX_CANDIDATES = 15            # 输出上限

# 排除关键词
_EXCLUDE_KEYWORDS = {"ST", "*ST", "退", "B股", "PT"}
_20PCT_LIMIT_PREFIXES = ("300", "301", "688", "689")

# ── 均线参数 ──────────────────────────────────────────────
MA_PERIODS = [5, 10, 20, 60]          # A 股经典均线组合

# ── 评分权重（满分 100）──────────────────────────────────
W_MA_ALIGN = 25        # 均线多头排列（A 股趋势交易的基石）
W_MACD = 20            # MACD 持续性（趋势动量确认）
W_ADX = 15             # ADX 趋势强度
W_VOLUME = 15          # 量价配合（放量上涨 + 换手率健康）
W_BREAKOUT = 15        # 突破确认（突破前高/平台）
W_RSI = 10             # RSI 趋势区间（50-80 强势区）

# ── 突破检测参数 ──────────────────────────────────────────
BREAKOUT_LOOKBACK = 20                 # 回看 20 天寻找前高
BREAKOUT_MARGIN = 0.02                 # 突破前高 2% 以上才算有效突破

# ── 换手率参数 ────────────────────────────────────────────
TURNOVER_HEALTHY_MIN = 2.0             # 健康换手率下限 2%
TURNOVER_HEALTHY_MAX = 15.0            # 健康换手率上限 15%（过高可能是出货）


class Skill1ACollect(BaseSkill):
    """A 股趋势选股 Skill（v2）。"""

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill1a_collect"
        self._client = client

    def run(self, input_data: dict) -> dict:
        # ── 大盘环境过滤（前置检查）──
        skip_regime = input_data.get("skip_market_regime", False)
        if not skip_regime:
            try:
                from src.infra.market_regime import get_regime_filter
                regime_filter = get_regime_filter(client=self._client)
                regime = regime_filter.get_current_regime()

                if not regime["allow_trend"]:
                    log.warning(
                        "[%s] 大盘环境不适合趋势选股，策略暂停。"
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
                            "after_signal_filter": 0,
                            "output_count": 0,
                            "skipped_reason": "market_regime_bear",
                            "market_trend": regime["trend"],
                            "market_reason": regime["reason"],
                        },
                    }

                # 回测验证：趋势策略在非牛市环境下高分组反而更差（85+胜率37%）
                # 横盘时大幅提高门槛，只保留最强势的标的
                if "min_signal_score" not in input_data:
                    suggested = regime.get("suggested_trend_min_score")
                    if suggested and suggested > DEFAULT_MIN_SIGNAL_SCORE:
                        log.info("[%s] 大盘横盘/偏弱，趋势评分门槛提升至 %d（原 %d）",
                                 self.name, suggested, DEFAULT_MIN_SIGNAL_SCORE)
                        input_data = {**input_data, "min_signal_score": suggested}
                    elif regime["trend"] == "sideways":
                        # 横盘时额外限制：只取评分 40-70 的标的，排除极高分（追高风险）
                        input_data = {**input_data,
                                      "min_signal_score": max(input_data.get("min_signal_score", 0), 55),
                                      "max_signal_score": 75}

            except Exception as e:
                log.warning("[%s] 大盘环境检查失败，降级继续运行: %s", self.name, e)

        min_amount = input_data.get("min_amount", DEFAULT_MIN_AMOUNT)
        min_price = input_data.get("min_price", DEFAULT_MIN_PRICE)
        min_klines = input_data.get("min_klines", DEFAULT_MIN_KLINES)
        min_score = input_data.get("min_signal_score", DEFAULT_MIN_SIGNAL_SCORE)
        max_score = input_data.get("max_signal_score", 100)   # 回测验证：85+分段表现最差
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")

        pipeline_run_id = str(uuid.uuid4())

        # ── Step 1: 基础过滤 ──
        all_tickers = self._client.get_spot_all()
        total_count = len(all_tickers)

        if target_symbols:
            pool = _build_target_pool(all_tickers, target_symbols)
            if not pool and hasattr(self._client, "get_spot_by_hist"):
                pool = self._client.get_spot_by_hist(target_symbols)
        else:
            pool = _base_filter(all_tickers, min_amount, min_price)

        log.info("[skill1a] Step1: %d/%d 通过基础过滤", len(pool), total_count)

        # ── Step 2+3: K 线分析 + 趋势评分 ──
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
                volumes = [float(k[5]) for k in klines]

                result = _calc_trend_score(closes, highs, lows, volumes,
                                           item.get("turnover", 0))

                # 趋势方向必须是做多（A 股散户无法做空）
                if result["direction"] != "long":
                    continue

                if result["total_score"] < min_score and not target_symbols:
                    continue

                # 回测验证：85+ 分段胜率37%，是最差的区间（追高风险）
                # 横盘时限制上限为 75，牛市时不限制（max_score 默认 100）
                if result["total_score"] > max_score and not target_symbols:
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
                    "turnover": item.get("turnover", 0),
                    "signal_score": result["total_score"],
                    "signal_direction": result["direction"],
                    "ma_align_score": result["ma_align_score"],
                    "ma_align_detail": result["ma_align_detail"],
                    "macd_score": result["macd_score"],
                    "adx": result["adx"],
                    "adx_score": result["adx_score"],
                    "volume_score": result["volume_score"],
                    "breakout_score": result["breakout_score"],
                    "breakout_detail": result["breakout_detail"],
                    "rsi": result["rsi"],
                    "rsi_score": result["rsi_score"],
                    "ema_bullish": result["ema_bullish"],
                    "macd_bullish": result["macd_bullish"],
                    "atr_pct": atr_pct,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[skill1a] %s 分析失败: %s", symbol, exc)

        scored.sort(key=lambda x: x["signal_score"], reverse=True)

        # ── Step 4: 相关性去重 ──
        candidates = _deduplicate(scored, returns_map, max_cands)

        log.info("[skill1a] 完成: pool=%d, scored=%d, output=%d",
                 len(pool), len(scored), len(candidates))

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
            "filter_summary": {
                "total_tickers": total_count,
                "after_base_filter": len(pool),
                "after_signal_filter": len(scored),
                "output_count": len(candidates),
            },
        }


# ══════════════════════════════════════════════════════════
# 趋势评分核心（满分 100）
# ══════════════════════════════════════════════════════════

def _calc_trend_score(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    volumes: List[float],
    turnover: float = 0,
) -> dict:
    """A 股趋势多因子评分。

    六维度评分体系，专为 A 股趋势交易设计：
    1. 均线多头排列（25 分）— 趋势的基石
    2. MACD 持续性（20 分）— 动量确认
    3. ADX 趋势强度（15 分）— 区分趋势和震荡
    4. 量价配合（15 分）— 放量上涨 + 换手率
    5. 突破确认（15 分）— 突破前高/平台
    6. RSI 趋势区间（10 分）— 强势区间确认
    """
    last_close = closes[-1]

    # ── 1. 均线多头排列（25 分）──
    ma_result = _score_ma_alignment(closes)

    # ── 2. MACD 持续性（20 分）──
    macd_result = _score_macd_trend(closes)

    # ── 3. ADX 趋势强度（15 分）──
    adx_val = calc_adx(highs, lows, closes)
    adx_score = _score_adx_trend(adx_val)

    # ── 4. 量价配合（15 分）──
    vol_score = _score_volume_price(closes, volumes, turnover)

    # ── 5. 突破确认（15 分）──
    breakout_result = _score_breakout(closes, highs, volumes)

    # ── 6. RSI 趋势区间（10 分）──
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    rsi_score = _score_rsi_trend(rsi_val)

    total = (ma_result["score"] + macd_result["score"] + adx_score +
             vol_score + breakout_result["score"] + rsi_score)

    # 方向判断：均线多头排列 + MACD 看多 → long，否则 neutral
    direction = "long" if ma_result["score"] >= 10 and macd_result["bullish"] else "neutral"

    return {
        "total_score": round(total),
        "direction": direction,
        "ma_align_score": round(ma_result["score"]),
        "ma_align_detail": ma_result["detail"],
        "ema_bullish": ma_result["score"] >= 10,
        "macd_score": round(macd_result["score"]),
        "macd_bullish": macd_result["bullish"],
        "adx": round(adx_val, 2) if adx_val is not None else None,
        "adx_score": round(adx_score),
        "volume_score": round(vol_score),
        "breakout_score": round(breakout_result["score"]),
        "breakout_detail": breakout_result["detail"],
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "rsi_score": round(rsi_score),
    }


# ══════════════════════════════════════════════════════════
# 各维度评分函数
# ══════════════════════════════════════════════════════════

def _score_ma_alignment(closes: List[float]) -> dict:
    """均线多头排列评分（满分 25）。

    A 股趋势交易的基石：MA5 > MA10 > MA20 > MA60
    - 完美多头排列（4 条均线严格递增）：25 分
    - 3 条均线多头排列：18 分
    - 价格站上 MA20 且 MA20 > MA60：12 分
    - 价格站上 MA20：6 分
    - 其他：0 分
    """
    if len(closes) < 60:
        return {"score": 0, "detail": "数据不足"}

    # 计算各周期 SMA
    mas = {}
    for p in MA_PERIODS:
        mas[p] = sum(closes[-p:]) / p

    last = closes[-1]
    detail_parts = []

    # 检查多头排列层级
    above_all = last > mas[5] > mas[10] > mas[20] > mas[60]
    above_3 = last > mas[5] > mas[10] > mas[20]
    above_20_60 = last > mas[20] and mas[20] > mas[60]
    above_20 = last > mas[20]

    if above_all:
        score = 25.0
        detail_parts.append("完美多头排列(MA5>10>20>60)")
    elif above_3:
        score = 18.0
        detail_parts.append("三线多头(MA5>10>20)")
    elif above_20_60:
        score = 12.0
        detail_parts.append("站上MA20且MA20>MA60")
    elif above_20:
        score = 6.0
        detail_parts.append("站上MA20")
    else:
        score = 0.0
        detail_parts.append("均线空头或混乱")

    # 加分：均线斜率向上（MA20 近 5 日在上升）
    if len(closes) >= 65:
        ma20_5d_ago = sum(closes[-25:-5]) / 20
        if mas[20] > ma20_5d_ago * 1.005:  # MA20 近 5 日上升 0.5% 以上
            score = min(score + 2, W_MA_ALIGN)
            detail_parts.append("MA20上升")

    return {"score": min(score, W_MA_ALIGN), "detail": " | ".join(detail_parts)}


def _score_macd_trend(closes: List[float]) -> dict:
    """MACD 趋势持续性评分（满分 20）。

    不只看金叉/死叉，更看 MACD 的持续性：
    - MACD 线 > 0 且柱状图 > 0 且柱状图在放大：20 分（强趋势）
    - MACD 线 > 0 且柱状图 > 0：15 分（趋势确认）
    - MACD 线 > 信号线（金叉状态）：10 分
    - MACD 线 > 0 但柱状图 < 0（趋势减弱）：5 分
    """
    macd = calc_macd(closes)
    ml = macd.get("macd_line")
    sl = macd.get("signal_line")
    hist = macd.get("histogram")

    if ml is None or sl is None or hist is None:
        return {"score": 0, "bullish": False}

    bullish = ml > sl

    if ml > 0 and hist > 0:
        # 检查柱状图是否在放大（需要前一根的 histogram）
        # 简化：用 MACD 线的绝对值判断动量强度
        if ml > sl * 1.1:  # MACD 线明显高于信号线
            return {"score": 20.0, "bullish": True}
        return {"score": 15.0, "bullish": True}
    elif bullish:
        return {"score": 10.0, "bullish": True}
    elif ml > 0:
        return {"score": 5.0, "bullish": True}
    else:
        return {"score": 0, "bullish": False}


def _score_adx_trend(adx: Optional[float]) -> float:
    """ADX 趋势强度评分（满分 15）。

    ADX > 25：有趋势（线性映射到满分）
    ADX 20-25：弱趋势（部分得分）
    ADX < 20：无趋势（0 分）
    """
    if adx is None:
        return 0.0
    if adx >= 40:
        return W_ADX  # 强趋势满分
    if adx >= 25:
        return W_ADX * 0.7 + W_ADX * 0.3 * (adx - 25) / 15  # 25-40 线性
    if adx >= 20:
        return W_ADX * 0.3 * (adx - 20) / 5  # 20-25 弱趋势
    return 0.0


def _score_volume_price(
    closes: List[float], volumes: List[float], turnover: float,
) -> float:
    """量价配合评分（满分 15）。

    三个子维度：
    1. 量比（近 5 日均量 / 近 20 日均量）> 1.2 = 资金关注（5 分）
    2. 量价同向（价格上涨时成交量放大）（5 分）
    3. 换手率在健康区间 2%-15%（5 分）
    """
    score = 0.0

    # 量比
    if len(volumes) >= 25:
        short_avg = sum(volumes[-5:]) / 5
        long_avg = sum(volumes[-25:-5]) / 20
        if long_avg > 0:
            vol_ratio = short_avg / long_avg
            if vol_ratio >= 1.5:
                score += 5.0
            elif vol_ratio >= 1.2:
                score += 3.0

    # 量价同向：近 5 天中，上涨日的成交量 > 下跌日的成交量
    if len(closes) >= 6 and len(volumes) >= 6:
        up_vol, down_vol = 0.0, 0.0
        for i in range(-5, 0):
            if closes[i] > closes[i - 1]:
                up_vol += volumes[i]
            else:
                down_vol += volumes[i]
        if up_vol > down_vol * 1.2:
            score += 5.0
        elif up_vol > down_vol:
            score += 2.5

    # 换手率
    if turnover and TURNOVER_HEALTHY_MIN <= turnover <= TURNOVER_HEALTHY_MAX:
        score += 5.0
    elif turnover and turnover > 0:
        score += 2.0

    # 活跃度基因检测：近 20 日内是否有涨幅 > 7% 的大阳线
    has_active_gene = False
    if len(closes) >= 21:
        for i in range(-20, 0):
            if closes[i-1] > 0 and (closes[i] - closes[i-1]) / closes[i-1] >= 0.07:
                has_active_gene = True
                break
    
    # A股无大阳线的票走势极慢，对没有活跃基因的个股量价得分减半
    if not has_active_gene:
        score *= 0.5

    return min(score, W_VOLUME)


def _score_breakout(
    closes: List[float], highs: List[float], volumes: List[float],
) -> dict:
    """突破确认评分（满分 15）。

    检测是否突破近期高点/平台：
    - 突破 20 日最高价 + 放量：15 分（强突破）
    - 突破 20 日最高价：10 分
    - 接近 20 日最高价（差距 < 2%）：5 分
    """
    if len(closes) < BREAKOUT_LOOKBACK + 1 or len(highs) < BREAKOUT_LOOKBACK + 1:
        return {"score": 0, "detail": "数据不足"}

    last = closes[-1]
    # 前 20 天的最高价（不含最后一天）
    prev_high = max(highs[-(BREAKOUT_LOOKBACK + 1):-1])

    if prev_high <= 0:
        return {"score": 0, "detail": ""}

    pct_above = (last - prev_high) / prev_high

    if pct_above >= BREAKOUT_MARGIN:
        # A股防假突破：突破必须伴随明显的放量（至少大于过去 20 日均量的 1.5 倍）
        if len(volumes) >= 21:
            today_vol = volumes[-1]
            avg_vol_20 = sum(volumes[-21:-1]) / 20
            if avg_vol_20 > 0 and today_vol >= avg_vol_20 * 1.5:
                return {"score": 15.0, "detail": f"强势放量突破前高({pct_above*100:+.1f}%)"}
            elif avg_vol_20 > 0 and today_vol >= avg_vol_20 * 1.0:
                return {"score": 8.0, "detail": f"温和突破前高({pct_above*100:+.1f}%)"}
            else:
                return {"score": 0.0, "detail": f"缩量假突破警告({pct_above*100:+.1f}%)"}
        return {"score": 0.0, "detail": "数据不足无法确认突破有效性"}
    elif pct_above >= -BREAKOUT_MARGIN:
        return {"score": 5.0, "detail": f"接近20日高点({pct_above*100:+.1f}%)"}
    else:
        return {"score": 0, "detail": ""}


def _score_rsi_trend(rsi: Optional[float]) -> float:
    """RSI 趋势区间评分（满分 10）。

    趋势股的 RSI 通常在 50-80 区间运行：
    - RSI 55-75：满分（最佳趋势区间）
    - RSI 50-55 或 75-80：部分得分
    - RSI < 50 或 > 80：0 分（超卖或超买，不是健康趋势）
    """
    if rsi is None:
        return 0.0
    if 55 <= rsi <= 75:
        return W_RSI
    if 50 <= rsi < 55:
        return W_RSI * (rsi - 50) / 5
    if 75 < rsi <= 80:
        return W_RSI * (80 - rsi) / 5
    return 0.0


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def _build_target_pool(all_tickers: List[dict], target_symbols: List[str]) -> List[dict]:
    normalized = set()
    for s in target_symbols:
        s = s.strip().upper()
        for pfx in ("SH", "SZ", "BJ"):
            if s.startswith(pfx) and len(s) > 2:
                s = s[len(pfx):]
        s = s.replace(".", "")
        normalized.add(s)
    pool = []
    for t in all_tickers:
        sym = t.get("symbol", "")
        code = sym[-6:] if len(sym) > 6 else sym
        if code in normalized:
            pool.append({**t, "symbol": code})
    return pool


def _base_filter(tickers: List[dict], min_amount: float, min_price: float) -> List[dict]:
    """基础过滤：排雷 + 流动性。

    与 v1 的关键区别：
    - 去掉了振幅下限（不再排除缩量窄幅整理股）
    - 去掉了涨跌幅下限（不再排除慢牛）
    - 成交额门槛从 5 亿降到 1 亿
    """
    result = []
    for t in tickers:
        raw_symbol = t.get("symbol", "")
        name = t.get("name", "")
        symbol = raw_symbol[-6:] if len(raw_symbol) > 6 else raw_symbol

        # 排除 ST/退市/B 股
        if any(kw in name for kw in _EXCLUDE_KEYWORDS):
            continue
        # 排除北交所
        if symbol.startswith(("8", "9")):
            continue

        close = t.get("close")
        if close is None or close <= 0 or close < min_price:
            continue

        amount = t.get("amount")
        if amount is None or amount < min_amount:
            continue

        # 排除一字涨停/跌停板（无法交易）
        high = t.get("high")
        low = t.get("low")
        open_p = t.get("open")
        if (high is not None and low is not None and open_p is not None
                and high == low == open_p and high > 0):
            continue

        result.append({**t, "symbol": symbol})
    return result


def _deduplicate(
    scored: List[dict], returns_map: Dict[str, List[float]], max_cands: int,
) -> List[dict]:
    selected, selected_returns = [], []
    for item in scored:
        if len(selected) >= max_cands:
            break
        rets = returns_map.get(item["symbol"], [])
        if not any(calc_correlation(rets, sr) > CORRELATION_THRESHOLD for sr in selected_returns):
            selected.append(item)
            selected_returns.append(rets)
    return selected
