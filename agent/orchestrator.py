# agent/orchestrator.py
"""
Orchestrator — customer message aane pe intent classify karke
sahi agent ko route karta hai.

Phase 0: skeleton + intent classification.
Phase 1: actual agent invocation.
"""
import json
import re
import time
from typing import Optional
from groq import Groq
from config import GROQ_API_KEY, AI_MODEL
from logger import get_logger
from agent.prompts import ORCHESTRATOR_PROMPT, HUMAN_HANDOFF_MESSAGE
from agent.guardrails import is_stop_message, check_all_guards
from agent import memory
from worker.db_worker import (
    find_customer_by_phone, get_shop_agent_settings,
    write_audit, set_consent, cancel_tasks,
)

log = get_logger("dukaan.agent.orchestrator")
client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# INTENT CLASSIFICATION
# =========================================================

def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    bracket = re.search(r"(\{.*\})", raw, re.DOTALL)
    if bracket:
        return json.loads(bracket.group(1))
    raise ValueError("No JSON in LLM response")


def classify_intent(message: str) -> dict:
    """
    Return: {"agent": "order|tracking|reminder|upsell|cold_call|human",
             "confidence": 0.0-1.0,
             "reason": "..."}
    """
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": ORCHESTRATOR_PROMPT},
                {"role": "user", "content": message},
            ],
        )
        data = _extract_json(response.choices[0].message.content)
        if "agent" not in data:
            data["agent"] = "human"
            data["confidence"] = 0.0
        return data
    except Exception as e:
        log.error(f"classify_intent failed: {e}")
        return {"agent": "human", "confidence": 0.0, "reason": f"error: {e}"}


# =========================================================
# MAIN HANDLER
# =========================================================

def handle_message(
    shop_id: int,
    channel: str,
    external_id: str,
    message: str,
    customer_id: Optional[int] = None,
) -> dict:
    """
    Main entry point — Vercel webhook isko call karega (worker ke through).

    Flow:
    1. STOP detect → opt-out + audit
    2. Customer identify (phone se)
    3. Conversation resume/create
    4. Guardrails check
    5. Intent classify
    6. Route to agent (Phase 1: actual; Phase 0: stub)
    7. Log everything
    """
    started = time.time()
    log.info(f"[handle] shop={shop_id} ch={channel} ext={external_id[:10]}...")

    # ---- 1. STOP detection ----
    if is_stop_message(message):
        log.info(f"STOP detected from {external_id}")
        if customer_id:
            set_consent(shop_id, customer_id, channel, False, "user_reply_stop")
            cancel_tasks("reminder", shop_id, customer_id)
            cancel_tasks("upsell", shop_id, customer_id)
        from agent.prompts import STOP_ACK_MESSAGE
        write_audit(
            shop_id=shop_id, agent_name="orchestrator",
            action="opt_out", customer_id=customer_id,
            input_data={"message": message},
            success=True,
        )
        return {
            "success": True,
            "action": "opt_out",
            "reply": STOP_ACK_MESSAGE,
        }

    # ---- 2. Customer identification ----
    if customer_id is None and channel == "whatsapp":
        cust = find_customer_by_phone(shop_id, external_id)
        if cust:
            customer_id = cust["customer_id"]

    # ---- 3. Conversation ----
    conv_id = memory.start_or_resume_conversation(
        shop_id=shop_id, channel=channel,
        external_id=external_id, customer_id=customer_id,
    )
    if conv_id is None:
        return {"success": False, "error": "conversation_create_failed"}

    memory.add_user_message(conv_id, message)

    # ---- 4. Guardrails ----
    settings = get_shop_agent_settings(shop_id)
    if customer_id:
        allowed, reason = check_all_guards(shop_id, customer_id, channel, settings)
        if not allowed:
            log.info(f"Guardrail blocked: {reason}")
            write_audit(
                shop_id=shop_id, agent_name="orchestrator",
                action="guardrail_blocked", conversation_id=conv_id,
                customer_id=customer_id,
                input_data={"message": message, "reason": reason},
                success=False,
            )
            return {
                "success": True,
                "action": "blocked",
                "reason": reason,
                "reply": "",   # no reply — respect quiet hours / rate limit
            }

    # ---- 5. Intent classification ----
    intent = classify_intent(message)
    log.info(f"Intent: {intent.get('agent')} ({intent.get('confidence')})")

    # ---- 6. Route to agent ----
    agent_name = intent.get("agent", "human")
    confidence = float(intent.get("confidence", 0.0))

    # Low confidence → human
    if confidence < 0.6:
        agent_name = "human"

    # ---- 7. Phase 0 stub; Phase 1 actual agents ----
    if agent_name == "human":
        reply = HUMAN_HANDOFF_MESSAGE
        memory.add_agent_message(conv_id, "human", reply)
        memory.end_conversation(conv_id, status="escalated")
    else:
        # Phase 1: yahan actual agent call hoga
        reply = f"[Phase 1] {agent_name} agent yahan reply dega"
        memory.add_agent_message(conv_id, agent_name, reply)

    latency_ms = int((time.time() - started) * 1000)

    # ---- 8. Audit ----
    write_audit(
        shop_id=shop_id, agent_name="orchestrator",
        action="message_handled", conversation_id=conv_id,
        customer_id=customer_id,
        input_data={"message": message, "channel": channel},
        output_data={"agent": agent_name, "confidence": confidence},
        latency_ms=latency_ms, success=True,
    )

    return {
        "success": True,
        "action": "replied",
        "agent": agent_name,
        "confidence": confidence,
        "conversation_id": conv_id,
        "reply": reply,
        "latency_ms": latency_ms,
    }