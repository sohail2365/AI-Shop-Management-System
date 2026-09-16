# inventory.py — PostgreSQL version
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.inventory")


def get_item_price(item_name: str, shop_id: int):
    try:
        with get_connection() as conn:
            item_lower = item_name.lower().strip()

            queries = [
                ("LOWER(item_name) = :val", item_lower),
                ("LOWER(item_name) LIKE :val", f"{item_lower}%"),
                ("LOWER(item_name) LIKE :val", f"%{item_lower}%"),
            ]

            for where_clause, value in queries:
                row = conn.execute(
                    text(f"""
                        SELECT id, item_name, sale_price, stock FROM inventory
                        WHERE shop_id = :shop_id AND {where_clause}
                        ORDER BY LENGTH(item_name) ASC
                        LIMIT 1
                    """),
                    {"shop_id": shop_id, "val": value}
                ).fetchone()
                if row:
                    return {"id": row[0], "name": row[1], "price": row[2], "stock": int(row[3])}

            words = [w for w in item_lower.split() if len(w) > 3]
            for word in words:
                row = conn.execute(
                    text("""
                        SELECT id, item_name, sale_price, stock FROM inventory
                        WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val
                        ORDER BY LENGTH(item_name) ASC
                        LIMIT 1
                    """),
                    {"shop_id": shop_id, "val": f"%{word}%"}
                ).fetchone()
                if row:
                    return {"id": row[0], "name": row[1], "price": row[2], "stock": int(row[3])}

        return None
    except Exception as e:
        log.error(f"Inventory error: {e}")
        return None


def get_variants(keyword: str, shop_id: int) -> list:
    try:
        with get_connection() as conn:
            keyword_lower = keyword.lower().strip()
            words = [w for w in keyword_lower.split() if len(w) > 2]
            if not words:
                words = [keyword_lower]

            placeholders = " OR ".join([f"LOWER(item_name) LIKE :w{i}" for i in range(len(words))])
            params = {"shop_id": shop_id}
            for i, w in enumerate(words):
                params[f"w{i}"] = f"%{w}%"

            rows = conn.execute(
                text(f"""
                    SELECT id, item_name, sale_price, stock FROM inventory
                    WHERE shop_id = :shop_id AND ({placeholders})
                    ORDER BY item_name
                """),
                params
            ).fetchall()

        return [{"id": r[0], "name": r[1], "price": r[2], "stock": int(r[3])} for r in rows]
    except Exception as e:
        log.error(f"Variants error: {e}")
        return []


def get_item_by_id(item_id: int, shop_id: int):
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("SELECT id, item_name, sale_price, stock FROM inventory WHERE id = :id AND shop_id = :shop_id"),
                {"id": item_id, "shop_id": shop_id}
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "price": row[2], "stock": int(row[3])}
    except Exception as e:
        log.error(f"Inventory by id error: {e}")
        return None


def update_stock_by_id(item_id: int, quantity_sold: float, shop_id: int) -> bool:
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("SELECT stock FROM inventory WHERE id = :id AND shop_id = :shop_id"),
                {"id": item_id, "shop_id": shop_id}
            ).fetchone()
            if not row:
                log.warning(f"Item id nahi mila: {item_id}")
                return False
            new_stock = row[0] - quantity_sold
            conn.execute(
                text("UPDATE inventory SET stock = :stock WHERE id = :id AND shop_id = :shop_id"),
                {"stock": new_stock, "id": item_id, "shop_id": shop_id}
            )
            conn.commit()
        return True
    except Exception as e:
        log.error(f"Stock by id update error: {e}")
        return False


def update_stock(item_name: str, quantity_sold: float, shop_id: int) -> bool:
    try:
        with get_connection() as conn:
            item_lower = item_name.lower().strip()

            row = conn.execute(
                text("SELECT id, stock FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val LIMIT 1"),
                {"shop_id": shop_id, "val": f"%{item_lower}%"}
            ).fetchone()

            if not row:
                for word in item_lower.split():
                    if len(word) <= 3:
                        continue
                    row = conn.execute(
                        text("SELECT id, stock FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val LIMIT 1"),
                        {"shop_id": shop_id, "val": f"%{word}%"}
                    ).fetchone()
                    if row:
                        break

            if not row:
                return False

            new_stock = row[1] - quantity_sold
            conn.execute(
                text("UPDATE inventory SET stock = :stock WHERE id = :id"),
                {"stock": new_stock, "id": row[0]}
            )
            conn.commit()
            return True

    except Exception as e:
        log.error(f"Stock update error: {e}")
        return False


