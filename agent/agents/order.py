# agent/agents/order.py
"""
Order Agent — multi-item, multi-turn, keyword-based.

State machine:
    (none)                →  pending_variant     (agar multiple variants)
                          →  pending_confirmation (agar single match)
                          →  (none)              (agar parse fail)
    pending_variant       →  pending_variant     (agar aur variants queue mein)
                          →  pending_confirmation (agar sab variants resolved)
    pending_confirmation  →  pending_payment     (agar "haan")
                          →  (none)              (agar "nahi")
                          →  pending_confirmation (agar modification)
    pending_payment       →  completed           (agar "udhaar"/"cash")
                          →  pending_payment     (agar invalid)

State store: conversations.state->'order_agent'  (JSONB)
Timeout: 10 minutes
"""
import json
import time
import threading
import re
from datetime import datetime, timedelta
from typing import Optional
from groq import Groq
from sqlalchemy import text
from database import get_connection
from config import GROQ_API_KEY, FAST_MODEL
from logger import get_logger
from ai_parser import _extract_json, MULTI_ITEM_PROMPT
from inventory import get_variants, get_item_price
from whatsapp import build_whatsapp_payload
from worker.db_worker import write_audit
from agent.messages import (
    ORDER_YES_KEYWORDS, ORDER_NO_KEYWORDS,
    ORDER_UDHAAR_KEYWORDS, ORDER_CASH_KEYWORDS, ORDER_DELETE_KEYWORDS,
    ORDER_PARSE_FAILED, ORDER_CUSTOMER_UNKNOWN, ORDER_NO_ITEMS_FOUND,
    ORDER_CONFIRMATION_HEADER, ORDER_ITEM_LINE, ORDER_MANUAL_PRICE_WARNING,
    ORDER_ASK_PAYMENT, ORDER_PAYMENT_INVALID,
    ORDER_UDHAAR_SAVED, ORDER_CASH_SAVED, ORDER_CANCELLED,
    ORDER_MODIFIED, ORDER_TIMEOUT, ORDER_DELETE_DONE, ORDER_DELETE_NOTHING,
    ORDER_VARIANT_ASK, ORDER_VARIANT_LINE,
    ORDER_VARIANT_INVALID, ORDER_VARIANT_SELECTED,
)

log = get_logger("dukaan.agent.order")

ORDER_STATE_TTL_MIN = 10
_parse_client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# HELPERS
# =========================================================

def _fmt_rs(amount) -> str:
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", (text or "").lower().strip())


def _match_short(text: str, keywords: list, max_words: int = 4) -> bool:
    """Short message (≤ max_words) jo keywords se match kare."""
    norm = _normalize(text)
    if not norm:
        return False
    words = norm.split()
    if len(words) > max_words:
        return False
    for kw in keywords:
        if norm == kw or norm in (kw, kw + " hai", kw + " karo"):
            return True
    return False


def is_yes(text): return _match_short(text, ORDER_YES_KEYWORDS)
def is_no(text): return _match_short(text, ORDER_NO_KEYWORDS)
def is_udhaar(text): return _match_short(text, ORDER_UDHAAR_KEYWORDS, max_words=5)
def is_cash(text): return _match_short(text, ORDER_CASH_KEYWORDS, max_words=5)
def is_delete(text): return _match_short(text, ORDER_DELETE_KEYWORDS, max_words=5)


# =========================================================
# FAST PARSER
# =========================================================

def _parse_items_fast(message: str) -> Optional[dict]:
    """Fast LLM parsing — chhota model, low reasoning, JSON output."""
    t = time.time()
    try:
        response = _parse_client.chat.completions.create(
            model=FAST_MODEL,
            max_tokens=400,
            temperature=0,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": MULTI_ITEM_PROMPT},
                {"role": "user", "content": message}
            ]
        )
        log.info(f"[PARSE TIMING] LLM call: {int((time.time()-t)*1000)}ms")
        return _extract_json(response.choices[0].message.content)
    except Exception as e:
        log.error(f"_parse_items_fast failed: {e}")
        return None


