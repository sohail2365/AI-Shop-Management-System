# agent/messages.py
"""
═══════════════════════════════════════════════════════════════
📝 CENTRAL MESSAGES FILE — Dukaan AI ke saare wordings yahan
═══════════════════════════════════════════════════════════════

Yeh file SINGLE SOURCE OF TRUTH hai. Yahan koi bhi wording badlo
aur poore system mein apply ho jayegi — reminder, order, tracking,
upsell, sab jagah.

Shop owner (client) bhi yeh file edit karke apni wording customize
kar sakta hai — developer ki zaroorat nahi.

LANGUAGE: Roman Urdu (default). Urdu script chahiye to alag file
banayenge.
═══════════════════════════════════════════════════════════════
"""

# =========================================================
# 🎨 STYLE D — Full customization
# =========================================================
#   1. Variety (har baar same wording nahi)
#   2. Tone-aware (customer type ke hisaab se)
#   3. Time-aware (din/date context)
# =========================================================


# =========================================================
# 📊 CUSTOMER TONE CATEGORIES
# =========================================================
# Agent customer ki history dekh kar decide karta hai kaunsi
# category hai, phir us hisaab se tone choose karta hai.

TONE_RULES = {
    "vip": {
        "description": "High-value, achha payment record — izzat se pesh aao",
        "conditions": {
            "min_lifetime_value": 10000,    # Rs. 10,000+ lifetime
            "min_payment_ratio": 0.7,        # 70%+ paid on time
        },
        "tone_hint": (
            "Customer VIP hai — humara sab se acha customer. "
            "Bohat izzat aur warmth se baat karo. Kabhi pressure mat daalo. "
            "'Aap' use karo, 'tum' bilkul nahi. Chhoti si narm yaad dehani."
        ),
    },
    "regular": {
        "description": "Normal customer, theek payment record",
        "conditions": {},
        "tone_hint": (
            "Normal, friendly reminder. Polite aur warm. "
            "Bina pressure ke yaad dilao."
        ),
    },
    "new": {
        "description": "Naya customer — pehli baar baaki hai",
        "conditions": {
            "max_lifetime_entries": 3,
        },
        "tone_hint": (
            "Yeh naya customer hai. Extra warm aur welcoming tone. "
            "Koi reminder jaisa feel na ho — sirf friendly update. "
            "Relationship build karo, paisa maangne ka feel na ho."
        ),
    },
    "chronic": {
        "description": "Baar baar late payment — thoda firm but polite",
        "conditions": {
            "min_reminders_sent": 2,        # 2+ reminders pehle bheje
            "min_days_pending": 30,          # 30+ din purana baaki
        },
        "tone_hint": (
            "Yeh customer baar baar late karta hai. Firm but respectful tone. "
            "Politely urgency dikhao — jaise 'kuch arsa ho gaya hai'. "
            "Kabhi rude nahi. Kabhi dhamki nahi. Business relationship "
            "maintain karo lekin clear raho ke payment pending hai."
        ),
    },
}


# =========================================================
# 🔤 GREETING VARIATIONS (Roman Urdu)
# =========================================================
# LLM in mein se koi ek chune — repetition avoid karne ke liye

GREETINGS = [
    "Salam {customer_name}!",
    "Assalam-o-alaikum {customer_name}!",
    "Salam {customer_name} bhai!",
    "Salam {customer_name} sahab!",
    "Salam {customer_name} ji!",
]


# =========================================================
# 💰 BAAKI MENTION — different phrasings
# =========================================================
# {amount} = "2,340", {days} = "15", {shop_name} = "Ali Store"

BAAKI_PHRASINGS = [
    "Aapka baaki Rs. {amount} hai.",
    "Aapka kul baaki Rs. {amount} ho gaya hai.",
    "Yaad dehani: aapka Rs. {amount} baaki hai.",
    "Aapke khaate mein Rs. {amount} baaki hai.",
    "Chhoti si yaad dehani — aapka baaki Rs. {amount} hai.",
]

