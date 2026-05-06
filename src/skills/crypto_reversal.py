"""
加密货币合约底部放量反转筛选 Skill（双模式）

与超跌反弹（crypto_oversold）的区别：
  超跌反弹 = "接飞刀"，在暴跌过程中抄底
  底部反转 = "确认转向"，等底部构筑完成后入场

核心逻辑：
  下跌趋势 → 底部缩量筑底 → 突然放量（大资金进场）→ 价格企稳不再创新低
  → 均线拐头 → MACD 金叉 → 反转确认

## 短期反转（4h K 线）— 日内/隔日波段
  适用场景：4h 级别底部放量反转，捕捉短期 V 型或 U 型反转
  K 线周期：4h（120 根 ≈ 20 天）
  核心信号：底部放量 + 价格企稳 + 均线拐头 + 资金费率转正
  持仓周期：4h ~ 3 天

## 长期反转（1d K 线）— 波段交易
  适用场景：日线级别底部构筑完成后的趋势反转
  K 线周期：1d（120 根 ≈ 120 天）
  核心信号：底部放量 + MACD 零轴下方金叉 + 均线拐头 + 距底部理想距离
  持仓周期：3 天 ~ 2 周

币圈 vs A 股反转信号的关键差异：
  - 24/7 交易，无涨跌停 → 放量信号更纯粹，不受 T+1 限制
  - 资金费率 → 替代换手率，费率从极端负值回归正常 = 空头平仓 = 反转信号
  - 持仓量变化 → 币圈独有，OI 增加 + 价格企稳 = 新多头建仓
  - 波动率更大 → 距底部理想距离和前期跌幅阈值需放大
  - 无板块联动 → 但有 BTC 相关性（BTC 反转带动山寨币）

九维度评分体系（满分 100）：

### 短期反转（4h）权重分配
  1. 底部放量（18 分）— 核心信号，近期量能 vs 前期地量
  2. 价格企稳（15 分）— 不再创新低 + 波动收窄
  3. 均线拐头（12 分）— EMA5 上穿 EMA10 或 EMA10 拐头向上
  4. 资金费率回归（15 分）— 从极端负值回归正常，空头平仓信号
  5. MACD 反转信号（8 分）— 零轴下方金叉（4h 级别可靠性一般）
  6. 距底部距离（10 分）— 距近期最低点 3%-12% 最佳
  7. 前期跌幅深度（7 分）— 跌得越深反转空间越大
  8. KDJ 低位金叉（8 分）— 超卖区金叉确认
  9. 长下影线（7 分）— 下方有强支撑

### 长期反转（1d）权重分配
  1. 底部放量（15 分）— 日线放量更可靠
  2. 价格企稳（12 分）— 日线级别企稳
  3. 均线拐头（15 分）— 日线均线拐头信号强
  4. 资金费率回归（10 分）— 长期看权重降低
  5. MACD 反转信号（15 分）— 日线 MACD 金叉/底背离可靠性高
  6. 距底部距离（10 分）— 距近期最低点 5%-15% 最佳
  7. 前期跌幅深度（8 分）— 中期跌幅
  8. KDJ 低位金叉（8 分）— 日线 KDJ 金叉
  9. 长下影线（7 分）— 日线长下影线

数据源：BinancePublicClient（K 线优先走本地 SQLite 缓存）
"""

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
    ATR_PERIOD,
    ATR_PERIOD_FILTER,
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 共享常量
# ══════════════════════════════════════════════════════════

KDJ_PERIOD = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 资金费率阈值
FUNDING_RATE_EXTREME = -0.001  # -0.1%，极端负值
FUNDING_RATE_NORMAL = 0.0001  # +0.01%，正常水平

# ══════════════════════════════════════════════════════════
# 短期反转参数（4h K 线）
# ══════════════════════════════════════════════════════════

ST_INTERVAL = "4h"
ST_MIN_KLINES = 60
ST_BOTTOM_LOOKBACK = 30  # 近期最低点回看 30 根 4h = 5 天
ST_PRICE_STABLE_WINDOW = 6  # 企稳观察窗口 6 根 4h = 1 天
ST_DROP_LOOKBACK = 42  # 前期跌幅回看 42 根 4h = 7 天
ST_VOLUME_SURGE_THRESHOLD = 2.0  # 放量倍数阈值
ST_VOLUME_SURGE_STRONG = 3.5  # 强放量（币圈波动大，阈值更高）
ST_DIST_BOTTOM_IDEAL_MIN = 3.0  # 距底部理想距离下限（%）
ST_DIST_BOTTOM_IDEAL_MAX = 12.0  # 距底部理想距离上限（%，币圈波动大）
ST_SHADOW_RATIO_THRESHOLD = 2.0  # 下影线长度 / 实体长度 ≥ 2 倍

# 短期评分权重（满分 100）— 回测优化后 v2
# 回测结论（200币 × 6个月，持有 18 根 4h）：
#   弱有效：底部放量(+0.24%) 距底部距离(+0.17%) KDJ金叉(+0.21%) 前期跌幅(+0.06%)
#   反效果：价格企稳(-0.54%) MACD反转(-0.71%) 长下影线(-0.36%) 均线拐头(-0.31%)
#   无数据：资金费率（实盘有效，保留高权重）
ST_W_VOLUME_SURGE = 22  # 底部放量（弱有效，升权 18→22，是最可靠的信号）
ST_W_PRICE_STABLE = 6  # 价格企稳（反效果，降权 15→6，保留少量因为逻辑上合理）
ST_W_MA_TURN = 5  # 均线拐头（反效果，降权 12→5）
ST_W_FUNDING = 20  # 资金费率回归（实盘有效，升权 15→20）
ST_W_MACD_REVERSAL = 3  # MACD 反转（最差维度，降权 8→3）
ST_W_DIST_BOTTOM = 14  # 距底部距离（弱有效，升权 10→14）
ST_W_PRIOR_DROP = 12  # 前期跌幅深度（弱有效，升权 7→12）
ST_W_KDJ_CROSS = 13  # KDJ 低位金叉（弱有效，升权 8→13）
ST_W_SHADOW = 3  # 长下影线（反效果，降权 7→3）
# 总计：22+6+5+20+3+14+12+13+3 = 98（资金费率实盘补足）

