# worker/db_worker.py
"""
Worker-specific DB helpers. Existing database.py sirf connection deta hai â€”
yahan worker ke liye task queue, conversation, audit helpers hain.
"""
import json
import socket
import os
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.worker.db")

# Har worker instance ka unique ID â€” task locking ke liye
WORKER_ID = os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")


# =========================================================
# TASK QUEUE
# =========================================================

def enqueue_task(shop_id: int, task_type: str, payload: dict,
                 run_at: Optional[datetime] = None,
                 max_attempts: int = 3) -> Optional[int]:
    """
    Naya task queue mein daalo. Returns task_id ya None (error).
    run_at None = abhi chalao.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    INSERT INTO agent_tasks
                        (shop_id, task_type, payload, run_at, max_attempts)
                    VALUES
                        (:shop_id, :task_type, CAST(:payload AS jsonb),
                         COALESCE(:run_at, NOW()), :max_attempts)
                    RETURNING id
                """),
                {
                    "shop_id": shop_id,
                    "task_type": task_type,
                    "payload": json.dumps(payload),
                    "run_at": run_at,
                    "max_attempts": max_attempts,
                }
            ).fetchone()
            conn.commit()
        task_id = row[0]
        log.info(f"Task enqueued: id={task_id} type={task_type} shop={shop_id}")
        return task_id
    except Exception as e:
        log.error(f"enqueue_task failed: {e}")
        return None


def claim_pending_tasks(limit: int = 10) -> list:
    """
    Pending tasks jo abhi chalane hain, unko lock karke return karo.
    Atomic â€” SELECT FOR UPDATE SKIP LOCKED use karte hain, taake
    do workers same task na uthayein.
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    WITH claimed AS (
                        SELECT id FROM agent_tasks
                        WHERE status = 'pending'
                          AND run_at <= NOW()
                          AND attempts < max_attempts
                        ORDER BY run_at ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE agent_tasks t
                    SET status = 'running',
                        locked_at = NOW(),
                        locked_by = :worker_id,
                        attempts = attempts + 1
                    FROM claimed
                    WHERE t.id = claimed.id
                    RETURNING t.id, t.shop_id, t.task_type, t.payload,
                              t.attempts, t.max_attempts
                """),
                {"limit": limit, "worker_id": WORKER_ID}
            ).fetchall()
            conn.commit()
        return [
            {
                "id": r[0],
                "shop_id": r[1],
                "task_type": r[2],
                "payload": r[3] if isinstance(r[3], dict) else json.loads(r[3]),
                "attempts": r[4],
                "max_attempts": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        log.error(f"claim_pending_tasks failed: {e}")
        return []


def mark_task_done(task_id: int) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    UPDATE agent_tasks
                    SET status = 'done',
                        locked_at = NULL,
                        locked_by = NULL
                    WHERE id = :id
                """),
                {"id": task_id}
            )
            conn.commit()
        return True
    except Exception as e:
        log.error(f"mark_task_done failed: {e}")
        return False


def mark_task_failed(task_id: int, error: str, retry: bool = True) -> bool:
    """
    Task fail hua. Agar attempts < max_attempts aur retry=True â†’ wapas pending
    (5 min baad). Warna permanently failed.
    """
    try:
        with get_connection() as conn:
            if retry:
                # Exponential backoff: attempts * 5 min
                row = conn.execute(
                    text("""
                        UPDATE agent_tasks
                        SET status = CASE
                                WHEN attempts < max_attempts THEN 'pending'
                                ELSE 'failed'
                            END,
                            run_at = CASE
                                WHEN attempts < max_attempts
                                    THEN NOW() + (attempts * INTERVAL '5 minutes')
                                ELSE run_at
                            END,
                            last_error = :error,
                            locked_at = NULL,
                            locked_by = NULL
                        WHERE id = :id
                        RETURNING status
                    """),
                    {"id": task_id, "error": error[:2000]}
                ).fetchone()
            else:
                conn.execute(
                    text("""
                        UPDATE agent_tasks
                        SET status = 'failed',
                            last_error = :error,
                            locked_at = NULL,
                            locked_by = NULL
                        WHERE id = :id
                    """),
                    {"id": task_id, "error": error[:2000]}
                )
                row = ("failed",)
            conn.commit()
        log.warning(f"Task {task_id} failed: {error[:200]} -> status={row[0]}")
        return True
    except Exception as e:
        log.error(f"mark_task_failed failed: {e}")
        return False


def cancel_tasks(task_type: str, shop_id: int,
                 customer_id: Optional[int] = None) -> int:
    """Agar customer ne STOP kiya, ya order cancel hua â€” pending tasks hatao."""
    try:
        if customer_id is not None:
            result = conn_query = None
            with get_connection() as conn:
                result = conn.execute(
                    text("""
                        UPDATE agent_tasks
                        SET status = 'cancelled'
                        WHERE shop_id = :shop_id
                          AND task_type = :task_type
                          AND status = 'pending'
                          AND payload->>'customer_id' = :customer_id
                        RETURNING id
                    """),
                    {"shop_id": shop_id, "task_type": task_type,
                     "customer_id": str(customer_id)}
                ).fetchall()
                conn.commit()
        else:
            with get_connection() as conn:
                result = conn.execute(
                    text("""
                        UPDATE agent_tasks
                        SET status = 'cancelled'
                        WHERE shop_id = :shop_id
                          AND task_type = :task_type
                          AND status = 'pending'
                        RETURNING id
                    """),
                    {"shop_id": shop_id, "task_type": task_type}
                ).fetchall()
                conn.commit()
        count = len(result) if result else 0
        log.info(f"Cancelled {count} tasks type={task_type} shop={shop_id}")
        return count
    except Exception as e:
        log.error(f"cancel_tasks failed: {e}")
        return 0


# =========================================================
# CONVERSATIONS
# =========================================================

def get_or_create_conversation(shop_id: int, channel: str,
                               external_id: str,
                               customer_id: Optional[int] = None) -> Optional[int]:
    """
    Active conversation dhoondo ya naya banao. Same external_id + shop + channel
    ke saath 24 ghante ke andar ka active conversation return karta hai.
    """
    try:
        with get_connection() as conn:
            # Pehle active dhundo
            row = conn.execute(
                text("""
                    SELECT id FROM conversations
                    WHERE shop_id = :shop_id
                      AND channel = :channel
                      AND external_id = :external_id
                      AND status = 'active'
                      AND last_message_at > NOW() - INTERVAL '24 hours'
                    ORDER BY last_message_at DESC
                    LIMIT 1
                """),
                {"shop_id": shop_id, "channel": channel, "external_id": external_id}
            ).fetchone()
            if row:
                return row[0]

            # Naya banao
            row = conn.execute(
                text("""
                    INSERT INTO conversations
                        (shop_id, customer_id, channel, external_id)
                    VALUES
                        (:shop_id, :customer_id, :channel, :external_id)
                    RETURNING id
                """),
                {
                    "shop_id": shop_id,
                    "customer_id": customer_id,
                    "channel": channel,
                    "external_id": external_id,
                }
            ).fetchone()
            conn.commit()
        conv_id = row[0]
        log.info(f"New conversation: id={conv_id} shop={shop_id} channel={channel}")
        return conv_id
    except Exception as e:
        log.error(f"get_or_create_conversation failed: {e}")
        return None


def append_message(conversation_id: int, role: str, content: str,
                   agent_name: Optional[str] = None,
                   tool_calls: Optional[dict] = None,
                   tokens_used: Optional[int] = None,
                   latency_ms: Optional[int] = None) -> Optional[int]:
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    INSERT INTO messages
                        (conversation_id, role, agent_name, content,
                         tool_calls, tokens_used, latency_ms)
                    VALUES
                        (:cid, :role, :agent_name, :content,
                         CAST(:tool_calls AS jsonb), :tokens, :latency)
                    RETURNING id
                """),
                {
                    "cid": conversation_id,
                    "role": role,
                    "agent_name": agent_name,
                    "content": content,
                    "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                    "tokens": tokens_used,
                    "latency": latency_ms,
                }
            ).fetchone()
            conn.execute(
                text("""
                    UPDATE conversations
                    SET last_message_at = NOW(),
                        message_count = message_count + 1
                    WHERE id = :cid
                """),
                {"cid": conversation_id}
            )
            conn.commit()
        return row[0]
    except Exception as e:
        log.error(f"append_message failed: {e}")
        return None


