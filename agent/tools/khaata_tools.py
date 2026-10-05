# agent/tools/khaata_tools.py
"""
Khaata tools — agents ke liye. Existing customers.py / server.py functions
ko wrap karte hain taake LLM inhe structured tool calls se bula sake.
"""
import json
from typing import Optional
from sqlalchemy import text
from database import get_connection
from customers import add_khaata_entry, add_customer, get_customer
from logger import get_logger

log = get_logger("dukaan.tools.khaata")


def tool_get_customer_baaki(shop_id: int, customer_name: str) -> dict:
    """Customer ka total baaki."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""
                    SELECT c.customer_id, c.name, c.phone,
                           COALESCE(SUM(k.total), 0) as baaki
                    FROM customers c
                    LEFT JOIN khaata k
                        ON k.customer_id = c.customer_id
                        AND k.deleted_at IS NULL
                    WHERE c.shop_id = :shop_id
                      AND LOWER(c.name) = :name
                    GROUP BY c.customer_id, c.name, c.phone
                """),
                {"shop_id": shop_id, "name": customer_name.lower().strip()}
            ).fetchone()
        if not row:
            return {"error": f"'{customer_name}' nahi mila"}
        return {
            "customer_id": row[0], "name": row[1],
            "phone": row[2], "baaki": float(row[3]),
        }
    except Exception as e:
        log.error(f"tool_get_customer_baaki failed: {e}")
        return {"error": str(e)}


def tool_get_khaata_summary(shop_id: int, customer_name: str,
                            limit: int = 5) -> dict:
    """Customer ki last N entries + total baaki."""
    try:
        cust = tool_get_customer_baaki(shop_id, customer_name)
        if "error" in cust:
            return cust
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, date, item_name, quantity, price_per_item, total
                    FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL
                    ORDER BY date DESC, id DESC
                    LIMIT :limit
                """),
                {"cid": cust["customer_id"], "limit": limit}
            ).fetchall()
        return {
            "customer": cust["name"],
            "baaki": cust["baaki"],
            "phone": cust["phone"],
            "entries": [
                {"id": r[0], "date": str(r[1]), "item": r[2],
                 "qty": r[3], "price": r[4], "total": r[5]}
                for r in rows
            ],
        }
    except Exception as e:
        log.error(f"tool_get_khaata_summary failed: {e}")
        return {"error": str(e)}


def tool_add_khaata_entry(shop_id: int, customer: str,
                          item: str, quantity: float, price: float) -> dict:
    """Naya khaata entry. Wrap karta hai customers.add_khaata_entry()."""
    try:
        ok = add_khaata_entry(customer, item, quantity, price, shop_id)
        if not ok:
            return {"error": "Entry save nahi hui"}
        # Updated baaki laao
        baaki = tool_get_customer_baaki(shop_id, customer)
        return {
            "success": True,
            "customer": customer, "item": item,
            "quantity": quantity, "price": price,
            "total": round(quantity * price, 2),
            "new_baaki": baaki.get("baaki", 0),
        }
    except Exception as e:
        log.error(f"tool_add_khaata_entry failed: {e}")
        return {"error": str(e)}


def tool_add_payment(shop_id: int, customer: str, amount: float) -> dict:
    """Payment record karo — negative khaata entry."""
    try:
        customer_row = get_customer(customer, shop_id)
        if not customer_row:
            return {"error": f"'{customer}' nahi mila"}
        with get_connection() as conn:
            conn.execute(
                text("""
                    INSERT INTO khaata
                        (customer_id, item_name, quantity, price_per_item, total)
                    VALUES
                        (:cid, 'PAYMENT', 1, :amt, :neg_amt)
                """),
                {"cid": customer_row[0], "amt": amount, "neg_amt": -amount}
            )
            conn.commit()
        baaki = tool_get_customer_baaki(shop_id, customer)
        return {
            "success": True,
            "customer": customer,
            "amount": amount,
            "new_baaki": baaki.get("baaki", 0),
        }
    except Exception as e:
        log.error(f"tool_add_payment failed: {e}")
        return {"error": str(e)}


def tool_list_defaulters(shop_id: int, min_baaki: float = 100,
                         limit: int = 20) -> dict:
    """Jin customers ka baaki > min_baaki hai."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                text("""
                    SELECT c.customer_id, c.name, c.phone,
                           COALESCE(SUM(k.total), 0) as baaki
                    FROM customers c
                    LEFT JOIN khaata k
                        ON k.customer_id = c.customer_id
                        AND k.deleted_at IS NULL
                    WHERE c.shop_id = :shop_id
                    GROUP BY c.customer_id, c.name, c.phone
                    HAVING COALESCE(SUM(k.total), 0) >= :min_baaki
                    ORDER BY baaki DESC
                    LIMIT :limit
                """),
                {"shop_id": shop_id, "min_baaki": min_baaki, "limit": limit}
            ).fetchall()
        return {
            "count": len(rows),
            "defaulters": [
                {"customer_id": r[0], "name": r[1],
                 "phone": r[2], "baaki": float(r[3])}
                for r in rows
            ],
        }
    except Exception as e:
        log.error(f"tool_list_defaulters failed: {e}")
        return {"error": str(e)}