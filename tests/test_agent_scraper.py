"""Tests for BaseAgentScraper's integration logic.

These mock browser_use.Agent entirely — they validate our credential checks,
status-to-Slot/ScraperError mapping, and timeout handling without ever
launching a real browser or calling a real LLM (no API key needed).
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

import browser_use
from app.config import settings
from app.scrapers.agent_scraper import AgentScrapeResult, AgentSlot, BaseAgentScraper
from app.scrapers.base import ScraperError
from app.scrapers.tlscontact_fr import TLScontactAgentScraper


class FakeHistory:
    def __init__(self, structured_output=None, successful=True, errors=None):
        self.structured_output = structured_output
        self._successful = successful
        self._errors = errors or []

    def is_successful(self):
        return self._successful

    def errors(self):
        return self._errors


def make_fake_agent_class(result):
    """result: a FakeHistory to return, or an Exception instance to raise."""

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, max_steps=None):
            if isinstance(result, Exception):
                raise result
            return result

    return FakeAgent


@pytest.fixture
def creds():
    """Set a valid Anthropic key, restore afterwards. Per-provider creds are passed
    directly to each scraper instance now (no more global settings.TLSCONTACT_*)."""
    originals = {
        "ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY,
        "AGENT_TIMEOUT_SECONDS": settings.AGENT_TIMEOUT_SECONDS,
        "AGENT_MAX_STEPS": settings.AGENT_MAX_STEPS,
    }
    settings.ANTHROPIC_API_KEY = "sk-ant-fake-test-key"
    yield
    for key, value in originals.items():
        setattr(settings, key, value)


def _scraper(**overrides):
    kwargs = dict(
        centre="London",
        destination="France",
        visa_type="tourism",
        credential_email="test@example.com",
        credential_password="hunter2",
    )
    kwargs.update(overrides)
    return TLScontactAgentScraper(**kwargs)


async def test_missing_provider_credentials_raises_without_calling_agent(creds):
    scraper = _scraper(credential_email="", credential_password="")
    with pytest.raises(ScraperError, match="credentials not configured"):
        await scraper.fetch()


async def test_missing_anthropic_key_raises(creds):
    settings.ANTHROPIC_API_KEY = ""
    scraper = _scraper()
    with pytest.raises(ScraperError, match="ANTHROPIC_API_KEY"):
        await scraper.fetch()


async def test_no_start_url_raises(creds):
    class NoUrlScraper(BaseAgentScraper):
        PROVIDER_LABEL: ClassVar[str] = "Nowhere"
        DEFAULT_START_URL: ClassVar[str] = ""

    scraper = NoUrlScraper(
        centre="London", destination="Nowhere", visa_type="tourism",
        credential_email="test@example.com", credential_password="hunter2",
    )
    with pytest.raises(ScraperError, match="no booking URL"):
        await scraper.fetch()


async def test_slots_found_maps_to_slot_list(creds, monkeypatch):
    result = AgentScrapeResult(
        status="slots_found",
        slots=[AgentSlot(date="2026-08-05", count=2), AgentSlot(date="2026-09-01", count=1)],
    )
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(FakeHistory(structured_output=result)))

    scraper = _scraper(booking_url="https://visas-fr.tlscontact.com/")
    slots = await scraper.fetch()

    assert len(slots) == 2
    assert slots[0].appt_date == date(2026, 8, 5)
    assert slots[0].count == 2
    assert slots[0].booking_url == "https://visas-fr.tlscontact.com/"


async def test_no_availability_returns_empty_list(creds, monkeypatch):
    result = AgentScrapeResult(status="no_availability", slots=[])
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(FakeHistory(structured_output=result)))

    scraper = _scraper()
    slots = await scraper.fetch()

    assert slots == []


async def test_blocked_status_raises_scraper_error_with_reason(creds, monkeypatch):
    result = AgentScrapeResult(status="blocked", slots=[], reason="Cloudflare Turnstile challenge")
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(FakeHistory(structured_output=result)))

    scraper = _scraper()
    with pytest.raises(ScraperError, match="Cloudflare Turnstile"):
        await scraper.fetch()


async def test_unsuccessful_run_with_no_structured_output_raises(creds, monkeypatch):
    history = FakeHistory(structured_output=None, successful=False, errors=["Agent got stuck on login"])
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(history))

    scraper = _scraper()
    with pytest.raises(ScraperError, match="Agent got stuck on login"):
        await scraper.fetch()


async def test_agent_exception_wrapped_as_scraper_error(creds, monkeypatch):
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(RuntimeError("browser crashed")))

    scraper = _scraper()
    with pytest.raises(ScraperError, match="browser crashed"):
        await scraper.fetch()


async def test_agent_timeout_raises_scraper_error(creds, monkeypatch):
    import asyncio

    class SlowFakeAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, max_steps=None):
            await asyncio.sleep(5)

    monkeypatch.setattr(browser_use, "Agent", SlowFakeAgent)
    settings.AGENT_TIMEOUT_SECONDS = 0.01

    scraper = _scraper()
    with pytest.raises(ScraperError, match="timed out"):
        await scraper.fetch()


async def test_unparsable_date_is_skipped_not_fatal(creds, monkeypatch):
    result = AgentScrapeResult(
        status="slots_found",
        slots=[AgentSlot(date="not-a-date", count=1), AgentSlot(date="2026-08-05", count=3)],
    )
    monkeypatch.setattr(browser_use, "Agent", make_fake_agent_class(FakeHistory(structured_output=result)))

    scraper = _scraper()
    slots = await scraper.fetch()

    assert len(slots) == 1
    assert slots[0].appt_date == date(2026, 8, 5)
