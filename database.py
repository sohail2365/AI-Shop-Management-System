# database.py — PostgreSQL (Supabase) version
import os
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv
from logger import get_logger

load_dotenv()

log = get_logger("dukaan.database")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL missing — .env file check karo")

# Supabase/Postgres needs sslmode + NullPool works best on serverless (Vercel)
if "sslmode" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require" if "?" not in DATABASE_URL else "&sslmode=require"

engine = create_engine(DATABASE_URL, poolclass=NullPool)


def get_connection():
    """Returns a SQLAlchemy connection. Use like:
        with get_connection() as conn:
            conn.execute(text("..."), {...})
            conn.commit()
    """
    return engine.connect()


def _safe_execute(sql: str, label: str = ""):
    """
    Ek statement ko apne transaction mein chalao. Agar fail ho (jaise
    duplicate index banane ki koshish jab duplicates maujood hon) to
    warning log karo aur aage barho — baaki migrations na ruken.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        if label:
            log.info(f"Migration OK: {label}")
    except Exception as e:
        log.warning(f"Migration skipped ({label or sql[:50]}): {e}")


def setup_database():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                shop_name TEXT NOT NULL,
                created_at DATE DEFAULT CURRENT_DATE
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers (
                customer_id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id),
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at DATE DEFAULT CURRENT_DATE
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS khaata (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
                item_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                price_per_item REAL NOT NULL,
                total REAL NOT NULL,
                date DATE DEFAULT CURRENT_DATE,
                inventory_id INTEGER
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id),
                supplier_name TEXT NOT NULL,
                item_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                purchase_rate REAL NOT NULL,
                total_cost REAL NOT NULL,
                date DATE DEFAULT CURRENT_DATE,
                inventory_id INTEGER
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id),
                item_name TEXT NOT NULL,
                category TEXT DEFAULT '',
                sale_price REAL NOT NULL,
                purchase_rate REAL DEFAULT 0,
                stock REAL DEFAULT 0,
                reorder_level INTEGER DEFAULT 5,
                created_at DATE DEFAULT CURRENT_DATE
            )
        """))
        conn.commit()

    # ---- Non-destructive migrations (existing DBs ke liye safe) ----
    # Har migration apni alag transaction mein, taake ek fail ho to baaki chalein.
    _safe_execute(
        "ALTER TABLE khaata ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL",
        "khaata.deleted_at"
    )
    _safe_execute(
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL",
        "purchases.deleted_at"
    )
    _safe_execute(
        "ALTER TABLE khaata ADD COLUMN IF NOT EXISTS inventory_id INTEGER",
        "khaata.inventory_id"
    )
    _safe_execute(
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS inventory_id INTEGER",
        "purchases.inventory_id"
    )
    _safe_execute(
        "CREATE INDEX IF NOT EXISTS idx_khaata_customer_deleted ON khaata(customer_id, deleted_at)",
        "idx_khaata_customer_deleted"
    )
    _safe_execute(
        "CREATE INDEX IF NOT EXISTS idx_customers_shop_name ON customers(shop_id, LOWER(name))",
        "idx_customers_shop_name"
    )
    # Unique indexes — duplicates maujood hon to skip ho jayenge (warning log mein aayega)
    _safe_execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_customer_shop_lower_name ON customers (shop_id, LOWER(name))",
        "uniq_customer_shop_lower_name"
    )
    _safe_execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_inventory_shop_lower_name ON inventory (shop_id, LOWER(item_name))",
        "uniq_inventory_shop_lower_name"
    )

    log.info("Database ready! (PostgreSQL)")