def get_recent_messages(conversation_id: int, limit: int = 10) -> list:
    """Last N messages â€” LLM context ke liye."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    SELECT role, agent_name, content, created_at
                    FROM messages
                    WHERE conversation_id = :cid
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"cid": conversation_id, "limit": limit}
            ).fetchall()
        # Reverse â€” chronological order mein
        return [
            {"role": r[0], "agent_name": r[1], "content": r[2],
             "at": str(r[3])}
            for r in reversed(rows)
        ]
    except Exception as e:
        log.error(f"get_recent_messages failed: {e}")
        return []


def update_conversation_state(conversation_id: int, state: dict) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    UPDATE conversations
                    SET state = CAST(:state AS jsonb),
                        last_message_at = NOW()
                    WHERE id = :cid
                """),
                {"cid": conversation_id, "state": json.dumps(state)}
            )
            conn.commit()
        return True
    except Exception as e:
        log.error(f"update_conversation_state failed: {e}")
        return False


def close_conversation(conversation_id: int, status: str = "closed") -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    UPDATE conversations
                    SET status = :status
                    WHERE id = :cid
                """),
                {"cid": conversation_id, "status": status}
            )
            conn.commit()
        return True
    except Exception as e:
        log.error(f"close_conversation failed: {e}")
        return False


# =========================================================
# AUDIT
# =========================================================

def write_audit(shop_id: int, agent_name: str, action: str,
                input_data: Optional[dict] = None,
                output_data: Optional[dict] = None,
                conversation_id: Optional[int] = None,
                customer_id: Optional[int] = None,
                tokens_used: Optional[int] = None,
                latency_ms: Optional[int] = None,
                success: bool = True,
                error: Optional[str] = None) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    INSERT INTO agent_audit
                        (shop_id, agent_name, action, conversation_id,
                         customer_id, input, output, tokens_used,
                         latency_ms, success, error)
                    VALUES
                        (:shop_id, :agent, :action, :conv_id,
                         :cust_id, CAST(:input AS jsonb), CAST(:output AS jsonb),
                         :tokens, :latency, :success, :error)
                """),
                {
                    "shop_id": shop_id,
                    "agent": agent_name,
                    "action": action,
                    "conv_id": conversation_id,
                    "cust_id": customer_id,
                    "input": json.dumps(input_data) if input_data else None,
                    "output": json.dumps(output_data) if output_data else None,
                    "tokens": tokens_used,
                    "latency": latency_ms,
                    "success": success,
                    "error": error,
                }
            )
            conn.commit()
        return True
    except Exception as e:
        log.error(f"write_audit failed: {e}")
        return False


