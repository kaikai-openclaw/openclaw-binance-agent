"""
加密货币合约超买做空筛选 Skill（双模式）

筛选短期涨幅过大、技术面和衍生品数据均显示多头过度拥挤的币种，寻找高胜率做空机会。

## 设计哲学

做空与做多的风险不对称：做空亏损理论上无限，做多最多归零。
因此本 Skill 的筛选标准比做多类 Skill 更严格：
  - 要求多个维度同时触发（单一信号不足以做空）
  - 内置轧空风险排查（低流动性 + 高 OI = 轧空陷阱）
  - 资金费率极端正值是币圈做空的最强信号（多头付费维持仓位 = 拥挤到极致）

## 短期超买（4h K 线）— 日内/隔日做空
  适用场景：4h 级别急涨后的回调，捕捉 FOMO 情绪见顶
  K 线周期：4h（100 根 ≈ 17 天）
  核心信号：RSI 极端超买 + 资金费率极端正值 + 量价背离 + 布林带突破上轨
  持仓周期：4h ~ 2 天
  阈值特点：RSI > 80、BIAS > +12%、连涨 ≥ 5 根 4h、资金费率 > +0.1%

## 长期超买（1d K 线）— 波段做空
  适用场景：日线级别持续上涨后的趋势衰竭，捕捉中期顶部
  K 线周期：1d（100 根 ≈ 100 天）
  核心信号：MACD 顶背离 + 日线 BIAS 极端偏离 + OI 异常膨胀 + 距低点涨幅过大
  持仓周期：3 天 ~ 2 周
  阈值特点：RSI > 75、BIAS > +18%、MACD 顶背离、距低点涨幅 > +60%

十维度评分体系（满分 100）：

### 短期超买（4h）权重分配
  1. RSI 极端超买（15 分）— RSI > 80，越高越危险
  2. 资金费率极端正值（18 分）— 币圈做空最强信号，多头拥挤到极致
  3. BIAS 正向偏离（12 分）— 价格严重偏离均线
  4. 量价背离（12 分）— 价格创新高但量能萎缩 = 上涨动能衰竭
  5. 布林带突破上轨（8 分）— 价格偏离统计极值
  6. 连续暴涨（10 分）— 连涨根数 + 累计涨幅
  7. KDJ 高位死叉（7 分）— 超买区死叉确认
  8. MACD 顶背离（5 分）— 4h 级别可靠性一般
  9. 长上影线（5 分）— 上方有强阻力
  10. 轧空风险扣分（-8 分）— 低流动性 + 高 OI = 轧空陷阱，扣分

### 长期超买（1d）权重分配
  1. RSI 超买（10 分）— 日线 RSI > 75
  2. 资金费率极端正值（12 分）— 长期看权重降低
  3. BIAS 正向偏离（15 分）— 日线 BIAS 更可靠
  4. 量价背离（12 分）— 日线量价背离信号强
  5. 布林带突破上轨（8 分）— 日线级别
  6. 连续暴涨 + 距低点涨幅（12 分）— 中期涨幅过大
  7. KDJ 高位死叉（7 分）— 日线 KDJ
  8. MACD 顶背离（15 分）— 日线级别顶背离可靠性高
  9. 长上影线（5 分）— 日线长上影线
  10. 轧空风险扣分（-4 分）— 长期看轧空风险降低

数据源：BinancePublicClient（K 线 + 资金费率 + 持仓量）
"""

import logging
import math
import re
import calendar
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
    ATR_PERIOD,
    ATR_PERIOD_FILTER,
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 共享常量
# ══════════════════════════════════════════════════════════

BOLL_PERIOD = 20
BOLL_STD_MULT = 2.0
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 资金费率阈值（做空方向：极端正值 = 多头拥挤）
FUNDING_RATE_HIGH = 0.001  # +0.1%，偏高
FUNDING_RATE_EXTREME = 0.003  # +0.3%，极端（多头付费维持仓位）
FUNDING_RATE_VERY_EXTREME = 0.005  # +0.5%，罕见极端
FUNDING_RATE_MAX_FOR_SHORT = (
    0.003  # +0.3%，做空资金费率上限（超过则借币成本过高，直接排除）
)
FUNDING_RATE_MIN_FOR_SCORE = 0.0005  # +0.05%，做空资金费率下限（低于此值不加分）

# ══════════════════════════════════════════════════════════
# 短期超买参数（4h K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "4h"
ST_MIN_KLINES = 50
ST_RSI_THRESHOLD = 75.0  # 4h RSI > 75（从 80 降低，捕捉更多超买信号）
ST_BIAS_THRESHOLD = 12.0  # 4h 乖离率 > +12%
ST_CONSECUTIVE_UP = 5  # 连续上涨 ≥ 5 根 4h（≈ 20 小时）
ST_RALLY_PCT = 15.0  # 近 N 根累计涨幅 > +15%
ST_RALLY_LOOKBACK = 18  # 回看 18 根 4h = 3 天
ST_RISE_LOOKBACK = 30  # 距低点涨幅回看 30 根 4h = 5 天

# 短期评分权重（满分 100）
ST_W_RSI = 12  # RSI 极端超买
ST_W_FUNDING = 15  # 资金费率极端正值（避免单独抬高弱顶部）
ST_W_BIAS = 12  # 乖离率正向偏离
ST_W_VOL_DIV = 15  # 量价背离
ST_W_BOLL = 6  # 布林带突破上轨
ST_W_RALLY = 6  # 连续暴涨
ST_W_KDJ = 8  # KDJ 高位死叉
ST_W_MACD_DIV = 8  # MACD / RSI 顶背离
ST_W_SHADOW = 4  # 长上影线
ST_W_SQUEEZE_RISK = -8  # 轧空风险扣分

# ══════════════════════════════════════════════════════════
# 超短期超买参数（1h K 线）
# ══════════════════════════════════════════════════════════

H1_INTERVAL = "1h"
H1_MIN_KLINES = 60
H1_RSI_THRESHOLD = 75.0  # 1h RSI > 75（从 80 降低，与 4h 对齐）
H1_BIAS_THRESHOLD = 10.0  # 1h 乖离率 > +10%
H1_CONSECUTIVE_UP = 7  # 连续上涨 ≥ 7 根 1h = 7 小时（从 10 放宽）
H1_RALLY_PCT = 20.0  # 近 N 根累计涨幅 > +20%
H1_RALLY_LOOKBACK = 24  # 回看 24 根 1h = 1 天
H1_RISE_LOOKBACK = 72  # 距低点涨幅回看 72 根 1h = 3 天

# 超短期评分权重 — 提高核心信号，轧空惩罚与 4h 对齐
H1_W_RSI = 20  # RSI（核心）
H1_W_FUNDING = 22  # 资金费率（做空最强信号）
H1_W_BIAS = 12  # 乖离率
H1_W_VOL_DIV = 15  # 量价背离（顶部缩量是关键确认）
H1_W_BOLL = 6  # 布林带
H1_W_RALLY = 6  # 连续暴涨
H1_W_KDJ = 5  # KDJ（1h 噪音大）
H1_W_MACD_DIV = 3  # MACD 顶背离（1h 可靠性很低）
H1_W_SHADOW = 3  # 长上影线
H1_W_SQUEEZE_RISK = -8  # 轧空风险扣分（从 -15 放宽，与 4h 对齐）

# ══════════════════════════════════════════════════════════
# 长期超买参数（1d K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1d"
LT_MIN_KLINES = 60
LT_RSI_THRESHOLD = 75.0  # 日线 RSI > 75
LT_BIAS_THRESHOLD = 18.0  # 日线 20 日乖离率 > +18%
LT_CONSECUTIVE_UP = 5  # 连续上涨 ≥ 5 天
LT_RALLY_PCT = 30.0  # 近 N 日累计涨幅 > +30%
LT_RALLY_LOOKBACK = 14  # 回看 14 天
LT_RISE_LOOKBACK = 60  # 距低点涨幅回看 60 天
LT_RISE_THRESHOLD = 60.0  # 距低点涨幅 > +60%

# 长期评分权重（满分 100）
LT_W_RSI = 10  # RSI
LT_W_FUNDING = 12  # 资金费率（长期看权重降低）
LT_W_BIAS = 15  # 乖离率（日线 BIAS 更可靠）
LT_W_VOL_DIV = 12  # 量价背离
LT_W_BOLL = 8  # 布林带
LT_W_RALLY = 12  # 连续暴涨 + 距低点涨幅
LT_W_KDJ = 7  # KDJ
LT_W_MACD_DIV = 15  # MACD 顶背离（日线级别可靠性高）
LT_W_SHADOW = 5  # 长上影线
LT_W_SQUEEZE_RISK = -4  # 轧空风险扣分（长期看风险降低）