# ══════════════════════════════════════════════════════════
# 超短期反转参数（1h K 线）
# ══════════════════════════════════════════════════════════

H1_INTERVAL = "1h"
H1_MIN_KLINES = 80
H1_BOTTOM_LOOKBACK = 72  # 近期最低点回看 72 根 1h = 3 天
H1_PRICE_STABLE_WINDOW = 12  # 企稳观察窗口 12 根 1h = 12 小时（从 8 收紧）
H1_DROP_LOOKBACK = 120  # 前期跌幅回看 120 根 1h = 5 天
H1_VOLUME_SURGE_THRESHOLD = (
    3.0  # 放量倍数阈值（回测显示 1h 放量是反效果，放宽 4.5→3.0 仅作评分参考）
)
H1_VOLUME_SURGE_STRONG = 5.0  # 强放量（从 6.5 放宽到 5.0）
H1_DIST_BOTTOM_IDEAL_MIN = 1.5  # 距底部理想距离下限（%，从 2.0 放宽，给 1h 波动留空间）
H1_DIST_BOTTOM_IDEAL_MAX = (
    6.0  # 距底部理想距离上限（%，从 4.0 放宽，2% 窗口对 1h 太窄）
)
H1_SHADOW_RATIO_THRESHOLD = 2.5  # 下影线长度 / 实体长度 ≥ 2.5 倍

# 超短期评分权重 — 回测优化后 v2
# 回测结论（200币 × 6个月，持有 24 根 1h）：
#   有效：KDJ金叉(+0.33%)
#   弱有效：均线拐头(+0.13%) MACD反转(+0.05%) 距底部距离(+0.01%) 前期跌幅(+0.16%) 长下影线(+0.17%)
#   反效果：底部放量(-1.29%) 价格企稳(-0.06%)
#   无数据：资金费率（实盘有效，保留）
H1_W_VOLUME_SURGE = 8  # 底部放量（1h 最差维度 -1.29%，大幅降权 25→8）
H1_W_PRICE_STABLE = 8  # 价格企稳（反效果，降权 15→8）
H1_W_MA_TURN = 14  # 均线拐头（弱有效，升权 12→14）
H1_W_FUNDING = 20  # 资金费率回归（实盘有效，升权 15→20）
H1_W_MACD_REVERSAL = 8  # MACD 反转（弱有效，升权 3→8）
H1_W_DIST_BOTTOM = 10  # 距底部距离（弱有效，升权 8→10）
H1_W_PRIOR_DROP = 12  # 前期跌幅深度（弱有效，升权 10→12）
H1_W_KDJ_CROSS = 15  # KDJ 低位金叉（唯一有效维度，大幅升权 7→15）
H1_W_SHADOW = 5  # 长下影线（弱有效，保持）
# 总计：8+8+14+20+8+10+12+15+5 = 100

# ══════════════════════════════════════════════════════════
# 长期反转参数（1d K 线）
# ══════════════════════════════════════════════════════════

LT_INTERVAL = "1d"
LT_MIN_KLINES = 60
LT_BOTTOM_LOOKBACK = 30  # 近期最低点回看 30 天
LT_PRICE_STABLE_WINDOW = 5  # 企稳观察窗口 5 天
LT_DROP_LOOKBACK = 45  # 前期跌幅回看 45 天
LT_VOLUME_SURGE_THRESHOLD = 1.8  # 日线放量阈值（比 4h 略低）
LT_VOLUME_SURGE_STRONG = 3.0  # 日线强放量
LT_DIST_BOTTOM_IDEAL_MIN = 5.0  # 距底部理想距离下限（%）
LT_DIST_BOTTOM_IDEAL_MAX = 15.0  # 距底部理想距离上限（%，日线级别更宽）
LT_SHADOW_RATIO_THRESHOLD = 2.0

# 长期评分权重（满分 100）
LT_W_VOLUME_SURGE = 15  # 底部放量
LT_W_PRICE_STABLE = 12  # 价格企稳
LT_W_MA_TURN = 15  # 均线拐头（日线级别信号强）
LT_W_FUNDING = 10  # 资金费率回归（长期看权重降低）
LT_W_MACD_REVERSAL = 15  # MACD 反转信号（日线级别可靠性高）
LT_W_DIST_BOTTOM = 10  # 距底部距离
LT_W_PRIOR_DROP = 8  # 前期跌幅深度
LT_W_KDJ_CROSS = 8  # KDJ 低位金叉
LT_W_SHADOW = 7  # 长下影线

DEFAULT_MIN_QUOTE_VOLUME = 10_000_000
DEFAULT_MIN_REVERSAL_SCORE = 55  # 回测优化最优值：胜率60.3%，均收益+0.91%
DEFAULT_MAX_CANDIDATES = 10

# 反转策略黑名单：反复亏损币种（亏损≥2笔 or 合计亏损过大）直接排除，不再反向追
REVERSAL_BLACKLIST = {
    "ENJUSDT",  # 2笔合计 -9.0，场均 -12.3，极端亏损
    "ORCAUSDT",  # 单笔亏损 -16.3，做空超跌均失败，极端风险
    "SOONUSDT",  # 2笔合计 -3.6，流动性差
    "CHZUSDT",  # 4h策略4笔合计 -6.95，2赢2输但亏的均值 -7.5 远大于赢的均值 +4.0
    "TRUMPUSDT",  # MEME/政治币，庄家控盘
    "PIPPINUSDT",  # 流动性极差，多次重复开仓
    "LABUBUSDT",  # 强庄MEME币
}


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════


