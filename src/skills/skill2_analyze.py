"""
Skill-2：深度分析与评级

对 Skill-1 输出的候选币种列表逐一调用 TradingAgentsModule 进行深度分析，
过滤评级分低于阈值（默认 6 分）的币种，输出结构化评级结果。

TradingAgentsModule 通过构造函数注入 analyzer 回调，便于测试时 mock。

需求: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# 类型别名：analyzer 接收 (symbol, market_data)，返回分析结果字典
# 返回值应包含 {"rating_score": int, "signal": str, "confidence": float}
AnalyzerFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]

# 默认评级过滤阈值
DEFAULT_RATING_THRESHOLD = 6

# TradingAgents 分析超时（秒），30 分钟以满足多智能体深度辩论
ANALYSIS_TIMEOUT = 1800


class TradingAgentsModule:
    """
    TradingAgents 开源项目的封装模块。

    通过构造函数注入 analyzer 回调，实现可测试的依赖注入设计。
    内置 30 秒超时控制，超时自动终止分析。
    复用 ThreadPoolExecutor 避免频繁创建销毁线程池。

    需求: 2.2, 2.6, 2.7
    """

    def __init__(self, analyzer: AnalyzerFn) -> None:
        """
        初始化 TradingAgentsModule。

        参数:
            analyzer: 分析回调函数，接收 (symbol, market_data)，
                      返回包含 rating_score / signal / confidence 的字典
        """
        self._analyzer = analyzer
        self._executor = ThreadPoolExecutor(max_workers=1)

    def analyze(self, symbol: str, market_data: dict) -> Dict[str, Any]:
        """
        对单个币种执行深度分析，30 秒超时自动终止。

        参数:
            symbol: 币种交易对符号（如 "BTCUSDT"）
            market_data: 该币种的市场数据

        返回:
            分析结果字典，包含 rating_score / signal / confidence

        异常:
            TimeoutError: 分析超过 30 秒时抛出
            Exception: analyzer 回调内部错误时透传
        """
        future = self._executor.submit(self._analyzer, symbol, market_data)
        try:
            result = future.result(timeout=ANALYSIS_TIMEOUT)
            return result
        except FuturesTimeoutError:
            future.cancel()
            log.warning(
                f"[TradingAgentsModule] {symbol} 分析超时（>{ANALYSIS_TIMEOUT}s），已终止"
            )
            raise TimeoutError(
                f"{symbol} 分析超时，超过 {ANALYSIS_TIMEOUT} 秒"
            )

    def shutdown(self) -> None:
        """关闭线程池，释放资源。"""
        self._executor.shutdown(wait=False)


class Skill2Analyze(BaseSkill):
    """
    深度分析与评级 Skill。

    从 State_Store 读取 Skill-1 输出的候选币种列表，
    对每个候选币种调用 TradingAgentsModule 进行深度分析，
    过滤评级分低于阈值的币种，输出结构化评级结果。

    需求: 2.1, 2.3, 2.4, 2.5
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        trading_agents: TradingAgentsModule,
        rating_threshold: int = DEFAULT_RATING_THRESHOLD,
    ) -> None:
        """
        初始化 Skill-2。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            trading_agents: TradingAgentsModule 实例（可注入 mock）
            rating_threshold: 评级过滤阈值，默认 6 分
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill2_analyze"
        self._trading_agents = trading_agents
        self._rating_threshold = rating_threshold

    def run(self, input_data: dict) -> dict:
        """
        执行深度分析与评级。

        流程:
        1. 从 State_Store 读取候选币种列表（通过 input_state_id）
        2. 对每个候选币种调用 TradingAgentsModule 分析
        3. 过滤评级分低于阈值的币种
        4. 组装输出

        参数:
            input_data: 经 Schema 校验的输入，包含 input_state_id

        返回:
            符合 skill2_output.json Schema 的输出字典
        """
        input_state_id = input_data["input_state_id"]

        # 步骤 1：从 State_Store 读取候选币种列表
        upstream_data = self.state_store.load(input_state_id)
        candidates = upstream_data.get("candidates", [])

        log.info(
            f"[{self.name}] 读取到 {len(candidates)} 个候选币种，"
            f"input_state_id={input_state_id}"
        )

        # 步骤 2：逐一分析每个候选币种
        all_ratings: List[Dict[str, Any]] = []
        failed_symbols: List[Dict[str, str]] = []  # 记录失败的币种和原因
        for candidate in candidates:
            symbol = candidate.get("symbol", "")
            if not symbol:
                continue

            # 构造市场数据传递给分析模块
            market_data = {
                "symbol": symbol,
                "heat_score": candidate.get("heat_score", 0),
                "source_url": candidate.get("source_url", ""),
                "collected_at": candidate.get("collected_at", ""),
            }

            try:
                result = self._trading_agents.analyze(symbol, market_data)
                rating = self._extract_rating(symbol, result)
                if rating is not None:
                    all_ratings.append(rating)
            except TimeoutError:
                # 需求 2.6：超时跳过，记录日志
                log.warning(f"[{self.name}] {symbol} 分析超时，已跳过")
                failed_symbols.append({"symbol": symbol, "reason": "分析超时"})
            except Exception as exc:
                # 需求 2.7：错误跳过，记录日志，继续处理剩余币种
                log.warning(
                    f"[{self.name}] {symbol} 分析失败: {exc}，已跳过"
                )
                failed_symbols.append({"symbol": symbol, "reason": str(exc)[:100]})

        # 步骤 3：过滤评级分低于阈值的币种
        filtered_ratings = [
            r for r in all_ratings
            if r["rating_score"] >= self._rating_threshold
        ]
        filtered_count = len(all_ratings) - len(filtered_ratings)

        log.info(
            f"[{self.name}] 分析完成: "
            f"总计={len(all_ratings)}, "
            f"通过={len(filtered_ratings)}, "
            f"过滤={filtered_count}"
        )

        # 步骤 4：组装输出
        output = {
            "state_id": str(uuid.uuid4()),
            "ratings": filtered_ratings,
            "filtered_count": filtered_count,
            "failed_symbols": failed_symbols,
            "analysis_summary": (
                f"候选 {len(candidates)} 个，"
                f"分析成功 {len(all_ratings)} 个，"
                f"通过评级 {len(filtered_ratings)} 个，"
                f"失败 {len(failed_symbols)} 个"
            ),
        }

        return output

    def _extract_rating(
        self, symbol: str, result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        从分析结果中提取评级信息。

        参数:
            symbol: 币种交易对符号
            result: TradingAgentsModule 返回的分析结果

        返回:
            标准化的评级字典，或 None（数据无效时）
        """
        rating_score = result.get("rating_score")
        signal = result.get("signal")
        confidence = result.get("confidence")

        # 校验必要字段
        if rating_score is None or signal is None or confidence is None:
            log.warning(
                f"[{self.name}] {symbol} 分析结果缺少必要字段，已跳过"
            )
            return None

        # 校验 rating_score 范围 [1, 10]
        if not isinstance(rating_score, int) or rating_score < 1 or rating_score > 10:
            log.warning(
                f"[{self.name}] {symbol} rating_score={rating_score} 无效，已跳过"
            )
            return None

        # 校验 signal 枚举值
        valid_signals = {"long", "short", "hold"}
        if signal not in valid_signals:
            log.warning(
                f"[{self.name}] {symbol} signal={signal} 无效，已跳过"
            )
            return None

        # 校验 confidence 范围 [0, 100]
        if not isinstance(confidence, (int, float)):
            log.warning(
                f"[{self.name}] {symbol} confidence={confidence} 无效，已跳过"
            )
            return None
        confidence = max(0.0, min(100.0, float(confidence)))

        return {
            "symbol": symbol,
            "rating_score": rating_score,
            "signal": signal,
            "confidence": confidence,
            "comment": result.get("comment", ""),
        }
