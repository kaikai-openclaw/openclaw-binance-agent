"""
Skill-1A：A股量化数据采集与候选筛选

四步筛选流水线（与 Skill-1 同构，数据源替换为 akshare）：
  1. 大盘过滤 — 实时行情快照，按成交额、振幅、涨跌幅区间过滤
  2. 活跃度异动 — 日线 K 线计算短期量比
  3. 技术指标 — RSI / EMA / MACD / ATR / ADX 多因子双向信号评分
  4. 相关性去重

数据源：AkshareClient（akshare 公开接口，无需 API Key）
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill
from src.skills.skill1_collect import (
    # 复用 Skill-1 的纯函数技术指标计算
    calc_ema,
    calc_rsi,
    calc_macd,
    calc_volume_surge,
    calc_atr,
    calc_adx,
    calc_returns,
    calc_correlation,
    # 复用常量
    KLINE_LIMIT,
    VOLUME_LONG_WINDOW,
    CORRELATION_THRESHOLD,
    WEIGHT_RSI,
    WEIGHT_EMA,
    WEIGHT_MACD,
    WEIGHT_ADX,
    WEIGHT_LIQUIDITY,
    RSI_OVERSOLD,
    RSI_OVERBOUGHT,
    EMA_FAST,
    EMA_SLOW,
    ADX_TREND_THRESHOLD,
)

log = logging.getLogger(__name__)

# ── A 股默认参数 ──────────────────────────────────────────
DEFAULT_MIN_AMOUNT = 500_000_000       # 最低成交额 5 亿元
DEFAULT_MIN_AMPLITUDE_PCT = 3.0        # 最低振幅 3%
DEFAULT_PRICE_CHANGE_MIN = 1.5         # 最低绝对涨跌幅 1.5%
DEFAULT_PRICE_CHANGE_MAX = 9.9         # 最高绝对涨跌幅 9.9%（涨跌停前）
DEFAULT_VOLUME_SURGE_RATIO = 1.3       # 量比阈值（A 股日线波动较小，放宽）
DEFAULT_MIN_SIGNAL_SCORE = 55          # 最低信号评分
DEFAULT_MIN_ADX = 18.0                 # 最低 ADX（A 股趋势性偏弱，放宽）
DEFAULT_MAX_CANDIDATES = 10

# A 股排除关键词（ST、退市、B 股等）
_EXCLUDE_KEYWORDS = {"ST", "*ST", "退", "B股", "PT"}


class Skill1ACollect(BaseSkill):
    """
    A 股量化数据采集与候选筛选 Skill。

    与 Skill-1（Binance）同构的四步筛选，数据源为 akshare。
    输出格式兼容下游 Skill-2A 深度分析。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        client: Any,
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill1a_collect"
        self._client = client

    def run(self, input_data: dict) -> dict:
        min_amount = input_data.get("min_amount", DEFAULT_MIN_AMOUNT)
        min_amp = input_data.get("min_amplitude_pct", DEFAULT_MIN_AMPLITUDE_PCT)
        pc_range = input_data.get("price_change_range", {})
        pc_min = pc_range.get("min_pct", DEFAULT_PRICE_CHANGE_MIN)
        pc_max = pc_range.get("max_pct", DEFAULT_PRICE_CHANGE_MAX)
        surge_ratio = input_data.get("volume_surge_ratio", DEFAULT_VOLUME_SURGE_RATIO)
        min_signal = input_data.get("min_signal_score", DEFAULT_MIN_SIGNAL_SCORE)
        min_adx = input_data.get("min_adx", DEFAULT_MIN_ADX)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")

        pipeline_run_id = str(uuid.uuid4())

        # ── Step 1: 获取实时行情 & 大盘过滤 ──
        all_tickers = self._client.get_spot_all()
        total_count = len(all_tickers)

        if target_symbols:
            pool = self._build_target_pool(all_tickers, target_symbols)
            # 终极 fallback：实时接口全挂时，用日线数据构造行情
            if not pool and hasattr(self._client, "get_spot_by_hist"):
                log.info("[skill1a] 实时接口未找到目标，尝试日线 fallback")
                pool = self._client.get_spot_by_hist(target_symbols)
        else:
            pool = self._filter_tickers(
                all_tickers, min_amount, min_amp, pc_min, pc_max,
            )
        log.info("[skill1a] Step1: %d/%d 通过大盘过滤", len(pool), total_count)

        max_amount_val = max((item["amount"] for item in pool if item.get("amount")), default=1.0)
        if max_amount_val <= 0:
            max_amount_val = 1.0

        # ── Step 2 + 3: K 线技术指标 ──
        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        for item in pool:
            symbol = item["symbol"]
            try:
                klines = self._client.get_klines(symbol, "daily", KLINE_LIMIT)
                if not klines or len(klines) < VOLUME_LONG_WINDOW:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                volumes = [float(k[5]) for k in klines]

                surge = calc_volume_surge(volumes)
                if surge is None:
                    surge = 0.0
                if not target_symbols and surge < surge_ratio:
                    continue

                score_detail = self._calc_signal_score(
                    closes, highs, lows,
                    item.get("amount", 0), max_amount_val,
                )
                # 指定个股模式：跳过评分和 ADX 过滤，直接输出
                if not target_symbols:
                    if score_detail["total_score"] < min_signal:
                        continue
                    adx_val = score_detail["adx"]
                    if adx_val is None or adx_val < min_adx:
                        continue

                returns_map[symbol] = calc_returns(closes)

                scored.append({
                    "symbol": symbol,
                    "name": item.get("name", ""),
                    "amount": item.get("amount", 0),
                    "price_change_pct": item.get("change_pct", 0),
                    "amplitude_pct": item.get("amplitude_pct", 0),
                    "volume_surge_ratio": round(surge, 2),
                    "rsi": score_detail["rsi"],
                    "ema_bullish": score_detail["ema_bullish"],
                    "macd_bullish": score_detail["macd_bullish"],
                    "signal_score": score_detail["total_score"],
                    "signal_direction": score_detail["direction"],
                    "atr": score_detail["atr"],
                    "atr_pct": score_detail["atr_pct"],
                    "adx": score_detail["adx"],
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[skill1a] %s K线分析失败: %s", symbol, exc)
                continue

        scored.sort(key=lambda x: x["signal_score"], reverse=True)

        # ── Step 4: 相关性去重 ──
        candidates = _deduplicate(scored, returns_map, max_cands)

        log.info(
            "[skill1a] 完成: pool=%d, scored=%d, output=%d",
            len(pool), len(scored), len(candidates),
        )

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

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _build_target_pool(
        all_tickers: List[dict], target_symbols: List[str],
    ) -> List[dict]:
        normalized = set()
        for s in target_symbols:
            s = s.strip()
            if not s:
                continue
            # 去掉可能的交易所前缀
            for pfx in ("SH", "SZ", "BJ", "sh", "sz", "bj"):
                if s.upper().startswith(pfx) and len(s) > 2:
                    s = s[len(pfx):]
            s = s.replace(".", "")
            normalized.add(s)

        pool = []
        for t in all_tickers:
            sym = t.get("symbol", "")
            # 兼容新浪格式 sh600519 → 取后 6 位
            code = sym[-6:] if len(sym) > 6 else sym
            if code in normalized:
                # 统一 symbol 为纯 6 位代码
                t = {**t, "symbol": code}
                pool.append(t)
        return pool

    @staticmethod
    def _filter_tickers(
        tickers: List[dict],
        min_amount: float,
        min_amp: float,
        pc_min: float,
        pc_max: float,
    ) -> List[dict]:
        """Step 1: 大盘过滤。排除 ST / 退市 / 北交所 / 成交额不足。"""
        result = []
        for t in tickers:
            raw_symbol = t.get("symbol", "")
            name = t.get("name", "")

            # 统一提取纯 6 位代码（兼容新浪 sh600519 格式）
            symbol = raw_symbol[-6:] if len(raw_symbol) > 6 else raw_symbol

            # 排除 ST、退市、B 股
            if any(kw in name for kw in _EXCLUDE_KEYWORDS):
                continue
            # 排除北交所（8/9 开头）
            if symbol.startswith(("8", "9")):
                continue

            amount = t.get("amount")
            if amount is None or amount < min_amount:
                continue

            amp = t.get("amplitude_pct")
            if amp is None or amp < min_amp:
                continue

            change = t.get("change_pct")
            if change is None:
                continue
            abs_change = abs(change)
            if abs_change < pc_min or abs_change > pc_max:
                continue

            close = t.get("close")
            if close is None or close <= 0:
                continue

            # 统一 symbol 为纯 6 位
            result.append({**t, "symbol": symbol})
        return result

    def _calc_signal_score(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        amount: float,
        max_amount: float,
    ) -> dict:
        """多因子技术指标评分（复用 Skill-1 的评分逻辑）。"""
        import math
        rsi_val = calc_rsi(closes)
        macd = calc_macd(closes)
        ema20 = calc_ema(closes, EMA_FAST)
        ema50 = calc_ema(closes, EMA_SLOW)
        atr_val = calc_atr(highs, lows, closes)
        adx_val = calc_adx(highs, lows, closes)

        last_close = closes[-1]
        last_ema20 = ema20[-1] if ema20 and not math.isnan(ema20[-1]) else None
        last_ema50 = ema50[-1] if ema50 and not math.isnan(ema50[-1]) else None

        ml = macd.get("macd_line")
        sl = macd.get("signal_line")
        hist = macd.get("histogram")

        # 做多评分
        long_rsi = _score_rsi_long(rsi_val)
        long_ema = _score_ema(last_close, last_ema20, last_ema50, bullish=True)
        long_macd = _score_macd(ml, sl, hist, bullish=True)

        # 做空评分
        short_rsi = _score_rsi_short(rsi_val)
        short_ema = _score_ema(last_close, last_ema20, last_ema50, bullish=False)
        short_macd = _score_macd(ml, sl, hist, bullish=False)

        # 方向无关
        adx_score = _score_adx(adx_val)
        liq_score = _score_liquidity(amount, max_amount)

        long_total = long_rsi + long_ema + long_macd + adx_score + liq_score
        short_total = short_rsi + short_ema + short_macd + adx_score + liq_score

        if long_total >= short_total:
            direction = "long"
            total_score = long_total
            ema_bullish = long_ema > 0
            macd_bullish = long_macd > 0
        else:
            direction = "short"
            total_score = short_total
            ema_bullish = False
            macd_bullish = False

        atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None

        return {
            "rsi": round(rsi_val, 2) if rsi_val is not None else None,
            "ema_bullish": ema_bullish,
            "macd_bullish": macd_bullish,
            "total_score": round(total_score),
            "direction": direction,
            "atr": round(atr_val, 4) if atr_val is not None else None,
            "atr_pct": atr_pct,
            "adx": round(adx_val, 2) if adx_val is not None else None,
        }


# ── 评分辅助函数（与 Skill-1 逻辑一致）────────────────────

def _score_rsi_long(rsi: Optional[float]) -> float:
    if rsi is None:
        return 0.0
    if rsi <= RSI_OVERSOLD:
        return float(WEIGHT_RSI)
    if rsi >= RSI_OVERBOUGHT:
        return 0.0
    return WEIGHT_RSI * (1.0 - (rsi - RSI_OVERSOLD) / (RSI_OVERBOUGHT - RSI_OVERSOLD))


def _score_rsi_short(rsi: Optional[float]) -> float:
    if rsi is None:
        return 0.0
    if rsi >= RSI_OVERBOUGHT:
        return float(WEIGHT_RSI)
    if rsi <= RSI_OVERSOLD:
        return 0.0
    return WEIGHT_RSI * (rsi - RSI_OVERSOLD) / (RSI_OVERBOUGHT - RSI_OVERSOLD)


def _score_ema(close: float, ema20: Optional[float], ema50: Optional[float], bullish: bool) -> float:
    if ema20 is None or ema50 is None:
        return 0.0
    if bullish:
        if close > ema20 > ema50:
            return float(WEIGHT_EMA)
        if close > ema20:
            return WEIGHT_EMA * 0.5
    else:
        if close < ema20 < ema50:
            return float(WEIGHT_EMA)
        if close < ema20:
            return WEIGHT_EMA * 0.5
    return 0.0


def _score_macd(ml, sl, hist, bullish: bool) -> float:
    if ml is None or sl is None or hist is None:
        return 0.0
    if bullish:
        if ml > 0 and hist >= 0:
            return float(WEIGHT_MACD)
        if hist > 0:
            return WEIGHT_MACD * 0.5
    else:
        if ml < 0 and hist <= 0:
            return float(WEIGHT_MACD)
        if hist < 0:
            return WEIGHT_MACD * 0.5
    return 0.0


def _score_adx(adx: Optional[float]) -> float:
    if adx is None:
        return 0.0
    return WEIGHT_ADX * (min(adx, 50.0) / 50.0)


def _score_liquidity(amount: float, max_amount: float) -> float:
    import math
    if amount <= 0 or max_amount <= 0:
        return 0.0
    log_vol = math.log(amount + 1)
    log_max = math.log(max_amount + 1)
    if log_max <= 0:
        return 0.0
    return WEIGHT_LIQUIDITY * min(log_vol / log_max, 1.0)


def _deduplicate(
    scored: List[dict],
    returns_map: Dict[str, List[float]],
    max_cands: int,
) -> List[dict]:
    selected: List[dict] = []
    selected_returns: List[List[float]] = []
    for item in scored:
        if len(selected) >= max_cands:
            break
        rets = returns_map.get(item["symbol"], [])
        redundant = False
        for sel_rets in selected_returns:
            if calc_correlation(rets, sel_rets) > CORRELATION_THRESHOLD:
                redundant = True
                break
        if not redundant:
            selected.append(item)
            selected_returns.append(rets)
    return selected
