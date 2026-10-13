"""FastAPI application — routes, startup wiring, template rendering.

Request flow: SessionMiddleware reads the signed cookie → a route runs; JSON routes take
`Depends(require_login)` (401 if not logged in) and a Pydantic body model (422 if invalid).
Every query that touches a watch is scoped to the logged-in user (see _get_owned_watch).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Literal, get_args

from fastapi import Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import select, desc, text
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    SESSION_USER_KEY,
    check_login_rate_limit,
    decrypt_credential,
    encrypt_credential,
    get_current_user,
    get_user_by_email,
    hash_password,
    require_login,
    verify_password,
)
from app.config import settings
from app.db import async_session, init_db
from app import google_oauth
from app.models import AvailabilitySnapshot, Credential, User, Watch
from app.scrapers.mock import MockScraper
from app.scrapers.base import ScraperError
from app import notifier
from app import scheduler as sched_module
from app import telegram as telegram_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx logs every request URL at INFO — the Telegram Bot API URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# The allowed values live in these Literal types: Pydantic enforces them on input, and
# PROVIDERS (the same list) drives the dashboard's credential forms.
ProviderName = Literal["tlscontact", "vfs", "bls"]
VisaType = Literal["tourism", "business", "long_stay"]
PROVIDERS = list(get_args(ProviderName))


# ── Request bodies (validated by FastAPI; invalid input → 422, never a 500) ──

OptionalDate = Annotated[date | None, BeforeValidator(lambda v: v or None)]  # "" / null → no date


class WatchCreate(BaseModel):
    centre: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    visa_type: VisaType
    provider: ProviderName
    booking_url: str = Field(default="", max_length=500)
    alert_before_date: OptionalDate = None


class WatchUpdate(BaseModel):
    alert_before_date: OptionalDate = None  # null or "" clears the cutoff


class CredentialIn(BaseModel):
    provider: ProviderName
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=200)


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once per process: everything before `yield` at startup, after it at shutdown."""
    # Startup — create missing tables, then start the background jobs on this event loop
    logger.info("Starting Schengen Appointment Tracker...")
    await init_db()
    sched_module.start_scheduler()
    telegram_module.start_polling()
    logger.info("App ready — mock mode: %s", settings.USE_MOCK_SCRAPER)
    yield
    # Shutdown
    sched_module.stop_scheduler()
    telegram_module.stop_polling()
    logger.info("Shutdown complete")


# ── App ──────────────────────────────────────────────────────

app = FastAPI(title="Schengen Appointment Tracker", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Same {"ok", "message"} shape the dashboard already shows. Only field + reason:
    # FastAPI's default body echoes the input back, which could include a password.
    err = exc.errors()[0]
    field = ".".join(str(part) for part in err["loc"] if part != "body")
    return JSONResponse({"ok": False, "message": f"{field}: {err['msg']}" if field else err["msg"]}, 422)


# Fail fast at import: without SECRET_KEY, sessions can't be signed or credentials decrypted.
if not settings.SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in .env (used for sessions + credential encryption)")
# FLY_APP_NAME is set automatically by the Fly.io runtime — absent in local dev, where the
# session cookie must still work over plain http://localhost.
_running_on_fly = bool(os.environ.get("FLY_APP_NAME"))
# Session = a signed (not encrypted) cookie holding only the user id. same_site="lax" stops
# other sites from sending it on cross-site POST/PATCH/DELETE requests (CSRF).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    same_site="lax",
    https_only=_running_on_fly,
)

_app_dir = pathlib.Path(__file__).parent
app.mount("/static", StaticFiles(directory=_app_dir / "static"), name="static")
templates = Jinja2Templates(directory=_app_dir / "templates")


# ── Helpers ──────────────────────────────────────────────────

