"""FastAPI application — routes, startup wiring, template rendering."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    SESSION_USER_KEY,
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
from app.scrapers.registry import get_scraper
from app.scrapers.base import ScraperError
from app import notifier
from app import scheduler as sched_module
from app import telegram as telegram_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROVIDERS = ["tlscontact", "vfs", "bls"]


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
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

if not settings.SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in .env (used for sessions + credential encryption)")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

import pathlib

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
    result = await session.execute(
        select(Watch).where(Watch.user_id == user_id).order_by(Watch.destination)
    )
    return list(result.scalars().all())


async def _get_owned_watch(session, watch_id: int, user_id: int) -> Watch | None:
    watch = await session.get(Watch, watch_id)
    if watch is None or watch.user_id != user_id:
        return None
    return watch


# ── Auth routes ──────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if await get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="register.html", context={"error": None})


@app.post("/register")
async def register_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
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
        user = User(email=email, password_hash=hash_password(password))
        session.add(user)
        await session.commit()
        await session.refresh(user)

    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = await get_user_by_email(email.strip().lower())
    if user is None or not verify_password(password, user.password_hash):
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
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse("/login", status_code=303)


# ── App routes ───────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness check for the deployment platform."""
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
            "now": datetime.utcnow(),
            "mock_mode": MockScraper.current_mode(),
            "providers": PROVIDERS,
            "configured_providers": configured_providers,
            "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN),
            "telegram_linked": bool(user.telegram_chat_id),
            "telegram_bot_username": settings.TELEGRAM_BOT_USERNAME,
            "google_oauth_configured": bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET),
            "google_linked": bool(user.google_refresh_token),
            "google_email": user.google_email,
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
async def create_watch(request: Request, user: User = Depends(require_login)):
    """Create a new watch owned by the current user."""
    body = await request.json()
    required = ("centre", "destination", "visa_type", "provider")
    if any(not body.get(f) for f in required):
        return JSONResponse({"ok": False, "message": "centre, destination, visa_type, provider are required"}, 400)

    alert_before_date = None
    if body.get("alert_before_date"):
        try:
            alert_before_date = date.fromisoformat(body["alert_before_date"])
        except ValueError:
            return JSONResponse({"ok": False, "message": "alert_before_date must be an ISO date (YYYY-MM-DD)"}, 400)

    async with async_session() as session:
        watch = Watch(
            user_id=user.id,
            centre=body["centre"],
            destination=body["destination"],
            visa_type=body["visa_type"],
            provider=body["provider"],
            booking_url=body.get("booking_url", ""),
            alert_before_date=alert_before_date,
            enabled=True,
        )
        session.add(watch)
        await session.commit()
        await session.refresh(watch)

    return {"ok": True, "watch": _watch_to_dict(watch)}


@app.patch("/api/watch/{watch_id}")
async def update_watch(watch_id: int, request: Request, user: User = Depends(require_login)):
    """Update a watch's alert_before_date — pass null/empty string to clear it."""
    body = await request.json()
    raw_date = body.get("alert_before_date")

    alert_before_date = None
    if raw_date:
        try:
            alert_before_date = date.fromisoformat(raw_date)
        except ValueError:
            return JSONResponse({"ok": False, "message": "alert_before_date must be an ISO date (YYYY-MM-DD)"}, 400)

    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"ok": False, "message": "Watch not found"}, 404)
        watch.alert_before_date = alert_before_date
        await session.commit()
        return {"ok": True, "watch": _watch_to_dict(watch)}


@app.delete("/api/watch/{watch_id}")
async def delete_watch(watch_id: int, user: User = Depends(require_login)):
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
async def upsert_credentials(request: Request, user: User = Depends(require_login)):
    """Save (or replace) a provider login for the current user. Password is encrypted at rest."""
    body = await request.json()
    provider = body.get("provider", "")
    email = body.get("email", "")
    password = body.get("password", "")
    if provider not in PROVIDERS or not email or not password:
        return JSONResponse({"ok": False, "message": "provider, email, and password are required"}, 400)

    async with async_session() as session:
        result = await session.execute(
            select(Credential).where(Credential.user_id == user.id, Credential.provider == provider)
        )
        credential = result.scalar_one_or_none()
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
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        return {"linked": bool(db_user.telegram_chat_id)}


@app.post("/api/telegram/unlink")
async def telegram_unlink(user: User = Depends(require_login)):
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
    return {"linked": bool(user.google_refresh_token), "email": user.google_email}


@app.post("/api/google/unlink")
async def google_unlink(user: User = Depends(require_login)):
    async with async_session() as session:
        db_user = await session.get(User, user.id)
        db_user.google_refresh_token = None
        db_user.google_email = None
        await session.commit()
    return {"ok": True}


@app.post("/api/test-email")
async def test_email(user: User = Depends(require_login)):
    """Send a test email — via the user's connected Gmail if linked, else the operator's SMTP/Resend account."""
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
    async with async_session() as session:
        watch = await _get_owned_watch(session, watch_id, user.id)
        if watch is None:
            return JSONResponse({"error": "Watch not found"}, 404)

        credential_email, credential_password = "", ""
        if not settings.USE_MOCK_SCRAPER:
            cred_result = await session.execute(
                select(Credential).where(Credential.user_id == user.id, Credential.provider == watch.provider)
            )
            credential = cred_result.scalar_one_or_none()
            if credential is not None:
                credential_email = credential.email
                credential_password = decrypt_credential(credential.encrypted_password)

    try:
        scraper = get_scraper(
            provider=watch.provider,
            centre=watch.centre,
            destination=watch.destination,
            visa_type=watch.visa_type,
            booking_url=watch.booking_url,
            credential_email=credential_email,
            credential_password=credential_password,
        )
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
    """Switch mock scraper mode: normal / empty / error (global dev/sandbox toggle)."""
    body = await request.json()
    mode = body.get("mode", "normal")

    MockScraper.reset()
    if mode == "empty":
        MockScraper.set_force_empty(True)
    elif mode == "error":
        MockScraper.set_force_error(True, "Simulated block for testing")

    return {"ok": True, "mode": mode}
