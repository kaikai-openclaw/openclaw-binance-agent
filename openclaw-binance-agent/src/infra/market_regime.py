"""
大盘环境过滤模块（MarketRegimeFilter）

判断当前 A 股市场所处的趋势阶段，为各 Skill 提供统一的"开关"和"门槛调整"。

缓存策略：
  - 指数 K 线：复用 AkshareClient.get_klines()，走 KlineCache（SQLite）
    命中则零网络请求；当日 17:00 后自动刷新一次
  - 情绪数据（涨跌停比）：内存 TTL 缓存，默认 10 分钟内复用
  - 整体 regime 结果：内存 TTL 缓存，默认 10 分钟内复用

大盘趋势分类：
  bull     — 多头排列（MA20 > MA60 > MA120），近 20 日上涨
  bear     — 空头排列（MA20 < MA60），近 20 日跌幅 > 5%
  sideways — 介于两者之间

各策略开关：
  allow_trend    — 趋势选股（Skill-1A）：牛市/横盘且近 5 日未加速下跌
  allow_oversold — 超跌反弹（Skill-1B）：牛市正常；横盘且近 5 日未加速下跌；熊市关闭
  allow_reversal — 底部反转：牛市/横盘均可；熊市仅高分才入场（由调用方判断）

横盘时自动提高评分门槛（由各 Skill 读取 suggested_min_score 字段）。
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── 模块级单例缓存 ────────────────────────────────────────
# 同一进程内所有 Skill 共享同一个 MarketRegimeFilter 实例，
# 避免每次 new 实例导致内存 TTL 缓存失效、情绪数据重复拉取。
_SINGLETON: Optional["MarketRegimeFilter"] = None


def get_regime_filter(client) -> "MarketRegimeFilter":
    """获取（或创建）进程级单例 MarketRegimeFilter。

    同一进程内多次调用返回同一个实例，TTL 内存缓存跨 Skill 共享。
    client 参数仅在首次创建时生效；后续调用忽略传入的 client。
    """
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = MarketRegimeFilter(client=client)
        log.debug("[MarketRegime] 创建单例实例")
    return _SINGLETON

# 指数代码（akshare 格式）
# 走 AkshareClient.get_klines() 的指数专用路径，缓存 key 为 idx_000001
# 上证综指（000001）：本地已有缓存（旧 key 是 000001，新 key 是 idx_000001）
# 沪深300（000300）：首次需联网拉取后缓存
INDEX_MAIN  = "000001"   # 上证综指，用于大盘趋势判断
INDEX_CSI300 = "000300"  # 沪深300，备用

# 大盘趋势判断所需最少 K 线数
MIN_KLINES_REQUIRED = 60

# 横盘时各策略评分门槛提升幅度
SIDEWAYS_OVERSOLD_MIN_SCORE_BUMP = 15   # 超跌：25 → 40
SIDEWAYS_TREND_MIN_SCORE_BUMP    = 10   # 趋势：50 → 60


class MarketRegimeFilter:
    """大盘环境过滤器。

    用法：
        regime = MarketRegimeFilter(client=akshare_client)
        state  = regime.get_current_regime()

        if not state["allow_oversold"]:
            return empty_result(state)

        # 横盘时提高门槛
        min_score = state.get("suggested_oversold_min_score", default_min_score)
    """

    def __init__(self, client, regime_ttl: int = 600) -> None:
        """
        Args:
            client:     AkshareClient 实例（提供 get_klines 接口）
            regime_ttl: 整体 regime 结果的内存缓存有效期（秒），默认 10 分钟
        """
        self._client = client
        self._regime_ttl = regime_ttl
        # 内存缓存
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0.0

    # ──────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────

    def get_current_regime(self, force_refresh: bool = False) -> dict:
        """获取当前大盘环境。TTL 内直接返回缓存，否则重新计算。

        Returns:
            {
                "trend":                   "bull" | "bear" | "sideways" | "unknown",
                "allow_trend":             bool,   # 趋势选股是否开放
                "allow_oversold":          bool,   # 超跌反弹是否开放
                "allow_reversal":          bool,   # 底部反转是否开放（熊市降低门槛而非关闭）
                "suggested_trend_min_score":    int,  # 趋势选股建议最低分
                "suggested_oversold_min_score": int,  # 超跌反弹建议最低分
                "ma20":   float,
                "ma60":   float,
                "chg20d": float,   # 近 20 日涨跌幅（%）
                "chg5d":  float,   # 近 5 日涨跌幅（%）
                "limit_ratio": float,  # 涨停数 / 跌停数
                "panic_mode":  bool,   # 跌停数 > 100
                "reason": str,         # 人类可读的判断依据
            }
        """
        now = time.time()
        if (not force_refresh
                and self._cache is not None
                and (now - self._cache_ts) < self._regime_ttl):
            log.debug("[MarketRegime] 命中缓存 (%.0fs前)", now - self._cache_ts)
            return self._cache

        result = self._compute_regime()
        self._cache = result
        self._cache_ts = time.time()
        log.info("[MarketRegime] 大盘环境: trend=%s allow_oversold=%s allow_trend=%s "
                 "chg20d=%.1f%% chg5d=%.1f%% panic=%s",
                 result["trend"], result["allow_oversold"], result["allow_trend"],
                 result["chg20d"], result["chg5d"], result["panic_mode"])
        return result

    # ──────────────────────────────────────────────────────
    # 内部计算
    # ──────────────────────────────────────────────────────

    def _compute_regime(self) -> dict:
        # ── 1. 指数 K 线（走 KlineCache，指数专用接口，缓存 key = idx_000001）──
        rows = self._client.get_klines(INDEX_MAIN, limit=150)

        if not rows or len(rows) < MIN_KLINES_REQUIRED:
            # 数据不足时降级：允许所有策略运行，但标注 unknown
            log.warning("[MarketRegime] 指数数据不足（%d 行），降级为 unknown", len(rows) if rows else 0)
            return self._unknown_regime("指数数据不足，无法判断大盘环境")

        # K 线格式：[date, open, high, low, close, volume]
        closes = [float(r[4]) for r in rows]
        last   = closes[-1]

        # 合理性校验：上证综指正常范围 500~10000，平安银行等个股 close 约 5~50
        # 如果 close 明显不在指数范围内，说明拿到的是旧 key 的个股数据，不可用
        if last < 500 or last > 20000:
            log.warning(
                "[MarketRegime] 指数数据异常（close=%.2f，不在合理范围 500~20000），"
                "可能是同代码个股数据。请运行 preload_klines.py --index 初始化指数缓存。"
                "降级为 unknown。", last,
            )
            return self._unknown_regime(
                f"指数数据异常(close={last:.2f})，请运行 preload_klines.py --index 初始化"
            )

        # K 线格式：[date, open, high, low, close, volume]
        closes = [float(r[4]) for r in rows]
        last   = closes[-1]

        ma20  = sum(closes[-20:]) / 20
        ma60  = sum(closes[-60:]) / 60
        ma120 = sum(closes[-120:]) / 120 if len(closes) >= 120 else ma60

        chg20 = (last - closes[-20]) / closes[-20] * 100 if closes[-20] > 0 else 0.0
        chg5  = (last - closes[-5])  / closes[-5]  * 100 if closes[-5]  > 0 else 0.0

        # ── 2. 趋势分类 ──
        if last > ma20 > ma60 > ma120 and chg20 > 0:
            trend = "bull"
            reason = f"多头排列(MA20>{ma60:.0f}>MA120)，近20日+{chg20:.1f}%"
        elif last < ma20 < ma60 and chg20 < -5:
            trend = "bear"
            reason = f"空头排列(MA20<MA60)，近20日{chg20:.1f}%"
        else:
            trend = "sideways"
            reason = f"横盘震荡，近20日{chg20:+.1f}%，近5日{chg5:+.1f}%"

        # ── 3. 情绪数据（内存 TTL，不走 SQLite）──
        limit_ratio, panic = self._get_sentiment()

        # ── 4. 策略开关 ──
        # 趋势选股：牛市/横盘 + 近 5 日未加速下跌
        allow_trend = trend in ("bull", "sideways") and chg5 > -2.0

        # 超跌反弹：
        #   牛市 → 开放（超跌反弹成功率最高）
        #   横盘 → 近 5 日未加速下跌 + 非持续恐慌
        #   熊市 → 关闭（接飞刀风险极高）
        if trend == "bull":
            allow_oversold = True
        elif trend == "sideways":
            allow_oversold = chg5 > -3.0 and not panic
        else:  # bear
            allow_oversold = False

        # 底部反转：熊市降低门槛而非关闭（反转信号本身就是底部确认）
        allow_reversal = trend != "bear" or chg5 > -1.0  # 熊市近期企稳才允许

        # ── 5. 横盘时建议提高评分门槛 ──
        from src.skills.skill1a_collect import DEFAULT_MIN_SIGNAL_SCORE
        from src.skills.skill1b_oversold import DEFAULT_MIN_OVERSOLD_SCORE

        if trend == "sideways":
            suggested_trend_min    = DEFAULT_MIN_SIGNAL_SCORE + SIDEWAYS_TREND_MIN_SCORE_BUMP
            suggested_oversold_min = DEFAULT_MIN_OVERSOLD_SCORE + SIDEWAYS_OVERSOLD_MIN_SCORE_BUMP
        elif trend == "bear":
            # 熊市：即使开放也要大幅提高门槛
            suggested_trend_min    = DEFAULT_MIN_SIGNAL_SCORE + 20
            suggested_oversold_min = DEFAULT_MIN_OVERSOLD_SCORE + 30
        else:
            suggested_trend_min    = DEFAULT_MIN_SIGNAL_SCORE
            suggested_oversold_min = DEFAULT_MIN_OVERSOLD_SCORE

        return {
            "trend":                        trend,
            "allow_trend":                  allow_trend,
            "allow_oversold":               allow_oversold,
            "allow_reversal":               allow_reversal,
            "suggested_trend_min_score":    suggested_trend_min,
            "suggested_oversold_min_score": suggested_oversold_min,
            "ma20":        round(ma20, 2),
            "ma60":        round(ma60, 2),
            "chg20d":      round(chg20, 2),
            "chg5d":       round(chg5, 2),
            "limit_ratio": round(limit_ratio, 2),
            "panic_mode":  panic,
            "reason":      reason,
        }

    def _get_sentiment(self) -> tuple:
        """拉取涨跌停数量（情绪温度计）。失败时降级返回中性值。

        涨停数 / 跌停数：
          > 3   = 市场情绪亢奋
          < 0.5 = 市场极度恐慌（超跌反弹的加分项，但持续恐慌是减分项）
        panic_mode = 跌停数 > 100（系统性抛压）

        接口：akshare stock_zt_pool_em（涨停池）/ stock_zt_pool_dtgc_em（跌停池）
        需要传当日日期，非交易日会返回空数据（正常降级）。
        """
        try:
            import akshare as ak
            from datetime import date
            today = date.today().strftime("%Y%m%d")

            # 涨停池
            df_up = ak.stock_zt_pool_em(date=today)
            up = len(df_up) if df_up is not None and not df_up.empty else 0

            # 跌停池
            df_down = ak.stock_zt_pool_dtgc_em(date=today)
            down = len(df_down) if df_down is not None and not df_down.empty else 0

            ratio = up / max(down, 1)
            panic = down > 100
            log.debug("[MarketRegime] 情绪: 涨停=%d 跌停=%d 比值=%.2f panic=%s",
                      up, down, ratio, panic)
            return ratio, panic
        except Exception as e:
            log.warning("[MarketRegime] 情绪数据获取失败: %s，降级中性", e)
            return 1.0, False

    @staticmethod
    def _unknown_regime(reason: str) -> dict:
        """数据不足时的降级结果：允许所有策略运行，使用默认门槛。"""
        from src.skills.skill1a_collect import DEFAULT_MIN_SIGNAL_SCORE
        from src.skills.skill1b_oversold import DEFAULT_MIN_OVERSOLD_SCORE
        return {
            "trend":                        "unknown",
            "allow_trend":                  True,
            "allow_oversold":               True,
            "allow_reversal":               True,
            "suggested_trend_min_score":    DEFAULT_MIN_SIGNAL_SCORE,
            "suggested_oversold_min_score": DEFAULT_MIN_OVERSOLD_SCORE,
            "ma20":        0.0,
            "ma60":        0.0,
            "chg20d":      0.0,
            "chg5d":       0.0,
            "limit_ratio": 1.0,
            "panic_mode":  False,
            "reason":      reason,
        }
