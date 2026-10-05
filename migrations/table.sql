-- migrations/002_agent_tables.sql
-- Phase 0 — Agentic AI foundation tables
-- Safe to run multiple times (idempotent)
-- Run on Supabase SQL editor

-- ============================================
-- CONVERSATIONS — multi-turn dialogue state
-- ============================================
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
    channel TEXT NOT NULL,                          -- 'whatsapp' | 'voice' | 'web' | 'sms'
    external_id TEXT NOT NULL,                      -- WhatsApp phone (923001234567) / call SID
    state JSONB DEFAULT '{}'::jsonb,                -- LangGraph state / agent state
    status TEXT DEFAULT 'active',                   -- active | escalated | closed | archived
    last_message_at TIMESTAMP DEFAULT NOW(),
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_shop_external
    ON conversations(shop_id, external_id);
CREATE INDEX IF NOT EXISTS idx_conv_status
    ON conversations(status, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_customer
    ON conversations(customer_id) WHERE customer_id IS NOT NULL;

-- ============================================
-- MESSAGES — every turn of every conversation
-- ============================================
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                             -- 'user' | 'agent' | 'system' | 'tool'
    agent_name TEXT,                                -- kaunsa agent ne reply diya
    content TEXT NOT NULL,
    tool_calls JSONB,                               -- agar LLM ne tool call kiya
    tokens_used INTEGER,
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_msg_conv
    ON messages(conversation_id, created_at);

-- ============================================
-- AGENT TASKS — background job queue
-- ============================================
CREATE TABLE IF NOT EXISTS agent_tasks (
    id SERIAL PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,                        -- 'reminder' | 'upsell' | 'followup' | 'cold_call' | 'payment_nudge'
    payload JSONB NOT NULL,                         -- task-specific data
    run_at TIMESTAMP NOT NULL DEFAULT NOW(),
    status TEXT DEFAULT 'pending',                  -- pending | running | done | failed | cancelled
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    last_error TEXT,
    locked_at TIMESTAMP,                            -- jab worker ne pick kiya
    locked_by TEXT,                                 -- worker instance id
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending
    ON agent_tasks(run_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tasks_shop
    ON agent_tasks(shop_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_tasks_running
    ON agent_tasks(locked_at) WHERE status = 'running';

-- ============================================
-- AGENT AUDIT — every action for compliance
-- ============================================
CREATE TABLE IF NOT EXISTS agent_audit (
    id SERIAL PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,                           -- 'parse' | 'send_whatsapp' | 'create_order' | ...
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
    input JSONB,
    output JSONB,
    tokens_used INTEGER,
    latency_ms INTEGER,
    success BOOLEAN DEFAULT TRUE,
    error TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_shop_agent
    ON agent_audit(shop_id, agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_conversation
    ON agent_audit(conversation_id, created_at) WHERE conversation_id IS NOT NULL;

-- ============================================
-- CONSENTS / DNC — opt-in / opt-out tracking
-- ============================================
CREATE TABLE IF NOT EXISTS consents (
    id SERIAL PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,                          -- 'whatsapp' | 'voice' | 'sms'
    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
    reason TEXT,                                    -- 'user_reply_stop' | 'manual' | 'default'
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(shop_id, customer_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_consents_lookup
    ON consents(shop_id, customer_id, channel);

-- ============================================
-- CALL LOGS — voice agent history
-- ============================================
CREATE TABLE IF NOT EXISTS call_logs (
    id SERIAL PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    customer_id INTEGER REFERENCES customers(customer_id) ON DELETE SET NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    direction TEXT NOT NULL,                        -- 'inbound' | 'outbound'
    provider TEXT,                                  -- 'vapi' | 'twilio' | 'bland'
    provider_call_id TEXT,
    from_number TEXT,
    to_number TEXT,
    duration_sec INTEGER,
    transcript TEXT,
    recording_url TEXT,
    outcome TEXT,                                   -- 'answered' | 'no_answer' | 'voicemail' | 'failed' | 'completed'
    cost_pkr REAL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calls_shop
    ON call_logs(shop_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_customer
    ON call_logs(customer_id, created_at DESC) WHERE customer_id IS NOT NULL;

-- ============================================
-- CUSTOMERS — agent-related columns
-- ============================================
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS whatsapp_opted_in BOOLEAN DEFAULT TRUE;
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS voice_opted_in BOOLEAN DEFAULT TRUE;
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS last_contacted_at TIMESTAMP DEFAULT NULL;
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb;         -- 'vip', 'chronic_defaulter', 'new'
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS preferred_language TEXT DEFAULT 'roman_urdu';
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS preferred_channel TEXT DEFAULT 'whatsapp';

-- ============================================
-- SHOPS — agent settings per tenant
-- ============================================
ALTER TABLE shops
    ADD COLUMN IF NOT EXISTS agent_settings JSONB DEFAULT '{}'::jsonb;
-- Example: {
--   "reminder_enabled": true,
--   "reminder_hour": 10,
--   "auto_whatsapp": true,
--   "cold_call_enabled": false,
--   "daily_call_limit": 50,
--   "quiet_hours_start": 20,
--   "quiet_hours_end": 9,
--   "ai_disclosure": true
-- }

-- ============================================
-- TRIGGERS — auto-update updated_at
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_tasks_updated_at ON agent_tasks;
CREATE TRIGGER trg_agent_tasks_updated_at
    BEFORE UPDATE ON agent_tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- CLEANUP — conversations older than 90 days archived
-- ============================================
-- Yeh manual ya cron se chalega (worker/scheduler.py mein handle hoga)

-- ============================================
-- DONE
-- ============================================