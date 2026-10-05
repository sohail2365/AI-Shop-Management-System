# agent/tools/voice_tools.py
from voice import transcribe_audio
from logger import get_logger

log = get_logger("dukaan.tools.voice")


def tool_transcribe(audio_bytes: bytes, filename: str = "voice.webm") -> dict:
    """Voice → Roman Urdu text. Phase 1 (voice inbound) mein use hoga."""
    return transcribe_audio(audio_bytes, filename=filename)