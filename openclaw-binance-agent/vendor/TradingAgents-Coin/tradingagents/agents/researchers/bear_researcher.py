from langchain_core.messages import AIMessage
import time
import json


def create_bear_researcher(llm, memory):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

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

        prompt = f"""你是一名看空分析师，主张做空该标的或避免做多。你的目标是提出一个论据充分的论点，强调风险、挑战和负面指标，并在下行趋势明确时积极建议做空获利。利用提供的研究和数据来突出潜在的下行风险并有效反驳看多论点。

重点关注：

- 做空机会：当技术指标、基本面或市场情绪明确指向下跌趋势时，明确建议做空（开空仓）而非仅仅"不买入"。分析做空的潜在收益和风险回报比。
- 风险和挑战：突出市场饱和、财务不稳定或可能阻碍标的表现的宏观经济威胁等因素。
- 竞争劣势：强调较弱的市场定位、创新下降或来自竞争对手的威胁等脆弱性。
- 负面指标：使用财务数据、市场趋势或近期不利新闻的证据来支持你的立场。
- 反驳看多观点：用具体数据和合理推理批判性分析看多论点，揭露弱点或过于乐观的假设。
- 互动性：以对话风格呈现你的论点，直接回应看多分析师的观点并有效辩论，而不仅仅是列举事实。

可用资源：

市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新时事新闻：{news_report}
公司基本面报告：{fundamentals_report}
辩论对话历史：{history}
上一轮看多论点：{current_response}
类似情况的反思和经验教训：{past_memory_str}
请利用这些信息提出令人信服的看空论点，反驳看多方的主张，并进行动态辩论以展示做空该标的的理由和潜在收益。当下行趋势明确时，你应该明确建议做空而非仅仅建议"不买入"或"卖出持仓"。你还必须回应反思并从过去的经验教训和错误中学习。请使用中文输出。
"""

        response = llm.invoke(prompt)

        argument = f"看空分析师：{response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