DEFAULT_MIN_QUOTE_VOLUME = 20_000_000  # 从10M提高到20M，只做流动性好的主流币
DEFAULT_MIN_OVERBOUGHT_SCORE = 45  # 从35收紧至45，减少低质量信号扫描通过
DEFAULT_MAX_CANDIDATES = 10

# 轧空风险：成交额低于此值且 OI/成交额比过高 → 扣分
# 阈值设为 2000 万：只对真正低流动性小币种扣分，避免误伤中等市值超买候选
SQUEEZE_RISK_QV_THRESHOLD = 20_000_000
SQUEEZE_RISK_OI_RATIO = 0.6  # OI 价值 / 24h 成交额 > 60% = 极度拥挤（从 80% 收紧）

# 4h 收盘后盘中继续上涨的追空风险阈值。
# 软阈值：扣分并要求更强顶部确认；硬阈值：直接跳过，避免强动能中开空。
MOMENTUM_SOFT_CHASE_PCT_4H = 1.5
MOMENTUM_HARD_CHASE_PCT_4H = 3.0
MOMENTUM_EXTREME_CHASE_PCT_4H = 5.0
MOMENTUM_SOFT_ATR_MULT_4H = 0.25
MOMENTUM_HARD_ATR_MULT_4H = 0.50

# BTC 日线 MA200 牛市过滤：BTC 在牛市结构中做空统计上负EV
BTC_DAILY_MA200_SCORE_ADJUSTMENT = 8  # BTC日线close > MA200时，做空门槛提高8分


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════


