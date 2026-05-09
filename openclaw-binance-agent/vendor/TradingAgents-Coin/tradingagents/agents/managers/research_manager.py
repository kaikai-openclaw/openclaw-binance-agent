import time
import json


def create_research_manager(llm, memory):
    def research_manager_node(state) -> dict:
        history = state["investment_debate_state"].get("history", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        investment_debate_state = state["investment_debate_state"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        prompt = f"""作为投资组合经理和辩论主持人，你的角色是批判性地评估本轮辩论，并做出明确的决定：支持看多分析师（做多）、看空分析师（做空），或仅在辩论论点强有力地支持时才选择持有或平仓。

你可以做出以下四种决策之一：
- **做多**：开多仓，预期价格上涨获利。当看多论点明显更有说服力时选择。
- **做空**：开空仓，预期价格下跌获利。当看空论点明显更有说服力且下行趋势明确时选择。
- **平仓**：关闭当前持有的多仓或空仓。当趋势反转或风险过高时选择。
- **持有**：维持当前仓位不变。仅在有具体论点强有力地支持时才选择，而不是作为默认选项。

简明扼要地总结双方的关键论点，聚焦于最有说服力的证据或推理。你的建议必须清晰且可操作。不要仅仅因为双方都有合理观点就默认选择持有；要基于辩论中最有力的论点做出明确立场。

此外，为交易者制定详细的投资计划，包括：

你的建议：基于最有说服力的论点做出的明确立场（做多/做空/平仓/持有）。
理由：解释为什么这些论点导致了你的结论。
战略行动：实施建议的具体步骤，包括建议的仓位方向和风险控制措施。
考虑你在类似情况下的过去错误。利用这些洞察来完善你的决策，确保你在不断学习和改进。以对话方式呈现你的分析，就像自然说话一样，不使用特殊格式。请使用中文输出。

以下是你过去的错误反思：
\"{past_memory_str}\"

以下是辩论内容：
辩论历史：
{history}"""
        response = llm.invoke(prompt)

        new_investment_debate_state = {
            "judge_decision": response.content,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": response.content,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": response.content,
        }

    return research_manager_node
