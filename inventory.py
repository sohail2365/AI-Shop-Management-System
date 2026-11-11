# inventory.py — PostgreSQL version
from typing import Optional
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.inventory")


def _stock_val(v) -> float:
    """Schema has stock REAL — never truncate to int (breaks fractional weights)."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# =========================================================
# ATOMIC STOCK HELPER — caller manages transaction
# =========================================================

def apply_stock_delta(conn, shop_id: int, item_name: str, delta: float,
                      inventory_id: Optional[int] = None) -> bool:
    """
    Apply a stock delta within an existing transaction. Caller must commit.

    delta > 0  → stock added (purchase, return)
    delta < 0  → stock removed (sale)

    If inventory_id given → updates that exact row.
    Otherwise → exact case-insensitive name match (NO wildcard/fuzzy).

    Returns True if an inventory row was updated.
    Safe for concurrent calls — single UPDATE, no read-modify-write.
    """
    if not delta:
        return True  # nothing to do, not an error

    try:
        if inventory_id is not None:
            result = conn.execute(
                text("""UPDATE inventory SET stock = stock + :d
                        WHERE id = :id AND shop_id = :sid"""),
                {"d": delta, "id": inventory_id, "sid": shop_id}
            )
            return result.rowcount > 0

        name = (item_name or "").strip().lower()
        if not name:
            return False

        result = conn.execute(
            text("""UPDATE inventory SET stock = stock + :d
                    WHERE shop_id = :sid AND LOWER(item_name) = :name"""),
            {"d": delta, "sid": shop_id, "name": name}
        )
        return result.rowcount > 0
    except Exception as e:
        log.error(f"apply_stock_delta failed: {e}")
        return False


# =========================================================
# READS
# =========================================================

def get_item_price(item_name: str, shop_id: int):
    try:
        with get_connection() as conn:
            item_lower = item_name.lower().strip()
            row = conn.execute(
                text("""
                    SELECT id, item_name, sale_price, stock
                    FROM inventory
                    WHERE shop_id = :shop_id
                      AND LOWER(item_name) LIKE :val
                    ORDER BY
                        CASE
                            WHEN LOWER(item_name) = :exact THEN 0
                            WHEN LOWER(item_name) LIKE :starts THEN 1
                            ELSE 2
                        END,
                        CASE WHEN stock > 0 THEN 0 ELSE 1 END,
                        LENGTH(item_name) DESC
                    LIMIT 1
                """),
                {
                    "shop_id": shop_id,
                    "val": f"%{item_lower}%",
                    "exact": item_lower,
                    "starts": f"{item_lower}%",
                }
            ).fetchone()

            if row:
                return {"id": row[0], "name": row[1],
                        "price": float(row[2]) if row[2] is not None else 0.0,
                        "stock": _stock_val(row[3])}

            # Word-level fallback
            words = [w for w in item_lower.split() if len(w) > 3]
            for word in words:
                row = conn.execute(
                    text("""
                        SELECT id, item_name, sale_price, stock
                        FROM inventory
                        WHERE shop_id = :shop_id AND LOWER(item_name) LIKE :val
                        ORDER BY
                            CASE WHEN stock > 0 THEN 0 ELSE 1 END,
                            LENGTH(item_name) DESC
                        LIMIT 1
                    """),
                    {"shop_id": shop_id, "val": f"%{word}%"}
                ).fetchone()
                if row:
                    return {"id": row[0], "name": row[1],
                            "price": float(row[2]) if row[2] is not None else 0.0,
                            "stock": _stock_val(row[3])}

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

        return [{"id": r[0], "name": r[1],
                 "price": float(r[2]) if r[2] is not None else 0.0,
                 "stock": _stock_val(r[3])} for r in rows]
    except Exception as e:
        log.error(f"Variants error: {e}")
        return []


def get_item_by_id(item_id: int, shop_id: int):
    try:
        with get_connection() as conn:
            row = conn.execute(
                text("""SELECT id, item_name, sale_price, stock, purchase_rate
                        FROM inventory WHERE id = :id AND shop_id = :shop_id"""),
                {"id": item_id, "shop_id": shop_id}
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1],
                "price": float(row[2]) if row[2] is not None else 0.0,
                "stock": _stock_val(row[3]),
                "purchase_rate": float(row[4]) if row[4] is not None else 0.0}
    except Exception as e:
        log.error(f"Inventory by id error: {e}")
        return None


# =========================================================
# DEPRECATED — kept for backward compat
# =========================================================

def update_stock_by_id(item_id: int, quantity_sold: float, shop_id: int) -> bool:
    """DEPRECATED — use apply_stock_delta inside a transaction.

    Kept for callers that still call it standalone. Now atomic (single UPDATE),
    so no lost-update race like the old read-modify-write. But you lose
    cross-table atomicity — prefer apply_stock_delta.
    """
    with get_connection() as conn:
        ok = apply_stock_delta(conn, shop_id, "", -quantity_sold, inventory_id=item_id)
        conn.commit()
    return ok


def update_stock(item_name: str, quantity_sold: float, shop_id: int) -> bool:
    """DEPRECATED — use apply_stock_delta inside a transaction.

    Strict EXACT (case-insensitive) name match only. Old `%LIKE%` fuzzy
    matching was silently decrementing the wrong item when variants existed
    (e.g. "brite" matching "brite 500gm" instead of "brite 1kg").
    """
    with get_connection() as conn:
        ok = apply_stock_delta(conn, shop_id, item_name, -quantity_sold)
        conn.commit()
    return ok


def update_stock_purchase(item_name: str, quantity_bought: float, shop_id: int) -> bool:
    """DEPRECATED — use apply_stock_delta inside a transaction.
    Strict EXACT (case-insensitive) name match only."""
    with get_connection() as conn:
        ok = apply_stock_delta(conn, shop_id, item_name, quantity_bought)
        conn.commit()
    return ok


# =========================================================
# CRUD
# =========================================================

def add_item(shop_id: int, item_name: str, sale_price: float,
             purchase_rate: float = 0, stock: float = 0,
             category: str = '', reorder_level: int = 5) -> dict:
    # ---- Validation ----
    name = (item_name or "").strip()
    if not name:
        return {"error": "Item name zaroori hai"}
    if len(name) > 200:
        return {"error": "Item name bahut lamba hai (max 200)"}
    try:
        sale_price = float(sale_price)
        purchase_rate = float(purchase_rate or 0)
        stock = float(stock or 0)
        reorder_level = int(reorder_level or 5)
    except (TypeError, ValueError):
        return {"error": "Price/stock/reorder numeric hona chahiye"}
    if sale_price <= 0:
        return {"error": "Sale price 0 se zyada hona chahiye"}
    if purchase_rate < 0:
        return {"error": "Purchase rate negative nahi ho sakta"}
    if stock < 0:
        return {"error": "Initial stock negative nahi ho sakta"}
    if reorder_level < 0:
        return {"error": "Reorder level negative nahi ho sakta"}

    try:
        with get_connection() as conn:
            existing = conn.execute(
                text("""SELECT id FROM inventory
                        WHERE shop_id = :shop_id AND LOWER(item_name) = :name"""),
                {"shop_id": shop_id, "name": name.lower()}
            ).fetchone()
            if existing:
                return {"error": f"'{name}' pehle se exist karta hai"}

            conn.execute(
                text("""
                    INSERT INTO inventory
                    (shop_id, item_name, category, sale_price, purchase_rate, stock, reorder_level)
                    VALUES (:shop_id, :item_name, :category, :sale_price, :purchase_rate, :stock, :reorder_level)
                """),
                {
                    "shop_id": shop_id, "item_name": name, "category": (category or "").strip(),
                    "sale_price": sale_price, "purchase_rate": purchase_rate,
                    "stock": stock, "reorder_level": reorder_level
                }
            )
            conn.commit()
        log.info(f"Item added: {name} (shop={shop_id})")
        return {"success": True}
    except Exception as e:
        log.error(f"add_item failed: {e}")
        return {"error": "Item add nahi hua"}


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
            "price": float(r[3]) if r[3] is not None else 0.0,
            "purchase_rate": float(r[4]) if r[4] is not None else 0.0,
            "stock": _stock_val(r[5]), "reorder_level": r[6],
            "low_stock": _stock_val(r[5]) <= (r[6] or 0)
        }
        for r in rows
    ]


def edit_item(item_id: int, shop_id: int, item_name: str, sale_price: float,
              purchase_rate: float, stock: float, category: str, reorder_level: int) -> dict:
    name = (item_name or "").strip()
    if not name:
        return {"error": "Item name zaroori hai"}
    try:
        sale_price = float(sale_price)
        purchase_rate = float(purchase_rate or 0)
        stock = float(stock or 0)
        reorder_level = int(reorder_level or 5)
    except (TypeError, ValueError):
        return {"error": "Price/stock/reorder numeric hona chahiye"}
    if sale_price <= 0:
        return {"error": "Sale price 0 se zyada hona chahiye"}

    try:
        with get_connection() as conn:
            result = conn.execute(
                text("""
                    UPDATE inventory SET item_name=:item_name, category=:category,
                        sale_price=:sale_price, purchase_rate=:purchase_rate,
                        stock=:stock, reorder_level=:reorder_level
                    WHERE id=:id AND shop_id=:shop_id
                """),
                {
                    "item_name": name, "category": (category or "").strip(),
                    "sale_price": sale_price, "purchase_rate": purchase_rate,
                    "stock": stock, "reorder_level": reorder_level,
                    "id": item_id, "shop_id": shop_id
                }
            )
            conn.commit()
            if result.rowcount == 0:
                return {"error": "Item nahi mila"}
        return {"success": True}
    except Exception as e:
        log.error(f"edit_item failed: {e}")
        return {"error": "Item update nahi hua"}


def delete_item(item_id: int, shop_id: int) -> dict:
    try:
        with get_connection() as conn:
            result = conn.execute(
                text("DELETE FROM inventory WHERE id=:id AND shop_id=:shop_id"),
                {"id": item_id, "shop_id": shop_id}
            )
            conn.commit()
            if result.rowcount == 0:
                return {"error": "Item nahi mila"}
        return {"success": True}
    except Exception as e:
        log.error(f"delete_item failed: {e}")
        return {"error": "Item delete nahi hua"}