# =========================================================
# STATE MANAGEMENT
# =========================================================

def _get_order_state(conversation_id: int) -> Optional[dict]:
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("SELECT state FROM conversations WHERE id = :cid"),
                {"cid": conversation_id}
            ).fetchone()
        if not row or not row[0]:
            return None
        state = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return state.get("order_agent")
    except Exception as e:
        log.error(f"_get_order_state failed: {e}")
        return None


def _set_order_state(conversation_id: int, order_state: Optional[dict]):
    try:
        with get_connection() as conn:
            if order_state is None:
                conn.execute(
                    text("""
                        UPDATE conversations
                        SET state = state - 'order_agent',
                            last_message_at = NOW()
                        WHERE id = :cid
                    """),
                    {"cid": conversation_id}
                )
            else:
                order_state["last_updated"] = datetime.now().isoformat()
                conn.execute(
                    text("""
                        UPDATE conversations
                        SET state = jsonb_set(
                                COALESCE(state, '{}'::jsonb),
                                '{order_agent}',
                                CAST(:state AS jsonb)
                            ),
                            last_message_at = NOW()
                        WHERE id = :cid
                    """),
                    {"cid": conversation_id, "state": json.dumps(order_state)}
                )
            conn.commit()
    except Exception as e:
        log.error(f"_set_order_state failed: {e}")


def _is_expired(state: dict) -> bool:
    try:
        last = state.get("last_updated")
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt) > timedelta(minutes=ORDER_STATE_TTL_MIN)
    except Exception:
        return True


# =========================================================
# INVENTORY MATCHING — single item
# =========================================================

def _match_one_item(shop_id: int, item_name: str, qty: float) -> dict:
    """
    Ek item ko inventory se match karo.
    Returns: {type: "matched"|"manual"|"ambiguous", ...}
    """
    variants = get_variants(item_name, shop_id)

    # 1. No variants → manual entry
    if not variants:
        return {
            "type": "manual",
            "item": {
                "item": item_name, "item_id": None,
                "quantity": qty, "price": 0,
                "total": 0, "found": False, "stock": None,
            }
        }

    # 2. Exact match
    item_lower = item_name.lower().strip()
    for v in variants:
        if v["name"].lower() == item_lower:
            return {
                "type": "matched",
                "item": {
                    "item": v["name"], "item_id": v["id"],
                    "quantity": qty, "price": v["price"],
                    "total": round(qty * v["price"], 2),
                    "found": True, "stock": v["stock"],
                }
            }

    # 3. Single variant → use it
    if len(variants) == 1:
        v = variants[0]
        return {
            "type": "matched",
            "item": {
                "item": v["name"], "item_id": v["id"],
                "quantity": qty, "price": v["price"],
                "total": round(qty * v["price"], 2),
                "found": True, "stock": v["stock"],
            }
        }

    # 4. Multiple variants → ask customer
    return {
        "type": "ambiguous",
        "original_name": item_name,
        "quantity": qty,
        "variants": variants,
    }


