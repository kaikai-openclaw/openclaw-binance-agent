"""
加密货币合约超跌反弹筛选 Skill（双模式）

两种独立的超跌分析模式，适用于不同交易策略：

## 短期超跌（ShortTermOversoldSkill）— 4h K 线
  适用场景：日内/隔日超短线反弹，捕捉恐慌抛售后的 V 型反转
  K 线周期：4h（100 根 ≈ 17 天）
  核心信号：RSI 极端超卖、资金费率极端负值、短期暴跌、底部放量
  持仓周期：4h ~ 2 天
  阈值特点：RSI < 22、BIAS < -10%、连跌 ≥ 5 根 4h、累跌 < -15%

## 长期超跌（LongTermOversoldSkill）— 1d K 线
  适用场景：波段反弹，捕捉中期超跌后的均值回归
  K 线周期：1d（100 根 ≈ 100 天）
  核心信号：日线级别 RSI/BIAS 超跌、距高点深度回撤、MACD 底背离
  持仓周期：3 天 ~ 2 周
  阈值特点：RSI < 35、BIAS < -15%、连跌 ≥ 3 天、累跌 < -35%、距高点 > -40%

两者共享：基础过滤逻辑、资金费率信号、相关性去重、纯函数指标库
"""

import calendar
import logging
import math
import re
import time
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
    EMA_SLOW,
    ATR_PERIOD,
    ATR_PERIOD_FILTER,
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
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 资金费率阈值（两种模式共享）
FUNDING_RATE_EXTREME = -0.001  # -0.1%
FUNDING_RATE_VERY_EXTREME = -0.003  # -0.3%

# ══════════════════════════════════════════════════════════
# 短期超跌参数（4h K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "4h"
ST_MIN_KLINES = 50
ST_RSI_THRESHOLD = 22.0  # 4h 级别 RSI < 22（收紧自28，减少噪音信号）
ST_BIAS_THRESHOLD = -10.0  # 4h 乖离率 < -10%（收紧自-8%）
ST_CONSECUTIVE_DOWN = 5  # 连续下跌 ≥ 5 根 4h ≈ 20小时（收紧自3，防止反弹中继误触）
ST_DROP_PCT = -15.0  # 近 N 根累计跌幅 < -15%（收紧自-12%）
ST_DROP_LOOKBACK = 18  # 回看 18 根 4h = 3 天
ST_DRAWDOWN_THRESHOLD = -20.0  # 距近期高点回撤 > 20%（短期看 5 天内）
ST_DRAWDOWN_LOOKBACK = 30  # 回看 30 根 4h = 5 天
ST_VOL_SURGE_THRESHOLD = 2.0  # 底部放量 ≥ 2x

# 短期评分权重（满分 100）— 回测优化后 v2
# 回测结论（200币 × 6个月）：
#   有效维度：RSI超卖(+1.04%) BIAS乖离(+1.04%) 布林下轨(+0.56%) 距高点回撤(+0.46%)
#   无效维度：连续杀跌(-0.33%) MACD背离(-0.06%) 底部放量(-0.00%)
#   弱有效：KDJ极值(+0.14%)
ST_W_RSI = 24  # RSI 极端超卖（有效，继续升权）
ST_W_BIAS = 18  # 乖离率（有效，继续升权）
ST_W_DROP = 4  # 连续杀跌（回测无效，降权 10→4）
ST_W_BOLL = 12  # 布林带（有效，升权）
ST_W_MACD_DIV = 2  # MACD 背离（回测无效，降权 5→2）
ST_W_KDJ = 4  # KDJ（弱有效，降权 7→4）
ST_W_FUNDING = 12  # 资金费率只作为确认项，避免弱形态被过度加分
ST_W_DRAWDOWN = 12  # 距高点回撤（有效，升权 10→12）
ST_W_VOLUME = 4  # 底部放量（回测无效，降权 10→4）
ST_W_CONFIRMATION = 8  # 1h/4h 右侧确认
# 总计：24+18+4+12+2+4+12+12+4+8 = 100

# ══════════════════════════════════════════════════════════
# 长期超跌参数（1d K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1d"
LT_MIN_KLINES = 60  # 最低 60 天数据（新币也能参与）
LT_RSI_THRESHOLD = 35.0  # 日线 RSI < 35 (从30放宽)
LT_BIAS_THRESHOLD = -15.0  # 日线 20 日乖离率 < -15%
LT_CONSECUTIVE_DOWN = 3  # 连续下跌 ≥ 3 天（收紧自2，减少噪音）
LT_DROP_PCT = -35.0  # 近 N 日累计跌幅 < -35%（收紧自-30%，要求更深的超跌）
LT_DROP_LOOKBACK = 14  # 回看 14 天
LT_DRAWDOWN_THRESHOLD = -40.0  # 距近期高点回撤 > 40%（中期深度回调）
LT_DRAWDOWN_LOOKBACK = 180  # 回看 180 天（半年），覆盖完整中期下跌周期
LT_VOL_SURGE_THRESHOLD = 1.5  # 日线放量 ≥ 1.5x

# 长期评分权重（满分 100）— 侧重趋势超跌和背离信号
LT_W_RSI = 12  # RSI
LT_W_BIAS = 15  # 乖离率（日线 BIAS 更可靠）
LT_W_DROP = 12  # 连续杀跌 + 累计跌幅
LT_W_BOLL = 10  # 布林带
LT_W_MACD_DIV = 15  # MACD 底背离（日线级别背离可靠性高，高权重）
LT_W_KDJ = 8  # KDJ
LT_W_FUNDING = 8  # 资金费率（长期看权重降低）
LT_W_DRAWDOWN = 15  # 距高点回撤（长期核心指标）
LT_W_VOLUME = 5  # 底部放量

DEFAULT_MIN_QUOTE_VOLUME = 10_000_000
DEFAULT_MIN_OVERSOLD_SCORE = 40  # 回测优化最优值
DEFAULT_MAX_CANDIDATES = 10
DEFAULT_MAX_SPREAD_PCT = 0.25
DEFAULT_MAX_ABS_FUNDING_RATE = 0.01
LIQUIDITY_VOLUME_RATIO_MIN = 0.50  # 最近 4h 成交量须 >= 24h 小时均值的 50%

