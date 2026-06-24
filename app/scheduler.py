"""APScheduler-based background scheduler for periodic watch checks.

Responsibilities:
 - Run all enabled watches on a configurable interval
 - Per-watch random jitter to avoid a fixed fingerprint
 - Exponential backoff on ScraperError (never hammer a blocked site)
 - Change detection: detect new/earlier availability and trigger alerts
 - Semaphore-limited concurrency (max 3 parallel scrapes)
 - Never crash — all exceptions caught and logged
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, desc

from app.auth import decrypt_credential
from app.config import settings
from app.db import async_session
from app.models import Watch, AvailabilitySnapshot, Credential, User
from app.scrapers.base import ScraperError
from app.scrapers.registry import get_scraper
from app import notifier

logger = logging.getLogger(__name__)

# ── Module-level scheduler instance ──────────────────────────
scheduler = AsyncIOScheduler()
_semaphore = asyncio.Semaphore(3)  # max 3 concurrent scrapes


# ── Single-watch check ───────────────────────────────────────

async def _check_single_watch(watch_id: int) -> dict | None:
    """Scrape a single watch, store the result, detect changes, alert if needed.

    Returns a summary dict or None on skip.
    """
    async with async_session() as session:
        watch = await session.get(Watch, watch_id)
        if watch is None or not watch.enabled:
            return None

        # Skip if in backoff
        if watch.backoff_until and datetime.utcnow() < watch.backoff_until:
            logger.debug("Watch %d still in backoff until %s", watch_id, watch.backoff_until)
            return None

        try:
            credential_email = ""
            credential_password = ""
            if not settings.USE_MOCK_SCRAPER:
                cred_stmt = select(Credential).where(
                    Credential.user_id == watch.user_id,
                    Credential.provider == watch.provider,
                )
                cred_result = await session.execute(cred_stmt)
                credential = cred_result.scalar_one_or_none()
                if credential is not None:
                    credential_email = credential.email
                    credential_password = decrypt_credential(credential.encrypted_password)

            scraper = get_scraper(
                provider=watch.provider,
                centre=watch.centre,
                destination=watch.destination,
                visa_type=watch.visa_type,
                booking_url=watch.booking_url,
                credential_email=credential_email,
                credential_password=credential_password,
            )
            async with _semaphore:
                slots = await scraper.fetch()

            # ── Success: store snapshot ──────────────────────
            earliest = min((s.appt_date for s in slots), default=None)
            slots_data = [
                {"date": s.appt_date.isoformat(), "count": s.count}
                for s in slots
            ]
            snapshot = AvailabilitySnapshot(
                watch_id=watch_id,
                checked_at=datetime.utcnow(),
                earliest_date=earliest,
                slots_json=json.dumps(slots_data),
                is_error=False,
            )
            session.add(snapshot)

            # Clear backoff on success
            watch.last_checked_at = datetime.utcnow()
            watch.last_error = None
            watch.backoff_until = None
            watch.backoff_count = 0

            await session.commit()

            # ── Change detection ─────────────────────────────
            if earliest and slots:
                await _detect_and_alert(session, watch, earliest, slots)

            logger.info(
                "Watch %d (%s→%s): %d slots, earliest=%s",
                watch_id, watch.centre, watch.destination,
                len(slots), earliest,
            )
            return {
                "watch_id": watch_id,
                "status": "ok",
                "slots": len(slots),
                "earliest": earliest.isoformat() if earliest else None,
            }

        except ScraperError as e:
            # ── Scraper error: backoff ───────────────────────
            watch.backoff_count += 1
            backoff_minutes = min(2 ** watch.backoff_count * 2, 60)  # cap at 60 min
            watch.backoff_until = datetime.utcnow() + timedelta(minutes=backoff_minutes)
            watch.last_error = str(e)
            watch.last_checked_at = datetime.utcnow()

            snapshot = AvailabilitySnapshot(
                watch_id=watch_id,
                checked_at=datetime.utcnow(),
                earliest_date=None,
                slots_json="[]",
                is_error=True,
                error_message=str(e),
            )
            session.add(snapshot)
            await session.commit()

            logger.warning(
                "Watch %d ScraperError (backoff %d min): %s",
                watch_id, backoff_minutes, e,
            )
            return {
                "watch_id": watch_id,
                "status": "error",
                "error": str(e),
                "backoff_minutes": backoff_minutes,
            }

        except Exception as e:
            # ── Unexpected error: log but don't crash ────────
            watch.last_error = f"Unexpected: {e}"
            watch.last_checked_at = datetime.utcnow()

            snapshot = AvailabilitySnapshot(
                watch_id=watch_id,
                checked_at=datetime.utcnow(),
                earliest_date=None,
                slots_json="[]",
                is_error=True,
                error_message=f"Unexpected: {e}",
            )
            session.add(snapshot)
            await session.commit()

            logger.exception("Unexpected error in watch %d", watch_id)
            return {
                "watch_id": watch_id,
                "status": "error",
                "error": str(e),
            }


def _should_alert(
    prev_earliest_date: date | None,
    prev_exists: bool,
    new_earliest: date,
    alert_before_date: date | None,
) -> bool:
    """Pure decision logic for whether a 'new opening' should trigger an alert.

    New opening = previously no availability OR new earliest date is earlier
    than the last non-error snapshot's earliest date. A configured
    alert_before_date overrides all of that — if the new earliest date falls
    after it, the alert is suppressed regardless (the watch keeps scraping,
    it just won't notify until something earlier than the cutoff shows up).
    """
    if alert_before_date is not None and new_earliest > alert_before_date:
        return False

    if not prev_exists:
        return True
    if prev_earliest_date is None:
        return True
    return new_earliest < prev_earliest_date


async def _detect_and_alert(
    session,
    watch: Watch,
    new_earliest: date,
    slots,
) -> None:
    """Detect if this is a 'new opening' and send alert if so."""
    # Get the previous non-error snapshot (the one before the current)
    stmt = (
        select(AvailabilitySnapshot)
        .where(AvailabilitySnapshot.watch_id == watch.id)
        .where(AvailabilitySnapshot.is_error == False)  # noqa: E712
        .order_by(desc(AvailabilitySnapshot.checked_at))
        .offset(1)  # skip the one we just inserted
        .limit(1)
    )
    result = await session.execute(stmt)
    prev = result.scalar_one_or_none()

    should_alert = _should_alert(
        prev_earliest_date=prev.earliest_date if prev else None,
        prev_exists=prev is not None,
        new_earliest=new_earliest,
        alert_before_date=watch.alert_before_date,
    )

    if should_alert:
        logger.info("Watch %d: new opening detected (earliest=%s) → alerting", watch.id, new_earliest)
    elif watch.alert_before_date is not None and new_earliest > watch.alert_before_date:
        logger.info(
            "Watch %d: earliest=%s is past alert_before_date=%s → suppressing alert",
            watch.id, new_earliest, watch.alert_before_date,
        )

    if should_alert:
        owner = await session.get(User, watch.user_id)
        google_refresh_token = (
            decrypt_credential(owner.google_refresh_token) if owner.google_refresh_token else None
        )
        sent = await notifier.send_alert(
            session=session,
            watch_id=watch.id,
            destination=watch.destination,
            visa_type=watch.visa_type,
            centre=watch.centre,
            slots=slots,
            booking_url=watch.booking_url,
            to_email=owner.email,
            telegram_chat_id=owner.telegram_chat_id,
            google_refresh_token=google_refresh_token,
        )
        if sent:
            logger.info("Watch %d: alert email sent", watch.id)
        else:
            logger.info("Watch %d: alert suppressed (cooldown)", watch.id)


# ── Check all watches ────────────────────────────────────────

async def check_all_watches(jitter: bool = True, user_id: int | None = None) -> list[dict]:
    """Run all enabled watches (optionally scoped to a single user).

    jitter=True (used by the periodic background job) staggers each watch by
    0-120s to avoid a fixed scraping fingerprint. jitter=False (used by the
    manual 'Check Now' button) runs all watches concurrently — a user who
    clicked a button wants fast feedback, not a multi-minute wait.
    """
    async with async_session() as session:
        stmt = select(Watch).where(Watch.enabled == True)  # noqa: E712
        if user_id is not None:
            stmt = stmt.where(Watch.user_id == user_id)
        result = await session.execute(stmt)
        watches = result.scalars().all()

    if not watches:
        logger.debug("No enabled watches to check")
        return []

    if not jitter:
        gathered = await asyncio.gather(*(_check_single_watch(w.id) for w in watches))
        return [r for r in gathered if r]

    results = []
    for watch in watches:
        # Random jitter: 0–120 seconds before each watch
        delay = random.uniform(0, 120)
        await asyncio.sleep(delay)
        r = await _check_single_watch(watch.id)
        if r:
            results.append(r)

    return results


async def check_one_watch(watch_id: int) -> dict | None:
    """Run a single watch immediately (no jitter). Used by 'Check Now' button."""
    return await _check_single_watch(watch_id)


# ── Scheduler lifecycle ──────────────────────────────────────

def start_scheduler() -> None:
    """Start the APScheduler periodic job."""
    scheduler.add_job(
        check_all_watches,
        trigger=IntervalTrigger(minutes=settings.CHECK_INTERVAL_MINUTES),
        id="check_all_watches",
        name="Check all visa appointment watches",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Scheduler started: checking every %d minutes",
        settings.CHECK_INTERVAL_MINUTES,
    )


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