class _CryptoReversalBase(BaseSkill):
    """底部反转筛选共享基类。"""

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
            if base in exclude_bases or not re.match(r"^[A-Z0-9]{2,15}$", base):
                continue
            # 黑名单币种直接排除（反复亏损币）
            if sym in REVERSAL_BLACKLIST:
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
        判断当前市场是否适合做趋势反转。

        BTC 短期暴跌时阻断（瀑布式下跌中的反弹是假信号）。
        """
        if input_data.get("ignore_market_regime"):
            return {"status": "enabled", "reason": "ignore_market_regime=true"}

        symbol = "BTCUSDT"
        try:
            klines = self._fetch_klines(symbol, "4h", 80)
        except Exception as exc:
            log.warning("[%s] 市场状态获取失败: %s", self.name, exc)
            return {"status": "unknown", "reason": f"fetch_failed:{exc}"}

        if not klines or len(klines) < 60:
            return {"status": "unknown", "reason": "insufficient_market_klines"}

        closes = [float(k[4]) for k in klines]
        last_close = closes[-1]
        lookback = 6
        recent_return_pct = (
            (last_close - closes[-lookback]) / closes[-lookback] * 100
            if len(closes) > lookback and closes[-lookback] > 0
            else 0.0
        )
        if recent_return_pct <= -5.0:
            return {
                "status": "blocked",
                "reason": f"BTC 短期暴跌 {recent_return_pct:.2f}%，反转信号不可靠",
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
        self,
        input_data: dict,
        interval: str,
        min_klines: int,
        bottom_lookback: int,
        price_stable_window: int,
        drop_lookback: int,
        vol_thresh: float,
        vol_strong: float,
        dist_min: float,
        dist_max: float,
        shadow_ratio: float,
        weights: dict,
    ) -> dict:
        """通用扫描流程，短期/长期共用。"""
        min_qv = input_data.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
        min_score = input_data.get("min_reversal_score", DEFAULT_MIN_REVERSAL_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")

        pipeline_run_id = str(uuid.uuid4())

        tickers = self._client.get_tickers_24hr()
        total_count = len(tickers)
        funding_map = self._build_funding_map()
        tradable = self._get_tradable_symbols()
        market_regime = self._get_market_regime(input_data)
        if market_regime.get("status") == "blocked":
            log.warning(
                "[%s] 市场状态阻断反转交易: %s",
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
                    "after_reversal_filter": 0,
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
                kline_need = max(KLINE_LIMIT, bottom_lookback + drop_lookback + 20)
                klines = self._fetch_klines(symbol, interval, kline_need)
                if not klines or len(klines) < min_klines:
                    continue

                closes = [float(k[4]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                opens = [float(k[1]) for k in klines]
                volumes = [float(k[5]) for k in klines]
                fr = funding_map.get(symbol)

                # ── 实时价格组合分析 ──────────────────────────────────────
                # 在已关闭 K 线形态判断基础上，叠加当前实时价格变动
                # 判断：形态形成后价格是否已走完（追高风险），或仍在合理区间（机会）
                last_closed_close = closes[-1]
                current_price = float(item.get("lastPrice", 0))
                price_change_since_close_pct = (
                    (current_price - last_closed_close) / last_closed_close * 100
                    if current_price > 0 and last_closed_close > 0
                    else 0.0
                )
                # 实时 RSI 估算：用当前价扩展 closes，计算当前超买/超卖状态
                closes_for_rsi = closes[:-1] + [current_price]
                current_rsi = (
                    calc_rsi(closes_for_rsi, RSI_PERIOD) if current_price > 0 else None
                )
                # ── 1h 增强：用真实 1h K 线替代推算 ──────────────────────
                # 1h K 线数量 = 当前 4h 周期内已走完的 1h 蜡烛数（精准定位周期位置）
                # 同时获取 1h RSI（比 4h RSI 更及时）
                interval_ms = 4 * 3600 * 1000
                now_ms = int(time.time() * 1000)
                current_candle_open = (now_ms // interval_ms) * interval_ms
                last_closed_open = klines[-1][0]
                elapsed_ratio = (
                    min(1.0, (now_ms - last_closed_open) / interval_ms)
                    if last_closed_open >= current_candle_open
                    and last_closed_open < now_ms
                    else 1.0
                )
                # 精准进度：用 1h K 线计数
                try:
                    klines_1h = self._fetch_klines(
                        symbol, "1h", 20
                    )  # 至少需要 14 根算 RSI
                    if klines_1h:
                        last_1h_open = klines_1h[-1][0]
                        # 当前 4h 周期内有多少根 1h K 线已关闭
                        hour_candles_in_4h = sum(
                            1
                            for k in klines_1h
                            if last_closed_open
                            <= float(k[0])
                            < (last_closed_open + interval_ms)
                        )
                        elapsed_ratio_precise = min(1.0, hour_candles_in_4h / 4.0)
                        # 1h RSI：用最后 3 根 1h 关闭 + 当前价估算（比 4h RSI 领先约 1-3h）
                        closes_1h = [float(k[4]) for k in klines_1h]
                        rsi_1h_raw = (
                            calc_rsi(closes_1h[:-1] + [current_price], RSI_PERIOD)
                            if current_price > 0
                            else None
                        )
                        rsi_1h = rsi_1h_raw
                        # 1h 成交量强度：当前 4h 周期内均量 vs 历史 4h 均量
                        vol_1h_recent = [float(k[5]) for k in klines_1h[-4:]]
                        avg_vol_1h = sum(vol_1h_recent) / len(vol_1h_recent)
                        vol_4h_hist = sum(volumes[-8:-1]) / 7
                        vol_intraday_strength = (
                            avg_vol_1h / vol_4h_hist if vol_4h_hist > 0 else 1.0
                        )
                    else:
                        hour_candles_in_4h = int(elapsed_ratio * 4)
                        elapsed_ratio_precise = elapsed_ratio
                        rsi_1h = None
                        vol_intraday_strength = 1.0
                except Exception:
                    hour_candles_in_4h = int(elapsed_ratio * 4)
                    elapsed_ratio_precise = elapsed_ratio
                    rsi_1h = None
                    vol_intraday_strength = 1.0
                # 用精准进度重新估算盘中成交量
                avg_hist_volume = (
                    sum(volumes[-6:-1]) / 5
                    if len(volumes) >= 6
                    else sum(volumes[-3:]) / max(1, len(volumes) - 1)
                )
                current_volume_estimate = (
                    volumes[-1] / elapsed_ratio_precise
                    if elapsed_ratio_precise > 0
                    else volumes[-1]
                )
                volume_so_far_ratio = (
                    current_volume_estimate / avg_hist_volume
                    if avg_hist_volume > 0
                    else 1.0
                )
                # ── 动能过滤（改为减分因子，不硬跳过）───
                # 4h 收盘后价格已大幅上涨 → 形态"走出来"了，追入风险高但不该直接拒绝
                # 4h 收盘后价格横盘/小回调 → 二次确认机会，更安全，给高分
                # 用减分代替跳过：保留机会但降低优先级
                _momentum_chase_thresh = 5.0 if interval == "4h" else 3.0
                _momentum_drop_thresh = -5.0 if interval == "4h" else -3.0
                momentum_penalty = 0.0
                if price_change_since_close_pct > _momentum_chase_thresh:
                    # 超过阈值越多，扣分越多（最多扣15分）
                    excess = price_change_since_close_pct - _momentum_chase_thresh
                    momentum_penalty = min(15.0, excess * 3.0)
                    log.info(
                        "[%s] %s 4h 收盘后已涨 %.2f%%，动能追高扣分 %.1f",
                        self.name,
                        symbol,
                        price_change_since_close_pct,
                        momentum_penalty,
                    )
                elif price_change_since_close_pct < _momentum_drop_thresh:
                    # 收盘后继续大跌 → 底部未稳，扣分（最多扣10分）
                    deficit = abs(price_change_since_close_pct) - abs(
                        _momentum_drop_thresh
                    )
                    momentum_penalty = min(10.0, deficit * 2.0)
                    log.info(
                        "[%s] %s 4h 收盘后继续跌 %.2f%%，底部未稳扣分 %.1f",
                        self.name,
                        symbol,
                        price_change_since_close_pct,
                        momentum_penalty,
                    )

                result = calc_reversal_score(
                    closes,
                    highs,
                    lows,
                    opens,
                    volumes,
                    fr,
                    bottom_lookback,
                    price_stable_window,
                    drop_lookback,
                    vol_thresh,
                    vol_strong,
                    dist_min,
                    dist_max,
                    shadow_ratio,
                    weights,
                )

                # ── 动能减分：应用到最终评分 ────────────────────────────────
                if momentum_penalty > 0:
                    result["reversal_score"] = max(
                        1, result["reversal_score"] - momentum_penalty
                    )
                    result["momentum_penalty"] = momentum_penalty

                # ── 1h RSI 先行信号加分 ──────────────────────────────────────
                # 1h RSI 已率先站上 50 轴，但 4h RSI 尚未同步强势
                # 说明盘中已有资金介入，比 4h 信号领先 1-3h
                if rsi_1h is not None and current_rsi is not None:
                    if 50 <= rsi_1h < 60:
                        result["reversal_score"] += 4
                        result["rsi_1h_bonus"] = 4
                    elif 60 <= rsi_1h < 70:
                        result["reversal_score"] += 5
                        result["rsi_1h_bonus"] = 5
                    elif rsi_1h >= 70:
                        result["reversal_score"] -= 3
                        result["rsi_1h_bonus"] = -3

                if result["reversal_score"] < min_score and not target_symbols:
                    continue

                # 1h 模式放量软过滤：回测显示 1h 放量是反效果维度（-1.29%），
                # 不再作为硬门槛，改为无放量时扣分（评分体系已通过低权重 8 分处理）
                # 保留日志便于观察
                if (
                    interval == "1h"
                    and result.get("volume_surge_ratio", 0) < vol_thresh
                ):
                    log.debug(
                        "[%s] %s 1h 无放量信号 (ratio=%.1f < %.1f)，不再硬性过滤",
                        self.name,
                        symbol,
                        result.get("volume_surge_ratio", 0),
                        vol_thresh,
                    )

                # 追高过滤：24h 涨幅过大说明反转行情已走大半，此时做多是追高
                # 收紧追高阈值：4h 25%→15%，1h 15%→12%（减少追涨被套）
                _price_change_pct = float(item.get("priceChangePercent", 0))
                _chase_threshold = 12.0 if interval == "1h" else 15.0
                if _price_change_pct > _chase_threshold and not target_symbols:
                    log.info(
                        "[%s] %s 24h 涨幅 %.2f%% 超过 %.0f%%，跳过（追高风险）",
                        self.name,
                        symbol,
                        _price_change_pct,
                        _chase_threshold,
                    )
                    continue

                # 1h 模式底部确认：强制执行（三选二），不只是特定条件才触发
                # 原逻辑要求 dist_bottom 在 2%~4% AND 技术确认，窗口太窄导致几乎无币通过
                # 新逻辑：以下三个条件满足至少两个即可
                #   a) 距底部在理想区间（1.5%~6%，已放宽）
                #   b) KDJ 低位金叉（回测最有效维度 +0.33%）
                #   c) MACD 底背离或零轴下方金叉
                # 仍保留最低安全线：dist_bottom 必须 ≥ 1%（防止接刀）
                if interval == "1h":
                    dist_bottom_pct = result.get("dist_bottom_pct")
                    # 安全线：反弹不足 1% 说明还在下跌途中，直接跳过
                    if dist_bottom_pct is None or dist_bottom_pct < 1.0:
                        log.info(
                            "[%s] %s 反弹不足 1%%，跳过接刀: dist_bottom=%.2f%%",
                            self.name,
                            symbol,
                            dist_bottom_pct if dist_bottom_pct is not None else 0.0,
                        )
                        continue
                    cond_dist = dist_min <= dist_bottom_pct <= dist_max
                    cond_kdj = result.get("kdj_score", 0) > 0
                    cond_macd = result.get("macd_reversal_score", 0) > 0
                    confirm_count = sum([cond_dist, cond_kdj, cond_macd])
                    if confirm_count < 2:
                        log.info(
                            "[%s] %s 底部确认不足（%d/3）: dist=%.2f%%(%s) kdj=%s macd=%s",
                            self.name,
                            symbol,
                            confirm_count,
                            dist_bottom_pct,
                            cond_dist,
                            cond_kdj,
                            cond_macd,
                        )
                        continue

                # 4h 模式底部确认：至少满足2个条件（比1h宽松）
                # 4h 策略已有盈利，但CHZ等币亏损较大，加底部确认减少接刀
                # 条件：KDJ金叉 OR MACD底背离 OR 距底部在理想区间
                if interval == "4h" and not target_symbols:
                    dist_bottom_pct = result.get("dist_bottom_pct")
                    if (
                        dist_bottom_pct is None or dist_bottom_pct < 2.0
                    ):  # 4h安全线略宽：2%
                        log.info(
                            "[%s] %s 4h反弹不足2%%，跳过接刀: dist_bottom=%.2f%%",
                            self.name,
                            symbol,
                            dist_bottom_pct if dist_bottom_pct is not None else 0.0,
                        )
                        continue
                    cond_dist_4h = 3.0 <= dist_bottom_pct <= 12.0
                    cond_kdj_4h = result.get("kdj_score", 0) > 0
                    cond_macd_4h = result.get("macd_reversal_score", 0) > 0
                    confirm_count_4h = sum([cond_dist_4h, cond_kdj_4h, cond_macd_4h])
                    if confirm_count_4h < 2:
                        log.info(
                            "[%s] %s 4h底部确认不足（%d/3）: dist=%.2f%% kdj=%s macd=%s",
                            self.name,
                            symbol,
                            confirm_count_4h,
                            dist_bottom_pct,
                            cond_kdj_4h,
                            cond_macd_4h,
                        )
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

                scored.append(
                    {
                        "symbol": symbol,
                        "close": last_close,
                        "current_price": current_price,
                        "quote_volume_24h": item.get("quoteVolume", 0),
                        "price_change_pct": item.get("priceChangePercent", 0),
                        "reversal_score": result["reversal_score"],
                        # ── 实时价格组合分析字段 ───────────────────────────────
                        "price_change_since_close_pct": round(
                            price_change_since_close_pct, 2
                        ),
                        "current_rsi": round(current_rsi, 1)
                        if current_rsi is not None
                        else None,
                        "volume_so_far_ratio": round(volume_so_far_ratio, 2),
                        # ── 1h 增强字段 ──────────────────────────────────────
                        "rsi_1h": round(rsi_1h, 1) if rsi_1h is not None else None,
                        "vol_intraday_strength": round(vol_intraday_strength, 2),
                        "hour_candles_in_4h": hour_candles_in_4h,
                        "elapsed_ratio_precise": round(elapsed_ratio_precise, 2),
                        "rsi_1h_bonus": result.get("rsi_1h_bonus"),
                        "momentum_penalty": result.get("momentum_penalty"),
                        # ── 形态评分 ─────────────────────────────────────────
                        "volume_surge_score": result["volume_surge_score"],
                        "volume_surge_ratio": result["volume_surge_ratio"],
                        "price_stable_score": result["price_stable_score"],
                        "ma_turn_score": result["ma_turn_score"],
                        "ma_turn_detail": result["ma_turn_detail"],
                        "funding_reversal_score": result["funding_reversal_score"],
                        "funding_rate": result["funding_rate"],
                        "macd_reversal_score": result["macd_reversal_score"],
                        "macd_detail": result["macd_detail"],
                        "dist_bottom_pct": result["dist_bottom_pct"],
                        "dist_bottom_score": result["dist_bottom_score"],
                        "prior_drop_pct": result["prior_drop_pct"],
                        "prior_drop_score": result["prior_drop_score"],
                        "kdj_score": result["kdj_score"],
                        "shadow_score": result["shadow_score"],
                        "signal_details": result["signal_details"],
                        "atr_pct": atr_pct,
                        "atr_filter_pct": atr_filter_pct,
                        "signal_direction": "long",
                        "strategy_tag": self.name,
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception as exc:
                log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)

        scored.sort(key=lambda x: x["reversal_score"], reverse=True)
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
                "after_reversal_filter": len(scored),
                "output_count": len(candidates),
            },
        }


# ══════════════════════════════════════════════════════════
# 短期反转 Skill（4h）
# ══════════════════════════════════════════════════════════


class ShortTermReversalSkill(_CryptoReversalBase):
    """短期底部放量反转筛选（4h K 线）。

    捕捉 4h 级别底部放量后的反转确认，适合日内/隔日波段。
    核心信号：底部放量 + 价格企稳 + 均线拐头 + 资金费率回归。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_reversal_4h"

    def run(self, input_data: dict) -> dict:
        # 4h 门槛：55（回测优化最优值）
        if "min_reversal_score" not in input_data:
            input_data = {**input_data, "min_reversal_score": 55}
        return self._run_scan(
            input_data,
            interval=ST_INTERVAL,
            min_klines=ST_MIN_KLINES,
            bottom_lookback=ST_BOTTOM_LOOKBACK,
            price_stable_window=ST_PRICE_STABLE_WINDOW,
            drop_lookback=ST_DROP_LOOKBACK,
            vol_thresh=ST_VOLUME_SURGE_THRESHOLD,
            vol_strong=ST_VOLUME_SURGE_STRONG,
            dist_min=ST_DIST_BOTTOM_IDEAL_MIN,
            dist_max=ST_DIST_BOTTOM_IDEAL_MAX,
            shadow_ratio=ST_SHADOW_RATIO_THRESHOLD,
            weights={
                "volume_surge": ST_W_VOLUME_SURGE,
                "price_stable": ST_W_PRICE_STABLE,
                "ma_turn": ST_W_MA_TURN,
                "funding": ST_W_FUNDING,
                "macd_reversal": ST_W_MACD_REVERSAL,
                "dist_bottom": ST_W_DIST_BOTTOM,
                "prior_drop": ST_W_PRIOR_DROP,
                "kdj_cross": ST_W_KDJ_CROSS,
                "shadow": ST_W_SHADOW,
            },
        )