BAAKI_PHRASINGS_CHRONIC = [
    "Aapka Rs. {amount} ka baaki kuch arse se pending hai.",
    "Yaad dehani — Rs. {amount} abhi tak baaki hai.",
    "Aapka baaki Rs. {amount} — kuch dinon se intezaar hai.",
]

BAAKI_PHRASINGS_NEW = [
    "Aapka pehla hisaab Rs. {amount} hai.",
    "Aapke khaate mein Rs. {amount} likha hai.",
]


# =========================================================
# ⏰ TIME-AWARE CONTEXT
# =========================================================
# Din ke hisaab se alag phrase

TIME_CONTEXT = {
    "fresh": [  # 0-7 din
        "",
        "Abhi koi jaldi nahi.",
        "Jab aasani ho.",
    ],
    "week": [   # 7-30 din
        "Pichle hafte se pending hai.",
        "Kuch din ho gaye hain.",
        "Thoda arsa ho gaya.",
    ],
    "month": [  # 30-60 din
        "Kuch arsa ho gaya hai.",
        "Kaafi din guzar gaye hain.",
        "Ab thoda wait ho gaya hai.",
    ],
    "old": [    # 60+ din
        "Kaafi arsa ho gaya hai.",
        "Bohat din ho gaye hain.",
        "Kuch mahine ho gaye.",
    ],
}


# =========================================================
# 🎯 CLOSING VARIATIONS
# =========================================================

CLOSINGS = [
    "Jab aasani ho, ada kar dein. Shukriya!",
    "Jab waqt mile, dukaan pe aa jayein. Shukriya!",
    "Allah Hafiz!",
    "Shukriya!",
    "Aap ka din acha guzre!",
]


# =========================================================
# ⚠️ FALLBACK MESSAGE (agar LLM fail ho jaye)
# =========================================================
# Yeh hardcoded hai — guarantee ke liye. LLM ke bina bhi reminder
# bhej sakte hain.

REMINDER_TEMPLATES = {

    # ─── NEW CUSTOMER — warm welcome, koi pressure nahi ───
    "new": [
        """Salam {name}!
Aapka pehla hisaab Rs. {amount} hai — koi jaldi nahi.
Jab aasani ho dukaan pe aa jayein. Aap ka swagat hai!
— {shop_name}""",

        """Assalam-o-alaikum {name}!
Aapke khaate mein Rs. {amount} likha hai.
Jab waqt mile, dukaan pe aa jayein.
— {shop_name}""",

        """Salam {name}!
{time_context}
Aapka Rs. {amount} baaki hai — koi jaldi nahi.
Jab aasani ho, ada kar dein. Shukriya!
— {shop_name}""",
    ],

    # ─── REGULAR — simple, friendly, warm ───
    "regular": [
        """Salam {name}!
Yaad dehani: aapka baaki Rs. {amount} hai.
Jab aasani ho, dukaan pe aa jayein. Shukriya!
— {shop_name}""",

        """Salam {name}!
{time_context}
Aapka Rs. {amount} baaki hai.
Jab waqt mile, ada kar dein. Shukriya!
— {shop_name}""",

        """Assalam-o-alaikum {name}!
Aapka Rs. {amount} ka hisaab pending hai.
Jab aasani ho, dukaan pe aa jayein.
— {shop_name}""",

        """Salam {name}!
{time_context}
Aapka baaki Rs. {amount} hai.
Jab sukoon mile, ada kar dein. Allah Hafiz!
— {shop_name}""",
    ],

    # ─── VIP — soft, izzat, no pressure ───
    "vip": [
        """Salam {name} sahab!
Aapka Rs. {amount} baaki hai — koi jaldi nahi.
Jab aasani ho, ada kar dein. Aap humare achhe customer hain.
— {shop_name}""",

        """Assalam-o-alaikum {name} sahab!
{time_context}
Aapka Rs. {amount} baaki hai.
Jab waqt mile, dukaan pe aa jayein. Shukriya!
— {shop_name}""",

        """Salam {name} sahab!
Chhoti si yaad dehani — aapka Rs. {amount} baaki hai.
Jab aasani ho, ada kar dein. Koi jaldi nahi.
— {shop_name}""",
    ],

    # ─── CHRONIC — firm but polite ───
    "chronic": [
        """Salam {name}!
{time_context}
Aapka Rs. {amount} ka baaki pending hai.
Jab aasani ho, ada kar dein taake hisaab saaf rahe.
— {shop_name}""",

        """Assalam-o-alaikum {name}!
Aapka Rs. {amount} kuch arse se pending hai.
Barah-e-karam jab waqt mile, ada kar dein.
— {shop_name}""",

        """Salam {name}!
{time_context}
Aapka Rs. {amount} baaki hai — pichli baar bhi yaad dilaya tha.
Jab aasani ho, dukaan pe aa jayein.
— {shop_name}""",
    ],
}
# =========================================================
# 📊 TRACKING AGENT TEMPLATES
# =========================================================

