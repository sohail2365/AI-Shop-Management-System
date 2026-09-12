from fastapi import FastAPI, Header, Request, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel, field_validator
from typing import Optional, List
from sqlalchemy import text
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from customers import add_khaata_entry, add_customer
from inventory import (get_item_price, get_item_by_id, update_stock, update_stock_by_id,
                       update_stock_purchase, get_variants, add_item, get_all_items,
                       edit_item, delete_item)
from ai_parser import parse_entry, parse_purchase, parse_multi_entry
from voice import transcribe_audio
from database import setup_database, get_connection
from auth import verify_token, register_shop, login_shop, reset_password
from whatsapp import (build_whatsapp_payload, compose_entry_receipt,
                      compose_payment_receipt, compose_reminder, AUTO_SEND_ENABLED)
from logger import get_logger

log = get_logger("dukaan.server")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Dukaan AI", version="1.1")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_database()

# ---- Friendly validation error handler ----
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Pydantic ke technical errors ko user-friendly Roman Urdu mein badal do."""
    errors = exc.errors()
    if errors:
        first = errors[0]
        msg = first.get("msg", "Input galat hai")
        # Pydantic v2 "Value error, X" prefix hata do
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        return JSONResponse(status_code=422, content={"error": msg})
    return JSONResponse(status_code=422, content={"error": "Input galat hai"})

# ---- Models ----
def _clean_name(v: str, field: str = "Name") -> str:
    if not v or not v.strip():
        raise ValueError(f"{field} khali nahi ho sakta")
    v = v.strip()
    if len(v) > 100:
        raise ValueError(f"{field} bahut lamba hai (max 100)")
    return v

def _clean_phone(v: str) -> str:
    if not v:
        return "unknown"
    v = v.strip()
    if v.lower() == "unknown" or v == "":
        return "unknown"
    # Digits, +, spaces, dashes allowed
    digits = "".join(c for c in v if c.isdigit())
    if len(digits) < 10:
        raise ValueError("Phone number kam az kam 10 digits ka hona chahiye (ya 'unknown' likho)")
    return v

class AuthRequest(BaseModel):
    username: str
    password: str
    shop_name: str = ""

    @field_validator("username")
    @classmethod
    def _u(cls, v): return v.strip().lower()

class ResetRequest(BaseModel):
    username: str
    shop_name: str
    new_password: str

class EntryRequest(BaseModel):
    text: str
    manual_price: float = 0

class MultiEntryRequest(BaseModel):
    text: str
    customer_override: str = ""


class SingleEntrySaveRequest(BaseModel):
    text: str = ""
    customer: str = ""
    item: str = ""
    quantity: float = 1
    price: float = 0
    manual_price: float = 0
    inventory_id: Optional[int] = None

    @field_validator("quantity")
    @classmethod
    def _q(cls, v):
        if v <= 0:
            raise ValueError("Quantity 0 se zyada honi chahiye")
        if v > 100000:
            raise ValueError("Quantity itni zyada nahi ho sakti (max 100000)")
        return v

    @field_validator("price")
    @classmethod
    def _p(cls, v):
        # 0 valid hai kyunki payment mein price alag handle hoti hai (negative bhi)
        if abs(v) > 100000000:
            raise ValueError("Price bahut zyada hai")
        return v

class MultiItemSave(BaseModel):
    item: str
    quantity: float
    price: float
    inventory_id: Optional[int] = None
    found: bool = False

    @field_validator("item")
    @classmethod
    def _i(cls, v): return _clean_name(v, "Item")

    @field_validator("quantity")
    @classmethod
    def _q(cls, v):
        if v <= 0:
            raise ValueError("Quantity 0 se zyada honi chahiye")
        return v

class MultiEntrySaveRequest(BaseModel):
    text: str = ""
    customer: str = ""
    customer_override: str = ""
    items: List[MultiItemSave] = []

class CustomerRequest(BaseModel):
    name: str
    phone: str

    @field_validator("name")
    @classmethod
    def _n(cls, v): return _clean_name(v, "Customer name")

    @field_validator("phone")
    @classmethod
    def _ph(cls, v): return _clean_phone(v)

class CustomerEditRequest(BaseModel):
    old_name: str
    new_name: str
    new_phone: str

    @field_validator("new_name")
    @classmethod
    def _n(cls, v): return _clean_name(v, "Customer name")

    @field_validator("new_phone")
    @classmethod
    def _ph(cls, v): return _clean_phone(v)

class EntryEditRequest(BaseModel):
    entry_id: int
    item_name: str
    quantity: float
    price_per_item: float
    date: str

class PurchaseRequest(BaseModel):
    text: str

class PurchaseEditRequest(BaseModel):
    purchase_id: int
    supplier_name: str
    item_name: str
    quantity: float
    purchase_rate: float
    date: str

class InventoryItemRequest(BaseModel):
    item_name: str
    sale_price: float
    purchase_rate: float = 0
    stock: float = 0
    category: str = ""
    reorder_level: int = 5

class InventoryEditRequest(BaseModel):
    item_id: int
    item_name: str
    sale_price: float
    purchase_rate: float = 0
    stock: float = 0
    category: str = ""
    reorder_level: int = 5

# ---- Auth Helper ----
def get_shop_id(authorization: Optional[str] = None) -> Optional[int]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ")[1]
    payload = verify_token(token)
    if not payload:
        return None
    return payload.get("shop_id")

def get_customer_phone_and_baaki(customer_name: str, shop_id: int):
    """Customer ka phone + current total baaki nikalta hai (WhatsApp ke liye)."""
    with get_connection() as conn:
        row = conn.execute(
            text("""
                SELECT c.customer_id, c.phone, COALESCE(SUM(k.total), 0)
                FROM customers c
                LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                WHERE LOWER(c.name) = :name AND c.shop_id = :shop_id
                GROUP BY c.customer_id, c.phone
            """),
            {"name": customer_name.lower().strip(), "shop_id": shop_id}
        ).fetchone()
    if not row:
        return None, 0
    return row[1], row[2]

def get_shop_name(shop_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute(
            text("SELECT shop_name FROM shops WHERE shop_id = :sid"),
            {"sid": shop_id}
        ).fetchone()
    return row[0] if row else "Dukaan AI"

# ---- Home ----
@app.get("/")
def home():
    return {"message": "Dukaan AI server chal raha hai!"}

@app.get("/ui")
def serve_ui():
    html_content = Path("index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)

# ---- Auth ----
@app.post("/auth/register")
@limiter.limit("5/hour")
def register(request: Request, req: AuthRequest):
    if not req.shop_name:
        return {"error": "Shop name zaroori hai"}
    result = register_shop(req.username, req.password, req.shop_name)
    if result.get("success"):
        log.info(f"Register success: {req.username} from {get_remote_address(request)}")
    else:
        log.warning(f"Register failed: {req.username} — {result.get('error')}")
    return result

@app.post("/auth/login")
@limiter.limit("10/minute")
def login(request: Request, req: AuthRequest):
    result = login_shop(req.username, req.password)
    if result.get("success"):
        log.info(f"Login success: {req.username}")
    else:
        log.warning(f"Login failed: {req.username} from {get_remote_address(request)}")
    return result

@app.post("/auth/reset-password")
@limiter.limit("3/hour")
def reset_pwd(request: Request, req: ResetRequest):
    result = reset_password(req.username, req.shop_name, req.new_password)
    if result.get("success"):
        log.info(f"Password reset: {req.username}")
    else:
        log.warning(f"Password reset failed: {req.username} from {get_remote_address(request)}")
    return result

# ---- Entry ----
@app.post("/entry/parse")
def parse_natural_entry(req: EntryRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}

    data = parse_entry(req.text)
    if not data:
        return {"error": "AI parse nahi kar saka"}
    log.debug(f"AI parsed: {data}")

    if not data.get("price"):
        item = data.get("item", "")
        if item:
            inv_item = get_item_price(item, shop_id)
            if inv_item:
                data["price"] = inv_item["price"]
                data["stock"] = inv_item["stock"]
                data["inventory_id"] = inv_item["id"]
                data["from_sheet"] = True
            else:
                data["manual"] = True
                data["from_sheet"] = False
        else:
            data["manual"] = True
            data["from_sheet"] = False
    return data

@app.post("/entry/voice-parse")
async def parse_voice_entry(audio: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    """
    Voice se entry parse karta hai. Flow: audio -> Whisper transcription -> parse_entry()
    Response shape /entry/parse jaisa hi hai, bas extra "transcript" field ke saath
    taake frontend user ko dikha sake "yeh suna gaya".
    """
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}

    audio_bytes = await audio.read()
    result = transcribe_audio(audio_bytes, filename=audio.filename or "voice.webm")

    if result["error"]:
        return {"error": result["error"]}

    transcript = result["text"]
    log.debug(f"Voice transcript: {transcript}")

    # Safety net: agar Whisper phir bhi Urdu/Arabic script mein de de (rare
    # edge case, language="en" force karne ke bawajood), to saaf warning do
    # — chup-chaap fail hone ya galat item match hone se behtar hai.
    if any('\u0600' <= ch <= '\u06FF' for ch in transcript):
        return {
            "error": "Awaaz Urdu script mein transcribe hui — dobara saaf bolo ya English/Roman mein likh kar entry karo",
            "transcript": transcript
        }

    data = parse_entry(transcript)
    if not data:
        return {"error": "AI parse nahi kar saka", "transcript": transcript}

    # Same inventory-matching logic as /entry/parse — consistent behavior
    if not data.get("price"):
        item = data.get("item", "")
        if item:
            inv_item = get_item_price(item, shop_id)
            if inv_item:
                data["price"] = inv_item["price"]
                data["stock"] = inv_item["stock"]
                data["inventory_id"] = inv_item["id"]
                data["from_sheet"] = True
            else:
                data["manual"] = True
                data["from_sheet"] = False
        else:
            data["manual"] = True
            data["from_sheet"] = False

    data["transcript"] = transcript
    return data


@app.post("/entry/save")
def save_entry(req: SingleEntrySaveRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}

    if req.customer or req.item:
        data = {
            "customer": req.customer.strip(),
            "item": req.item.strip(),
            "quantity": req.quantity,
            "price": req.manual_price if req.manual_price > 0 else req.price,
            "inventory_id": req.inventory_id,
        }
    else:
        data = parse_entry(req.text)
        if not data:
            return {"error": "Parse nahi hua"}
        if req.manual_price > 0:
            data["price"] = req.manual_price

    if not data.get("customer"):
        return {"error": "Customer missing hai"}

    if data.get("item") == "PAYMENT":
        from customers import get_customer
        customer = get_customer(data["customer"], shop_id)
        if not customer:
            return {"error": f"'{data['customer']}' nahi mila"}
        with get_connection() as conn:
            conn.execute(
                text("""
                    INSERT INTO khaata (customer_id, item_name, quantity, price_per_item, total)
                    VALUES (:cid, 'PAYMENT', 1, :price, :neg_price)
                """),
                {"cid": customer[0], "price": data["price"], "neg_price": -data["price"]}
            )
            conn.commit()
        log.info(f"Payment: {data['customer']} Rs.{data['price']} shop={shop_id}")
        phone, baaki = get_customer_phone_and_baaki(data["customer"], shop_id)
        wa_msg = compose_payment_receipt(get_shop_name(shop_id), data["customer"], data["price"], baaki)
        return {"success": True, "type": "payment", "data": data,
                "whatsapp": build_whatsapp_payload(phone, wa_msg)}

    if not data.get("item"):
        return {"error": "Item missing hai"}

    if data.get("inventory_id"):
        inv_item = get_item_by_id(data["inventory_id"], shop_id)
        if not inv_item:
            return {"error": "Selected inventory item nahi mila"}
        data["item"] = inv_item["name"]
        if not data.get("price"):
            data["price"] = inv_item["price"]
    elif not data.get("price"):
        inv_item = get_item_price(data["item"], shop_id)
        if inv_item:
            data["inventory_id"] = inv_item["id"]
            data["item"] = inv_item["name"]
            data["price"] = inv_item["price"]

    if not data.get("price") or data["price"] <= 0:
        return {"error": "Price missing hai"}

    success = add_khaata_entry(
        data["customer"], data["item"], data["quantity"], data["price"], shop_id
    )

    if success:
        if data.get("inventory_id"):
            update_stock_by_id(data["inventory_id"], data["quantity"], shop_id)
        elif req.manual_price == 0:
            update_stock(data["item"], data["quantity"], shop_id)

    entry_total = round(data["quantity"] * data["price"], 2)
    phone, baaki = get_customer_phone_and_baaki(data["customer"], shop_id)
    wa_msg = compose_entry_receipt(
        get_shop_name(shop_id), data["customer"],
        [{"item": data["item"], "quantity": data["quantity"],
          "price": data["price"], "total": entry_total}],
        entry_total, baaki
    )
    return {"success": True, "type": "udhaar", "data": data,
            "whatsapp": build_whatsapp_payload(phone, wa_msg)}

@app.put("/entry/edit")
def edit_entry(req: EntryEditRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        entry = conn.execute(
            text("""
                SELECT k.* FROM khaata k
                JOIN customers c ON c.customer_id = k.customer_id
                WHERE k.id = :eid AND c.shop_id = :shop_id
            """),
            {"eid": req.entry_id, "shop_id": shop_id}
        ).fetchone()
        if not entry:
            return {"error": f"Entry #{req.entry_id} nahi mili"}
        total = round(req.quantity * req.price_per_item, 2)
        conn.execute(
            text("""
                UPDATE khaata SET item_name=:item_name, quantity=:qty, price_per_item=:price, total=:total, date=:date
                WHERE id=:eid AND customer_id IN (SELECT customer_id FROM customers WHERE shop_id=:shop_id)
            """),
            {"item_name": req.item_name.lower(), "qty": req.quantity, "price": req.price_per_item,
             "total": total, "date": req.date, "eid": req.entry_id, "shop_id": shop_id}
        )
        conn.commit()
    return {"success": True}

@app.delete("/entry/{entry_id}")
def delete_entry(entry_id: int, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        entry = conn.execute(
            text("""
                SELECT k.* FROM khaata k
                JOIN customers c ON c.customer_id = k.customer_id
                WHERE k.id = :eid AND c.shop_id = :shop_id
            """),
            {"eid": entry_id, "shop_id": shop_id}
        ).fetchone()
        if not entry:
            return {"error": f"Entry #{entry_id} nahi mili"}
        conn.execute(
            text("""
                UPDATE khaata SET deleted_at = NOW()
                WHERE id = :eid AND customer_id IN (SELECT customer_id FROM customers WHERE shop_id = :shop_id)
            """),
            {"eid": entry_id, "shop_id": shop_id}
        )
        log.info(f"Entry soft-deleted: id={entry_id}, shop={shop_id}")
        conn.commit()
    return {"success": True}

# ---- Khaata ----
@app.get("/khaata/{customer_name}")
def get_khaata(customer_name: str, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        customer = conn.execute(
            text("SELECT * FROM customers WHERE LOWER(name) = :name AND shop_id = :shop_id"),
            {"name": customer_name.lower(), "shop_id": shop_id}
        ).fetchone()
        if not customer:
            return {"error": f"{customer_name} nahi mila"}
        rows = conn.execute(
            text("""
                SELECT id, date, item_name, quantity, price_per_item, total
                FROM khaata WHERE customer_id = :cid AND deleted_at IS NULL ORDER BY date ASC
            """),
            {"cid": customer[0]}
        ).fetchall()
        total = conn.execute(
            text("SELECT SUM(total) FROM khaata WHERE customer_id = :cid AND deleted_at IS NULL"),
            {"cid": customer[0]}
        ).fetchone()[0] or 0
    return {
        "customer": customer_name,
        "entries": [
            {"id": r[0], "date": str(r[1]), "item": r[2], "quantity": r[3], "price": r[4], "total": r[5]}
            for r in rows
        ],
        "total_baaki": total
    }

# ---- Customers ----
@app.post("/customer/add")
def new_customer(req: CustomerRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    add_customer(req.name, req.phone, shop_id)
    return {"success": True, "customer": req.name}

@app.put("/customer/edit")
def edit_customer(req: CustomerEditRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    from customers import get_customer
    customer = get_customer(req.old_name, shop_id)
    if not customer:
        return {"error": f"'{req.old_name}' nahi mila"}
    try:
        with get_connection() as conn:
            conn.execute(
                text("UPDATE customers SET name=:name, phone=:phone WHERE customer_id=:cid AND shop_id=:shop_id"),
                {"name": req.new_name.lower().strip(), "phone": req.new_phone.strip(),
                 "cid": customer[0], "shop_id": shop_id}
            )
            conn.commit()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/customer/{customer_name}")
def delete_customer(customer_name: str, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    from customers import get_customer
    customer = get_customer(customer_name, shop_id)
    if not customer:
        return {"error": f"'{customer_name}' nahi mila"}
    # Whole customer delete = permanent (confirm dialog already warns user).
    # Hard delete khaata (active + trashed both) to avoid FK constraint issue.
    with get_connection() as conn:
        deleted = conn.execute(
            text("DELETE FROM khaata WHERE customer_id = :cid"),
            {"cid": customer[0]}
        )
        conn.execute(
            text("DELETE FROM customers WHERE customer_id = :cid AND shop_id = :shop_id"),
            {"cid": customer[0], "shop_id": shop_id}
        )
        conn.commit()
    log.info(f"Customer deleted: {customer_name} (shop={shop_id}, khaata rows removed)")
    return {"success": True}

@app.get("/customers/all")
def all_customers(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        customers = conn.execute(
            text("""
                SELECT c.name, c.phone,
                       COALESCE(SUM(k.total), 0) as total_baaki,
                       COUNT(k.id) as total_entries
                FROM customers c
                LEFT JOIN khaata k ON c.customer_id = k.customer_id AND k.deleted_at IS NULL
                WHERE c.shop_id = :shop_id
                GROUP BY c.customer_id, c.name, c.phone ORDER BY total_baaki DESC
            """),
            {"shop_id": shop_id}
        ).fetchall()
    return {
        "customers": [
            {"name": r[0].title(), "phone": r[1], "total_baaki": r[2], "total_entries": r[3]}
            for r in customers
        ],
        "total_customers": len(customers),
        "grand_total": sum(r[2] for r in customers)
    }

# ---- Inventory ----
@app.get("/inventory/all")
def get_inventory(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    items = get_all_items(shop_id)
    low_stock = [i for i in items if i["low_stock"]]
    return {"items": items, "total": len(items), "low_stock_count": len(low_stock)}

@app.post("/inventory/add")
def add_inventory_item(req: InventoryItemRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    return add_item(shop_id, req.item_name, req.sale_price,
                    req.purchase_rate, req.stock, req.category, req.reorder_level)

@app.put("/inventory/edit")
def edit_inventory_item(req: InventoryEditRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    return edit_item(req.item_id, shop_id, req.item_name, req.sale_price,
                     req.purchase_rate, req.stock, req.category, req.reorder_level)

@app.delete("/inventory/item/{item_id}")
def delete_inventory_item(item_id: int, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    return delete_item(item_id, shop_id)

@app.get("/inventory/variants/{keyword}")
def get_item_variants(keyword: str, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    variants = get_variants(keyword, shop_id)
    return {"variants": variants, "count": len(variants)}

@app.get("/inventory/{item_name}")
def check_inventory(item_name: str, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    # Pehle sab variants dhundo — agar multiple mile toh sab wapas karo,
    # ek hi ho toh usko convenience ke liye "match" field mein bhi rakh do
    variants = get_variants(item_name, shop_id)
    if not variants:
        # Fallback: fuzzy single lookup
        result = get_item_price(item_name, shop_id)
        if result:
            return {"variants": [result], "count": 1, "match": result}
        return {"error": f"{item_name} inventory mein nahi mila"}
    return {
        "variants": variants,
        "count": len(variants),
        "match": variants[0] if len(variants) == 1 else None,
    }

# ---- Purchase ----
@app.post("/purchase/parse")
def parse_purchase_entry(req: PurchaseRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    data = parse_purchase(req.text)
    if not data:
        return {"error": "AI parse nahi kar saka"}
    if data.get("total_given", 0) > 0 and data.get("quantity", 0) > 0:
        data["rate"] = round(data["total_given"] / data["quantity"], 2)
        data["auto_divided"] = True
    else:
        data["auto_divided"] = False
    log.debug(f"Purchase parsed: {data}")
    inv_item = get_item_price(data.get("item", ""), shop_id)
    if inv_item:
        data["found_in_sheet"] = True
        data["current_stock"] = inv_item["stock"]
        data["sale_price"] = inv_item["price"]
    else:
        data["found_in_sheet"] = False
    return data

@app.post("/purchase/save")
def save_purchase(req: PurchaseRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    data = parse_purchase(req.text)
    if not data:
        return {"error": "Parse nahi hua"}
    if not data.get("item"):
        return {"error": "Item missing hai"}
    if data.get("total_given", 0) > 0 and data.get("quantity", 0) > 0:
        data["rate"] = round(data["total_given"] / data["quantity"], 2)
    if not data.get("rate"):
        return {"error": "Rate missing hai"}
    total_cost = round(data["quantity"] * data["rate"], 2)
    with get_connection() as conn:
        conn.execute(
            text("""
                INSERT INTO purchases (shop_id, supplier_name, item_name, quantity, purchase_rate, total_cost)
                VALUES (:shop_id, :supplier, :item, :qty, :rate, :total)
            """),
            {"shop_id": shop_id, "supplier": data.get("supplier", "unknown"),
             "item": data["item"].lower(), "qty": data["quantity"],
             "rate": data["rate"], "total": total_cost}
        )
        conn.commit()
    update_stock_purchase(data["item"], data["quantity"], shop_id)
    log.info(f"Purchase: {data['item']} x{data['quantity']} Rs.{total_cost} shop={shop_id}")
    return {"success": True, "data": data, "total_cost": total_cost}

@app.put("/purchase/edit")
def edit_purchase(req: PurchaseEditRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        entry = conn.execute(
            text("SELECT * FROM purchases WHERE id = :pid AND shop_id = :shop_id"),
            {"pid": req.purchase_id, "shop_id": shop_id}
        ).fetchone()
        if not entry:
            return {"error": f"Purchase #{req.purchase_id} nahi mila"}
        total_cost = round(req.quantity * req.purchase_rate, 2)
        conn.execute(
            text("""
                UPDATE purchases SET supplier_name=:supplier, item_name=:item, quantity=:qty,
                purchase_rate=:rate, total_cost=:total, date=:date WHERE id=:pid AND shop_id=:shop_id
            """),
            {"supplier": req.supplier_name, "item": req.item_name.lower(), "qty": req.quantity,
             "rate": req.purchase_rate, "total": total_cost, "date": req.date,
             "pid": req.purchase_id, "shop_id": shop_id}
        )
        conn.commit()
    return {"success": True}

@app.delete("/purchase/{purchase_id}")
def delete_purchase(purchase_id: int, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        entry = conn.execute(
            text("SELECT * FROM purchases WHERE id = :pid AND shop_id = :shop_id"),
            {"pid": purchase_id, "shop_id": shop_id}
        ).fetchone()
        if not entry:
            return {"error": f"Purchase #{purchase_id} nahi mila"}
        conn.execute(
            text("DELETE FROM purchases WHERE id = :pid AND shop_id = :shop_id"),
            {"pid": purchase_id, "shop_id": shop_id}
        )
        conn.commit()
    return {"success": True}

@app.get("/purchases/all")
def get_all_purchases(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT id, supplier_name, item_name, quantity, purchase_rate, total_cost, date
                FROM purchases WHERE shop_id = :shop_id ORDER BY date DESC
            """),
            {"shop_id": shop_id}
        ).fetchall()
        grand_total = conn.execute(
            text("SELECT COALESCE(SUM(total_cost), 0) FROM purchases WHERE shop_id = :shop_id"),
            {"shop_id": shop_id}
        ).fetchone()[0]
    return {
        "purchases": [
            {"id": r[0], "supplier": r[1], "item": r[2], "quantity": r[3],
             "rate": r[4], "total": r[5], "date": str(r[6])}
            for r in rows
        ],
        "total_purchases": len(rows),
        "grand_total": grand_total
    }

