# auth.py — PostgreSQL version (Bcrypt secured)
import os
import bcrypt
from datetime import datetime, timedelta
from jose import JWTError, jwt
from sqlalchemy import text
from database import get_connection
from logger import get_logger

log = get_logger("dukaan.auth")

SECRET_KEY = os.getenv("SECRET_KEY", "dukaan-ai-secret-key-2024")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except ValueError:
        # Purane SHA-256 hashes ke liye backward compatibility
        import hashlib
        salt = "dukaan-ai-salt-2024"
        legacy_hash = hashlib.sha256(f"{salt}{plain}{salt}".encode()).hexdigest()
        return legacy_hash == hashed

def create_token(shop_id: int, username: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    data = {"shop_id": shop_id, "username": username, "exp": expire}
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

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

    # Auto-upgrade: agar purana SHA-256 hash hai, toh login pe bcrypt mein badal do
    import hashlib
    salt = "dukaan-ai-salt-2024"
    legacy_hash = hashlib.sha256(f"{salt}{password}{salt}".encode()).hexdigest()
    if shop[2] == legacy_hash:
        new_hashed = hash_password(password)
        with get_connection() as conn:
            conn.execute(
                text("UPDATE shops SET password = :p WHERE shop_id = :sid"),
                {"p": new_hashed, "sid": shop[0]}
            )
            conn.commit()
        log.info(f"Password hash upgraded to bcrypt: {username}")

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