MARKET_BREADTH_MIN_QUOTE_VOLUME = 20_000_000
MARKET_BREADTH_MIN_SAMPLE_SIZE = 30
MAJOR_BREADTH_MIN_SAMPLE_SIZE = 5
MAJOR_BREADTH_BASES = {
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "LTC",
    "BCH",
    "DOT",
    "NEAR",
    "AAVE",
    "UNI",
}
MARKET_REGIME_SYMBOL = "BTCUSDT"
MARKET_REGIME_INTERVAL = "4h"
MARKET_REGIME_KLINE_LIMIT = 80
MARKET_REGIME_PANIC_DROP_PCT = -8.0
MARKET_REGIME_DOWNTREND_LOOKBACK = 12
MARKET_REGIME_WEAK_TREND_SCORE_ADJUSTMENT = 10
SYMBOL_TREND_INTERVAL = "6h"
SYMBOL_TREND_KLINE_LIMIT = 80
SYMBOL_TREND_RECENT_DROP_PCT = -10.0
SYMBOL_TREND_RECENT_LOOKBACK = 8


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════


class _CryptoOversoldBase(BaseSkill):
    """超跌筛选共享基类，封装基础过滤、资金费率获取、去重等通用逻辑。"""

    def __init__(
        self,
        state_store,
        input_schema,
        output_schema,
        client,
        risk_controller: Optional["RiskController"] = None,
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self._client = client
        self._risk_controller = risk_controller

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

    def _base_filter(self, tickers, tradable, min_qv):
        exclude_bases = {"USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP"}
        result = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in tradable:
                continue
            base = sym.replace("USDT", "")
            if base in exclude_bases or not re.match(r"^[A-Z0-9]{2,15}$", base):
                continue
            if self._risk_controller and self._risk_controller.is_blacklisted(sym):
                continue
            qv = float(t.get("quoteVolume", 0))
            if qv < min_qv:
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

    def _calculate_market_breadth(
        self,
        tickers: Optional[list],
        tradable: Optional[set] = None,
    ) -> dict:
        """计算 24h/4h 全市场和主流币上涨广度。"""
        breadth = {
            "breadth_pct_24h": None,
            "breadth_pct_4h": None,
            "major_breadth_pct_4h": None,
            "breadth_sample_size": 0,
            "major_breadth_sample_size": 0,
        }
        if not tickers:
            return breadth
        if tradable is None:
            tradable = self._get_tradable_symbols()

        universe = []
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if symbol not in tradable:
                continue
            try:
                quote_volume = float(ticker.get("quoteVolume", 0))
                change_24h = float(ticker.get("priceChangePercent", 0))
            except (TypeError, ValueError):
                continue
            if quote_volume < MARKET_BREADTH_MIN_QUOTE_VOLUME:
                continue
            universe.append((symbol, change_24h))

        if universe:
            up_24h = sum(1 for _, change in universe if change > 0)
            breadth["breadth_pct_24h"] = round(up_24h / len(universe) * 100, 2)

        up_4h = 0
        sample_4h = 0
        major_up_4h = 0
        major_sample_4h = 0
        for symbol, _ in universe:
            try:
                # 缓存层会剔除当前未闭合 K 线；取 3 根可确保仍保留
                # 最近两根已闭合 4h K 线用于广度比较。
                klines_4h = self._fetch_klines(symbol, "4h", 3)
            except Exception:
                continue
            if not klines_4h or len(klines_4h) < 2:
                continue
            try:
                prev_close = float(klines_4h[-2][4])
                last_close = float(klines_4h[-1][4])
            except (TypeError, ValueError, IndexError):
                continue
            if prev_close <= 0:
                continue

            sample_4h += 1
            is_up = last_close > prev_close
            if is_up:
                up_4h += 1

            base = symbol[:-4]
            if base in MAJOR_BREADTH_BASES:
                major_sample_4h += 1
                if is_up:
                    major_up_4h += 1

        breadth["breadth_sample_size"] = sample_4h
        breadth["major_breadth_sample_size"] = major_sample_4h
        if sample_4h:
            breadth["breadth_pct_4h"] = round(up_4h / sample_4h * 100, 2)
        if major_sample_4h:
            breadth["major_breadth_pct_4h"] = round(
                major_up_4h / major_sample_4h * 100, 2
            )
        return breadth

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

    def _symbol_trend_filter_reason(
        self, symbol: str, input_data: dict
    ) -> Optional[str]:
        if input_data.get("ignore_symbol_trend_filter"):
            return None

        try:
            klines = self._fetch_klines(
                symbol,
                input_data.get("symbol_trend_interval", SYMBOL_TREND_INTERVAL),
                input_data.get("symbol_trend_kline_limit", SYMBOL_TREND_KLINE_LIMIT),
            )
        except Exception as exc:
            log.warning("[%s] %s 趋势过滤数据获取失败: %s", self.name, symbol, exc)
            return "symbol_trend_unknown"

        if not klines or len(klines) < 60:
            return "symbol_trend_insufficient_klines"

        closes = [float(k[4]) for k in klines]
        ema_fast_series = calc_ema(closes, EMA_FAST)
        ema_slow_series = calc_ema(closes, EMA_SLOW)
        ema_fast = ema_fast_series[-1] if ema_fast_series else None
        ema_slow = ema_slow_series[-1] if ema_slow_series else None
        last_close = closes[-1]
        lookback = input_data.get(
            "symbol_trend_recent_lookback",
            SYMBOL_TREND_RECENT_LOOKBACK,
        )
        recent_return_pct = (
            (last_close - closes[-lookback]) / closes[-lookback] * 100
            if len(closes) > lookback and closes[-lookback] > 0
            else 0.0
        )
        downtrend = (
            ema_fast is not None
            and ema_slow is not None
            and last_close < ema_fast < ema_slow
        )
        waterfall = recent_return_pct <= input_data.get(
            "symbol_trend_recent_drop_pct",
            SYMBOL_TREND_RECENT_DROP_PCT,
        )
        if downtrend and waterfall:
            return f"symbol_6h_waterfall:{recent_return_pct:.2f}%"
        return None

    def _get_market_regime(
        self,
        input_data: dict,
        tickers: Optional[list] = None,
        tradable: Optional[set] = None,
        use_market_breadth: bool = True,
    ) -> dict:
        """
        判断当前市场是否适合做超跌反弹。

        超跌均值回归只适合非单边瀑布环境。若 BTCUSDT 处于 4h 级别
        明确下跌趋势或短期暴跌，本轮直接阻断，避免系统性接刀。
        """
        if input_data.get("ignore_market_regime"):
            return {
                "status": "enabled",
                "breadth_status": "enabled",
                "reason": "ignore_market_regime=true",
                "score_adjustment": 0,
            }

        symbol = input_data.get("market_regime_symbol", MARKET_REGIME_SYMBOL)
        try:
            klines = self._fetch_klines(
                symbol,
                input_data.get("market_regime_interval", MARKET_REGIME_INTERVAL),
                input_data.get("market_regime_kline_limit", MARKET_REGIME_KLINE_LIMIT),
            )
        except Exception as exc:
            log.warning("[%s] 市场状态获取失败: %s", self.name, exc)
            return {"status": "unknown", "reason": f"fetch_failed:{exc}"}

        if not klines or len(klines) < 60:
            return {"status": "unknown", "reason": "insufficient_market_klines"}

        closes = [float(k[4]) for k in klines]
        last_close = closes[-1]
        ema5_series = calc_ema(closes, 5)
        ema20_series = calc_ema(closes, 20)
        ema5 = ema5_series[-1] if ema5_series else None
        ema20 = ema20_series[-1] if ema20_series else None
        lookback = input_data.get(
            "market_regime_downtrend_lookback",
            MARKET_REGIME_DOWNTREND_LOOKBACK,
        )
        recent_return_pct = (
            (last_close - closes[-lookback]) / closes[-lookback] * 100
            if len(closes) > lookback and closes[-lookback] > 0
            else 0.0
        )
        panic_drop_pct = input_data.get(
            "market_regime_panic_drop_pct",
            MARKET_REGIME_PANIC_DROP_PCT,
        )
        panic_drop = recent_return_pct <= panic_drop_pct
        if panic_drop:
            reasons = [f"BTC recent_return={recent_return_pct:.2f}%"]
            return {
                "status": "blocked",
                "breadth_status": "blocked",
                "reason": "; ".join(reasons),
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                "score_adjustment": 0,
            }
        hard_weak_trend = (
            ema5 is not None
            and ema20 is not None
            and last_close < ema20
            and ema5 < ema20 * 0.995
        )
        if hard_weak_trend:
            return {
                "status": "blocked",
                "breadth_status": "blocked",
                "reason": "BTC 4h strong weak trend",
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4),
                "btc_ema20": round(ema20, 4),
                "score_adjustment": 0,
            }

        soft_weak_trend = (
            ema5 is not None
            and ema20 is not None
            and last_close < ema20
            and ema5 < ema20
        )
        if not use_market_breadth:
            if soft_weak_trend:
                return {
                    "status": "blocked",
                    "breadth_status": "not_applicable",
                    "reason": "BTC 4h weak trend",
                    "symbol": symbol,
                    "recent_return_pct": round(recent_return_pct, 4),
                    "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                    "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                    "score_adjustment": 0,
                }
            return {
                "status": "enabled",
                "breadth_status": "not_applicable",
                "reason": "market_regime_ok",
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                "score_adjustment": 0,
            }

        if tickers is None:
            try:
                tickers = self._client.get_tickers_24hr()
            except Exception as exc:
                log.warning("[%s] 市场广度行情获取失败: %s", self.name, exc)
                return {
                    "status": "unknown",
                    "breadth_status": "unknown",
                    "reason": f"breadth_fetch_failed:{exc}",
                    "symbol": symbol,
                    "recent_return_pct": round(recent_return_pct, 4),
                    "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                    "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                    "score_adjustment": 0,
                }
        breadth = self._calculate_market_breadth(tickers, tradable=tradable)
        breadth_pct_4h = breadth["breadth_pct_4h"]
        major_breadth_pct_4h = breadth["major_breadth_pct_4h"]
        if breadth["breadth_sample_size"] < MARKET_BREADTH_MIN_SAMPLE_SIZE:
            return {
                "status": "blocked",
                "breadth_status": "blocked",
                "reason": f"4h 广度样本不足 {breadth['breadth_sample_size']}，暂停超跌做多",
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                "breadth_pct": breadth_pct_4h,
                "score_adjustment": 0,
                **breadth,
            }
        if breadth_pct_4h is not None and breadth_pct_4h < 35.0:
            return {
                "status": "blocked",
                "breadth_status": "blocked",
                "reason": f"全市场4h上涨广度 {breadth_pct_4h:.1f}% 过低，暂停超跌做多",
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                "breadth_pct": breadth_pct_4h,
                "score_adjustment": 0,
                **breadth,
            }

        score_adjustment = 0
        breadth_status = "enabled"
        cautious_reasons = []
        weak_4h_breadth = breadth_pct_4h is not None and breadth_pct_4h < 45.0
        weak_major_breadth = (
            major_breadth_pct_4h is not None
            and breadth["major_breadth_sample_size"] >= MAJOR_BREADTH_MIN_SAMPLE_SIZE
            and major_breadth_pct_4h < 40.0
        )
        if soft_weak_trend:
            cautious_reasons.append("BTC 4h weak trend")
            score_adjustment += MARKET_REGIME_WEAK_TREND_SCORE_ADJUSTMENT
        if weak_4h_breadth:
            breadth_status = "cautious"
            cautious_reasons.append(f"全市场4h上涨广度 {breadth_pct_4h:.1f}% 偏低")
            score_adjustment += 10
        if weak_major_breadth:
            breadth_status = "cautious"
            cautious_reasons.append(f"主流币4h上涨广度 {major_breadth_pct_4h:.1f}% 偏低")
            score_adjustment += 5
        if cautious_reasons:
            return {
                "status": "cautious",
                "breadth_status": breadth_status,
                "reason": "；".join(cautious_reasons),
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
                "btc_ema5": round(ema5, 4) if ema5 is not None else None,
                "btc_ema20": round(ema20, 4) if ema20 is not None else None,
                "breadth_pct": breadth_pct_4h,
                "score_adjustment": min(score_adjustment, 20),
                **breadth,
            }

        return {
            "status": "enabled",
            "breadth_status": "enabled",
            "reason": "market_regime_ok",
            "symbol": symbol,
            "recent_return_pct": round(recent_return_pct, 4),
            "btc_ema5": round(ema5, 4) if ema5 is not None else None,
            "btc_ema20": round(ema20, 4) if ema20 is not None else None,
            "breadth_pct": breadth_pct_4h,
            "score_adjustment": 0,
            **breadth,
        }

    @staticmethod
    def _build_4h_confirmation(
        current_price: float,
        lows: List[float],
        klines_1h: List[list],
        rsi_1h: Optional[float],
        rsi_1h_trend: Optional[float],
        support_distance_pct: Optional[float],
        panic_selling_detected: bool,
    ) -> dict:
        """构建 4h 超跌右侧确认。"""
        reasons: List[str] = []
        signal_count = 0
        momentum_count = 0

        if (
            rsi_1h is not None
            and rsi_1h_trend is not None
            and 30 <= rsi_1h < 50
            and rsi_1h_trend > 0
        ):
            signal_count += 1
            momentum_count += 1
            reasons.append("1h RSI回升")

        recent_4h_low = min(lows[-2:]) if len(lows) >= 2 else None
        if recent_4h_low is not None and current_price >= recent_4h_low:
            signal_count += 1
            reasons.append("未破近4h低点")

        if len(klines_1h) >= 3:
            recent_1h_high = max(float(k[2]) for k in klines_1h[-3:-1])
            if current_price > recent_1h_high:
                signal_count += 1
                momentum_count += 1
                reasons.append("站回近2根1h高点")

        if (
            support_distance_pct is not None
            and 0 <= support_distance_pct <= 3.0
            and not panic_selling_detected
        ):
            signal_count += 1
            reasons.append("支撑附近未放量破位")

        passed = signal_count >= 2 and momentum_count >= 1
        strong = signal_count >= 3 and momentum_count >= 1
        return {
            "passed": passed,
            "strong": strong,
            "signal_count": signal_count,
            "momentum_count": momentum_count,
            "reasons": reasons,
            "reason": " | ".join(reasons) if reasons else "右侧确认不足",
        }

    def _run_scan(
        self,
        input_data: dict,
        interval: str,
        min_klines: int,
        rsi_thresh: float,
        bias_thresh: float,
        consec_thresh: int,
        drop_thresh: float,
        drop_lookback: int,
        dd_thresh: float,
        dd_lookback: int,
        vol_thresh: float,
        weights: dict,
    ) -> dict:
        """通用扫描流程，短期/长期共用。"""
        min_qv = input_data.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
        min_score = input_data.get("min_oversold_score", DEFAULT_MIN_OVERSOLD_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        max_spread_pct = input_data.get("max_spread_pct", DEFAULT_MAX_SPREAD_PCT)
        max_abs_funding_rate = input_data.get(
            "max_abs_funding_rate",
            DEFAULT_MAX_ABS_FUNDING_RATE,
        )
        target_symbols = input_data.get("target_symbols")
        # 允许输入覆盖默认阈值
        rsi_thresh = input_data.get("rsi_threshold", rsi_thresh)
        bias_thresh = input_data.get("bias_threshold", bias_thresh)

        pipeline_run_id = str(uuid.uuid4())

        tickers = self._client.get_tickers_24hr()
        total_count = len(tickers)
        funding_map = self._build_funding_map()
        tradable = self._get_tradable_symbols()
        market_regime = self._get_market_regime(
            input_data,
            tickers=tickers,
            tradable=tradable,
            use_market_breadth=interval == "4h",
        )
        if market_regime.get("status") == "blocked" or (
            interval == "4h" and market_regime.get("status") not in {"enabled", "cautious"}
        ):
            log.warning(
                "[%s] 市场状态阻断超跌交易: %s",
                self.name,
                market_regime.get("reason", ""),
            )
            return {
                "state_id": str(uuid.uuid4()),
                "candidates": [],
                "pipeline_run_id": pipeline_run_id,
                "filter_summary": {
                    "total_tickers": total_count,
                    "after_base_filter": 0,
                    "after_oversold_filter": 0,
                    "output_count": 0,
                    "min_oversold_score": min_score,
                    "effective_min_oversold_score": min_score,
                },
                "market_regime": market_regime,
            }
        score_adjustment = int(market_regime.get("score_adjustment", 0) or 0)
        effective_min_score = min_score + score_adjustment
        cautious_mode = interval == "4h" and market_regime.get("status") == "cautious"
        if cautious_mode:
            log.warning(
                "[%s] 谨慎模式: %s，超跌评分门槛 %s -> %s，要求强右侧确认",
                self.name,
                market_regime.get("reason", ""),
                min_score,
                effective_min_score,
            )

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
                quality_reason = self._quality_filter_reason(
                    item,
                    fr,
                    max_spread_pct,
                    max_abs_funding_rate,
                )
                if quality_reason:
                    log.info(
                        "[%s] %s 交易对象质量过滤: %s",
                        self.name,
                        symbol,
                        quality_reason,
                    )
                    continue

                trend_reason = self._symbol_trend_filter_reason(symbol, input_data)
                if trend_reason:
                    log.info("[%s] %s 趋势过滤: %s", self.name, symbol, trend_reason)
                    continue

                # 追高过滤：24h涨幅过大说明反弹已走完，此时做多是追高
                # 超跌策略适合在下跌中进场，若24h已大幅反弹说明错过了最佳时机
                _price_change_pct = float(item.get("priceChangePercent", 0))
                _chase_threshold = 10.0  # 24h涨幅超过10%视为追高
                if _price_change_pct > _chase_threshold:
                    log.info(
                        "[%s] %s 24h涨幅 %.2f%% 超过 %.0f%%，跳过（反弹已走完，追高风险）",
                        self.name,
                        symbol,
                        _price_change_pct,
                        _chase_threshold,
                    )
                    continue

                # 拉取足够的 K 线：至少覆盖回撤回看窗口
                kline_need = max(KLINE_LIMIT, dd_lookback + 20)
                klines = self._fetch_klines(symbol, interval, kline_need)
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                volumes = [float(k[5]) for k in klines]
                opens = [float(k[1]) for k in klines]
                current_price = float(item.get("lastPrice", 0))

                # ── 流动性检查 ──────────────────────────────────────────────
                # RSI 极端时流动性往往枯竭，避免高滑点入场
                if interval == "4h" and len(volumes) >= 6:
                    recent_4h_vol = volumes[-1]
                    avg_4h_vol_24h = (
                        sum(volumes[-6:]) / 6.0 if len(volumes) >= 6 else 0
                    )
                    if (
                        avg_4h_vol_24h > 0
                        and recent_4h_vol < avg_4h_vol_24h * LIQUIDITY_VOLUME_RATIO_MIN
                    ):
                        log.info(
                            f"[{self.name}] {symbol} 流动性不足: "
                            f"4h成交量={recent_4h_vol:.0f} < 24h均值*50%={avg_4h_vol_24h * LIQUIDITY_VOLUME_RATIO_MIN:.0f}"
                        )
                        continue

                # ── 支撑位检测 ──────────────────────────────────────────────
                # 查找近60根K线的局部低点作为支撑位参考
                recent_lows = []
                lookback_support = 60
                for i in range(lookback_support, len(lows)):
                    window = lows[i - lookback_support : i]
                    if window:
                        min_val = min(window)
                        min_idx = window.index(min_val)
                        recent_lows.append((min_val, i - lookback_support + min_idx))
                # 找到最近的低点
                support_level = None
                support_distance_pct = None
                if recent_lows:
                    support_level, _ = recent_lows[-1]
                    if support_level > 0:
                        support_distance_pct = (
                            (current_price - support_level) / support_level * 100
                            if current_price > 0
                            else None
                        )
                # 价格距离支撑位 < 3% 认为在支撑位附近
                is_near_support = (
                    support_distance_pct is not None
                    and 0 <= support_distance_pct <= 3.0
                )

                # ── 恐慌抛售检测 ──────────────────────────────────────────
                # 特征：放量 + 快速下跌（单根K线跌幅 > 3%）+ 长下影线
                panic_selling_detected = False
                if len(klines) >= 4:
                    recent_4h = klines[-4:]
                    for k in recent_4h:
                        k_open = float(k[1])
                        k_close = float(k[4])
                        k_high = float(k[2])
                        k_low = float(k[3])
                        k_vol = float(k[5])
                        k_change = (
                            (k_close - k_open) / k_open * 100 if k_open > 0 else 0
                        )
                        body = abs(k_close - k_open)
                        lower_shadow = (
                            min(k_open, k_close) - k_low if k_close != k_open else 0
                        )
                        # 恐慌抛售：跌幅 > 3%，下影线 > 实体的2倍
                        if k_change < -3.0 and body > 0 and lower_shadow / body >= 2.0:
                            panic_selling_detected = True
                            break

                result = calc_oversold_score(
                    closes,
                    highs,
                    lows,
                    volumes,
                    rsi_thresh,
                    bias_thresh,
                    consec_thresh,
                    drop_thresh,
                    drop_lookback,
                    dd_thresh,
                    dd_lookback,
                    vol_thresh,
                    fr,
                    weights,
                )

                # ── 实时价格组合分析 ──────────────────────────────────────
                # 在已关闭 K 线形态判断基础上，叠加当前实时价格变动
                # 判断：4h收盘后价格是否继续大跌（接飞刀风险）或已大幅反弹（踏空风险）
                last_closed_close = closes[-1]
                price_change_since_close_pct = (
                    (current_price - last_closed_close) / last_closed_close * 100
                    if current_price > 0 and last_closed_close > 0
                    else 0.0
                )
                # 动能惩罚：4h收盘后价格继续大跌 → 接飞刀风险高，扣分
                # 做多方向：收盘后继续大跌意味着还没见底，抄底可能抄在半山腰
                # 使用平方惩罚：跌幅越大惩罚增速越快（比线性更陡峭）
                momentum_penalty = 0.0
                _momentum_drop_thresh = -3.0  # 超过3%继续跌认为还没见底
                if price_change_since_close_pct < _momentum_drop_thresh:
                    excess = abs(price_change_since_close_pct) - abs(
                        _momentum_drop_thresh
                    )
                    # 平方惩罚：-4%→扣1.5分，-6%→扣13.5分，-8%→扣15分(封顶)
                    momentum_penalty = min(15.0, excess * excess * 1.5)
                    # 如果价格在支撑位附近，惩罚减半（支撑位继续跌可能是最后一跌）
                    if is_near_support:
                        momentum_penalty *= 0.5
                        log.info(
                            "[%s] %s 4h收盘后继续跌 %.2f%%（支撑位附近），接飞刀风险扣分 %.1f（衰减后）",
                            self.name,
                            symbol,
                            price_change_since_close_pct,
                            momentum_penalty,
                        )
                    else:
                        log.info(
                            "[%s] %s 4h收盘后继续跌 %.2f%%，接飞刀风险扣分 %.1f",
                            self.name,
                            symbol,
                            price_change_since_close_pct,
                            momentum_penalty,
                        )
                result["oversold_score"] = max(
                    1, result["oversold_score"] - momentum_penalty
                )
                result["momentum_penalty"] = momentum_penalty

                # ── 1h RSI 先行信号（增强版：加入趋势方向）───────────────
                # RSI趋势比绝对值更重要：RSI正在回升 > RSI静态低位
                # RSI 30-40: 初步回升，+5分（入场信号确认中）
                # RSI 40-50: 回升确认，+4分
                # RSI < 30: 仍在极超卖，可能继续跌，-3分
                # RSI > 50: 已进入正常/偏强，可能已反弹过多，-3分
                # 恐慌抛售检测到时，RSI低位但仍在恶化，额外扣分
                klines_1h: List[list] = []
                try:
                    klines_1h = self._fetch_klines(symbol, "1h", 20)
                    if klines_1h:
                        closes_1h = [float(k[4]) for k in klines_1h]
                        rsi_1h_raw = (
                            calc_rsi(closes_1h[:-1] + [current_price], RSI_PERIOD)
                            if current_price > 0
                            else None
                        )
                        # 计算RSI趋势：用前一个周期的RSI比较
                        rsi_1h_previous = (
                            calc_rsi(closes_1h[:-2], RSI_PERIOD)
                            if len(closes_1h) >= 3 and current_price > 0
                            else None
                        )
                        rsi_1h_trend = (
                            (rsi_1h_raw - rsi_1h_previous)
                            if rsi_1h_raw is not None and rsi_1h_previous is not None
                            else None
                        )
                        result["rsi_1h"] = (
                            round(rsi_1h_raw, 1) if rsi_1h_raw is not None else None
                        )
                        result["rsi_1h_trend"] = (
                            round(rsi_1h_trend, 2) if rsi_1h_trend is not None else None
                        )
                        if rsi_1h_raw is not None:
                            base_bonus = 0
                            if 30 <= rsi_1h_raw < 40:
                                base_bonus = 5
                            elif 40 <= rsi_1h_raw < 50:
                                base_bonus = 4
                            elif rsi_1h_raw < 30:
                                base_bonus = -3
                            elif rsi_1h_raw > 50:
                                base_bonus = -3
                            # 根据RSI趋势调整加分
                            # RSI正在回升（趋势>0）：加强加分
                            # RSI仍在恶化（趋势<0）：减少加分或不变
                            if base_bonus > 0 and rsi_1h_trend is not None:
                                if rsi_1h_trend > 2:
                                    # RSI快速回升：额外+2分
                                    final_bonus = base_bonus + 2
                                elif rsi_1h_trend > 0:
                                    # RSI缓慢回升：保持原分
                                    final_bonus = base_bonus
                                else:
                                    # RSI仍在恶化：减少加分
                                    final_bonus = max(0, base_bonus - 2)
                            elif base_bonus < 0 and rsi_1h_trend is not None:
                                # RSI在超卖区继续恶化：加重扣分
                                if rsi_1h_trend < -2:
                                    final_bonus = base_bonus - 2
                                else:
                                    final_bonus = base_bonus
                            else:
                                final_bonus = base_bonus
                            result["oversold_score"] += final_bonus
                            result["rsi_1h_bonus"] = final_bonus
                            # 恐慌抛售检测到时，RSI低位但仍在恶化，额外扣分
                            if panic_selling_detected and rsi_1h_raw < 35:
                                result["oversold_score"] -= 3
                                result["panic_penalty"] = 3
                            else:
                                result["panic_penalty"] = 0
                    else:
                        result["rsi_1h"] = None
                        result["rsi_1h_trend"] = None
                        result["rsi_1h_bonus"] = 0
                        result["panic_penalty"] = 0
                except Exception:
                    result["rsi_1h"] = None
                    result["rsi_1h_trend"] = None
                    result["rsi_1h_bonus"] = 0
                    result["panic_penalty"] = 0

                # ── 周期进度检测（时间戳法，更精准）────────────────────
                # 改用时间戳推算，避免1h K线未关闭导致的计数误差
                if interval == "4h":
                    try:
                        interval_ms = 4 * 3600 * 1000
                        now_ms = int(time.time() * 1000)
                        last_closed_open = klines[-1][0]
                        # 用时间戳计算进度：更精准，不依赖K线关闭
                        elapsed_ratio = min(
                            1.0, (now_ms - last_closed_open) / interval_ms
                        )
                        result["elapsed_ratio"] = round(elapsed_ratio, 2)
                        result["hour_candles_in_4h"] = int(elapsed_ratio * 4)
                        # 4h周期即将收盘（elapsed > 0.75）时，如果1h RSI仍在低位(30-40)，
                        # 说明4h收盘后可能继续支撑，做多信号更强
                        # momentum_penalty > 0 说明价格在4h收盘后继续跌，此时不应该加分
                        rsi_1h_val = result.get("rsi_1h", 0) or 0
                        if (
                            elapsed_ratio > 0.75
                            and 30 <= rsi_1h_val < 40
                            and momentum_penalty == 0
                        ):
                            result["oversold_score"] += 3
                            result["closing_period_bonus"] = 3
                    except Exception:
                        result["hour_candles_in_4h"] = 0
                        result["elapsed_ratio"] = 1.0

                confirmation = {
                    "passed": True,
                    "strong": True,
                    "signal_count": 0,
                    "momentum_count": 0,
                    "reasons": [],
                    "reason": "非4h模式不要求右侧确认",
                }
                if interval == "4h":
                    confirmation = self._build_4h_confirmation(
                        current_price=current_price,
                        lows=lows,
                        klines_1h=klines_1h,
                        rsi_1h=result.get("rsi_1h"),
                        rsi_1h_trend=result.get("rsi_1h_trend"),
                        support_distance_pct=support_distance_pct,
                        panic_selling_detected=panic_selling_detected,
                    )
                    if confirmation["passed"]:
                        result["oversold_score"] += ST_W_CONFIRMATION
                        result["signal_details"] = (
                            f"{result['signal_details']} | {confirmation['reason']}"
                        )
                    else:
                        log.info(
                            "[%s] %s 4h 右侧确认不足: %s",
                            self.name,
                            symbol,
                            confirmation["reason"],
                        )
                        continue
                    if cautious_mode and not confirmation.get("strong"):
                        log.info(
                            "[%s] %s 谨慎模式要求强右侧确认: %s",
                            self.name,
                            symbol,
                            confirmation["reason"],
                        )
                        continue

                result["oversold_score"] = min(100, round(result["oversold_score"]))

                if result["oversold_score"] < effective_min_score:
                    continue

                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                atr_filter_val = calc_atr(highs, lows, closes, ATR_PERIOD_FILTER)
                last_close = closes[-1]
                atr_pct = (
                    round(atr_val / last_close * 100, 2)
                    if (atr_val and last_close > 0)
                    else None
                )
                atr_filter_pct = (
                    round(atr_filter_val / last_close * 100, 2)
                    if (atr_filter_val and last_close > 0)
                    else None
                )
                volatility_action = "normal"
                if interval == "4h" and atr_filter_pct is not None:
                    if atr_filter_pct > 7.0:
                        log.info(
                            "[%s] %s ATR %.2f%% > 7%%，跳过",
                            self.name,
                            symbol,
                            atr_filter_pct,
                        )
                        continue
                    if atr_filter_pct > 5.0 and not confirmation.get("strong"):
                        log.info(
                            "[%s] %s ATR %.2f%% > 5%% 且右侧确认不强，跳过",
                            self.name,
                            symbol,
                            atr_filter_pct,
                        )
                        continue
                    if atr_filter_pct > 5.0:
                        volatility_action = "allow_strong_confirmation"
                    elif atr_filter_pct > 4.0:
                        volatility_action = "half_size"

                # P2-1: 季度交割周检测
                now_utc = datetime.now(timezone.utc)
                is_delivery_week = _is_delivery_week(now_utc)

                scored.append(
                    {
                        "symbol": symbol,
                        "close": last_close,
                        "current_price": current_price,
                        "quote_volume_24h": item.get("quoteVolume", 0),
                        "price_change_pct": item.get("priceChangePercent", 0),
                        # ── 实时价格分析字段 ─────────────────────────────
                        "price_change_since_close_pct": round(
                            price_change_since_close_pct, 2
                        ),
                        "momentum_penalty": result.get("momentum_penalty", 0),
                        "rsi_1h": result.get("rsi_1h"),
                        "rsi_1h_trend": result.get("rsi_1h_trend"),
                        "rsi_1h_bonus": result.get("rsi_1h_bonus", 0),
                        "hour_candles_in_4h": result.get("hour_candles_in_4h", 0),
                        "elapsed_ratio": result.get("elapsed_ratio", 1.0),
                        "closing_period_bonus": result.get("closing_period_bonus", 0),
                        # ── 支撑位和恐慌抛售字段 ────────────────────────
                        "support_distance_pct": (
                            round(support_distance_pct, 2)
                            if support_distance_pct is not None
                            else None
                        ),
                        "is_near_support": is_near_support,
                        "panic_selling_detected": panic_selling_detected,
                        "panic_penalty": result.get("panic_penalty", 0),
                        "oversold_confirmation": confirmation,
                        "volatility_action": volatility_action,
                        "market_regime_status": market_regime.get("status"),
                        "market_score_adjustment": score_adjustment,
                        "effective_min_oversold_score": effective_min_score,
                        # ── 原有字段 ───────────────────────────────────
                        "rsi": result["rsi"],
                        "bias_20": result["bias_20"],
                        "consecutive_down": result["consecutive_down"],
                        "drop_pct": result["drop_pct"],
                        "below_boll_lower": result["below_boll_lower"],
                        "kdj_j": result["kdj_j"],
                        "macd_divergence": result["macd_divergence"],
                        "volume_surge": result["volume_surge"],
                        "funding_rate": result["funding_rate"],
                        "oi_change_pct": None,
                        "distance_from_high_pct": result["distance_from_high_pct"],
                        "oversold_score": result["oversold_score"],
                        "signal_details": result["signal_details"],
                        "atr_pct": atr_pct,
                        "atr_filter_pct": atr_filter_pct,
                        "signal_direction": "long",
                        "strategy_tag": self.name,
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                        "delivery_week": is_delivery_week,  # P2-1: 季度交割周标记
                    }
                )
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["oversold_score"], reverse=True)
        candidates = self._deduplicate(scored, returns_map, max_cands)

        log.info(
            "[%s] 完成: pool=%d, scored=%d, output=%d",
            self.name,
            len(pool),
            len(scored),
            len(candidates),
        )

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
            "market_regime": market_regime,
            "filter_summary": {
                "total_tickers": total_count,
                "after_base_filter": len(pool),
                "after_oversold_filter": len(scored),
                "output_count": len(candidates),
                "min_oversold_score": min_score,
                "effective_min_oversold_score": effective_min_score,
            },
        }


