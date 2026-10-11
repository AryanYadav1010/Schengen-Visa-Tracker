"""Alert notifications — transactional outbox + email (Gmail OAuth / Resend / SMTP) + Telegram.

Lifecycle of an alert (ADR-001):
 - queue_alert():           the scheduler records an owed alert (OutboxAlert, PENDING) in the
                            SAME transaction as the snapshot that made it eligible.
 - deliver_pending_alerts(): sends owed alerts — right after a check queues one, and again at
                            the start of every scheduler run until each ends as SENT, FAILED
                            (retry limit) or STALE (revalidated before sending).
 - AlertLog:                one row per accepted email; the cooldown check reads it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import StrEnum

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import decrypt_credential
from app.config import settings
from app.db import async_session
from app.google_oauth import send_via_gmail
from app.models import AlertLog, AlertStatus, AvailabilitySnapshot, OutboxAlert, User, Watch
from app.scrapers.base import Slot
from app.scrapers.registry import default_booking_url
from app.telegram import send_telegram_message

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 8            # one retry per scheduler run → ~2 h at the default 15 min
MAX_ALERT_AGE = timedelta(hours=6)   # never send an alert detected longer ago than this

# One deliverer at a time in this process. NOT a distributed lock: correct only while the app
# runs as a single process (one uvicorn worker, one machine) — see ADR-001 §7.
_delivery_lock = asyncio.Lock()


# ── Eligibility helpers (run inside the scheduler's transaction; never commit) ──

async def is_on_cooldown(
    session: AsyncSession,
    watch_id: int,
    earliest_date: date,
) -> bool:
    """Return True if an email for this watch+date was accepted within the cooldown window."""
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


async def queue_alert(
    session: AsyncSession,
    watch_id: int,
    earliest_date: date,
    slots_json: str,
    detected_at: datetime,
) -> OutboxAlert:
    """Record an owed alert in the caller's transaction. Does NOT commit.

    If this watch already has an undelivered alert, that one is marked SUPERSEDED (kept,
    not overwritten): the new opening is earlier, and one email about the best date
    beats two emails where the first is already out of date.
    """
    previous = (await session.execute(
        select(OutboxAlert).where(OutboxAlert.watch_id == watch_id, OutboxAlert.status == AlertStatus.PENDING)
    )).scalar_one_or_none()
    if previous is not None:
        previous.status = AlertStatus.SUPERSEDED
        previous.resolved_at = detected_at
        await session.flush()  # free the one-pending-per-watch unique index before inserting
        logger.info("Alert %d for watch %d (earliest=%s) superseded by a newer opening (earliest=%s)",
                    previous.id, watch_id, previous.earliest_date, earliest_date)

    alert = OutboxAlert(watch_id=watch_id, earliest_date=earliest_date, slots_json=slots_json,
                        detected_at=detected_at, status=AlertStatus.PENDING, attempts=0)
    session.add(alert)
    await session.flush()  # assigns alert.id for logging
    return alert


# ── Email building ───────────────────────────────────────────

def _build_email(
    destination: str,
    visa_type: str,
    centre: str,
    slots: list[Slot],
    booking_url: str,
    checked_at: datetime | None = None,
) -> tuple[str, str, str]:
    """Return (subject, plain_body, html_body).

    checked_at is when the opening was observed — a retried alert must not claim it was
    checked at retry time.
    """
    earliest = min(s.appt_date for s in slots)
    checked = (checked_at or datetime.utcnow()).strftime('%Y-%m-%d %H:%M UTC')

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
        f"Checked at: {checked}\n"
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
    Checked at {checked} · Schengen Appointment Tracker
  </p>
</div>"""

    return subject, plain, html


# ── Sending ──────────────────────────────────────────────────

