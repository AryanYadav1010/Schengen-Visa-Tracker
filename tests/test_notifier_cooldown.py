"""Cooldown query and email-transport routing.

The end-to-end alert lifecycle (queueing, delivery, retries, Telegram) is covered
in test_alert_delivery.py.
"""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import notifier
from app.config import settings
from app.models import AlertLog, Base

OPENING = date(2026, 8, 5)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def stub_email(monkeypatch):
    sent = []

    async def fake_send_email(to, subject, plain, html):
        sent.append((to, subject))

    monkeypatch.setattr(notifier, "_send_email", fake_send_email)
    return sent


async def _log(session, earliest, hours_ago=0.0):
    session.add(AlertLog(watch_id=1, alerted_at=datetime.utcnow() - timedelta(hours=hours_ago),
                         earliest_date=earliest, email_sent_to="test@example.com"))
    await session.commit()


async def test_not_on_cooldown_without_history(session):
    assert await notifier.is_on_cooldown(session, 1, OPENING) is False


async def test_on_cooldown_for_same_date_within_window(session):
    await _log(session, OPENING)
    assert await notifier.is_on_cooldown(session, 1, OPENING) is True


async def test_different_earliest_date_bypasses_cooldown(session):
    await _log(session, OPENING)
    assert await notifier.is_on_cooldown(session, 1, date(2026, 7, 1)) is False


async def test_cooldown_expires_after_window(session):
    await _log(session, OPENING, hours_ago=settings.ALERT_COOLDOWN_HOURS + 1)
    assert await notifier.is_on_cooldown(session, 1, OPENING) is False


async def test_deliver_email_prefers_gmail_when_connected(stub_email, monkeypatch):
    gmail_sent = []

    async def fake_send_via_gmail(refresh_token, to_email, subject, plain, html):
        gmail_sent.append(refresh_token)

    monkeypatch.setattr(notifier, "send_via_gmail", fake_send_via_gmail)
    await notifier._deliver_email("test@example.com", "s", "p", "<p>h</p>", "user-refresh-token")
    assert gmail_sent == ["user-refresh-token"]
    assert stub_email == []  # operator SMTP/Resend path not used


async def test_deliver_email_falls_back_to_operator_account(stub_email):
    await notifier._deliver_email("test@example.com", "s", "p", "<p>h</p>", None)
    assert stub_email == [("test@example.com", "s")]


async def test_send_test_email_falls_back_to_operator_email_without_google_link(stub_email):
    result = await notifier.send_test_email("test@example.com")
    assert result["ok"] is True
    assert len(stub_email) == 1


async def test_send_test_email_uses_gmail_when_linked(stub_email, monkeypatch):
    gmail_sent = []

    async def fake_send_via_gmail(refresh_token, to_email, subject, plain, html):
        gmail_sent.append(refresh_token)

    monkeypatch.setattr(notifier, "send_via_gmail", fake_send_via_gmail)

    result = await notifier.send_test_email("test@example.com", google_refresh_token="user-refresh-token")

    assert result["ok"] is True
    assert gmail_sent == ["user-refresh-token"]
    assert len(stub_email) == 0