# ══════════════════════════════════════════════════════════
# 短期超跌 Skill（4h）
# ══════════════════════════════════════════════════════════


class ShortTermOversoldSkill(_CryptoOversoldBase):
    """短期超跌反弹筛选（4h K 线）。

    捕捉恐慌抛售后的 V 型反转，适合日内/隔日超短线。
    核心信号：RSI 极端超卖 + 资金费率极端负值 + 底部放量。
    """

    def __init__(
        self,
        state_store,
        input_schema,
        output_schema,
        client,
        risk_controller: Optional["RiskController"] = None,
    ) -> None:
        super().__init__(
            state_store, input_schema, output_schema, client, risk_controller
        )
        self.name = "crypto_oversold_4h"

    def run(self, input_data: dict) -> dict:
        # 4h 门槛：40（回测优化最优值，综合评分最高）
        if "min_oversold_score" not in input_data:
            input_data = {**input_data, "min_oversold_score": 40}
        return self._run_scan(
            input_data,
            interval=ST_INTERVAL,
            min_klines=ST_MIN_KLINES,
            rsi_thresh=ST_RSI_THRESHOLD,
            bias_thresh=ST_BIAS_THRESHOLD,
            consec_thresh=ST_CONSECUTIVE_DOWN,
            drop_thresh=ST_DROP_PCT,
            drop_lookback=ST_DROP_LOOKBACK,
            dd_thresh=ST_DRAWDOWN_THRESHOLD,
            dd_lookback=ST_DRAWDOWN_LOOKBACK,
            vol_thresh=ST_VOL_SURGE_THRESHOLD,
            weights={
                "rsi": ST_W_RSI,
                "bias": ST_W_BIAS,
                "drop": ST_W_DROP,
                "boll": ST_W_BOLL,
                "macd_div": ST_W_MACD_DIV,
                "kdj": ST_W_KDJ,
                "funding": ST_W_FUNDING,
                "drawdown": ST_W_DRAWDOWN,
                "volume": ST_W_VOLUME,
            },
        )


