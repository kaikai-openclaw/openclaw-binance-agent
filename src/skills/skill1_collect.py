"""
Skill-1：信息收集与候选筛选

调用 OpenClaw websearch 技能检索市场热点，再调用 xurl 技能抓取结构化数据，
为每条数据标注 source_url 和 collected_at，仅输出经来源验证的真实数据。

searcher / fetcher 通过构造函数注入，便于测试时 mock。

需求: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from src.infra.state_store import StateStore
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# 类型别名：searcher 接收关键词列表，返回搜索结果列表
# 每条搜索结果至少包含 {"url": str, ...}
SearcherFn = Callable[[list[str]], list[dict[str, Any]]]

# 类型别名：fetcher 接收 URL，返回结构化数据字典
# 返回值应包含 {"symbol": str, "heat_score": float, ...}，失败时返回 None
FetcherFn = Callable[[str], dict[str, Any] | None]

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 60


class Skill1Collect(BaseSkill):
    """
    信息收集与候选筛选 Skill。

    通过 websearch 检索市场热点，再通过 xurl 抓取结构化数据，
    为每条记录标注 source_url 和 collected_at，
    仅输出经来源验证的真实数据（防幻觉约束）。
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        searcher: SearcherFn,
        fetcher: FetcherFn,
    ) -> None:
        """
        初始化 Skill-1。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            searcher: websearch 回调，接收关键词列表，返回搜索结果
            fetcher: xurl 回调，接收 URL，返回结构化数据或 None
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill1_collect"
        self._searcher = searcher
        self._fetcher = fetcher

    def run(self, input_data: dict) -> dict:
        """
        执行信息收集与候选筛选。

        流程:
        1. 从输入中提取搜索关键词
        2. 调用 searcher（websearch）检索市场热点（含重试）
        3. 对每条搜索结果调用 fetcher（xurl）抓取结构化数据（含重试）
        4. 防幻觉过滤：仅保留包含有效 symbol 和 source_url 的记录
        5. 标注 collected_at 时间戳
        6. 组装输出

        参数:
            input_data: 经 Schema 校验的输入，包含 trigger_time 和 search_keywords

        返回:
            符合 skill1_output.json Schema 的输出字典
        """
        keywords = input_data.get("search_keywords", [])
        pipeline_run_id = str(uuid.uuid4())

        # 步骤 1：调用 searcher 检索市场热点（带重试）
        search_results = self._call_with_retry(
            fn=lambda: self._searcher(keywords),
            description="websearch",
        )

        # 步骤 2：对每条搜索结果调用 fetcher 抓取结构化数据
        candidates: list[dict[str, Any]] = []
        for result in search_results:
            url = result.get("url", "")
            if not url:
                continue

            fetched = self._call_with_retry(
                fn=lambda u=url: self._fetcher(u),
                description=f"xurl({url})",
            )

            if fetched is None:
                # fetcher 返回 None 表示抓取失败，跳过
                continue

            # 防幻觉约束：仅保留包含有效 symbol 的记录
            candidate = self._validate_and_annotate(fetched, url)
            if candidate is not None:
                candidates.append(candidate)

        # 步骤 3：组装输出（state_id 由 BaseSkill.execute() 在存储时生成，
        # 此处使用占位符，后续由 execute() 覆盖）
        output = {
            "state_id": str(uuid.uuid4()),
            "candidates": candidates,
            "pipeline_run_id": pipeline_run_id,
        }

        return output

    def _validate_and_annotate(
        self, fetched: dict[str, Any], source_url: str
    ) -> dict[str, Any] | None:
        """
        防幻觉校验并标注来源信息。

        仅当 fetched 包含有效的 symbol 和 heat_score 时才输出，
        确保所有数据均来自外部真实来源。

        参数:
            fetched: fetcher 返回的结构化数据
            source_url: 数据来源 URL

        返回:
            标注后的候选记录字典，或 None（校验不通过时）
        """
        symbol = fetched.get("symbol")
        heat_score = fetched.get("heat_score")

        # 防幻觉：symbol 必须存在且为非空字符串
        if not isinstance(symbol, str) or not symbol:
            log.warning(f"防幻觉过滤：缺少有效 symbol，来源={source_url}")
            return None

        # 防幻觉：heat_score 必须为数值
        if not isinstance(heat_score, (int, float)):
            log.warning(
                f"防幻觉过滤：heat_score 无效 ({heat_score})，来源={source_url}"
            )
            return None

        # 防幻觉：heat_score 范围 [0, 100]
        heat_score = max(0.0, min(100.0, float(heat_score)))

        # 标注来源信息
        collected_at = datetime.now(timezone.utc).isoformat()

        return {
            "symbol": symbol,
            "heat_score": heat_score,
            "source_url": source_url,
            "collected_at": collected_at,
        }

    def _call_with_retry(
        self,
        fn: Callable,
        description: str,
    ) -> Any:
        """
        带重试的调用封装。

        失败后等待 60 秒重试，最多 3 次。
        3 次均失败则抛出最后一次异常。

        参数:
            fn: 要执行的可调用对象
            description: 调用描述（用于日志）

        返回:
            fn() 的返回值

        异常:
            Exception: 重试 3 次后仍失败时抛出最后一次异常
        """
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = fn()
                return result
            except Exception as exc:
                last_error = exc
                log.warning(
                    f"[{self.name}] {description} 第 {attempt}/{MAX_RETRIES} 次调用失败: {exc}"
                )
                if attempt < MAX_RETRIES:
                    log.info(
                        f"[{self.name}] 将在 {RETRY_DELAY_SECONDS} 秒后重试 {description}"
                    )
                    time.sleep(RETRY_DELAY_SECONDS)

        # 重试耗尽，记录告警并抛出异常
        log.error(
            f"[{self.name}] {description} 重试 {MAX_RETRIES} 次后仍失败，"
            f"Skill-1 标记为失败"
        )
        raise last_error  # type: ignore[misc]