# ══════════════════════════════════════════════════════════
# 超短期反转 Skill（1h）
# ══════════════════════════════════════════════════════════


class HourlyReversalSkill(_CryptoReversalBase):
    """超短期底部放量反转筛选（1h K 线）。

    捕捉小时级别底部放量后的快速反转，适合 4h~24h 持仓。
    核心信号：底部放量 + 价格企稳 + KDJ 金叉 + 资金费率回归。
    比 4h 模式更敏感，放量要求更高以过滤噪音。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_reversal_1h"

    def run(self, input_data: dict) -> dict:
        # 1h 门槛：70（1h策略57笔亏损-21，4h策略27笔盈利+47，主因是1h信号质量差）
        if "min_reversal_score" not in input_data:
            input_data = {**input_data, "min_reversal_score": 70}
        return self._run_scan(
            input_data,
            interval=H1_INTERVAL,
            min_klines=H1_MIN_KLINES,
            bottom_lookback=H1_BOTTOM_LOOKBACK,
            price_stable_window=H1_PRICE_STABLE_WINDOW,
            drop_lookback=H1_DROP_LOOKBACK,
            vol_thresh=H1_VOLUME_SURGE_THRESHOLD,
            vol_strong=H1_VOLUME_SURGE_STRONG,
            dist_min=H1_DIST_BOTTOM_IDEAL_MIN,
            dist_max=H1_DIST_BOTTOM_IDEAL_MAX,
            shadow_ratio=H1_SHADOW_RATIO_THRESHOLD,
            weights={
                "volume_surge": H1_W_VOLUME_SURGE,
                "price_stable": H1_W_PRICE_STABLE,
                "ma_turn": H1_W_MA_TURN,
                "funding": H1_W_FUNDING,
                "macd_reversal": H1_W_MACD_REVERSAL,
                "dist_bottom": H1_W_DIST_BOTTOM,
                "prior_drop": H1_W_PRIOR_DROP,
                "kdj_cross": H1_W_KDJ_CROSS,
                "shadow": H1_W_SHADOW,
            },
        )


# ══════════════════════════════════════════════════════════
# 长期反转 Skill（1d）
# ══════════════════════════════════════════════════════════


class LongTermReversalSkill(_CryptoReversalBase):
    """长期底部放量反转筛选（1d K 线）。

    捕捉日线级别底部构筑完成后的趋势反转，适合波段交易（3天~2周）。
    核心信号：底部放量 + MACD 零轴下方金叉/底背离 + 均线拐头 + 距底部理想距离。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "crypto_reversal_1d"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(
            input_data,
            interval=LT_INTERVAL,
            min_klines=LT_MIN_KLINES,
            bottom_lookback=LT_BOTTOM_LOOKBACK,
            price_stable_window=LT_PRICE_STABLE_WINDOW,
            drop_lookback=LT_DROP_LOOKBACK,
            vol_thresh=LT_VOLUME_SURGE_THRESHOLD,
            vol_strong=LT_VOLUME_SURGE_STRONG,
            dist_min=LT_DIST_BOTTOM_IDEAL_MIN,
            dist_max=LT_DIST_BOTTOM_IDEAL_MAX,
            shadow_ratio=LT_SHADOW_RATIO_THRESHOLD,
            weights={
                "volume_surge": LT_W_VOLUME_SURGE,
                "price_stable": LT_W_PRICE_STABLE,
                "ma_turn": LT_W_MA_TURN,
                "funding": LT_W_FUNDING,
                "macd_reversal": LT_W_MACD_REVERSAL,
                "dist_bottom": LT_W_DIST_BOTTOM,
                "prior_drop": LT_W_PRIOR_DROP,
                "kdj_cross": LT_W_KDJ_CROSS,
                "shadow": LT_W_SHADOW,
            },
        )