@app.post("/entry/multi/parse")
def parse_multi(req: MultiEntryRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}

    data = parse_multi_entry(req.text)
    if not data:
        return {"error": "AI parse nahi kar saka"}

    if req.customer_override:
        data["customer"] = req.customer_override

    enriched_items = []
    for item in data.get("items", []):
        item_name = item.get("item", "")
        qty = item.get("quantity", 1) or 1
        variants = get_variants(item_name, shop_id) if item_name else []
        inv = get_item_price(item_name, shop_id) if item_name else None
        chosen = inv or (variants[0] if len(variants) == 1 else None)
        price = chosen["price"] if chosen else item.get("price", 0)
        enriched_items.append({
            "item": chosen["name"] if chosen else item_name,
            "quantity": qty,
            "price": price,
            "inventory_id": chosen["id"] if chosen else None,
            "stock": chosen["stock"] if chosen else None,
            "found": chosen is not None,
            "variants": variants,
            "needs_variant": False,
            "total": round(qty * price, 2)
        })

    data["items"] = enriched_items
    data["grand_total"] = round(sum(i["total"] for i in enriched_items), 2)
    return data

@app.post("/entry/multi/save")
def save_multi(req: MultiEntrySaveRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}

    customer_name = (req.customer or req.customer_override).strip()
    items = req.items

    if not items and req.text:
        data = parse_multi_entry(req.text)
        if not data:
            return {"error": "Parse nahi hua"}
        customer_name = customer_name or data.get("customer", "")
        items = [MultiItemSave(**{
            "item": i.get("item", ""),
            "quantity": i.get("quantity", 1),
            "price": i.get("price", 0),
            "found": False,
        }) for i in data.get("items", [])]

    if not customer_name:
        return {"error": "Customer missing hai"}
    if not items:
        return {"error": "Items missing hain"}

    saved_items = []
    for item in items:
        item_name = item.item.strip()
        qty = item.quantity
        price = item.price
        inventory_id = item.inventory_id

        if inventory_id:
            inv = get_item_by_id(inventory_id, shop_id)
            if not inv:
                return {"error": f"'{item_name}' inventory mein nahi mila"}
            item_name = inv["name"]
            if price <= 0:
                price = inv["price"]
        elif price <= 0:
            inv = get_item_price(item_name, shop_id)
            if inv:
                inventory_id = inv["id"]
                item_name = inv["name"]
                price = inv["price"]

        if not item_name or qty <= 0 or price <= 0:
            continue

        success = add_khaata_entry(customer_name, item_name, qty, price, shop_id)
        if success:
            if inventory_id:
                update_stock_by_id(inventory_id, qty, shop_id)
            elif item.found:
                update_stock(item_name, qty, shop_id)
            saved_items.append({
                "item": item_name,
                "quantity": qty,
                "price": price,
                "inventory_id": inventory_id,
                "total": round(qty * price, 2)
            })

    grand_total = round(sum(i["total"] for i in saved_items), 2)
    log.info(f"Multi-entry: {customer_name} {len(saved_items)} items Rs.{grand_total} shop={shop_id}")

    whatsapp = None
    if saved_items:
        phone, baaki = get_customer_phone_and_baaki(customer_name, shop_id)
        wa_msg = compose_entry_receipt(get_shop_name(shop_id), customer_name,
                                       saved_items, grand_total, baaki)
        whatsapp = build_whatsapp_payload(phone, wa_msg)

    return {
        "success": True,
        "customer": customer_name,
        "items": saved_items,
        "grand_total": grand_total,
        "items_saved": len(saved_items),
        "whatsapp": whatsapp
    }