# ─── Full khaata summary (jab customer ne poocha) ───
TRACKING_HEADER = """Salam {name}!
Aapka kul baaki: *Rs. {baaki}*"""

TRACKING_ENTRIES_HEADER = "\nLast {count} entries:"

TRACKING_ENTRY_LINE = "- {date}: {item} x{qty} = Rs. {amount}{paid_marker}"

TRACKING_FOOTER = "\nKoi aur cheez chahiye?\n— {shop_name}"


# ─── Special cases ───
TRACKING_NO_BAAKI = """Salam {name}!
Aapka koi baaki nahi hai — hisaab saaf hai. 
Shukriya!
— {shop_name}"""

TRACKING_CREDIT = """Salam {name}!
Aapka koi baaki nahi — balke *Rs. {amount} credit* hai dukaan par.
Agli kharidari mein adjust kar lenge.
— {shop_name}"""

TRACKING_NO_RECORD = """Salam!
Yeh number humare records mein nahi mila.
Kya aap apna naam ya dukaan ka naam bata sakte hain?
— {shop_name}"""

TRACKING_NO_ENTRIES = """Salam {name}!
Aapka abhi tak koi khata record nahi hai.
Jab pehli kharidari karenge, yahan hisaab dikhega.
— {shop_name}"""


# ─── Intent detection keywords (customer ne kya bola) ───
TRACKING_INTENT_KEYWORDS = [
    "baaki", "baqi", "baki", "hisaab", "hisab", "khata", "khaata",
    "kitna", "kitne", "balance", "statement", "total",
    "mera kya", "mera hisaab", "mera baaki",
]
# =========================================================
# 🧠 LLM SYSTEM PROMPT — Reminder Agent
# =========================================================
# Yeh prompt LLM ko diya jayega. Ismein tone rules + examples
# hain. Time-aware aur variety dono handle karta hai.

