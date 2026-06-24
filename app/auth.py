"""Password hashing, session-based login, and credential encryption at rest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.models import User

_PBKDF2_ITERATIONS = 390_000


# ── Password hashing ─────────────────────────────────────────

def hash_password(plain: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        salt_hex, digest_hex = password_hash.split("$", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# ── Credential encryption (provider passwords at rest) ──────

def _fernet() -> Fernet:
    if not settings.SECRET_KEY:
        raise RuntimeError("SECRET_KEY must be set in .env to encrypt/decrypt provider credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_credential(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_credential(token: str) -> str:
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


# ── Session-based auth ───────────────────────────────────────

SESSION_USER_KEY = "user_id"


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    async with async_session() as session:
        return await session.get(User, user_id)


async def require_login(request: Request) -> User:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    return user


async def get_user_by_email(email: str) -> User | None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()