# ---- WhatsApp ----
class SetPhoneRequest(BaseModel):
    customer: str
    phone: str
    resend_message: str = ""   # if provided, returns new wa link with same message

@app.post("/customer/set-phone")
def set_customer_phone(req: SetPhoneRequest, authorization: Optional[str] = Header(None)):
    """Phone save karta hai customer ke record mein, aur agar resend_message diya ho
    toh naya wa.me link wapas karta hai — dialog se save karke direct WhatsApp kholne ke liye."""
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    from customers import get_customer
    customer = get_customer(req.customer, shop_id)
    if not customer:
        # Customer exist nahi karta — bana do
        add_customer(req.customer, req.phone, shop_id)
    else:
        with get_connection() as conn:
            conn.execute(
                text("UPDATE customers SET phone=:p WHERE customer_id=:cid AND shop_id=:sid"),
                {"p": req.phone.strip(), "cid": customer[0], "sid": shop_id}
            )
            conn.commit()
    result = {"success": True, "customer": req.customer, "phone": req.phone}
    if req.resend_message:
        result["whatsapp"] = build_whatsapp_payload(req.phone, req.resend_message)
    return result

class ReminderRequest(BaseModel):
    customer: str

class BulkReminderRequest(BaseModel):
    customers: List[str] = []   # khaali = sab jinke baaki > 0
    min_baaki: float = 1

