# ai_parser.py
import json
import re
import time
import functools
from groq import Groq
from config import GROQ_API_KEY, AI_MODEL
from logger import get_logger


log = get_logger("dukaan.ai_parser")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None



def _retry(max_attempts=3, base_delay=1, max_delay=8, retry_on=(ConnectionError, TimeoutError)):
    """Simple retry decorator — stdlib only, koi external dep nahi."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt == max_attempts:
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    log.warning(f"{func.__name__} attempt {attempt} failed: {e} — retrying in {delay}s")
                    time.sleep(delay)
                except Exception:
                    raise  # non-retryable
            if last_exc:
                raise last_exc
        return wrapper
    return decorator


def _extract_json(raw: str) -> dict:
    """
    Naye models kabhi kabhi ```json ... ``` fences ya extra text ke saath
    wrap kar dete hain. Yeh safety-net hai — pehle plain json.loads try karo,
    phir markdown fence strip karke retry karo.
    """
    raw = (raw or "").strip()
    if not raw:
        raise json.JSONDecodeError("Empty response", raw, 0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))

    bracket_match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
    if bracket_match:
        return json.loads(bracket_match.group(1))

    raise json.JSONDecodeError("No JSON found in response", raw, 0)


@retry(max_attempts=3, base_delay=1, max_delay=8)
def _groq_call(system_prompt: str, user_input: str, max_tokens: int = 200) -> str:
    """Groq call with retry on transient network errors."""
    if client is None:
        raise RuntimeError("GROQ_API_KEY missing — AI features disabled")

    response = client.chat.completions.create(
        model=AI_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        reasoning_effort="low",
        response_format={"type": "json_object"},
        timeout=20,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
    )
    return response.choices[0].message.content


# ---- Shared output rules: input Urdu/Urdu-Roman/English ho sakta hai,
#      lekin OUTPUT hamesha Roman Urdu (Latin lowercase) mein. ----
_OUTPUT_RULES = """IMPORTANT OUTPUT RULES:
- customer aur item ka naam HAMESHA Roman Urdu (English letters, lowercase) mein likho
- Chahe input Urdu script (اردو) mein ho, Roman mein convert karo:
    بلال → bilal, برائٹ → brite, سرف ایکسل → surf excel, کیپسٹان → capstan
- Quantity aur price HAMESHA numbers (digits) mein — Urdu ہندسے نہیں (۱، ۲ نہیں — 1، 2)
- item name mein size/variant agar user ne bataya ho to include karo (jaise "coke 1.5l", "brite 500gm")"""


SYSTEM_PROMPT = """Tu ek Karyana shop ka assistant hai.
User Urdu script (اردو)، Roman Urdu، ya English mein batayega ke kisne kya liya ya kisne paisa diya.

""" + _OUTPUT_RULES + """

Rules:
- customer: woh shakhs jisne cheez li ya paisa diya
- item: woh cheez jo li gayi — agar payment ho toh item: "PAYMENT" likho
- quantity: kitni li — agar payment ho toh 1 rakho
- price: kitne mein — agar payment ho toh amount rakho, udhaar mein 0
- Urdu/Roman grammar words ignore karo: mein, me, par, ka, ne, liya, liye, aur, or / میں، نے، نے، لیا، کے، کا، اور
- Payment words: received, wasool, wapas, diya, mila / موصول، وصول، واپس، دیا، ملا

Examples (Roman Urdu input):
"Ali ne Surf Excel 2 liya 200 mein" → {"customer": "ali", "item": "surf excel", "quantity": 2, "price": 200}
"bilal brite 3 liya 300 me" → {"customer": "bilal", "item": "brite", "quantity": 3, "price": 300}
"Sultan received 1040" → {"customer": "sultan", "item": "PAYMENT", "quantity": 1, "price": 1040}
"Ali ne 500 diye" → {"customer": "ali", "item": "PAYMENT", "quantity": 1, "price": 500}

Examples (Urdu script input — OUTPUT Roman Urdu):
"بلال نے 2 برائٹ لیا 200 میں" → {"customer": "bilal", "item": "brite", "quantity": 2, "price": 200}
"علی نے سرف ایکسل 2 لیا 200 میں" → {"customer": "ali", "item": "surf excel", "quantity": 2, "price": 200}
"سلطان نے 1040 دیے" → {"customer": "sultan", "item": "PAYMENT", "quantity": 1, "price": 1040}
"علی نے 500 دیے" → {"customer": "ali", "item": "PAYMENT", "quantity": 1, "price": 500}