# 向后兼容
CryptoReversalSkill = ShortTermReversalSkill


# ══════════════════════════════════════════════════════════
# 九维度反转评分（纯函数，短期/长期共用）
# ══════════════════════════════════════════════════════════


def calc_reversal_score(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    opens: List[float],
    volumes: List[float],
    funding_rate: Optional[float],
    bottom_lookback: int,
    price_stable_window: int,
    drop_lookback: int,
    vol_thresh: float,
    vol_strong: float,
    dist_min: float,
    dist_max: float,
    shadow_ratio: float,
    weights: dict,
) -> dict:
    """计算底部反转综合评分（满分 100）。"""
    signals = []
    last_close = closes[-1]
    w = weights

    # ── 1. 底部放量 ──
    # 核心信号：近 3 根均量 vs 前 15 根均量（排除最近 3 根）
    vol_surge_ratio = 0.0
    vol_surge_score = 0.0
    if len(volumes) >= 20:
        recent_avg = sum(volumes[-3:]) / 3
        base_avg = sum(volumes[-18:-3]) / 15
        if base_avg > 0:
            vol_surge_ratio = recent_avg / base_avg
            if vol_surge_ratio >= vol_strong:
                vol_surge_score = w["volume_surge"]
                signals.append(f"强放量{vol_surge_ratio:.1f}x")
            elif vol_surge_ratio >= vol_thresh:
                vol_surge_score = (
                    w["volume_surge"] * (vol_surge_ratio - 1.0) / (vol_strong - 1.0)
                )
                signals.append(f"放量{vol_surge_ratio:.1f}x")

    # ── 2. 价格企稳 ──
    # 近 N 根不再创新低 + 波动收窄
    price_stable_score = 0.0
    if len(closes) >= bottom_lookback + price_stable_window:
        recent_low = min(lows[-price_stable_window:])
        prior_low = min(
            lows[-(bottom_lookback + price_stable_window) : -price_stable_window]
        )
        # 近期最低价高于前期最低价 = 不再创新低（允许 1.5% 误差，币圈波动大）
        if recent_low >= prior_low * 0.985:
            price_stable_score += w["price_stable"] * 0.55
            signals.append("不再创新低")
        # 近期振幅收窄
        recent_range = max(highs[-price_stable_window:]) - min(
            lows[-price_stable_window:]
        )
        prior_range = (
            max(highs[-15:-5]) - min(lows[-15:-5])
            if len(highs) >= 15
            else recent_range * 2
        )
        if prior_range > 0 and recent_range < prior_range * 0.7:
            price_stable_score += w["price_stable"] * 0.45
            signals.append("波动收窄")
    price_stable_score = min(price_stable_score, float(w["price_stable"]))

    # ── 3. 均线拐头 ──
    ma_turn_score, ma_turn_detail = _score_ma_turn(closes, w["ma_turn"])
    if ma_turn_detail:
        signals.append(ma_turn_detail)

    # ── 4. 资金费率回归（币圈独有）──
    # 从极端负值回归正常 = 空头平仓 = 反转信号
    funding_reversal_score = 0.0
    fr_display = None
    if funding_rate is not None:
        fr_display = round(funding_rate * 100, 4)
        # 费率从负值回归到正常区间 = 空头平仓完成
        if FUNDING_RATE_EXTREME < funding_rate <= FUNDING_RATE_NORMAL:
            # 刚从极端负值回归，反转信号最强
            funding_reversal_score = w["funding"] * 0.7
            signals.append(f"费率回归{fr_display:.3f}%")
        elif funding_rate > FUNDING_RATE_NORMAL and funding_rate < 0.0005:
            # 费率已转正但不过高 = 多头开始占优
            funding_reversal_score = w["funding"]
            signals.append(f"费率转正{fr_display:.3f}%")
        elif funding_rate <= FUNDING_RATE_EXTREME:
            # 仍在极端负值 = 还没反转，但有潜力
            ratio = min(1.0, abs(funding_rate) / 0.005)
            funding_reversal_score = w["funding"] * 0.3 * ratio
            signals.append(f"费率极端{fr_display:.3f}%(待反转)")

    # ── 5. MACD 反转信号 ──
    macd_score, macd_detail = _score_macd_reversal(closes, w["macd_reversal"])
    if macd_detail:
        signals.append(macd_detail)

    # ── 6. 距底部距离 ──
    dist_bottom_pct = None
    dist_score = 0.0
    if len(lows) >= bottom_lookback:
        bottom = min(lows[-bottom_lookback:])
        if bottom > 0:
            dist_bottom_pct = (last_close - bottom) / bottom * 100
            if dist_min <= dist_bottom_pct <= dist_max:
                dist_score = w["dist_bottom"]
                signals.append(f"距底部{dist_bottom_pct:.1f}%(理想)")
            elif 0 < dist_bottom_pct < dist_min:
                dist_score = w["dist_bottom"] * 0.4  # 太近，可能还没企稳
            elif dist_max < dist_bottom_pct <= dist_max * 2:
                dist_score = w["dist_bottom"] * 0.3  # 稍远，但还行

    # ── 7. 前期跌幅深度 ──
    prior_drop_pct = None
    prior_drop_score = 0.0
    if len(closes) >= drop_lookback + 1:
        base = max(closes[-(drop_lookback + 1) : -price_stable_window])
        if base > 0:
            prior_drop_pct = (last_close - base) / base * 100
            # 币圈波动大，跌 25% 以上才算深度回调
            if prior_drop_pct < -25:
                prior_drop_score = w["prior_drop"]
                signals.append(f"前期跌{prior_drop_pct:.1f}%")
            elif prior_drop_pct < -15:
                prior_drop_score = w["prior_drop"] * 0.6

    # ── 8. KDJ 低位金叉 ──
    kdj_score = _score_kdj_golden_cross(closes, highs, lows, w["kdj_cross"])
    if kdj_score > 0:
        signals.append("KDJ低位金叉")

    # ── 9. 长下影线 ──
    shadow_score = _score_lower_shadow(
        closes, opens, highs, lows, shadow_ratio, w["shadow"]
    )
    if shadow_score > 0:
        signals.append("长下影线")

    total = (
        vol_surge_score
        + price_stable_score
        + ma_turn_score
        + funding_reversal_score
        + macd_score
        + dist_score
        + prior_drop_score
        + kdj_score
        + shadow_score
    )

    return {
        "reversal_score": round(total),
        "volume_surge_score": round(vol_surge_score),
        "volume_surge_ratio": round(vol_surge_ratio, 2),
        "price_stable_score": round(price_stable_score),
        "ma_turn_score": round(ma_turn_score),
        "ma_turn_detail": ma_turn_detail,
        "funding_reversal_score": round(funding_reversal_score),
        "funding_rate": fr_display,
        "macd_reversal_score": round(macd_score),
        "macd_detail": macd_detail,
        "dist_bottom_pct": round(dist_bottom_pct, 2)
        if dist_bottom_pct is not None
        else None,
        "dist_bottom_score": round(dist_score),
        "prior_drop_pct": round(prior_drop_pct, 2)
        if prior_drop_pct is not None
        else None,
        "prior_drop_score": round(prior_drop_score),
        "kdj_score": round(kdj_score),
        "shadow_score": round(shadow_score),
        "signal_details": " | ".join(signals) if signals else "无反转信号",
    }


