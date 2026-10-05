# agent/agents/tracking.py
"""
Tracking Agent — customer ko turant khaata summary deta hai.

Design:
  - Template-based (no LLM) — instant
  - Single DB connection
  - Roman Urdu
  - Last N entries (default 5)
  - Edge cases handled
"""
import time
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from database import get_connection
from logger import get_logger
from agent.messages import (
    TRACKING_HEADER, TRACKING_ENTRIES_HEADER, TRACKING_ENTRY_LINE,
    TRACKING_FOOTER, TRACKING_NO_BAAKI, TRACKING_CREDIT,
    TRACKING_NO_RECORD, TRACKING_NO_ENTRIES,
    TRACKING_INTENT_KEYWORDS,
)
from worker.db_worker import write_audit

log = get_logger("dukaan.agent.tracking")


# =========================================================
# HELPERS
# =========================================================

def _fmt_rs(amount) -> str:
    """1540.0 → '1,540'"""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def _fmt_date(d) -> str:
    """Date ko '15 Oct' format mein — WhatsApp mein clean lagta hai."""
    try:
        if isinstance(d, str):
            d = datetime.fromisoformat(d).date()
        elif isinstance(d, datetime):
            d = d.date()
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{d.day} {months[d.month - 1]}"
    except Exception:
        return str(d)


def _title_item(item_name: str) -> str:
    """'brite 500g' → 'Brite 500g'"""
    if not item_name:
        return ""
    if item_name.upper() == "PAYMENT":
        return "💰 Payment"
    return item_name.title()


# =========================================================
# INTENT DETECTION
# =========================================================

def detect_tracking_intent(message: str) -> bool:
    """
    Customer ne khaata poocha? Simple keyword match — no LLM.
    """
    if not message:
        return False
    msg_lower = message.lower().strip()
    return any(kw in msg_lower for kw in TRACKING_INTENT_KEYWORDS)


# =========================================================
# CUSTOMER LOOKUP + DATA FETCH — single connection
# =========================================================

def _fetch_tracking_data(shop_id: int, customer_id: int,
                         entries_limit: int = 5) -> Optional[dict]:
    """
    Sab kuch ek connection mein:
      1. Customer name + phone
      2. Total baaki
      3. Last N entries
      4. Shop name
    """
    try:
        with get_connection() as conn:
            # ---- Q1: Customer basic ----
            cust_row = conn.execute(
                text("""
                    SELECT customer_id, name, phone
                    FROM customers
                    WHERE customer_id = :cid AND shop_id = :sid
                """),
                {"cid": customer_id, "sid": shop_id}
            ).fetchone()

            if not cust_row:
                return None

            customer = {
                "customer_id": cust_row[0],
                "name": cust_row[1],
                "phone": cust_row[2],
            }

            # ---- Q2: Total baaki ----
            baaki_row = conn.execute(
                text("""
                    SELECT COALESCE(SUM(total), 0) as baaki
                    FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL
                """),
                {"cid": customer_id}
            ).fetchone()
            customer["baaki"] = float(baaki_row[0]) if baaki_row else 0

            # ---- Q3: Last N entries ----
            entries_rows = conn.execute(
                text("""
                    SELECT id, date, item_name, quantity, price_per_item, total
                    FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL
                    ORDER BY date DESC, id DESC
                    LIMIT :lim
                """),
                {"cid": customer_id, "lim": entries_limit}
            ).fetchall()

            customer["entries"] = [
                {
                    "id": r[0],
                    "date": r[1],
                    "item_name": r[2],
                    "quantity": r[3],
                    "price_per_item": r[4],
                    "total": r[5],
                }
                for r in entries_rows
            ]

            # ---- Q4: Shop name ----
            shop_row = conn.execute(
                text("SELECT shop_name FROM shops WHERE shop_id = :sid"),
                {"sid": shop_id}
            ).fetchone()
            customer["shop_name"] = shop_row[0] if shop_row else "Dukaan"

            return customer
    except Exception as e:
        log.error(f"_fetch_tracking_data failed: {e}")
        return None


