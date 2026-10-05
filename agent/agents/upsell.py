# agent/agents/upsell.py
"""Upsell agent — Phase 2 mein implement hoga."""
from agent.agents.base import BaseAgent

class UpsellAgent(BaseAgent):
    name = "upsell"

    def handle(self, message: str, context: list) -> dict:
        return {
            "reply": "[Phase 2] Upsell agent yahan reply dega",
            "tool_calls": [],
            "tokens_used": 0,
        }