# ══════════════════════════════════════════════════════════
# 长期超跌 Skill（1d）
# ══════════════════════════════════════════════════════════


class LongTermOversoldSkill(_CryptoOversoldBase):
    """长期超跌反弹筛选（1d K 线）。

    捕捉中期超跌后的均值回归，适合波段交易（3天~2周）。
    核心信号：日线 BIAS 深度偏离 + MACD 底背离 + 距高点深度回撤。
    """

    def __init__(
        self,
        state_store,
        input_schema,
        output_schema,
        client,
        risk_controller: Optional["RiskController"] = None,
    ) -> None:
        super().__init__(
            state_store, input_schema, output_schema, client, risk_controller
        )
        self.name = "crypto_oversold_1d"

    def run(self, input_data: dict) -> dict:
        # 1d 门槛：50（日线持仓周期长，门槛应高于4h，减少错误信号）
        if "min_oversold_score" not in input_data:
            input_data = {**input_data, "min_oversold_score": 50}
        return self._run_scan(
            input_data,
            interval=LT_INTERVAL,
            min_klines=LT_MIN_KLINES,
            rsi_thresh=LT_RSI_THRESHOLD,
            bias_thresh=LT_BIAS_THRESHOLD,
            consec_thresh=LT_CONSECUTIVE_DOWN,
            drop_thresh=LT_DROP_PCT,
            drop_lookback=LT_DROP_LOOKBACK,
            dd_thresh=LT_DRAWDOWN_THRESHOLD,
            dd_lookback=LT_DRAWDOWN_LOOKBACK,
            vol_thresh=LT_VOL_SURGE_THRESHOLD,
            weights={
                "rsi": LT_W_RSI,
                "bias": LT_W_BIAS,
                "drop": LT_W_DROP,
                "boll": LT_W_BOLL,
                "macd_div": LT_W_MACD_DIV,
                "kdj": LT_W_KDJ,
                "funding": LT_W_FUNDING,
                "drawdown": LT_W_DRAWDOWN,
                "volume": LT_W_VOLUME,
                "macd_lookback": 60,  # 日线级别底背离需要更大窗口
            },
        )


