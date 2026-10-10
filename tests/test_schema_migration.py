"""Expand-only migration for the alert outbox (ADR-001).

init_db() runs Base.metadata.create_all at startup, which creates *missing* tables
(and their indexes) and leaves existing tables untouched. These tests pin that:
an existing database gains `alert_outbox`, keeps its data, and the DB itself
enforces "at most one pending alert per watch".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

PREVIOUS_RELEASE_TABLES = ["users", "credentials", "watches", "availability_snapshots", "alert_logs"]


@pytest.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'existing.db'}")
    yield eng
    await eng.dispose()


async def _create_previous_release_schema_with_data(engine):
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[Base.metadata.tables[name] for name in PREVIOUS_RELEASE_TABLES]))
        await conn.execute(text(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (1, 'a@x.test', 'h', CURRENT_TIMESTAMP)"))
        await conn.execute(text(
            "INSERT INTO watches (id, user_id, centre, destination, visa_type, provider, enabled, booking_url, backoff_count)"
            " VALUES (1, 1, 'London', 'France', 'tourism', 'tlscontact', 1, '', 0)"))
        await conn.execute(text(
            "INSERT INTO alert_logs (watch_id, alerted_at, earliest_date, email_sent_to)"
            " VALUES (1, CURRENT_TIMESTAMP, '2026-11-02', 'a@x.test')"))


async def _startup(engine):
    """Exactly what app.db.init_db() does on every application start."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_startup_adds_outbox_to_existing_db_without_touching_data(engine):
    await _create_previous_release_schema_with_data(engine)
    await _startup(engine)
    await _startup(engine)  # second start: must be a no-op, not an error

    async with engine.connect() as conn:
        tables = set((await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars())
        index_sql = (await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name='uq_alert_outbox_one_pending_per_watch'"))).scalar_one()
        counts = [(await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
                  for t in ("users", "watches", "alert_logs")]

    assert "alert_outbox" in tables
    assert "UNIQUE" in index_sql and "WHERE status = 'pending'" in index_sql
    assert counts == [1, 1, 1]


async def test_database_rejects_a_second_pending_alert_for_the_same_watch(engine):
    await _create_previous_release_schema_with_data(engine)
    await _startup(engine)
    insert = ("INSERT INTO alert_outbox (watch_id, earliest_date, slots_json, detected_at, status, attempts)"
              " VALUES (1, '2026-11-02', '[]', CURRENT_TIMESTAMP, :status, 0)")

    async with engine.begin() as conn:
        await conn.execute(text(insert), {"status": "pending"})
        await conn.execute(text(insert), {"status": "sent"})       # history rows are unrestricted
        await conn.execute(text(insert), {"status": "superseded"})

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text(insert), {"status": "pending"})
