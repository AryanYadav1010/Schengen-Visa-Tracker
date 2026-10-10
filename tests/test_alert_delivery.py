"""ADR-001: durable, deduplicated alert delivery (F-01 / F-05).

These drive the real scheduler + notifier code against a file-backed SQLite
database (so separate sessions really are separate connections, as in
production), with deterministic fakes for the provider, the mailer and Telegram.

Invariant under test: one eligible opening → the user eventually receives exactly
one email, despite delivery failures, crashes, or concurrent checks — and every
owed alert ends in an explicit, persisted status.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import notifier, scheduler
from app.auth import encrypt_credential
from app.models import AlertLog, AlertStatus, AvailabilitySnapshot, Base, OutboxAlert, User, Watch
from app.scrapers.base import ScraperError, Slot

OPENING = date(2026, 11, 2)
EARLIER_OPENING = date(2026, 10, 20)


# ── Deterministic fakes ──────────────────────────────────────────────────────

class FakeProvider:
    """Stands in for registry.get_scraper(); returns `outcome` slots or raises it."""

    def __init__(self):
        self.outcome: list[date] | BaseException = [OPENING]
        self.before_return = None  # optional async hook, used to line up concurrent checks

    def get_scraper(self, **kwargs):
        provider = self

        class _Scraper:
            async def fetch(self):
                if isinstance(provider.outcome, BaseException):
                    raise provider.outcome
                if provider.before_return:
                    await provider.before_return()
                return [Slot(appt_date=d, count=1, booking_url="https://example.test") for d in provider.outcome]

        return _Scraper()


class FakeMailer:
    """Replaces notifier._deliver_email. `failures` is a queue of exceptions to raise first."""

    def __init__(self):
        self.failures: list[BaseException] = []
        self.attempts = 0
        self.delivered: list[str] = []
        self.tokens: list[str | None] = []
        self.gate: asyncio.Event | None = None  # if set, every send waits for it

    async def __call__(self, to_email, subject, plain, html, google_refresh_token):
        self.attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.delivered.append(to_email)
        self.tokens.append(google_refresh_token)


class Env:
    async def alert_log_count(self):
        async with self.maker() as s:
            return (await s.execute(select(func.count()).select_from(AlertLog))).scalar_one()

    async def outbox(self):
        async with self.maker() as s:
            return (await s.execute(select(OutboxAlert).order_by(OutboxAlert.id))).scalars().all()


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alerts.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as s:
        user = User(email="owner@example.test", password_hash="x")
        s.add(user)
        await s.flush()
        watch = Watch(user_id=user.id, centre="London", destination="France", visa_type="tourism",
                      provider="tlscontact", booking_url="https://example.test")
        s.add(watch)
        await s.commit()

    e = Env()
    e.maker, e.watch_id, e.user_id = maker, watch.id, user.id
    e.provider, e.mailer, e.telegram_sent = FakeProvider(), FakeMailer(), []

    async def fake_telegram(chat_id, text):
        e.telegram_sent.append(chat_id)

    monkeypatch.setattr(scheduler, "async_session", maker)
    monkeypatch.setattr(notifier, "async_session", maker)
    monkeypatch.setattr(scheduler, "get_scraper", e.provider.get_scraper)
    monkeypatch.setattr(notifier, "_deliver_email", e.mailer)
    monkeypatch.setattr(notifier, "send_telegram_message", fake_telegram)
    # asyncio locks bind to the event loop that first waits on them; each test has its own loop.
    monkeypatch.setattr(scheduler, "_watch_locks", defaultdict(asyncio.Lock))
    monkeypatch.setattr(notifier, "_delivery_lock", asyncio.Lock())
    yield e
    await engine.dispose()


async def tick():
    """One periodic scheduler run (sweep owed alerts, then check watches), minus the jitter."""
    return await scheduler.check_all_watches(jitter=False)


def smtp_down(n=1):
    return [ConnectionRefusedError("SMTP down")] * n


# ── Baseline behaviour (unchanged) ──────────────────────────────────────────

async def test_first_opening_is_delivered_once_and_logged(env):
    await tick()
    assert env.mailer.delivered == ["owner@example.test"]
    assert await env.alert_log_count() == 1
    [alert] = await env.outbox()
    assert (alert.status, alert.attempts, alert.earliest_date) == (AlertStatus.SENT, 1, OPENING)


async def test_unchanged_opening_is_not_re_sent(env):
    await tick()
    await tick()
    assert len(env.mailer.delivered) == 1
    assert await env.alert_log_count() == 1


# ── F-01: owed alerts survive failures ──────────────────────────────────────

async def test_smtp_outage_then_recovery_delivers_exactly_once(env):
    env.mailer.failures = smtp_down()
    await tick()   # observation + owed alert committed; send fails
    [alert] = await env.outbox()
    assert (alert.status, alert.attempts, alert.last_error) == (AlertStatus.PENDING, 1, "ConnectionRefusedError")
    await tick()   # sweep retries the owed alert
    assert len(env.mailer.delivered) == 1
    assert await env.alert_log_count() == 1


async def test_crash_after_snapshot_commit_still_delivers(env):
    # CancelledError is what an in-flight task gets on shutdown; it is a BaseException,
    # so no `except Exception` intercepts it.
    env.mailer.failures = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await scheduler._check_single_watch(env.watch_id)
    [alert] = await env.outbox()
    assert (alert.status, alert.attempts) == (AlertStatus.PENDING, 1)  # attempt counted *before* sending
    await tick()   # "after restart"
    assert len(env.mailer.delivered) == 1


async def test_owed_alert_is_retried_even_if_next_scrape_is_blocked(env):
    env.mailer.failures = smtp_down()
    await tick()
    env.provider.outcome = ScraperError("CAPTCHA")   # provider now blocks us
    await tick()
    assert len(env.mailer.delivered) == 1   # retry does not depend on a successful scrape


async def test_retry_limit_marks_alert_failed_and_keeps_it(env):
    env.mailer.failures = smtp_down(100)
    for _ in range(notifier.MAX_DELIVERY_ATTEMPTS + 2):
        await tick()
    assert env.mailer.attempts == notifier.MAX_DELIVERY_ATTEMPTS   # bounded: no attempts after giving up
    [alert] = await env.outbox()                                   # never deleted
    assert (alert.status, alert.attempts, alert.last_error) == (
        AlertStatus.FAILED, notifier.MAX_DELIVERY_ATTEMPTS, "ConnectionRefusedError")
    assert alert.resolved_at is not None
    assert await env.alert_log_count() == 0


async def test_observation_and_alert_intent_are_committed_atomically(env, monkeypatch):
    real_queue_alert = notifier.queue_alert
    calls = 0

    async def queue_alert_failing_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk full")
        return await real_queue_alert(*args, **kwargs)

    monkeypatch.setattr(notifier, "queue_alert", queue_alert_failing_once)
    await tick()
    async with env.maker() as s:
        ok_snapshots = (await s.execute(select(func.count()).select_from(AvailabilitySnapshot)
                                        .where(AvailabilitySnapshot.is_error == False))).scalar_one()  # noqa: E712
    assert ok_snapshots == 0           # observation rolled back together with the failed intent
    assert await env.outbox() == []
    await tick()                       # the same opening is still "new" → queued and delivered
    assert len(env.mailer.delivered) == 1


async def test_app_start_retries_owed_alerts_without_waiting_a_full_interval(env, monkeypatch):
    """Restart loop shorter than CHECK_INTERVAL_MINUTES must not starve retries."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    env.mailer.failures = smtp_down()
    await scheduler._check_single_watch(env.watch_id)          # owed alert, first attempt failed
    monkeypatch.setattr(scheduler, "scheduler", AsyncIOScheduler())
    scheduler.start_scheduler()                                 # what the app lifespan does on start
    try:
        for _ in range(200):                                    # periodic job is 15 min away
            if env.mailer.delivered:
                break
            await asyncio.sleep(0.01)
    finally:
        scheduler.stop_scheduler()
    assert len(env.mailer.delivered) == 1