def _current_months() -> list[str]:
    """Return the next 3 month labels like ['Jun 2026', 'Jul 2026', 'Aug 2026']."""
    today = date.today()
    months = []
    for i in range(3):
        d = today.replace(day=1) + timedelta(days=32 * i)
        d = d.replace(day=1)
        months.append(d.strftime("%b %Y"))
    return months


async def _get_latest_snapshots(session, watches) -> dict[int, AvailabilitySnapshot]:
    """Return the latest snapshot for each given watch, keyed by watch_id."""
    # ponytail: one query per watch (N+1); a single window-function query if users get many watches.
    snapshots: dict[int, AvailabilitySnapshot] = {}
    for watch in watches:
        stmt = (
            select(AvailabilitySnapshot)
            .where(AvailabilitySnapshot.watch_id == watch.id)
            .order_by(desc(AvailabilitySnapshot.checked_at))
            .limit(1)
        )
        result = await session.execute(stmt)
        snap = result.scalar_one_or_none()
        if snap:
            snapshots[watch.id] = snap

    return snapshots


def _snapshot_to_dict(snap: AvailabilitySnapshot | None) -> dict | None:
    """JSON-safe snapshot for /api/state (dates as ISO strings)."""
    if snap is None:
        return None
    return {
        "earliest_date": snap.earliest_date.isoformat() if snap.earliest_date else None,
        "slots_json": snap.slots_json,
        "checked_at": snap.checked_at.isoformat() if snap.checked_at else None,
        "is_error": snap.is_error,
        "error_message": snap.error_message,
    }


def _watch_to_dict(w: Watch) -> dict:
    """JSON-safe watch for the API (dates as ISO strings)."""
    return {
        "id": w.id,
        "centre": w.centre,
        "destination": w.destination,
        "visa_type": w.visa_type,
        "provider": w.provider,
        "enabled": w.enabled,
        "booking_url": w.booking_url,
        "alert_before_date": w.alert_before_date.isoformat() if w.alert_before_date else None,
        "last_checked_at": w.last_checked_at.isoformat() if w.last_checked_at else None,
        "last_error": w.last_error,
        "backoff_until": w.backoff_until.isoformat() if w.backoff_until else None,
        "flag_emoji": w.flag_emoji,
    }


async def _get_user_watches(session, user_id: int) -> list[Watch]:
    """All of one user's watches, alphabetical by destination."""
    result = await session.execute(
        select(Watch).where(Watch.user_id == user_id).order_by(Watch.destination)
    )
    return list(result.scalars().all())


async def _get_owned_watch(session, watch_id: int, user_id: int) -> Watch | None:
    """The watch, or None if it doesn't exist OR belongs to someone else.

    Callers answer both cases with the same 404, so nobody can probe which ids exist.
    """
    watch = await session.get(Watch, watch_id)
    if watch is None or watch.user_id != user_id:
        return None
    return watch


def _is_admin(user: User) -> bool:
    """ADMIN_EMAIL may use the process-wide /debug/mock-mode switch (and, in mock mode, is
    the only account that receives email)."""
    return settings.is_admin_email(user.email)


# ── Auth routes ──────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Registration form (already logged in → dashboard)."""
    if await get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="register.html", context={"error": None})