def update_stock_purchase(item_name: str, quantity_bought: float, shop_id: int) -> bool:
    try:
        with get_connection() as conn:
            item_lower = item_name.lower().strip()

            row = conn.execute(
                text("SELECT id, stock FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val LIMIT 1"),
                {"shop_id": shop_id, "val": f"%{item_lower}%"}
            ).fetchone()

            if not row:
                for word in item_lower.split():
                    if len(word) <= 3:
                        continue
                    row = conn.execute(
                        text("SELECT id, stock FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val LIMIT 1"),
                        {"shop_id": shop_id, "val": f"%{word}%"}
                    ).fetchone()
                    if row:
                        break

            if not row:
                return False

            new_stock = row[1] + quantity_bought
            conn.execute(
                text("UPDATE inventory SET stock = :stock WHERE id = :id"),
                {"stock": new_stock, "id": row[0]}
            )
            conn.commit()
            return True

    except Exception as e:
        log.error(f"Purchase stock error: {e}")
        return False


def adjust_stock_in_conn(conn, shop_id: int, delta: float,
                         inventory_id: int = None, item_name: str = None) -> bool:
    """
    Stock ko `delta` se badhao (positive) ya ghatao (negative) — EK EXISTING
    TRANSACTION KE ANDAR. inventory_id preferred hai; na ho to item_name se match.

    Yeh delete/restore/edit flows mein use hota hai jahan poora operation
    ek atomic transaction hona chahiye.
    """
    if inventory_id:
        result = conn.execute(
            text("UPDATE inventory SET stock = stock + :delta WHERE id = :id AND shop_id = :sid RETURNING id"),
            {"delta": delta, "id": inventory_id, "sid": shop_id}
        ).fetchone()
        if result:
            return True

    if item_name:
        item_lower = item_name.lower().strip()
        result = conn.execute(
            text("""UPDATE inventory SET stock = stock + :delta
                    WHERE id = (SELECT id FROM inventory
                                WHERE shop_id = :sid AND LOWER(item_name) LIKE :val
                                ORDER BY LENGTH(item_name) ASC LIMIT 1)
                    RETURNING id"""),
            {"delta": delta, "sid": shop_id, "val": f"%{item_lower}%"}
        ).fetchone()
        return result is not None

    return False


# ---- Inventory CRUD ----
def add_item(shop_id: int, item_name: str, sale_price: float,
             purchase_rate: float = 0, stock: float = 0,
             category: str = '', reorder_level: int = 5) -> dict:
    try:
        with get_connection() as conn:
            existing = conn.execute(
                text("SELECT id FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) = :name"),
                {"shop_id": shop_id, "name": item_name.lower().strip()}
            ).fetchone()
            if existing:
                return {"error": f"'{item_name}' pehle se exist karta hai"}

            conn.execute(
                text("""
                    INSERT INTO inventory
                    (shop_id, item_name, category, sale_price, purchase_rate, stock, reorder_level)
                    VALUES (:shop_id, :item_name, :category, :sale_price, :purchase_rate, :stock, :reorder_level)
                """),
                {
                    "shop_id": shop_id, "item_name": item_name.strip(), "category": category,
                    "sale_price": sale_price, "purchase_rate": purchase_rate,
                    "stock": stock, "reorder_level": reorder_level
                }
            )
            conn.commit()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


def get_all_items(shop_id: int) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT id, item_name, category, sale_price, purchase_rate, stock, reorder_level
                FROM inventory WHERE shop_id = :shop_id ORDER BY item_name
            """),
            {"shop_id": shop_id}
        ).fetchall()
    return [
        {
            "id": r[0], "name": r[1], "category": r[2],
            "price": r[3], "purchase_rate": r[4],
            "stock": r[5], "reorder_level": r[6],
            "low_stock": r[5] <= r[6]
        }
        for r in rows
    ]


def edit_item(item_id: int, shop_id: int, item_name: str, sale_price: float,
              purchase_rate: float, stock: float, category: str, reorder_level: int) -> dict:
    try:
        with get_connection() as conn:
            conn.execute(
                text("""
                    UPDATE inventory SET item_name=:item_name, category=:category, sale_price=:sale_price,
                    purchase_rate=:purchase_rate, stock=:stock, reorder_level=:reorder_level
                    WHERE id=:id AND shop_id=:shop_id
                """),
                {
                    "item_name": item_name.strip(), "category": category, "sale_price": sale_price,
                    "purchase_rate": purchase_rate, "stock": stock, "reorder_level": reorder_level,
                    "id": item_id, "shop_id": shop_id
                }
            )
            conn.commit()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


def delete_item(item_id: int, shop_id: int) -> dict:
    with get_connection() as conn:
        conn.execute(
            text("DELETE FROM inventory WHERE id=:id AND shop_id=:shop_id"),
            {"id": item_id, "shop_id": shop_id}
        )
        conn.commit()
    return {"success": True}