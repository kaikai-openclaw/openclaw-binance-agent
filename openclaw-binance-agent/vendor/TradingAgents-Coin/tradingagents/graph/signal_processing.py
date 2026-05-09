# TradingAgents/graph/signal_processing.py

from langchain_openai import ChatOpenAI


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted decision: 做多(LONG), 做空(SHORT), 平仓(CLOSE), or 持有(HOLD)
        """
        messages = [
            (
                "system",
                "你是一个高效的助手，专门分析分析师团队提供的段落或财务报告。你的任务是提取投资决策：做多、做空、平仓或持有。仅输出提取的决策（做多、做空、平仓或持有），不添加任何额外的文字或信息。注意：'买入'等同于'做多'，'卖出'在没有持仓的情况下等同于'做空'，有持仓时等同于'平仓'。如果无法明确判断，输出'持有'。",
            ),
            ("human", full_signal),
        ]

        return self.quick_thinking_llm.invoke(messages).content
