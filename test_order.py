# test_order.py — Order Agent conversation simulator
"""
Simulates a multi-turn WhatsApp order conversation.
Run: python test_order.py
"""
from database import get_connection
from sqlalchemy import text

SHOP_ID = 1
CUSTOMER_NAME = "test customer"

# Get customer
with get_connection() as conn:
    cust = conn.execute(
        text("SELECT customer_id, name, phone FROM customers WHERE shop_id = :sid AND LOWER(name) = :n"),
        {"sid": SHOP_ID, "n": CUSTOMER_NAME.lower()}
    ).fetchone()

if not cust:
    print(f"❌ Customer '{CUSTOMER_NAME}' nahi mila")
    exit(1)

customer_id, name, phone = cust
print(f"✓ Customer: {name} (ID: {customer_id})\n")

# Create/resume conversation
from agent import memory
conv_id = memory.start_or_resume_conversation(
    shop_id=SHOP_ID, channel="whatsapp",
    external_id=phone, customer_id=customer_id
)
print(f"✓ Conversation ID: {conv_id}\n")

from agent.agents.order import handle_order_message

with get_connection() as conn:
    conn.execute(
        text("UPDATE conversations SET state = state - 'order_agent' WHERE id = :cid"),
        {"cid": conv_id}
    )
    conn.commit()
    print("✓ State cleared\n")
# Simulated messages
conversation = [
    "2 brite aur 1 surf excel bhej do",   # Brite + Surf dono variants
    "3",                                    # Brite 500gm select
    "2",                                    # Surf Excel 500gm select
    "haan",                                 # confirm
    "udhaar",                               # payment
]

for i, msg in enumerate(conversation, 1):
    print(f"{'='*60}")
    print(f"CUSTOMER #{i}: {msg}")
    print(f"{'='*60}")

    result = handle_order_message(
        shop_id=SHOP_ID,
        customer_id=customer_id,
        conversation_id=conv_id,
        message=msg,
        customer_name=name,
        phone=phone,
    )

    print(f"\n🤖 AGENT:\n{result.get('reply')}\n")
    print(f"[Latency: {result.get('latency_ms')}ms, saved: {result.get('saved', False)}]\n")