REMINDER_SYSTEM_PROMPT = """Tu Dukaan AI ka Reminder Agent hai — Pakistani karyana dukaan ke liye.

Tera kaam: customer ko unka baaki politely yaad dilana, ek chhota Roman Urdu WhatsApp message bhej kar.

═══ RULES ═══
1. Roman Urdu (English words allowed: Rs., WhatsApp, shop)
2. **3-4 lines** — bas itna. Isse lamba nahi.
3. Structure: Greeting → Baaki amount → Ek warm closing. Bas.
4. **Kabhi same baat do baar mat likho.** Har line alag idea ho.
5. Specific amount: "Rs. 520" (commas ke saath)
6. Tone: VIP = soft, chronic = firm but polite, new = warm welcome
7. NEVER: dhamki, gaali, "last warning", "legal action"
8. NEVER: pressure
9. Emoji: 0-1 (optional)
10. English sentences mat likho — Roman Urdu hi

═══ STRUCTURE (follow karo) ═══
Line 1: Salam {name}! (ya Assalam-o-alaikum {name}!)
Line 2: Aapka Rs. {amount} baaki hai. (ya similar — ek line)
Line 3: Ek warm closing — jaise "Jab aasani ho, ada kar dein. Shukriya!"
Bas. Iske baad kuch nahi.

═══ EXAMPLES (yeh FOLLOW karo) ═══

VIP:
"Salam Ali sahab!
Aapka Rs. 2,340 baaki hai — koi jaldi nahi.
Jab aasani ho, ada kar dein. Shukriya!"

Regular:
"Salam Bilal!
Aapka Rs. 850 baaki hai.
Jab waqt mile, dukaan pe aa jayein. Shukriya!"

Chronic:
"Salam Kashif!
Aapka Rs. 4,500 kuch arse se pending hai.
Jab aasani ho, ada kar dein taake hisaab saaf rahe."

New:
"Salam Saad!
Aapka Rs. 260 baaki hai — koi jaldi nahi.
Jab aasani ho dukaan pe aa jayein. Shukriya!"

═══ GALAT EXAMPLES (yeh MAT likho) ═══
❌ "Jab aasani ho, dukaan pe aa jayein. Jab waqt mile, dukaan pe aa jayein."
   (repetition — same baat do baar)

❌ "Aapko yeh message isliye mil raha hai ke aapka khaata pending hai aur hume..."
   (bahut lamba — 3-4 lines se zyada)

═══ OUTPUT ═══
Sirf WhatsApp message. Koi JSON, markdown, ya explanation nahi.
"""


# =========================================================
# 🛑 STOP / OPT-OUT (already existing, yahan bhi rakhe)
# =========================================================
STOP_KEYWORDS = [
    "stop", "mat bhejo", "band karo", "pareshan mat karo",
    "unsubscribe", "opt out", "no more messages",
    "message na karein", "tang mat karo",
]

STOP_ACK_MESSAGE = """Theek hai — hum aapko aur reminders nahi bhejenge.
Kabhi bhi dukaan pe aayein, khushi hogi.
Shukriya!"""


# =========================================================
# 📞 HUMAN HANDOFF MESSAGE
# =========================================================
HUMAN_HANDOFF_MESSAGE = """Salam! Aapka message hum tak pohnch gaya hai.
Dukaan ka malik thodi der mein khud reply karega — shukriya intezaar ke liye."""


# =========================================================
# 🛠️ HELPER — pick random
# =========================================================
import random

def pick_greeting(customer_name: str, recent: list = None) -> str:
    """Random greeting chuno, purane avoid karke."""
    recent = recent or []
    available = [g for g in GREETINGS if g.format(customer_name=customer_name) not in recent]
    if not available:
        available = GREETINGS
    return random.choice(available).format(customer_name=customer_name)


def pick_baaki_phrase(amount: str, customer_type: str = "regular") -> str:
    """Baaki amount ka phrase chuno — type ke hisaab se."""
    if customer_type == "chronic":
        pool = BAAKI_PHRASINGS_CHRONIC
    elif customer_type == "new":
        pool = BAAKI_PHRASINGS_NEW
    else:
        pool = BAAKI_PHRASINGS
    return random.choice(pool).format(amount=amount)


def pick_time_context(days_pending: int) -> str:
    """Din ke hisaab se context phrase."""
    if days_pending < 7:
        pool = TIME_CONTEXT["fresh"]
    elif days_pending < 30:
        pool = TIME_CONTEXT["week"]
    elif days_pending < 60:
        pool = TIME_CONTEXT["month"]
    else:
        pool = TIME_CONTEXT["old"]
    return random.choice(pool)


def pick_closing() -> str:
    return random.choice(CLOSINGS)


# =========================================================
# 🛒 ORDER AGENT TEMPLATES + KEYWORDS
# =========================================================

