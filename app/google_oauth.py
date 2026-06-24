"""Per-user Gmail sending via Google OAuth.

Each user authorizes this app to send mail as themselves (gmail.send scope)
instead of routing every alert through one shared operator SMTP account.
No password ever touches this app — only a revocable OAuth refresh token.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
]
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }


def _make_flow() -> Flow:
    return Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )


def build_auth_url(state: str) -> str:
    """Return the Google consent-screen URL the user should be redirected to."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID/SECRET not configured")

    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # force a refresh token on every consent, not just the first
        state=state,
    )
    return auth_url


def _fetch_token_sync(code: str) -> Credentials:
    flow = _make_flow()
    flow.fetch_token(code=code)
    return flow.credentials


async def exchange_code(code: str) -> tuple[str, str]:
    """Exchange an authorization code for (refresh_token, connected_email)."""
    creds = await asyncio.to_thread(_fetch_token_sync, code)
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token — the user may need to revoke prior "
            "access at myaccount.google.com/permissions and reconnect"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.get(_USERINFO_URL, headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
        resp.raise_for_status()
        email = resp.json().get("email", "")

    return creds.refresh_token, email


def _build_raw_message(to_email: str, subject: str, plain: str, html: str) -> str:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _send_sync(refresh_token: str, to_email: str, subject: str, plain: str, html: str) -> None:
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())

    service = build("gmail", "v1", credentials=creds)
    raw = _build_raw_message(to_email, subject, plain, html)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


async def send_via_gmail(refresh_token: str, to_email: str, subject: str, plain: str, html: str) -> None:
    """Send via the Gmail API using the user's own connected account. Raises on failure."""
    await asyncio.to_thread(_send_sync, refresh_token, to_email, subject, plain, html)