# 向后兼容：保留原名指向短期版本
CryptoOversoldSkill = ShortTermOversoldSkill


# ══════════════════════════════════════════════════════════
# 超跌评分函数（纯函数，短期/长期共用）
# ══════════════════════════════════════════════════════════


def calc_oversold_score(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    volumes: List[float],
    rsi_thresh: float,
    bias_thresh: float,
    consec_down_thresh: int,
    drop_pct_thresh: float,
    drop_lookback: int,
    dd_thresh: float,
    dd_lookback: int,
    vol_thresh: float,
    funding_rate: Optional[float],
    weights: dict,
) -> dict:
    """计算超跌综合评分（满分 100）。

    通过 weights 字典控制各维度权重，短期/长期使用不同权重配置。
    """
    signals = []
    score = 0.0
    w = weights

    # ── 1. RSI 超卖 ──
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    if rsi_val is not None and rsi_val < rsi_thresh:
        score += w["rsi"] * min(1.0, (rsi_thresh - rsi_val) / rsi_thresh)
        signals.append(f"RSI={rsi_val:.1f}<{rsi_thresh}")

    # ── 2. 乖离率 BIAS(20) ──
    bias_20 = _calc_bias(closes, BOLL_PERIOD)
    if bias_20 is not None and bias_20 < bias_thresh:
        score += w["bias"] * min(1.0, (bias_thresh - bias_20) / abs(bias_thresh))
        signals.append(f"BIAS={bias_20:.1f}%<{bias_thresh}%")

    # ── 3. 连续杀跌 + 累计跌幅 ──
    consec = _calc_consecutive_down(closes)
    drop_pct = _calc_drop_pct(closes, drop_lookback)
    drop_score = 0.0
    if consec >= consec_down_thresh:
        drop_score += w["drop"] * 0.5 * min(1.0, consec / (consec_down_thresh * 2))
        signals.append(f"连跌{consec}根≥{consec_down_thresh}")
    if drop_pct is not None and drop_pct < drop_pct_thresh:
        drop_score += (
            w["drop"]
            * 0.5
            * min(1.0, (drop_pct_thresh - drop_pct) / abs(drop_pct_thresh))
        )
        signals.append(f"近{drop_lookback}根跌{drop_pct:.1f}%")
    score += min(drop_score, float(w["drop"]))

    # ── 4. 布林带下轨突破 ──
    below_boll = _check_below_boll_lower(closes)
    if below_boll:
        score += w["boll"]
        signals.append("跌破BOLL下轨")

    # ── 5. MACD 底背离 ──
    macd_lookback = w.get("macd_lookback", 30)
    macd_div = _check_macd_divergence(closes, lookback=macd_lookback)
    if macd_div:
        score += w["macd_div"]
        signals.append("MACD底背离")

    # ── 6. KDJ J值极值 ──
    kdj_j = _calc_kdj_j(closes, highs, lows)
    if kdj_j is not None and kdj_j < 0:
        score += w["kdj"] * min(1.0, abs(kdj_j) / 30.0)
        signals.append(f"KDJ_J={kdj_j:.1f}<0")

    # ── 7. 资金费率极端负值 ──
    fr_display = None
    if funding_rate is not None:
        fr_display = round(funding_rate * 100, 4)
        if funding_rate < FUNDING_RATE_EXTREME:
            if funding_rate < FUNDING_RATE_VERY_EXTREME:
                score += w["funding"]
                signals.append(f"费率={fr_display:.3f}%极端")
            else:
                score += w["funding"] * min(
                    1.0,
                    (FUNDING_RATE_EXTREME - funding_rate)
                    / (FUNDING_RATE_EXTREME - FUNDING_RATE_VERY_EXTREME),
                )
                signals.append(f"费率={fr_display:.3f}%负")

    # ── 8. 距近期高点回撤 ──
    drawdown = _calc_distance_from_high(closes, dd_lookback)
    if drawdown is not None and drawdown < dd_thresh:
        score += w["drawdown"] * min(1.0, (dd_thresh - drawdown) / abs(dd_thresh))
        signals.append(f"距高点{drawdown:.1f}%")

    # ── 加分项: 底部放量 ──
    vol_surge = _calc_volume_surge_bottom(volumes)
    if vol_surge is not None and vol_surge >= vol_thresh:
        score += w["volume"]
        signals.append(f"放量{vol_surge:.1f}x")

    return {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "bias_20": round(bias_20, 2) if bias_20 is not None else None,
        "consecutive_down": consec,
        "drop_pct": round(drop_pct, 2) if drop_pct is not None else None,
        "below_boll_lower": below_boll,
        "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
        "macd_divergence": macd_div,
        "volume_surge": round(vol_surge, 2) if vol_surge is not None else None,
        "funding_rate": fr_display,
        "distance_from_high_pct": round(drawdown, 2) if drawdown is not None else None,
        "oversold_score": round(score),
        "signal_details": " | ".join(signals) if signals else "无超跌信号",
    }


