import functools
import time
import json


def create_trader(llm, memory):
    def trader_node(state, name):
        company_name = state["company_of_interest"]
        investment_plan = state["investment_plan"]
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        if past_memories:
            for i, rec in enumerate(past_memories, 1):
                past_memory_str += rec["recommendation"] + "\n\n"
        else:
            past_memory_str = "No past memories found."

        context = {
            "role": "user",
            "content": f"基于分析师团队的综合分析，以下是为{company_name}量身定制的投资计划。该计划整合了当前技术市场趋势、宏观经济指标和社交媒体情绪的洞察。请以此计划为基础评估你的下一个交易决策。\n\n拟议投资计划：{investment_plan}\n\n请利用这些洞察做出明智且具有战略性的决策。",
        }

        messages = [
            {
                "role": "system",
                "content": f"""你是一名交易代理，负责分析市场数据以做出投资决策。你可以做出以下四种决策：

- **做多**：开多仓，预期价格上涨获利
- **做空**：开空仓，预期价格下跌获利
- **平仓**：关闭当前持有的多仓或空仓
- **持有**：维持当前仓位不变

根据你的分析，提供具体的交易建议。以明确的决定结束，并始终在回复末尾加上'最终交易建议：**做多/做空/平仓/持有**'以确认你的建议。不要忘记利用过去决策的经验教训来从错误中学习。以下是你在类似交易情况下的一些反思和经验教训：{past_memory_str}。请使用中文输出。""",
            },
            context,
        ]

        result = llm.invoke(messages)

        return {
            "messages": [result],
            "trader_investment_plan": result.content,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