def _match_all_items(shop_id: int, raw_items: list) -> tuple:
    """
    Saare items match karo. Returns (enriched_list, variant_queue).
    Saare ambiguous items ko queue mein daal deta hai — loop break nahi karta.
    """
    enriched = []
    variant_queue = []

    for item in raw_items:
        item_name = (item.get("item") or "").strip()
        if not item_name:
            continue
        try:
            qty = float(item.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            qty = 1.0

        result = _match_one_item(shop_id, item_name, qty)
        if result["type"] == "ambiguous":
            variant_queue.append({
                "original_name": result["original_name"],
                "quantity": result["quantity"],
                "variants": result["variants"],
            })
        else:
            enriched.append(result["item"])

    return enriched, variant_queue


# =========================================================
# MESSAGE COMPOSERS
# =========================================================

def _items_block(items: list) -> str:
    lines = []
    for i in items:
        qty = int(i["quantity"]) if i["quantity"] == int(i["quantity"]) else i["quantity"]
        lines.append(ORDER_ITEM_LINE.format(
            item=i["item"].title(),
            qty=qty,
            total=_fmt_rs(i["total"]),
        ))
    return "\n".join(lines)


def _confirmation_message(name, items, grand_total, shop_name):
    manual = any(not i["found"] for i in items)
    msg = ORDER_CONFIRMATION_HEADER.format(
        name=name.title(),
        items_block=_items_block(items),
        grand_total=_fmt_rs(grand_total),
    )
    if manual:
        msg += ORDER_MANUAL_PRICE_WARNING
    return msg


def _variant_ask_message(customer_name, variant_query) -> str:
    """Variant ask message banao — dynamic options hint."""
    variants = variant_query["variants"]
    variants_block = "\n".join([
        ORDER_VARIANT_LINE.format(
            num=i + 1, name=v["name"], price=_fmt_rs(v["price"])
        )
        for i, v in enumerate(variants)
    ])
    # Dynamic hint: "1, 2" ya "1, 2, 3"
    nums = [str(i + 1) for i in range(len(variants))]
    if len(nums) == 1:
        options_hint = nums[0]
    else:
        options_hint = ", ".join(nums[:-1]) + ", ya " + nums[-1]

    return ORDER_VARIANT_ASK.format(
        name=customer_name.title(),
        item=variant_query["original_name"].title(),
        variants_block=variants_block,
        options_hint=options_hint,
    )


def _get_shop_name(shop_id: int) -> str:
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("SELECT shop_name FROM shops WHERE shop_id = :sid"),
                {"sid": shop_id}
            ).fetchone()
        return row[0] if row else "Dukaan"
    except Exception:
        return "Dukaan"


# =========================================================
# BACKGROUND AUDIT
# =========================================================

def _audit_async(shop_id, customer_id, action, input_data, output_data, latency_ms, success=True, error=None):
    try:
        write_audit(
            shop_id=shop_id, agent_name="order", action=action,
            customer_id=customer_id,
            input_data=input_data, output_data=output_data,
            latency_ms=latency_ms, success=success, error=error,
        )
    except Exception as e:
        log.error(f"Background audit failed: {e}")


# =========================================================
# FLOW: START NEW ORDER
# =========================================================

def _start_new_order(shop_id, customer_id, conv_id, message, customer_name, phone, shop_name):
    """Naya order — parse, match items, ya variant poochho (saare)."""
    parsed = _parse_items_fast(message)
    if not parsed:
        return {"reply": ORDER_PARSE_FAILED.format(shop_name=shop_name), "state": None}

    raw_items = parsed.get("items", [])
    if not raw_items:
        return {
            "reply": ORDER_NO_ITEMS_FOUND.format(name=customer_name.title(), shop_name=shop_name),
            "state": None
        }

    # ---- Match all items (variant_queue collect karta hai) ----
    enriched, variant_queue = _match_all_items(shop_id, raw_items)

    # ---- Agar variants poochne hain ----
    if variant_queue:
        current = variant_queue[0]
        remaining = variant_queue[1:]

        state = {
            "status": "pending_variant",
            "customer_id": customer_id,
            "customer_name": customer_name,
            "phone": phone,
            "shop_id": shop_id,
            "shop_name": shop_name,
            "items": enriched,
            "variant_queue": remaining,
            "variant_query": current,
            "started_at": datetime.now().isoformat(),
        }
        _set_order_state(conv_id, state)

        reply = _variant_ask_message(customer_name, current)
        return {"reply": reply, "state": state}

    # ---- Sab matched — normal confirm ----
    if not enriched:
        return {
            "reply": ORDER_NO_ITEMS_FOUND.format(name=customer_name.title(), shop_name=shop_name),
            "state": None
        }

    grand_total = round(sum(i["total"] for i in enriched), 2)

    state = {
        "status": "pending_confirmation",
        "customer_id": customer_id,
        "customer_name": customer_name,
        "phone": phone,
        "shop_id": shop_id,
        "shop_name": shop_name,
        "items": enriched,
        "grand_total": grand_total,
        "started_at": datetime.now().isoformat(),
    }
    _set_order_state(conv_id, state)

    reply = _confirmation_message(customer_name, enriched, grand_total, shop_name)
    return {"reply": reply, "state": state}


