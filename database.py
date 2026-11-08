# database.py — PostgreSQL (Supabase) version
# Includes: shops, customers, khaata, purchases, inventory + Phase 0 agent tables
import os
import re
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL missing — .env file check karo")

# ---- Validation: direct-connection hostname (db.xxx.supabase.co) detect karo ----
# Yeh hostname sirf IPv6 support karta hai — zyada tar home/office networks
# (khaas kar Windows) IPv6 properly configure nahi hote, isliye "could not
# translate host name" jaisa cryptic error aata hai. Pooler hostname
# (aws-0-xxx.pooler.supabase.com) IPv4 bhi support karta hai — hamesha reliable.
if re.search(r"db\.[a-z0-9]+\.supabase\.co", DATABASE_URL):
    raise ValueError(
        "\n❌ DATABASE_URL galat format mein hai — 'Direct Connection' hostname "
        "(db.xxx.supabase.co) use ho raha hai, jo IPv6-only hota hai aur zyada tar "
        "networks (especially Windows) se connect nahi hota.\n\n"
        "FIX: Supabase dashboard → Project Settings → Database → Connection String "
        "→ 'Transaction pooler' ya 'Session pooler' mode select karo (Direct connection nahi). "
        "Format kuch is tarah hoga:\n"
        "  postgresql://postgres.XXXXX:[PASSWORD]@aws-0-REGION.pooler.supabase.com:6543/postgres\n\n"
        ".env file mein DATABASE_URL ko is pooler wale se replace karo."
    )

# Supabase/Postgres needs sslmode + NullPool works best on serverless (Vercel)
if "sslmode" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require" if "?" not in DATABASE_URL else "&sslmode=require"
IS_SERVERLESS = os.getenv("VERCEL", "") == "1"

if IS_SERVERLESS:
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
else:
    # Local — connection pool use karo
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,           # 5 persistent connections
        max_overflow=10,       # +10 temporary
        pool_pre_ping=True,    # dead connections detect
        pool_recycle=300,      # 5 min pe refresh
    )


def get_connection():
    """Returns a SQLAlchemy connection. Use like:
        with get_connection() as conn:
            conn.execute(text("..."), {...})
            conn.commit()
    """
    return engine.connect()


# =========================================================
# CORE TABLES (existing)
# =========================================================

