"""
Skill-1：Binance 量化数据采集与候选筛选（v2）

四步筛选流水线：
  1. 大盘过滤 — 调用 /fapi/v1/ticker/24hr，按成交额、振幅、绝对涨跌幅区间过滤
  2. 活跃度异动 — 调用 /fapi/v1/klines 计算短期量比，筛选资金聚焦标的
  3. 技术指标 — 计算 RSI / EMA / MACD / ATR / ADX，生成多因子双向信号评分
  4. 流动性加权 + 相关性去重 — 成交额归一化加分，高相关候选去重

改进点（相对 v1）：
  - P0: 支持做空信号，涨跌幅改为绝对值区间
  - P0: 连续化评分函数替代阶梯打分
  - P1: 加入 ATR 维度，输出供下游止损止盈使用
  - P1: 成交额加权评分，减少低流动性标的
  - P1: 加入 ADX 趋势强度指标
  - P2: 相关性去重，避免重复暴露

数据源：BinancePublicClient（公开端点，无需签名）
通过构造函数注入，便于测试时 mock。
"""

import logging
import math
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# ── 默认参数 ──────────────────────────────────────────────
DEFAULT_MIN_QUOTE_VOLUME = 30_000_000  # 3000 万 USDT
DEFAULT_MIN_AMPLITUDE_PCT = 5.0
DEFAULT_PRICE_CHANGE_MIN = 2.0
DEFAULT_PRICE_CHANGE_MAX = 20.0
DEFAULT_VOLUME_SURGE_RATIO = 1.5
DEFAULT_MIN_SIGNAL_SCORE = 60
DEFAULT_MIN_ADX = 20.0
DEFAULT_KLINE_INTERVAL = "4h"
DEFAULT_MAX_CANDIDATES = 10
KLINE_LIMIT = 100  # K 线条数（用于技术指标计算）

# K 线并发拉取线程数，IO 密集型，可适当调高
DEFAULT_KLINE_WORKERS = 10

# ── RSI 参数 ──────────────────────────────────────────────
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_STRONG_LOW = 50.0
RSI_STRONG_HIGH = 70.0
RSI_OVERBOUGHT = 80.0

# ── EMA 参数 ──────────────────────────────────────────────
EMA_FAST = 20
EMA_SLOW = 50

# ── MACD 参数 ─────────────────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ── ADX 参数 ──────────────────────────────────────────────
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25.0  # ADX > 25 视为有趋势

# ── ATR 参数 ──────────────────────────────────────────────
ATR_PERIOD = 14

# ── 量比计算窗口 ──────────────────────────────────────────
VOLUME_SHORT_WINDOW = 5   # 近 5 根 K 线
VOLUME_LONG_WINDOW = 20   # 近 20 根 K 线

# ── 相关性去重阈值 ────────────────────────────────────────
CORRELATION_THRESHOLD = 0.85

# ── 评分权重（满分 100）─────────────────────────────────
WEIGHT_RSI = 30
WEIGHT_EMA = 20
WEIGHT_MACD = 20
WEIGHT_ADX = 15
WEIGHT_LIQUIDITY = 15


# ══════════════════════════════════════════════════════════
# 技术指标计算（纯函数，无副作用）
# ══════════════════════════════════════════════════════════

def calc_ema(closes: List[float], period: int) -> List[float]:
    """计算 EMA 序列。返回与 closes 等长的列表，前 period-1 个为 NaN。"""
    if len(closes) < period:
        return [float("nan")] * len(closes)

    multiplier = 2.0 / (period + 1)
    ema = [float("nan")] * (period - 1)
    # 初始值：前 period 个的 SMA
    sma = sum(closes[:period]) / period
    ema.append(sma)

    for i in range(period, len(closes)):
        val = (closes[i] - ema[-1]) * multiplier + ema[-1]
        ema.append(val)

    return ema


