"""Password hashing, session-based login, and credential encryption at rest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict

from cryptography.fernet import Fernet
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.models import User

# OWASP's current PBKDF2-HMAC-SHA256 recommendation. The iteration count is embedded in
# every new hash (format: "iterations$salt$digest"), so raising this later never invalidates
# existing users' passwords — verify_password always re-derives using whatever count is
# stored in *their* hash, not this constant.
_PBKDF2_ITERATIONS = 600_000
_LEGACY_PBKDF2_ITERATIONS = 390_000  # used by hashes from before iteration-count embedding


# ── Password hashing ─────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Slow, salted one-way hash. A random 16-byte salt per user means equal passwords get
    different hashes, and precomputed (rainbow) tables are useless. Blocking CPU work: call
    it via asyncio.to_thread from async code."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, password_hash: str) -> bool:
    """Re-hash `plain` with the salt and iteration count stored in `password_hash` and
    compare. Any malformed hash → False (never an exception)."""
    parts = password_hash.split("$")
    if len(parts) == 3:
        iterations_str, salt_hex, digest_hex = parts
        try:
            iterations = int(iterations_str)
        except ValueError:
            return False
    elif len(parts) == 2:
        # Legacy format from before the iteration count was stored per-hash.
        iterations = _LEGACY_PBKDF2_ITERATIONS
        salt_hex, digest_hex = parts
    else:
        return False

    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    # Constant-time comparison: timing can't reveal how many leading bytes matched.
    return hmac.compare_digest(actual, expected)


# ── Credential encryption (provider passwords at rest) ──────

def _fernet() -> Fernet:
    """Fernet = AES encryption + HMAC (tamper-evident), keyed from SECRET_KEY.
    Changing SECRET_KEY makes every stored credential undecryptable."""
    # ponytail: one app-wide key derived with plain SHA-256; MultiFernet if keys need rotating.
    if not settings.SECRET_KEY:
        raise RuntimeError("SECRET_KEY must be set in .env to encrypt/decrypt provider credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_credential(plain: str) -> str:
    """Reversible on purpose: provider passwords must be decrypted again to log in."""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_credential(token: str) -> str:
    """Raises cryptography.fernet.InvalidToken if the data was tampered with or SECRET_KEY changed."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


# ── Session-based auth ───────────────────────────────────────

SESSION_USER_KEY = "user_id"  # the only thing stored in the (signed) session cookie


async def get_current_user(request: Request) -> User | None:
    """The logged-in User, or None. The cookie is signed with SECRET_KEY, so its user id
    can't be forged — but it can be replayed until it expires (no server-side session)."""
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    async with async_session() as session:
        return await session.get(User, user_id)


async def require_login(request: Request) -> User:
    """FastAPI dependency: `user: User = Depends(require_login)` → 401 if not logged in."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    return user


async def get_user_by_email(email: str) -> User | None:
    """Exact match — callers pass the lowercased email."""
    async with async_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


# ── Login rate limiting ──────────────────────────────────────
# In-memory sliding window, keyed by client IP (not email — rate-limiting by email would let
# an attacker lock a real user out of their own account just by spamming wrong passwords for
# their address from elsewhere). Fine for this app's single-machine deployment; would need a
# shared store (e.g. Redis) if this ever runs as more than one instance.

_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 900  # 15 minutes
_login_attempts: dict[str, list[float]] = defaultdict(list)


def check_login_rate_limit(request: Request) -> bool:
    """Return True if this client may attempt a login right now, False if rate-limited."""
    # Behind a reverse proxy this is the proxy's address unless uvicorn trusts X-Forwarded-For.
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _login_attempts[client_ip]
    attempts[:] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]  # drop expired attempts
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return False
    attempts.append(now)
    return True