# =========================================================
# MESSAGE COMPOSER
# =========================================================

def _compose_summary(customer: dict, max_entries: int = 5) -> str:
    """Khaata summary message banao."""
    name = customer["name"].title()
    baaki = customer["baaki"]
    shop_name = customer["shop_name"]
    entries = customer["entries"]

    # ---- Case 1: No baaki (clean) ----
    if baaki == 0 and not entries:
        return TRACKING_NO_ENTRIES.format(name=name, shop_name=shop_name)

    if baaki == 0:
        return TRACKING_NO_BAAKI.format(name=name, shop_name=shop_name)

    # ---- Case 2: Credit (negative baaki) ----
    if baaki < 0:
        return TRACKING_CREDIT.format(
            name=name, amount=_fmt_rs(-baaki), shop_name=shop_name
        )

    # ---- Case 3: Normal — with entries ----
    parts = [TRACKING_HEADER.format(name=name, baaki=_fmt_rs(baaki))]

    if entries:
        parts.append(TRACKING_ENTRIES_HEADER.format(count=len(entries)))
        for e in entries[:max_entries]:
            item_display = _title_item(e["item_name"])
            is_payment = e["item_name"].upper() == "PAYMENT"
            qty = int(e["quantity"]) if e["quantity"] == int(e["quantity"]) else e["quantity"]
            amount = _fmt_rs(abs(e["total"]))
            paid_marker = " ✓" if is_payment else ""

            parts.append(TRACKING_ENTRY_LINE.format(
                date=_fmt_date(e["date"]),
                item=item_display,
                qty=qty,
                amount=amount,
                paid_marker=paid_marker,
            ))

    parts.append(TRACKING_FOOTER.format(shop_name=shop_name))
    return "\n".join(parts)


# =========================================================
# BACKGROUND AUDIT
# =========================================================

def _write_audit_async(shop_id, customer_id, query, response, latency_ms):
    try:
        write_audit(
            shop_id=shop_id,
            agent_name="tracking",
            action="tracking_query",
            customer_id=customer_id,
            input_data={"query": query[:200]},
            output_data={"response": response[:500]},
            latency_ms=latency_ms,
            success=True,
        )
    except Exception as e:
        log.error(f"Background audit failed: {e}")


# =========================================================
# MAIN HANDLER
# =========================================================

def handle_tracking_query(shop_id: int, customer_id: int,
                          query: str = "") -> dict:
    """
    Customer ka khaata summary return karo.

    Args:
        shop_id: shop
        customer_id: customer
        query: customer ka original message (audit ke liye)

    Returns:
        {
            "status": "sent" | "error" | "no_customer",
            "message": "...",
            "baaki": float,
            "latency_ms": int,
        }
    """
    import threading
    started = time.time()

    log.info(f"Tracking query: shop={shop_id} customer={customer_id}")

    # ---- Fetch all data (single connection) ----
    t = time.time()
    customer = _fetch_tracking_data(shop_id, customer_id)
    log.info(f"[TIMING] fetch: {int((time.time()-t)*1000)}ms")

    if not customer:
        return {"status": "error", "reason": "customer_not_found"}

    # ---- Compose message ----
    message = _compose_summary(customer)

    # ---- WhatsApp payload ----
    from whatsapp import build_whatsapp_payload
    wa_result = build_whatsapp_payload(customer["phone"], message, auto_send=True)

    # ---- Audit (background) ----
    latency_ms = int((time.time() - started) * 1000)
    threading.Thread(
        target=_write_audit_async,
        args=(shop_id, customer_id, query, message, latency_ms),
        daemon=True,
    ).start()

    log.info(f"Tracking reply: {customer['name']} baaki=Rs.{customer['baaki']} in {latency_ms}ms")

    return {
        "status": "sent",
        "customer": customer["name"],
        "baaki": customer["baaki"],
        "message": message,
        "auto_sent": wa_result.get("auto_sent", False),
        "link": wa_result.get("link"),
        "latency_ms": latency_ms,
    }