class _CryptoOverboughtBase(BaseSkill):
    """超买做空筛选共享基类。"""

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

    def _build_oi_map(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取当前持仓量（USDT 价值）。"""
        oi_map: Dict[str, float] = {}
        for sym in symbols:
            try:
                data = self._client.get_open_interest(sym)
                if data:
                    oi_val = float(data.get("openInterest", 0))
                    # 需要乘以当前价格得到 USDT 价值，这里先存原始值
                    oi_map[sym] = oi_val
            except Exception:
                pass
        return oi_map

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
        result = [t for t in tickers if t.get("symbol", "") in normalized]
        # 确保 quoteVolume / priceChangePercent 为 float（Binance API 返回 str）
        for t in result:
            t["quoteVolume"] = float(t.get("quoteVolume", 0))
            t["priceChangePercent"] = float(t.get("priceChangePercent", 0))
        return result

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

    def _get_market_regime(self, input_data: dict) -> dict:
        """
        判断当前市场是否适合做空。

        P2-7 改造:
        - BTC 4h 短期暴跌 > 8%：阻断做空（已经在跌了，做空追跌风险高）
        - BTC 1d 级别强势上涨 > 8%：克制做空（顺势做空胜率低）
        - BTC 4h 短期暴涨 > 5%：放行（FOMO 见顶是做空最佳时机）

        BTC 暴涨时放行（FOMO 见顶是做空最佳时机）。
        """
        if input_data.get("ignore_market_regime"):
            return {"status": "enabled", "reason": "ignore_market_regime=true"}

        symbol = "BTCUSDT"
        try:
            klines_4h = self._fetch_klines(symbol, "4h", 80)
            klines_1d = self._fetch_klines(symbol, "1d", 250)
        except Exception as exc:
            log.warning("[%s] 市场状态获取失败: %s", self.name, exc)
            return {"status": "unknown", "reason": f"fetch_failed:{exc}"}

        if not klines_4h or len(klines_4h) < 60:
            return {"status": "unknown", "reason": "insufficient_market_klines_4h"}

        closes_4h = [float(k[4]) for k in klines_4h]
        last_close_4h = closes_4h[-1]

        # 4h 短期检查（最近 24h = 6 根 4h）
        lookback_4h = 6
        recent_return_4h_pct = (
            (last_close_4h - closes_4h[-lookback_4h]) / closes_4h[-lookback_4h] * 100
            if len(closes_4h) > lookback_4h and closes_4h[-lookback_4h] > 0
            else 0.0
        )
        if recent_return_4h_pct <= -8.0:
            return {
                "status": "blocked",
                "reason": f"BTC 短期暴跌 {recent_return_4h_pct:.2f}%，不宜追空",
                "symbol": symbol,
                "recent_return_pct": round(recent_return_4h_pct, 4),
            }

        ema5_series = calc_ema(closes_4h, 5)
        ema20_series = calc_ema(closes_4h, 20)
        ema5_4h = ema5_series[-1] if ema5_series else None
        ema20_4h = ema20_series[-1] if ema20_series else None
        if (
            self.name == "crypto_overbought_4h"
            and ema5_4h is not None
            and ema20_4h is not None
            and last_close_4h > ema20_4h
            and ema5_4h > ema20_4h
        ):
            return {
                "status": "cautious",
                "reason": (
                    f"BTC 4h 强趋势上涨 last={last_close_4h:.2f}>EMA20={ema20_4h:.2f}, "
                    f"EMA5={ema5_4h:.2f}>EMA20，做空需谨慎"
                ),
                "symbol": symbol,
                "recent_return_pct": round(recent_return_4h_pct, 4),
                "btc_above_ma200": False,
                "ma200": None,
                "score_adjustment": 15,
            }

        # 1d 级别趋势检查
        score_adjustment = 0
        btc_above_ma200 = False
        ma200_val = None

        if klines_1d and len(klines_1d) >= 5:
            closes_1d = [float(k[4]) for k in klines_1d]
            last_close_1d = closes_1d[-1]

            # BTC 日线 MA200 牛市过滤
            # BTC 在 MA200 以上 = 牛市结构，做空统计上负EV，提高信号质量门槛
            if len(closes_1d) >= 200:
                ma200_val = sum(closes_1d[-200:]) / 200.0
                if ma200_val > 0 and last_close_1d > ma200_val:
                    btc_above_ma200 = True
                    score_adjustment += BTC_DAILY_MA200_SCORE_ADJUSTMENT

            # P2-7: 5日涨幅检查（克制做空）
            lookback_1d = 5
            if len(closes_1d) >= lookback_1d:
                daily_return_5d_pct = (
                    (last_close_1d - closes_1d[-lookback_1d])
                    / closes_1d[-lookback_1d]
                    * 100
                    if closes_1d[-lookback_1d] > 0
                    else 0.0
                )
                if daily_return_5d_pct > 8.0:
                    score_adjustment += 10
                    return {
                        "status": "cautious",
                        "reason": (
                            f"BTC 日线强势上涨 {daily_return_5d_pct:.2f}%，做空需谨慎"
                        ),
                        "symbol": symbol,
                        "recent_return_pct": round(recent_return_4h_pct, 4),
                        "daily_return_5d_pct": round(daily_return_5d_pct, 4),
                        "btc_above_ma200": btc_above_ma200,
                        "ma200": round(ma200_val, 2) if ma200_val else None,
                        "score_adjustment": score_adjustment,
                    }

        return {
            "status": "enabled",
            "reason": "market_regime_ok",
            "symbol": symbol,
            "recent_return_pct": round(recent_return_4h_pct, 4),
            "btc_above_ma200": btc_above_ma200,
            "ma200": round(ma200_val, 2) if ma200_val else None,
            "score_adjustment": score_adjustment,
        }

    @staticmethod
    def _build_4h_confirmation(
        closes: List[float],
        highs: List[float],
        lows: List[float],
        klines_1h: List[list],
        current_price: float,
        rsi_1h: Optional[float],
        rsi_1h_trend: Optional[float],
        macd_divergence: bool,
        rsi_divergence: bool,
        volume_divergence: bool,
        kdj_dead_cross: bool,
        drawdown_from_high: Optional[float],
    ) -> dict:
        """构建 4h 超买做空顶部确认，至少一个动能信号。"""
        reasons: List[str] = []
        signal_count = 0
        momentum_count = 0
        structural_count = 0

        if (
            rsi_1h is not None
            and rsi_1h_trend is not None
            and 60 <= rsi_1h <= 85
            and rsi_1h_trend < 0
        ):
            signal_count += 1
            momentum_count += 1
            reasons.append("1h RSI高位下拐")

        if len(klines_1h) >= 3:
            recent_1h_low = min(float(k[3]) for k in klines_1h[-3:-1])
            if current_price < recent_1h_low:
                signal_count += 1
                momentum_count += 1
                reasons.append("跌回近2根1h低点")

        if len(closes) >= 20:
            boll_mid = sum(closes[-20:]) / 20.0
            if current_price < boll_mid:
                signal_count += 1
                momentum_count += 1
                reasons.append("跌回4h中轨下方")

        if macd_divergence:
            signal_count += 1
            structural_count += 1
            reasons.append("MACD顶背离")

        if rsi_divergence:
            signal_count += 1
            structural_count += 1
            reasons.append("RSI顶背离")

        if kdj_dead_cross:
            signal_count += 1
            structural_count += 1
            reasons.append("KDJ高位死叉")

        if volume_divergence:
            signal_count += 1
            structural_count += 1
            reasons.append("量价背离")

        if drawdown_from_high is not None and -10.0 <= drawdown_from_high <= -2.0:
            signal_count += 1
            structural_count += 1
            reasons.append("已从高点回落")

        passed = (
            signal_count >= 2 and momentum_count >= 1
        ) or (
            signal_count >= 4 and structural_count >= 3
        )
        strong = signal_count >= 3 and momentum_count >= 1 and structural_count >= 1
        very_strong = (
            signal_count >= 4 and momentum_count >= 2 and structural_count >= 1
        )
        return {
            "passed": passed,
            "strong": strong,
            "very_strong": very_strong,
            "signal_count": signal_count,
            "momentum_count": momentum_count,
            "structural_count": structural_count,
            "reasons": reasons,
            "reason": " | ".join(reasons) if reasons else "顶部确认不足",
        }

    @staticmethod
    def _calculate_4h_momentum_risk(
        price_change_since_close_pct: float,
        atr_filter_pct: Optional[float],
    ) -> dict:
        """计算 4h 做空的盘中动能延续风险。"""
        soft_threshold = MOMENTUM_SOFT_CHASE_PCT_4H
        hard_threshold = MOMENTUM_HARD_CHASE_PCT_4H
        if atr_filter_pct is not None and atr_filter_pct > 0:
            soft_threshold = max(
                soft_threshold,
                atr_filter_pct * MOMENTUM_SOFT_ATR_MULT_4H,
            )
            hard_threshold = max(
                hard_threshold,
                atr_filter_pct * MOMENTUM_HARD_ATR_MULT_4H,
            )

        risk_level = "normal"
        hard_block = False
        penalty = 0.0
        if (
            price_change_since_close_pct > hard_threshold
            or price_change_since_close_pct > MOMENTUM_EXTREME_CHASE_PCT_4H
        ):
            risk_level = "hard_block"
            hard_block = True
        elif price_change_since_close_pct > soft_threshold:
            risk_level = "elevated"
            excess = price_change_since_close_pct - soft_threshold
            penalty = min(15.0, excess * 4.0)

        return {
            "risk_level": risk_level,
            "hard_block": hard_block,
            "penalty": penalty,
            "soft_threshold": round(soft_threshold, 4),
            "hard_threshold": round(hard_threshold, 4),
        }

    def _run_scan(
        self,
        input_data: dict,
        interval: str,
        min_klines: int,
        rsi_thresh: float,
        bias_thresh: float,
        consec_thresh: int,
        rally_thresh: float,
        rally_lookback: int,
        rise_lookback: int,
        weights: dict,
    ) -> dict:
        """通用扫描流程，短期/长期共用。"""
        min_qv = input_data.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
        min_score = input_data.get("min_overbought_score", DEFAULT_MIN_OVERBOUGHT_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
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
                "[%s] 市场状态阻断做空交易: %s",
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
                    "after_overbought_filter": 0,
                    "output_count": 0,
                },
                "market_regime": market_regime,
            }

        # 市场环境阈值调整
        score_adjustment = market_regime.get("score_adjustment", 0)
        market_cautious = market_regime.get("status") == "cautious"
        if score_adjustment > 0:
            log.warning(
                "[%s] 做空门槛提高 %d 分（cautious=%s, btc_above_ma200=%s）: %s",
                self.name,
                score_adjustment,
                market_cautious,
                market_regime.get("btc_above_ma200", False),
                market_regime.get("reason", ""),
            )

        if target_symbols:
            pool = self._build_target_pool(tickers, target_symbols)
        else:
            pool = self._base_filter(tickers, tradable, min_qv)

        log.info("[%s] Step1: %d/%d 通过基础过滤", self.name, len(pool), total_count)

        # 批量获取 OI（仅对通过基础过滤的币种）
        pool_symbols = [item["symbol"] for item in pool]
        oi_map = self._build_oi_map(pool_symbols)

        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        for item in pool:
            symbol = item["symbol"]
            try:
                kline_need = max(KLINE_LIMIT, rise_lookback + 20)
                klines = self._fetch_klines(symbol, interval, kline_need)
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                opens = [float(k[1]) for k in klines]
                volumes = [float(k[5]) for k in klines]
                fr = funding_map.get(symbol)
                oi_raw = oi_map.get(symbol)
                qv = item.get("quoteVolume", 0)
                last_close_for_atr = closes[-1]
                atr_filter_val = calc_atr(highs, lows, closes, ATR_PERIOD_FILTER)
                atr_filter_pct = (
                    round(atr_filter_val / last_close_for_atr * 100, 2)
                    if (atr_filter_val and last_close_for_atr > 0)
                    else None
                )

                # 计算 OI 的 USDT 价值
                oi_value = oi_raw * closes[-1] if oi_raw and closes[-1] > 0 else None

                result = calc_overbought_score(
                    closes,
                    highs,
                    lows,
                    opens,
                    volumes,
                    fr,
                    oi_value,
                    qv,
                    rsi_thresh,
                    bias_thresh,
                    consec_thresh,
                    rally_thresh,
                    rally_lookback,
                    rise_lookback,
                    weights,
                )

                # ── 阻力位检测 ──────────────────────────────────────────────
                # 最近 lookback 根 K 线（含当前）的最高价作为阻力位
                resistance_level = None
                resistance_distance_pct = None
                lookback_resistance = 20
                if len(highs) >= lookback_resistance:
                    resistance_level = max(highs[-lookback_resistance:])
                else:
                    resistance_level = max(highs) if highs else None
                if resistance_level is not None and resistance_level > 0:
                    resistance_distance_pct = (
                        (resistance_level - closes[-1]) / resistance_level * 100
                        if closes[-1] > 0
                        else None
                    )
                # 价格距离阻力位 < 5% 认为在阻力位附近
                is_near_resistance = (
                    resistance_distance_pct is not None
                    and 0 <= resistance_distance_pct <= 5.0
                )

                # ── FOMO买入检测 ──────────────────────────────────────────
                # FOMO买入特征：放量 + 快速上涨（单根K线涨幅 > 3%）+ 长上影线
                fomo_detected = False
                if len(klines) >= 4:
                    recent_4h = klines[-4:]
                    for k in recent_4h:
                        k_open = float(k[1])
                        k_close = float(k[4])
                        k_high = float(k[2])
                        k_low = float(k[3])
                        k_change = (
                            (k_close - k_open) / k_open * 100 if k_open > 0 else 0
                        )
                        body = abs(k_close - k_open)
                        upper_shadow = k_high - max(k_open, k_close)
                        # FOMO买入：涨幅 > 3%，上影线 > 实体的2倍
                        if k_change > 3.0 and body > 0 and upper_shadow / body >= 2.0:
                            fomo_detected = True
                            break

                # ── 空头踩踏检测 ──────────────────────────────────────────
                # 特征：价格在阻力位附近盘整，突然放量下跌
                short_squeeze_detected = False
                if is_near_resistance and len(volumes) >= 2:
                    vol_recent = sum(volumes[-3:]) / 3
                    vol_avg = (
                        sum(volumes[-10:-3]) / 7 if len(volumes) >= 10 else vol_recent
                    )
                    if vol_recent > vol_avg * 2:  # 放量2倍以上
                        price_drop = (
                            (closes[-1] - closes[-2]) / closes[-2] * 100
                            if len(closes) >= 2 and closes[-2] > 0
                            else 0
                        )
                        if price_drop < -2.0:  # 同时价格下跌>2%
                            short_squeeze_detected = True

                # ── 实时价格组合分析 ──────────────────────────────────────
                # 在已关闭 K 线形态判断基础上，叠加当前实时价格变动。
                # 判断：4h 收盘后价格是否仍在继续上涨（盘中动能延续/追空风险）。
                current_price_raw = item.get("lastPrice")
                current_price = float(current_price_raw) if current_price_raw else 0.0
                if current_price <= 0:
                    log.info(
                        "[%s] %s 缺少有效实时价格，跳过",
                        self.name,
                        symbol,
                    )
                    continue
                last_closed_close = closes[-1]
                price_change_since_close_pct = (
                    (current_price - last_closed_close) / last_closed_close * 100
                    if current_price > 0 and last_closed_close > 0
                    else 0.0
                )
                # 动能惩罚：4h 收盘后价格继续上涨 → 做空风险升高。
                # 软阈值扣分，硬阈值直接跳过，阈值随 ATR 自适应。
                momentum_penalty = 0.0
                momentum_risk_level = "normal"
                momentum_threshold_soft = 2.0
                momentum_threshold_hard = None
                klines_1h: List[list] = []
                if interval == "4h":
                    momentum_risk = self._calculate_4h_momentum_risk(
                        price_change_since_close_pct,
                        atr_filter_pct,
                    )
                    momentum_penalty = momentum_risk["penalty"]
                    momentum_risk_level = momentum_risk["risk_level"]
                    momentum_threshold_soft = momentum_risk["soft_threshold"]
                    momentum_threshold_hard = momentum_risk["hard_threshold"]
                    if momentum_risk["hard_block"]:
                        log.info(
                            "[%s] %s 4h收盘后继续上涨 %.2f%%，超过追空硬阈值 %.2f%%，跳过",
                            self.name,
                            symbol,
                            price_change_since_close_pct,
                            momentum_threshold_hard,
                        )
                        continue
                    if momentum_risk_level == "elevated":
                        log.info(
                            "[%s] %s 4h收盘后继续涨 %.2f%%，盘中动能延续扣分 %.1f",
                            self.name,
                            symbol,
                            price_change_since_close_pct,
                            momentum_penalty,
                        )
                else:
                    if price_change_since_close_pct > momentum_threshold_soft:
                        excess = price_change_since_close_pct - momentum_threshold_soft
                        momentum_penalty = min(15.0, excess * 3.0)
                        momentum_risk_level = "elevated"
                        log.info(
                            "[%s] %s 1h收盘后继续涨 %.2f%%，盘中动能延续扣分 %.1f",
                            self.name,
                            symbol,
                            price_change_since_close_pct,
                            momentum_penalty,
                        )
                result["overbought_score"] = max(
                    1, result["overbought_score"] - momentum_penalty
                )
                result["momentum_penalty"] = momentum_penalty
                result["momentum_risk_level"] = momentum_risk_level
                result["momentum_threshold_soft"] = momentum_threshold_soft
                result["momentum_threshold_hard"] = momentum_threshold_hard

                # ── 1h RSI 先行信号（增强版：加入趋势方向）───────────────
                # 做空方向：RSI趋势比绝对值更重要
                # RSI在超买区继续上升 = 空头被轧风险极高
                # RSI在超买区开始下降 = 反弹信号，做空更安全
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
                        # 1h RSI 先行信号：1h 已超买说明盘中已有做空压力
                        # 做空方向：1h RSI 越高 = 短期反弹风险越大 = 做空越危险
                        # RSI 75-80：超买确认，做空压力积聚
                        # RSI 80-90：超买严重，短期反弹风险高
                        # RSI > 90：极端超买，轧空风险极高
                        # RSI趋势：上升=加剧危险，下降=缓解风险
                        if rsi_1h_raw is not None:
                            base_bonus = 0
                            if 75 <= rsi_1h_raw < 80:
                                base_bonus = 5
                            elif 80 <= rsi_1h_raw < 90:
                                base_bonus = 3
                            elif rsi_1h_raw >= 90:
                                base_bonus = -5
                            # 根据RSI趋势调整：做空方向与超跌相反
                            # RSI继续上升 → 加剧轧空风险 → 减少加分或加重扣分
                            # RSI开始下降 → 反弹可能 → 增加加分或减少扣分
                            if base_bonus > 0 and rsi_1h_trend is not None:
                                if rsi_1h_trend > 2:
                                    # RSI快速上升：轧空风险加剧，减少加分
                                    final_bonus = base_bonus - 2
                                elif rsi_1h_trend > 0:
                                    # RSI缓慢上升：保持原分
                                    final_bonus = base_bonus
                                else:
                                    # RSI开始下降：反弹信号，增加加分
                                    final_bonus = base_bonus + 2
                            elif base_bonus < 0 and rsi_1h_trend is not None:
                                # RSI在极端区继续上升：加重扣分
                                if rsi_1h_trend > 2:
                                    final_bonus = base_bonus - 2
                                elif rsi_1h_trend > 0:
                                    final_bonus = base_bonus
                                else:
                                    # RSI下降：缓解，减分减少
                                    final_bonus = base_bonus + 2
                            else:
                                final_bonus = base_bonus
                            result["overbought_score"] += final_bonus
                            result["rsi_1h_bonus"] = final_bonus
                    else:
                        result["rsi_1h"] = None
                        result["rsi_1h_trend"] = None
                        result["rsi_1h_bonus"] = 0
                except Exception:
                    klines_1h = []
                    result["rsi_1h"] = None
                    result["rsi_1h_trend"] = None
                    result["rsi_1h_bonus"] = 0

                if (
                    interval == "4h"
                    and result.get("rsi_1h") is not None
                    and result.get("rsi_1h_trend") is not None
                    and result["rsi_1h"] >= 90
                    and result["rsi_1h_trend"] > 0
                ):
                    log.info(
                        "[%s] %s 1h RSI %.1f 且继续上升 %.2f，极端追空风险，跳过",
                        self.name,
                        symbol,
                        result["rsi_1h"],
                        result["rsi_1h_trend"],
                    )
                    continue

                # ── 周期进度检测 ──────────────────────────────────────────
                # 计算当前4h周期内已完成多少根1h K线，判断是否接近收盘
                if interval == "4h":
                    try:
                        interval_ms = 4 * 3600 * 1000
                        now_ms = int(time.time() * 1000)
                        last_closed_open = klines[-1][0]
                        klines_1h = self._fetch_klines(symbol, "1h", 20)
                        if klines_1h:
                            hour_candles_in_4h = sum(
                                1
                                for k in klines_1h
                                if last_closed_open
                                <= float(k[0])
                                < (last_closed_open + interval_ms)
                            )
                            elapsed_ratio = min(1.0, hour_candles_in_4h / 4.0)
                            result["hour_candles_in_4h"] = hour_candles_in_4h
                            result["elapsed_ratio"] = round(elapsed_ratio, 2)
                            # 4h周期即将收盘（elapsed > 0.75）时，如果1h RSI在合理超买区间(75-90)，
                            # 且动能未追空（收盘后价格未大涨），说明4h收盘后大概率继续回调
                            # RSI >= 90 已进入极端区，不享受此加分（轧空风险太高）
                            # momentum_penalty > 0 说明价格在4h收盘后继续上涨，此时不应该加分
                            rsi_1h_val = result.get("rsi_1h", 0) or 0
                            if (
                                elapsed_ratio > 0.75
                                and 75 <= rsi_1h_val < 90
                                and momentum_penalty == 0
                            ):
                                result["overbought_score"] += 3
                                result["closing_period_bonus"] = 3
                        else:
                            result["hour_candles_in_4h"] = 0
                            result["elapsed_ratio"] = 1.0
                    except Exception:
                        result["hour_candles_in_4h"] = 0
                        result["elapsed_ratio"] = 1.0

                # ── 阻力位/FOMO/空头踩踏加分 ──────────────────────────────
                # 阻力位附近：价格回落概率高，做空信号更强
                # 但如果在追空过程中（momentum_penalty > 0），加分应谨慎
                if is_near_resistance and momentum_penalty == 0:
                    result["overbought_score"] += 5
                    result["resistance_bonus"] = 5
                else:
                    result["resistance_bonus"] = 0

                # FOMO买入检测到：价格可能已到阶段性顶部
                # FOMO本身是顶部信号，即使在动能追空中也可能有效（追高被套后的卖出压力）
                if fomo_detected:
                    result["overbought_score"] += 3
                    result["fomo_bonus"] = 3
                else:
                    result["fomo_bonus"] = 0

                # 空头踩踏确认：最佳做空时机
                # 已在阻力位附近且放量下跌，是较强的做空信号
                if short_squeeze_detected:
                    result["overbought_score"] += 5
                    result["short_squeeze_bonus"] = 5
                else:
                    result["short_squeeze_bonus"] = 0

                # P1-4: 资金费率硬顶过滤（超过 0.15% 的币种借币成本过高，排除）
                if fr is not None and fr > FUNDING_RATE_MAX_FOR_SHORT:
                    log.info(
                        "[%s] %s 资金费率 %.4f%% 超过做空上限 %.4f%%，跳过",
                        self.name,
                        symbol,
                        fr * 100,
                        FUNDING_RATE_MAX_FOR_SHORT * 100,
                    )
                    continue

                effective_min_score = min_score + score_adjustment
                if result["overbought_score"] < effective_min_score:
                    continue

                # 顶部确认硬性门槛：价格必须已从近期高点回落 ≥ 2% 且 ≤ 12%（1h）/ ≤ 15%（4h/1d）
                # - 回落不足 2%：价格仍在上涨途中，追空风险高（TAGUSDT/LABUSDT 案例）
                # - 回落超过上限：做空空间已大幅消耗，盈亏比变差
                # 且至少满足以下四个顶部确认信号中的至少2个（收紧，避免单信号误判）：
                #   1. MACD 顶背离（修复后的双峰检测）
                #   2. RSI 顶背离（短周期上比 MACD 更稳定）
                #   3. KDJ 高位死叉（1h/4h 均用 80 阈值）
                #   4. 量价背离（价涨量缩，动能衰竭的直接证据）
                kdj_threshold = 80.0  # 4h 从 70 收紧至 80，1h 保持 80
                drawdown = _calc_drawdown_from_high(closes, rally_lookback, highs)
                kdj_dead_cross = (
                    result.get("kdj_j") is not None
                    and result["kdj_j"] > 80
                    and _check_kdj_dead_cross(
                        closes, highs, lows, high_threshold=kdj_threshold
                    )
                )
                confirmation = {
                    "passed": True,
                    "strong": False,
                    "very_strong": False,
                    "signal_count": 0,
                    "momentum_count": 0,
                    "structural_count": 0,
                    "reasons": [],
                    "reason": "not_required",
                }
                if interval == "4h":
                    confirmation = self._build_4h_confirmation(
                        closes=closes,
                        highs=highs,
                        lows=lows,
                        klines_1h=klines_1h,
                        current_price=current_price,
                        rsi_1h=result.get("rsi_1h"),
                        rsi_1h_trend=result.get("rsi_1h_trend"),
                        macd_divergence=bool(result.get("macd_divergence")),
                        rsi_divergence=bool(result.get("rsi_divergence")),
                        volume_divergence=bool(result.get("volume_divergence")),
                        kdj_dead_cross=bool(kdj_dead_cross),
                        drawdown_from_high=drawdown,
                    )
                else:
                    reversal_signals = [
                        result.get("macd_divergence"),
                        result.get("rsi_divergence"),
                        kdj_dead_cross,
                        result.get("volume_divergence"),
                    ]
                    max_drawdown = -8.0 if interval == H1_INTERVAL else -10.0
                    has_drawdown = (
                        drawdown is not None and max_drawdown <= drawdown <= -2.0
                    )
                    has_reversal_confirm = sum(1 for s in reversal_signals if s) >= 2
                    confirmation["passed"] = has_drawdown and has_reversal_confirm
                if not confirmation.get("passed"):
                    log.info(
                        "[%s] %s 顶部未确认，跳过: drawdown=%.2f%%, reason=%s",
                        self.name,
                        symbol,
                        drawdown if drawdown is not None else 0.0,
                        confirmation.get("reason", ""),
                    )
                    continue
                if interval == "4h" and momentum_risk_level == "elevated":
                    if not confirmation.get("strong"):
                        log.info(
                            "[%s] %s 盘中动能延续风险偏高，需要强顶部确认，跳过: reason=%s",
                            self.name,
                            symbol,
                            confirmation.get("reason", ""),
                        )
                        continue
                if interval == "4h":
                    result["overbought_score"] += (
                        8 if confirmation.get("very_strong")
                        else (5 if confirmation.get("strong") else 3)
                    )

                # 所有 bonus 应用后上界钳制
                result["overbought_score"] = min(
                    100, result["overbought_score"]
                )

                # P2-6: 4h 高波动做空改为硬过滤 + 分级处理
                atr_check_pct = atr_filter_pct
                volatility_action = "allow"
                if interval == "4h" and atr_check_pct is not None:
                    if atr_check_pct > 8.0:
                        log.info(
                            "[%s] %s ATR%.2f%%>8%%，4h 做空直接跳过",
                            self.name,
                            symbol,
                            atr_check_pct,
                        )
                        continue
                    if 6.0 < atr_check_pct <= 8.0:
                        if not confirmation.get("very_strong"):
                            log.info(
                                "[%s] %s ATR%.2f%% 位于 6-8%%，需要极强顶部确认，跳过",
                                self.name,
                                symbol,
                                atr_check_pct,
                            )
                            continue
                        volatility_action = "reduce_size_strict"
                    elif 5.0 < atr_check_pct <= 6.0:
                        if not confirmation.get("strong"):
                            log.info(
                                "[%s] %s ATR%.2f%% 位于 5-6%%，需要强顶部确认，跳过",
                                self.name,
                                symbol,
                                atr_check_pct,
                            )
                            continue
                        volatility_action = "reduce_size"

                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                atr_filter_val = calc_atr(highs, lows, closes, ATR_PERIOD_FILTER)
                last_close = closes[-1]
                atr_pct = (
                    round(atr_val / last_close * 100, 2)
                    if (atr_val and last_close > 0)
                    else None
                )

                # P2-8: 日历效应检测（季度交割周）
                now_utc = datetime.now(timezone.utc)
                is_delivery_week = _is_delivery_week(now_utc)

                scored.append(
                    {
                        "symbol": symbol,
                        "close": last_close,
                        "current_price": current_price,
                        "quote_volume_24h": qv,
                        "price_change_pct": item.get("priceChangePercent", 0),
                        # ── 实时价格分析字段 ─────────────────────────────
                        "price_change_since_close_pct": round(
                            price_change_since_close_pct, 2
                        ),
                        "momentum_penalty": result.get("momentum_penalty", 0),
                        "momentum_risk_level": result.get("momentum_risk_level", "normal"),
                        "momentum_threshold_soft": result.get("momentum_threshold_soft"),
                        "momentum_threshold_hard": result.get("momentum_threshold_hard"),
                        "rsi_1h": result.get("rsi_1h"),
                        "rsi_1h_trend": result.get("rsi_1h_trend"),
                        "rsi_1h_bonus": result.get("rsi_1h_bonus", 0),
                        "hour_candles_in_4h": result.get("hour_candles_in_4h", 0),
                        "elapsed_ratio": result.get("elapsed_ratio", 1.0),
                        "closing_period_bonus": result.get("closing_period_bonus", 0),
                        # ── 阻力位/FOMO/空头踩踏字段 ────────────────────
                        "resistance_distance_pct": (
                            round(resistance_distance_pct, 2)
                            if resistance_distance_pct is not None
                            else None
                        ),
                        "is_near_resistance": is_near_resistance,
                        "fomo_detected": fomo_detected,
                        "fomo_bonus": result.get("fomo_bonus", 0),
                        "short_squeeze_detected": short_squeeze_detected,
                        "short_squeeze_bonus": result.get("short_squeeze_bonus", 0),
                        "resistance_bonus": result.get("resistance_bonus", 0),
                        "overbought_confirmation": confirmation,
                        "volatility_action": volatility_action,
                        "effective_min_overbought_score": effective_min_score,
                        # ── 原有字段 ───────────────────────────────────
                        "rsi": result["rsi"],
                        "bias_20": result["bias_20"],
                        "consecutive_up": result["consecutive_up"],
                        "rally_pct": result["rally_pct"],
                        "above_boll_upper": result["above_boll_upper"],
                        "kdj_j": result["kdj_j"],
                        "macd_divergence": result["macd_divergence"],
                        "rsi_divergence": result["rsi_divergence"],
                        "volume_divergence": result["volume_divergence"],
                        "funding_rate": result["funding_rate"],
                        "oi_value_usdt": round(oi_value, 2) if oi_value else None,
                        "squeeze_risk": result["squeeze_risk"],
                        "rise_from_low_pct": result["rise_from_low_pct"],
                        "overbought_score": result["overbought_score"],
                        "signal_details": result["signal_details"],
                        "atr_pct": atr_pct,
                        "atr_filter_pct": atr_filter_pct,
                        "signal_direction": "short",
                        "strategy_tag": self.name,
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                        "delivery_week": is_delivery_week,  # P2-8: 季度交割周标记
                    }
                )
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["overbought_score"], reverse=True)
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
                "after_overbought_filter": len(scored),
                "output_count": len(candidates),
            },
        }


# ══════════════════════════════════════════════════════════
# 短期超买 Skill（4h）
# ══════════════════════════════════════════════════════════


class ShortTermOverboughtSkill(_CryptoOverboughtBase):
    """短期超买做空筛选（4h K 线）。

    捕捉 FOMO 情绪见顶后的急跌，适合日内/隔日做空。
    核心信号：RSI 极端超买 + 资金费率极端正值 + 量价背离。
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
        self.name = "crypto_overbought_4h"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(
            input_data,
            interval=ST_INTERVAL,
            min_klines=ST_MIN_KLINES,
            rsi_thresh=ST_RSI_THRESHOLD,
            bias_thresh=ST_BIAS_THRESHOLD,
            consec_thresh=ST_CONSECUTIVE_UP,
            rally_thresh=ST_RALLY_PCT,
            rally_lookback=ST_RALLY_LOOKBACK,
            rise_lookback=ST_RISE_LOOKBACK,
            weights={
                "rsi": ST_W_RSI,
                "funding": ST_W_FUNDING,
                "bias": ST_W_BIAS,
                "vol_div": ST_W_VOL_DIV,
                "boll": ST_W_BOLL,
                "rally": ST_W_RALLY,
                "kdj": ST_W_KDJ,
                "macd_div": ST_W_MACD_DIV,
                "shadow": ST_W_SHADOW,
                "squeeze_risk": ST_W_SQUEEZE_RISK,
            },
        )


class HourlyOverboughtSkill(_CryptoOverboughtBase):
    """超短期超买做空筛选（1h K 线）。

    捕捉小时级别的 FOMO 见顶，适合快速做空（4h~24h 持仓）。
    核心信号：1h RSI 极端超买 + 资金费率极端正值 + 量价背离。
    比 4h 模式更敏感，轧空风险扣分更重。
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
        self.name = "crypto_overbought_1h"

    def run(self, input_data: dict) -> dict:
        # 1h 回测优化门槛：45（做空风险不对称，门槛应高于做多）
        # 原 35 分只需 2-3 个维度触发就能通过，对做空偏松
        # 45 分要求至少 3-4 个维度同时触发，过滤低质量信号
        if "min_overbought_score" not in input_data:
            input_data = {**input_data, "min_overbought_score": 45}
        return self._run_scan(
            input_data,
            interval=H1_INTERVAL,
            min_klines=H1_MIN_KLINES,
            rsi_thresh=H1_RSI_THRESHOLD,
            bias_thresh=H1_BIAS_THRESHOLD,
            consec_thresh=H1_CONSECUTIVE_UP,
            rally_thresh=H1_RALLY_PCT,
            rally_lookback=H1_RALLY_LOOKBACK,
            rise_lookback=H1_RISE_LOOKBACK,
            weights={
                "rsi": H1_W_RSI,
                "funding": H1_W_FUNDING,
                "bias": H1_W_BIAS,
                "vol_div": H1_W_VOL_DIV,
                "boll": H1_W_BOLL,
                "rally": H1_W_RALLY,
                "kdj": H1_W_KDJ,
                "macd_div": H1_W_MACD_DIV,
                "shadow": H1_W_SHADOW,
                "squeeze_risk": H1_W_SQUEEZE_RISK,
            },
        )


# ══════════════════════════════════════════════════════════


class LongTermOverboughtSkill(_CryptoOverboughtBase):
    """长期超买做空筛选（1d K 线）。

    捕捉日线级别持续上涨后的趋势衰竭，适合波段做空（3天~2周）。
    核心信号：MACD 顶背离 + 日线 BIAS 极端偏离 + 资金费率极端正值。
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
        self.name = "crypto_overbought_1d"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(
            input_data,
            interval=LT_INTERVAL,
            min_klines=LT_MIN_KLINES,
            rsi_thresh=LT_RSI_THRESHOLD,
            bias_thresh=LT_BIAS_THRESHOLD,
            consec_thresh=LT_CONSECUTIVE_UP,
            rally_thresh=LT_RALLY_PCT,
            rally_lookback=LT_RALLY_LOOKBACK,
            rise_lookback=LT_RISE_LOOKBACK,
            weights={
                "rsi": LT_W_RSI,
                "funding": LT_W_FUNDING,
                "bias": LT_W_BIAS,
                "vol_div": LT_W_VOL_DIV,
                "boll": LT_W_BOLL,
                "rally": LT_W_RALLY,
                "kdj": LT_W_KDJ,
                "macd_div": LT_W_MACD_DIV,
                "shadow": LT_W_SHADOW,
                "squeeze_risk": LT_W_SQUEEZE_RISK,
                "rise_threshold": LT_RISE_THRESHOLD,
            },
        )


# 向后兼容
CryptoOverboughtSkill = ShortTermOverboughtSkill


# ══════════════════════════════════════════════════════════
# 十维度超买评分（纯函数，短期/长期共用）
# ══════════════════════════════════════════════════════════


def calc_overbought_score(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    opens: List[float],
    volumes: List[float],
    funding_rate: Optional[float],
    oi_value: Optional[float],
    quote_volume_24h: float,
    rsi_thresh: float,
    bias_thresh: float,
    consec_up_thresh: int,
    rally_pct_thresh: float,
    rally_lookback: int,
    rise_lookback: int,
    weights: dict,
) -> dict:
    """计算超买做空综合评分（满分 100）。

    通过 weights 字典控制各维度权重，短期/长期使用不同权重配置。
    内置轧空风险扣分机制。
    """
    signals = []
    score = 0.0
    w = weights

    # ── 1. RSI 极端超买 ──
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    if rsi_val is not None and rsi_val > rsi_thresh:
        # RSI 越高分越高，80→满分的 50%，90→满分的 100%
        ratio = min(1.0, (rsi_val - rsi_thresh) / (100 - rsi_thresh))
        score += w["rsi"] * (0.5 + 0.5 * ratio)
        signals.append(f"RSI={rsi_val:.1f}>{rsi_thresh}")

    # ── 2. 资金费率极端正值（做空最强信号）──
    # 费率为负 = 空头已拥挤在付费，此时做空是逆向信号，扣分
    fr_display = None
    if funding_rate is not None:
        fr_display = round(funding_rate * 100, 4)
        # P1-4: 0.05%-0.1% 区间降低评分（借币成本开始侵蚀利润）
        if funding_rate > FUNDING_RATE_HIGH:
            if funding_rate >= FUNDING_RATE_VERY_EXTREME:
                score += w["funding"]
                signals.append(f"费率={fr_display:.3f}%罕见极端")
            elif funding_rate >= FUNDING_RATE_EXTREME:
                score += w["funding"] * 0.8
                signals.append(f"费率={fr_display:.3f}%极端")
            else:
                ratio = (funding_rate - FUNDING_RATE_HIGH) / (
                    FUNDING_RATE_EXTREME - FUNDING_RATE_HIGH
                )
                score += w["funding"] * 0.5 * min(1.0, ratio)
                signals.append(f"费率={fr_display:.3f}%偏高")
        elif funding_rate >= FUNDING_RATE_HIGH:
            # 0.1% 临界值，按偏低处理（避免 fr=0.001 时 penalty=0 的信号真空）
            penalty = w["funding"] * 0.15
            score -= penalty
            signals.append(
                f"费率={fr_display:.3f}%临界(借币成本偏高,扣{penalty:.1f}分)"
            )
        elif funding_rate > FUNDING_RATE_MIN_FOR_SCORE:
            # 0.05% < funding_rate < 0.1%：借币成本开始侵蚀利润，降低评分
            ratio = (funding_rate - FUNDING_RATE_MIN_FOR_SCORE) / (
                FUNDING_RATE_HIGH - FUNDING_RATE_MIN_FOR_SCORE
            )
            penalty = w["funding"] * 0.3 * (1 - ratio)
            score -= penalty
            signals.append(f"费率={fr_display:.3f}%偏低(借币成本高,扣{penalty:.1f}分)")
        elif funding_rate < -0.0005:
            # 费率为负：空头在付费给多头，说明空头已经很拥挤
            # 此时做空 = 加入拥挤的一方，轧空风险高
            # 费率越负扣分越重，-0.05% 扣 5 分，-0.1% 扣 10 分，上限 15 分
            penalty = min(15.0, abs(funding_rate) / 0.001 * 5.0)
            score -= penalty
            signals.append(f"⚠️费率={fr_display:.3f}%为负(空头拥挤,扣{penalty:.0f}分)")

    # ── 3. BIAS 正向偏离 ──
    bias_20 = _calc_bias(closes, BOLL_PERIOD)
    if bias_20 is not None and bias_20 > bias_thresh:
        ratio = min(1.0, (bias_20 - bias_thresh) / bias_thresh)
        score += w["bias"] * (0.5 + 0.5 * ratio)
        signals.append(f"BIAS={bias_20:.1f}%>{bias_thresh}%")

    # ── 4. 量价背离（价格创新高但量能萎缩）──
    vol_div = _check_volume_divergence(closes, volumes)
    if vol_div:
        score += w["vol_div"]
        signals.append("量价背离(价涨量缩)")

    # ── 5. 布林带突破上轨 ──
    above_boll = _check_above_boll_upper(closes)
    if above_boll:
        score += w["boll"]
        signals.append("突破BOLL上轨")

    # ── 6. 连续暴涨 + 累计涨幅 ──
    consec = _calc_consecutive_up(closes)
    rally_pct = _calc_rally_pct(closes, rally_lookback)
    rally_score = 0.0
    if consec >= consec_up_thresh:
        rally_score += w["rally"] * 0.4 * min(1.0, consec / (consec_up_thresh * 2))
        signals.append(f"连涨{consec}根≥{consec_up_thresh}")
    if rally_pct is not None and rally_pct > rally_pct_thresh:
        rally_score += (
            w["rally"]
            * 0.6
            * min(1.0, (rally_pct - rally_pct_thresh) / rally_pct_thresh)
        )
        signals.append(f"近{rally_lookback}根涨{rally_pct:.1f}%")
    score += min(rally_score, float(w["rally"]))

    # 距低点涨幅（长期模式额外检查）
    rise_from_low = _calc_rise_from_low(closes, rise_lookback)
    rise_threshold = w.get("rise_threshold")
    if rise_threshold and rise_from_low is not None and rise_from_low > rise_threshold:
        # 已在 rally 权重内，这里作为额外信号记录
        signals.append(f"距低点涨{rise_from_low:.1f}%>{rise_threshold}%")

    # ── 7. KDJ 高位死叉 ──
    kdj_j = _calc_kdj_j(closes, highs, lows)
    kdj_score = 0.0
    if kdj_j is not None and kdj_j > 100:
        kdj_score = w["kdj"] * min(1.0, (kdj_j - 100) / 30.0)
        signals.append(f"KDJ_J={kdj_j:.1f}>100")
    # 检测 KDJ 死叉（K 下穿 D 且都在 80 以上）
    kdj_dead = _check_kdj_dead_cross(closes, highs, lows)
    if kdj_dead:
        kdj_score = max(kdj_score, w["kdj"])
        if "KDJ_J" not in str(signals):
            signals.append("KDJ高位死叉")
        else:
            signals.append("KDJ死叉确认")
    score += kdj_score

    # ── 8. MACD 顶背离 ──
    macd_div = _check_macd_top_divergence(closes)
    if macd_div:
        score += w["macd_div"]
        signals.append("MACD顶背离")

    # ── 8.5 RSI 顶背离（补充信号，1h/4h 上比 MACD 更稳定）──
    rsi_div = _check_rsi_top_divergence(closes)
    if rsi_div:
        # 权重复用 macd_div 的 50%，避免重复计分过高
        score += w["macd_div"] * 0.5
        signals.append("RSI顶背离")

    # ── 9. 长上影线 ──
    shadow_score = _score_upper_shadow(closes, opens, highs, lows)
    if shadow_score > 0:
        score += w["shadow"]
        signals.append("长上影线")

    # ── 9.5 距近期高点回撤检查 ──
    # 如果价格已经从高点大幅回落，说明做空最佳时机已过，扣分
    drawdown_from_high = _calc_drawdown_from_high(closes, rally_lookback, highs)
    drawdown_penalty_pct = w.get("drawdown_penalty_pct", -5.0)  # 默认回撤 5% 开始扣分
    if drawdown_from_high is not None and drawdown_from_high < drawdown_penalty_pct:
        # 回撤越深扣分越多，最多扣 20 分
        penalty = min(20.0, abs(drawdown_from_high - drawdown_penalty_pct) * 2.0)
        score -= penalty
        signals.append(f"⚠️已回撤{drawdown_from_high:.1f}%(扣{penalty:.0f}分)")

    # ── 10. 轧空风险直接排除 ──
    # 低流动性高OI币种直接跳过，不只是扣分（避免追空后被轧）
    squeeze_risk = False
    if oi_value and quote_volume_24h > 0:
        oi_ratio = oi_value / quote_volume_24h
        if (
            quote_volume_24h < SQUEEZE_RISK_QV_THRESHOLD
            and oi_ratio > SQUEEZE_RISK_OI_RATIO
        ):
            squeeze_risk = True
            # 直接排除，不返回结果
            return {
                "rsi": round(rsi_val, 2) if rsi_val is not None else None,
                "bias_20": round(bias_20, 2) if bias_20 is not None else None,
                "consecutive_up": None,
                "rally_pct": None,
                "above_boll_upper": None,
                "kdj_j": None,
                "macd_divergence": False,
                "rsi_divergence": False,
                "volume_divergence": False,
                "funding_rate": fr_display,
                "squeeze_risk": True,
                "rise_from_low_pct": None,
                "overbought_score": 0,  # score=0，被 min_score 阈值过滤；target_symbols 非空时也会被过滤
                "signal_details": f"⚠️轧空风险排除(OI/Vol={oi_ratio:.2f})，不做空",
            }

    return {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "bias_20": round(bias_20, 2) if bias_20 is not None else None,
        "consecutive_up": consec,
        "rally_pct": round(rally_pct, 2) if rally_pct is not None else None,
        "above_boll_upper": above_boll,
        "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
        "macd_divergence": macd_div,
        "rsi_divergence": rsi_div,
        "volume_divergence": vol_div,
        "funding_rate": fr_display,
        "squeeze_risk": squeeze_risk,
        "rise_from_low_pct": round(rise_from_low, 2)
        if rise_from_low is not None
        else None,
        "overbought_score": max(0, round(score)),
        "signal_details": " | ".join(signals) if signals else "无超买信号",
    }


# ══════════════════════════════════════════════════════════
# 纯函数指标库
# ══════════════════════════════════════════════════════════


def _calc_bias(closes: List[float], period: int = 20) -> Optional[float]:
    """乖离率 BIAS = (收盘价 - MA) / MA * 100。"""
    if len(closes) < period:
        return None
    ma = sum(closes[-period:]) / period
    if ma <= 0:
        return None
    return (closes[-1] - ma) / ma * 100


def _calc_consecutive_up(closes: List[float]) -> int:
    """计算从最新 K 线往回的连续上涨根数。"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            count += 1
        else:
            break
    return count


def _calc_rally_pct(closes: List[float], lookback: int) -> Optional[float]:
    """计算近 N 根的累计涨幅。"""
    if len(closes) < lookback + 1:
        return None
    base = closes[-(lookback + 1)]
    if base <= 0:
        return None
    return (closes[-1] - base) / base * 100


def _calc_rise_from_low(closes: List[float], lookback: int) -> Optional[float]:
    """计算距近期最低点的涨幅。"""
    if len(closes) < 2:
        return None
    window = closes[-min(lookback, len(closes)) :]
    low = min(window)
    return (closes[-1] - low) / low * 100 if low > 0 else None


def _calc_drawdown_from_high(
    closes: List[float], lookback: int, highs: Optional[List[float]] = None
) -> Optional[float]:
    """计算当前收盘价距近期最高价的回撤幅度（负值表示回撤）。

    用于判断做空时机是否已过：如果价格已经从高点大幅回落，
    做空的盈亏比变差（下跌空间已被消耗）。

    优先使用 highs（K 线最高价）计算真实高点，
    回退到 closes（收盘价）以兼容无 highs 数据的调用方。

    返回:
        回撤百分比（负值），如 -5.0 表示从高点回撤了 5%。
        None 表示数据不足。
    """
    if len(closes) < 2:
        return None
    # 优先用最高价序列确定真实高点（捕捉长上影线顶部）
    if highs and len(highs) >= 2:
        window_high = max(highs[-min(lookback, len(highs)) :])
    else:
        window_high = max(closes[-min(lookback, len(closes)) :])
    if window_high <= 0:
        return None
    return (closes[-1] - window_high) / window_high * 100


def _check_above_boll_upper(closes: List[float]) -> bool:
    """检测价格是否突破布林带上轨。"""
    if len(closes) < BOLL_PERIOD:
        return False
    window = closes[-BOLL_PERIOD:]
    ma = sum(window) / BOLL_PERIOD
    variance = sum((x - ma) ** 2 for x in window) / BOLL_PERIOD
    std = math.sqrt(variance)
    return closes[-1] > ma + BOLL_STD_MULT * std


def _check_volume_divergence(
    closes: List[float],
    volumes: List[float],
    lookback: int = 20,
) -> bool:
    """检测量价背离：价格创新高但量能萎缩。

    条件：
    1. 最新收盘价是近 lookback 根的最高价（或接近最高价 98%）
    2. 最新 3 根均量 < 前 10 根均量的 70%（量能明显萎缩）
    """
    if len(closes) < lookback or len(volumes) < lookback:
        return False

    recent_closes = closes[-lookback:]
    max_close = max(recent_closes)

    # 价格在近期高位（≥ 最高价的 98%）
    if closes[-1] < max_close * 0.98:
        return False

    # 量能萎缩：近 3 根均量 vs 前 10 根均量
    if len(volumes) < 15:
        return False
    recent_vol = sum(volumes[-3:]) / 3
    prior_vol = sum(volumes[-13:-3]) / 10
    if prior_vol <= 0:
        return False

    return recent_vol < prior_vol * 0.8  # 放宽：量能萎缩 20% 即触发（原 30%）


def _calc_kdj_j(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    period: int = KDJ_PERIOD,
    m1: int = KDJ_M1,
    m2: int = KDJ_M2,
) -> Optional[float]:
    """计算 KDJ 的 J 值。"""
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


def _check_kdj_dead_cross(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    high_threshold: float = 70.0,  # 从 80 降到 70，与调用方统一
) -> bool:
    """检测 KDJ 高位死叉：K 下穿 D，且死叉前 K 在高位。

    high_threshold 控制"高位"定义：
    - 统一使用 70（原 4h/1d 用 80，1h 用 70，现在统一为 70）
    - K 值 ≥ 70 即为超买区，死叉就有做空意义
    死叉时 K 只需 > high_threshold * 0.6，允许死叉发生时已有小幅回落。
    """
    if len(closes) < KDJ_PERIOD + KDJ_M1 + KDJ_M2 + 3:
        return False

    def _calc_kd(c, h, l):
        rsvs = []
        for i in range(KDJ_PERIOD - 1, len(c)):
            hh = max(h[i - KDJ_PERIOD + 1 : i + 1])
            ll = min(l[i - KDJ_PERIOD + 1 : i + 1])
            rsvs.append(50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100)
        if not rsvs:
            return None, None
        k = d = rsvs[0]
        for rsv in rsvs[1:]:
            k = (k * (KDJ_M1 - 1) + rsv) / KDJ_M1
            d = (d * (KDJ_M2 - 1) + k) / KDJ_M2
        return k, d

    k_now, d_now = _calc_kd(closes, highs, lows)
    k_prev, d_prev = _calc_kd(closes[:-1], highs[:-1], lows[:-1])

    if k_now is None or k_prev is None:
        return False

    # K 下穿 D（死叉），且死叉前 K 在高位（k_prev >= high_threshold）
    # 死叉时 K 只需 > high_threshold * 0.6，允许死叉发生时已有小幅回落
    return (
        k_now < d_now
        and k_prev >= d_prev
        and k_prev >= high_threshold
        and k_now > high_threshold * 0.6
    )


def _check_macd_top_divergence(closes: List[float], lookback: int = 30) -> bool:
    """检测 MACD 顶背离：价格创新高但 MACD 直方图未创新高。

    在完整序列上计算一次 MACD，通过索引取直方图值。
    同时验证两个高点处 MACD 直方图为正（标准看跌背离要求 MACD 在零轴上方）。
    使用局部极大值检测，而非从最高点往前固定偏移。
    """
    if len(closes) < 35 or len(closes) < lookback + 10:
        return False

    # 在完整序列上计算 MACD 直方图
    ema_fast = calc_ema(closes, 12)
    ema_slow = calc_ema(closes, 26)
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if math.isnan(f) or math.isnan(s):
            macd_line.append(float("nan"))
        else:
            macd_line.append(f - s)
    valid_macd = [v for v in macd_line if not math.isnan(v)]
    if len(valid_macd) < 9:
        return False
    signal_ema = calc_ema(valid_macd, 9)
    nan_count = len(closes) - len(valid_macd)
    histogram = []
    for i in range(len(closes)):
        if i < nan_count:
            histogram.append(float("nan"))
        else:
            vi = i - nan_count
            if vi < len(signal_ema) and not math.isnan(signal_ema[vi]):
                histogram.append(macd_line[i] - signal_ema[vi])
            else:
                histogram.append(float("nan"))

    recent = closes[-lookback:]
    base_idx = len(closes) - lookback

    # 找窗口内所有局部高点（前后各 2 根都低于它）
    peaks = []
    for i in range(2, len(recent) - 2):
        if (
            recent[i] > recent[i - 1]
            and recent[i] > recent[i - 2]
            and recent[i] > recent[i + 1]
            and recent[i] > recent[i + 2]
        ):
            peaks.append(i)

    if len(peaks) < 2:
        return False

    p1, p2 = peaks[-2], peaks[-1]

    # 价格必须创新高
    if recent[p2] <= recent[p1]:
        return False

    h1 = histogram[base_idx + p1]
    h2 = histogram[base_idx + p2]

    # 直方图值必须有效且为正（标准看跌背离要求 MACD 在零轴上方）
    if math.isnan(h1) or math.isnan(h2):
        return False
    if h1 <= 0 or h2 <= 0:
        return False

    # 顶背离：价格新高但 MACD 直方图更低
    return h2 < h1


def _check_rsi_top_divergence(closes: List[float], lookback: int = 30) -> bool:
    """检测 RSI 顶背离：价格创新高但 RSI 未创新高。

    RSI 背离在短周期（1h/4h）上比 MACD 背离更稳定，是顶部确认的有效补充信号。
    使用与 MACD 背离相同的局部极大值检测逻辑，保持一致性。
    """
    if len(closes) < lookback + RSI_PERIOD:
        return False

    recent = closes[-lookback:]
    base_idx = len(closes) - lookback

    # 找窗口内所有局部高点（前后各 2 根都低于它）
    peaks = []
    for i in range(2, len(recent) - 2):
        if (
            recent[i] > recent[i - 1]
            and recent[i] > recent[i - 2]
            and recent[i] > recent[i + 1]
            and recent[i] > recent[i + 2]
        ):
            peaks.append(i)

    if len(peaks) < 2:
        return False

    p1, p2 = peaks[-2], peaks[-1]

    # 价格必须创新高
    if recent[p2] <= recent[p1]:
        return False

    # 比较两个高点对应的 RSI
    rsi1 = calc_rsi(closes[: base_idx + p1 + 1], RSI_PERIOD)
    rsi2 = calc_rsi(closes[: base_idx + p2 + 1], RSI_PERIOD)

    # 顶背离：价格新高但 RSI 更低
    return rsi1 is not None and rsi2 is not None and rsi2 < rsi1


def _score_upper_shadow(
    closes: List[float],
    opens: List[float],
    highs: List[float],
    lows: List[float],
) -> float:
    """长上影线检测。

    近 3 根 K 线内出现长上影线 = 上方有强阻力。
    上影线长度 / 实体长度 ≥ 2 倍。
    """
    for i in range(-3, 0):
        if i >= -len(closes):
            c, o, h, l = closes[i], opens[i], highs[i], lows[i]
            body = abs(c - o)
            upper_shadow = h - max(c, o)
            if body > 0 and upper_shadow >= body * 2.0:
                return 1.0
            # 十字星（实体极小但上影线长）
            if body < (h - l) * 0.1 and upper_shadow > (h - l) * 0.5:
                return 0.7
    return 0.0


# ══════════════════════════════════════════════════════════
# P2-8: 日历效应过滤
# ══════════════════════════════════════════════════════════

DELIVERY_MONTHS = {3, 6, 9, 12}  # 季度交割月份
DELIVERY_LOOKBACK_DAYS = 7  # 交割周定义：交割日前后 7 天


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
