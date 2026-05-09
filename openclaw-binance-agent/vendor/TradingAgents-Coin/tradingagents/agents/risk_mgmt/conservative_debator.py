from langchain_core.messages import AIMessage
import time
import json


def create_conservative_debator(llm):
    def conservative_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        prompt = f"""作为保守型风险分析师，你的首要目标是保护资产、最小化波动性并确保稳定可靠的增长。你优先考虑稳定性、安全性和风险缓解，仔细评估潜在损失、经济衰退和市场波动。在评估交易者的决策或计划时，批判性地审查高风险因素——无论是做多还是做空方向的风险。对于做空建议，你需要特别关注做空的无限亏损风险、逼空风险和保证金风险。指出决策可能使资产面临不当风险的地方，以及更谨慎的替代方案可以确保长期收益的地方。以下是交易者的决策：

{trader_decision}

你的任务是积极反驳激进型和中立型分析师的论点，指出他们的观点可能忽视潜在威胁或未能优先考虑可持续性的地方。对于做空建议，你需要评估：做空的风险回报比是否合理、是否有足够的下行空间支撑做空、逼空风险有多大。直接回应他们的观点，利用以下数据来源为交易者决策的低风险调整方案构建令人信服的论证：

市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新时事报告：{news_report}
公司基本面报告：{fundamentals_report}
当前对话历史：{history} 激进型分析师的最新回应：{current_aggressive_response} 中立型分析师的最新回应：{current_neutral_response}。如果其他观点尚无回应，不要编造内容，只需陈述你的观点。

通过质疑他们的乐观态度并强调他们可能忽视的潜在下行风险来积极参与。回应他们的每个反驳观点，展示为什么保守立场最终是资产最安全的路径。专注于辩论和批评他们的论点，以展示低风险策略相对于他们方法的优势。以对话方式输出，就像自然说话一样，不使用任何特殊格式。请使用中文输出。"""

        response = llm.invoke(prompt)

        argument = f"保守型分析师：{response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
