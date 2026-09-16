# customers.py — PostgreSQL version
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.customers")


def get_customer(name: str, shop_id: int):
    with get_connection() as conn:
        result = conn.execute(
            text("SELECT * FROM customers WHERE LOWER(name) = :name AND shop_id = :shop_id"),
            {"name": name.lower().strip(), "shop_id": shop_id}
        ).fetchone()
    return result


def add_customer(name: str, phone: str, shop_id: int) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                text("INSERT INTO customers (name, phone, shop_id) VALUES (:name, :phone, :shop_id)"),
                {"name": name.lower().strip(), "phone": phone.strip(), "shop_id": shop_id}
            )
            conn.commit()
        log.info(f"Customer added: {name.title()}")
        return True
    except Exception as e:
        log.warning(f"Customer add failed for '{name}': {e}")
        return False


def add_khaata_entry(customer_name: str, item_name: str,
                     quantity: float, price: float, shop_id: int,
                     inventory_id: int = None) -> bool:
    customer = get_customer(customer_name, shop_id)
    if not customer:
        add_customer(customer_name, "unknown", shop_id)
        customer = get_customer(customer_name, shop_id)
    if not customer:
        return False

    total = round(quantity * price, 2)
    with get_connection() as conn:
        conn.execute(
            text("""
                INSERT INTO khaata (customer_id, item_name, quantity, price_per_item, total, inventory_id)
                VALUES (:customer_id, :item_name, :quantity, :price, :total, :inventory_id)
            """),
            {
                "customer_id": customer[0],
                "item_name": item_name.lower(),
                "quantity": quantity,
                "price": price,
                "total": total,
                "inventory_id": inventory_id,
            }
        )
        conn.commit()
    log.info(f"Khaata: {customer_name} {item_name} x{quantity} Rs.{total}")
    return True


def show_customer_khaata(name: str, shop_id: int):
    customer = get_customer(name, shop_id)
    if not customer:
        print(f"'{name}' nahi mila")
        return

    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT date, item_name, quantity, price_per_item, total
                FROM khaata WHERE customer_id = :cid AND deleted_at IS NULL
                ORDER BY date ASC
            """),
            {"cid": customer[0]}
        ).fetchall()
        total = conn.execute(
            text("SELECT SUM(total) FROM khaata WHERE customer_id = :cid AND deleted_at IS NULL"),
            {"cid": customer[0]}
        ).fetchone()[0] or 0

    print(f"\n{name.title()} ka Khaata:")
    print("-" * 55)
    for row in rows:
        print(f"  {row[0]} | {row[1].title()} | x{row[2]} | Rs.{row[3]}/item | Total: Rs.{row[4]}")
    print("-" * 55)
    print(f"  Total Baaki: Rs. {total}")