# ai_parser.py
import json
import re
from groq import Groq
from config import GROQ_API_KEY, AI_MODEL

client = Groq(api_key=GROQ_API_KEY)


def _extract_json(raw: str) -> dict:
    """
    Naye models (gpt-oss-120b) kabhi kabhi ```json ... ``` fences ya extra
    text ke saath wrap kar dete hain. Yeh safety-net hai — pehle plain
    json.loads try karo, phir markdown fence strip karke retry karo.
    """
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # ```json { ... } ``` ya ``` { ... } ``` fences hata do
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))
    # Fallback: pehla { ya [ se lekar aakhri } ya ] tak nikal lo
    bracket_match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
    if bracket_match:
        return json.loads(bracket_match.group(1))
    raise json.JSONDecodeError("No JSON found in response", raw, 0)

SYSTEM_PROMPT = """Tu ek Karyana shop ka assistant hai.
User Urdu ya English mein batayega ke kisne kya liya ya kisne paisa diya.

Rules:
- customer: woh shakhs jisne cheez li ya paisa diya
- item: woh cheez jo li gayi — agar payment ho toh item: "PAYMENT" likho
- quantity: kitni li — agar payment ho toh 1 rakho
- price: kitne mein — agar payment ho toh amount rakho, udhaar mein 0
- "mein", "me", "par", "ka", "ne", "liya", "liye" — Urdu grammar, ignore karo
- "received", "wasool", "wapas", "diya", "mila" — yeh payment hai

Examples:
"Ali ne Surf Excel 2 liya 200 mein" → {"customer": "ali", "item": "surf excel", "quantity": 2, "price": 200}
"bilal brite 3 liya 300 me" → {"customer": "bilal", "item": "brite", "quantity": 3, "price": 300}
"Sultan received 1040" → {"customer": "sultan", "item": "PAYMENT", "quantity": 1, "price": 1040}
"Ali ne 500 diye" → {"customer": "ali", "item": "PAYMENT", "quantity": 1, "price": 500}

Sirf JSON — kuch aur mat likho."""
MULTI_ITEM_PROMPT = """Tu ek Karyana shop ka assistant hai.
User batayega ke ek customer ne multiple items liye.
Tujhe JSON array mein jawab dena hai — kuch aur mat likho.

Rules:
- customer: woh shakhs jisne cheez li
- items: array of {item, quantity, price} — price 0 if not mentioned
- "mein", "me", "par", "ka", "ne", "liya", "liye", "aur", "or" — Urdu/English grammar

Examples:
"Ali ne brite 2 aur surf excel 1 liya" → {"customer": "ali", "items": [{"item": "brite", "quantity": 2, "price": 0}, {"item": "surf excel", "quantity": 1, "price": 0}]}
"bilal capstan 2, pepsi 3, bread 1 liya" → {"customer": "bilal", "items": [{"item": "capstan", "quantity": 2, "price": 0}, {"item": "pepsi", "quantity": 3, "price": 0}, {"item": "bread", "quantity": 1, "price": 0}]}

Sirf JSON — kuch aur mat likho."""
PURCHASE_PROMPT = """Tu ek Karyana shop ka assistant hai.
User batayega ke supplier se kya kharida.
Tujhe sirf JSON mein jawab dena hai — kuch aur mat likho.

Rules:
- supplier: supplier ka naam
- item: kya kharida — size/variant bhi include karo (jaise "coke 1.5l")
- quantity: kitna kharida
- rate: per unit price
- total_given: agar user ne total amount bataya ho toh woh yahan rakho, warna 0
- Agar rate clearly per unit lage toh rate mein rakho, total_given: 0
- Agar amount bada lage aur quantity se divide karna pade toh total_given mein rakho, rate: 0

Examples:
"Rehman se 24 Brite 260 mein liye" → {"supplier": "rehman", "item": "brite", "quantity": 24, "rate": 260, "total_given": 0}
"shah nawaz se 24 coke 1.5 liye 3940 mein" → {"supplier": "shah nawaz", "item": "coke 1.5l", "quantity": 24, "rate": 0, "total_given": 3940}
"48 Capstan 230 mein aaya" → {"supplier": "", "item": "capstan", "quantity": 48, "rate": 230, "total_given": 0}

Sirf JSON — kuch aur mat likho."""

def parse_purchase(user_input: str) -> dict:
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=200,
            temperature=0,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PURCHASE_PROMPT},
                {"role": "user", "content": user_input}
            ]
        )
        return _extract_json(response.choices[0].message.content)
    except json.JSONDecodeError:
        print("❌ AI ne galat format diya")
        return None
    except Exception as e:
        print(f"❌ AI error: {e}")
        return None

def parse_entry(user_input: str) -> dict:
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=200,
            temperature=0,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_input}
            ]
        )
        return _extract_json(response.choices[0].message.content)
    except json.JSONDecodeError:
        print("❌ AI ne galat format diya — dobara try karo")
        return None
    except Exception as e:
        print(f"❌ AI error: {e}")
        return None
def parse_multi_entry(user_input: str) -> dict:
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=500,
            temperature=0,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": MULTI_ITEM_PROMPT},
                {"role": "user", "content": user_input}
            ]
        )
        return _extract_json(response.choices[0].message.content)
    except json.JSONDecodeError:
        print("❌ AI ne galat format diya")
        return None
    except Exception as e:
        print(f"❌ AI error: {e}")
        return None
