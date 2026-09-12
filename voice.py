# voice.py
"""
Voice input support — audio ko Groq Whisper se transcribe karta hai.

IMPORTANT — Roman Urdu transcription ka masla aur fix (2 iterations se seekha):

Iteration 1 (language chhoda khali): Whisper Urdu bolne pe URDU SCRIPT
(بلال نے) mein transcribe karta tha — Latin letters mein nahi.

Iteration 2 (language="en" force kiya): Ye "English hi samjho" wala
force bahut zyada strong nikla — Whisper ab Roman Urdu ko bhi galat
English words mein transcribe karne laga (jaisa poori tarah English
sunne ki koshish karta, Roman Urdu ko nahi).

Iteration 3 (yeh fix) — `language` bilkul mat do (Whisper khud detect
kare), lekin `prompt` parameter se ek Roman Urdu example do. Groq docs
ke mutabiq "prompt" model ki OUTPUT STYLE ko bias karta hai bina strict
language force kiye — is se Whisper "samajh" jata hai ke expected output
Roman/Latin script mein hai, na ke Urdu script ya pure English mein.
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

# Yeh prompt Whisper ko "style hint" deta hai — dukaan ki Roman Urdu
# vocabulary aur sentence pattern dikha kar output usi style mein bias
# karta hai. Groq max 224 tokens allow karta hai prompt ke liye.
#
# NOTE: Yeh "few-shot examples" jaisa training NAHI karta — Whisper is
# text ko "yaad" nahi rakhta, sirf overall style/spelling bias karta hai.
# Isliye zyada se zyada examples thoke se accuracy proportionally nahi
# badhti — targeted, varied examples (alag sentence types + common naam/
# items) zyada faida dete hain bajaye ek hi pattern ke 50 variations ke.
#
# Neeche diye gaye 3 categories cover karte hain: sale entry, payment,
# aur purchase — taake teeno flow ke liye style consistent rahe. Real
# customer/item naam use kiye hain (jo shop mein zyada aate hain) taake
# unki spelling Roman mein consistent rahe.
ROMAN_URDU_STYLE_PROMPT = (
    "bilal noor ne 2 brite liya, asif traders ne 3 lays 40 liye. "
    "kashif sarwar ne 5 sooper h aur 2 pepsi liye. "
    "sultan ne 500 diye, haad sohail ne 1000 wapas kiye. "
    "asad ali se 24 coke 1.5 liye 3940 mein."
)


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
            prompt=ROMAN_URDU_STYLE_PROMPT,
            # language jaan-boojh kar NAHI de rahe — Whisper khud detect kare,
            # prompt hi Roman-script output ki taraf bias karega (upar comment dekho)
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
