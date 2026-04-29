"""
加密货币合约超跌反弹筛选 Skill（双模式）

两种独立的超跌分析模式，适用于不同交易策略：

## 短期超跌（ShortTermOversoldSkill）— 4h K 线
  适用场景：日内/隔日超短线反弹，捕捉恐慌抛售后的 V 型反转
  K 线周期：4h（100 根 ≈ 17 天）
  核心信号：RSI 极端超卖、资金费率极端负值、短期暴跌、底部放量
  持仓周期：4h ~ 2 天
  阈值特点：RSI < 20、BIAS < -10%、连跌 ≥ 5 根 4h、累跌 < -15%

## 长期超跌（LongTermOversoldSkill）— 1d K 线
  适用场景：波段反弹，捕捉中期超跌后的均值回归
  K 线周期：1d（100 根 ≈ 100 天）
  核心信号：日线级别 RSI/BIAS 超跌、距高点深度回撤、MACD 底背离
  持仓周期：3 天 ~ 2 周
  阈值特点：RSI < 30、BIAS < -15%、连跌 ≥ 3 天、累跌 < -30%、距高点 > -40%

两者共享：基础过滤逻辑、资金费率信号、相关性去重、纯函数指标库
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
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 资金费率阈值（两种模式共享）
FUNDING_RATE_EXTREME = -0.001           # -0.1%
FUNDING_RATE_VERY_EXTREME = -0.003      # -0.3%

# ══════════════════════════════════════════════════════════
# 短期超跌参数（4h K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "4h"
ST_MIN_KLINES = 50
ST_RSI_THRESHOLD = 20.0          # 4h 级别 RSI < 20 才算极端超卖
ST_BIAS_THRESHOLD = -10.0        # 4h 乖离率 < -10%
ST_CONSECUTIVE_DOWN = 5          # 连续下跌 ≥ 5 根 4h（≈ 20 小时）
ST_DROP_PCT = -15.0              # 近 N 根累计跌幅 < -15%
ST_DROP_LOOKBACK = 18            # 回看 18 根 4h = 3 天
ST_DRAWDOWN_THRESHOLD = -20.0    # 距近期高点回撤 > 20%（短期看 5 天内）
ST_DRAWDOWN_LOOKBACK = 30        # 回看 30 根 4h = 5 天
ST_VOL_SURGE_THRESHOLD = 2.0     # 底部放量 ≥ 2x

# 短期评分权重（满分 100）— 侧重即时超卖信号和资金费率
ST_W_RSI = 18           # RSI 极端超卖（短期核心）
ST_W_BIAS = 12          # 乖离率
ST_W_DROP = 10          # 连续杀跌
ST_W_BOLL = 8           # 布林带
ST_W_MACD_DIV = 5       # MACD 背离（4h 级别背离可靠性一般）
ST_W_KDJ = 7            # KDJ
ST_W_FUNDING = 20       # 资金费率（短期反弹最强信号，高权重）
ST_W_DRAWDOWN = 10      # 距高点回撤
ST_W_VOLUME = 10        # 底部放量（恐慌盘涌出，短期反弹前兆）

# ══════════════════════════════════════════════════════════
# 长期超跌参数（1d K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1d"
LT_MIN_KLINES = 60               # 最低 60 天数据（新币也能参与）
LT_RSI_THRESHOLD = 30.0          # 日线 RSI < 30
LT_BIAS_THRESHOLD = -15.0        # 日线 20 日乖离率 < -15%
LT_CONSECUTIVE_DOWN = 3          # 连续下跌 ≥ 3 天
LT_DROP_PCT = -30.0              # 近 N 日累计跌幅 < -30%
LT_DROP_LOOKBACK = 14            # 回看 14 天
LT_DRAWDOWN_THRESHOLD = -40.0    # 距近期高点回撤 > 40%（中期深度回调）
LT_DRAWDOWN_LOOKBACK = 180       # 回看 180 天（半年），覆盖完整中期下跌周期
LT_VOL_SURGE_THRESHOLD = 1.5     # 日线放量 ≥ 1.5x

# 长期评分权重（满分 100）— 侧重趋势超跌和背离信号
LT_W_RSI = 12           # RSI
LT_W_BIAS = 15          # 乖离率（日线 BIAS 更可靠）
LT_W_DROP = 12          # 连续杀跌 + 累计跌幅
LT_W_BOLL = 10          # 布林带
LT_W_MACD_DIV = 15      # MACD 底背离（日线级别背离可靠性高，高权重）
LT_W_KDJ = 8            # KDJ
LT_W_FUNDING = 8        # 资金费率（长期看权重降低）
LT_W_DRAWDOWN = 15      # 距高点回撤（长期核心指标）
LT_W_VOLUME = 5         # 底部放量

DEFAULT_MIN_QUOTE_VOLUME = 10_000_000
DEFAULT_MIN_OVERSOLD_SCORE = 25
DEFAULT_MAX_CANDIDATES = 20
DEFAULT_MAX_SPREAD_PCT = 0.25
DEFAULT_MAX_ABS_FUNDING_RATE = 0.01

MARKET_REGIME_SYMBOL = "BTCUSDT"
MARKET_REGIME_INTERVAL = "4h"
MARKET_REGIME_KLINE_LIMIT = 80
MARKET_REGIME_PANIC_DROP_PCT = -8.0
MARKET_REGIME_DOWNTREND_LOOKBACK = 12
SYMBOL_TREND_INTERVAL = "6h"
SYMBOL_TREND_KLINE_LIMIT = 80
SYMBOL_TREND_RECENT_DROP_PCT = -10.0
SYMBOL_TREND_RECENT_LOOKBACK = 8


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════

class _CryptoOversoldBase(BaseSkill):
    """超跌筛选共享基类，封装基础过滤、资金费率获取、去重等通用逻辑。"""

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
            if base in exclude_bases:
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
            if not any(calc_correlation(rets, sr) > CORRELATION_THRESHOLD for sr in selected_returns):
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

    def _symbol_trend_filter_reason(self, symbol: str, input_data: dict) -> Optional[str]:
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

    def _get_market_regime(self, input_data: dict) -> dict:
        """
        判断当前市场是否适合做超跌反弹。

        超跌均值回归只适合非单边瀑布环境。若 BTCUSDT 处于 4h 级别
        明确下跌趋势或短期暴跌，本轮直接阻断，避免系统性接刀。
        """
        if input_data.get("ignore_market_regime"):
            return {"status": "enabled", "reason": "ignore_market_regime=true"}

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
        ema_fast_series = calc_ema(closes, EMA_FAST)
        ema_slow_series = calc_ema(closes, EMA_SLOW)
        ema_fast = ema_fast_series[-1] if ema_fast_series else None
        ema_slow = ema_slow_series[-1] if ema_slow_series else None
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
        downtrend = (
            ema_fast is not None
            and ema_slow is not None
            and last_close < ema_fast < ema_slow
        )
        panic_drop = recent_return_pct <= panic_drop_pct
        if downtrend or panic_drop:
            reasons = []
            if downtrend:
                reasons.append("BTC 4h close<EMA_FAST<EMA_SLOW")
            if panic_drop:
                reasons.append(f"BTC recent_return={recent_return_pct:.2f}%")
            return {
                "status": "blocked",
                "reason": "; ".join(reasons),
                "symbol": symbol,
                "recent_return_pct": round(recent_return_pct, 4),
            }

        return {
            "status": "enabled",
            "reason": "market_regime_ok",
            "symbol": symbol,
            "recent_return_pct": round(recent_return_pct, 4),
        }

    def _run_scan(
        self, input_data: dict, interval: str, min_klines: int,
        rsi_thresh: float, bias_thresh: float, consec_thresh: int,
        drop_thresh: float, drop_lookback: int, dd_thresh: float,
        dd_lookback: int, vol_thresh: float, weights: dict,
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
        market_regime = self._get_market_regime(input_data)
        if market_regime.get("status") == "blocked":
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
                },
                "market_regime": market_regime,
            }

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
                    log.info("[%s] %s 交易对象质量过滤: %s", self.name, symbol, quality_reason)
                    continue

                trend_reason = self._symbol_trend_filter_reason(symbol, input_data)
                if trend_reason:
                    log.info("[%s] %s 趋势过滤: %s", self.name, symbol, trend_reason)
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
                result = calc_oversold_score(
                    closes, highs, lows, volumes,
                    rsi_thresh, bias_thresh, consec_thresh,
                    drop_thresh, drop_lookback,
                    dd_thresh, dd_lookback, vol_thresh,
                    fr, weights,
                )

                if result["oversold_score"] < min_score and not target_symbols:
                    continue

                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                last_close = closes[-1]
                atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None

                scored.append({
                    "symbol": symbol,
                    "close": last_close,
                    "quote_volume_24h": item.get("quoteVolume", 0),
                    "price_change_pct": item.get("priceChangePercent", 0),
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
                    "strategy_tag": self.name,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["oversold_score"], reverse=True)
        candidates = self._deduplicate(scored, returns_map, max_cands)

        log.info("[%s] 完成: pool=%d, scored=%d, output=%d",
                 self.name, len(pool), len(scored), len(candidates))

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

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_oversold_short"

    def run(self, input_data: dict) -> dict:
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
                "rsi": ST_W_RSI, "bias": ST_W_BIAS, "drop": ST_W_DROP,
                "boll": ST_W_BOLL, "macd_div": ST_W_MACD_DIV, "kdj": ST_W_KDJ,
                "funding": ST_W_FUNDING, "drawdown": ST_W_DRAWDOWN,
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

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_oversold_long"

    def run(self, input_data: dict) -> dict:
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
                "rsi": LT_W_RSI, "bias": LT_W_BIAS, "drop": LT_W_DROP,
                "boll": LT_W_BOLL, "macd_div": LT_W_MACD_DIV, "kdj": LT_W_KDJ,
                "funding": LT_W_FUNDING, "drawdown": LT_W_DRAWDOWN,
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
        drop_score += w["drop"] * 0.5 * min(1.0, (drop_pct_thresh - drop_pct) / abs(drop_pct_thresh))
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
                score += w["funding"] * min(1.0,
                    (FUNDING_RATE_EXTREME - funding_rate) /
                    (FUNDING_RATE_EXTREME - FUNDING_RATE_VERY_EXTREME))
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
    closes: List[float], highs: List[float], lows: List[float],
    period: int = KDJ_PERIOD, m1: int = KDJ_M1, m2: int = KDJ_M2,
) -> Optional[float]:
    if len(closes) < period + m1 + m2:
        return None
    rsvs = []
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1: i + 1])
        ll = min(lows[i - period + 1: i + 1])
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
    h1 = calc_macd(closes[:base_idx + prev_min_idx + 1]).get("histogram")
    h2 = calc_macd(closes[:base_idx + min_idx + 1]).get("histogram")
    return h1 is not None and h2 is not None and h2 > h1


def _calc_distance_from_high(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < 2:
        return None
    window = closes[-min(lookback, len(closes)):]
    high = max(window)
    return (closes[-1] - high) / high * 100 if high > 0 else None


def _calc_volume_surge_bottom(volumes: List[float], long_w: int = 5) -> Optional[float]:
    if len(volumes) < long_w + 1:
        return None
    avg = sum(volumes[-(long_w + 1):-1]) / long_w
    return volumes[-1] / avg if avg > 0 else None