def calc_rsi(closes: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    """计算最新 RSI 值。数据不足时返回 None。"""
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Wilder 平滑
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_macd(
    closes: List[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal_period: int = MACD_SIGNAL,
) -> Dict[str, Optional[float]]:
    """
    计算最新 MACD 值。

    返回:
        {"macd_line": float, "signal_line": float, "histogram": float}
        数据不足时对应字段为 None。
    """
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    # MACD line = EMA_fast - EMA_slow
    macd_line_series: List[float] = []
    for f, s in zip(ema_fast, ema_slow):
        if math.isnan(f) or math.isnan(s):
            macd_line_series.append(float("nan"))
        else:
            macd_line_series.append(f - s)

    # 过滤有效值计算 signal line
    valid_macd = [v for v in macd_line_series if not math.isnan(v)]
    if len(valid_macd) < signal_period:
        return {"macd_line": None, "signal_line": None, "histogram": None}

    signal_ema = calc_ema(valid_macd, signal_period)

    macd_val = valid_macd[-1]
    signal_val = signal_ema[-1] if not math.isnan(signal_ema[-1]) else None

    if signal_val is None:
        return {"macd_line": macd_val, "signal_line": None, "histogram": None}

    return {
        "macd_line": macd_val,
        "signal_line": signal_val,
        "histogram": macd_val - signal_val,
    }


def calc_volume_surge(volumes: List[float], short_w: int = VOLUME_SHORT_WINDOW, long_w: int = VOLUME_LONG_WINDOW) -> Optional[float]:
    """计算量比：近 short_w 根均量 / 近 long_w 根均量。数据不足返回 None。"""
    if len(volumes) < long_w:
        return None
    recent = volumes[-long_w:]
    long_avg = sum(recent) / long_w
    short_avg = sum(recent[-short_w:]) / short_w
    if long_avg <= 0:
        return None
    return short_avg / long_avg


def calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = ATR_PERIOD) -> Optional[float]:
    """
    计算 ATR（Average True Range）。

    使用 Wilder 平滑法。数据不足返回 None。
    """
    if len(closes) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    # Wilder 平滑
    atr = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[i]) / period

    return atr


def calc_adx(highs: List[float], lows: List[float], closes: List[float], period: int = ADX_PERIOD) -> Optional[float]:
    """
    计算 ADX（Average Directional Index）。

    ADX 衡量趋势强度（不区分方向）。> 25 表示有趋势，< 20 表示震荡。
    数据不足返回 None。
    """
    n = len(closes)
    if n < period * 2 + 1:
        return None

    # 计算 +DM / -DM / TR 序列
    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # Wilder 平滑 +DM, -DM, TR
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])
    smoothed_tr = sum(tr_list[:period])

    dx_list = []

    for i in range(period, len(tr_list)):
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]

        if smoothed_tr == 0:
            dx_list.append(0.0)
            continue

        plus_di = 100.0 * smoothed_plus_dm / smoothed_tr
        minus_di = 100.0 * smoothed_minus_dm / smoothed_tr

        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0.0)
        else:
            dx_list.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return None

    # ADX = DX 的 Wilder 平滑
    adx = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return adx


def calc_returns(closes: List[float]) -> List[float]:
    """计算收益率序列，用于相关性计算。"""
    if len(closes) < 2:
        return []
    return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]


def calc_correlation(returns_a: List[float], returns_b: List[float]) -> float:
    """计算两个收益率序列的 Pearson 相关系数。数据不足返回 0。"""
    n = min(len(returns_a), len(returns_b))
    if n < 10:
        return 0.0

    a = returns_a[-n:]
    b = returns_b[-n:]

    mean_a = sum(a) / n
    mean_b = sum(b) / n

    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
    std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a) / n)
    std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b) / n)

    if std_a == 0 or std_b == 0:
        return 0.0

    return cov / (std_a * std_b)


# ══════════════════════════════════════════════════════════
# 协议：BinancePublicClient 需要实现的方法
# ══════════════════════════════════════════════════════════
# get_exchange_info() -> dict
# get_tickers_24hr() -> list[dict]
# get_klines(symbol, interval, limit) -> list[list]


