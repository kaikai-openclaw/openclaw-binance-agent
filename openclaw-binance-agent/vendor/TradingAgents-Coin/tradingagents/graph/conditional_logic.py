# TradingAgents/graph/conditional_logic.py

import logging
from tradingagents.agents.utils.agent_states import AgentState

logger = logging.getLogger(__name__)

# Maximum tool call rounds per analyst before forcing completion
MAX_TOOL_ROUNDS = 15


class ConditionalLogic:
    """Handles conditional logic for determining graph flow."""

    def __init__(self, max_debate_rounds=1, max_risk_discuss_rounds=1):
        """Initialize with configuration parameters."""
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds
        self._tool_rounds = {}

    def _check_analyst_tools(self, state, analyst_key, tools_route, clear_route):
        """Check if analyst should continue calling tools, with a safety limit."""
        messages = state["messages"]
        last_message = messages[-1]

        if last_message.tool_calls:
            count = self._tool_rounds.get(analyst_key, 0) + 1
            self._tool_rounds[analyst_key] = count
            if count > MAX_TOOL_ROUNDS:
                logger.warning(
                    f"{analyst_key} analyst exceeded {MAX_TOOL_ROUNDS} tool rounds, forcing completion"
                )
                self._tool_rounds[analyst_key] = 0
                return clear_route
            return tools_route

        self._tool_rounds[analyst_key] = 0
        return clear_route

    def should_continue_market(self, state: AgentState):
        return self._check_analyst_tools(state, "market", "tools_market", "Msg Clear Market")

    def should_continue_social(self, state: AgentState):
        return self._check_analyst_tools(state, "social", "tools_social", "Msg Clear Social")

    def should_continue_news(self, state: AgentState):
        return self._check_analyst_tools(state, "news", "tools_news", "Msg Clear News")

    def should_continue_fundamentals(self, state: AgentState):
        return self._check_analyst_tools(state, "fundamentals", "tools_fundamentals", "Msg Clear Fundamentals")

    def should_continue_debate(self, state: AgentState) -> str:
        """Determine if investment debate should continue."""
        debate = state["investment_debate_state"]

        if debate["count"] >= 2 * self.max_debate_rounds:
            return "Research Manager"

        current = debate["current_response"]
        # Bull researcher sets: "看多分析师：..."
        if current.startswith("Bull") or current.startswith("看多"):
            return "Bear Researcher"
        # Bear researcher sets: "看空分析师：..." or anything else → back to Bull
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """Determine if risk analysis debate should continue."""
        risk = state["risk_debate_state"]

        if risk["count"] >= 3 * self.max_risk_discuss_rounds:
            return "Portfolio Manager"

        # latest_speaker is hardcoded English: "Aggressive", "Conservative", "Neutral"
        speaker = risk["latest_speaker"]
        if speaker == "Aggressive":
            return "Conservative Analyst"
        if speaker == "Conservative":
            return "Neutral Analyst"
        return "Aggressive Analyst"
