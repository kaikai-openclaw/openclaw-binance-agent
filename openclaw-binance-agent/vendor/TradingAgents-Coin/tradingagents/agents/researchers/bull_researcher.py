from langchain_core.messages import AIMessage
import time
import json


def create_bull_researcher(llm, memory):
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        prompt = f"""你是一名看多分析师，主张做多该标的。你的任务是构建一个强有力的、基于证据的论点，强调增长潜力、竞争优势和积极的市场指标。利用提供的研究和数据来回应担忧并有效反驳看空论点。

重点关注：
- 做多机会：当技术指标、基本面或市场情绪明确指向上涨趋势时，明确建议做多（开多仓）。分析做多的潜在收益和风险回报比。
- 增长潜力：突出标的的市场机会、收入预测和可扩展性。
- 竞争优势：强调独特产品、强大品牌或主导市场地位等因素。
- 积极指标：使用财务健康状况、行业趋势和近期正面新闻作为证据。
- 反驳看空和做空观点：用具体数据和合理推理批判性分析看空论点，特别是反驳做空建议，全面回应担忧，展示为什么做多观点更有说服力。
- 互动性：以对话风格呈现你的论点，直接回应看空分析师的观点并有效辩论，而不仅仅是列举数据。

可用资源：
市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新时事新闻：{news_report}
公司基本面报告：{fundamentals_report}
辩论对话历史：{history}
上一轮看空论点：{current_response}
类似情况的反思和经验教训：{past_memory_str}
请利用这些信息提出令人信服的做多论点，反驳看空方的担忧和做空建议，并进行动态辩论以展示做多立场的优势。你还必须回应反思并从过去的经验教训和错误中学习。请使用中文输出。
"""

        response = llm.invoke(prompt)

        argument = f"看多分析师：{response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
