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
from collections import defaultdict
from datetime import datetime, timedelta, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, desc

from app.auth import decrypt_credential
from app.config import settings
from app.db import async_session
from app.models import Watch, AvailabilitySnapshot, Credential
from app.scrapers.base import ScraperError
from app.scrapers.registry import get_scraper
from app import notifier

logger = logging.getLogger(__name__)

# ── Module-level scheduler instance ──────────────────────────
scheduler = AsyncIOScheduler()
_semaphore = asyncio.Semaphore(3)  # max 3 concurrent scrapes
# One check per watch at a time (scheduler vs "Check Now" vs double-clicks). Like the
# semaphore, these are in-process asyncio locks — NOT distributed locks (ADR-001 §7).
_watch_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ── Single-watch check ───────────────────────────────────────

async def _check_single_watch(watch_id: int) -> dict | None:
    """Check one watch (serialised per watch), then try to deliver any alert it queued.

    Returns a summary dict or None on skip.
    """
    async with _watch_locks[watch_id]:
        result = await _check_and_record(watch_id)
    if result and result.get("alert_queued"):
        await notifier.deliver_pending_alerts(watch_id=watch_id)
    return result


async def _check_and_record(watch_id: int) -> dict | None:
    """Scrape a single watch and record the outcome in ONE transaction: the snapshot, the
    watch's backoff state and — if the observation is alert-worthy — the owed alert."""
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
            checked_at = datetime.utcnow()
            snapshot = AvailabilitySnapshot(
                watch_id=watch_id,
                checked_at=checked_at,
                earliest_date=earliest,
                slots_json=json.dumps(slots_data),
                is_error=False,
            )
            session.add(snapshot)

            # Clear backoff on success
            watch.last_checked_at = checked_at
            watch.last_error = None
            watch.backoff_until = None
            watch.backoff_count = 0

            # ── Change detection: queue an owed alert in this same transaction ──
            alert_queued = False
            if earliest and slots:
                alert_queued = await _queue_alert_if_eligible(session, watch, earliest, snapshot)

            # The observation and the intent to notify become durable together, or not at all.
            await session.commit()

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
                "alert_queued": alert_queued,
            }

        except ScraperError as e:
            # ── Scraper error: backoff ───────────────────────
            await session.rollback()  # discard any half-written success state before recording the error
            await session.refresh(watch)
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
            # Roll back first: if the failure happened after the snapshot/alert were added (e.g.
            # while queueing the alert), neither may be committed — the next run re-observes.
            await session.rollback()
            await session.refresh(watch)
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


async def _queue_alert_if_eligible(
    session,
    watch: Watch,
    new_earliest: date,
    snapshot: AvailabilitySnapshot,
) -> bool:
    """Eligibility: if this observation is a 'new opening' not already alerted within the
    cooldown, queue an owed alert in the caller's transaction. Returns True if queued."""
    await session.flush()  # make the new snapshot visible to the OFFSET 1 query below
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

    if not should_alert:
        if watch.alert_before_date is not None and new_earliest > watch.alert_before_date:
            logger.info(
                "Watch %d: earliest=%s is past alert_before_date=%s → suppressing alert",
                watch.id, new_earliest, watch.alert_before_date,
            )
        return False

    if await notifier.is_on_cooldown(session, watch.id, new_earliest):
        logger.info("Watch %d: earliest=%s already alerted within cooldown → not queued", watch.id, new_earliest)
        return False

    alert = await notifier.queue_alert(session, watch.id, new_earliest, snapshot.slots_json, snapshot.checked_at)
    logger.info("Watch %d: new opening detected (earliest=%s) → alert %d queued", watch.id, new_earliest, alert.id)
    return True


# ── Check all watches ────────────────────────────────────────

async def check_all_watches(jitter: bool = True, user_id: int | None = None) -> list[dict]:
    """Run all enabled watches (optionally scoped to a single user).

    jitter=True (used by the periodic background job) staggers each watch by
    0-120s to avoid a fixed scraping fingerprint. jitter=False (used by the
    manual 'Check Now' button) runs all watches concurrently — a user who
    clicked a button wants fast feedback, not a multi-minute wait.

    A system-wide run (user_id=None, i.e. the periodic job) first retries every owed
    alert, so retries never depend on a later scrape succeeding.
    """
    if user_id is None:
        try:
            await notifier.deliver_pending_alerts()
        except Exception:
            logger.exception("Retrying pending alerts failed; continuing with checks")

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
    # One-off run at startup: alerts owed from before a restart are retried now, not a full
    # interval later. notifier._delivery_lock stops it overlapping the periodic sweep.
    scheduler.add_job(
        notifier.deliver_pending_alerts,
        id="deliver_pending_alerts_on_startup",
        replace_existing=True,
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