# =========================================================
# FLOW: VARIANT SELECTION
# =========================================================

def _handle_variant_selection(shop_id, customer_id, conv_id, message, state, shop_name):
    """Customer ne variant number reply kiya. Queue process karo."""
    nums = re.findall(r'\d+', message)
    variants = state["variant_query"]["variants"]

    if not nums:
        nums_str = ", ".join(str(i + 1) for i in range(len(variants)))
        return {"reply": ORDER_VARIANT_INVALID.format(nums=nums_str), "state": state}

    choice = int(nums[0])
    if choice < 1 or choice > len(variants):
        nums_str = ", ".join(str(i + 1) for i in range(len(variants)))
        return {"reply": ORDER_VARIANT_INVALID.format(nums=nums_str), "state": state}

    v = variants[choice - 1]
    qty = state["variant_query"]["quantity"]

    # Selected item add karo
    state["items"].append({
        "item": v["name"], "item_id": v["id"],
        "quantity": qty, "price": v["price"],
        "total": round(qty * v["price"], 2),
        "found": True, "stock": v["stock"],
    })
    state.pop("variant_query", None)

    # ---- Aur variants baaki hain? ----
    variant_queue = state.get("variant_queue", [])
    if variant_queue:
        next_v = variant_queue[0]
        state["variant_queue"] = variant_queue[1:]
        state["variant_query"] = next_v
        _set_order_state(conv_id, state)

        reply = _variant_ask_message(state["customer_name"], next_v)
        return {"reply": reply, "state": state}

    # ---- Sab variants resolved — full confirmation ----
    grand_total = round(sum(i["total"] for i in state["items"]), 2)
    state["grand_total"] = grand_total
    state["status"] = "pending_confirmation"
    _set_order_state(conv_id, state)

    reply = _confirmation_message(
        state["customer_name"], state["items"], grand_total, shop_name
    )
    return {"reply": reply, "state": state}


# =========================================================
# FLOW: CONFIRMATION
# =========================================================

def _handle_confirmation(shop_id, customer_id, conv_id, message, state, shop_name):
    """Customer ne confirm/cancel/modify bola."""
    if is_yes(message):
        state["status"] = "pending_payment"
        _set_order_state(conv_id, state)
        return {"reply": ORDER_ASK_PAYMENT, "state": state}

    if is_no(message):
        _set_order_state(conv_id, None)
        return {"reply": ORDER_CANCELLED.format(shop_name=shop_name), "state": None}

    # Modification — re-parse
    parsed = _parse_items_fast(message)
    if not parsed or not parsed.get("items"):
        reply = _confirmation_message(
            state["customer_name"], state["items"], state["grand_total"], shop_name
        )
        return {"reply": reply, "state": state}

    # ---- Match all items ----
    enriched, variant_queue = _match_all_items(shop_id, parsed["items"])

    if variant_queue:
        current = variant_queue[0]
        remaining = variant_queue[1:]
        state["items"] = enriched
        state["variant_queue"] = remaining
        state["variant_query"] = current
        state["status"] = "pending_variant"
        _set_order_state(conv_id, state)

        reply = _variant_ask_message(state["customer_name"], current)
        return {"reply": reply, "state": state}

    if not enriched:
        reply = _confirmation_message(
            state["customer_name"], state["items"], state["grand_total"], shop_name
        )
        return {"reply": reply, "state": state}

    grand_total = round(sum(i["total"] for i in enriched), 2)
    state["items"] = enriched
    state["grand_total"] = grand_total
    state["status"] = "pending_confirmation"
    _set_order_state(conv_id, state)

    reply = ORDER_MODIFIED.format(
        items_block=_items_block(enriched),
        grand_total=_fmt_rs(grand_total),
    )
    return {"reply": reply, "state": state}


