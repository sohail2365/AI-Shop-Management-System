# voice.py
"""
Voice input support — audio ko Groq Whisper se transcribe karta hai.

STRATEGY (Plan A):
Whisper ko NATIVE Urdu mode (language="ur") mein chalate hain. Yeh
Whisper ka strong point hai — Urdu audio ko Urdu script mein accurately
likhta hai, khaas tor pe item names aur numbers.

Phir ai_parser.py Urdu script (اردو) ko directly parse karta hai aur
Roman Urdu JSON return karta hai. Isse do faide:
  1. Urdu audio ki accuracy 90-95% (pehle "language=en" trick se 70-85% thi)
  2. User Urdu keyboard se type kare to bhi kaam karega

Purani "language=en" trick ab use NAHI hoti kyunki woh item names ko
phonetically distort kar deti thi ("brite" → "bright", "capstan" → "cap stone").
"""
import io
from groq import Groq
from config import GROQ_API_KEY, WHISPER_MODEL
from logger import get_logger

log = get_logger("dukaan.voice")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Vercel Hobby request body limit 4.5MB — iske neeche rehte hain
MAX_AUDIO_BYTES = 4 * 1024 * 1024  # 4MB

# Urdu prompt — Whisper ko bias karta hai Urdu vocabulary aur shop terminology ki taraf.
# Item names Urdu script mein likhe hain, aur numbers ko digits mein rakhne ka ishara.
_TRANSCRIPTION_PROMPT = (
    "یہ ایک پاکستانی کریانہ دکان کی اردو انٹری ہے۔ "
    "مثالیں: 'بلال نے 2 برائٹ لیا 200 میں'، "
    "'علی نے سرف ایکسل 1 لیا'، "
    "'سلطان نے 500 دیے'، "
    "'رحمٰن سے 24 کوک 1.5 لیے'۔ "
    "عام اشیاء: برائٹ، سرف ایکسل، کیپسٹان، پیپسی، کوک، بریڈ، لیز، سوپر، "
    "ڈیٹا، چاول، چینی، تیل، گھی، دودھ، دہی، چائے، بسکٹ، صابن۔ "
    "نمبر ہمیشہ انگریزی ہندسوں میں لکھو (1، 2، 3)۔"
)


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.webm") -> dict:
    """
    Audio bytes leta hai, Groq Whisper (Urdu mode) se transcribe karta hai.
    Return: {"text": "...", "error": None} ya {"text": "", "error": "..."}
    """
    if client is None:
        return {"text": "", "error": "GROQ_API_KEY missing — voice features disabled"}

    if not audio_bytes:
        return {"text": "", "error": "Audio khali hai — dobara record karo"}

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return {"text": "", "error": "Audio bahut lambi hai — chhoti recording try karo (max ~60 seconds)"}

    try:
        audio_file = (filename, io.BytesIO(audio_bytes))

        transcription = client.audio.transcriptions.create(
            file=audio_file,
            model=WHISPER_MODEL,
            response_format="text",
            temperature=0,
            language="ur",                   # Native Urdu mode — accurate item names
            prompt=_TRANSCRIPTION_PROMPT,    # Urdu vocabulary bias
        )

        text = transcription if isinstance(transcription, str) else transcription.text
        text = text.strip()

        if not text:
            return {"text": "", "error": "Kuch samajh nahi aaya — saaf awaaz mein dobara bolo"}

        log.debug(f"Voice transcribed (ur): {text}")
        return {"text": text, "error": None}

    except Exception as e:
        log.error(f"Whisper transcription failed: {e}")
        return {"text": "", "error": "Awaaz samajhne mein masla hua — dobara try karo ya likh kar entry karo"}