# ── Newer events while an alert is pending ──────────────────────────────────

async def test_earlier_opening_supersedes_pending_alert_and_both_are_recorded(env):
    env.mailer.failures = smtp_down(2)
    await tick()                                   # OPENING queued, attempt 1 fails
    env.provider.outcome = [EARLIER_OPENING]
    await tick()                                   # sweep: attempt 2 fails; check: earlier date → supersede
    assert len(env.mailer.delivered) == 1          # one email, about the best date
    old, new = await env.outbox()
    assert (old.earliest_date, old.status) == (OPENING, AlertStatus.SUPERSEDED)
    assert (new.earliest_date, new.status) == (EARLIER_OPENING, AlertStatus.SENT)


# ── Revalidation before sending ─────────────────────────────────────────────

async def test_alert_is_marked_stale_if_the_opening_disappeared(env):
    env.mailer.failures = smtp_down(2)
    await tick()                       # queued, attempt 1 fails
    env.provider.outcome = []          # next check: nothing available
    await tick()                       # sweep attempt 2 fails (latest check still showed it); check records "none"
    await tick()                       # sweep: latest successful check shows no availability → stale
    assert env.mailer.delivered == []
    [alert] = await env.outbox()
    assert alert.status == AlertStatus.STALE and "no availability" in alert.last_error


