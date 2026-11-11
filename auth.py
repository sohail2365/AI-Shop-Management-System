# auth.py — PostgreSQL version (Bcrypt secured)
import os
import hmac
import hashlib
import bcrypt
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.auth")

# ---- SECRET_KEY: no default, strict validation ----
SECRET_KEY = os.getenv("SECRET_KEY", "")
_KNOWN_BAD_KEYS = {
    "",
    "change-this-to-a-random-long-string",
    "dukaan-ai-secret-key-2024",
}
if SECRET_KEY in _KNOWN_BAD_KEYS:
    raise ValueError(
        "❌ SECRET_KEY .env mein set nahi hai ya default hai.\n"
        "Generate: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )
if len(SECRET_KEY) < 32:
    raise ValueError(
        f"❌ SECRET_KEY kam az kam 32 characters ka hona chahiye (abhi {len(SECRET_KEY)})"
    )

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24
BCRYPT_ROUNDS = 12
LEGACY_SALT = "dukaan-ai-salt-2024"


# =========================================================
# Password hashing
# =========================================================

def _is_bcrypt(h: str) -> bool:
    return bool(h) and h.startswith(("$2a$", "$2b$", "$2y$"))


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password kam az kam 8 characters ka hona chahiye")
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def _legacy_sha256(password: str) -> str:
    return hashlib.sha256(
        f"{LEGACY_SALT}{password}{LEGACY_SALT}".encode()
    ).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    if _is_bcrypt(hashed):
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    # Legacy SHA-256 (pre-bcrypt) — timing-safe compare
    return hmac.compare_digest(_legacy_sha256(plain), hashed)


# =========================================================
# JWT
# =========================================================

def create_token(shop_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    data = {"shop_id": shop_id, "username": username, "exp": expire}
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# =========================================================
# Shop registration / login
# =========================================================

def register_shop(username: str, password: str, shop_name: str) -> dict:
    username = (username or "").strip().lower()
    shop_name = (shop_name or "").strip()
    if len(username) < 3:
        return {"error": "Username kam az kam 3 characters ka hona chahiye"}
    if len(password) < 8:
        return {"error": "Password kam az kam 8 characters ka hona chahiye"}
    if not shop_name:
        return {"error": "Shop name zaroori hai"}

    with get_connection() as conn:
        existing = conn.execute(
            text("SELECT 1 FROM shops WHERE username = :u"),
            {"u": username},
        ).fetchone()
        if existing:
            return {"error": "Yeh username already use ho raha hai"}

        hashed = hash_password(password)
        conn.execute(
            text("""INSERT INTO shops (username, password, shop_name)
                    VALUES (:u, :p, :s)"""),
            {"u": username, "p": hashed, "s": shop_name},
        )
        conn.commit()

        shop = conn.execute(
            text("SELECT shop_id, shop_name FROM shops WHERE username = :u"),
            {"u": username},
        ).fetchone()

    token = create_token(shop[0], username)
    log.info(f"New shop registered: {shop_name} ({username})")
    return {"success": True, "token": token, "shop_name": shop[1]}


def login_shop(username: str, password: str) -> dict:
    username = (username or "").strip().lower()
    with get_connection() as conn:
        shop = conn.execute(
            text("""SELECT shop_id, username, password, shop_name
                    FROM shops WHERE username = :u"""),
            {"u": username},
        ).fetchone()

    if not shop or not verify_password(password, shop[2]):
        return {"error": "Username ya password galat hai"}

    # One-time bcrypt upgrade (race-safe: only if still legacy)
    if not _is_bcrypt(shop[2]):
        new_hashed = hash_password(password)
        with get_connection() as conn:
            conn.execute(
                text("""UPDATE shops SET password = :new
                        WHERE shop_id = :sid AND password = :old"""),
                {"new": new_hashed, "sid": shop[0], "old": shop[2]},
            )
            conn.commit()
        log.info(f"Password hash upgraded to bcrypt: {username}")

    token = create_token(shop[0], username)
    log.info(f"Login: {shop[3]} ({username})")
    return {"success": True, "token": token, "shop_name": shop[3]}


def verify_shop_identity(username: str, shop_name: str) -> bool:
    with get_connection() as conn:
        shop = conn.execute(
            text("""SELECT 1 FROM shops
                    WHERE username = :u AND LOWER(shop_name) = :s"""),
            {"u": (username or "").strip().lower(), "s": (shop_name or "").strip().lower()},
        ).fetchone()
    return shop is not None


def reset_password(username: str, shop_name: str, new_password: str) -> dict:
    if not verify_shop_identity(username, shop_name):
        return {"error": "Username ya shop name galat hai"}
    if len(new_password) < 8:
        return {"error": "Password kam az kam 8 characters ka hona chahiye"}

    hashed = hash_password(new_password)
    with get_connection() as conn:
        conn.execute(
            text("UPDATE shops SET password = :p WHERE username = :u"),
            {"p": hashed, "u": username.strip().lower()},
        )
        conn.commit()

    log.info(f"Password reset: {username}")
    return {"success": True, "message": "Password reset ho gaya — ab login karo"}