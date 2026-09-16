# auth.py — PostgreSQL version
"""
Dukaan AI authentication.

- Passwords: bcrypt (12 rounds), legacy SHA-256 hashes ko transparently
  upgrade karta hai jab user agla baar login karta hai.
- Sessions: JWT (HS256).
"""
import os
import hmac
import hashlib
from datetime import datetime, timedelta
from jose import JWTError, jwt
import bcrypt
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.auth")

# ---------- Secret key ----------
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Purani default — kabhi production mein na rahe
    SECRET_KEY = "dukaan-ai-secret-key-2024"
    log.warning(
        "⚠️  SECRET_KEY env var set nahi hai — fallback use ho raha hai. "
        "Production mein .env mein SECRET_KEY=<long-random-string> zaroor daalein."
    )

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

# bcrypt ka max input 72 bytes hai — isse zyada bytes truncate ho jaate hain.
# Hum explicitly truncate karte hain taake 72+ bytes password pe surprise na ho.
_BCRYPT_MAX_BYTES = 72

# Legacy SHA-256 salt — sirf purane hashes verify karne ke liye
_LEGACY_SALT = "dukaan-ai-salt-2024"


# ---------- Password hashing ----------
def _to_bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Naya password → bcrypt hash (12 rounds)."""
    return bcrypt.hashpw(
        _to_bcrypt_bytes(password), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")


def _verify_legacy_sha256(plain: str, hashed: str) -> bool:
    """Purane SHA-256 hashes verify karo (migration ke liye)."""
    legacy = hashlib.sha256(
        f"{_LEGACY_SALT}{plain}{_LEGACY_SALT}".encode()
    ).hexdigest()
    # Timing-safe comparison
    return hmac.compare_digest(legacy, hashed)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Password verify karo.
    - bcrypt hash  → bcrypt.checkpw
    - purana hash  → legacy SHA-256 fallback
    """
    if not hashed:
        return False

    # bcrypt hashes "$2a$", "$2b$", ya "$2y$" se shuru hote hain
    if hashed.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(
                _to_bcrypt_bytes(plain), hashed.encode("utf-8")
            )
        except (ValueError, TypeError):
            return False

    # Legacy SHA-256
    return _verify_legacy_sha256(plain, hashed)


def needs_rehash(hashed: str) -> bool:
    """Agar hash legacy format mein hai toh login pe upgrade karna hai."""
    return not hashed.startswith(("$2a$", "$2b$", "$2y$"))


# ---------- JWT ----------
def create_token(shop_id: int, username: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    data = {"shop_id": shop_id, "username": username, "exp": expire}
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ---------- Shop auth ----------
def register_shop(username: str, password: str, shop_name: str) -> dict:
    if len(username) < 3:
        return {"error": "Username kam az kam 3 characters ka hona chahiye"}
    if len(password) < 4:
        return {"error": "Password kam az kam 4 characters ka hona chahiye"}

    with get_connection() as conn:
        existing = conn.execute(
            text("SELECT * FROM shops WHERE username = :u"),
            {"u": username.lower()}
        ).fetchone()
        if existing:
            return {"error": "Yeh username already use ho raha hai"}

        hashed = hash_password(password)
        conn.execute(
            text("INSERT INTO shops (username, password, shop_name) VALUES (:u, :p, :s)"),
            {"u": username.lower(), "p": hashed, "s": shop_name}
        )
        conn.commit()

        shop = conn.execute(
            text("SELECT * FROM shops WHERE username = :u"),
            {"u": username.lower()}
        ).fetchone()

    token = create_token(shop[0], username)
    log.info(f"New shop registered: {shop_name} ({username})")
    return {"success": True, "token": token, "shop_name": shop_name}


def login_shop(username: str, password: str) -> dict:
    with get_connection() as conn:
        shop = conn.execute(
            text("SELECT * FROM shops WHERE username = :u"),
            {"u": username.lower()}
        ).fetchone()

    if not shop or not verify_password(password, shop[2]):
        return {"error": "Username ya password galat hai"}

    # Auto-upgrade: agar legacy SHA-256 hash tha, ab bcrypt mein badlo
    if needs_rehash(shop[2]):
        try:
            new_hash = hash_password(password)
            with get_connection() as conn:
                conn.execute(
                    text("UPDATE shops SET password = :p WHERE shop_id = :sid"),
                    {"p": new_hash, "sid": shop[0]}
                )
                conn.commit()
            log.info(f"Password hash upgraded to bcrypt: {username}")
        except Exception as e:
            # Upgrade fail ho jaye toh bhi login chalne do — agli baar try hoga
            log.warning(f"Password rehash failed for {username}: {e}")

    token = create_token(shop[0], username)
    log.info(f"Login: {shop[3]} ({username})")
    return {"success": True, "token": token, "shop_name": shop[3]}


def verify_shop_identity(username: str, shop_name: str) -> bool:
    with get_connection() as conn:
        shop = conn.execute(
            text("SELECT * FROM shops WHERE username = :u AND LOWER(shop_name) = :s"),
            {"u": username.lower(), "s": shop_name.lower().strip()}
        ).fetchone()
    return shop is not None


def reset_password(username: str, shop_name: str, new_password: str) -> dict:
    if not verify_shop_identity(username, shop_name):
        return {"error": "Username ya shop name galat hai"}
    if len(new_password) < 4:
        return {"error": "Password kam az kam 4 characters ka hona chahiye"}

    hashed = hash_password(new_password)
    with get_connection() as conn:
        conn.execute(
            text("UPDATE shops SET password = :p WHERE username = :u"),
            {"p": hashed, "u": username.lower()}
        )
        conn.commit()

    log.info(f"Password reset: {username}")
    return {"success": True, "message": "Password reset ho gaya — ab login karo"}