# ══════════════════════════════════════════════════════════
# 子维度评分函数
# ══════════════════════════════════════════════════════════


def _score_ma_turn(closes: List[float], max_score: float) -> tuple:
    """均线拐头评分。

    使用 EMA 而非 SMA，币圈波动大 EMA 响应更快。
    - EMA5 上穿 EMA10（金叉）：满分
    - EMA5 拐头向上（但还在 EMA10 下方）：满分 * 0.7
    - EMA10 拐头向上：满分 * 0.5
    """
    if len(closes) < 15:
        return 0.0, ""

    ema5_series = calc_ema(closes, 5)
    ema10_series = calc_ema(closes, 10)

    if len(ema5_series) < 4 or len(ema10_series) < 4:
        return 0.0, ""

    ema5_now = ema5_series[-1]
    ema5_3ago = ema5_series[-4]
    ema10_now = ema10_series[-1]
    ema10_3ago = ema10_series[-4]

    if math.isnan(ema5_now) or math.isnan(ema10_now):
        return 0.0, ""

    # EMA5 上穿 EMA10（金叉）
    if ema5_now > ema10_now and ema5_3ago <= ema10_3ago:
        return max_score, "EMA5上穿EMA10(金叉)"

    # EMA5 拐头向上
    if len(ema5_series) >= 7:
        ema5_6ago = ema5_series[-7]
        if not math.isnan(ema5_6ago) and ema5_now > ema5_3ago and ema5_3ago < ema5_6ago:
            if ema5_now > ema10_now:
                return max_score * 0.8, "EMA5拐头向上(在EMA10上方)"
            return max_score * 0.7, "EMA5拐头向上"

    # EMA10 拐头向上
    if len(ema10_series) >= 7:
        ema10_6ago = ema10_series[-7]
        if (
            not math.isnan(ema10_6ago)
            and ema10_now > ema10_3ago
            and ema10_3ago < ema10_6ago
        ):
            return max_score * 0.5, "EMA10拐头向上"

    return 0.0, ""


