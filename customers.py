# customers.py — PostgreSQL version
from typing import Optional
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.customers")


def get_customer(name: str, shop_id: int):
    """Returns row: (customer_id, name, phone, opening_balance) or None."""
    with get_connection() as conn:
        return conn.execute(
            text("""SELECT customer_id, name, phone, COALESCE(opening_balance, 0)
                    FROM customers
                    WHERE LOWER(name) = :name AND shop_id = :shop_id"""),
            {"name": (name or "").lower().strip(), "shop_id": shop_id}
        ).fetchone()


def add_customer(name: str, phone: str, shop_id: int) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("INSERT INTO customers (name, phone, shop_id) VALUES (:name, :phone, :shop_id)"),
                {"name": (name or "").lower().strip(),
                 "phone": (phone or "unknown").strip(),
                 "shop_id": shop_id}
            )
            conn.commit()
        log.info(f"Customer added: {(name or '').title()} shop={shop_id}")
        return True
    except Exception as e:
        log.warning(f"Customer add failed for '{name}': {e}")
        return False


def add_khaata_entry(customer_name: str, item_name: str,
                     quantity: float, price: float, shop_id: int,
                     inventory_id: Optional[int] = None,
                     cost_price: float = 0,
                     conn=None) -> bool:
    """
    Insert a khaata entry + auto-create customer if missing.

    conn=None   → opens own connection, commits, closes (backward compat)
    conn=given  → uses caller's transaction; caller must commit/rollback

    inventory_id : historical reference to inventory row (for profit reports)
    cost_price   : purchase_rate snapshot at time of sale (frozen for stable profit)
    """
    _owns = conn is None
    if _owns:
        conn = get_connection()

    try:
        customer_name_clean = (customer_name or "").lower().strip()
        if not customer_name_clean:
            if _owns:
                conn.rollback()
            return False

        # ---- Find customer within the SAME transaction ----
        row = conn.execute(
            text("""SELECT customer_id FROM customers
                    WHERE LOWER(name) = :name AND shop_id = :sid"""),
            {"name": customer_name_clean, "sid": shop_id}
        ).fetchone()

        if not row:
            conn.execute(
                text("INSERT INTO customers (name, phone, shop_id) VALUES (:n, 'unknown', :sid)"),
                {"n": customer_name_clean, "sid": shop_id}
            )
            row = conn.execute(
                text("""SELECT customer_id FROM customers
                        WHERE LOWER(name) = :name AND shop_id = :sid"""),
                {"name": customer_name_clean, "sid": shop_id}
            ).fetchone()

        if not row:
            if _owns:
                conn.rollback()
            log.error(f"add_khaata_entry: customer create failed for {customer_name}")
            return False

        customer_id = row[0]
        total = round(quantity * price, 2)

        conn.execute(
            text("""
                INSERT INTO khaata
                    (customer_id, item_name, quantity, price_per_item, total,
                     inventory_id, cost_price)
                VALUES (:customer_id, :item_name, :quantity, :price, :total,
                        :inventory_id, :cost_price)
            """),
            {
                "customer_id": customer_id,
                "item_name": (item_name or "").lower(),
                "quantity": quantity,
                "price": price,
                "total": total,
                "inventory_id": inventory_id,
                "cost_price": cost_price or 0,
            }
        )

        if _owns:
            conn.commit()
        log.info(f"Khaata: {customer_name} {item_name} x{quantity} Rs.{total} shop={shop_id}")
        return True

    except Exception as e:
        if _owns:
            conn.rollback()
        log.error(f"add_khaata_entry failed: {e}")
        return False

    finally:
        if _owns:
            conn.close()


def show_customer_khaata(name: str, shop_id: int):
    """CLI helper — prints khaata to stdout. Not used by server."""
    customer = get_customer(name, shop_id)
    if not customer:
        print(f"'{name}' nahi mila")
        return

    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT date, item_name, quantity, price_per_item, total
                FROM khaata
                WHERE customer_id = :cid AND deleted_at IS NULL
                ORDER BY date ASC
            """),
            {"cid": customer[0]}
        ).fetchall()
        total = conn.execute(
            text("""SELECT COALESCE(SUM(total), 0) FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL"""),
            {"cid": customer[0]}
        ).fetchone()[0] or 0

    opening = float(customer[3] or 0)
    print(f"\n{name.title()} ka Khaata:")
    print("-" * 55)
    for row in rows:
        print(f"  {row[0]} | {row[1].title()} | x{row[2]} | Rs.{row[3]}/item | Total: Rs.{row[4]}")
    print("-" * 55)
    if opening:
        print(f"  Purana hisaab: Rs. {opening}")
    print(f"  Total Baaki: Rs. {round(float(total) + opening, 2)}")