Sirf JSON — kuch aur mat likho."""


MULTI_ITEM_PROMPT = """Tu ek Karyana shop ka assistant hai.
User batayega ke ek customer ne multiple items liye — Urdu script, Roman Urdu, ya English mein.
Tujhe JSON array mein jawab dena hai — kuch aur mat likho.

""" + _OUTPUT_RULES + """

Rules:
- customer: woh shakhs jisne cheez li
- items: array of {item, quantity, price} — price 0 if not mentioned
- Grammar words ignore: mein, me, par, ka, ne, liya, liye, aur, or / میں، نے، کا، کے، لیا، اور

Examples (Roman Urdu input):
"Ali ne brite 2 aur surf excel 1 liya" → {"customer": "ali", "items": [{"item": "brite", "quantity": 2, "price": 0}, {"item": "surf excel", "quantity": 1, "price": 0}]}
"bilal capstan 2, pepsi 3, bread 1 liya" → {"customer": "bilal", "items": [{"item": "capstan", "quantity": 2, "price": 0}, {"item": "pepsi", "quantity": 3, "price": 0}, {"item": "bread", "quantity": 1, "price": 0}]}

Examples (Urdu script input — OUTPUT Roman Urdu):
"بلال نے برائٹ 2 اور سرف ایکسل 1 لیا" → {"customer": "bilal", "items": [{"item": "brite", "quantity": 2, "price": 0}, {"item": "surf excel", "quantity": 1, "price": 0}]}
"علی نے کیپسٹان 2، پیپسی 3، بریڈ 1 لیا" → {"customer": "ali", "items": [{"item": "capstan", "quantity": 2, "price": 0}, {"item": "pepsi", "quantity": 3, "price": 0}, {"item": "bread", "quantity": 1, "price": 0}]}

Sirf JSON — kuch aur mat likho."""


PURCHASE_PROMPT = """Tu ek Karyana shop ka assistant hai.
User batayega ke supplier se kya kharida — Urdu script, Roman Urdu, ya English mein.
Tujhe sirf JSON mein jawab dena hai — kuch aur mat likho.

""" + _OUTPUT_RULES + """

Rules:
- supplier: supplier ka naam
- item: kya kharida — size/variant bhi include karo (jaise "coke 1.5l")
- quantity: kitna kharida
- rate: per unit price
- total_given: agar user ne total amount bataya ho toh woh yahan rakho, warna 0
- Agar rate clearly per unit lage toh rate mein rakho, total_given: 0
- Agar amount bada lage aur quantity se divide karna pade toh total_given mein rakho, rate: 0

Examples (Roman Urdu input):
"Rehman se 24 Brite 260 mein liye" → {"supplier": "rehman", "item": "brite", "quantity": 24, "rate": 260, "total_given": 0}
"shah nawaz se 24 coke 1.5 liye 3940 mein" → {"supplier": "shah nawaz", "item": "coke 1.5l", "quantity": 24, "rate": 0, "total_given": 3940}
"48 Capstan 230 mein aaya" → {"supplier": "", "item": "capstan", "quantity": 48, "rate": 230, "total_given": 0}

Examples (Urdu script input — OUTPUT Roman Urdu):
"رحمٰن سے 24 برائٹ 260 میں لیے" → {"supplier": "rehman", "item": "brite", "quantity": 24, "rate": 260, "total_given": 0}
"شاہ نواز سے 24 کوک 1.5 لیے 3940 میں" → {"supplier": "shah nawaz", "item": "coke 1.5l", "quantity": 24, "rate": 0, "total_given": 3940}

Sirf JSON — kuch aur mat likho."""


def parse_purchase(user_input: str) -> dict:
    try:
        raw = _groq_call(PURCHASE_PROMPT, user_input, max_tokens=200)
        return _extract_json(raw)
    except json.JSONDecodeError as e:
        log.error(f"AI ne galat JSON diya (purchase): {e}")
        return None
    except Exception as e:
        log.error(f"AI error (purchase): {e}")
        return None


def parse_entry(user_input: str) -> dict:
    try:
        raw = _groq_call(SYSTEM_PROMPT, user_input, max_tokens=200)
        return _extract_json(raw)
    except json.JSONDecodeError as e:
        log.error(f"AI ne galat JSON diya (entry): {e}")
        return None
    except Exception as e:
        log.error(f"AI error (entry): {e}")
        return None


def parse_multi_entry(user_input: str) -> dict:
    try:
        raw = _groq_call(MULTI_ITEM_PROMPT, user_input, max_tokens=500)
        return _extract_json(raw)
    except json.JSONDecodeError as e:
        log.error(f"AI ne galat JSON diya (multi): {e}")
        return None
    except Exception as e:
        log.error(f"AI error (multi): {e}")
        return None