# ══════════════════════════════════════════════════════════
# 纯函数指标库
# ══════════════════════════════════════════════════════════


def _calc_bias(closes: List[float], period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None
    ma = sum(closes[-period:]) / period
    if ma <= 0:
        return None
    return (closes[-1] - ma) / ma * 100


def _calc_consecutive_down(closes: List[float]) -> int:
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _calc_drop_pct(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    base = closes[-(lookback + 1)]
    if base <= 0:
        return None
    return (closes[-1] - base) / base * 100


def _check_below_boll_lower(closes: List[float]) -> bool:
    if len(closes) < BOLL_PERIOD:
        return False
    window = closes[-BOLL_PERIOD:]
    ma = sum(window) / BOLL_PERIOD
    variance = sum((x - ma) ** 2 for x in window) / BOLL_PERIOD
    std = math.sqrt(variance)
    return closes[-1] < ma - BOLL_STD_MULT * std


def _calc_kdj_j(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    period: int = KDJ_PERIOD,
    m1: int = KDJ_M1,
    m2: int = KDJ_M2,
) -> Optional[float]:
    if len(closes) < period + m1 + m2:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1 : i + 1])
        ll = min(lows[i - period + 1 : i + 1])
        rsvs.append(50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100)
    if not rsvs:
        return None
    k_val = d_val = rsvs[0]
    for rsv in rsvs[1:]:
        k_val = (k_val * (m1 - 1) + rsv) / m1
        d_val = (d_val * (m2 - 1) + k_val) / m2
    return 3 * k_val - 2 * d_val


