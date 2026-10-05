# agent/agents/reminder.py
"""
Reminder Agent — Single connection + Templates + Fast.
"""
import time
import random
import threading
import json
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from database import get_connection
from logger import get_logger
from agent.messages import REMINDER_TEMPLATES, TONE_RULES, pick_time_context
from agent.guardrails import is_quiet_hour
from worker.db_worker import write_audit

log = get_logger("dukaan.agent.reminder")


def _fmt_rs(amount) -> str:
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


# =========================================================
# FETCH — sab queries ek connection mein
# =========================================================

def _fetch_all(conn, shop_id: int, customer_id: int) -> Optional[dict]:
    """
    Ek connection use karke 3 queries — customer, shop, consent.
    Connection caller (handle_reminder_task) banata hai.
    """
    # ---- Q1: Customer info ----
    cust_row = conn.execute(
        text("""
            SELECT
                c.customer_id, c.name, c.phone,
                COALESCE(SUM(CASE WHEN k.item_name != 'PAYMENT' THEN k.total ELSE 0 END), 0) as lifetime_value,
                COALESCE(SUM(CASE WHEN k.item_name = 'PAYMENT' THEN -k.total ELSE 0 END), 0) as total_paid,
                COALESCE(SUM(k.total), 0) as baaki,
                COUNT(k.id) as total_entries,
                MIN(CASE WHEN k.total > 0 THEN k.date END) as oldest_unpaid,
                MAX(CASE WHEN k.item_name = 'PAYMENT' THEN k.date END) as last_payment
            FROM customers c
            LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
            WHERE c.customer_id = :cid AND c.shop_id = :sid
            GROUP BY c.customer_id, c.name, c.phone
        """),
        {"cid": customer_id, "sid": shop_id}
    ).fetchone()

    if not cust_row:
        return None

    days_pending = 0
    if cust_row[7]:
        try:
            oldest = cust_row[7] if isinstance(cust_row[7], datetime) else datetime.fromisoformat(str(cust_row[7]))
            days_pending = max(0, (datetime.now() - oldest).days)
        except Exception:
            pass

    info = {
        "customer_id": cust_row[0],
        "name": cust_row[1],
        "phone": cust_row[2],
        "lifetime_value": float(cust_row[3]),
        "total_paid": float(cust_row[4]),
        "baaki": float(cust_row[5]),
        "total_entries": int(cust_row[6]) if cust_row[6] else 0,
        "days_pending": days_pending,
        "last_payment": str(cust_row[8]) if cust_row[8] else None,
    }

    # ---- Q2: Shop + settings ----
    shop_row = conn.execute(
        text("SELECT shop_name, agent_settings FROM shops WHERE shop_id = :sid"),
        {"sid": shop_id}
    ).fetchone()

    if shop_row:
        info["shop_name"] = shop_row[0] or "Dukaan"
        db_settings = shop_row[1] or {}
        if isinstance(db_settings, str):
            try:
                db_settings = json.loads(db_settings)
            except Exception:
                db_settings = {}
    else:
        info["shop_name"] = "Dukaan"
        db_settings = {}

    defaults = {
        "reminder_enabled": True,
        "quiet_hours_start": 20,
        "quiet_hours_end": 9,
    }
    info["settings"] = {**defaults, **db_settings}

    # ---- Q3: Consent ----
    consent_row = conn.execute(
        text("""
            SELECT opted_in FROM consents
            WHERE shop_id = :sid AND customer_id = :cid AND channel = 'whatsapp'
            LIMIT 1
        """),
        {"sid": shop_id, "cid": customer_id}
    ).fetchone()
    info["is_opted_in"] = True if consent_row is None else bool(consent_row[0])

    return info


# =========================================================
# TONE
# =========================================================

def _detect_customer_type(info: dict) -> str:
    if info["total_entries"] <= 3 and info["lifetime_value"] < 2000:
        return "new"

    vip_rules = TONE_RULES["vip"]["conditions"]
    if info["lifetime_value"] >= vip_rules.get("min_lifetime_value", 10000):
        if info["lifetime_value"] > 0:
            ratio = info["total_paid"] / info["lifetime_value"]
            if ratio >= vip_rules.get("min_payment_ratio", 0.7):
                return "vip"

    chronic = TONE_RULES["chronic"]["conditions"]
    if info["days_pending"] >= chronic.get("min_days_pending", 30):
        return "chronic"

    return "regular"


