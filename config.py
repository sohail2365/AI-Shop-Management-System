# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Database
DB_NAME = "dukaan.db"

# Google Sheets
SHEET_ID = "1FBOBFMsmygArr5HjpPUdpA6taAoKK8KBIr0EgGy2rnw"
CREDENTIALS_FILE = "credentials.json"

# AI Model
# NOTE: llama-3.3-70b-versatile was decommissioned by Groq on Aug 16, 2026.
# Migrated to openai/gpt-oss-120b (Groq's official recommended replacement).
# Configurable via env var so future Groq deprecations don't require a code change.
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b")

# Whisper model for voice transcription (still active, not deprecated)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

# Validation
if not GROQ_API_KEY:
    raise ValueError("❌ GROQ_API_KEY missing — .env file check karo")