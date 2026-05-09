import time
import json


def create_risk_manager(llm, memory):
    def risk_manager_node(state) -> dict:

        company_name = state["company_of_interest"]

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        market_research_report = state["market_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        sentiment_report = state["sentiment_report"]
        trader_plan = state["investment_plan"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        prompt = f"""作为风险管理裁判和辩论主持人，你的目标是评估三位风险分析师——激进型、中立型和保守型——之间的辩论，并为交易者确定最佳行动方案。你的决定必须产生明确的建议：做多、做空、平仓或持有。

决策选项说明：
- **做多**：开多仓，预期价格上涨获利。
- **做空**：开空仓，预期价格下跌获利。当下行趋势明确且风险可控时选择。
- **平仓**：关闭当前持有的多仓或空仓。当趋势反转或风险过高时选择。
- **持有**：维持当前仓位不变。仅在有具体论点强有力地支持时才选择，而不是在各方观点都有道理时作为默认选项。

力求清晰和果断。

决策指南：
1. **总结关键论点**：提取每位分析师最有力的观点，聚焦于与当前情境的相关性。
2. **提供理由**：用辩论中的直接引用和反驳论点来支持你的建议。
3. **完善交易者的计划**：从交易者的原始计划**{trader_plan}**开始，根据分析师的洞察进行调整。
4. **从过去的错误中学习**：利用**{past_memory_str}**中的经验教训来纠正之前的误判，改进你当前的决策，确保不会做出导致亏损的错误决定。

交付成果：
- 清晰且可操作的建议：做多、做空、平仓或持有。
- 基于辩论和过去反思的详细推理。

---

**分析师辩论历史：**
{history}

---

聚焦于可操作的洞察和持续改进。基于过去的经验教训，批判性地评估所有观点，确保每个决策都能带来更好的结果。请使用中文输出。"""

        response = llm.invoke(prompt)

        new_risk_debate_state = {
            "judge_decision": response.content,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": response.content,
        }

    return risk_manager_node
