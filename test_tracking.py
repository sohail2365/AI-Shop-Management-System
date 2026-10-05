# test_tracking.py — Tracking Agent manual test
"""
Chalao: python test_tracking.py
"""
import sys
from database import get_connection
from sqlalchemy import text

SHOP_ID = 1
CUSTOMER_NAME = "test customer"

print(f"\n{'='*60}")
print(f"Tracking Agent Test — Shop {SHOP_ID}")
print(f"{'='*60}\n")

# Customer dhoondo
with get_connection() as conn:
    cust = conn.execute(
        text("""
            SELECT customer_id, name, phone
            FROM customers
            WHERE shop_id = :sid AND LOWER(name) = :n
        """),
        {"sid": SHOP_ID, "n": CUSTOMER_NAME.lower()}
    ).fetchone()

if not cust:
    print(f"❌ '{CUSTOMER_NAME}' nahi mila")
    sys.exit(1)

customer_id, name, phone = cust
print(f"✓ Customer: {name} (ID: {customer_id})")

# Intent detection test
from agent.agents.tracking import detect_tracking_intent

test_messages = [
    "mera hisaab batao",
    "kitna baaki hai",
    "baaki?",
    "kya haal hai",   # <-- yeh tracking nahi hai
    "2 brite bhej do",   # <-- yeh bhi nahi
]

print(f"\n{'='*60}")
print("Intent Detection Test")
print(f"{'='*60}")
for msg in test_messages:
    is_tracking = detect_tracking_intent(msg)
    marker = "✓" if is_tracking else "✗"
    print(f"  {marker} '{msg}' → {is_tracking}")

# Actual tracking query
print(f"\n{'='*60}")
print("Actual Tracking Query")
print(f"{'='*60}\n")

from agent.agents.tracking import handle_tracking_query

result = handle_tracking_query(SHOP_ID, customer_id, query="mera hisaab batao")

print(f"\n{'='*60}")
print("RESULT")
print(f"{'='*60}")
print(f"Status: {result.get('status')}")
print(f"Baaki:  Rs. {result.get('baaki', '-')}")
print(f"Latency: {result.get('latency_ms', '?')} ms")
print(f"\nMessage:\n{'-'*60}")
print(result.get('message', '(koi message nahi)'))
print(f"{'-'*60}\n")

if result.get('link'):
    print(f"WhatsApp link:\n{result['link'][:150]}...\n")