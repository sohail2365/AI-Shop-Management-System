# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# ---------- API Keys ----------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ---------- Database ----------
DB_NAME = "dukaan.db"

# ---------- Google Sheets ----------
SHEET_ID = "1FBOBFMsmygArr5HjpPUdpA6taAoKK8KBIr0EgGy2rnw"
CREDENTIALS_FILE = "credentials.json"

# ---------- AI Models ----------
# NOTE: llama-3.3-70b-versatile was decommissioned by Groq on Aug 16, 2026.
# Migrated to openai/gpt-oss-120b (Groq's official recommended replacement).
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b")

# Whisper model for voice transcription
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

# ---------- Validation ----------
# GROQ_API_KEY sirf tab zaroori hai jab AI features use ho rahe hon.
# CLI tools (backup.py, import_sheet.py) iske baghair bhi chal sakte hain.
if not GROQ_API_KEY:
    print("⚠️  GROQ_API_KEY missing — AI features (parse/voice) kaam nahi karenge. .env check karo.")