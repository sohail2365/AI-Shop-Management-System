# agent/guardrails.py
"""
Guardrails — spam prevention, DNC, time windows, AI disclosure.
Agent ka har action yahan se pass karta hai.
"""
import re
from datetime import datetime, time as dtime
from typing import Optional, Tuple
from sqlalchemy import text
from database import get_connection
from logger import get_logger
from agent.prompts import STOP_KEYWORDS

log = get_logger("dukaan.agent.guardrails")


# =========================================================
# STOP / OPT-OUT DETECTION
# =========================================================

def is_stop_message(text: str) -> bool:
    """Customer ne STOP bola? Case-insensitive, Roman Urdu + English."""
    if not text:
        return False
    text_lower = text.lower().strip()
    # Exact short messages
    for kw in STOP_KEYWORDS:
        if kw in text_lower:
            return True
    return False


# =========================================================
# QUIET HOURS
# =========================================================

def is_quiet_hour(shop_settings: dict) -> bool:
    """
    Raat 8 baje se subah 9 baje tak — messages nahi.
    Return True if currently in quiet hours.
    """
    start = int(shop_settings.get("quiet_hours_start", 20))
    end = int(shop_settings.get("quiet_hours_end", 9))
    hour = datetime.now().hour
    if start > end:   # e.g. 20 -> 9 (wrap midnight)
        return hour >= start or hour < end
    else:
        return start <= hour < end


# =========================================================
# RATE LIMITING
# =========================================================

def rate_limit_check(shop_id: int, customer_id: int,
                     window_hours: int = 24,
                     max_messages: int = 3) -> Tuple[bool, str]:
    """
    Customer ko 24 ghante mein max N messages.
    Return (allowed, reason).
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    SELECT COUNT(*) FROM agent_audit
                    WHERE shop_id = :shop_id
                      AND customer_id = :cid
                      AND action IN ('send_whatsapp', 'send_sms')
                      AND created_at > NOW() - (:window || ' hours')::interval
                """),
                {"shop_id": shop_id, "cid": customer_id, "window": str(window_hours)}
            ).fetchone()
        count = row[0] if row else 0
        if count >= max_messages:
            return False, f"rate_limit_exceeded ({count}/{max_messages} in {window_hours}h)"
        return True, ""
    except Exception as e:
        log.error(f"rate_limit_check failed: {e}")
        # Fail-open? No. Fail-closed safer.
        return False, "rate_limit_check_error"


def daily_call_limit_check(shop_id: int, limit: int = 50) -> Tuple[bool, str]:
    """Roz max N calls per shop."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    SELECT COUNT(*) FROM call_logs
                    WHERE shop_id = :shop_id
                      AND direction = 'outbound'
                      AND created_at::date = CURRENT_DATE
                """),
                {"shop_id": shop_id}
            ).fetchone()
        count = row[0] if row else 0
        if count >= limit:
            return False, f"daily_call_limit_reached ({count}/{limit})"
        return True, ""
    except Exception as e:
        log.error(f"daily_call_limit_check failed: {e}")
        return False, "check_error"


# =========================================================
# CONSENT CHECK
# =========================================================

def can_contact(shop_id: int, customer_id: int, channel: str) -> Tuple[bool, str]:
    """Consent + DNC check."""
    from worker.db_worker import is_opted_in
    if not is_opted_in(shop_id, customer_id, channel):
        return False, f"opted_out_for_{channel}"
    return True, ""


# =========================================================
# PII REDACTION
# =========================================================

PHONE_RE = re.compile(r'(\+?92|0)?3\d{2}[\s-]?\d{7}')
CNIC_RE = re.compile(r'\d{5}-?\d{7}-?\d')

def redact_pii(text: str) -> str:
    """Logs mein PII redact karo."""
    if not text:
        return text
    text = PHONE_RE.sub('[PHONE]', text)
    text = CNIC_RE.sub('[CNIC]', text)
    return text


# =========================================================
# MASTER CHECK
# =========================================================

def check_all_guards(shop_id: int, customer_id: int,
                     channel: str, shop_settings: dict) -> Tuple[bool, str]:
    """
    Simplified guardrails — sirf zaroori checks.
    """
    # 1. Consent (ek query)
    ok, reason = can_contact(shop_id, customer_id, channel)
    if not ok:
        return False, reason

    # 2. Quiet hours (no DB)
    if is_quiet_hour(shop_settings):
        return False, "quiet_hours"

    return True, "ok"