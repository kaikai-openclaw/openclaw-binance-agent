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
# 共享常量
# ══════════════════════════════════════════════════════════

BOLL_PERIOD = 20
BOLL_STD_MULT = 2.0
KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 资金费率阈值（做空方向：极端正值 = 多头拥挤）
FUNDING_RATE_HIGH = 0.001               # +0.1%，偏高
FUNDING_RATE_EXTREME = 0.003            # +0.3%，极端（多头付费维持仓位）
FUNDING_RATE_VERY_EXTREME = 0.005       # +0.5%，罕见极端

# ══════════════════════════════════════════════════════════
# 短期超买参数（4h K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "4h"
ST_MIN_KLINES = 50
ST_RSI_THRESHOLD = 80.0          # 4h RSI > 80 = 极端超买
ST_BIAS_THRESHOLD = 12.0         # 4h 乖离率 > +12%
ST_CONSECUTIVE_UP = 5            # 连续上涨 ≥ 5 根 4h（≈ 20 小时）
ST_RALLY_PCT = 15.0              # 近 N 根累计涨幅 > +15%
ST_RALLY_LOOKBACK = 18           # 回看 18 根 4h = 3 天
ST_RISE_LOOKBACK = 30            # 距低点涨幅回看 30 根 4h = 5 天

# 短期评分权重（满分 100）
ST_W_RSI = 15            # RSI 极端超买
ST_W_FUNDING = 18        # 资金费率极端正值（短期做空最强信号）
ST_W_BIAS = 12           # 乖离率正向偏离
ST_W_VOL_DIV = 12        # 量价背离
ST_W_BOLL = 8            # 布林带突破上轨
ST_W_RALLY = 10          # 连续暴涨
ST_W_KDJ = 7             # KDJ 高位死叉
ST_W_MACD_DIV = 5        # MACD 顶背离（4h 可靠性一般）
ST_W_SHADOW = 5          # 长上影线
ST_W_SQUEEZE_RISK = -8   # 轧空风险扣分

# ══════════════════════════════════════════════════════════
# 长期超买参数（1d K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1d"
LT_MIN_KLINES = 60
LT_RSI_THRESHOLD = 75.0          # 日线 RSI > 75
LT_BIAS_THRESHOLD = 18.0         # 日线 20 日乖离率 > +18%
LT_CONSECUTIVE_UP = 5            # 连续上涨 ≥ 5 天
LT_RALLY_PCT = 30.0              # 近 N 日累计涨幅 > +30%
LT_RALLY_LOOKBACK = 14           # 回看 14 天
LT_RISE_LOOKBACK = 60            # 距低点涨幅回看 60 天
LT_RISE_THRESHOLD = 60.0         # 距低点涨幅 > +60%

# 长期评分权重（满分 100）
LT_W_RSI = 10            # RSI
LT_W_FUNDING = 12        # 资金费率（长期看权重降低）
LT_W_BIAS = 15           # 乖离率（日线 BIAS 更可靠）
LT_W_VOL_DIV = 12        # 量价背离
LT_W_BOLL = 8            # 布林带
LT_W_RALLY = 12          # 连续暴涨 + 距低点涨幅
LT_W_KDJ = 7             # KDJ
LT_W_MACD_DIV = 15       # MACD 顶背离（日线级别可靠性高）
LT_W_SHADOW = 5          # 长上影线
LT_W_SQUEEZE_RISK = -4   # 轧空风险扣分（长期看风险降低）

DEFAULT_MIN_QUOTE_VOLUME = 10_000_000
DEFAULT_MIN_OVERBOUGHT_SCORE = 25
DEFAULT_MAX_CANDIDATES = 20
# 轧空风险：成交额低于此值且 OI/成交额比过高 → 扣分
SQUEEZE_RISK_QV_THRESHOLD = 50_000_000
SQUEEZE_RISK_OI_RATIO = 0.5     # OI 价值 / 24h 成交额 > 50% = 拥挤


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════