def setup_core_tables():
    """Original dukaan tables — shops, customers, khaata, purchases, inventory."""
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
                date DATE DEFAULT CURRENT_DATE
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
                date DATE DEFAULT CURRENT_DATE
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
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id),
                expense_name TEXT NOT NULL,
                amount REAL NOT NULL,
                date DATE DEFAULT CURRENT_DATE,
                notes TEXT DEFAULT ''
            )
        """))

        # ---- Non-destructive migrations (safe on existing DBs) ----
        # Soft delete: khaata + purchases par deleted_at column
        conn.execute(text(
            "ALTER TABLE khaata ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL"
        ))
        conn.execute(text(
            "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL"
        ))

        # Opening balance — "pehla udhaar" (purani register se migrate)
        # Khaata page pe grand total = opening_balance + sum(entries)
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS opening_balance REAL DEFAULT 0"
        ))

        # Indexes for faster search + filtering
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_khaata_customer_deleted "
            "ON khaata(customer_id, deleted_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_customers_shop_name "
            "ON customers(shop_id, LOWER(name))"
        ))
        conn.commit()


# =========================================================
# PHASE 0 AGENT TABLES (agentic AI foundation)
# =========================================================

def setup_agent_tables():
    """
    Agentic AI foundation tables — conversations, tasks, audit, consents,
    call_logs + customer/shop column additions.

    Idempotent — dobara chalao to safe hai. sab IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
    """
    with engine.connect() as conn:

        # ---------- CONVERSATIONS ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
                customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL,
                state JSONB DEFAULT '{}'::jsonb,
                status TEXT DEFAULT 'active',
                last_message_at TIMESTAMP DEFAULT NOW(),
                message_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_conv_shop_external "
            "ON conversations(shop_id, external_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_conv_status "
            "ON conversations(status, last_message_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_conv_customer "
            "ON conversations(customer_id) WHERE customer_id IS NOT NULL"
        ))

        # ---------- MESSAGES ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                agent_name TEXT,
                content TEXT NOT NULL,
                tool_calls JSONB,
                tokens_used INTEGER,
                latency_ms INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_msg_conv "
            "ON messages(conversation_id, created_at)"
        ))

        # ---------- AGENT TASKS (background queue) ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
                task_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                run_at TIMESTAMP NOT NULL DEFAULT NOW(),
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error TEXT,
                locked_at TIMESTAMP,
                locked_by TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_tasks_pending "
            "ON agent_tasks(run_at) WHERE status = 'pending'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_tasks_shop "
            "ON agent_tasks(shop_id, task_type, status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_tasks_running "
            "ON agent_tasks(locked_at) WHERE status = 'running'"
        ))

        # ---------- AGENT AUDIT ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS agent_audit (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
                agent_name TEXT NOT NULL,
                action TEXT NOT NULL,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
                input JSONB,
                output JSONB,
                tokens_used INTEGER,
                latency_ms INTEGER,
                success BOOLEAN DEFAULT TRUE,
                error TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_audit_shop_agent "
            "ON agent_audit(shop_id, agent_name, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_audit_conversation "
            "ON agent_audit(conversation_id, created_at) "
            "WHERE conversation_id IS NOT NULL"
        ))

        # ---------- CONSENTS (DNC / opt-in-out) ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS consents (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
                customer_id INTEGER NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
                channel TEXT NOT NULL,
                opted_in BOOLEAN NOT NULL DEFAULT TRUE,
                reason TEXT,
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(shop_id, customer_id, channel)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_consents_lookup "
            "ON consents(shop_id, customer_id, channel)"
        ))

        # ---------- CALL LOGS (voice agents) ----------
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS call_logs (
                id SERIAL PRIMARY KEY,
                shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
                customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                direction TEXT NOT NULL,
                provider TEXT,
                provider_call_id TEXT,
                from_number TEXT,
                to_number TEXT,
                duration_sec INTEGER,
                transcript TEXT,
                recording_url TEXT,
                outcome TEXT,
                cost_pkr REAL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_calls_shop "
            "ON call_logs(shop_id, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_calls_customer "
            "ON call_logs(customer_id, created_at DESC) "
            "WHERE customer_id IS NOT NULL"
        ))

        # ---------- CUSTOMERS — agent columns ----------
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "whatsapp_opted_in BOOLEAN DEFAULT TRUE"
        ))
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "voice_opted_in BOOLEAN DEFAULT TRUE"
        ))
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "last_contacted_at TIMESTAMP DEFAULT NULL"
        ))
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "tags JSONB DEFAULT '[]'::jsonb"
        ))
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "preferred_language TEXT DEFAULT 'roman_urdu'"
        ))
        conn.execute(text(
            "ALTER TABLE customers ADD COLUMN IF NOT EXISTS "
            "preferred_channel TEXT DEFAULT 'whatsapp'"
        ))

        # ---------- SHOPS — agent settings ----------
        conn.execute(text(
            "ALTER TABLE shops ADD COLUMN IF NOT EXISTS "
            "agent_settings JSONB DEFAULT '{}'::jsonb"
        ))

        conn.commit()


# =========================================================
# TRIGGERS (auto-update updated_at)
# =========================================================

def setup_triggers():
    """agent_tasks.updated_at auto-update trigger."""
    with engine.connect() as conn:
        # Function banao (idempotent — CREATE OR REPLACE)
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        # Trigger drop + recreate (Postgres mein CREATE TRIGGER IF NOT EXISTS nahi hota)
        conn.execute(text(
            "DROP TRIGGER IF EXISTS trg_agent_tasks_updated_at ON agent_tasks"
        ))
        conn.execute(text("""
            CREATE TRIGGER trg_agent_tasks_updated_at
                BEFORE UPDATE ON agent_tasks
                FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
        """))
        conn.commit()


# =========================================================
# MASTER SETUP
# =========================================================

def setup_database():
    """
    Poora database setup — core + agent tables + triggers.
    Idempotent — server start pe safely chalta hai.
    """
    setup_core_tables()
    setup_agent_tables()
    setup_triggers()
    print("Database ready! (PostgreSQL — core + agent tables)")


# =========================================================
# OPTIONAL: manual run
# =========================================================

if __name__ == "__main__":
    setup_database()