# =========================================================
# FLOW: PAYMENT TYPE
# =========================================================

def _handle_payment_type(shop_id, customer_id, conv_id, message, state, shop_name):
    """Customer ne udhaar/cash bataya — save karo."""
    if is_udhaar(message):
        return _save_order(shop_id, customer_id, conv_id, state, shop_name, payment_type="udhaar")
    if is_cash(message):
        return _save_order(shop_id, customer_id, conv_id, state, shop_name, payment_type="cash")
    return {"reply": ORDER_PAYMENT_INVALID, "state": state}


# =========================================================
# FLOW: SAVE ORDER
# =========================================================

def _save_order(shop_id, customer_id, conv_id, state, shop_name, payment_type):
    """
    Order save karo — single connection mein sab kuch.
    Udhaar: sirf sale entries.
    Cash: sale + payment entry (khaata saaf).
    """
    items = state["items"]
    grand_total = state["grand_total"]
    customer_name = state["customer_name"]

    items_to_save = [i for i in items if i["found"]]
    if not items_to_save:
        _set_order_state(conv_id, None)
        return {
            "reply": ORDER_PARSE_FAILED.format(shop_name=shop_name),
            "state": None,
        }

    saved_items = []
    new_baaki = 0
    try:
        with get_connection() as conn:
            cust = conn.execute(
                text("""
                    SELECT customer_id FROM customers
                    WHERE shop_id = :sid AND LOWER(name) = :n
                """),
                {"sid": shop_id, "n": customer_name.lower().strip()}
            ).fetchone()
            if not cust:
                return {
                    "reply": ORDER_CUSTOMER_UNKNOWN.format(shop_name=shop_name),
                    "state": None,
                }
            cid = cust[0]

            for i in items_to_save:
                total = round(i["quantity"] * i["price"], 2)
                conn.execute(
                    text("""
                        INSERT INTO khaata
                            (customer_id, item_name, quantity, price_per_item, total)
                        VALUES
                            (:cid, :item, :qty, :price, :total)
                    """),
                    {
                        "cid": cid,
                        "item": i["item"].lower(),
                        "qty": i["quantity"],
                        "price": i["price"],
                        "total": total,
                    }
                )
                if i.get("item_id"):
                    conn.execute(
                        text("""
                            UPDATE inventory SET stock = stock - :qty
                            WHERE id = :iid AND shop_id = :sid
                        """),
                        {"qty": i["quantity"], "iid": i["item_id"], "sid": shop_id}
                    )
                saved_items.append(i)

            if payment_type == "cash":
                conn.execute(
                    text("""
                        INSERT INTO khaata
                            (customer_id, item_name, quantity, price_per_item, total)
                        VALUES
                            (:cid, 'PAYMENT', 1, :amt, :neg)
                    """),
                    {"cid": cid, "amt": grand_total, "neg": -grand_total}
                )

            baaki_row = conn.execute(
                text("""
                    SELECT COALESCE(SUM(total), 0) FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL
                """),
                {"cid": cid}
            ).fetchone()
            new_baaki = float(baaki_row[0]) if baaki_row else 0

            conn.commit()
    except Exception as e:
        log.error(f"_save_order failed: {e}")
        return {
            "reply": ORDER_PARSE_FAILED.format(shop_name=shop_name),
            "state": None,
        }

    _set_order_state(conv_id, None)

    if payment_type == "cash":
        reply = ORDER_CASH_SAVED.format(
            name=customer_name.title(),
            items_block=_items_block(saved_items),
            grand_total=_fmt_rs(grand_total),
            shop_name=shop_name,
        )
    else:
        reply = ORDER_UDHAAR_SAVED.format(
            name=customer_name.title(),
            items_block=_items_block(saved_items),
            grand_total=_fmt_rs(grand_total),
            new_baaki=_fmt_rs(new_baaki),
            shop_name=shop_name,
        )

    log.info(f"Order saved: {customer_name} {payment_type} Rs.{grand_total} (baaki={new_baaki})")
    return {
        "reply": reply,
        "state": None,
        "saved": True,
        "payment_type": payment_type,
    }


