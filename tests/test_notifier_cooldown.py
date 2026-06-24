from datetime import date

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import notifier
from app.models import AlertLog, Base
from app.scrapers.base import Slot


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


async def test_send_alert_sends_and_logs(session, stub_email):
    slots = [Slot(appt_date=date(2026, 8, 5), count=2, booking_url="https://example.com")]
    sent = await notifier.send_alert(
        session=session,
        watch_id=1,
        destination="France",
        visa_type="tourism",
        centre="London",
        slots=slots,
        booking_url="https://example.com",
        to_email="test@example.com",
    )
    assert sent is True
    assert len(stub_email) == 1

    logs = (await session.execute(AlertLog.__table__.select())).fetchall()
    assert len(logs) == 1


async def test_send_alert_respects_cooldown(session, stub_email):
    slots = [Slot(appt_date=date(2026, 8, 5), count=2, booking_url="https://example.com")]

    first = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots, booking_url="https://example.com", to_email="test@example.com",
    )
    second = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots, booking_url="https://example.com", to_email="test@example.com",
    )

    assert first is True
    assert second is False  # suppressed by cooldown
    assert len(stub_email) == 1


async def test_send_alert_with_no_slots_is_noop(session, stub_email):
    sent = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=[], booking_url="https://example.com", to_email="test@example.com",
    )
    assert sent is False
    assert len(stub_email) == 0


async def test_different_earliest_date_bypasses_cooldown(session, stub_email):
    slots_a = [Slot(appt_date=date(2026, 8, 5), count=1, booking_url="https://example.com")]
    slots_b = [Slot(appt_date=date(2026, 7, 1), count=1, booking_url="https://example.com")]

    await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots_a, booking_url="https://example.com", to_email="test@example.com",
    )
    second = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots_b, booking_url="https://example.com", to_email="test@example.com",
    )

    assert second is True
    assert len(stub_email) == 2


async def test_send_alert_also_sends_telegram_when_chat_id_present(session, stub_email, monkeypatch):
    telegram_sent = []

    async def fake_send_telegram(chat_id, text):
        telegram_sent.append((chat_id, text))

    monkeypatch.setattr(notifier, "send_telegram_message", fake_send_telegram)

    slots = [Slot(appt_date=date(2026, 8, 5), count=2, booking_url="https://example.com")]
    sent = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots, booking_url="https://example.com",
        to_email="test@example.com", telegram_chat_id="999",
    )

    assert sent is True
    assert len(stub_email) == 1
    assert len(telegram_sent) == 1
    assert telegram_sent[0][0] == "999"


async def test_telegram_failure_does_not_block_email_or_dedup(session, stub_email, monkeypatch):
    async def failing_send_telegram(chat_id, text):
        raise RuntimeError("Telegram API down")

    monkeypatch.setattr(notifier, "send_telegram_message", failing_send_telegram)

    slots = [Slot(appt_date=date(2026, 8, 5), count=2, booking_url="https://example.com")]
    sent = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots, booking_url="https://example.com",
        to_email="test@example.com", telegram_chat_id="999",
    )

    assert sent is True
    assert len(stub_email) == 1

    logs = (await session.execute(AlertLog.__table__.select())).fetchall()
    assert len(logs) == 1


async def test_send_alert_uses_gmail_when_connected_instead_of_operator_email(session, stub_email, monkeypatch):
    gmail_sent = []

    async def fake_send_via_gmail(refresh_token, to_email, subject, plain, html):
        gmail_sent.append((refresh_token, to_email, subject))

    monkeypatch.setattr(notifier, "send_via_gmail", fake_send_via_gmail)

    slots = [Slot(appt_date=date(2026, 8, 5), count=2, booking_url="https://example.com")]
    sent = await notifier.send_alert(
        session=session, watch_id=1, destination="France", visa_type="tourism",
        centre="London", slots=slots, booking_url="https://example.com",
        to_email="test@example.com", google_refresh_token="user-refresh-token",
    )

    assert sent is True
    assert len(gmail_sent) == 1
    assert gmail_sent[0][0] == "user-refresh-token"
    assert len(stub_email) == 0  # operator SMTP/Resend path not used


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