async def test_failed_scrape_does_not_make_an_owed_alert_stale(env):
    env.mailer.failures = smtp_down(2)
    await tick()
    env.provider.outcome = ScraperError("CAPTCHA")
    await tick()                       # error snapshot: "unknown", not "gone"
    await tick()
    assert len(env.mailer.delivered) == 1


async def test_alert_older_than_max_age_is_stale(env):
    env.mailer.failures = smtp_down()
    await tick()
    async with env.maker() as s:
        too_old = datetime.utcnow() - notifier.MAX_ALERT_AGE - timedelta(minutes=1)
        await s.execute(update(OutboxAlert).values(detected_at=too_old))
        await s.commit()
    await notifier.deliver_pending_alerts()
    assert env.mailer.delivered == []
    [alert] = await env.outbox()
    assert alert.status == AlertStatus.STALE


# ── F-05: concurrency (single process) ──────────────────────────────────────

async def test_two_concurrent_checks_of_one_opening_deliver_exactly_once(env):
    arrived = 0
    both_scraped = asyncio.Event()

    async def rendezvous():
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_scraped.set()
        try:
            await asyncio.wait_for(both_scraped.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass  # per-watch lock: the second check cannot be scraping at the same time

    env.provider.before_return = rendezvous
    await asyncio.gather(scheduler._check_single_watch(env.watch_id),
                         scheduler._check_single_watch(env.watch_id))
    assert len(env.mailer.delivered) == 1


async def test_concurrent_checks_both_committed_before_detection_still_deliver_once(env, monkeypatch):
    """Previously lost the alert (each check saw the sibling's snapshot as "previous")."""
    original = scheduler._queue_alert_if_eligible
    arrived = 0
    both_arrived = asyncio.Event()

    async def detect_together(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        try:
            await asyncio.wait_for(both_arrived.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass  # serialised per watch: the other check is waiting on the lock
        return await original(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_queue_alert_if_eligible", detect_together)
    await asyncio.gather(scheduler._check_single_watch(env.watch_id),
                         scheduler._check_single_watch(env.watch_id))
    assert len(env.mailer.delivered) == 1


async def test_concurrent_deliveries_of_one_owed_alert_send_once(env):
    env.mailer.failures = smtp_down()
    await tick()                                   # one PENDING alert after a failed attempt
    env.mailer.gate = asyncio.Event()
    first = asyncio.create_task(notifier.deliver_pending_alerts())
    second = asyncio.create_task(notifier.deliver_pending_alerts())
    while env.mailer.attempts < 2:                 # first deliverer is inside the send
        await asyncio.sleep(0)
    for _ in range(20):                            # give the second every chance to (wrongly) send too
        await asyncio.sleep(0)
    env.mailer.gate.set()
    await asyncio.gather(first, second)
    assert len(env.mailer.delivered) == 1
    assert await env.alert_log_count() == 1


# ── Channels ────────────────────────────────────────────────────────────────

async def test_owner_gmail_token_is_decrypted_for_delivery(env):
    async with env.maker() as s:
        owner = await s.get(User, env.user_id)
        owner.google_refresh_token = encrypt_credential("user-refresh-token")
        await s.commit()
    await tick()
    assert env.mailer.tokens == ["user-refresh-token"]


async def test_telegram_is_sent_after_email_and_its_failure_does_not_undo_delivery(env, monkeypatch):
    async with env.maker() as s:
        (await s.get(User, env.user_id)).telegram_chat_id = "999"
        await s.commit()

    async def telegram_down(chat_id, text):
        raise RuntimeError("Telegram API down")

    monkeypatch.setattr(notifier, "send_telegram_message", telegram_down)
    await tick()
    [alert] = await env.outbox()
    assert alert.status == AlertStatus.SENT and len(env.mailer.delivered) == 1


async def test_deleting_a_watch_removes_its_outbox_history(env):
    env.mailer.failures = smtp_down()
    await tick()
    async with env.maker() as s:
        await s.delete(await s.get(Watch, env.watch_id))
        await s.commit()          # would raise if the FK weren't cascaded (cf. commit caa5930)
    assert await env.outbox() == []
