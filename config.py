# config.py
import os
import warnings
from dotenv import load_dotenv

load_dotenv()

# ---- Environment mode ----
ENV = os.getenv("ENV", "development").lower()
IS_PRODUCTION = (
    ENV == "production"
    or os.getenv("VERCEL_ENV", "") == "production"
)

# ---- API Keys ----
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
if not GROQ_API_KEY:
    warnings.warn(
        "GROQ_API_KEY missing — AI features (parse, multi-parse, purchase, voice) disabled",
        stacklevel=2,
    )

# ---- AI Models ----
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-20b")
FAST_MODEL = os.getenv("FAST_MODEL", "qwen/qwen3.8-27b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

# ---- Agent ----
WORKER_URL = os.getenv("WORKER_URL", "http://localhost:8001")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "false").lower() == "true"

# ---- WhatsApp ----
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "dukaan-ai-verify")

# ---- CORS ----
_allowed = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://localhost:3000",
)
ALLOWED_ORIGINS = [o.strip() for o in _allowed.split(",") if o.strip()]
if IS_PRODUCTION and "*" in ALLOWED_ORIGINS:
    raise ValueError(
        "❌ Production mein ALLOWED_ORIGINS mein '*' allowed nahi. "
        "Specific origin set karein (e.g. https://your-app.vercel.app)"
    )

# ---- Voice (Phase 3) ----
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "")
VAPI_PHONE_ID = os.getenv("VAPI_PHONE_ID", "")

# ---- Production safety checks ----
if IS_PRODUCTION and AGENT_ENABLED and not WHATSAPP_APP_SECRET:
    raise ValueError(
        "❌ Production mein WHATSAPP_APP_SECRET zaroori hai jab AGENT_ENABLED=true. "
        "Warna webhook open hai — koi bhi fake messages bhej sakta hai."
    )

if IS_PRODUCTION and not GROQ_API_KEY:
    warnings.warn(
        "⚠️ Production mein GROQ_API_KEY missing — AI features disabled",
        stacklevel=2,
    )