# =========================================================
# FLOW: DELETE LAST ORDER
# =========================================================

def _delete_last_order(shop_id, customer_id, conv_id, shop_name):
    """Pichle order ki entries soft-delete karo."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    UPDATE khaata SET deleted_at = NOW()
                    WHERE customer_id = :cid
                      AND deleted_at IS NULL
                      AND date = CURRENT_DATE
                      AND item_name != 'PAYMENT'
                      AND id IN (
                          SELECT id FROM khaata
                          WHERE customer_id = :cid
                            AND deleted_at IS NULL
                            AND item_name != 'PAYMENT'
                          ORDER BY id DESC
                          LIMIT 20
                      )
                    RETURNING id
                """),
                {"cid": customer_id}
            ).fetchall()
            conn.commit()
        count = len(rows)
        log.info(f"Deleted {count} recent entries for customer {customer_id}")
        return count
    except Exception as e:
        log.error(f"_delete_last_order failed: {e}")
        return 0


# =========================================================
# MAIN HANDLER
# =========================================================

def handle_order_message(shop_id: int, customer_id: Optional[int],
                         conversation_id: int, message: str,
                         customer_name: str = "", phone: str = "") -> dict:
    """
    Main entry — orchestrator isse call karega jab intent=order ho.
    """
    started = time.time()
    shop_name = _get_shop_name(shop_id)

    if not customer_name:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    text("SELECT name, phone FROM customers WHERE customer_id = :cid"),
                    {"cid": customer_id}
                ).fetchone()
            if row:
                customer_name, phone = row[0], row[1]
        except Exception:
            pass

    if not customer_name:
        return {
            "reply": ORDER_CUSTOMER_UNKNOWN.format(shop_name=shop_name),
            "state": None,
        }

    # ---- Get state ----
    state = _get_order_state(conversation_id)
    if state and _is_expired(state):
        log.info(f"Order state expired for conv {conversation_id}")
        _set_order_state(conversation_id, None)
        state = None

    # ---- Route by state ----
    if state is None:
        if is_delete(message) and customer_id:
            count = _delete_last_order(shop_id, customer_id, conversation_id, shop_name)
            if count > 0:
                return {"reply": ORDER_DELETE_DONE.format(shop_name=shop_name), "state": None}
            else:
                return {"reply": ORDER_DELETE_NOTHING.format(shop_name=shop_name), "state": None}

        result = _start_new_order(
            shop_id, customer_id, conversation_id,
            message, customer_name, phone, shop_name
        )
    elif state["status"] == "pending_variant":
        result = _handle_variant_selection(
            shop_id, customer_id, conversation_id, message, state, shop_name
        )
    elif state["status"] == "pending_confirmation":
        result = _handle_confirmation(
            shop_id, customer_id, conversation_id, message, state, shop_name
        )
    elif state["status"] == "pending_payment":
        result = _handle_payment_type(
            shop_id, customer_id, conversation_id, message, state, shop_name
        )
    else:
        result = _start_new_order(
            shop_id, customer_id, conversation_id,
            message, customer_name, phone, shop_name
        )

    # ---- Audit ----
    latency_ms = int((time.time() - started) * 1000)
    threading.Thread(
        target=_audit_async,
        args=(
            shop_id, customer_id, "order_message",
            {"message": message[:200], "state_status": (state or {}).get("status")},
            {"reply": result.get("reply", "")[:300], "saved": result.get("saved", False)},
            latency_ms,
        ),
        daemon=True,
    ).start()

    result["latency_ms"] = latency_ms
    log.info(f"Order: shop={shop_id} cust={customer_id} latency={latency_ms}ms")
    return result