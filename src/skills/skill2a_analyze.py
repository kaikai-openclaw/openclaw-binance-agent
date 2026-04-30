"""
Skill-2A：A股深度分析与评级

对 Skill-1A 输出的候选 A 股列表逐一调用 TradingAgents 多智能体框架进行深度分析。
TradingAgents 配置 data_vendors 为 akshare，使其通过 akshare 获取 A 股行情、
技术指标、基本面和新闻数据。

与 Skill-2（Binance）同构，区别：
  - 数据源: akshare（A 股）而非 binance（加密货币）
  - 股票代码: 6 位 A 股代码（如 600519）而非 BTCUSDT
  - TradingAgents ticker 格式: 直接使用 A 股代码

支持两种模式：
  - fast_mode=True：单次 LLM 快速分析
  - fast_mode=False：完整 TradingAgents 多智能体辩论
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional

from src.infra.memory_store import MemoryStore
from src.infra.state_store import StateStore
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

AnalyzerFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]

DEFAULT_RATING_THRESHOLD = 6
ANALYSIS_TIMEOUT = 1800  # 30 分钟


class AStockTradingAgentsModule:
    """
    TradingAgents A 股封装模块。

    与 TradingAgentsModule（Binance）同构，区别在于：
    - data_vendors 配置为 akshare
    - ticker 格式为 A 股代码
    """

    def __init__(self, analyzer: AnalyzerFn) -> None:
        self._analyzer = analyzer
        self._executor = ThreadPoolExecutor(max_workers=1)

    def analyze(self, symbol: str, market_data: dict) -> Dict[str, Any]:
        future = self._executor.submit(self._analyzer, symbol, market_data)
        try:
            return future.result(timeout=ANALYSIS_TIMEOUT)
        except FuturesTimeoutError:
            future.cancel()
            log.warning("[AStockTA] %s 分析超时（>%ds）", symbol, ANALYSIS_TIMEOUT)
            raise TimeoutError(f"{symbol} 分析超时")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


class Skill2AAnalyze(BaseSkill):
    """
    A 股深度分析与评级 Skill。

    从 StateStore 读取 Skill-1A 输出的候选列表，
    逐一调用 AStockTradingAgentsModule 分析，
    过滤低于阈值的评级，输出结构化结果。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        trading_agents: AStockTradingAgentsModule,
        rating_threshold: int = DEFAULT_RATING_THRESHOLD,
        memory_store: Optional[MemoryStore] = None,
    ) -> None:
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill2a_analyze"
        self._trading_agents = trading_agents
        self._rating_threshold = rating_threshold
        self._memory_store = memory_store

    def run(self, input_data: dict) -> dict:
        input_state_id = input_data["input_state_id"]

        # 热更新：每轮从 MemoryStore 读取最新进化阈值
        if self._memory_store is not None:
            evolved_threshold, _ = self._memory_store.get_evolved_params(
                default_rating_threshold=self._rating_threshold,
            )
            effective_threshold = evolved_threshold
            if evolved_threshold != self._rating_threshold:
                log.info("[%s] 热更新 rating_threshold: %d → %d（来自 MemoryStore）",
                         self.name, self._rating_threshold, evolved_threshold)
        else:
            effective_threshold = self._rating_threshold

        upstream = self.state_store.load(input_state_id)
        candidates = upstream.get("candidates", [])

        log.info("[%s] 读取 %d 个候选, input_state_id=%s",
                 self.name, len(candidates), input_state_id)

        all_ratings: List[Dict[str, Any]] = []
        failed_symbols: List[Dict[str, str]] = []

        for candidate in candidates:
            symbol = candidate.get("symbol", "")
            if not symbol:
                continue

            market_data = {
                "symbol": symbol,
                "name": candidate.get("name", ""),
                "signal_score": candidate.get("signal_score", 0),
                "signal_direction": candidate.get("signal_direction", ""),
                "collected_at": candidate.get("collected_at", ""),
            }

            try:
                result = self._trading_agents.analyze(symbol, market_data)
                rating = self._extract_rating(symbol, result)
                if rating is not None:
                    all_ratings.append(rating)
            except TimeoutError:
                log.warning("[%s] %s 超时，跳过", self.name, symbol)
                failed_symbols.append({"symbol": symbol, "reason": "分析超时"})
            except Exception as exc:
                log.warning("[%s] %s 失败: %s", self.name, symbol, exc)
                failed_symbols.append({"symbol": symbol, "reason": str(exc)[:100]})

        filtered = [r for r in all_ratings if r["rating_score"] >= effective_threshold]
        filtered_count = len(all_ratings) - len(filtered)

        log.info("[%s] 完成: 总=%d, 通过=%d, 过滤=%d",
                 self.name, len(all_ratings), len(filtered), filtered_count)

        return {
            "state_id": str(uuid.uuid4()),
            "ratings": filtered,
            "filtered_count": filtered_count,
            "failed_symbols": failed_symbols,
            "analysis_summary": (
                f"候选 {len(candidates)} 个，"
                f"分析成功 {len(all_ratings)} 个，"
                f"通过评级 {len(filtered)} 个，"
                f"失败 {len(failed_symbols)} 个"
            ),
        }

    @staticmethod
    def _extract_rating(symbol: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """从分析结果中提取标准化评级。"""
        rating_score = result.get("rating_score")
        signal = result.get("signal")
        confidence = result.get("confidence")

        if rating_score is None or signal is None or confidence is None:
            log.warning("[skill2a] %s 结果缺少必要字段，跳过", symbol)
            return None

        if not isinstance(rating_score, int) or not (1 <= rating_score <= 10):
            log.warning("[skill2a] %s rating_score=%s 无效", symbol, rating_score)
            return None

        if signal not in ("long", "short", "hold"):
            log.warning("[skill2a] %s signal=%s 无效", symbol, signal)
            return None

        if not isinstance(confidence, (int, float)):
            log.warning("[skill2a] %s confidence=%s 无效", symbol, confidence)
            return None
        confidence = max(0.0, min(100.0, float(confidence)))

        return {
            "symbol": symbol,
            "rating_score": rating_score,
            "signal": signal,
            "confidence": confidence,
            "comment": result.get("comment", ""),
        }