# ─── Confirmation keywords (short message match) ───
ORDER_YES_KEYWORDS = [
    "haan", "han", "ha", "ji", "yes", "yeah", "yep", "ok", "okay",
    "theek", "thik", "theek hai", "sahi", "confirm", "save",
    "bhejo", "bhej do", "chalega", "chal", "g", "sahi hai",
]
ORDER_NO_KEYWORDS = [
    "nahi", "na", "no", "nope", "cancel", "rehne", "rehne do",
    "chhoro", "chhod", "chhod do", "mat", "abhi nahi", "band", "band karo",
]
ORDER_UDHAAR_KEYWORDS = [
    "udhaar", "udhar", "udhar", "baaki", "khaata", "khata",
    "baad", "baad mein", "baad me", "likh lo",
]
ORDER_CASH_KEYWORDS = [
    "cash", "naqad", "naqd", "abhi", "spot", "paisa", "paise",
    "foran", "turant", "aaj", "now",
]
ORDER_DELETE_KEYWORDS = [
    "delete", "delete karo", "hata", "hatao", "remove", "nikal",
    "cancel karo", "wapas karo", "undo",
]

# ─── Messages ───
ORDER_PARSE_FAILED = """Salam!
Aapka message samajh nahi aaya.
Jaise likhein: "2 brite, 1 surf excel bhej do"
— {shop_name}"""

ORDER_CUSTOMER_UNKNOWN = """Salam!
Yeh number humare records mein nahi mila.
Pehle dukaan pe aake apna record banwa lein.
— {shop_name}"""

ORDER_NO_ITEMS_FOUND = """Salam {name}!
Aapke message mein koi item nahi mila.
Jaise likhein: "2 brite, 1 surf excel bhej do"
— {shop_name}"""

ORDER_CONFIRMATION_HEADER = """Salam {name}!
Yeh order confirm karein:

{items_block}

*Total: Rs. {grand_total}*

Confirm? (haan / nahi)"""

ORDER_ITEM_LINE = "• {item} x{qty} = Rs. {total}"

ORDER_MANUAL_PRICE_WARNING = "\n\n⚠️ Kuch items ka price manual daalna padega — dukaan pe confirm ho jayega."

ORDER_ASK_PAYMENT = """Theek hai!

*Udhaar* ya *cash*?"""

ORDER_PAYMENT_INVALID = """Samajh nahi aaya — *udhaar* ya *cash*?"""

ORDER_UDHAAR_SAVED = """Salam {name}!
Order save ho gaya (udhaar):

{items_block}

*Total: Rs. {grand_total}*
*Ab kul baaki: Rs. {new_baaki}*

Shukriya!
— {shop_name}"""

ORDER_CASH_SAVED = """Salam {name}!
Order save ho gaya (cash):

{items_block}

*Total: Rs. {grand_total}*
Cash received — shukriya!
— {shop_name}"""

ORDER_CANCELLED = """Theek hai — order cancel kar diya.
Jab chahein naya order bhejein.
— {shop_name}"""

ORDER_MODIFIED = """Theek hai — update kar diya:

{items_block}

*Total: Rs. {grand_total}*

Confirm? (haan / nahi)"""

ORDER_TIMEOUT = """Pichla order 10 minute mein confirm nahi hua — cancel kar diya.
Naya order bhejein jab chahein.
— {shop_name}"""

ORDER_DELETE_DONE = """Theek hai — pichla order delete kar diya.
Ab naya order bhejein.
— {shop_name}"""

ORDER_DELETE_NOTHING = """Koi pichla order nahi mila delete karne ke liye.
Naya order bhejein.
— {shop_name}"""

# =========================================================
# 🎯 VARIANT SELECTION (Order Agent)
# =========================================================

ORDER_VARIANT_ASK = """Salam {name}!
"{item}" ke multiple options hain — kaunsa chahiye?

{variants_block}

Reply karein: {options_hint}"""

ORDER_VARIANT_LINE = "{num}. {name} — Rs. {price}"

ORDER_VARIANT_INVALID = """Samajh nahi aaya — sirf number reply karein: {nums}"""

ORDER_VARIANT_SELECTED = """Theek hai — {name} selected.

{items_block}

*Total: Rs. {grand_total}*

Confirm? (haan / nahi)"""