@app.post("/whatsapp/reminder")
def whatsapp_reminder(req: ReminderRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    phone, baaki = get_customer_phone_and_baaki(req.customer, shop_id)
    if phone is None and baaki == 0:
        return {"error": f"'{req.customer}' nahi mila"}
    msg = compose_reminder(get_shop_name(shop_id), req.customer, baaki)
    return {"success": True, "customer": req.customer, "baaki": baaki,
            "whatsapp": build_whatsapp_payload(phone, msg)}

@app.post("/whatsapp/bulk-reminders")
def whatsapp_bulk_reminders(req: BulkReminderRequest, authorization: Optional[str] = Header(None)):
    """Group messaging: selected customers (ya sab jinke baaki hai) ke liye
    ready-made reminder links. Cloud API configured ho toh auto-send bhi."""
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    shop_name = get_shop_name(shop_id)

    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT c.name, c.phone, COALESCE(SUM(k.total), 0) as baaki
                FROM customers c
                LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                WHERE c.shop_id = :shop_id
                GROUP BY c.customer_id, c.name, c.phone
                HAVING COALESCE(SUM(k.total), 0) >= :min_baaki
                ORDER BY baaki DESC
            """),
            {"shop_id": shop_id, "min_baaki": req.min_baaki}
        ).fetchall()

    selected = {c.lower().strip() for c in req.customers} if req.customers else None
    results = []
    for name, phone, baaki in rows:
        if selected is not None and name.lower() not in selected:
            continue
        msg = compose_reminder(shop_name, name, baaki)
        payload = build_whatsapp_payload(phone, msg)
        results.append({
            "customer": name.title(), "phone": phone, "baaki": baaki,
            "link": payload["link"], "phone_valid": payload["phone_valid"],
            "auto_sent": payload["auto_sent"],
        })
    return {"success": True, "count": len(results),
            "auto_send_enabled": AUTO_SEND_ENABLED, "reminders": results}

# ---- Reports / Dashboard ----
@app.get("/reports/dashboard")
def reports_dashboard(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        today_sale = conn.execute(text("""
            SELECT COALESCE(SUM(k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date = CURRENT_DATE AND k.item_name != 'PAYMENT' AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]
        today_payments = conn.execute(text("""
            SELECT COALESCE(SUM(-k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date = CURRENT_DATE AND k.item_name = 'PAYMENT' AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]
        month_sale = conn.execute(text("""
            SELECT COALESCE(SUM(k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date >= date_trunc('month', CURRENT_DATE)
              AND k.item_name != 'PAYMENT' AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]
        month_payments = conn.execute(text("""
            SELECT COALESCE(SUM(-k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date >= date_trunc('month', CURRENT_DATE)
              AND k.item_name = 'PAYMENT' AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]
        total_baaki = conn.execute(text("""
            SELECT COALESCE(SUM(k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]
        customer_count = conn.execute(
            text("SELECT COUNT(*) FROM customers WHERE shop_id = :sid"),
            {"sid": shop_id}).fetchone()[0]
        top_baaki = conn.execute(text("""
            SELECT c.name, c.phone, COALESCE(SUM(k.total), 0) as baaki
            FROM customers c LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
            WHERE c.shop_id = :sid
            GROUP BY c.customer_id, c.name, c.phone
            HAVING COALESCE(SUM(k.total), 0) > 0
            ORDER BY baaki DESC LIMIT 5
        """), {"sid": shop_id}).fetchall()
        low_stock = conn.execute(text("""
            SELECT item_name, stock, reorder_level FROM inventory
            WHERE shop_id = :sid AND stock <= reorder_level
            ORDER BY stock ASC LIMIT 10
        """), {"sid": shop_id}).fetchall()
        month_purchases = conn.execute(text("""
            SELECT COALESCE(SUM(total_cost), 0) FROM purchases
            WHERE shop_id = :sid AND date >= date_trunc('month', CURRENT_DATE)
        """), {"sid": shop_id}).fetchone()[0]
    return {
        "today_sale": today_sale,
        "today_payments": today_payments,
        "month_sale": month_sale,
        "month_payments": month_payments,
        "month_purchases": month_purchases,
        "total_baaki": total_baaki,
        "customer_count": customer_count,
        "top_baaki": [{"name": r[0].title(), "phone": r[1], "baaki": r[2]} for r in top_baaki],
        "low_stock": [{"item": r[0].title(), "stock": r[1], "reorder_level": r[2]} for r in low_stock],
    }

# ---- CSV Export (backup) ----
from fastapi.responses import PlainTextResponse
import csv
import io

def _csv_response(rows, headers, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/export/customers")
def export_customers(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT c.name, c.phone, COALESCE(SUM(k.total), 0), COUNT(k.id)
            FROM customers c LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
            WHERE c.shop_id = :sid
            GROUP BY c.customer_id, c.name, c.phone ORDER BY c.name
        """), {"sid": shop_id}).fetchall()
    return _csv_response(
        [(r[0].title(), r[1], r[2], r[3]) for r in rows],
        ["Customer", "Phone", "Total Baaki", "Entries"], "customers.csv")

@app.get("/export/khaata")
def export_khaata(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT c.name, k.date, k.item_name, k.quantity, k.price_per_item, k.total
            FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.deleted_at IS NULL ORDER BY c.name, k.date
        """), {"sid": shop_id}).fetchall()
    return _csv_response(
        [(r[0].title(), str(r[1]), r[2].title(), r[3], r[4], r[5]) for r in rows],
        ["Customer", "Date", "Item", "Quantity", "Price", "Total"], "khaata.csv")

@app.get("/export/inventory")
def export_inventory(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT item_name, category, sale_price, purchase_rate, stock, reorder_level
            FROM inventory WHERE shop_id = :sid ORDER BY item_name
        """), {"sid": shop_id}).fetchall()
    return _csv_response(
        [(r[0].title(), r[1], r[2], r[3], r[4], r[5]) for r in rows],
        ["Item", "Category", "Sale Price", "Purchase Rate", "Stock", "Reorder Level"],
        "inventory.csv")

# ---- Trash (soft-deleted entries) ----
@app.get("/trash/khaata")
def trash_khaata(authorization: Optional[str] = Header(None)):
    """Last 30 days ke deleted entries — restore ho sakti hain."""
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT k.id, c.name, k.date, k.item_name, k.quantity, k.price_per_item, k.total, k.deleted_at
            FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.deleted_at IS NOT NULL
              AND k.deleted_at >= NOW() - INTERVAL '30 days'
            ORDER BY k.deleted_at DESC
            LIMIT 200
        """), {"sid": shop_id}).fetchall()
    return {
        "entries": [
            {"id": r[0], "customer": r[1].title(), "date": str(r[2]),
             "item": r[3].title(), "quantity": r[4], "price": r[5], "total": r[6],
             "deleted_at": str(r[7])}
            for r in rows
        ],
        "count": len(rows)
    }

@app.post("/trash/restore/{entry_id}")
def trash_restore(entry_id: int, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        result = conn.execute(text("""
            UPDATE khaata SET deleted_at = NULL
            WHERE id = :eid AND customer_id IN (SELECT customer_id FROM customers WHERE shop_id = :sid)
              AND deleted_at IS NOT NULL
            RETURNING id
        """), {"eid": entry_id, "sid": shop_id}).fetchone()
        conn.commit()
    if not result:
        return {"error": f"Entry #{entry_id} trash mein nahi mili"}
    log.info(f"Entry restored: id={entry_id}, shop={shop_id}")
    return {"success": True, "id": entry_id}

@app.delete("/trash/purge")
def trash_purge(authorization: Optional[str] = Header(None)):
    """30+ din purani deleted entries permanently hata do. Frontend se
    manually trigger hoti hai — user ko dikhata hai kitni entries hatengi."""
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return {"error": "Login zaroori hai"}
    with get_connection() as conn:
        result = conn.execute(text("""
            DELETE FROM khaata
            WHERE customer_id IN (SELECT customer_id FROM customers WHERE shop_id = :sid)
              AND deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'
            RETURNING id
        """), {"sid": shop_id}).fetchall()
        conn.commit()
    log.info(f"Trash purged: {len(result)} entries, shop={shop_id}")
    return {"success": True, "purged": len(result)}