def _score_macd_reversal(closes: List[float], max_score: float) -> tuple:
    """MACD 反转信号评分。

    - 零轴下方金叉（MACD 线上穿信号线，且都在零轴下方）：满分
    - MACD 底背离（价格新低但 MACD 未新低）：满分 * 0.85
    - 柱状图由负转正：满分 * 0.5
    """
    macd = calc_macd(closes)
    ml = macd.get("macd_line")
    sl = macd.get("signal_line")
    hist = macd.get("histogram")

    if ml is None or sl is None or hist is None:
        return 0.0, ""

    # 零轴下方金叉
    if ml < 0 and sl < 0 and ml > sl and hist > 0:
        return max_score, "MACD零轴下方金叉"

    # 底背离检测
    if _check_macd_divergence(closes):
        return max_score * 0.85, "MACD底背离"

    # 柱状图由负转正
    if hist > 0 and ml < 0:
        return max_score * 0.5, "MACD柱状图转正"

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
    for i in range(min_idx - 1, -1, -1):
        if prev_min_idx is None or recent[i] < recent[prev_min_idx]:
            prev_min_idx = i
    if prev_min_idx is None or recent[min_idx] >= recent[prev_min_idx]:
        return False
    h1 = calc_macd(closes[: base_idx + prev_min_idx + 1]).get("histogram")
    h2 = calc_macd(closes[: base_idx + min_idx + 1]).get("histogram")
    return h1 is not None and h2 is not None and h2 > h1


