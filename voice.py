# voice.py
"""
Voice input support — audio ko Groq Whisper se transcribe karta hai.

IMPORTANT — Roman Urdu transcription ka masla aur fix:
Whisper ko agar language decide karne diya jaye (ya "ur" force kiya jaye),
to Urdu bolne pe woh transcription URDU SCRIPT (بلال نے) mein deta hai,
Roman Urdu (Latin letters — "bilal ne") mein NAHI. Yeh Whisper ki apni
training ki wajah se hai — iska koi "Roman Urdu" mode nahi hota.

Iska direct nuksan: humara ai_parser.py aur inventory.py dono Roman/English
text expect karte hain. Agar Whisper Urdu script bhej de, to:
  1. AI parser ke examples (SYSTEM_PROMPT) Roman Urdu mein hain — match nahi hoga
  2. inventory.py ka matching (LOWER(item_name) LIKE ...) sirf Latin letters
     pe kaam karta hai — Urdu script se kabhi match nahi hoga, isliye price/
     stock database se nahi aata (yehi woh bug tha jo report hua)

FIX: language="en" force karna — is se Whisper "English sun raha hoon" samajh
kar jo bhi sunta hai wahi Latin letters mein likhta hai (phonetic spelling).
Yeh community-verified workaround hai jab Groq jaisa hosted Whisper use ho
raha ho (jahan specialized Roman-Urdu fine-tuned model available nahi).
100% perfect nahi hoga, lekin Urdu-script output se kahin behtar hai.
"""
import io
from groq import Groq
from config import GROQ_API_KEY, WHISPER_MODEL
from logger import get_logger

log = get_logger("dukaan.voice")

client = Groq(api_key=GROQ_API_KEY)

# Max audio size — Vercel Hobby tier ka request body HARD LIMIT hai 4.5MB
# (yeh Vercel ki apni limit hai, hum increase nahi kar sakte). Isse neeche
# rakhte hain taake hamara error clear ho, Vercel ka generic 413 error na aaye.
# Dukaan entry ~10-15 second ki hoti hai jo webm/opus compression mein
# typically 100-300KB banti hai — 4MB kaafi buffer hai normal use ke liye.
MAX_AUDIO_BYTES = 4 * 1024 * 1024  # 4MB (Vercel Hobby 4.5MB limit se neeche)


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.webm") -> dict:
    """
    Audio bytes leta hai, Groq Whisper se transcribe karta hai.
    Return: {"text": "...", "error": None} ya {"text": "", "error": "..."}
    """
    if not audio_bytes:
        return {"text": "", "error": "Audio khali hai — dobara record karo"}

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return {"text": "", "error": "Audio bahut lambi hai — chhoti recording try karo (max ~60 seconds)"}

    try:
        # Groq SDK ko (filename, bytes) tuple chahiye hota hai file upload ke liye
        audio_file = (filename, io.BytesIO(audio_bytes))

        transcription = client.audio.transcriptions.create(
            file=audio_file,
            model=WHISPER_MODEL,
            response_format="text",
            temperature=0,
            language="en",  # Roman/Latin script output force karne ke liye — upar comment dekho
        )

        # response_format="text" mode mein SDK seedha string return karta hai
        text = transcription if isinstance(transcription, str) else transcription.text
        text = text.strip()

        if not text:
            return {"text": "", "error": "Kuch samajh nahi aaya — saaf awaaz mein dobara bolo"}

        log.debug(f"Voice transcribed: {text}")
        return {"text": text, "error": None}

    except Exception as e:
        log.error(f"Whisper transcription failed: {e}")
        return {"text": "", "error": "Awaaz samajhne mein masla hua — dobara try karo ya likh kar entry karo"}
