"""Email notification module — SMTP (Gmail App Password) or Resend API.

Responsibilities:
 - Build alert email (subject + HTML + plain text)
 - Check cooldown via AlertLog before sending
 - Send via SMTP or Resend
 - Log every sent alert for dedup
"""

from __future__ import annotations

import logging
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.google_oauth import send_via_gmail
from app.models import AlertLog
from app.telegram import send_telegram_message

if TYPE_CHECKING:
    from app.scrapers.base import Slot

logger = logging.getLogger(__name__)


# ── Cooldown check ───────────────────────────────────────────

async def _is_on_cooldown(
    session: AsyncSession,
    watch_id: int,
    earliest_date: date,
) -> bool:
    """Return True if we already alerted this watch+date within the cooldown window."""
    cutoff = datetime.utcnow() - timedelta(hours=settings.ALERT_COOLDOWN_HOURS)
    stmt = (
        select(AlertLog)
        .where(AlertLog.watch_id == watch_id)
        .where(AlertLog.earliest_date == earliest_date)
        .where(AlertLog.alerted_at >= cutoff)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _log_alert(
    session: AsyncSession,
    watch_id: int,
    earliest_date: date,
    email: str,
) -> None:
    """Record that we sent an alert."""
    entry = AlertLog(
        watch_id=watch_id,
        alerted_at=datetime.utcnow(),
        earliest_date=earliest_date,
        email_sent_to=email,
    )
    session.add(entry)
    await session.commit()


# ── Email building ───────────────────────────────────────────

def _build_email(
    destination: str,
    visa_type: str,
    centre: str,
    slots: list[Slot],
    booking_url: str,
) -> tuple[str, str, str]:
    """Return (subject, plain_body, html_body)."""
    earliest = min(s.appt_date for s in slots)

    # Group by month for the summary
    by_month: dict[str, int] = defaultdict(int)
    for s in slots:
        key = s.appt_date.strftime("%B %Y")
        by_month[key] += s.count

    month_lines = "\n".join(f"  • {m}: {c} slot(s)" for m, c in by_month.items())

    subject = f"🔔 New {destination} ({visa_type}) appointment — {centre}"

    plain = (
        f"Great news! New appointment slots detected.\n\n"
        f"Destination: {destination}\n"
        f"Visa type:   {visa_type}\n"
        f"Centre:      {centre}\n"
        f"Earliest:    {earliest.strftime('%d %B %Y')}\n\n"
        f"Slots by month:\n{month_lines}\n\n"
        f"Book now: {booking_url}\n\n"
        f"Checked at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"— Schengen Appointment Tracker"
    )

    month_rows = "".join(
        f'<tr><td style="padding:4px 12px">{m}</td>'
        f'<td style="padding:4px 12px;font-weight:bold">{c} slot(s)</td></tr>'
        for m, c in by_month.items()
    )

    html = f"""\
<div style="font-family:Inter,system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px">
  <h2 style="color:#1a1a2e;margin:0 0 8px">🔔 New appointment available</h2>
  <p style="color:#555;margin:0 0 20px;font-size:14px">
    {destination} ({visa_type}) — {centre}
  </p>
  <div style="background:#f0fdf4;border-left:4px solid #22c55e;padding:16px;border-radius:8px;margin-bottom:20px">
    <p style="margin:0;font-size:22px;font-weight:700;color:#166534">
      {earliest.strftime('%d %B %Y')}
    </p>
    <p style="margin:4px 0 0;color:#555;font-size:13px">Earliest available date</p>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
    <thead><tr style="background:#f8f9fa">
      <th style="padding:8px 12px;text-align:left">Month</th>
      <th style="padding:8px 12px;text-align:left">Slots</th>
    </tr></thead>
    <tbody>{month_rows}</tbody>
  </table>
  <a href="{booking_url}"
     style="display:inline-block;background:#4f46e5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px">
    Book Now →
  </a>
  <p style="color:#999;font-size:11px;margin-top:24px">
    Checked at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · Schengen Appointment Tracker
  </p>
</div>"""

    return subject, plain, html


# ── Sending ──────────────────────────────────────────────────

def _send_smtp(to: str, subject: str, plain: str, html: str) -> None:
    """Send email via SMTP (Gmail App Password)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings.SMTP_USER, settings.SMTP_PASS)
        server.sendmail(settings.SMTP_USER, to, msg.as_string())

    logger.info("SMTP email sent to %s: %s", to, subject)


async def _send_resend(to: str, subject: str, plain: str, html: str) -> None:
    """Send email via Resend API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": "Schengen Tracker <onboarding@resend.dev>",
                "to": [to],
                "subject": subject,
                "text": plain,
                "html": html,
            },
        )
        resp.raise_for_status()

    logger.info("Resend email sent to %s: %s", to, subject)


