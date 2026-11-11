"""
import_sheet.py — Google Sheets se items PostgreSQL inventory mein import karta hai.

Chalane se pehle:
  1. .env mein DATABASE_URL sahi hona chahiye
  2. GOOGLE_CREDENTIALS_PATH env var set karein (ya default path)
  3. GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB, SHOP_USERNAME env vars set karein

.env mein:
  GOOGLE_CREDENTIALS_PATH=/etc/dukaan/credentials.json
  GOOGLE_SHEET_ID=1FBOBFMsmygArr5HjpPUdpA6taAoKK8KBIr0EgGy2rnw
  GOOGLE_SHEET_TAB=Inventory
  IMPORT_SHOP_USERNAME=sohail

Chalao:
  python import_sheet.py
"""
import os
import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import text
from dotenv import load_dotenv
from database import get_connection

load_dotenv()

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("GOOGLE_SHEET_TAB", "Inventory").strip()
SHOP_USERNAME = os.getenv("IMPORT_SHOP_USERNAME", "").strip()

# Sheet ke column numbers (0-indexed) — apni sheet ke hisaab se check karein
COL_ITEM_NAME = 0
COL_CATEGORY = 2
COL_SALE_PRICE = 3
COL_STOCK = 7
COL_PURCHASE_RATE = 13

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _require(label, value):
    if not value:
        raise ValueError(f"❌ {label} set nahi hai — .env check karein")


def get_shop_id(username: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            text("SELECT shop_id FROM shops WHERE username = :u"),
            {"u": username.lower()}
        ).fetchone()
    if not row:
        raise ValueError(f"'{username}' username se koi shop nahi mila — pehle register karein!")
    return row[0]


def safe_float(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def import_items():
    _require("GOOGLE_SHEET_ID", SHEET_ID)
    _require("IMPORT_SHOP_USERNAME", SHOP_USERNAME)

    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"❌ Credentials file nahi mili: {CREDENTIALS_PATH}\n"
            f"   GOOGLE_CREDENTIALS_PATH env var set karein, "
            f"ya file ko isi path par rakhein."
        )

    shop_id = get_shop_id(SHOP_USERNAME)
    print(f"Shop ID: {shop_id} ({SHOP_USERNAME})")

    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    rows = sheet.get_all_values()

    added, skipped, updated = 0, 0, 0

    with get_connection() as conn:
        for row in rows[1:]:  # header skip
            if not row or len(row) <= COL_ITEM_NAME or not row[COL_ITEM_NAME].strip():
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
                text("""SELECT id FROM inventory
                        WHERE shop_id = :shop_id AND LOWER(item_name) = :name"""),
                {"shop_id": shop_id, "name": item_name.lower()}
            ).fetchone()

            if existing:
                conn.execute(
                    text("""UPDATE inventory SET sale_price=:price, purchase_rate=:rate,
                            stock=:stock, category=:cat WHERE id=:id"""),
                    {"price": sale_price, "rate": purchase_rate, "stock": stock,
                     "cat": category, "id": existing[0]}
                )
                updated += 1
            else:
                conn.execute(
                    text("""INSERT INTO inventory
                            (shop_id, item_name, category, sale_price,
                             purchase_rate, stock, reorder_level)
                            VALUES (:shop_id, :name, :cat, :price, :rate, :stock, 5)"""),
                    {"shop_id": shop_id, "name": item_name, "cat": category,
                     "price": sale_price, "rate": purchase_rate, "stock": stock}
                )
                added += 1

        conn.commit()

    print(f"\nDone! Added: {added} | Updated: {updated} | Skipped (no price): {skipped}")


if __name__ == "__main__":
    import_items()