# =========================================================
# COMPOSE
# =========================================================

def _compose_template(info: dict, shop_name: str, customer_type: str) -> str:
    templates = REMINDER_TEMPLATES.get(customer_type, REMINDER_TEMPLATES["regular"])
    template = random.choice(templates)

    time_context = pick_time_context(info["days_pending"])
    if not time_context:
        template = template.replace("{time_context}\n", "")

    message = template.format(
        name=info["name"].title(),
        amount=_fmt_rs(info["baaki"]),
        shop_name=shop_name,
        time_context=time_context,
    )
    lines = [line for line in message.split("\n") if line.strip()]
    return "\n".join(lines)


# =========================================================
# AUDIT (background)
# =========================================================

def _write_audit_async(shop_id, customer_id, info, customer_type,
                       message, wa_result, latency_ms, scheduled_by):
    try:
        write_audit(
            shop_id=shop_id,
            agent_name="reminder",
            action="reminder_sent",
            customer_id=customer_id,
            input_data={"baaki": info["baaki"], "customer_type": customer_type,
                        "days_pending": info["days_pending"], "scheduled_by": scheduled_by},
            output_data={"message": message, "auto_sent": wa_result.get("auto_sent"),
                         "phone_valid": wa_result.get("phone_valid")},
            latency_ms=latency_ms,
            success=True,
        )
    except Exception as e:
        log.error(f"Background audit failed: {e}")


# =========================================================
# MAIN
# =========================================================

def handle_reminder_task(shop_id: int, payload: dict) -> dict:
    started = time.time()
    customer_id = payload.get("customer_id")
    if not customer_id:
        return {"status": "error", "reason": "customer_id missing"}

    log.info(f"Reminder task: shop={shop_id} customer={customer_id}")

    # ---- 1. ALL DB in ONE connection ----
    t = time.time()
    try:
        with get_connection() as conn:
            info = _fetch_all(conn, shop_id, customer_id)
    except Exception as e:
        log.error(f"DB fetch failed: {e}")
        return {"status": "error", "reason": f"db_error: {e}"}

    log.info(f"[TIMING] db_all: {int((time.time()-t)*1000)}ms")
    if not info:
        return {"status": "error", "reason": "customer_not_found"}

    if info["baaki"] <= 0:
        return {"status": "skipped", "reason": "no_baaki"}

    # ---- 2. Guardrails (pure Python) ----
    settings = info["settings"]
    if not settings.get("reminder_enabled", True):
        return {"status": "blocked", "reason": "reminders_disabled"}
    if is_quiet_hour(settings):
        return {"status": "blocked", "reason": "quiet_hours"}
    if not info["is_opted_in"]:
        return {"status": "blocked", "reason": "opted_out"}

    # ---- 3. Type + compose ----
    customer_type = _detect_customer_type(info)
    log.info(f"Type: {customer_type}, baaki: Rs.{info['baaki']}, days: {info['days_pending']}")

    message = _compose_template(info, info["shop_name"], customer_type)

    # ---- 4. WhatsApp ----
    from whatsapp import build_whatsapp_payload
    wa_result = build_whatsapp_payload(info["phone"], message, auto_send=True)

    # ---- 5. Audit (background) ----
    latency_ms = int((time.time() - started) * 1000)
    threading.Thread(
        target=_write_audit_async,
        args=(shop_id, customer_id, info, customer_type,
              message, wa_result, latency_ms,
              payload.get("scheduled_by", "unknown")),
        daemon=True,
    ).start()

    log.info(f"Sent: {info['name']} ({customer_type}) in {latency_ms}ms")

    return {
        "status": "sent",
        "customer": info["name"],
        "customer_type": customer_type,
        "message": message,
        "auto_sent": wa_result.get("auto_sent", False),
        "link": wa_result.get("link"),
        "latency_ms": latency_ms,
    }