class _CryptoOverboughtBase(BaseSkill):
    """超买做空筛选共享基类。"""

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

    def _run_scan(
        self, input_data: dict, interval: str, min_klines: int,
        rsi_thresh: float, bias_thresh: float, consec_thresh: int,
        rally_thresh: float, rally_lookback: int,
        rise_lookback: int, weights: dict,
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

                # 计算 OI 的 USDT 价值
                oi_value = oi_raw * closes[-1] if oi_raw and closes[-1] > 0 else None

                result = calc_overbought_score(
                    closes, highs, lows, opens, volumes,
                    fr, oi_value, qv,
                    rsi_thresh, bias_thresh, consec_thresh,
                    rally_thresh, rally_lookback, rise_lookback,
                    weights,
                )

                if result["overbought_score"] < min_score and not target_symbols:
                    continue

                returns_map[symbol] = calc_returns(closes)
                atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
                last_close = closes[-1]
                atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None

                scored.append({
                    "symbol": symbol,
                    "close": last_close,
                    "quote_volume_24h": qv,
                    "price_change_pct": item.get("priceChangePercent", 0),
                    "rsi": result["rsi"],
                    "bias_20": result["bias_20"],
                    "consecutive_up": result["consecutive_up"],
                    "rally_pct": result["rally_pct"],
                    "above_boll_upper": result["above_boll_upper"],
                    "kdj_j": result["kdj_j"],
                    "macd_divergence": result["macd_divergence"],
                    "volume_divergence": result["volume_divergence"],
                    "funding_rate": result["funding_rate"],
                    "oi_value_usdt": round(oi_value, 2) if oi_value else None,
                    "squeeze_risk": result["squeeze_risk"],
                    "rise_from_low_pct": result["rise_from_low_pct"],
                    "overbought_score": result["overbought_score"],
                    "signal_details": result["signal_details"],
                    "atr_pct": atr_pct,
                    "signal_direction": "short",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["overbought_score"], reverse=True)
        candidates = self._deduplicate(scored, returns_map, max_cands)

        log.info("[%s] 完成: pool=%d, scored=%d, output=%d",
                 self.name, len(pool), len(scored), len(candidates))

        return {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
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

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_overbought_short"

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
                "rsi": ST_W_RSI, "funding": ST_W_FUNDING,
                "bias": ST_W_BIAS, "vol_div": ST_W_VOL_DIV,
                "boll": ST_W_BOLL, "rally": ST_W_RALLY,
                "kdj": ST_W_KDJ, "macd_div": ST_W_MACD_DIV,
                "shadow": ST_W_SHADOW,
                "squeeze_risk": ST_W_SQUEEZE_RISK,
            },
        )


# ══════════════════════════════════════════════════════════
# 长期超买 Skill（1d）
# ══════════════════════════════════════════════════════════

class LongTermOverboughtSkill(_CryptoOverboughtBase):
    """长期超买做空筛选（1d K 线）。

    捕捉日线级别持续上涨后的趋势衰竭，适合波段做空（3天~2周）。
    核心信号：MACD 顶背离 + 日线 BIAS 极端偏离 + 资金费率极端正值。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_overbought_long"

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
                "rsi": LT_W_RSI, "funding": LT_W_FUNDING,
                "bias": LT_W_BIAS, "vol_div": LT_W_VOL_DIV,
                "boll": LT_W_BOLL, "rally": LT_W_RALLY,
                "kdj": LT_W_KDJ, "macd_div": LT_W_MACD_DIV,
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
    fr_display = None
    if funding_rate is not None:
        fr_display = round(funding_rate * 100, 4)
        if funding_rate > FUNDING_RATE_HIGH:
            if funding_rate >= FUNDING_RATE_VERY_EXTREME:
                score += w["funding"]
                signals.append(f"费率={fr_display:.3f}%罕见极端")
            elif funding_rate >= FUNDING_RATE_EXTREME:
                score += w["funding"] * 0.8
                signals.append(f"费率={fr_display:.3f}%极端")
            else:
                ratio = (funding_rate - FUNDING_RATE_HIGH) / (FUNDING_RATE_EXTREME - FUNDING_RATE_HIGH)
                score += w["funding"] * 0.5 * min(1.0, ratio)
                signals.append(f"费率={fr_display:.3f}%偏高")

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
        rally_score += w["rally"] * 0.6 * min(1.0, (rally_pct - rally_pct_thresh) / rally_pct_thresh)
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

    # ── 9. 长上影线 ──
    shadow_score = _score_upper_shadow(closes, opens, highs, lows)
    if shadow_score > 0:
        score += w["shadow"]
        signals.append("长上影线")

    # ── 10. 轧空风险扣分 ──
    squeeze_risk = False
    if oi_value and quote_volume_24h > 0:
        oi_ratio = oi_value / quote_volume_24h
        if quote_volume_24h < SQUEEZE_RISK_QV_THRESHOLD and oi_ratio > SQUEEZE_RISK_OI_RATIO:
            squeeze_risk = True
            score += w["squeeze_risk"]  # 负值，扣分
            signals.append(f"⚠️轧空风险(OI/Vol={oi_ratio:.2f})")

    return {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "bias_20": round(bias_20, 2) if bias_20 is not None else None,
        "consecutive_up": consec,
        "rally_pct": round(rally_pct, 2) if rally_pct is not None else None,
        "above_boll_upper": above_boll,
        "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
        "macd_divergence": macd_div,
        "volume_divergence": vol_div,
        "funding_rate": fr_display,
        "squeeze_risk": squeeze_risk,
        "rise_from_low_pct": round(rise_from_low, 2) if rise_from_low is not None else None,
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
    window = closes[-min(lookback, len(closes)):]
    low = min(window)
    return (closes[-1] - low) / low * 100 if low > 0 else None


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
    closes: List[float], volumes: List[float], lookback: int = 20,
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

    return recent_vol < prior_vol * 0.7


def _calc_kdj_j(
    closes: List[float], highs: List[float], lows: List[float],
    period: int = KDJ_PERIOD, m1: int = KDJ_M1, m2: int = KDJ_M2,
) -> Optional[float]:
    """计算 KDJ 的 J 值。"""
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


def _check_kdj_dead_cross(
    closes: List[float], highs: List[float], lows: List[float],
) -> bool:
    """检测 KDJ 高位死叉：K 下穿 D 且都在 80 以上。"""
    if len(closes) < KDJ_PERIOD + KDJ_M1 + KDJ_M2 + 3:
        return False

    def _calc_kd(c, h, l):
        rsvs = []
        for i in range(KDJ_PERIOD - 1, len(c)):
            hh = max(h[i - KDJ_PERIOD + 1: i + 1])
            ll = min(l[i - KDJ_PERIOD + 1: i + 1])
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

    # K 下穿 D 且都在 80 以上（高位死叉，币圈用 80）
    return k_now < d_now and k_prev >= d_prev and k_now > 50


def _check_macd_top_divergence(closes: List[float], lookback: int = 30) -> bool:
    """检测 MACD 顶背离：价格创新高但 MACD 柱状图未创新高。

    与底背离逻辑镜像：
    1. 在 lookback 窗口内找到两个价格高点
    2. 后一个高点 > 前一个高点（价格创新高）
    3. 后一个高点对应的 MACD histogram < 前一个高点对应的 histogram（动能衰竭）
    """
    macd_data = calc_macd(closes)
    if macd_data.get("histogram") is None or len(closes) < lookback + 10:
        return False

    recent = closes[-lookback:]
    base_idx = len(closes) - lookback

    # 找最近的最高点
    max_idx = max(range(len(recent)), key=lambda i: recent[i])

    # 找前一个高点
    prev_max_idx = None
    for i in range(max(0, max_idx - 5) - 1, -1, -1):
        if prev_max_idx is None or recent[i] > recent[prev_max_idx]:
            prev_max_idx = i

    if prev_max_idx is None or recent[max_idx] <= recent[prev_max_idx]:
        return False

    # 比较两个高点对应的 MACD histogram
    h1 = calc_macd(closes[:base_idx + prev_max_idx + 1]).get("histogram")
    h2 = calc_macd(closes[:base_idx + max_idx + 1]).get("histogram")

    # 顶背离：价格新高但 MACD histogram 更低
    return h1 is not None and h2 is not None and h2 < h1


def _score_upper_shadow(
    closes: List[float], opens: List[float],
    highs: List[float], lows: List[float],
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
