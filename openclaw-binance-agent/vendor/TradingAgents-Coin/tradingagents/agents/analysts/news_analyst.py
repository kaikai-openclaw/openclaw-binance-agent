from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import get_news, get_global_news
from tradingagents.dataflows.config import get_config


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        tools = [
            get_news,
            get_global_news,
        ]

        system_message = (
            "你是一名新闻研究员，负责分析过去一周的最新新闻和趋势。请撰写一份关于当前世界形势的综合报告，内容应与交易和宏观经济相关。使用可用工具：get_news(query, start_date, end_date)用于公司特定或定向新闻搜索，get_global_news(curr_date, look_back_days, limit)用于更广泛的宏观经济新闻。不要简单地说趋势混合，请提供详细且精细的分析和洞察，帮助交易者做出决策。"
            + """ 请确保在报告末尾附加一个Markdown表格，整理报告中的关键要点，使其有条理且易于阅读。所有分析和报告内容请使用中文输出。"""
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
                    "供参考，当前日期是{current_date}。我们正在分析的公司是{ticker}",
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
            "news_report": report,
        }

    return news_analyst_node