@app.post("/register")
async def register_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    """Create the account, log it in (session cookie), redirect to the dashboard."""
    email = email.strip().lower()  # one canonical form, so "A@x.com" and "a@x.com" are one account
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Password must be at least 8 characters."},
            status_code=400,
        )
    if await get_user_by_email(email) is not None:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "An account with that email already exists."},
            status_code=400,
        )

    async with async_session() as session:
        # PBKDF2 is deliberately slow CPU work — keep it off the event loop.
        user = User(email=email, password_hash=await asyncio.to_thread(hash_password, password))
        session.add(user)
        await session.commit()
        await session.refresh(user)

    request.session[SESSION_USER_KEY] = user.id  # SessionMiddleware signs this into the cookie
    return RedirectResponse("/", status_code=303)  # 303: browser follows with a GET


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login form (already logged in → dashboard)."""
    if await get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    """Check the password and start a session. Same message for unknown email and wrong password."""
    if not check_login_rate_limit(request):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Too many login attempts. Please wait a few minutes and try again."},
            status_code=429,
        )

    user = await get_user_by_email(email.strip().lower())
    if user is None or not await asyncio.to_thread(verify_password, password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password."},
            status_code=400,
        )

    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    """Drop the user id from the session cookie."""
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse("/login", status_code=303)


# ── App routes ───────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    """Unauthenticated health check for the platform: DB reachable and scheduler running.
    A 503 makes Fly restart the machine, which also restarts the scheduler."""
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check: database unreachable")
        return JSONResponse({"status": "error", "reason": "database"}, 503)
    if not sched_module.scheduler.running:
        return JSONResponse({"status": "error", "reason": "scheduler"}, 503)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page with Jinja2."""
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    async with async_session() as session:
        watches = await _get_user_watches(session, user.id)
        snapshots = await _get_latest_snapshots(session, watches)
        cred_result = await session.execute(select(Credential).where(Credential.user_id == user.id))
        configured_providers = {c.provider for c in cred_result.scalars().all()}

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "watches": watches,
            "snapshots": snapshots,
            "settings": settings,
            "current_months": _current_months(),
            "mock_mode": MockScraper.current_mode(),
            "providers": PROVIDERS,
            "configured_providers": configured_providers,
            "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN),
            "telegram_linked": bool(user.telegram_chat_id),
            "google_oauth_configured": bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET),
            "google_linked": bool(user.google_refresh_token),
            "google_email": user.google_email,
            "is_admin": _is_admin(user),
        },
    )


@app.get("/api/state")
async def api_state(user: User = Depends(require_login)):
    """JSON endpoint for auto-refresh — returns the current user's watches + latest snapshots."""
    async with async_session() as session:
        watches = await _get_user_watches(session, user.id)
        snapshots = await _get_latest_snapshots(session, watches)

    return {
        "watches": [_watch_to_dict(w) for w in watches],
        "snapshots": {
            str(wid): _snapshot_to_dict(s) for wid, s in snapshots.items()
        },
        "current_months": _current_months(),
    }


@app.post("/api/watch")
async def create_watch(body: WatchCreate, user: User = Depends(require_login)):
    """Create a new watch owned by the current user."""
    async with async_session() as session:
        # body is already validated; its fields map 1:1 onto Watch columns
        watch = Watch(user_id=user.id, enabled=True, **body.model_dump())
        session.add(watch)
        await session.commit()
        await session.refresh(watch)

    return {"ok": True, "watch": _watch_to_dict(watch)}


@app.patch("/api/watch/{watch_id}")
async def update_watch(watch_id: int, body: WatchUpdate, user: User = Depends(require_login)):
    """Update a watch's alert_before_date — pass null/empty string to clear it."""
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"ok": False, "message": "Watch not found"}, 404)
        watch.alert_before_date = body.alert_before_date
        await session.commit()
        return {"ok": True, "watch": _watch_to_dict(watch)}


@app.delete("/api/watch/{watch_id}")
async def delete_watch(watch_id: int, user: User = Depends(require_login)):
    """Delete a watch; its snapshots, alert log and outbox rows go with it (ORM cascade)."""
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"ok": False, "message": "Watch not found"}, 404)
        await session.delete(watch)
        await session.commit()
        return {"ok": True}


@app.post("/api/watch/{watch_id}/toggle")
async def toggle_watch(watch_id: int, user: User = Depends(require_login)):
    """Toggle a watch's enabled state."""
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"ok": False, "message": "Watch not found"}, 404)
        watch.enabled = not watch.enabled
        await session.commit()
        return {"ok": True, "enabled": watch.enabled}


