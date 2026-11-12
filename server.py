from fastapi import FastAPI, Header, Request, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pathlib import Path
from pydantic import BaseModel, field_validator
from typing import Optional, List
from sqlalchemy import text
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager
from datetime import datetime as dt
import hmac
import hashlib
import json
import httpx
import csv
import io

from customers import add_khaata_entry, add_customer, get_customer
from inventory import (get_item_price, get_item_by_id, get_variants,
                       add_item, get_all_items, edit_item, delete_item,
                       apply_stock_delta)
from ai_parser import parse_entry, parse_purchase, parse_multi_entry
from voice import transcribe_audio
from database import setup_database, get_connection
from auth import verify_token, register_shop, login_shop, reset_password
from whatsapp import (build_whatsapp_payload, compose_entry_receipt,
                      compose_payment_receipt, compose_reminder, AUTO_SEND_ENABLED)
from logger import get_logger
from config import (
    WORKER_URL, WORKER_API_KEY, AGENT_ENABLED,
    WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN,
    ALLOWED_ORIGINS, GROQ_API_KEY,
)

log = get_logger("dukaan.server")
limiter = Limiter(key_func=get_remote_address)


# ==================== STARTUP (lifespan) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_database()
    log.info(f"CORS origins: {ALLOWED_ORIGINS}")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY missing — AI features disabled")
    if not WHATSAPP_APP_SECRET:
        log.warning("WHATSAPP_APP_SECRET missing — webhook POST will return 503")
    yield


