# agent/tools/inventory_tools.py
from inventory import get_item_price, get_variants
from logger import get_logger

log = get_logger("dukaan.tools.inventory")


def tool_find_item(shop_id: int, item_name: str) -> dict:
    """Item dhoondo — exact ya fuzzy. Multiple variants ho to sab."""
    try:
        variants = get_variants(item_name, shop_id)
        if not variants:
            single = get_item_price(item_name, shop_id)
            if single:
                return {"count": 1, "variants": [single], "match": single}
            return {"count": 0, "variants": [], "error": "not_found"}
        return {
            "count": len(variants),
            "variants": variants,
            "match": variants[0] if len(variants) == 1 else None,
        }
    except Exception as e:
        log.error(f"tool_find_item failed: {e}")
        return {"error": str(e)}


def tool_check_stock(shop_id: int, item_name: str) -> dict:
    """Kya item stock mein hai?"""
    result = tool_find_item(shop_id, item_name)
    if "error" in result and result["error"] == "not_found":
        return {"available": False, "reason": "not_in_inventory"}
    if result.get("match"):
        return {
            "available": result["match"]["stock"] > 0,
            "stock": result["match"]["stock"],
            "price": result["match"]["price"],
            "name": result["match"]["name"],
        }
    return {"available": False, "reason": "multiple_variants_choose_one",
            "variants": result.get("variants", [])}