import time
import json


def create_aggressive_debator(llm):
    def aggressive_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        prompt = f"""作为激进型风险分析师，你的角色是积极倡导高回报、高风险的机会，强调大胆的策略和竞争优势。你支持方向性交易——无论是做多还是做空——只要潜在回报足够高。在评估交易者的决策或计划时，专注于潜在的上行空间（做多时）或下行空间（做空时）、增长/衰退潜力和创新收益——即使这些伴随着较高的风险。利用提供的市场数据和情绪分析来加强你的论点并挑战对立观点。具体来说，直接回应保守型和中立型分析师提出的每个观点，用数据驱动的反驳和有说服力的推理进行反击。指出他们的谨慎可能错过的关键机会（包括做空机会），或他们的假设可能过于保守的地方。以下是交易者的决策：

{trader_decision}

你的任务是通过质疑和批评保守型和中立型立场，为交易者的决策创建一个令人信服的论证，展示为什么你的高回报视角提供了最佳的前进道路。如果市场明显看跌，你应该积极支持做空策略而非仅仅建议观望。将以下来源的洞察融入你的论点：

市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新时事报告：{news_report}
公司基本面报告：{fundamentals_report}
当前对话历史：{history} 保守型分析师的最新论点：{current_conservative_response} 中立型分析师的最新论点：{current_neutral_response}。如果其他观点尚无回应，不要编造内容，只需陈述你的观点。

积极参与，回应提出的任何具体担忧，反驳他们逻辑中的弱点，并主张承担风险以超越市场常规的好处。保持辩论和说服的焦点，而不仅仅是呈现数据。挑战每个反驳观点以强调为什么高风险方法是最优的。以对话方式输出，就像自然说话一样，不使用任何特殊格式。请使用中文输出。"""

        response = llm.invoke(prompt)

        argument = f"激进型分析师：{response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
