# test_urdu.py — Urdu/Roman parser test (local only, delete kar sakte ho baad mein)
import sys
from ai_parser import parse_entry, parse_multi_entry, parse_purchase

# Test cases — Roman aur Urdu dono
tests = [
    ("Roman single",  "bilal ne 2 brite liya 200 mein",                parse_entry),
    ("Urdu single",   "بلال نے 2 برائٹ لیا 200 میں",                    parse_entry),
    ("Urdu payment",  "سلطان نے 1040 دیے",                               parse_entry),
    ("Urdu multi",    "بلال نے برائٹ 2 اور سرف ایکسل 1 لیا",            parse_multi_entry),
    ("Roman multi",   "ali ne brite 2 aur surf excel 1 liya",           parse_multi_entry),
    ("Urdu purchase", "رحمٰن سے 24 برائٹ 260 میں لیے",                   parse_purchase),
]

print("=" * 60)
for name, text, func in tests:
    print(f"\n[{name}]")
    print(f"  Input : {text}")
    result = func(text)
    print(f"  Output: {result}")
print("\n" + "=" * 60)