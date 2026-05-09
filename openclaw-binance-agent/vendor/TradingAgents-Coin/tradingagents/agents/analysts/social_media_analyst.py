from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import get_news
from tradingagents.dataflows.config import get_config


def create_social_media_analyst(llm):
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        tools = [
            get_news,
        ]

        system_message = (
            "你是一名社交媒体和公司特定新闻研究员/分析师，负责分析过去一周某特定公司的社交媒体帖子、最新公司新闻和公众情绪。你将获得一个公司名称，你的目标是撰写一份详尽的长篇报告，详细说明你的分析、洞察以及对交易者和投资者关于该公司当前状态的影响。报告应涵盖社交媒体分析、人们对该公司的评论、每日情绪数据分析以及最新公司新闻。使用get_news(query, start_date, end_date)工具搜索公司特定新闻和社交媒体讨论。尽量查看所有可能的来源，从社交媒体到情绪数据到新闻。不要简单地说趋势混合，请提供详细且精细的分析和洞察，帮助交易者做出决策。"
            + """ 请确保在报告末尾附加一个Markdown表格，整理报告中的关键要点，使其有条理且易于阅读。所有分析和报告内容请使用中文输出。""",
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一个有用的AI助手，正在与其他助手协作。"
                    " 请使用提供的工具来推进问题的解答。"
                    " 如果你无法完全回答，没关系；拥有不同工具的其他助手"
                    " 会在你停下的地方继续。尽你所能推进工作。"
                    " 如果你或其他助手得出了最终交易建议：**做多/做空/平仓/持有**或可交付成果，"
                    " 请在回复前加上'最终交易建议：**做多/做空/平仓/持有**'以便团队知道可以停止。"
                    " 你可以使用以下工具：{tool_names}。\n{system_message}"
                    "供参考，当前日期是{current_date}。我们当前要分析的公司是{ticker}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(ticker=ticker)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "sentiment_report": report,
        }

    return social_media_analyst_node
