# agent/tools/whatsapp_tools.py
from whatsapp import (
    build_whatsapp_payload, compose_entry_receipt,
    compose_payment_receipt, compose_reminder,
)
from worker.db_worker import write_audit
from logger import get_logger

log = get_logger("dukaan.tools.whatsapp")


def tool_send_whatsapp(shop_id: int, phone: str, message: str,
                       customer_id: int = None,
                       conversation_id: int = None) -> dict:
    """WhatsApp message bhejo — auto ya wa.me link."""
    try:
        payload = build_whatsapp_payload(phone, message, auto_send=True)
        write_audit(
            shop_id=shop_id, agent_name="tool.send_whatsapp",
            action="send_whatsapp", conversation_id=conversation_id,
            customer_id=customer_id,
            input_data={"phone": phone, "message": message},
            output_data={"auto_sent": payload.get("auto_sent"),
                         "phone_valid": payload.get("phone_valid")},
            success=True,
        )
        return payload
    except Exception as e:
        log.error(f"tool_send_whatsapp failed: {e}")
        return {"error": str(e)}


def tool_compose_receipt(shop_name: str, customer_name: str,
                         items: list, grand_total: float,
                         total_baaki: float) -> str:
    return compose_entry_receipt(shop_name, customer_name, items,
                                 grand_total, total_baaki)


def tool_compose_reminder(shop_name: str, customer_name: str,
                          baaki: float) -> str:
    return compose_reminder(shop_name, customer_name, baaki)