app = FastAPI(title="Dukaan AI", version="1.3", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if errors:
        first = errors[0]
        msg = first.get("msg", "Input galat hai")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        return JSONResponse(status_code=422, content={"error": msg})
    return JSONResponse(status_code=422, content={"error": "Input galat hai"})


# ==================== HELPERS ====================
def err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def auth_err() -> JSONResponse:
    return err("Login zaroori hai", 401)


def get_shop_id(authorization: Optional[str] = None) -> Optional[int]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    payload = verify_token(parts[1].strip())
    if not payload:
        return None
    return payload.get("shop_id")


def get_customer_phone_and_baaki(customer_name: str, shop_id: int):
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


# ==================== MODELS ====================
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


class OpeningBalanceRequest(BaseModel):
    customer_id: int
    opening_balance: float


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


class ExpenseRequest(BaseModel):
    expense_name: str
    amount: float
    notes: str = ""


class ReturnRequest(BaseModel):
    customer: str
    item: str
    quantity: float
    price: float
    inventory_id: Optional[int] = None


class SupplierPaymentRequest(BaseModel):
    supplier: str
    amount: float
    notes: str = ""


class QuickPurchaseRequest(BaseModel):
    supplier: str
    item: str
    quantity: float
    rate: float
    inventory_id: Optional[int] = None


class BulkPurchaseItem(BaseModel):
    item: str
    quantity: float
    rate: float
    inventory_id: Optional[int] = None


class BulkPurchaseRequest(BaseModel):
    supplier: str
    items: List[BulkPurchaseItem]


class SetPhoneRequest(BaseModel):
    customer: str
    phone: str
    resend_message: str = ""


class ReminderRequest(BaseModel):
    customer: str


class BulkReminderRequest(BaseModel):
    customers: List[str] = []
    min_baaki: float = 1


class AgentMessageRequest(BaseModel):
    channel: str
    external_id: str
    message: str
    customer_id: Optional[int] = None
class ShopProfileRequest(BaseModel):
    shop_name: str
    phones: List[str] = []
    offers_board: str = ""
    receipt_greeting: str = ""
    show_offers: bool = True
    show_greeting: bool = True
    logo_base64: str = ""

# ==================== HOME & AUTH ====================
@app.get("/")
def home():
    return {"message": "Dukaan AI server chal raha hai!"}


_UI_CACHE: Optional[str] = None


@app.get("/ui")
def serve_ui():
    global _UI_CACHE
    if _UI_CACHE is None:
        _UI_CACHE = Path("index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=_UI_CACHE)


@app.post("/auth/register")
@limiter.limit("5/hour")
def register(request: Request, req: AuthRequest):
    if not req.shop_name:
        return err("Shop name zaroori hai", 422)
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


# ==================== ENTRY ====================
@app.post("/entry/parse")
@limiter.limit("30/minute")
def parse_natural_entry(request: Request, req: EntryRequest,
                        authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not GROQ_API_KEY:
        return err("AI features disabled — GROQ_API_KEY missing", 503)

    data = parse_entry(req.text)
    if not data:
        return err("AI parse nahi kar saka", 422)

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
@limiter.limit("15/minute")
async def parse_voice_entry(request: Request, audio: UploadFile = File(...),
                            authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not GROQ_API_KEY:
        return err("AI features disabled — GROQ_API_KEY missing", 503)

    audio_bytes = await audio.read()
    result = transcribe_audio(audio_bytes, filename=audio.filename or "voice.webm")
    if result["error"]:
        return err(result["error"], 422)
    transcript = result["text"]
    data = parse_entry(transcript)
    if not data:
        return err("AI parse nahi kar saka", 422)
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
def save_entry(req: SingleEntrySaveRequest,
               authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    # ---- Build payload ----
    if req.customer or req.item:
        data = {
            "customer": req.customer.strip(),
            "item": req.item.strip(),
            "quantity": req.quantity,
            "price": req.manual_price if req.manual_price > 0 else req.price,
            "inventory_id": req.inventory_id,
        }
    else:
        if not GROQ_API_KEY:
            return err("AI features disabled — GROQ_API_KEY missing", 503)
        data = parse_entry(req.text)
        if not data:
            return err("Parse nahi hua", 422)
        if req.manual_price > 0:
            data["price"] = req.manual_price

    if not data.get("customer"):
        return err("Customer missing hai", 422)

    # ---- Price validation BEFORE branch (payment also needs valid price) ----
    try:
        price_val = float(data.get("price") or 0)
    except (TypeError, ValueError):
        return err("Price numeric hona chahiye", 422)
    if price_val <= 0:
        return err("Price 0 se zyada honi chahiye", 422)
    data["price"] = price_val

    # ============ PAYMENT branch (unchanged logic, error codes added) ============
    if data.get("item") == "PAYMENT":
        customer = get_customer(data["customer"], shop_id)
        if not customer:
            return err(f"'{data['customer']}' nahi mila", 404)

        with get_connection() as conn:
            try:
                conn.execute(
                    text("""INSERT INTO khaata
                            (customer_id, item_name, quantity, price_per_item, total)
                            VALUES (:cid, 'PAYMENT', 1, :price, :neg_price)"""),
                    {"cid": customer[0], "price": price_val, "neg_price": -price_val}
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.error(f"Payment insert failed: {e}")
                return err("Payment save nahi hui", 500)

        log.info(f"Payment: {data['customer']} Rs.{price_val} shop={shop_id}")
        phone, baaki = get_customer_phone_and_baaki(data["customer"], shop_id)
        wa_msg = compose_payment_receipt(get_shop_name(shop_id), data["customer"],
                                         price_val, baaki)
        return {"success": True, "type": "payment", "data": data,
                "whatsapp": build_whatsapp_payload(phone, wa_msg)}

    # ============ UDHAAR branch — atomic khaata + stock ============
    if not data.get("item"):
        return err("Item missing hai", 422)

    # Resolve inventory + cost_price (skip when manual_price > 0 — custom items)
    cost_price = 0
    if req.manual_price == 0:
        if data.get("inventory_id"):
            inv_item = get_item_by_id(data["inventory_id"], shop_id)
            if not inv_item:
                return err("Selected inventory item nahi mila", 404)
            data["item"] = inv_item["name"]
            if not data.get("price") or float(data["price"]) <= 0:
                data["price"] = inv_item["price"]
            cost_price = inv_item.get("purchase_rate", 0) or 0
        else:
            inv_item = get_item_price(data["item"], shop_id)
            if inv_item:
                data["inventory_id"] = inv_item["id"]
                data["item"] = inv_item["name"]
                if not data.get("price") or float(data["price"]) <= 0:
                    data["price"] = inv_item["price"]
                full = get_item_by_id(inv_item["id"], shop_id)
                if full:
                    cost_price = full.get("purchase_rate", 0) or 0

    qty = float(data["quantity"])
    price_val = float(data["price"])
    entry_total = round(qty * price_val, 2)

    # ---- Single transaction: khaata insert + stock deduction ----
    with get_connection() as conn:
        try:
            ok = add_khaata_entry(
                data["customer"], data["item"], qty, price_val, shop_id,
                inventory_id=data.get("inventory_id"),
                cost_price=cost_price,
                conn=conn,
            )
            if not ok:
                conn.rollback()
                return err("Entry save nahi hui", 500)

            if req.manual_price == 0:
                if data.get("inventory_id"):
                    stock_ok = apply_stock_delta(
                        conn, shop_id, data["item"], -qty,
                        inventory_id=data["inventory_id"]
                    )
                    if not stock_ok:
                        conn.rollback()
                        return err(f"'{data['item']}' ka stock update nahi hua", 500)
                else:
                    # Best-effort fuzzy match, log if missed
                    if not apply_stock_delta(conn, shop_id, data["item"], -qty):
                        log.warning(f"Stock not updated (no inventory match): {data['item']}")

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"save_entry transaction failed: {e}")
            return err("Entry save nahi hui", 500)

    phone, baaki = get_customer_phone_and_baaki(data["customer"], shop_id)
    wa_msg = compose_entry_receipt(
        get_shop_name(shop_id), data["customer"],
        [{"item": data["item"], "quantity": qty,
          "price": price_val, "total": entry_total}],
        entry_total, baaki
    )
    log.info(f"Udhaar: {data['customer']} {data['item']} x{qty} Rs.{entry_total} shop={shop_id}")
    return {"success": True, "type": "udhaar", "data": data,
            "whatsapp": build_whatsapp_payload(phone, wa_msg)}


@app.put("/entry/edit")
def edit_entry(req: EntryEditRequest,
               authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.quantity <= 0 or req.price_per_item <= 0:
        return err("Quantity aur price 0 se zyada hone chahiye", 422)

    with get_connection() as conn:
        try:
            entry = conn.execute(
                text("""SELECT k.item_name, k.quantity, k.inventory_id
                        FROM khaata k
                        JOIN customers c ON c.customer_id = k.customer_id
                        WHERE k.id = :eid AND c.shop_id = :sid AND k.deleted_at IS NULL"""),
                {"eid": req.entry_id, "sid": shop_id}
            ).fetchone()
            if not entry:
                return err(f"Entry #{req.entry_id} nahi mili", 404)

            old_item, old_qty, inv_id = entry[0], float(entry[1]), entry[2]

            if old_item and old_item.upper() in ("PAYMENT", "RETURN"):
                return err(
                    "PAYMENT/RETURN entries edit nahi ho sakti — delete karke nayi banayein",
                    400
                )

            total = round(req.quantity * req.price_per_item, 2)
            conn.execute(
                text("""UPDATE khaata SET item_name=:item, quantity=:qty,
                        price_per_item=:price, total=:total, date=:date
                        WHERE id=:eid AND customer_id IN
                            (SELECT customer_id FROM customers WHERE shop_id=:sid)"""),
                {"item": req.item_name.lower(), "qty": req.quantity,
                 "price": req.price_per_item, "total": total, "date": req.date,
                 "eid": req.entry_id, "sid": shop_id}
            )

            # Stock delta: old_qty - new_qty = amount to RESTORE to stock
            delta = old_qty - float(req.quantity)
            if delta != 0 and old_item:
                apply_stock_delta(conn, shop_id, old_item, delta, inventory_id=inv_id)

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"edit_entry failed: {e}")
            return err("Entry update nahi hui", 500)

    return {"success": True}


@app.delete("/entry/{entry_id}")
def delete_entry(entry_id: int,
                 authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    with get_connection() as conn:
        try:
            entry = conn.execute(
                text("""SELECT k.item_name, k.quantity, k.inventory_id
                        FROM khaata k
                        JOIN customers c ON c.customer_id = k.customer_id
                        WHERE k.id = :eid AND c.shop_id = :sid AND k.deleted_at IS NULL"""),
                {"eid": entry_id, "sid": shop_id}
            ).fetchone()
            if not entry:
                return err(f"Entry #{entry_id} nahi mili", 404)

            item_name, qty, inv_id = entry[0], float(entry[1]), entry[2]

            conn.execute(
                text("""UPDATE khaata SET deleted_at = NOW()
                        WHERE id = :eid AND customer_id IN
                            (SELECT customer_id FROM customers WHERE shop_id = :sid)"""),
                {"eid": entry_id, "sid": shop_id}
            )

            is_stock_entry = item_name and item_name.upper() not in ("PAYMENT", "RETURN")
            if is_stock_entry and qty:
                apply_stock_delta(conn, shop_id, item_name, qty, inventory_id=inv_id)

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"delete_entry failed: {e}")
            return err("Entry delete nahi hui", 500)

    log.info(f"Entry soft-deleted: id={entry_id}, stock +{qty}, shop={shop_id}")
    return {"success": True}


@app.post("/entry/multi/parse")
@limiter.limit("30/minute")
def parse_multi(request: Request, req: MultiEntryRequest,
                authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not GROQ_API_KEY:
        return err("AI features disabled — GROQ_API_KEY missing", 503)

    data = parse_multi_entry(req.text)
    if not data:
        return err("AI parse nahi kar saka", 422)
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
            "total": round(qty * price, 2),
        })
    data["items"] = enriched_items
    data["grand_total"] = round(sum(i["total"] for i in enriched_items), 2)
    return data


@app.post("/entry/multi/save")
def save_multi(req: MultiEntrySaveRequest,
               authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    customer_name = (req.customer or req.customer_override).strip()
    items = req.items

    if not items and req.text:
        if not GROQ_API_KEY:
            return err("AI features disabled — GROQ_API_KEY missing", 503)
        data = parse_multi_entry(req.text)
        if not data:
            return err("Parse nahi hua", 422)
        customer_name = customer_name or data.get("customer", "")
        items = [MultiItemSave(**{
            "item": i.get("item", ""),
            "quantity": i.get("quantity", 1),
            "price": i.get("price", 0),
            "found": False,
        }) for i in data.get("items", [])]

    if not customer_name:
        return err("Customer missing hai", 422)
    if not items:
        return err("Items missing hain", 422)

    # ---- Phase 1: Resolve ALL items (reads only, outside transaction) ----
    resolved = []
    for item in items:
        item_name = (item.item or "").strip()
        qty = float(item.quantity)
        price = float(item.price)
        inventory_id = item.inventory_id
        cost_price = 0

        if not item_name:
            return err("Item name khali hai", 422)
        if qty <= 0:
            return err(f"'{item_name}' ki quantity 0 se zyada honi chahiye", 422)

        if inventory_id:
            inv = get_item_by_id(inventory_id, shop_id)
            if not inv:
                return err(f"'{item_name}' inventory mein nahi mila", 404)
            item_name = inv["name"]
            cost_price = inv.get("purchase_rate", 0) or 0
            if price <= 0:
                price = inv["price"]
        else:
            inv = get_item_price(item_name, shop_id)
            if inv:
                inventory_id = inv["id"]
                item_name = inv["name"]
                if price <= 0:
                    price = inv["price"]
                full = get_item_by_id(inv["id"], shop_id)
                if full:
                    cost_price = full.get("purchase_rate", 0) or 0

        if price <= 0:
            return err(f"'{item_name}' ka price 0 se zyada hona chahiye", 422)

        resolved.append({
            "item": item_name, "quantity": qty, "price": price,
            "inventory_id": inventory_id, "cost_price": cost_price,
            "total": round(qty * price, 2),
        })

    # ---- Phase 2: Single transaction — all-or-nothing ----
    with get_connection() as conn:
        try:
            for r in resolved:
                ok = add_khaata_entry(
                    customer_name, r["item"], r["quantity"], r["price"], shop_id,
                    inventory_id=r["inventory_id"], cost_price=r["cost_price"],
                    conn=conn,
                )
                if not ok:
                    conn.rollback()
                    return err(f"'{r['item']}' save nahi hua", 500)

                if r["inventory_id"]:
                    stock_ok = apply_stock_delta(
                        conn, shop_id, r["item"], -r["quantity"],
                        inventory_id=r["inventory_id"]
                    )
                    if not stock_ok:
                        conn.rollback()
                        return err(f"'{r['item']}' ka stock update nahi hua", 500)
                else:
                    if not apply_stock_delta(conn, shop_id, r["item"], -r["quantity"]):
                        log.warning(f"Stock not updated (no inventory match): {r['item']}")

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"save_multi transaction failed: {e}")
            return err("Entries save nahi hui", 500)

    grand_total = round(sum(r["total"] for r in resolved), 2)
    log.info(f"Multi-entry: {customer_name} {len(resolved)} items Rs.{grand_total} shop={shop_id}")

    phone, baaki = get_customer_phone_and_baaki(customer_name, shop_id)
    wa_msg = compose_entry_receipt(get_shop_name(shop_id), customer_name,
                                   resolved, grand_total, baaki)

    return {
        "success": True,
        "customer": customer_name,
        "items": resolved,
        "grand_total": grand_total,
        "items_saved": len(resolved),
        "whatsapp": build_whatsapp_payload(phone, wa_msg),
    }


@app.post("/entry/return")
def process_return(req: ReturnRequest,
                   authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.quantity <= 0 or req.price <= 0:
        return err("Quantity aur price 0 se zyada hone chahiye", 422)

    customer = get_customer(req.customer, shop_id)
    if not customer:
        return err(f"Customer '{req.customer}' nahi mila", 404)

    total = round(req.quantity * req.price, 2)

    with get_connection() as conn:
        try:
            conn.execute(
                text("""INSERT INTO khaata
                        (customer_id, item_name, quantity, price_per_item, total, inventory_id)
                        VALUES (:cid, 'RETURN', :qty, :price, :neg_total, :inv_id)"""),
                {"cid": customer[0], "qty": req.quantity, "price": req.price,
                 "neg_total": -total, "inv_id": req.inventory_id}
            )

            if req.inventory_id:
                apply_stock_delta(conn, shop_id, req.item, req.quantity,
                                  inventory_id=req.inventory_id)
            else:
                if not apply_stock_delta(conn, shop_id, req.item, req.quantity):
                    log.warning(f"Return: no inventory match for '{req.item}'")

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"process_return failed: {e}")
            return err("Return save nahi hui", 500)

    log.info(f"Return: {req.customer} {req.item} x{req.quantity} Rs.{total} shop={shop_id}")
    return {"success": True, "total": total}
# ==================== SHOP PROFILE ====================
@app.get("/shop/profile")
def get_shop_profile(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    
    with get_connection() as conn:
        row = conn.execute(
            text("""SELECT shop_name, phones, offers_board, receipt_greeting, 
                           show_offers, show_greeting, logo_base64 
                    FROM shops WHERE shop_id = :sid"""),
            {"sid": shop_id}
        ).fetchone()
        
    if not row:
        return err("Shop nahi mili", 404)
        
    import json
    try:
        phones = json.loads(row[1]) if row[1] else []
    except:
        phones = []
        
    return {
        "shop_name": row[0],
        "phones": phones,
        "offers_board": row[2] or "",
        "receipt_greeting": row[3] or "",
        "show_offers": row[4],
        "show_greeting": row[5],
        "logo_base64": row[6] or ""
    }

@app.put("/shop/profile")
def update_shop_profile(req: ShopProfileRequest, authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
        
    import json
    phones_json = json.dumps(req.phones)
    
    with get_connection() as conn:
        conn.execute(
            text("""UPDATE shops SET 
                        shop_name = :name, 
                        phones = :phones, 
                        offers_board = :offers, 
                        receipt_greeting = :greeting,
                        show_offers = :show_offers,
                        show_greeting = :show_greeting,
                        logo_base64 = :logo
                    WHERE shop_id = :sid"""),
            {
                "name": req.shop_name.strip(),
                "phones": phones_json,
                "offers": req.offers_board.strip(),
                "greeting": req.receipt_greeting.strip(),
                "show_offers": req.show_offers,
                "show_greeting": req.show_greeting,
                "logo": req.logo_base64,
                "sid": shop_id
            }
        )
        conn.commit()
        
    log.info(f"Shop profile updated: shop={shop_id}")
    return {"success": True, "message": "Profile update ho gayi!"}

# ==================== KHAATA & CUSTOMERS ====================
@app.get("/khaata/{identifier}")
def get_khaata(identifier: str,
               authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    with get_connection() as conn:
        customer = None
        try:
            cid = int(identifier)
            customer = conn.execute(
                text("""SELECT customer_id, name, phone, COALESCE(opening_balance, 0)
                        FROM customers WHERE customer_id = :cid AND shop_id = :sid"""),
                {"cid": cid, "sid": shop_id}
            ).fetchone()
        except ValueError:
            pass
        if not customer:
            customer = conn.execute(
                text("""SELECT customer_id, name, phone, COALESCE(opening_balance, 0)
                        FROM customers WHERE LOWER(name) = :name AND shop_id = :sid"""),
                {"name": identifier.lower().strip(), "sid": shop_id}
            ).fetchone()
        if not customer:
            return err(f"'{identifier}' nahi mila", 404)

        rows = conn.execute(
            text("""SELECT id, date, item_name, quantity, price_per_item, total
                    FROM khaata WHERE customer_id = :cid AND deleted_at IS NULL
                    ORDER BY date ASC, id ASC"""),
            {"cid": customer[0]}
        ).fetchall()

        entries_total = sum(float(r[5] or 0) for r in rows)
        opening = float(customer[3] or 0)
        grand_total = round(entries_total + opening, 2)

    return {
        "customer": customer[1].title(),
        "customer_id": customer[0],
        "phone": customer[2],
        "entries": [
            {"id": r[0], "date": str(r[1]), "item": r[2],
             "quantity": r[3], "price": r[4], "total": r[5]}
            for r in rows
        ],
        "total_baaki": grand_total,
        "entries_total": round(entries_total, 2),
        "opening_balance": opening,
        "grand_total": grand_total,
    }


@app.post("/customer/add")
def new_customer(req: CustomerRequest,
                 authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not add_customer(req.name, req.phone, shop_id):
        return err("Customer add nahi hua", 500)
    return {"success": True, "customer": req.name}


@app.put("/customer/edit")
def edit_customer(req: CustomerEditRequest,
                  authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    customer = get_customer(req.old_name, shop_id)
    if not customer:
        return err(f"'{req.old_name}' nahi mila", 404)
    try:
        with get_connection() as conn:
            conn.execute(
                text("""UPDATE customers SET name=:name, phone=:phone
                        WHERE customer_id=:cid AND shop_id=:sid"""),
                {"name": req.new_name.lower().strip(),
                 "phone": req.new_phone.strip(),
                 "cid": customer[0], "sid": shop_id}
            )
            conn.commit()
        return {"success": True}
    except Exception as e:
        log.error(f"edit_customer failed: {e}")
        return err("Customer update nahi hua", 500)


@app.put("/customer/opening-balance")
def set_opening_balance(req: OpeningBalanceRequest,
                        authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    with get_connection() as conn:
        result = conn.execute(
            text("""UPDATE customers SET opening_balance = :ob
                    WHERE customer_id = :cid AND shop_id = :sid RETURNING customer_id"""),
            {"ob": req.opening_balance, "cid": req.customer_id, "sid": shop_id}
        ).fetchone()
        conn.commit()
    if not result:
        return err("Customer nahi mila", 404)
    log.info(f"Opening balance set: customer={req.customer_id} Rs.{req.opening_balance} shop={shop_id}")
    return {"success": True}


@app.delete("/customer/{customer_name}")
def delete_customer(customer_name: str,
                    authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    customer = get_customer(customer_name, shop_id)
    if not customer:
        return err(f"'{customer_name}' nahi mila", 404)

    with get_connection() as conn:
        baaki = conn.execute(
            text("""SELECT COALESCE(SUM(total), 0) FROM khaata
                    WHERE customer_id = :cid AND deleted_at IS NULL"""),
            {"cid": customer[0]}
        ).fetchone()[0] or 0

    if abs(float(baaki)) > 0.01:
        return err(
            f"Customer ka baaki Rs. {baaki} hai — pehle clear karein phir delete karein",
            409
        )

    with get_connection() as conn:
        conn.execute(
            text("DELETE FROM khaata WHERE customer_id = :cid"),
            {"cid": customer[0]}
        )
        conn.execute(
            text("DELETE FROM customers WHERE customer_id = :cid AND shop_id = :sid"),
            {"cid": customer[0], "sid": shop_id}
        )
        conn.commit()

    log.info(f"Customer deleted (clean): {customer_name} shop={shop_id}")
    return {"success": True}


@app.get("/customers/all")
def all_customers(limit: int = 500,
                  authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    limit = max(1, min(int(limit), 1000))
    with get_connection() as conn:
        customers = conn.execute(
            text("""SELECT c.name, c.phone,
                           COALESCE(SUM(k.total), 0) as total_baaki,
                           COUNT(k.id) as total_entries
                    FROM customers c
                    LEFT JOIN khaata k ON c.customer_id = k.customer_id AND k.deleted_at IS NULL
                    WHERE c.shop_id = :shop_id
                    GROUP BY c.customer_id, c.name, c.phone
                    ORDER BY total_baaki DESC
                    LIMIT :lim"""),
            {"shop_id": shop_id, "lim": limit}
        ).fetchall()
    return {
        "customers": [
            {"name": r[0].title(), "phone": r[1],
             "total_baaki": r[2], "total_entries": r[3]}
            for r in customers
        ],
        "total_customers": len(customers),
        "grand_total": sum(r[2] for r in customers),
    }


@app.get("/customers/search")
def search_customers(q: str = "", limit: int = 10,
                     authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    limit = max(1, min(int(limit), 50))
    q = (q or "").strip().lower()

    with get_connection() as conn:
        if not q:
            rows = conn.execute(
                text("""SELECT c.customer_id, c.name, c.phone,
                               COALESCE(SUM(k.total), 0) AS baaki
                        FROM customers c
                        LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                        WHERE c.shop_id = :sid
                        GROUP BY c.customer_id, c.name, c.phone
                        ORDER BY baaki DESC LIMIT :lim"""),
                {"sid": shop_id, "lim": limit}
            ).fetchall()
        else:
            rows = []
            try:
                cid = int(q)
                id_row = conn.execute(
                    text("""SELECT c.customer_id, c.name, c.phone,
                                   COALESCE(SUM(k.total), 0) AS baaki
                            FROM customers c
                            LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                            WHERE c.shop_id = :sid AND c.customer_id = :cid
                            GROUP BY c.customer_id, c.name, c.phone"""),
                    {"sid": shop_id, "cid": cid}
                ).fetchall()
                rows = list(id_row)
            except ValueError:
                pass
            remaining = limit - len(rows)
            if remaining > 0:
                seen_ids = {r[0] for r in rows}
                name_rows = conn.execute(
                    text("""SELECT c.customer_id, c.name, c.phone,
                                   COALESCE(SUM(k.total), 0) AS baaki
                            FROM customers c
                            LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                            WHERE c.shop_id = :sid AND LOWER(c.name) LIKE :q
                            GROUP BY c.customer_id, c.name, c.phone
                            ORDER BY c.name LIMIT :lim"""),
                    {"sid": shop_id, "q": f"%{q}%", "lim": remaining + len(rows)}
                ).fetchall()
                for r in name_rows:
                    if r[0] not in seen_ids and len(rows) < limit:
                        rows.append(r)
                        seen_ids.add(r[0])

    return {
        "customers": [
            {"id": r[0], "name": r[1].title(), "phone": r[2], "baaki": r[3]}
            for r in rows
        ]
    }


@app.post("/customer/set-phone")
def set_customer_phone(req: SetPhoneRequest,
                       authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    customer = get_customer(req.customer, shop_id)
    if not customer:
        if not add_customer(req.customer, req.phone, shop_id):
            return err("Customer add nahi hua", 500)
    else:
        with get_connection() as conn:
            conn.execute(
                text("""UPDATE customers SET phone=:p
                        WHERE customer_id=:cid AND shop_id=:sid"""),
                {"p": req.phone.strip(), "cid": customer[0], "sid": shop_id}
            )
            conn.commit()
    result = {"success": True, "customer": req.customer, "phone": req.phone}
    if req.resend_message:
        result["whatsapp"] = build_whatsapp_payload(req.phone, req.resend_message)
    return result


# ==================== INVENTORY ====================
@app.get("/inventory/all")
def get_inventory(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    items = get_all_items(shop_id)
    low_stock = [i for i in items if i["low_stock"]]
    return {"items": items, "total": len(items), "low_stock_count": len(low_stock)}


@app.post("/inventory/add")
def add_inventory_item(req: InventoryItemRequest,
                       authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    return add_item(shop_id, req.item_name, req.sale_price,
                    req.purchase_rate, req.stock, req.category, req.reorder_level)


@app.put("/inventory/edit")
def edit_inventory_item(req: InventoryEditRequest,
                        authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    return edit_item(req.item_id, shop_id, req.item_name, req.sale_price,
                     req.purchase_rate, req.stock, req.category, req.reorder_level)


@app.delete("/inventory/item/{item_id}")
def delete_inventory_item(item_id: int,
                          authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    return delete_item(item_id, shop_id)


@app.get("/inventory/variants/{keyword}")
def get_item_variants(keyword: str,
                      authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    variants = get_variants(keyword, shop_id)
    return {"variants": variants, "count": len(variants)}


@app.get("/inventory/{item_name}")
def check_inventory(item_name: str,
                    authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    variants = get_variants(item_name, shop_id)
    if not variants:
        result = get_item_price(item_name, shop_id)
        if result:
            return {"variants": [result], "count": 1, "match": result}
        return err(f"{item_name} inventory mein nahi mila", 404)
    return {
        "variants": variants,
        "count": len(variants),
        "match": variants[0] if len(variants) == 1 else None,
    }


@app.get("/items/search")
def search_items(q: str = "", limit: int = 10,
                 authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    limit = max(1, min(int(limit), 50))
    q = (q or "").strip().lower()
    with get_connection() as conn:
        if not q:
            rows = conn.execute(
                text("""SELECT id, item_name, sale_price, stock
                        FROM inventory WHERE shop_id = :sid
                        ORDER BY item_name LIMIT :lim"""),
                {"sid": shop_id, "lim": limit}
            ).fetchall()
        else:
            rows = conn.execute(
                text("""SELECT id, item_name, sale_price, stock
                        FROM inventory
                        WHERE shop_id = :sid AND LOWER(item_name) LIKE :contains
                        ORDER BY
                            CASE
                                WHEN LOWER(item_name) = :exact THEN 0
                                WHEN LOWER(item_name) LIKE :prefix THEN 1
                                ELSE 2
                            END,
                            LENGTH(item_name),
                            item_name
                        LIMIT :lim"""),
                {"sid": shop_id, "contains": f"%{q}%",
                 "exact": q, "prefix": f"{q}%", "lim": limit}
            ).fetchall()
    return {
        "items": [
            {"id": r[0], "name": r[1],
             "price": float(r[2]) if r[2] is not None else 0.0,
             "stock": float(r[3]) if r[3] is not None else 0.0}
            for r in rows
        ]
    }


# ==================== PURCHASES ====================
@app.post("/purchase/parse")
@limiter.limit("20/minute")
def parse_purchase_entry(request: Request, req: PurchaseRequest,
                         authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not GROQ_API_KEY:
        return err("AI features disabled — GROQ_API_KEY missing", 503)

    data = parse_purchase(req.text)
    if not data:
        return err("AI parse nahi kar saka", 422)

    if data.get("total_given", 0) > 0 and data.get("quantity", 0) > 0:
        data["rate"] = round(data["total_given"] / data["quantity"], 2)
        data["auto_divided"] = True
    else:
        data["auto_divided"] = False

    inv_item = get_item_price(data.get("item", ""), shop_id)
    if inv_item:
        data["found_in_sheet"] = True
        data["current_stock"] = inv_item["stock"]
        data["sale_price"] = inv_item["price"]
    else:
        data["found_in_sheet"] = False
    return data


@app.post("/purchase/save")
def save_purchase(req: PurchaseRequest,
                  authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not GROQ_API_KEY:
        return err("AI features disabled — GROQ_API_KEY missing", 503)

    data = parse_purchase(req.text)
    if not data:
        return err("Parse nahi hua", 422)
    if not data.get("item"):
        return err("Item missing hai", 422)

    if data.get("total_given", 0) > 0 and data.get("quantity", 0) > 0:
        data["rate"] = round(data["total_given"] / data["quantity"], 2)
    if not data.get("rate"):
        return err("Rate missing hai", 422)

    quantity = float(data["quantity"])
    rate = float(data["rate"])
    if quantity <= 0 or rate <= 0:
        return err("Quantity aur rate 0 se zyada hone chahiye", 422)

    total_cost = round(quantity * rate, 2)
    item_name = data["item"].lower()

    inv_item = get_item_price(item_name, shop_id)
    inv_id = inv_item["id"] if inv_item else None

    with get_connection() as conn:
        try:
            conn.execute(
                text("""INSERT INTO purchases
                        (shop_id, supplier_name, item_name, quantity,
                         purchase_rate, total_cost, inventory_id)
                        VALUES (:sid, :supplier, :item, :qty, :rate, :total, :inv_id)"""),
                {"sid": shop_id,
                 "supplier": (data.get("supplier") or "unknown").lower(),
                 "item": item_name, "qty": quantity, "rate": rate,
                 "total": total_cost, "inv_id": inv_id}
            )

            if inv_id:
                apply_stock_delta(conn, shop_id, item_name, quantity, inventory_id=inv_id)
            else:
                if not apply_stock_delta(conn, shop_id, item_name, quantity):
                    log.warning(f"Purchase: no inventory match for '{item_name}' — stock not updated")

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"save_purchase failed: {e}")
            return err("Purchase save nahi hua", 500)

    log.info(f"Purchase: {item_name} x{quantity} Rs.{total_cost} shop={shop_id}")
    return {"success": True, "data": data, "total_cost": total_cost}


@app.put("/purchase/edit")
def edit_purchase(req: PurchaseEditRequest,
                  authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.quantity <= 0 or req.purchase_rate <= 0:
        return err("Quantity aur rate 0 se zyada hone chahiye", 422)

    with get_connection() as conn:
        try:
            entry = conn.execute(
                text("""SELECT item_name, quantity, inventory_id
                        FROM purchases WHERE id = :pid AND shop_id = :sid"""),
                {"pid": req.purchase_id, "sid": shop_id}
            ).fetchone()
            if not entry:
                return err(f"Purchase #{req.purchase_id} nahi mila", 404)

            old_item, old_qty, inv_id = entry[0], float(entry[1]), entry[2]
            delta = float(req.quantity) - old_qty

            total_cost = round(req.quantity * req.purchase_rate, 2)
            conn.execute(
                text("""UPDATE purchases
                        SET supplier_name=:supplier, item_name=:item,
                            quantity=:qty, purchase_rate=:rate,
                            total_cost=:total, date=:date
                        WHERE id=:pid AND shop_id=:sid"""),
                {"supplier": req.supplier_name, "item": req.item_name.lower(),
                 "qty": req.quantity, "rate": req.purchase_rate,
                 "total": total_cost, "date": req.date,
                 "pid": req.purchase_id, "sid": shop_id}
            )

            if delta != 0:
                apply_stock_delta(conn, shop_id, old_item, delta, inventory_id=inv_id)

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"edit_purchase failed: {e}")
            return err("Purchase update nahi hua", 500)

    return {"success": True}


@app.delete("/purchase/{purchase_id}")
def delete_purchase(purchase_id: int,
                    authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    with get_connection() as conn:
        try:
            entry = conn.execute(
                text("""SELECT item_name, quantity, inventory_id
                        FROM purchases WHERE id = :pid AND shop_id = :sid"""),
                {"pid": purchase_id, "sid": shop_id}
            ).fetchone()
            if not entry:
                return err(f"Purchase #{purchase_id} nahi mila", 404)

            item_name, qty, inv_id = entry[0], float(entry[1]), entry[2]

            # Reverse stock (purchase added qty; delete removes it)
            apply_stock_delta(conn, shop_id, item_name, -qty, inventory_id=inv_id)

            conn.execute(
                text("DELETE FROM purchases WHERE id = :pid AND shop_id = :sid"),
                {"pid": purchase_id, "sid": shop_id}
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"delete_purchase failed: {e}")
            return err("Purchase delete nahi hua", 500)

    log.info(f"Purchase deleted: id={purchase_id} item={item_name} qty=-{qty} shop={shop_id}")
    return {"success": True}


@app.get("/purchases/all")
def get_all_purchases(limit: int = 500,
                      authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    limit = max(1, min(int(limit), 2000))
    with get_connection() as conn:
        rows = conn.execute(
            text("""SELECT id, supplier_name, item_name, quantity,
                           purchase_rate, total_cost, date
                    FROM purchases WHERE shop_id = :sid
                    ORDER BY date DESC, id DESC LIMIT :lim"""),
            {"sid": shop_id, "lim": limit}
        ).fetchall()
        grand_total = conn.execute(
            text("SELECT COALESCE(SUM(total_cost), 0) FROM purchases WHERE shop_id = :sid"),
            {"sid": shop_id}
        ).fetchone()[0]
    return {
        "purchases": [
            {"id": r[0], "supplier": r[1], "item": r[2], "quantity": r[3],
             "rate": r[4], "total": r[5], "date": str(r[6])}
            for r in rows
        ],
        "total_purchases": len(rows),
        "grand_total": grand_total,
    }


@app.post("/purchase/quick-add")
def quick_add_purchase(req: QuickPurchaseRequest,
                       authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.quantity <= 0 or req.rate <= 0:
        return err("Quantity aur Rate 0 se zyada hone chahiye", 422)

    item_name = (req.item or "").strip().lower()
    if not item_name:
        return err("Item name zaroori hai", 422)

    total_cost = round(req.quantity * req.rate, 2)
    supplier = (req.supplier or "").strip().lower() or "unknown supplier"

    with get_connection() as conn:
        try:
            conn.execute(
                text("""INSERT INTO purchases
                        (shop_id, supplier_name, item_name, quantity,
                         purchase_rate, total_cost, inventory_id)
                        VALUES (:sid, :sup, :item, :qty, :rate, :total, :inv_id)"""),
                {"sid": shop_id, "sup": supplier, "item": item_name,
                 "qty": req.quantity, "rate": req.rate,
                 "total": total_cost, "inv_id": req.inventory_id}
            )

            if req.inventory_id:
                apply_stock_delta(conn, shop_id, item_name, req.quantity,
                                  inventory_id=req.inventory_id)
            else:
                if not apply_stock_delta(conn, shop_id, item_name, req.quantity):
                    log.warning(f"Quick purchase: no inventory match for '{item_name}'")

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"quick_add_purchase failed: {e}")
            return err("Purchase save nahi hua", 500)

    return {"success": True, "total_cost": total_cost,
            "message": "Purchase saved and stock updated!"}


@app.post("/purchase/bulk-add")
def bulk_add_purchase(req: BulkPurchaseRequest,
                      authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if not req.items:
        return err("Koi item nahi mila", 422)

    supplier = (req.supplier or "").strip().lower() or "unknown supplier"
    total_bill = 0
    items_saved = 0

    with get_connection() as conn:
        try:
            for item in req.items:
                if item.quantity <= 0 or item.rate <= 0:
                    continue

                total_cost = round(item.quantity * item.rate, 2)
                total_bill += total_cost
                item_name = item.item.strip().lower()
                inv_id = item.inventory_id

                if not inv_id:
                    inv_row = conn.execute(
                        text("""SELECT id FROM inventory
                                WHERE shop_id = :sid AND LOWER(item_name) = :name"""),
                        {"sid": shop_id, "name": item_name}
                    ).fetchone()
                    if inv_row:
                        inv_id = inv_row[0]
                    else:
                        # New item — sale_price = 0 (user must set it later)
                        new_inv = conn.execute(
                            text("""INSERT INTO inventory
                                    (shop_id, item_name, sale_price, purchase_rate,
                                     stock, reorder_level)
                                    VALUES (:sid, :name, 0, :pr, 0, 5)
                                    RETURNING id"""),
                            {"sid": shop_id, "name": item.item.strip(), "pr": item.rate}
                        ).fetchone()
                        inv_id = new_inv[0]

                conn.execute(
                    text("""INSERT INTO purchases
                            (shop_id, supplier_name, item_name, quantity,
                             purchase_rate, total_cost, inventory_id)
                            VALUES (:sid, :sup, :item, :qty, :rate, :total, :inv_id)"""),
                    {"sid": shop_id, "sup": supplier, "item": item_name,
                     "qty": item.quantity, "rate": item.rate,
                     "total": total_cost, "inv_id": inv_id}
                )

                if inv_id:
                    apply_stock_delta(conn, shop_id, item_name, item.quantity,
                                      inventory_id=inv_id)

                items_saved += 1

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"bulk_add_purchase failed: {e}")
            return err("Bulk purchase save nahi hui", 500)

    log.info(f"Bulk Purchase: {supplier} - {items_saved} items, Rs.{total_bill} shop={shop_id}")
    return {"success": True, "total_cost": total_bill, "items_saved": items_saved}


# ==================== SUPPLIER ====================
@app.get("/supplier/ledger")
def supplier_ledger(supplier: str,
                    authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    supplier = supplier.strip()

    with get_connection() as conn:
        purchases = conn.execute(
            text("""SELECT date, item_name, quantity, purchase_rate, total_cost
                    FROM purchases
                    WHERE shop_id = :sid AND LOWER(supplier_name) = LOWER(:sup)
                    ORDER BY date DESC, id DESC"""),
            {"sid": shop_id, "sup": supplier}
        ).fetchall()

        payments = conn.execute(
            text("""SELECT date, amount, notes
                    FROM supplier_payments
                    WHERE shop_id = :sid AND LOWER(supplier_name) = LOWER(:sup)
                    ORDER BY date DESC, id DESC"""),
            {"sid": shop_id, "sup": supplier}
        ).fetchall()

    total_purchases = sum(float(r[4] or 0) for r in purchases)
    total_payments = sum(float(r[1] or 0) for r in payments)

    return {
        "supplier": supplier,
        "purchases": [
            {"date": str(r[0]), "item": r[1], "qty": r[2],
             "rate": r[3], "total": r[4]}
            for r in purchases
        ],
        "payments": [
            {"date": str(r[0]), "amount": r[1], "notes": r[2]}
            for r in payments
        ],
        "total_purchases": round(total_purchases, 2),
        "total_payments": round(total_payments, 2),
        "net_balance": round(total_purchases - total_payments, 2),
    }


@app.post("/supplier/payment/add")
def add_supplier_payment(req: SupplierPaymentRequest,
                         authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.amount <= 0:
        return err("Amount 0 se zyada hona chahiye", 422)
    with get_connection() as conn:
        conn.execute(
            text("""INSERT INTO supplier_payments
                    (shop_id, supplier_name, amount, notes)
                    VALUES (:sid, :sup, :amt, :notes)"""),
            {"sid": shop_id, "sup": req.supplier.lower(),
             "amt": req.amount, "notes": req.notes}
        )
        conn.commit()
    return {"success": True, "message": "Payment save ho gayi"}


@app.get("/supplier/payments/all")
def get_supplier_payments(supplier: str,
                          authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(
            text("""SELECT date, amount, notes FROM supplier_payments
                    WHERE shop_id = :sid AND LOWER(supplier_name) = LOWER(:sup)
                    ORDER BY date DESC"""),
            {"sid": shop_id, "sup": supplier.strip()}
        ).fetchall()
    return {"payments": [
        {"date": str(r[0]), "amount": r[1], "notes": r[2]}
        for r in rows
    ]}


# ==================== WHATSAPP ====================
@app.post("/whatsapp/reminder")
def whatsapp_reminder(req: ReminderRequest,
                      authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    phone, baaki = get_customer_phone_and_baaki(req.customer, shop_id)
    if phone is None and baaki == 0:
        return err(f"'{req.customer}' nahi mila", 404)
    msg = compose_reminder(get_shop_name(shop_id), req.customer, baaki)
    return {"success": True, "customer": req.customer, "baaki": baaki,
            "whatsapp": build_whatsapp_payload(phone, msg)}


@app.post("/whatsapp/bulk-reminders")
@limiter.limit("5/minute")
def whatsapp_bulk_reminders(request: Request, req: BulkReminderRequest,
                            authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    shop_name = get_shop_name(shop_id)
    with get_connection() as conn:
        rows = conn.execute(
            text("""SELECT c.name, c.phone, COALESCE(SUM(k.total), 0) as baaki
                    FROM customers c
                    LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
                    WHERE c.shop_id = :sid
                    GROUP BY c.customer_id, c.name, c.phone
                    HAVING COALESCE(SUM(k.total), 0) >= :min_baaki
                    ORDER BY baaki DESC"""),
            {"sid": shop_id, "min_baaki": req.min_baaki}
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


# ==================== EXPENSES & REPORTS ====================
@app.post("/expense/add")
def add_expense(req: ExpenseRequest,
                authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    if req.amount <= 0:
        return err("Amount 0 se zyada hona chahiye", 422)
    with get_connection() as conn:
        conn.execute(
            text("""INSERT INTO expenses (shop_id, expense_name, amount, notes)
                    VALUES (:sid, :name, :amt, :notes)"""),
            {"sid": shop_id, "name": req.expense_name,
             "amt": req.amount, "notes": req.notes}
        )
        conn.commit()
    return {"success": True, "message": "Kharcha save ho gaya"}


@app.get("/journal")
def get_journal(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(
            text("""
                SELECT date, type, party, details, amount_in, amount_out FROM (
                    SELECT k.date, 'Sale' as type, c.name as party,
                           k.item_name || ' x' || k.quantity as details,
                           k.total as amount_in, 0 as amount_out
                    FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
                    WHERE c.shop_id = :sid
                      AND k.item_name NOT IN ('PAYMENT', 'RETURN')
                      AND k.deleted_at IS NULL
                    UNION ALL
                    SELECT k.date, 'Payment In' as type, c.name as party,
                           'Payment Received' as details,
                           ABS(k.total) as amount_in, 0 as amount_out
                    FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
                    WHERE c.shop_id = :sid AND k.item_name = 'PAYMENT' AND k.deleted_at IS NULL
                    UNION ALL
                    SELECT p.date, 'Purchase' as type, p.supplier_name as party,
                           p.item_name || ' x' || p.quantity as details,
                           0 as amount_in, p.total_cost as amount_out
                    FROM purchases p WHERE p.shop_id = :sid
                    UNION ALL
                    SELECT e.date, 'Expense' as type, 'Cash' as party,
                           e.expense_name as details,
                           0 as amount_in, e.amount as amount_out
                    FROM expenses e WHERE e.shop_id = :sid
                ) AS j
                ORDER BY date DESC, type ASC
                LIMIT 500
            """),
            {"sid": shop_id}
        ).fetchall()
    return {
        "journal": [
            {"date": str(r[0]), "type": r[1], "party": r[2].title(),
             "details": r[3], "amount_in": r[4], "amount_out": r[5]}
            for r in rows
        ]
    }


@app.get("/reports/daily-closing")
def get_daily_closing(date: str = "",
                      authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    if date:
        try:
            dt.strptime(date, "%Y-%m-%d")
        except ValueError:
            return err("Date format YYYY-MM-DD hona chahiye", 422)

    d_param = {"sid": shop_id}
    if date:
        d_param["target_date"] = date
        date_cond = "k.date = :target_date"
        p_date_cond = "p.date = :target_date"
        e_date_cond = "e.date = :target_date"
    else:
        date_cond = "k.date = CURRENT_DATE"
        p_date_cond = "p.date = CURRENT_DATE"
        e_date_cond = "e.date = CURRENT_DATE"

    with get_connection() as conn:
        # Cost source: khaata.cost_price (snapshot) preferred, else current rate
        sales_rows = conn.execute(
            text(f"""
                SELECT k.item_name, k.quantity, k.price_per_item, k.total,
                       COALESCE(NULLIF(k.cost_price, 0), i.purchase_rate, 0) as cost_rate
                FROM khaata k
                JOIN customers c ON c.customer_id = k.customer_id
                LEFT JOIN inventory i ON i.id = k.inventory_id
                WHERE c.shop_id = :sid AND {date_cond}
                  AND k.item_name NOT IN ('PAYMENT', 'RETURN')
                  AND k.deleted_at IS NULL
            """),
            d_param
        ).fetchall()

        payments_rows = conn.execute(
            text(f"""
                SELECT c.name, ABS(k.total) as amount
                FROM khaata k
                JOIN customers c ON c.customer_id = k.customer_id
                WHERE c.shop_id = :sid AND {date_cond}
                  AND k.item_name = 'PAYMENT' AND k.deleted_at IS NULL
            """),
            d_param
        ).fetchall()

        purchase_rows = conn.execute(
            text(f"""SELECT item_name, quantity, purchase_rate, total_cost
                     FROM purchases p WHERE p.shop_id = :sid AND {p_date_cond}"""),
            d_param
        ).fetchall()

        expense_rows = conn.execute(
            text(f"""SELECT expense_name, amount
                     FROM expenses e WHERE e.shop_id = :sid AND {e_date_cond}"""),
            d_param
        ).fetchall()

    total_sales = 0.0
    total_profit = 0.0
    sales_items = []
    for r in sales_rows:
        item, qty, price, total, cost_rate = r
        qty_f = float(qty or 0)
        price_f = float(price or 0)
        total_f = float(total or 0)
        cost_f = float(cost_rate or 0)
        profit = (price_f - cost_f) * qty_f
        total_sales += total_f
        total_profit += profit
        sales_items.append({"item": item, "qty": qty_f, "price": price_f,
                            "total": total_f, "profit": round(profit, 2)})

    total_payments = sum(float(r[1] or 0) for r in payments_rows)
    total_purchases = sum(float(r[3] or 0) for r in purchase_rows)
    purchase_items = [{"item": r[0], "qty": r[1], "total": r[3]} for r in purchase_rows]
    total_expenses = sum(float(r[1] or 0) for r in expense_rows)
    expense_items = [{"name": r[0], "amount": r[1]} for r in expense_rows]
    daily_cash_flow = total_payments - total_purchases - total_expenses

    return {
        "date": date if date else "Aaj",
        "sales": sales_items,
        "total_sales": round(total_sales, 2),
        "total_profit": round(total_profit, 2),
        "total_payments": round(total_payments, 2),
        "purchases": purchase_items,
        "total_purchases": round(total_purchases, 2),
        "expenses": expense_items,
        "total_expenses": round(total_expenses, 2),
        "daily_cash_flow": round(daily_cash_flow, 2),
    }


@app.get("/reports/dashboard")
def reports_dashboard(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    with get_connection() as conn:
        today_sale = conn.execute(text("""
            SELECT COALESCE(SUM(k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date = CURRENT_DATE
              AND k.item_name NOT IN ('PAYMENT', 'RETURN')
              AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]

        today_payments = conn.execute(text("""
            SELECT COALESCE(SUM(-k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date = CURRENT_DATE
              AND k.item_name = 'PAYMENT' AND k.deleted_at IS NULL
        """), {"sid": shop_id}).fetchone()[0]

        month_sale = conn.execute(text("""
            SELECT COALESCE(SUM(k.total), 0) FROM khaata k
            JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.date >= date_trunc('month', CURRENT_DATE)
              AND k.item_name NOT IN ('PAYMENT', 'RETURN')
              AND k.deleted_at IS NULL
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
            {"sid": shop_id}
        ).fetchone()[0]

        top_baaki = conn.execute(text("""
            SELECT c.name, c.phone, COALESCE(SUM(k.total), 0) as baaki
            FROM customers c
            LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
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


# ==================== EXPORT & TRASH ====================
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
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT c.name, c.phone, COALESCE(SUM(k.total), 0), COUNT(k.id)
            FROM customers c
            LEFT JOIN khaata k ON k.customer_id = c.customer_id AND k.deleted_at IS NULL
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
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT c.name, k.date, k.item_name, k.quantity, k.price_per_item, k.total
            FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.deleted_at IS NULL
            ORDER BY c.name, k.date
        """), {"sid": shop_id}).fetchall()
    return _csv_response(
        [(r[0].title(), str(r[1]), r[2].title(), r[3], r[4], r[5]) for r in rows],
        ["Customer", "Date", "Item", "Quantity", "Price", "Total"], "khaata.csv")


@app.get("/export/inventory")
def export_inventory(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT item_name, category, sale_price, purchase_rate, stock, reorder_level
            FROM inventory WHERE shop_id = :sid ORDER BY item_name
        """), {"sid": shop_id}).fetchall()
    return _csv_response(
        [(r[0].title(), r[1], r[2], r[3], r[4], r[5]) for r in rows],
        ["Item", "Category", "Sale Price", "Purchase Rate", "Stock", "Reorder Level"],
        "inventory.csv")


@app.get("/trash/khaata")
def trash_khaata(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    with get_connection() as conn:
        rows = conn.execute(text("""
            SELECT k.id, c.name, k.date, k.item_name, k.quantity,
                   k.price_per_item, k.total, k.deleted_at
            FROM khaata k JOIN customers c ON c.customer_id = k.customer_id
            WHERE c.shop_id = :sid AND k.deleted_at IS NOT NULL
              AND k.deleted_at >= NOW() - INTERVAL '30 days'
            ORDER BY k.deleted_at DESC LIMIT 200
        """), {"sid": shop_id}).fetchall()
    return {
        "entries": [
            {"id": r[0], "customer": r[1].title(), "date": str(r[2]),
             "item": r[3].title(), "quantity": r[4], "price": r[5],
             "total": r[6], "deleted_at": str(r[7])}
            for r in rows
        ],
        "count": len(rows),
    }


@app.post("/trash/restore/{entry_id}")
def trash_restore(entry_id: int,
                  authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    with get_connection() as conn:
        try:
            entry = conn.execute(
                text("""SELECT k.item_name, k.quantity, k.inventory_id
                        FROM khaata k
                        JOIN customers c ON c.customer_id = k.customer_id
                        WHERE k.id = :eid AND c.shop_id = :sid
                          AND k.deleted_at IS NOT NULL"""),
                {"eid": entry_id, "sid": shop_id}
            ).fetchone()
            if not entry:
                return err(f"Entry #{entry_id} trash mein nahi mili", 404)

            item_name, qty, inv_id = entry[0], float(entry[1]), entry[2]

            conn.execute(
                text("""UPDATE khaata SET deleted_at = NULL
                        WHERE id = :eid AND customer_id IN
                            (SELECT customer_id FROM customers WHERE shop_id = :sid)"""),
                {"eid": entry_id, "sid": shop_id}
            )

            # Reverse delete's +qty → -qty (stock goes back out)
            is_stock_entry = item_name and item_name.upper() not in ("PAYMENT", "RETURN")
            if is_stock_entry and qty:
                apply_stock_delta(conn, shop_id, item_name, -qty, inventory_id=inv_id)

            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"trash_restore failed: {e}")
            return err("Restore fail hui", 500)

    log.info(f"Entry restored: id={entry_id}, stock -{qty}, shop={shop_id}")
    return {"success": True, "id": entry_id}


@app.delete("/trash/purge")
def trash_purge(authorization: Optional[str] = Header(None)):
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
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


# ==================== AGENT & WEBHOOKS ====================
@app.post("/agent/message")
@limiter.limit("30/minute")
async def agent_message(request: Request, req: AgentMessageRequest,
                        authorization: Optional[str] = Header(None)):
    if not AGENT_ENABLED:
        return err("Agent disabled", 503)
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{WORKER_URL}/agent/handle",
                json={
                    "shop_id": shop_id,
                    "channel": req.channel,
                    "external_id": req.external_id,
                    "message": req.message,
                    "customer_id": req.customer_id,
                },
                headers={"X-Worker-Key": WORKER_API_KEY},
            )
        return resp.json()
    except Exception as e:
        log.error(f"agent_message proxy failed: {e}")
        return err("Agent worker se connect nahi hua", 502)


@app.post("/agent/schedule-reminder")
async def agent_schedule_reminder(req: ReminderRequest,
                                  authorization: Optional[str] = Header(None)):
    if not AGENT_ENABLED:
        return err("Agent disabled", 503)
    shop_id = get_shop_id(authorization)
    if not shop_id:
        return auth_err()

    customer = req.customer.lower().strip()
    phone, baaki = get_customer_phone_and_baaki(customer, shop_id)
    if phone is None and baaki == 0:
        return err(f"'{req.customer}' nahi mila", 404)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{WORKER_URL}/tasks/enqueue",
                json={
                    "shop_id": shop_id,
                    "task_type": "reminder",
                    "payload": {
                        "customer_name": customer,
                        "phone": phone,
                        "baaki": float(baaki),
                        "channel": "whatsapp",
                        "scheduled_by": "manual",
                    },
                },
                headers={"X-Worker-Key": WORKER_API_KEY},
            )
        return resp.json()
    except Exception as e:
        log.error(f"agent_schedule_reminder failed: {e}")
        return err("Reminder schedule nahi hua", 502)


@app.get("/whatsapp/webhook")
def whatsapp_webhook_verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "", status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    # Hard fail if secret is missing — otherwise webhook is open to anyone
    if not WHATSAPP_APP_SECRET:
        log.error("Webhook called but WHATSAPP_APP_SECRET not set — rejecting")
        return PlainTextResponse("Webhook not configured", status_code=503)

    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        log.warning("Webhook: missing/malformed signature")
        return PlainTextResponse("Forbidden", status_code=403)

    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        log.warning("Webhook: signature mismatch")
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        body = json.loads(raw_body)
    except Exception:
        return {"status": "ignored"}

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")
                for msg in messages:
                    if msg.get("type") != "text":
                        continue
                    from_phone = msg.get("from")
                    text_body = msg.get("text", {}).get("body", "")
                    if not phone_number_id:
                        log.warning("Webhook: no phone_number_id")
                        continue

                    with get_connection() as conn:
                        row = conn.execute(
                            text("""SELECT shop_id FROM shops
                                    WHERE agent_settings->>'whatsapp_phone_id' = :pid
                                    LIMIT 1"""),
                            {"pid": phone_number_id}
                        ).fetchone()
                    shop_id = row[0] if row else None

                    if not shop_id:
                        log.warning(f"Webhook: no shop for phone_number_id={phone_number_id}")
                        continue

                    if AGENT_ENABLED:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                await client.post(
                                    f"{WORKER_URL}/agent/handle",
                                    json={
                                        "shop_id": shop_id,
                                        "channel": "whatsapp",
                                        "external_id": from_phone,
                                        "message": text_body,
                                    },
                                    headers={"X-Worker-Key": WORKER_API_KEY},
                                )
                        except Exception as e:
                            log.error(f"Worker forward failed: {e}")
    except Exception as e:
        log.error(f"Webhook parse failed: {e}")

    return {"status": "ok"}