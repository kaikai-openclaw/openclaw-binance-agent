import time
import json


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        prompt = f"""作为中立型风险分析师，你的角色是提供平衡的视角，权衡交易者决策或计划的潜在收益和风险。你优先考虑全面的方法，评估上行和下行空间，同时考虑更广泛的市场趋势、潜在的经济变化和多元化策略。你需要客观评估做多和做空两个方向的可行性。以下是交易者的决策：

{trader_decision}

你的任务是挑战激进型和保守型分析师，指出每种观点可能过于乐观或过于谨慎的地方。对于做空建议，你需要平衡评估：做空的潜在收益与风险、当前市场环境是否适合做空、是否有更好的替代策略（如持有或平仓）。利用以下数据来源的洞察来支持对交易者决策的适度、可持续的策略调整：

市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新时事报告：{news_report}
公司基本面报告：{fundamentals_report}
当前对话历史：{history} 激进型分析师的最新回应：{current_aggressive_response} 保守型分析师的最新回应：{current_conservative_response}。如果其他观点尚无回应，不要编造内容，只需陈述你的观点。

通过批判性地分析双方来积极参与，指出激进型和保守型论点中的弱点，倡导更平衡的方法。挑战他们的每个观点，说明为什么适度的风险策略可能兼具两者之长，在防范极端波动的同时提供增长潜力。专注于辩论而非简单呈现数据，旨在展示平衡的观点能带来最可靠的结果。以对话方式输出，就像自然说话一样，不使用任何特殊格式。请使用中文输出。"""

        response = llm.invoke(prompt)

        argument = f"中立型分析师：{response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