@app.get("/api/credentials")
async def list_credentials(user: User = Depends(require_login)):
    """List which providers the current user has credentials configured for (never returns the password)."""
    async with async_session() as session:
        result = await session.execute(select(Credential).where(Credential.user_id == user.id))
        creds = result.scalars().all()
    return {
        "providers": {
            provider: next((c.email for c in creds if c.provider == provider), None)
            for provider in PROVIDERS
        }
    }


@app.post("/api/credentials")
async def upsert_credentials(body: CredentialIn, user: User = Depends(require_login)):
    """Save (or replace) a provider login for the current user. Password is encrypted at rest."""
    provider, email, password = body.provider, body.email, body.password
    async with async_session() as session:
        result = await session.execute(
            select(Credential).where(Credential.user_id == user.id, Credential.provider == provider)
        )
        credential = result.scalar_one_or_none()
        # Encrypted (not hashed): the scraper needs the original password to log in for the user.
        encrypted = encrypt_credential(password)
        if credential is None:
            credential = Credential(user_id=user.id, provider=provider, email=email, encrypted_password=encrypted)
            session.add(credential)
        else:
            credential.email = email
            credential.encrypted_password = encrypted
        await session.commit()

    return {"ok": True}


@app.post("/api/telegram/link")
async def telegram_link(user: User = Depends(require_login)):
    """Generate a one-time code for the current user to send the bot via /start <code>."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return JSONResponse({"ok": False, "message": "Telegram is not configured on this server"}, 400)

    code = telegram_module.generate_link_code()
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        db_user.telegram_link_code = code
        await session.commit()

    return {"ok": True, "code": code, "bot_username": settings.TELEGRAM_BOT_USERNAME}


@app.get("/api/telegram/status")
async def telegram_status(user: User = Depends(require_login)):
    """Polled by the dashboard after showing the link code, until the bot has linked the chat."""
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        return {"linked": bool(db_user.telegram_chat_id)}


@app.post("/api/telegram/unlink")
async def telegram_unlink(user: User = Depends(require_login)):
    """Stop Telegram alerts for this user (forget the chat id and any pending link code)."""
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        db_user.telegram_chat_id = None
        db_user.telegram_link_code = None
        await session.commit()
    return {"ok": True}


@app.get("/oauth/google/start")
async def google_oauth_start(request: Request, user: User = Depends(require_login)):
    """Redirect the user to Google's consent screen to connect their own Gmail for sending."""
    if not (settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET):
        return JSONResponse({"ok": False, "message": "Google OAuth is not configured on this server"}, 400)

    # Random `state` stored in the session and checked on the callback: proves the callback
    # answers a flow this browser started (OAuth CSRF protection).
    state = secrets.token_urlsafe(16)
    request.session["google_oauth_state"] = state
    auth_url = google_oauth.build_auth_url(state)
    return RedirectResponse(auth_url, status_code=303)


@app.get("/oauth/google/callback")
async def google_oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    """Handle Google's redirect back after consent: exchange the code, store the refresh token."""
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    expected_state = request.session.pop("google_oauth_state", None)
    if not state or state != expected_state:
        return JSONResponse({"ok": False, "message": "Invalid OAuth state"}, 400)
    if not code:
        return JSONResponse({"ok": False, "message": "Google did not return an authorization code"}, 400)

    try:
        refresh_token, email = await google_oauth.exchange_code(code)
    except Exception as e:
        logger.exception("Google OAuth exchange failed")
        return JSONResponse({"ok": False, "message": f"Google OAuth failed: {e}"}, 400)

    async with async_session() as session:
        db_user = await session.get(User, user.id)
        db_user.google_refresh_token = encrypt_credential(refresh_token)
        db_user.google_email = email
        await session.commit()

    return RedirectResponse("/", status_code=303)


@app.get("/api/google/status")
async def google_status(user: User = Depends(require_login)):
    """Whether this user's Gmail is connected for sending, and which address."""
    return {"linked": bool(user.google_refresh_token), "email": user.google_email}


