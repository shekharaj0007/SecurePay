import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import ApiKey, Merchant, User

API_KEY_PREFIX = "sp_test_"

JWT_SECRET = os.environ.get("JWT_SECRET", "securepay-dev-secret-change-me")
JWT_ALG = "HS256"
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    iterations = 120_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(hours=12),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def _user_from_jwt(db: Session, token: str) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def _user_from_api_key(db: Session, secret: str) -> User:
    row = db.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == hash_api_key(secret),
            ApiKey.revoked_at.is_(None),
        )
    )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    merchant = db.get(Merchant, row.merchant_id)
    if merchant is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    user = db.get(User, merchant.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return user


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _user_from_jwt(db, creds.credentials.strip())


def get_user_or_api_key(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """JWT for the console, or a merchant API key, for POST /api/payments only."""
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = creds.credentials.strip()
    if token.startswith(API_KEY_PREFIX):
        return _user_from_api_key(db, token)
    return _user_from_jwt(db, token)


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