async def _send_email(to: str, subject: str, plain: str, html: str) -> None:
    """Route to SMTP or Resend based on config."""
    if settings.use_resend:
        await _send_resend(to, subject, plain, html)
    else:
        _send_smtp(to, subject, plain, html)


async def _deliver_email(
    to_email: str,
    subject: str,
    plain: str,
    html: str,
    google_refresh_token: str | None,
) -> None:
    """Prefer the user's own connected Gmail; fall back to the operator's SMTP/Resend account."""
    if google_refresh_token:
        await send_via_gmail(google_refresh_token, to_email, subject, plain, html)
    else:
        await _send_email(to_email, subject, plain, html)


# ── Public API ───────────────────────────────────────────────

async def send_alert(
    session: AsyncSession,
    watch_id: int,
    destination: str,
    visa_type: str,
    centre: str,
    slots: list[Slot],
    booking_url: str,
    to_email: str,
    telegram_chat_id: str | None = None,
    google_refresh_token: str | None = None,
) -> bool:
    """Send an alert email (and Telegram message, if linked) if not on cooldown. Returns True if sent."""
    if not slots:
        return False

    earliest = min(s.appt_date for s in slots)

    # Check cooldown
    if await _is_on_cooldown(session, watch_id, earliest):
        logger.info(
            "Alert suppressed (cooldown): watch=%d earliest=%s", watch_id, earliest
        )
        return False

    # Build and send
    subject, plain, html = _build_email(destination, visa_type, centre, slots, booking_url)
    try:
        await _deliver_email(to_email, subject, plain, html, google_refresh_token)
    except Exception:
        logger.exception("Failed to send alert email for watch=%d", watch_id)
        return False

    # Telegram is a best-effort second channel — never blocks the email path or dedup log.
    if telegram_chat_id:
        try:
            await send_telegram_message(telegram_chat_id, f"{subject}\n\n{plain}")
        except Exception:
            logger.exception("Failed to send Telegram alert for watch=%d", watch_id)

    # Log it
    await _log_alert(session, watch_id, earliest, to_email)
    return True


async def send_test_email(to_email: str, google_refresh_token: str | None = None) -> dict:
    """Send a test email to verify email delivery config. Returns status dict."""
    subject = "✅ Schengen Tracker — Test Email"
    plain = (
        "If you can read this, your email configuration is working!\n\n"
        f"Sent at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        "— Schengen Appointment Tracker"
    )
    html = f"""\
<div style="font-family:Inter,system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px">
  <h2 style="color:#1a1a2e;margin:0 0 12px">✅ Test Email</h2>
  <p style="color:#555">Your email configuration is working correctly.</p>
  <p style="color:#999;font-size:12px;margin-top:20px">
    Sent at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · Schengen Appointment Tracker
  </p>
</div>"""

    try:
        await _deliver_email(to_email, subject, plain, html, google_refresh_token)
        return {"ok": True, "message": f"Test email sent to {to_email}"}
    except Exception as e:
        logger.exception("Test email failed")
        return {"ok": False, "message": f"Failed: {e}"}
