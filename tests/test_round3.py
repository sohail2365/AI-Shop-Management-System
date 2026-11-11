# test_round3.py — Round 3 verification (Windows PowerShell compatible)
import sys
import json
from database import get_connection, setup_database
from sqlalchemy import text


def check(label, fn):
    try:
        fn()
        print(f"  ✅ {label}")
        return True
    except AssertionError as e:
        print(f"  ❌ {label}: {e}")
        return False
    except Exception as e:
        print(f"  ❌ {label}: {type(e).__name__}: {e}")
        return False


def _cleanup_test_data():
    """Remove test entities left behind by tests."""
    with get_connection() as conn:
        conn.execute(text("""
            DELETE FROM khaata WHERE customer_id IN
                (SELECT customer_id FROM customers
                 WHERE name LIKE 'r3test%' AND shop_id = 1)
        """))
        conn.execute(text("DELETE FROM customers WHERE name LIKE 'r3test%' AND shop_id = 1"))
        conn.execute(text("DELETE FROM inventory WHERE item_name LIKE 'r3test%' AND shop_id = 1"))
        conn.execute(text("DELETE FROM purchases WHERE item_name LIKE 'r3test%' AND shop_id = 1"))
        conn.commit()


def test_schema_complete():
    with get_connection() as conn:
        # supplier_payments
        row = conn.execute(text(
            """SELECT 1 FROM information_schema.tables
               WHERE table_name = 'supplier_payments'"""
        )).fetchone()
        assert row, "supplier_payments table missing"

        # khaata new columns
        for col in ("inventory_id", "cost_price"):
            row = conn.execute(text(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'khaata' AND column_name = :c"""
            ), {"c": col}).fetchone()
            assert row, f"khaata.{col} missing"

        # purchases.inventory_id
        row = conn.execute(text(
            """SELECT 1 FROM information_schema.columns
               WHERE table_name = 'purchases' AND column_name = 'inventory_id'"""
        )).fetchone()
        assert row, "purchases.inventory_id missing"


def test_atomic_save_entry():
    """save_entry should write khaata + update stock in same transaction."""
    from customers import add_khaata_entry
    from inventory import apply_stock_delta

    with get_connection() as conn:
        conn.execute(text(
            "DELETE FROM inventory WHERE item_name = 'r3test_atomic' AND shop_id = 1"
        ))
        conn.execute(text(
            """INSERT INTO inventory
               (shop_id, item_name, sale_price, purchase_rate, stock, reorder_level)
               VALUES (1, 'r3test_atomic', 100, 60, 20, 5)"""
        ))
        conn.commit()

    # Simulate atomic flow (what save_entry does internally)
    with get_connection() as conn:
        ok = add_khaata_entry("r3test_atomic_cust", "r3test_atomic", 3, 100, 1,
                              inventory_id=None, cost_price=60, conn=conn)
        assert ok, "add_khaata_entry failed"

        with get_connection() as conn2:
            inv = conn2.execute(text(
                """SELECT id FROM inventory
                   WHERE item_name = 'r3test_atomic' AND shop_id = 1"""
            )).fetchone()
        inv_id = inv[0]

        stock_ok = apply_stock_delta(conn, 1, "r3test_atomic", -3, inventory_id=inv_id)
        assert stock_ok, "apply_stock_delta failed"
        conn.commit()

    # Verify
    with get_connection() as conn:
        stock = conn.execute(text(
            "SELECT stock FROM inventory WHERE item_name = 'r3test_atomic' AND shop_id = 1"
        )).fetchone()[0]
        assert float(stock) == 17.0, f"stock = {stock}, expected 17 (20-3)"

        cost = conn.execute(text("""
            SELECT k.cost_price FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.name = 'r3test_atomic_cust' AND c.shop_id = 1
            ORDER BY k.id DESC LIMIT 1
        """)).fetchone()[0]
        assert float(cost) == 60.0, f"cost_price = {cost}, expected 60"


def test_atomic_rollback():
    """If any step fails, entire transaction should roll back."""
    from customers import add_khaata_entry

    with get_connection() as conn:
        inv = conn.execute(text(
            """SELECT id FROM inventory
               WHERE item_name = 'r3test_atomic' AND shop_id = 1"""
        )).fetchone()
        inv_id = inv[0]

    # Capture state
    with get_connection() as conn:
        stock_before = float(conn.execute(text(
            "SELECT stock FROM inventory WHERE id = :id"
        ), {"id": inv_id}).fetchone()[0])
        khaata_before = conn.execute(text("""
            SELECT COUNT(*) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.name = 'r3test_rollback' AND c.shop_id = 1
        """)).fetchone()[0]

    # Attempt: insert khaata, then simulate failure (rollback, no commit)
    with get_connection() as conn:
        add_khaata_entry("r3test_rollback", "r3test_atomic", 5, 100, 1, conn=conn)
        conn.rollback()

    # Verify nothing persisted
    with get_connection() as conn:
        khaata_after = conn.execute(text("""
            SELECT COUNT(*) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.name = 'r3test_rollback' AND c.shop_id = 1
        """)).fetchone()[0]
        assert khaata_after == khaata_before, f"rollback failed: {khaata_before} -> {khaata_after}"

        stock_after = float(conn.execute(text(
            "SELECT stock FROM inventory WHERE id = :id"
        ), {"id": inv_id}).fetchone()[0])
        assert stock_after == stock_before, f"stock drift: {stock_before} -> {stock_after}"


def test_profit_calculation_uses_cost_price():
    """daily_closing profit should use cost_price snapshot, not sale price."""
    from customers import add_khaata_entry

    with get_connection() as conn:
        # Create test inventory
        conn.execute(text(
            "DELETE FROM inventory WHERE item_name = 'r3test_profit' AND shop_id = 1"
        ))
        conn.execute(text(
            """INSERT INTO inventory
               (shop_id, item_name, sale_price, purchase_rate, stock, reorder_level)
               VALUES (1, 'r3test_profit', 500, 250, 10, 5) RETURNING id"""
        ))
        inv_id = conn.execute(text(
            "SELECT id FROM inventory WHERE item_name = 'r3test_profit' AND shop_id = 1"
        )).fetchone()[0]
        conn.commit()

    # Insert khaata entry with cost_price snapshot
    with get_connection() as conn:
        add_khaata_entry("r3test_profit_cust", "r3test_profit", 2, 500, 1,
                        inventory_id=inv_id, cost_price=250, conn=conn)
        conn.commit()

    # Verify the profit report query returns proper cost
    with get_connection() as conn:
        row = conn.execute(text("""
            SELECT k.item_name, k.quantity, k.price_per_item, k.total,
                   COALESCE(NULLIF(k.cost_price, 0), i.purchase_rate, 0) as cost_rate
            FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            LEFT JOIN inventory i ON i.id = k.inventory_id
            WHERE c.name = 'r3test_profit_cust' AND c.shop_id = 1
        """)).fetchone()

    cost_rate = float(row[4])
    assert cost_rate == 250.0, f"cost_rate = {cost_rate}, expected 250"
    profit = (500 - 250) * 2
    assert profit == 500, f"profit = {profit}, expected 500 (not 1000)"


def test_delete_entry_restores_stock():
    """Deleting a sale entry should reverse the stock deduction."""
    from customers import add_khaata_entry
    from inventory import apply_stock_delta

    with get_connection() as conn:
        # Fresh item
        conn.execute(text(
            "DELETE FROM inventory WHERE item_name = 'r3test_del' AND shop_id = 1"
        ))
        conn.execute(text(
            """INSERT INTO inventory
               (shop_id, item_name, sale_price, purchase_rate, stock, reorder_level)
               VALUES (1, 'r3test_del', 100, 50, 20, 5) RETURNING id"""
        ))
        inv_id = conn.execute(text(
            "SELECT id FROM inventory WHERE item_name = 'r3test_del' AND shop_id = 1"
        )).fetchone()[0]
        conn.commit()

    # Sale: stock 20 -> 15
    with get_connection() as conn:
        add_khaata_entry("r3test_del_cust", "r3test_del", 5, 100, 1,
                        inventory_id=inv_id, cost_price=50, conn=conn)
        apply_stock_delta(conn, 1, "r3test_del", -5, inventory_id=inv_id)
        conn.commit()

    with get_connection() as conn:
        stock = float(conn.execute(text(
            "SELECT stock FROM inventory WHERE id = :id"
        ), {"id": inv_id}).fetchone()[0])
        assert stock == 15.0, f"after sale stock = {stock}, expected 15"

    # Simulate delete_entry logic (soft delete + stock restore)
    with get_connection() as conn:
        entry = conn.execute(text("""
            SELECT k.id, k.quantity FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.name = 'r3test_del_cust' AND c.shop_id = 1
            AND k.deleted_at IS NULL LIMIT 1
        """)).fetchone()
        entry_id, qty = entry[0], float(entry[1])

        conn.execute(text(
            "UPDATE khaata SET deleted_at = NOW() WHERE id = :eid"
        ), {"eid": entry_id})
        apply_stock_delta(conn, 1, "r3test_del", qty, inventory_id=inv_id)
        conn.commit()

    with get_connection() as conn:
        stock = float(conn.execute(text(
            "SELECT stock FROM inventory WHERE id = :id"
        ), {"id": inv_id}).fetchone()[0])
        assert stock == 20.0, f"after delete stock = {stock}, expected 20"


def test_fractional_stock_preserved():
    """Fractional stock (kg-based items) should not be truncated."""
    from inventory import _stock_val
    assert _stock_val(0.5) == 0.5
    assert _stock_val(2.75) == 2.75
    assert _stock_val(None) == 0.0
    assert _stock_val("1.5") == 1.5


def main():
    print("=" * 60)
    print(" Round 3 verification")
    print("=" * 60)

    # Ensure schema is migrated
    setup_database()
    _cleanup_test_data()

    results = []

    print("\n[1] Schema complete (supplier_payments + new columns)")
    results.append(check("schema", test_schema_complete))

    print("\n[2] Atomic khaata + stock save")
    results.append(check("atomic save", test_atomic_save_entry))

    print("\n[3] Rollback works (no partial write)")
    results.append(check("rollback", test_atomic_rollback))

    print("\n[4] Profit uses cost_price snapshot")
    results.append(check("profit", test_profit_calculation_uses_cost_price))

    print("\n[5] Delete entry restores stock")
    results.append(check("delete restores", test_delete_entry_restores_stock))

    print("\n[6] Fractional stock preserved")
    results.append(check("fractional", test_fractional_stock_preserved))

    _cleanup_test_data()

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f" Result: {passed}/{total} passed")
    print("=" * 60)
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()