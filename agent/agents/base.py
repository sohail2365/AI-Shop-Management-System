# agent/agents/base.py
"""
Base agent — saare agents isko inherit karenge (Phase 1).
Phase 0 mein interface define karta hai.
"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseAgent(ABC):
    name: str = "base"

    def __init__(self, shop_id: int, conversation_id: int,
                 customer_id: Optional[int] = None):
        self.shop_id = shop_id
        self.conversation_id = conversation_id
        self.customer_id = customer_id

    @abstractmethod
    def handle(self, message: str, context: list) -> dict:
        """
        Return: {"reply": str, "tool_calls": list, "tokens_used": int}
        """
        pass