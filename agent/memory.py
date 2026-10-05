# agent/memory.py
"""
Conversation memory — LangGraph state ke saath integrate hoga.
Phase 0 mein simple helper functions hain.
"""
from typing import Optional
from worker.db_worker import (
    get_or_create_conversation,
    append_message,
    get_recent_messages,
    update_conversation_state,
    close_conversation,
)
from logger import get_logger

log = get_logger("dukaan.agent.memory")


def start_or_resume_conversation(shop_id: int, channel: str,
                                 external_id: str,
                                 customer_id: Optional[int] = None) -> Optional[int]:
    return get_or_create_conversation(
        shop_id=shop_id,
        channel=channel,
        external_id=external_id,
        customer_id=customer_id,
    )


def add_user_message(conversation_id: int, content: str) -> Optional[int]:
    return append_message(conversation_id, role="user", content=content)


def add_agent_message(conversation_id: int, agent_name: str, content: str,
                      tool_calls: Optional[dict] = None,
                      tokens_used: Optional[int] = None,
                      latency_ms: Optional[int] = None) -> Optional[int]:
    return append_message(
        conversation_id, role="agent", agent_name=agent_name,
        content=content, tool_calls=tool_calls,
        tokens_used=tokens_used, latency_ms=latency_ms,
    )


def add_system_message(conversation_id: int, content: str) -> Optional[int]:
    return append_message(conversation_id, role="system", content=content)


def build_llm_context(conversation_id: int, limit: int = 10) -> list:
    """
    Groq/LLM messages format mein context banao.
    Returns: [{"role": "system|user|assistant", "content": "..."}]
    """
    msgs = get_recent_messages(conversation_id, limit=limit)
    context = []
    for m in msgs:
        role = m["role"]
        if role == "agent":
            role = "assistant"
        context.append({"role": role, "content": m["content"]})
    return context


def save_state(conversation_id: int, state: dict):
    update_conversation_state(conversation_id, state)


def end_conversation(conversation_id: int, status: str = "closed"):
    close_conversation(conversation_id, status)