"""
Skill-1B：A 股超跌反弹筛选（双模式）

两种独立的超跌分析模式，针对 A 股市场特性深度定制：

## 短期超跌反弹（ShortTermAStockOversold）— 3~5 天持仓
  适用场景：情绪错杀后的技术性修复，连续跌停后开板反弹
  核心逻辑：恐慌抛售 → 超卖极值 → 底部放量（恐慌盘出尽）→ V 型反弹
  回看窗口：20~30 天日线
  A 股特有信号：
    - 跌停板计数（连续跌停是 A 股独有的极端信号）
    - 底部放量权重高（T+1 下恐慌盘集中释放 = 抛压出尽）
    - KDJ 金叉确认（短期反转的即时信号）
  阈值特点：RSI < 25、BIAS < -8%、连跌 ≥ 3 天、累跌 < -12%（10 天内）

## 长期超跌蓄能（LongTermAStockOversold）— 2~4 周持仓
  适用场景：中期阴跌后的底部构筑，缩量企稳 + 均值回归
  核心逻辑：持续下跌 → 深度偏离均线 → 缩量筑底（不是放量！）→ MACD 底背离 → 趋势反转
  回看窗口：60~120 天日线
  A 股特有信号：
    - 缩量企稳（A 股底部特征是地量，不是放量）
    - 距 120 日高点回撤幅度（A 股中期调整通常 30-50%）
    - MACD 底背离权重高（日线级别背离在 A 股可靠性很高）
    - 60 日乖离率（中期偏离度比 20 日更有意义）
  阈值特点：RSI < 35、BIAS(60) < -15%、连跌 ≥ 5 天、累跌 < -25%（30 天内）

两者共享：基础过滤（排除 ST/退市/北交所/低价股）、相关性去重

A 股 vs 加密货币的关键差异（影响指标设计）：
  - 涨跌停板 10%/20% → 连续跌停是极端信号，需要专门计数
  - T+1 交易 → 底部放量 = 恐慌盘集中释放 = 抛压出尽信号
  - 板块联动强 → 相关性去重更重要
  - 无资金费率 → 用换手率/量能变化替代
  - 底部特征是缩量（地量见地价）→ 长期模式用缩量企稳而非放量

数据源：AkshareClient（akshare 公开接口，K 线优先走本地 SQLite 缓存）
"""

import logging
import math
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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

_EXCLUDE_KEYWORDS = {"ST", "*ST", "退", "B股", "PT"}

DEFAULT_MIN_AMOUNT = 50_000_000
DEFAULT_MIN_PRICE = 3.0
DEFAULT_MIN_OVERSOLD_SCORE = 35  # 回测验证：≥35 胜率60%+，低于此分段噪音过多
DEFAULT_MAX_CANDIDATES = 30
DEFAULT_PREFILTER_CHANGE_PCT = 0.0

# K 线并发拉取线程数（IO 密集型）
DEFAULT_KLINE_WORKERS = 8

# ══════════════════════════════════════════════════════════
# 短期超跌参数（3~5 天反弹）
# ══════════════════════════════════════════════════════════

ST_MIN_KLINES = 30               # 最低 30 天数据
ST_RSI_THRESHOLD = 25.0          # RSI < 25 极端超卖（A 股有涨跌停，RSI 更容易到极值）
ST_BIAS_THRESHOLD = -8.0         # 20 日乖离率 < -8%
ST_CONSECUTIVE_DOWN = 3          # 连续下跌 ≥ 3 天
ST_DROP_PCT = -12.0              # 近 10 日累计跌幅 < -12%
ST_DROP_LOOKBACK = 10            # 回看 10 天
ST_DRAWDOWN_LOOKBACK = 30        # 距高点回看 30 天