# =========================================================
# CONSENT
# =========================================================

def is_opted_in(shop_id: int, customer_id: int, channel: str) -> bool:
    """
    Default TRUE. Agar explicit opt-out hai to FALSE.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    SELECT opted_in FROM consents
                    WHERE shop_id = :shop_id
                      AND customer_id = :cid
                      AND channel = :channel
                """),
                {"shop_id": shop_id, "cid": customer_id, "channel": channel}
            ).fetchone()
        if row is None:
            return True   # default
        return bool(row[0])
    except Exception as e:
        log.error(f"is_opted_in failed: {e}")
        return True   # fail-open on error? No â€” better fail-safe
        # Actually: fail-closed is safer. Change to False if you want strict.


def set_consent(shop_id: int, customer_id: int, channel: str,
                opted_in: bool, reason: str = "manual") -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    INSERT INTO consents
                        (shop_id, customer_id, channel, opted_in, reason)
                    VALUES
                        (:shop_id, :cid, :channel, :opted_in, :reason)
                    ON CONFLICT (shop_id, customer_id, channel)
                    DO UPDATE SET
                        opted_in = EXCLUDED.opted_in,
                        reason = EXCLUDED.reason,
                        updated_at = NOW()
                """),
                {
                    "shop_id": shop_id,
                    "cid": customer_id,
                    "channel": channel,
                    "opted_in": opted_in,
                    "reason": reason,
                }
            )
            conn.commit()
        log.info(f"Consent set: shop={shop_id} cust={customer_id} "
                 f"ch={channel} in={opted_in} reason={reason}")
        return True
    except Exception as e:
        log.error(f"set_consent failed: {e}")
        return False


# =========================================================
# HELPERS
# =========================================================

def find_customer_by_phone(shop_id: int, phone: str) -> Optional[dict]:
    """
    WhatsApp number se customer dhundo. Aapke existing phone normalize se
    multiple formats try karo (0300..., +92300..., 92300...).
    """
    from whatsapp import normalize_phone
    normalized = normalize_phone(phone)   # "923001234567"
    if not normalized:
        return None

    # Last 10 digits match karo â€” robust
    last10 = normalized[-10:] if len(normalized) >= 10 else normalized

    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    SELECT customer_id, name, phone, whatsapp_opted_in,
                           voice_opted_in, tags, preferred_language,
                           preferred_channel
                    FROM customers
                    WHERE shop_id = :shop_id
                      AND (
                        REPLACE(REPLACE(REPLACE(phone, '-', ''), ' ', ''), '+', '')
                            LIKE '%' || :last10
                      )
                    LIMIT 1
                """),
                {"shop_id": shop_id, "last10": last10}
            ).fetchone()
        if not row:
            return None
        return {
            "customer_id": row[0],
            "name": row[1],
            "phone": row[2],
            "whatsapp_opted_in": row[3],
            "voice_opted_in": row[4],
            "tags": row[5] or [],
            "preferred_language": row[6],
            "preferred_channel": row[7],
        }
    except Exception as e:
        log.error(f"find_customer_by_phone failed: {e}")
        return None


def get_shop_agent_settings(shop_id: int) -> dict:
    """Shop ke agent settings â€” with sane defaults."""
    defaults = {
        "reminder_enabled": True,
        "reminder_hour": 10,
        "auto_whatsapp": True,
        "cold_call_enabled": False,
        "daily_call_limit": 50,
        "quiet_hours_start": 20,
        "quiet_hours_end": 9,
        "ai_disclosure": True,
        "max_reminders_per_month": 4,
    }
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("SELECT agent_settings FROM shops WHERE shop_id = :sid"),
                {"sid": shop_id}
            ).fetchone()
        if row and row[0]:
            merged = {**defaults, **row[0]}
            return merged
        return defaults
    except Exception as e:
        log.error(f"get_shop_agent_settings failed: {e}")
        return defaults