def _check_macd_divergence(closes: List[float], lookback: int = 30) -> bool:
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
    h1 = calc_macd(closes[: base_idx + prev_min_idx + 1]).get("histogram")
    h2 = calc_macd(closes[: base_idx + min_idx + 1]).get("histogram")
    return h1 is not None and h2 is not None and h2 > h1


def _calc_distance_from_high(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < 2:
        return None
    window = closes[-min(lookback, len(closes)) :]
    high = max(window)
    return (closes[-1] - high) / high * 100 if high > 0 else None


def _calc_volume_surge_bottom(volumes: List[float], long_w: int = 5) -> Optional[float]:
    if len(volumes) < long_w + 1:
        return None
    avg = sum(volumes[-(long_w + 1) : -1]) / long_w
    return volumes[-1] / avg if avg > 0 else None


# ══════════════════════════════════════════════════════════
# P2-1: 季度交割周检测（与超买策略共用逻辑）
# ══════════════════════════════════════════════════════════

DELIVERY_MONTHS = {3, 6, 9, 12}
DELIVERY_LOOKBACK_DAYS = 7


def _is_delivery_week(dt: datetime) -> bool:
    """检测是否处于季度交割周。

    季度交割日 = 季度最后一个周五
    交割周 = 交割日前后 7 天
    """
    if dt.month not in DELIVERY_MONTHS:
        return False

    _, last_day = calendar.monthrange(dt.year, dt.month)
    last_friday = None
    for day in range(last_day, 0, -1):
        check_date = datetime(dt.year, dt.month, day)
        if check_date.weekday() == calendar.FRIDAY:
            last_friday = day
            break

    if last_friday is None:
        return False

    delivery_date = datetime(dt.year, dt.month, last_friday)
    days_to_delivery = (delivery_date - dt).days
    return -DELIVERY_LOOKBACK_DAYS <= days_to_delivery <= DELIVERY_LOOKBACK_DAYS
