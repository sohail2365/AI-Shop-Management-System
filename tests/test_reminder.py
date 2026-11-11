# test_reminder.py — Reminder Agent manual test
"""
Chalao:
    python test_reminder.py

Yeh script:
1. 'test customer' ko database se dhoondta hai
2. Reminder Agent chalata hai
3. Result print karta hai
"""
import sys
from database import get_connection
from sqlalchemy import text

SHOP_ID = 1
CUSTOMER_NAME = "test customer"

print(f"\n{'='*60}")
print(f"Reminder Agent Test — Shop {SHOP_ID}")
print(f"{'='*60}\n")

# 1. Customer dhoondo
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
    print(f"❌ Customer '{CUSTOMER_NAME}' nahi mila shop {SHOP_ID} mein")
    sys.exit(1)

customer_id, name, phone = cust
print(f"✓ Customer mila: {name} (ID: {customer_id}, Phone: {phone})")

# 2. Current baaki dekho
with get_connection() as conn:
    baaki_row = conn.execute(
        text("""
            SELECT COALESCE(SUM(total), 0)
            FROM khaata
            WHERE customer_id = :cid AND deleted_at IS NULL
        """),
        {"cid": customer_id}
    ).fetchone()
    baaki = float(baaki_row[0]) if baaki_row else 0

print(f"✓ Current baaki: Rs. {baaki}")

if baaki <= 0:
    print("⚠️  Koi baaki nahi — reminder skip hoga")
    sys.exit(0)

# 3. Reminder Agent chalao
print(f"\n{'='*60}")
print("Reminder Agent chal raha hai...")
print(f"{'='*60}\n")

from agent.agents.reminder import handle_reminder_task

result = handle_reminder_task(SHOP_ID, {
    "customer_id": customer_id,
    "customer_name": name,
    "phone": phone,
    "baaki": baaki,
    "channel": "whatsapp",
    "scheduled_by": "test_reminder.py",
})

# 4. Result print
print(f"\n{'='*60}")
print("RESULT")
print(f"{'='*60}")
print(f"Status:        {result.get('status')}")
print(f"Customer type: {result.get('customer_type', '-')}")
print(f"Auto sent:     {result.get('auto_sent', False)}")
print(f"Latency:       {result.get('latency_ms', '?')} ms")
print(f"\nMessage:\n{'-'*60}")
print(result.get('message', '(koi message nahi)'))
print(f"{'-'*60}\n")

if result.get('link'):
    print(f"WhatsApp link: {result['link'][:100]}...")

if result.get('auto_sent'):
    print("\n✅ Message customer ko bhej diya gaya!")
else:
    print("\n⚠️  Auto-send OFF — message manually bhejna padega")