def _send_smtp(to: str, subject: str, plain: str, html: str) -> None:
    """Send email via SMTP (Gmail App Password). Blocking — call it via asyncio.to_thread."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as server:
        server.ehlo()
        server.starttls()  # upgrade to TLS before the password is sent
        server.ehlo()
        server.login(settings.SMTP_USER, settings.SMTP_PASS)
        server.sendmail(settings.SMTP_USER, to, msg.as_string())

    logger.info("SMTP email sent to %s: %s", to, subject)


async def _send_resend(to: str, subject: str, plain: str, html: str) -> None:
    """Send email via Resend API (async HTTP; httpx's default 5 s timeout bounds it)."""
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
        resp.raise_for_status()  # non-2xx → exception → counted as a failed delivery attempt

    logger.info("Resend email sent to %s: %s", to, subject)


async def _send_email(to: str, subject: str, plain: str, html: str) -> None:
    """Route to SMTP or Resend based on config."""
    if settings.use_resend:
        await _send_resend(to, subject, plain, html)
    else:
        # smtplib is synchronous: run it on a worker thread so the event loop keeps serving
        # requests and the scheduler while the SMTP conversation is in progress.
        await asyncio.to_thread(_send_smtp, to, subject, plain, html)


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


# ── Delivery (outbox relay) ──────────────────────────────────

class DeliveryOutcome(StrEnum):
    SENT = "sent"
    RETRY = "retry"      # attempt failed; stays PENDING for the next scheduler run
    FAILED = "failed"    # attempt failed and the retry limit is reached
    STALE = "stale"
    SKIPPED = "skipped"  # no longer PENDING when we got to it (e.g. another deliverer finished it)


async def deliver_pending_alerts(watch_id: int | None = None) -> list[tuple[int, DeliveryOutcome]]:
    """Attempt every PENDING alert (optionally for one watch), oldest first."""
    async with _delivery_lock:
        async with async_session() as session:
            stmt = select(OutboxAlert.id).where(OutboxAlert.status == AlertStatus.PENDING).order_by(OutboxAlert.id)
            if watch_id is not None:
                stmt = stmt.where(OutboxAlert.watch_id == watch_id)
            alert_ids = list((await session.execute(stmt)).scalars())
        return [(alert_id, await _deliver_one(alert_id)) for alert_id in alert_ids]


async def _stale_reason(session: AsyncSession, alert: OutboxAlert, now: datetime) -> str | None:
    """Revalidation policy: send only if the alert is young enough AND the latest *successful*
    check still lists availability on or before the alerted date. A failed check (error
    snapshot) means "unknown", so it never makes an alert stale."""
    if now - alert.detected_at > MAX_ALERT_AGE:
        return f"older than {MAX_ALERT_AGE}"
    latest = (await session.execute(
        select(AvailabilitySnapshot)
        .where(AvailabilitySnapshot.watch_id == alert.watch_id, AvailabilitySnapshot.is_error == False)  # noqa: E712
        .order_by(desc(AvailabilitySnapshot.checked_at))
        .limit(1)
    )).scalar_one_or_none()
    if latest is not None and latest.earliest_date is None:
        return "latest check found no availability"
    if latest is not None and latest.earliest_date > alert.earliest_date:
        return f"latest earliest date {latest.earliest_date} is later than alerted {alert.earliest_date}"
    return None


async def _deliver_one(alert_id: int) -> DeliveryOutcome:
    now = datetime.utcnow()

    # Transaction 1 — revalidate, then count the attempt BEFORE the side effect, so a send that
    # crashes the process still uses up an attempt (the retry limit holds even under crashes).
    async with async_session() as session:
        alert = await session.get(OutboxAlert, alert_id)
        if alert is None or alert.status != AlertStatus.PENDING:
            return DeliveryOutcome.SKIPPED
        reason = await _stale_reason(session, alert, now)
        if reason is None and alert.attempts >= MAX_DELIVERY_ATTEMPTS:
            alert.status, alert.resolved_at = AlertStatus.FAILED, now
            await session.commit()
            logger.error("Alert %d for watch %d: FAILED — retry limit reached", alert_id, alert.watch_id)
            return DeliveryOutcome.FAILED
        if reason is not None:
            alert.status, alert.resolved_at, alert.last_error = AlertStatus.STALE, now, reason
            await session.commit()
            logger.info("Alert %d for watch %d: STALE, not sent (%s)", alert_id, alert.watch_id, reason)
            return DeliveryOutcome.STALE

        alert.attempts += 1
        alert.last_attempt_at = now
        watch = await session.get(Watch, alert.watch_id)
        owner = await session.get(User, watch.user_id)
        # Copy what the send needs, so no session/transaction is open during network I/O.
        attempt = alert.attempts
        earliest, slots_json, detected_at = alert.earliest_date, alert.slots_json, alert.detected_at
        destination, visa_type, centre = watch.destination, watch.visa_type, watch.centre
        booking_url = watch.booking_url or default_booking_url(watch.provider)
        to_email, telegram_chat_id, encrypted_token = owner.email, owner.telegram_chat_id, owner.google_refresh_token
        await session.commit()

    # Side effect — outside any transaction (an email cannot be rolled back).
    try:
        slots = [Slot(appt_date=date.fromisoformat(s["date"]), count=s["count"], booking_url=booking_url)
                 for s in json.loads(slots_json)]
        subject, plain, html = _build_email(destination, visa_type, centre, slots, booking_url, detected_at)
        google_refresh_token = decrypt_credential(encrypted_token) if encrypted_token else None
        await _deliver_email(to_email, subject, plain, html, google_refresh_token)
    except Exception as e:
        # Transaction 2a — record the failed attempt (exception class only: no payloads in the DB).
        async with async_session() as session:
            alert = await session.get(OutboxAlert, alert_id)
            alert.last_error = type(e).__name__
            gave_up = alert.attempts >= MAX_DELIVERY_ATTEMPTS
            if gave_up:
                alert.status, alert.resolved_at = AlertStatus.FAILED, datetime.utcnow()
            await session.commit()
        if gave_up:
            logger.error("Alert %d for watch %d: FAILED after %d attempts (%s) — kept in alert_outbox",
                         alert_id, alert.watch_id, attempt, type(e).__name__)
            return DeliveryOutcome.FAILED
        logger.warning("Alert %d for watch %d: delivery attempt %d/%d failed (%s), will retry",
                       alert_id, alert.watch_id, attempt, MAX_DELIVERY_ATTEMPTS, type(e).__name__)
        return DeliveryOutcome.RETRY

    # Transaction 2b — success: status and the cooldown log entry commit together.
    async with async_session() as session:
        alert = await session.get(OutboxAlert, alert_id)
        alert.status, alert.resolved_at, alert.last_error = AlertStatus.SENT, datetime.utcnow(), None
        session.add(AlertLog(watch_id=alert.watch_id, alerted_at=alert.resolved_at,
                             earliest_date=earliest, email_sent_to=to_email))
        await session.commit()
    logger.info("Alert %d for watch %d: SENT (attempt %d, earliest=%s)", alert_id, alert.watch_id, attempt, earliest)

    # Telegram — best effort, AFTER the success commit (keeps the duplicate-email window minimal).
    # Not retried and not tracked: a Telegram failure is only logged. See ADR-001 limitations.
    if telegram_chat_id:
        try:
            await send_telegram_message(telegram_chat_id, f"{subject}\n\n{plain}")
        except Exception:
            logger.exception("Alert %d: Telegram message failed (best effort, not retried)", alert_id)
    return DeliveryOutcome.SENT


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