@app.post("/api/google/unlink")
async def google_unlink(user: User = Depends(require_login)):
    """Revoke the token at Google (so a leaked copy dies too), then forget it locally."""
    if user.google_refresh_token:
        try:
            await google_oauth.revoke_token(decrypt_credential(user.google_refresh_token))
        except Exception:
            logger.exception("Failed to revoke Google token with Google (unlinking locally anyway)")

    async with async_session() as session:
        db_user = await session.get(User, user.id)
        db_user.google_refresh_token = None
        db_user.google_email = None
        await session.commit()
    return {"ok": True}


@app.post("/api/test-email")
async def test_email(user: User = Depends(require_login)):
    """Send a test email — via the user's connected Gmail if linked, else the operator's SMTP/Resend account."""
    # Mock mode = demo: only the operator may make the server send email (registration is
    # open and emails are unverified, so anyone else could aim it at any address).
    if settings.USE_MOCK_SCRAPER and not _is_admin(user):
        return JSONResponse({"ok": False, "message": "Email is disabled for this account in demo (mock) mode"}, 403)
    google_refresh_token = decrypt_credential(user.google_refresh_token) if user.google_refresh_token else None
    result = await notifier.send_test_email(user.email, google_refresh_token=google_refresh_token)
    return result


@app.post("/api/check-now")
async def check_now(user: User = Depends(require_login)):
    """Trigger an immediate check of the current user's enabled watches (no jitter — manual trigger)."""
    results = await sched_module.check_all_watches(jitter=False, user_id=user.id)
    return {"ok": True, "results": results}


@app.post("/api/check-now/{watch_id}")
async def check_now_single(watch_id: int, user: User = Depends(require_login)):
    """Trigger an immediate check of a single watch owned by the current user."""
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"ok": False, "message": "Watch not found or disabled"}, 404)

    result = await sched_module.check_one_watch(watch_id)
    if result is None:
        return JSONResponse({"ok": False, "message": "Watch not found or disabled"}, 404)
    return {"ok": True, "result": result}


@app.get("/debug/scrape")
async def debug_scrape(watch_id: int, user: User = Depends(require_login)):
    """Run one scrape for a watch owned by the current user and return raw JSON — for debugging."""
    # Nothing is stored: no snapshot, no alert — just the raw scraper output.
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"error": "Watch not found"}, 404)
        try:
            scraper = await sched_module.build_scraper(session, watch)
        except ScraperError as e:  # no scraper registered for this provider/route
            return JSONResponse({"watch_id": watch_id, "error": "ScraperError", "message": str(e)}, 503)

    try:
        slots = await scraper.fetch()
        return {
            "watch_id": watch_id,
            "scraper": repr(scraper),
            "slots": [
                {
                    "date": s.appt_date.isoformat(),
                    "count": s.count,
                    "booking_url": s.booking_url,
                }
                for s in slots
            ],
            "count": len(slots),
        }
    except ScraperError as e:
        return JSONResponse(
            {"watch_id": watch_id, "error": "ScraperError", "message": str(e)},
            status_code=503,
        )


@app.post("/debug/mock-mode")
async def set_mock_mode(request: Request, user: User = Depends(require_login)):
    """Switch mock scraper mode: normal / empty / error (global dev/sandbox toggle).

    This flips shared process-wide state affecting every user's watches, so it's
    restricted to ADMIN_EMAIL — never safe to expose to arbitrary logged-in users
    on a multi-tenant deployment.
    """
    if not _is_admin(user):
        return JSONResponse({"ok": False, "message": "Not authorized"}, 403)

    body = await request.json()
    mode = body.get("mode", "normal")

    MockScraper.reset()
    if mode == "empty":
        MockScraper.set_force_empty(True)
    elif mode == "error":
        MockScraper.set_force_error(True, "Simulated block for testing")

    return {"ok": True, "mode": mode}
