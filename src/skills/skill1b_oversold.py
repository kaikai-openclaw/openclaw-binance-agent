"""
Skill-1B：A股超跌反弹筛选

多维度超跌信号检测与综合评分：
  1. 基础过滤 — 排除 ST/退市/北交所/低价股/流动性枯竭股（不做当日涨跌预筛）
  2. 超跌信号检测 — 六维度量化评分：
     - 价格偏离度: 20日乖离率（BIAS < -6%）
     - 动量极值: RSI(14) 超卖区（< 35）
     - 连续杀跌: 连续下跌天数 + 近10日累计跌幅（< -8%）
     - 通道突破: 布林带下轨突破
     - 动量背离: MACD 底背离
     - KDJ 极值: J 值 < 0
  3. 辅助确认 — 底部放量（恐慌盘涌出）
  4. 相关性去重

设计原则：超跌是历史累积状态，基础过滤仅排雷不做当日预筛，
          让 K 线技术指标来判断是否真正超跌。

策略逻辑：均值回归，捕捉市场情绪错杀后的短期修复机会。
风险提示：超跌反弹本质是左侧交易（接飞刀），必须严格止损。

数据源：AkshareClient（akshare 公开接口，K 线优先走本地缓存）
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
    EMA_FAST,
    ATR_PERIOD,
)

log = logging.getLogger(__name__)

# ── 默认参数 ──────────────────────────────────────────────
DEFAULT_MIN_AMOUNT = 50_000_000        # 最低成交额 5000 万
DEFAULT_MIN_PRICE = 3.0
DEFAULT_MIN_KLINES = 60                # 约 3 个月（放宽，覆盖更多次新股）
DEFAULT_RSI_THRESHOLD = 35.0           # RSI < 35 视为偏弱（放宽，原 25 太严）
DEFAULT_BIAS_THRESHOLD = -6.0          # 20日乖离率 < -6%（放宽，原 -10% 太严）
DEFAULT_CONSECUTIVE_DOWN = 3           # 连续下跌 ≥ 3 天
DEFAULT_DROP_PCT = -8.0                # 近 N 日累计跌幅 < -8%（放宽，原 -15%）
DEFAULT_DROP_LOOKBACK = 10             # 回看 10 天
DEFAULT_MIN_OVERSOLD_SCORE = 25        # 最低评分（放宽，原 50 太严）
DEFAULT_MAX_CANDIDATES = 30            # 输出上限（扩大，原 15）
DEFAULT_PREFILTER_CHANGE_PCT = 0.0     # 当日涨跌幅预筛（0=禁用，不再用当日跌幅过滤）

# 布林带参数
BOLL_PERIOD = 20
BOLL_STD_MULT = 2.0

# KDJ 参数
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 排除关键词
_EXCLUDE_KEYWORDS = {"ST", "*ST", "退", "B股", "PT"}
_20PCT_LIMIT_PREFIXES = ("300", "301", "688", "689")

# ── 评分权重（满分 100）──────────────────────────────────
W_BIAS = 20       # 乖离率
W_RSI = 20        # RSI 超卖
W_DROP = 15       # 连续杀跌 + 累计跌幅
W_BOLL = 15       # 布林带下轨突破
W_MACD_DIV = 15   # MACD 底背离
W_KDJ = 10        # KDJ J值极值
W_VOLUME = 5      # 底部放量确认（加分项）


class Skill1BOversold(BaseSkill):
    """
    A 股超跌反弹筛选 Skill。

    多维度超跌信号检测 + 综合评分，输出格式兼容下游 Skill-2A 深度分析。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        client: Any,
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill1b_oversold"
        self._client = client

    def run(self, input_data: dict) -> dict:
        min_amount = input_data.get("min_amount", DEFAULT_MIN_AMOUNT)
        min_price = input_data.get("min_price", DEFAULT_MIN_PRICE)
        min_klines = input_data.get("min_klines", DEFAULT_MIN_KLINES)
        rsi_thresh = input_data.get("rsi_threshold", DEFAULT_RSI_THRESHOLD)
        bias_thresh = input_data.get("bias_threshold", DEFAULT_BIAS_THRESHOLD)
        consec_down = input_data.get("consecutive_down_days", DEFAULT_CONSECUTIVE_DOWN)
        drop_pct_thresh = input_data.get("drop_pct_threshold", DEFAULT_DROP_PCT)
        drop_lookback = input_data.get("drop_lookback_days", DEFAULT_DROP_LOOKBACK)
        min_score = input_data.get("min_oversold_score", DEFAULT_MIN_OVERSOLD_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")
        require_vol = input_data.get("require_volume_confirm", False)
        prefilter_pct = input_data.get("prefilter_change_pct", DEFAULT_PREFILTER_CHANGE_PCT)

        pipeline_run_id = str(uuid.uuid4())

        # ── Step 1: 获取实时行情 & 基础过滤 ──
        all_tickers = self._client.get_spot_all()
        total_count = len(all_tickers)

        if target_symbols:
            pool = self._build_target_pool(all_tickers, target_symbols)
            if not pool and hasattr(self._client, "get_spot_by_hist"):
                pool = self._client.get_spot_by_hist(target_symbols)
        else:
            pool = self._base_filter(all_tickers, min_amount, min_price, prefilter_pct)
        log.info("[skill1b] Step1: %d/%d 通过基础过滤", len(pool), total_count)

        # ── Step 2: 超跌信号检测 + 评分 ──
        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        for item in pool:
            symbol = item["symbol"]
            try:
                klines = self._client.get_klines(symbol, "daily", max(KLINE_LIMIT, min_klines))
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                volumes = [float(k[5]) for k in klines]

                result = self._calc_oversold_score(
                    closes, highs, lows, volumes,
                    rsi_thresh, bias_thresh, consec_down,
                    drop_pct_thresh, drop_lookback,
                )

                if result["oversold_score"] < min_score and not target_symbols:
                    continue

                if require_vol and (result["volume_surge"] is None or result["volume_surge"] < 1.5):
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
                    "rsi": result["rsi"],
                    "bias_20": result["bias_20"],
                    "consecutive_down": result["consecutive_down"],
                    "drop_pct": result["drop_pct"],
                    "below_boll_lower": result["below_boll_lower"],
                    "kdj_j": result["kdj_j"],
                    "macd_divergence": result["macd_divergence"],
                    "volume_surge": result["volume_surge"],
                    "oversold_score": result["oversold_score"],
                    "signal_details": result["signal_details"],
                    "atr_pct": atr_pct,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[skill1b] %s 分析失败: %s", symbol, exc)
                continue

        scored.sort(key=lambda x: x["oversold_score"], reverse=True)

        # ── Step 3: 相关性去重 ──
        candidates = self._deduplicate(scored, returns_map, max_cands)

        log.info("[skill1b] 完成: pool=%d, scored=%d, output=%d",
                 len(pool), len(scored), len(candidates))

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
            "filter_summary": {
                "total_tickers": total_count,
                "after_base_filter": len(pool),
                "after_oversold_filter": len(scored),
                "output_count": len(candidates),
            },
        }

    # ── 超跌评分核心 ─────────────────────────────────────

    @staticmethod
    def _calc_oversold_score(
        closes: List[float],
        highs: List[float],
        lows: List[float],
        volumes: List[float],
        rsi_thresh: float,
        bias_thresh: float,
        consec_down_thresh: int,
        drop_pct_thresh: float,
        drop_lookback: int,
    ) -> dict:
        """计算超跌综合评分（满分 100）。"""
        signals = []
        score = 0.0

        # ── 1. RSI 超卖 ──
        rsi_val = calc_rsi(closes, RSI_PERIOD)
        rsi_score = 0.0
        if rsi_val is not None and rsi_val < rsi_thresh:
            # RSI 越低分越高，线性映射
            rsi_score = W_RSI * min(1.0, (rsi_thresh - rsi_val) / rsi_thresh)
            signals.append(f"RSI({RSI_PERIOD})={rsi_val:.1f}<{rsi_thresh}")
        score += rsi_score

        # ── 2. 乖离率 BIAS(20) ──
        bias_20 = _calc_bias(closes, BOLL_PERIOD)
        bias_score = 0.0
        if bias_20 is not None and bias_20 < bias_thresh:
            bias_score = W_BIAS * min(1.0, (bias_thresh - bias_20) / abs(bias_thresh))
            signals.append(f"BIAS(20)={bias_20:.1f}%<{bias_thresh}%")
        score += bias_score

        # ── 3. 连续杀跌 + 累计跌幅 ──
        consec = _calc_consecutive_down(closes)
        drop_pct = _calc_drop_pct(closes, drop_lookback)
        drop_score = 0.0
        if consec >= consec_down_thresh:
            drop_score += W_DROP * 0.5 * min(1.0, consec / (consec_down_thresh * 2))
            signals.append(f"连跌{consec}天≥{consec_down_thresh}")
        if drop_pct is not None and drop_pct < drop_pct_thresh:
            drop_score += W_DROP * 0.5 * min(1.0, (drop_pct_thresh - drop_pct) / abs(drop_pct_thresh))
            signals.append(f"近{drop_lookback}日跌{drop_pct:.1f}%<{drop_pct_thresh}%")
        score += min(drop_score, float(W_DROP))

        # ── 4. 布林带下轨突破 ──
        below_boll = _check_below_boll_lower(closes)
        if below_boll:
            score += W_BOLL
            signals.append("跌破BOLL下轨")

        # ── 5. MACD 底背离 ──
        macd_div = _check_macd_divergence(closes)
        if macd_div:
            score += W_MACD_DIV
            signals.append("MACD底背离")

        # ── 6. KDJ J值极值 ──
        kdj_j = _calc_kdj_j(closes, highs, lows)
        kdj_score = 0.0
        if kdj_j is not None and kdj_j < 0:
            kdj_score = W_KDJ * min(1.0, abs(kdj_j) / 20.0)
            signals.append(f"KDJ_J={kdj_j:.1f}<0")
        score += kdj_score

        # ── 7. 底部放量（加分项）──
        vol_surge = _calc_volume_surge_bottom(volumes)
        if vol_surge is not None and vol_surge >= 1.5:
            score += W_VOLUME
            signals.append(f"底部放量{vol_surge:.1f}x")

        return {
            "rsi": round(rsi_val, 2) if rsi_val is not None else None,
            "bias_20": round(bias_20, 2) if bias_20 is not None else None,
            "consecutive_down": consec,
            "drop_pct": round(drop_pct, 2) if drop_pct is not None else None,
            "below_boll_lower": below_boll,
            "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
            "macd_divergence": macd_div,
            "volume_surge": round(vol_surge, 2) if vol_surge is not None else None,
            "oversold_score": round(score),
            "signal_details": " | ".join(signals) if signals else "无超跌信号",
        }

    # ── 基础过滤 ─────────────────────────────────────────

    @staticmethod
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

    @staticmethod
    def _base_filter(tickers: List[dict], min_amount: float, min_price: float,
                     prefilter_pct: float = DEFAULT_PREFILTER_CHANGE_PCT) -> List[dict]:
        """基础过滤：仅排雷，不做激进的当日预筛。

        超跌是历史累积状态，不应该只看今天一天的涨跌。
        排雷逻辑：
        1. 排除 ST/退市/北交所/低价股
        2. 排除流动性枯竭（成交额过低）
        3. 可选：当日跌幅预筛（prefilter_pct=0 时禁用）
        """
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

            # 可选预筛：当日跌幅（prefilter_pct=0 或正数时禁用）
            if prefilter_pct < 0:
                change = t.get("change_pct")
                if change is not None and change > prefilter_pct:
                    continue

            result.append({**t, "symbol": symbol})
        return result

    @staticmethod
    def _deduplicate(
        scored: List[dict], returns_map: Dict[str, List[float]], max_cands: int,
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


# ══════════════════════════════════════════════════════════
# 超跌指标计算（纯函数）
# ══════════════════════════════════════════════════════════


def _calc_bias(closes: List[float], period: int = 20) -> Optional[float]:
    """计算乖离率 BIAS = (收盘价 - MA) / MA * 100。"""
    if len(closes) < period:
        return None
    ma = sum(closes[-period:]) / period
    if ma <= 0:
        return None
    return (closes[-1] - ma) / ma * 100


def _calc_consecutive_down(closes: List[float]) -> int:
    """计算最近连续下跌天数。"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _calc_drop_pct(closes: List[float], lookback: int) -> Optional[float]:
    """计算近 N 日累计跌幅（%）。"""
    if len(closes) < lookback + 1:
        return None
    base = closes[-(lookback + 1)]
    if base <= 0:
        return None
    return (closes[-1] - base) / base * 100


def _check_below_boll_lower(closes: List[float]) -> bool:
    """检查最新收盘价是否跌破布林带下轨。"""
    if len(closes) < BOLL_PERIOD:
        return False
    window = closes[-BOLL_PERIOD:]
    ma = sum(window) / BOLL_PERIOD
    variance = sum((x - ma) ** 2 for x in window) / BOLL_PERIOD
    std = math.sqrt(variance)
    lower = ma - BOLL_STD_MULT * std
    return closes[-1] < lower


def _calc_kdj_j(
    closes: List[float], highs: List[float], lows: List[float],
    period: int = KDJ_PERIOD, m1: int = KDJ_M1, m2: int = KDJ_M2,
) -> Optional[float]:
    """计算 KDJ 指标的 J 值。J = 3K - 2D。"""
    if len(closes) < period + m1 + m2:
        return None

    # 计算 RSV 序列
    rsvs = []
    for i in range(period - 1, len(closes)):
        h_window = highs[i - period + 1: i + 1]
        l_window = lows[i - period + 1: i + 1]
        hh = max(h_window)
        ll = min(l_window)
        if hh == ll:
            rsvs.append(50.0)
        else:
            rsvs.append((closes[i] - ll) / (hh - ll) * 100)

    if not rsvs:
        return None

    # K = SMA(RSV, m1)，D = SMA(K, m2)
    k_val = rsvs[0]
    d_val = k_val
    for rsv in rsvs[1:]:
        k_val = (k_val * (m1 - 1) + rsv) / m1
        d_val = (d_val * (m2 - 1) + k_val) / m2

    j_val = 3 * k_val - 2 * d_val
    return j_val


def _check_macd_divergence(closes: List[float], lookback: int = 30) -> bool:
    """
    检测 MACD 底背离：价格创新低但 MACD 柱状图未创新低。

    简化实现：比较最近 lookback 根 K 线内的两个低点区域。
    """
    macd_data = calc_macd(closes)
    hist = macd_data.get("histogram")
    if hist is None:
        return False

    # 需要足够的数据
    if len(closes) < lookback + 10:
        return False

    recent_closes = closes[-lookback:]
    recent_hist_start = len(closes) - lookback

    # 找最近 lookback 根内的最低价位置
    min_idx = 0
    for i in range(1, len(recent_closes)):
        if recent_closes[i] < recent_closes[min_idx]:
            min_idx = i

    # 在最低价之前找次低点（至少间隔 5 根）
    prev_min_idx = None
    search_end = max(0, min_idx - 5)
    for i in range(search_end - 1, -1, -1):
        if prev_min_idx is None or recent_closes[i] < recent_closes[prev_min_idx]:
            prev_min_idx = i

    if prev_min_idx is None:
        return False

    # 价格创新低
    if recent_closes[min_idx] >= recent_closes[prev_min_idx]:
        return False

    # 获取对应位置的 MACD histogram
    # calc_macd 返回的是最后一根的值，需要重新计算序列
    # 简化：用两段区域的 closes 分别算 MACD
    full_idx_1 = recent_hist_start + prev_min_idx
    full_idx_2 = recent_hist_start + min_idx

    macd1 = calc_macd(closes[:full_idx_1 + 1])
    macd2 = calc_macd(closes[:full_idx_2 + 1])

    h1 = macd1.get("histogram")
    h2 = macd2.get("histogram")

    if h1 is None or h2 is None:
        return False

    # MACD histogram 未创新低 = 底背离
    return h2 > h1


def _calc_volume_surge_bottom(volumes: List[float], short_w: int = 1, long_w: int = 5) -> Optional[float]:
    """
    底部放量检测：最后一根 K 线成交量 / 前 5 根均量。

    放量 ≥ 1.5 倍视为恐慌盘涌出信号。
    """
    if len(volumes) < long_w + 1:
        return None
    avg = sum(volumes[-(long_w + 1):-1]) / long_w
    if avg <= 0:
        return None
    return volumes[-1] / avg