# 短期评分权重（满分 100）— 侧重即时超卖信号和恐慌盘释放
ST_W_RSI = 20           # RSI 极端超卖（短期核心）
ST_W_BIAS = 12          # 乖离率
ST_W_DROP = 12          # 连续杀跌 + 累计跌幅
ST_W_BOLL = 10          # 布林带下轨突破
ST_W_MACD_DIV = 5       # MACD 底背离（短期可靠性一般）
ST_W_KDJ = 10           # KDJ J 值极值（短期反转信号）
ST_W_LIMIT_DOWN = 13    # 跌停板计数（A 股独有，连续跌停 = 极端恐慌）
ST_W_VOLUME = 13        # 底部放量（T+1 下恐慌盘集中释放 = 抛压出尽）
ST_W_DRAWDOWN = 5       # 距高点回撤（短期权重低）

# ══════════════════════════════════════════════════════════
# 长期超跌蓄能参数（2~4 周波段）
# ══════════════════════════════════════════════════════════

LT_MIN_KLINES = 60               # 最低 60 天数据
LT_RSI_THRESHOLD = 35.0          # RSI < 35（长期不需要极端值，偏弱即可）
LT_BIAS_THRESHOLD = -15.0        # 60 日乖离率 < -15%（用 60 日均线衡量中期偏离）
LT_BIAS_PERIOD = 60              # 长期用 60 日乖离率（不是 20 日）
LT_CONSECUTIVE_DOWN = 5          # 连续下跌 ≥ 5 天
LT_DROP_PCT = -25.0              # 近 30 日累计跌幅 < -25%
LT_DROP_LOOKBACK = 30            # 回看 30 天
LT_DRAWDOWN_LOOKBACK = 120       # 距高点回看 120 天（半年，覆盖完整中期调整）
LT_DRAWDOWN_THRESHOLD = -30.0    # 距高点回撤 > 30%

# 长期评分权重（满分 100）— 侧重趋势偏离和底部构筑信号
LT_W_RSI = 10           # RSI（长期权重降低）
LT_W_BIAS = 18          # 60 日乖离率（中期偏离度，长期核心）
LT_W_DROP = 10          # 连续杀跌 + 累计跌幅
LT_W_BOLL = 8           # 布林带
LT_W_MACD_DIV = 18      # MACD 底背离（日线级别在 A 股可靠性很高，长期核心）
LT_W_KDJ = 5            # KDJ（长期权重低）
LT_W_LIMIT_DOWN = 3     # 跌停板（长期看意义不大）
LT_W_SHRINK_VOL = 13    # 缩量企稳（A 股底部特征：地量见地价，长期独有）
LT_W_DRAWDOWN = 15      # 距高点回撤（长期核心）


# ══════════════════════════════════════════════════════════
# 共享基类
# ══════════════════════════════════════════════════════════