class Skill1Collect(BaseSkill):
    """
    Binance 量化数据采集与候选筛选 Skill（v2）。

    四步筛选：
      Step 1: 大盘过滤（ticker/24hr）— 绝对涨跌幅区间，支持做空标的
      Step 2: 活跃度异动（klines 量比）
      Step 3: 技术指标评分（RSI + EMA + MACD + ADX + 流动性，连续化评分，双向信号）
      Step 4: 相关性去重

    按综合评分排序，输出 top N 候选。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        client: Any,
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill1_collect"
        self._client = client

    def run(self, input_data: dict) -> dict:
        """
        执行量化数据采集与候选筛选。

        支持两种模式：
          - 全量扫描：不传 target_symbols，走四步筛选
          - 指定币种：传 target_symbols，跳过大盘过滤

        返回:
            符合 skill1_output Schema 的输出字典
        """
        # 解析参数（带默认值）
        min_qv = input_data.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
        min_amp = input_data.get("min_amplitude_pct", DEFAULT_MIN_AMPLITUDE_PCT)
        pc_range = input_data.get("price_change_range", {})
        pc_min = pc_range.get("min_pct", DEFAULT_PRICE_CHANGE_MIN)
        pc_max = pc_range.get("max_pct", DEFAULT_PRICE_CHANGE_MAX)
        surge_ratio = input_data.get("volume_surge_ratio", DEFAULT_VOLUME_SURGE_RATIO)
        min_signal_score = input_data.get("min_signal_score", DEFAULT_MIN_SIGNAL_SCORE)
        min_adx = input_data.get("min_adx", DEFAULT_MIN_ADX)
        interval = input_data.get("kline_interval", DEFAULT_KLINE_INTERVAL)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")

        pipeline_run_id = str(uuid.uuid4())

        # ── 指定币种模式 vs 全量扫描模式 ──
        if target_symbols:
            pool, tickers_count = self._build_target_pool(target_symbols)
        else:
            tradable_symbols = self._get_tradable_symbols()
            tickers = self._client.get_tickers_24hr()
            tickers_count = len(tickers)
            pool = self._filter_tickers(tickers, tradable_symbols, min_qv, min_amp, pc_min, pc_max)
            log.info("[skill1] Step1 大盘过滤: %d/%d 通过", len(pool), tickers_count)

        # 收集所有 pool 中的最大成交额，用于流动性归一化
        max_quote_volume = max((item["quote_volume"] for item in pool), default=1.0)
        if max_quote_volume <= 0:
            max_quote_volume = 1.0

        # ── Step 2 + 3: 并发拉取 K 线并计算技术指标 ──
        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        workers = min(DEFAULT_KLINE_WORKERS, len(pool)) if pool else 1
        log.info("[skill1] 并发拉取 K 线: 候选=%d, 线程数=%d", len(pool), workers)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skill1_kline") as executor:
            future_to_item = {
                executor.submit(
                    self._fetch_and_analyze_symbol,
                    item,
                    interval,
                    surge_ratio,
                    min_signal_score,
                    min_adx,
                    bool(target_symbols),
                    max_quote_volume,
                ): item
                for item in pool
            }
            for future in as_completed(future_to_item):
                result = future.result()  # _fetch_and_analyze_symbol 内部已 catch 异常
                if result is not None:
                    scored_item, returns = result
                    scored.append(scored_item)
                    returns_map[scored_item["symbol"]] = returns

        # 按 signal_score 降序排序
        scored.sort(key=lambda x: x["signal_score"], reverse=True)

        # ── Step 4: 相关性去重 ──
        candidates = self._deduplicate_by_correlation(scored, returns_map, max_cands)

        log.info(
            "[skill1] 完成: pool=%d, 量比通过+有信号=%d, 去重后输出=%d",
            len(pool), len(scored), len(candidates),
        )

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
            "filter_summary": {
                "total_tickers": tickers_count,
                "after_base_filter": len(pool),
                "after_signal_filter": len(scored),
                "output_count": len(candidates),
            },
        }

    def _fetch_and_analyze_symbol(
        self,
        item: dict,
        interval: str,
        surge_ratio: float,
        min_signal_score: int,
        min_adx: float,
        is_target_mode: bool,
        max_quote_volume: float,
    ) -> Optional[Tuple[dict, List[float]]]:
        """
        拉取单币种 K 线并执行量比 + 技术指标评分（线程安全，无共享状态）。

        返回:
            (scored_item, returns) 元组，或 None（不满足条件时）。
        """
        symbol = item["symbol"]
        try:
            klines = self._client.get_klines(symbol, interval, KLINE_LIMIT)
            if not klines or len(klines) < VOLUME_LONG_WINDOW:
                return None

            closes = [float(k[4]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]

            # Step 2: 量比过滤（指定币种模式下放宽，不过滤）
            surge = calc_volume_surge(volumes)
            if surge is None:
                surge = 0.0
            if not is_target_mode and surge < surge_ratio:
                return None

            # Step 3: 技术指标评分（双向）
            score_detail = self._calc_signal_score(
                closes, highs, lows, item["quote_volume"], max_quote_volume
            )
            if score_detail["total_score"] < min_signal_score:
                return None
            adx_val = score_detail["adx"]
            if adx_val is None or adx_val < min_adx:
                return None

            returns = calc_returns(closes)
            scored_item = {
                "symbol": symbol,
                "quote_volume_24h": item["quote_volume"],
                "price_change_pct": item["price_change_pct"],
                "amplitude_pct": item["amplitude_pct"],
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
            }
            return scored_item, returns

        except Exception as exc:
            log.warning("[skill1] %s K线分析失败: %s", symbol, exc)
            return None

    # ── 内部方法 ──────────────────────────────────────────

    def _get_tradable_symbols(self) -> Set[str]:
        """获取当前可交易的 USDT 永续合约交易对集合。"""
        try:
            info = self._client.get_exchange_info()
            symbols = set()
            for s in info.get("symbols", []):
                if (
                    s.get("status") == "TRADING"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                ):
                    symbols.add(s["symbol"])
            return symbols
        except Exception as exc:
            log.warning("[skill1] 获取 exchangeInfo 失败: %s, 跳过交易状态过滤", exc)
            return set()

    def _build_target_pool(self, target_symbols: List[str]) -> Tuple[List[dict], int]:
        """
        指定币种模式：根据用户输入的币种列表构建 pool。

        自动补全 USDT 后缀，从 ticker/24hr 中查找对应行情数据。
        跳过大盘过滤条件，直接进入技术分析。
        """
        normalized = []
        for s in target_symbols:
            s = s.strip().upper()
            if not s:
                continue
            if not s.endswith("USDT"):
                s = s + "USDT"
            normalized.append(s)

        if not normalized:
            return [], 0

        target_set = set(normalized)
        tickers = self._client.get_tickers_24hr()
        pool = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if symbol not in target_set:
                continue
            try:
                quote_vol = float(t.get("quoteVolume", 0))
                high = float(t.get("highPrice", 0))
                low = float(t.get("lowPrice", 0))
                price_change_pct = float(t.get("priceChangePercent", 0))
                amplitude = (high - low) / low * 100.0 if low > 0 else 0.0
            except (ValueError, TypeError, ZeroDivisionError):
                quote_vol = 0.0
                price_change_pct = 0.0
                amplitude = 0.0

            pool.append({
                "symbol": symbol,
                "quote_volume": round(quote_vol, 2),
                "price_change_pct": round(price_change_pct, 2),
                "amplitude_pct": round(amplitude, 2),
            })

        found = {p["symbol"] for p in pool}
        missing = target_set - found
        if missing:
            log.warning("[skill1] 指定币种未找到: %s", ", ".join(sorted(missing)))

        log.info("[skill1] 指定币种模式: 请求 %d 个, 找到 %d 个", len(normalized), len(pool))
        return pool, len(tickers)

    def _filter_tickers(
        self,
        tickers: List[dict],
        tradable: Set[str],
        min_qv: float,
        min_amp: float,
        pc_min: float,
        pc_max: float,
    ) -> List[dict]:
        """
        Step 1: 大盘过滤。

        v2 改进：涨跌幅使用绝对值区间，同时纳入上涨和下跌标的。

        条件：
          - 交易对以 USDT 结尾
          - 在可交易集合中（如果集合非空）
          - 24h 成交额 >= min_qv
          - 24h 振幅 >= min_amp%
          - 24h |涨跌幅| 在 [pc_min, pc_max] 区间（绝对值）
        """
        result = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            if tradable and symbol not in tradable:
                continue

            try:
                quote_vol = float(t.get("quoteVolume", 0))
                high = float(t.get("highPrice", 0))
                low = float(t.get("lowPrice", 0))
                price_change_pct = float(t.get("priceChangePercent", 0))
            except (ValueError, TypeError):
                continue

            if quote_vol < min_qv:
                continue

            if low <= 0:
                continue
            amplitude = (high - low) / low * 100.0
            if amplitude < min_amp:
                continue

            # v2: 绝对值涨跌幅区间过滤（支持做空标的）
            abs_change = abs(price_change_pct)
            if abs_change < pc_min or abs_change > pc_max:
                continue

            # base asset 长度过滤：排除 base < 2 字符的币种
            base_asset = symbol[:-4]
            if len(base_asset) < 2:
                continue

            result.append({
                "symbol": symbol,
                "quote_volume": round(quote_vol, 2),
                "price_change_pct": round(price_change_pct, 2),
                "amplitude_pct": round(amplitude, 2),
            })

        return result

    def _calc_signal_score(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        quote_volume: float,
        max_quote_volume: float,
    ) -> dict:
        """
        Step 3: 多因子技术指标评分（v2 — 连续化、双向、含 ATR/ADX/流动性）。

        评分规则（满分 100）：
          - RSI 信号（30 分）：连续函数，做多/做空分别评分
          - EMA 排列（20 分）：多头/空头排列
          - MACD 信号（20 分）：金叉/死叉 + histogram 方向
          - ADX 趋势强度（15 分）：ADX 越高，信号越可信
          - 流动性加权（15 分）：成交额归一化

        返回 direction: "long" | "short"，取多空中得分更高的方向。
        """
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

        # ── 做多评分 ──
        long_rsi = self._score_rsi_long(rsi_val)
        long_ema = self._score_ema_long(last_close, last_ema20, last_ema50)
        long_macd = self._score_macd_long(ml, sl, hist)

        # ── 做空评分 ──
        short_rsi = self._score_rsi_short(rsi_val)
        short_ema = self._score_ema_short(last_close, last_ema20, last_ema50)
        short_macd = self._score_macd_short(ml, sl, hist)

        # ── 方向无关评分 ──
        adx_score = self._score_adx(adx_val)
        liquidity_score = self._score_liquidity(quote_volume, max_quote_volume)

        long_total = long_rsi + long_ema + long_macd + adx_score + liquidity_score
        short_total = short_rsi + short_ema + short_macd + adx_score + liquidity_score

        if long_total >= short_total:
            direction = "long"
            total_score = long_total
            ema_bullish = True if long_ema > 0 else False
            macd_bullish = True if long_macd > 0 else False
        else:
            direction = "short"
            total_score = short_total
            ema_bullish = False
            macd_bullish = False

        # ATR 百分比（相对于收盘价）
        atr_pct = round(atr_val / last_close * 100.0, 2) if (atr_val and last_close > 0) else None

        return {
            "rsi": round(rsi_val, 2) if rsi_val is not None else None,
            "ema_bullish": ema_bullish,
            "macd_bullish": macd_bullish,
            "total_score": round(total_score),
            "direction": direction,
            "atr": round(atr_val, 8) if atr_val is not None else None,
            "atr_pct": atr_pct,
            "adx": round(adx_val, 2) if adx_val is not None else None,
        }

    # ── RSI 连续化评分 ────────────────────────────────────

    @staticmethod
    def _score_rsi_long(rsi: Optional[float]) -> float:
        """做多 RSI 评分（满分 WEIGHT_RSI=30）。RSI 越低（超卖）分越高。"""
        if rsi is None:
            return 0.0
        if rsi <= RSI_OVERSOLD:
            # 超卖区：30 分（满分）
            return float(WEIGHT_RSI)
        if rsi >= RSI_OVERBOUGHT:
            # 超买区：0 分
            return 0.0
        # 线性插值：30 → 0 分，RSI 从 30 到 80
        return WEIGHT_RSI * (1.0 - (rsi - RSI_OVERSOLD) / (RSI_OVERBOUGHT - RSI_OVERSOLD))

    @staticmethod
    def _score_rsi_short(rsi: Optional[float]) -> float:
        """做空 RSI 评分（满分 WEIGHT_RSI=30）。RSI 越高（超买）分越高。"""
        if rsi is None:
            return 0.0
        if rsi >= RSI_OVERBOUGHT:
            return float(WEIGHT_RSI)
        if rsi <= RSI_OVERSOLD:
            return 0.0
        return WEIGHT_RSI * (rsi - RSI_OVERSOLD) / (RSI_OVERBOUGHT - RSI_OVERSOLD)

    # ── EMA 评分 ──────────────────────────────────────────

    @staticmethod
    def _score_ema_long(close: float, ema20: Optional[float], ema50: Optional[float]) -> float:
        """做多 EMA 评分（满分 WEIGHT_EMA=20）。"""
        if ema20 is None or ema50 is None:
            return 0.0
        if close > ema20 > ema50:
            return float(WEIGHT_EMA)  # 完美多头排列
        if close > ema20:
            return WEIGHT_EMA * 0.5  # 价格在短期均线上方
        return 0.0

    @staticmethod
    def _score_ema_short(close: float, ema20: Optional[float], ema50: Optional[float]) -> float:
        """做空 EMA 评分（满分 WEIGHT_EMA=20）。"""
        if ema20 is None or ema50 is None:
            return 0.0
        if close < ema20 < ema50:
            return float(WEIGHT_EMA)  # 完美空头排列
        if close < ema20:
            return WEIGHT_EMA * 0.5
        return 0.0

    # ── MACD 评分 ─────────────────────────────────────────

    @staticmethod
    def _score_macd_long(ml: Optional[float], sl: Optional[float], hist: Optional[float]) -> float:
        """做多 MACD 评分（满分 WEIGHT_MACD=20）。"""
        if ml is None or sl is None or hist is None:
            return 0.0
        if ml > 0 and hist >= 0:
            return float(WEIGHT_MACD)  # 零轴上方金叉/正柱
        if hist > 0:
            return WEIGHT_MACD * 0.5  # histogram 转正
        return 0.0

    @staticmethod
    def _score_macd_short(ml: Optional[float], sl: Optional[float], hist: Optional[float]) -> float:
        """做空 MACD 评分（满分 WEIGHT_MACD=20）。"""
        if ml is None or sl is None or hist is None:
            return 0.0
        if ml < 0 and hist <= 0:
            return float(WEIGHT_MACD)  # 零轴下方死叉/负柱
        if hist < 0:
            return WEIGHT_MACD * 0.5
        return 0.0

    # ── ADX 评分 ──────────────────────────────────────────

    @staticmethod
    def _score_adx(adx: Optional[float]) -> float:
        """ADX 趋势强度评分（满分 WEIGHT_ADX=15）。ADX 越高分越高。"""
        if adx is None:
            return 0.0
        # ADX 0~50 线性映射到 0~15 分，超过 50 封顶
        clamped = min(adx, 50.0)
        return WEIGHT_ADX * (clamped / 50.0)

    # ── 流动性评分 ────────────────────────────────────────

    @staticmethod
    def _score_liquidity(quote_volume: float, max_quote_volume: float) -> float:
        """流动性评分（满分 WEIGHT_LIQUIDITY=15）。对数归一化。"""
        if quote_volume <= 0 or max_quote_volume <= 0:
            return 0.0
        # 对数归一化：log(vol) / log(max_vol)
        log_vol = math.log(quote_volume + 1)
        log_max = math.log(max_quote_volume + 1)
        if log_max <= 0:
            return 0.0
        ratio = min(log_vol / log_max, 1.0)
        return WEIGHT_LIQUIDITY * ratio

    # ── 相关性去重 ────────────────────────────────────────

    @staticmethod
    def _deduplicate_by_correlation(
        scored: List[dict],
        returns_map: Dict[str, List[float]],
        max_cands: int,
    ) -> List[dict]:
        """
        Step 4: 相关性去重。

        从已排序的候选列表中，逐个加入结果集。
        如果新候选与已选中的任一候选相关系数 > 阈值，则跳过。
        """
        selected: List[dict] = []
        selected_returns: List[List[float]] = []

        for item in scored:
            if len(selected) >= max_cands:
                break

            symbol = item["symbol"]
            rets = returns_map.get(symbol, [])

            # 检查与已选候选的相关性
            is_redundant = False
            for sel_rets in selected_returns:
                corr = calc_correlation(rets, sel_rets)
                if corr > CORRELATION_THRESHOLD:
                    is_redundant = True
                    log.debug("[skill1] %s 与已选候选正相关 %.2f > %.2f，跳过",
                              symbol, corr, CORRELATION_THRESHOLD)
                    break

            if not is_redundant:
                selected.append(item)
                selected_returns.append(rets)

        return selected