def _score_kdj_golden_cross(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    max_score: float,
) -> float:
    """KDJ 低位金叉评分。

    J 值从负值区域上穿 0 线，或 K 上穿 D 且都在 30 以下。
    """
    if len(closes) < KDJ_PERIOD + KDJ_M1 + KDJ_M2 + 3:
        return 0.0

    def _calc_kdj(c, h, l):
        rsvs = []
        for i in range(KDJ_PERIOD - 1, len(c)):
            hh = max(h[i - KDJ_PERIOD + 1 : i + 1])
            ll = min(l[i - KDJ_PERIOD + 1 : i + 1])
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

    # K 上穿 D 且都在 50 以下（低位金叉，币圈用 50 而非 30，波动更大）
    if k_now > d_now and k_prev <= d_prev and k_now < 50:
        return max_score

    # J 值从负值上穿 0
    if j_now is not None and j_prev is not None:
        if j_now > 0 and j_prev < 0:
            return max_score * 0.7

    return 0.0


def _score_lower_shadow(
    closes: List[float],
    opens: List[float],
    highs: List[float],
    lows: List[float],
    shadow_ratio: float,
    max_score: float,
) -> float:
    """长下影线评分。

    近 3 根 K 线内出现长下影线 = 下方有强支撑。
    下影线长度 / 实体长度 ≥ shadow_ratio 倍。
    """
    for i in range(-3, 0):
        if i >= -len(closes):
            c, o, h, l = closes[i], opens[i], highs[i], lows[i]
            body = abs(c - o)
            lower_shadow = min(c, o) - l
            if body > 0 and lower_shadow >= body * shadow_ratio:
                return max_score
            # 十字星也算（实体极小但下影线长）
            if body < (h - l) * 0.1 and lower_shadow > (h - l) * 0.5:
                return max_score * 0.7
    return 0.0
