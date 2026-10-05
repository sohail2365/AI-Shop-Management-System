# agent/agents/cold_call.py
"""Cold call agent — Phase 3 mein implement hoga."""
from agent.agents.base import BaseAgent

class ColdCallAgent(BaseAgent):
    name = "cold_call"

    def handle(self, message: str, context: list) -> dict:
        return {
            "reply": "[Phase 3] Cold call agent yahan reply dega",
            "tool_calls": [],
            "tokens_used": 0,
        }