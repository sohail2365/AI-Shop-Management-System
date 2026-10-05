"""
import_sheet.py — Google Sheets se items PostgreSQL inventory mein import karta hai.

Chalane se pehle:
  1. .env mein DATABASE_URL sahi hona chahiye
  2. credentials.json isi folder mein honi chahiye (Google Service Account)
  3. Neeche SHEET_ID, WORKSHEET_NAME, aur SHOP_USERNAME set karo

Chalao:
  python import_sheet.py
"""
import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import text
from database import get_connection

# ---- YEH 3 CHEEZEIN SET KARO ----
SHEET_ID = "1FBOBFMsmygArr5HjpPUdpA6taAoKK8KBIr0EgGy2rnw"   # Sheet URL se ID
WORKSHEET_NAME = "Inventory"                                  # Tab ka naam
SHOP_USERNAME = "sohail"                                       # Tumhara login username

# ---- Sheet ke column numbers (0-indexed) — apni sheet ke hisaab se check karo ----
COL_ITEM_NAME = 0
COL_CATEGORY = 2
COL_SALE_PRICE = 3
COL_STOCK = 7
COL_PURCHASE_RATE = 13

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def get_shop_id(username: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            text("SELECT shop_id FROM shops WHERE username = :u"),
            {"u": username.lower()}
        ).fetchone()
    if not row:
        raise ValueError(f"'{username}' username se koi shop nahi mila — pehle register karo!")
    return row[0]


def safe_float(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def import_items():
    shop_id = get_shop_id(SHOP_USERNAME)
    print(f"Shop ID: {shop_id} ({SHOP_USERNAME})")

    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    rows = sheet.get_all_values()

    added, skipped, updated = 0, 0, 0

    with get_connection() as conn:
        for row in rows[1:]:  # header skip
            if not row or not row[COL_ITEM_NAME].strip():
                continue

            item_name = row[COL_ITEM_NAME].strip()
            category = row[COL_CATEGORY].strip() if len(row) > COL_CATEGORY else ""
            sale_price = safe_float(row[COL_SALE_PRICE]) if len(row) > COL_SALE_PRICE else 0
            stock = safe_float(row[COL_STOCK]) if len(row) > COL_STOCK else 0
            purchase_rate = safe_float(row[COL_PURCHASE_RATE]) if len(row) > COL_PURCHASE_RATE else 0

            if sale_price <= 0:
                skipped += 1
                continue

            existing = conn.execute(
                text("SELECT id FROM inventory WHERE shop_id = :shop_id AND LOWER(item_name) = :name"),
                {"shop_id": shop_id, "name": item_name.lower()}
            ).fetchone()

            if existing:
                conn.execute(
                    text("""
                        UPDATE inventory SET sale_price=:price, purchase_rate=:rate,
                        stock=:stock, category=:cat WHERE id=:id
                    """),
                    {"price": sale_price, "rate": purchase_rate, "stock": stock,
                     "cat": category, "id": existing[0]}
                )
                updated += 1
            else:
                conn.execute(
                    text("""
                        INSERT INTO inventory (shop_id, item_name, category, sale_price, purchase_rate, stock, reorder_level)
                        VALUES (:shop_id, :name, :cat, :price, :rate, :stock, 5)
                    """),
                    {"shop_id": shop_id, "name": item_name, "cat": category,
                     "price": sale_price, "rate": purchase_rate, "stock": stock}
                )
                added += 1

        conn.commit()

    print(f"\nDone! Added: {added} | Updated: {updated} | Skipped (no price): {skipped}")


if __name__ == "__main__":
    import_items()