class _AStockOversoldBase(BaseSkill):
    """A 股超跌筛选共享基类。"""

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self._client = client

    @staticmethod
    def _build_target_pool(all_tickers, target_symbols):
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
    def _base_filter(tickers, min_amount, min_price, prefilter_pct=0.0):
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
            if prefilter_pct < 0:
                change = t.get("change_pct")
                if change is not None and change > prefilter_pct:
                    continue
            result.append({**t, "symbol": symbol})
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

    def _run_scan(self, input_data: dict, mode: str) -> dict:
        """通用扫描流程。mode = 'short' 或 'long'。"""
        # ── 大盘环境过滤（前置检查，不影响评分逻辑）──
        skip_regime = input_data.get("skip_market_regime", False)
        if not skip_regime:
            try:
                from src.infra.market_regime import get_regime_filter
                regime_filter = get_regime_filter(client=self._client)
                regime = regime_filter.get_current_regime()

                if not regime["allow_oversold"]:
                    log.warning(
                        "[%s] 大盘环境不适合超跌反弹，策略暂停。"
                        "trend=%s chg5d=%.1f%% panic=%s reason=%s",
                        self.name, regime["trend"], regime["chg5d"],
                        regime["panic_mode"], regime["reason"],
                    )
                    return {
                        "state_id": str(uuid.uuid4()),
                        "candidates": [],
                        "pipeline_run_id": str(uuid.uuid4()),
                        "filter_summary": {
                            "total_tickers": 0,
                            "after_base_filter": 0,
                            "after_oversold_filter": 0,
                            "output_count": 0,
                            "skipped_reason": "market_regime_bear",
                            "market_trend": regime["trend"],
                            "market_reason": regime["reason"],
                        },
                    }

                # 横盘/熊市时自动提高评分门槛（input_data 未显式指定时才覆盖）
                if "min_oversold_score" not in input_data:
                    suggested = regime.get("suggested_oversold_min_score")
                    if suggested and suggested > DEFAULT_MIN_OVERSOLD_SCORE:
                        log.info("[%s] 大盘横盘/偏弱，评分门槛提升至 %d（原 %d）",
                                 self.name, suggested, DEFAULT_MIN_OVERSOLD_SCORE)
                        input_data = {**input_data, "min_oversold_score": suggested}

            except Exception as e:
                # 大盘过滤本身出错时不阻断策略，降级继续运行
                log.warning("[%s] 大盘环境检查失败，降级继续运行: %s", self.name, e)

        min_amount = input_data.get("min_amount", DEFAULT_MIN_AMOUNT)
        min_price = input_data.get("min_price", DEFAULT_MIN_PRICE)
        min_score = input_data.get("min_oversold_score", DEFAULT_MIN_OVERSOLD_SCORE)
        max_cands = input_data.get("max_candidates", DEFAULT_MAX_CANDIDATES)
        target_symbols = input_data.get("target_symbols")
        prefilter_pct = input_data.get("prefilter_change_pct", DEFAULT_PREFILTER_CHANGE_PCT)

        if mode == "long":
            min_klines = input_data.get("min_klines", LT_MIN_KLINES)
            rsi_thresh = input_data.get("rsi_threshold", LT_RSI_THRESHOLD)
            bias_thresh = input_data.get("bias_threshold", LT_BIAS_THRESHOLD)
            consec_thresh = input_data.get("consecutive_down_days", LT_CONSECUTIVE_DOWN)
            drop_thresh = input_data.get("drop_pct_threshold", LT_DROP_PCT)
            drop_lookback = input_data.get("drop_lookback_days", LT_DROP_LOOKBACK)
        else:
            min_klines = input_data.get("min_klines", ST_MIN_KLINES)
            rsi_thresh = input_data.get("rsi_threshold", ST_RSI_THRESHOLD)
            bias_thresh = input_data.get("bias_threshold", ST_BIAS_THRESHOLD)
            consec_thresh = input_data.get("consecutive_down_days", ST_CONSECUTIVE_DOWN)
            drop_thresh = input_data.get("drop_pct_threshold", ST_DROP_PCT)
            drop_lookback = input_data.get("drop_lookback_days", ST_DROP_LOOKBACK)

        pipeline_run_id = str(uuid.uuid4())

        all_tickers = self._client.get_spot_all()
        total_count = len(all_tickers)

        # 排除科创板（688 开头）
        exclude_kcb = input_data.get("exclude_kcb", False)
        if exclude_kcb:
            before = len(all_tickers)
            all_tickers = [
                t for t in all_tickers
                if not (t.get("symbol", "").startswith("688") or t.get("code", "").startswith("688"))
            ]
            log.info("[%s] 排除科创板: %d → %d", self.name, before, len(all_tickers))

        if target_symbols:
            pool = self._build_target_pool(all_tickers, target_symbols)
            if not pool and hasattr(self._client, "get_spot_by_hist"):
                pool = self._client.get_spot_by_hist(target_symbols)
        else:
            pool = self._base_filter(all_tickers, min_amount, min_price, prefilter_pct)

        log.info("[%s] Step1: %d/%d 通过基础过滤", self.name, len(pool), total_count)

        # 拉取足够的 K 线
        kline_need = max(KLINE_LIMIT, min_klines,
                         LT_DRAWDOWN_LOOKBACK + 20 if mode == "long" else ST_DRAWDOWN_LOOKBACK + 20)

        scored: List[dict] = []
        returns_map: Dict[str, List[float]] = {}

        workers = min(DEFAULT_KLINE_WORKERS, len(pool)) if pool else 1
        log.info("[%s] 并发拉取 K 线: 候选=%d, 线程数=%d", self.name, len(pool), workers)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skill1b_kline") as executor:
            future_to_item = {
                executor.submit(
                    self._fetch_and_score_symbol,
                    item, mode, kline_need, min_klines,
                    rsi_thresh, bias_thresh, consec_thresh,
                    drop_thresh, drop_lookback, min_score,
                    bool(target_symbols),
                ): item
                for item in pool
            }
            for future in as_completed(future_to_item):
                result = future.result()  # 内部已 catch 异常
                if result is not None:
                    scored_item, returns = result
                    scored.append(scored_item)
                    returns_map[scored_item["symbol"]] = returns

        scored.sort(key=lambda x: x["oversold_score"], reverse=True)
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
                "after_oversold_filter": len(scored),
                "output_count": len(candidates),
            },
        }

    def _fetch_and_score_symbol(
        self,
        item: dict,
        mode: str,
        kline_need: int,
        min_klines: int,
        rsi_thresh: float,
        bias_thresh: float,
        consec_thresh: int,
        drop_thresh: float,
        drop_lookback: int,
        min_score: int,
        is_target_mode: bool,
    ) -> Optional[tuple]:
        """拉取单股票 K 线并计算超跌评分（线程安全）。

        返回 (scored_item, returns) 元组，或 None。
        """
        symbol = item["symbol"]
        try:
            klines = self._client.get_klines(symbol, "daily", kline_need)
            if not klines or len(klines) < min_klines:
                return None

            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            if mode == "short":
                result = _calc_short_term_score(
                    closes, highs, lows, volumes,
                    rsi_thresh, bias_thresh, consec_thresh,
                    drop_thresh, drop_lookback,
                )
            else:
                result = _calc_long_term_score(
                    closes, highs, lows, volumes,
                    rsi_thresh, bias_thresh, consec_thresh,
                    drop_thresh, drop_lookback,
                )
                # 回测验证：长期超跌 70 分以上反而表现变差（过拟合极端信号）
                # 70-85 胜率42% 均收益-11%，截断上限避免误入
                if result["oversold_score"] > 70:
                    return None

            if result["oversold_score"] < min_score and not is_target_mode:
                return None

            returns = calc_returns(closes)
            atr_val = calc_atr(highs, lows, closes, ATR_PERIOD)
            atr_filter_val = calc_atr(highs, lows, closes, ATR_PERIOD_FILTER)
            last_close = closes[-1]
            atr_pct = round(atr_val / last_close * 100, 2) if (atr_val and last_close > 0) else None
            atr_filter_pct = round(atr_filter_val / last_close * 100, 2) if (atr_filter_val and last_close > 0) else None

            scored_item = {
                "symbol": symbol,
                "name": item.get("name", ""),
                "close": last_close,
                "amount": item.get("amount", 0),
                "rsi": result["rsi"],
                "bias_20": result["bias"],
                "consecutive_down": result["consecutive_down"],
                "drop_pct": result["drop_pct"],
                "below_boll_lower": result["below_boll_lower"],
                "kdj_j": result["kdj_j"],
                "macd_divergence": result["macd_divergence"],
                "volume_surge": result.get("volume_surge"),
                "oversold_score": result["oversold_score"],
                "signal_details": result["signal_details"],
                "atr_pct": atr_pct,
                "atr_filter_pct": atr_filter_pct,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            return scored_item, returns

        except Exception as exc:
            log.warning("[%s] %s 分析失败: %s", self.name, symbol, exc)
            return None


# ══════════════════════════════════════════════════════════
# 短期超跌反弹 Skill
# ══════════════════════════════════════════════════════════

class ShortTermAStockOversold(_AStockOversoldBase):
    """A 股短期超跌反弹筛选（3~5 天持仓）。

    捕捉情绪错杀后的技术性修复。
    核心信号：RSI 极端超卖 + 跌停板计数 + 底部放量（恐慌盘出尽）。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "skill1b_oversold_short"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(input_data, mode="short")


# ══════════════════════════════════════════════════════════
# 长期超跌蓄能 Skill
# ══════════════════════════════════════════════════════════

class LongTermAStockOversold(_AStockOversoldBase):
    """A 股长期超跌蓄能筛选（2~4 周持仓）。

    捕捉中期阴跌后的底部构筑和均值回归。
    核心信号：60 日 BIAS 深度偏离 + MACD 底背离 + 缩量企稳 + 距高点深度回撤。
    """

    def __init__(self, state_store, input_schema, output_schema, client) -> None:
        super().__init__(state_store, input_schema, output_schema, client)
        self.name = "skill1b_oversold_long"

    def run(self, input_data: dict) -> dict:
        return self._run_scan(input_data, mode="long")


# 向后兼容：保留原名指向短期版本
Skill1BOversold = ShortTermAStockOversold


# ══════════════════════════════════════════════════════════
# 短期超跌评分（侧重即时超卖 + 恐慌盘释放）
# ══════════════════════════════════════════════════════════

def _calc_short_term_score(
    closes: List[float], highs: List[float], lows: List[float],
    volumes: List[float],
    rsi_thresh: float, bias_thresh: float, consec_thresh: int,
    drop_thresh: float, drop_lookback: int,
) -> dict:
    """短期超跌评分（满分 100）。

    侧重即时超卖极值和恐慌盘释放信号。
    A 股特有：跌停板计数（权重 13）、底部放量（权重 13）。
    """
    signals = []
    score = 0.0

    # ── 1. RSI 极端超卖（权重 20）──
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    if rsi_val is not None and rsi_val < rsi_thresh:
        score += ST_W_RSI * min(1.0, (rsi_thresh - rsi_val) / rsi_thresh)
        signals.append(f"RSI={rsi_val:.1f}<{rsi_thresh}")

    # ── 2. 20 日乖离率（权重 12）──
    bias = _calc_bias(closes, BOLL_PERIOD)
    if bias is not None and bias < bias_thresh:
        score += ST_W_BIAS * min(1.0, (bias_thresh - bias) / abs(bias_thresh))
        signals.append(f"BIAS(20)={bias:.1f}%<{bias_thresh}%")

    # ── 3. 连续杀跌 + 累计跌幅（权重 12）──
    consec = _calc_consecutive_down(closes)
    drop_pct = _calc_drop_pct(closes, drop_lookback)
    drop_score = 0.0
    if consec >= consec_thresh:
        drop_score += ST_W_DROP * 0.5 * min(1.0, consec / (consec_thresh * 2))
        signals.append(f"连跌{consec}天≥{consec_thresh}")
    if drop_pct is not None and drop_pct < drop_thresh:
        drop_score += ST_W_DROP * 0.5 * min(1.0, (drop_thresh - drop_pct) / abs(drop_thresh))
        signals.append(f"近{drop_lookback}日跌{drop_pct:.1f}%")
    score += min(drop_score, float(ST_W_DROP))

    # ── 4. 布林带下轨突破（权重 10）──
    below_boll = _check_below_boll_lower(closes)
    if below_boll:
        score += ST_W_BOLL
        signals.append("跌破BOLL下轨")

    # ── 5. MACD 底背离（权重 5，短期可靠性一般）──
    macd_div = _check_macd_divergence(closes, lookback=20)
    if macd_div:
        score += ST_W_MACD_DIV
        signals.append("MACD底背离")

    # ── 6. KDJ J 值极值（权重 10）──
    kdj_j = _calc_kdj_j(closes, highs, lows)
    if kdj_j is not None and kdj_j < 0:
        score += ST_W_KDJ * min(1.0, abs(kdj_j) / 20.0)
        signals.append(f"KDJ_J={kdj_j:.1f}<0")

    # ── 7. 跌停板计数（权重 13，A 股独有）──
    # 连续跌停 = 流动性枯竭 = 极端恐慌，开板后反弹概率高
    limit_down_count = _calc_limit_down_count(closes)
    if limit_down_count >= 1:
        ld_score = ST_W_LIMIT_DOWN * min(1.0, limit_down_count / 3.0)
        score += ld_score
        signals.append(f"近期{limit_down_count}个跌停")

    # ── 8. 底部放量（权重 13，T+1 下恐慌盘集中释放）──
    vol_surge = _calc_volume_surge_bottom(volumes)
    if vol_surge is not None and vol_surge >= 1.5:
        score += ST_W_VOLUME * min(1.0, (vol_surge - 1.0) / 2.0)
        signals.append(f"底部放量{vol_surge:.1f}x")

    # ── 9. 距高点回撤（权重 5，短期参考）──
    drawdown = _calc_distance_from_high(closes, ST_DRAWDOWN_LOOKBACK)
    if drawdown is not None and drawdown < -15.0:
        score += ST_W_DRAWDOWN * min(1.0, (-15.0 - drawdown) / 15.0)
        signals.append(f"距高点{drawdown:.1f}%")

    return {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "bias": round(bias, 2) if bias is not None else None,
        "consecutive_down": consec,
        "drop_pct": round(drop_pct, 2) if drop_pct is not None else None,
        "below_boll_lower": below_boll,
        "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
        "macd_divergence": macd_div,
        "volume_surge": round(vol_surge, 2) if vol_surge is not None else None,
        "oversold_score": round(score),
        "signal_details": " | ".join(signals) if signals else "无超跌信号",
    }


# ══════════════════════════════════════════════════════════
# 长期超跌蓄能评分（侧重趋势偏离 + 底部构筑）
# ══════════════════════════════════════════════════════════

def _calc_long_term_score(
    closes: List[float], highs: List[float], lows: List[float],
    volumes: List[float],
    rsi_thresh: float, bias_thresh: float, consec_thresh: int,
    drop_thresh: float, drop_lookback: int,
) -> dict:
    """长期超跌蓄能评分（满分 100）。

    侧重中期趋势偏离和底部构筑信号。
    A 股特有：缩量企稳（权重 13，地量见地价）、60 日乖离率（权重 18）。
    """
    signals = []
    score = 0.0

    # ── 1. RSI 偏弱（权重 10）──
    rsi_val = calc_rsi(closes, RSI_PERIOD)
    if rsi_val is not None and rsi_val < rsi_thresh:
        score += LT_W_RSI * min(1.0, (rsi_thresh - rsi_val) / rsi_thresh)
        signals.append(f"RSI={rsi_val:.1f}<{rsi_thresh}")

    # ── 2. 60 日乖离率（权重 18，长期核心）──
    # 用 60 日均线衡量中期偏离度，比 20 日更能反映中期超跌
    bias = _calc_bias(closes, LT_BIAS_PERIOD)
    if bias is not None and bias < bias_thresh:
        score += LT_W_BIAS * min(1.0, (bias_thresh - bias) / abs(bias_thresh))
        signals.append(f"BIAS(60)={bias:.1f}%<{bias_thresh}%")

    # ── 3. 连续杀跌 + 累计跌幅（权重 10）──
    consec = _calc_consecutive_down(closes)
    drop_pct = _calc_drop_pct(closes, drop_lookback)
    drop_score = 0.0
    if consec >= consec_thresh:
        drop_score += LT_W_DROP * 0.5 * min(1.0, consec / (consec_thresh * 2))
        signals.append(f"连跌{consec}天≥{consec_thresh}")
    if drop_pct is not None and drop_pct < drop_thresh:
        drop_score += LT_W_DROP * 0.5 * min(1.0, (drop_thresh - drop_pct) / abs(drop_thresh))
        signals.append(f"近{drop_lookback}日跌{drop_pct:.1f}%")
    score += min(drop_score, float(LT_W_DROP))

    # ── 4. 布林带下轨突破（权重 8）──
    below_boll = _check_below_boll_lower(closes)
    if below_boll:
        score += LT_W_BOLL
        signals.append("跌破BOLL下轨")

    # ── 5. MACD 底背离（权重 18，长期核心）──
    # 日线级别 MACD 底背离在 A 股可靠性很高，回看 60 天
    macd_div = _check_macd_divergence(closes, lookback=60)
    if macd_div:
        score += LT_W_MACD_DIV
        signals.append("MACD底背离(60日)")

    # ── 6. KDJ J 值（权重 5）──
    kdj_j = _calc_kdj_j(closes, highs, lows)
    if kdj_j is not None and kdj_j < 0:
        score += LT_W_KDJ * min(1.0, abs(kdj_j) / 20.0)
        signals.append(f"KDJ_J={kdj_j:.1f}<0")

    # ── 7. 跌停板（权重 3，长期看意义不大）──
    limit_down_count = _calc_limit_down_count(closes)
    if limit_down_count >= 2:
        score += LT_W_LIMIT_DOWN
        signals.append(f"近期{limit_down_count}个跌停")

    # ── 8. 缩量企稳（权重 13，A 股底部独有特征）──
    # A 股底部特征是"地量见地价"：成交量萎缩到极致 = 抛压枯竭 = 底部
    # 与短期的"放量"信号相反！长期底部是缩量筑底
    shrink = _calc_volume_shrink(volumes)
    if shrink is not None and shrink < 0.5:
        # 当前量能不到 20 日均量的 50% = 极度缩量
        shrink_score = LT_W_SHRINK_VOL * min(1.0, (0.5 - shrink) / 0.3)
        score += shrink_score
        signals.append(f"缩量企稳(量比{shrink:.2f})")

    # ── 9. 距 120 日高点回撤（权重 15，长期核心）──
    drawdown = _calc_distance_from_high(closes, LT_DRAWDOWN_LOOKBACK)
    if drawdown is not None and drawdown < LT_DRAWDOWN_THRESHOLD:
        score += LT_W_DRAWDOWN * min(1.0,
            (LT_DRAWDOWN_THRESHOLD - drawdown) / abs(LT_DRAWDOWN_THRESHOLD))
        signals.append(f"距120日高点{drawdown:.1f}%")

    return {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "bias": round(bias, 2) if bias is not None else None,
        "consecutive_down": consec,
        "drop_pct": round(drop_pct, 2) if drop_pct is not None else None,
        "below_boll_lower": below_boll,
        "kdj_j": round(kdj_j, 2) if kdj_j is not None else None,
        "macd_divergence": macd_div,
        "volume_surge": round(shrink, 2) if shrink is not None else None,
        "oversold_score": round(score),
        "signal_details": " | ".join(signals) if signals else "无超跌信号",
    }


# ══════════════════════════════════════════════════════════
# 纯函数指标库
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
    """检测 MACD 底背离。"""
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
    """底部放量：最后一根 / 前 5 根均量。"""
    if len(volumes) < long_w + 1:
        return None
    avg = sum(volumes[-(long_w + 1):-1]) / long_w
    return volumes[-1] / avg if avg > 0 else None


# ── A 股独有指标 ─────────────────────────────────────────

def _calc_limit_down_count(closes: List[float], lookback: int = 10) -> int:
    """计算近 N 天内的跌停板次数。

    A 股跌停判定：
    - 主板/中小板：跌幅 ≥ 9.5%（考虑四舍五入，实际跌停是 -10%）
    - 创业板/科创板：跌幅 ≥ 19%（实际跌停是 -20%）
    简化处理：跌幅 ≥ 9.5% 视为跌停（覆盖 10% 和 20% 两种）
    """
    count = 0
    end = len(closes)
    start = max(1, end - lookback)
    for i in range(start, end):
        if closes[i - 1] > 0:
            change = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            if change <= -9.5:
                count += 1
    return count


def _calc_volume_shrink(volumes: List[float], short_w: int = 5, long_w: int = 20) -> Optional[float]:
    """缩量企稳检测：近 5 日均量 / 近 20 日均量。

    A 股底部特征是"地量见地价"：
    - 比值 < 0.5 = 极度缩量（抛压枯竭）
    - 比值 < 0.3 = 地量级别（底部信号极强）

    这与短期超跌的"放量"信号相反！
    短期看放量 = 恐慌盘出尽 → 即时反弹
    长期看缩量 = 抛压枯竭 → 底部构筑完成
    """
    if len(volumes) < long_w + 1:
        return None
    short_avg = sum(volumes[-short_w:]) / short_w
    long_avg = sum(volumes[-(long_w + 1):-1]) / long_w
    return short_avg / long_avg if long_avg > 0 else None
