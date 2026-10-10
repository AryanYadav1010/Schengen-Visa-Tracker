"""F-02: an inconsistent or malformed agent result must never become "no availability".

The fake history parses raw JSON with AgentScrapeResult.model_validate_json —
exactly what browser-use's AgentHistoryList.structured_output does — so these
tests exercise the same validation path a real LLM response would.
"""

from __future__ import annotations

import logging
from datetime import date

import browser_use
import pytest
from pydantic import ValidationError

from app.config import settings
from app.scrapers.agent_scraper import AgentScrapeResult
from app.scrapers.base import ScraperError
from app.scrapers.tlscontact_fr import TLScontactAgentScraper

CANARY = "leak-canary-7f3a"  # marker for "raw LLM output must not be echoed"


class JsonHistory:
    def __init__(self, raw_json: str):
        self._raw = raw_json

    @property
    def structured_output(self):
        return AgentScrapeResult.model_validate_json(self._raw)

    def is_successful(self):
        return True

    def errors(self):
        return []


@pytest.fixture
def agent_returns(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-fake-test-key")

    def _install(raw_json: str):
        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps=None):
                return JsonHistory(raw_json)

        monkeypatch.setattr(browser_use, "Agent", FakeAgent)

    return _install


def _scraper():
    return TLScontactAgentScraper(
        centre="London", destination="France", visa_type="tourism",
        credential_email="test@example.com", credential_password="hunter2",
    )


@pytest.mark.parametrize(
    "raw_json",
    [
        pytest.param('{"status": "slots_found", "slots": []}', id="slots_found-but-empty"),
        pytest.param('{"status": "no_availability", "slots": [{"date": "2026-11-02", "count": 2}]}',
                     id="no_availability-but-has-slots"),
        pytest.param('{"status": "error", "slots": []}', id="unknown-status-empty"),
        pytest.param('{"status": "Blocked", "slots": [], "reason": "CAPTCHA"}', id="wrong-case-blocked"),
        pytest.param('{"status": "maybe", "slots": [{"date": "2026-11-02", "count": 1}]}',
                     id="unknown-status-with-slots"),
        pytest.param('{"status": "slots_found", "slots": [{"date": "02/11/2026", "count": 1}]}',
                     id="all-dates-unparsable"),
        pytest.param('{"slots": []}', id="missing-status"),
        pytest.param('{"status": "slots_found", "slots": [', id="truncated-json"),
        # A run that stops at max_steps without calling done: final_result() is the last
        # action's plain text, which browser-use still tries to parse as JSON.
        pytest.param("Clicked element 12", id="non-json-final-result"),
    ],
)
async def test_invalid_result_raises_scraper_error(agent_returns, raw_json):
    agent_returns(raw_json)
    with pytest.raises(ScraperError):
        await _scraper().fetch()


async def test_schema_violation_is_chained_and_not_echoed(agent_returns):
    agent_returns(f'{{"status": "{CANARY}", "slots": [{{"date": "{CANARY}", "count": 1}}]}}')
    with pytest.raises(ScraperError) as exc_info:
        await _scraper().fetch()
    assert "invalid result" in str(exc_info.value)
    assert CANARY not in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValidationError)  # raise ... from e keeps the cause


async def test_all_dates_invalid_raises_without_echoing_dates(agent_returns, caplog):
    agent_returns(f'{{"status": "slots_found", "slots": [{{"date": "{CANARY}-1", "count": 1}}, '
                  f'{{"date": "{CANARY}-2", "count": 1}}]}}')
    with caplog.at_level(logging.WARNING, logger="app.scrapers.agent_scraper"):
        with pytest.raises(ScraperError, match="none had a valid date") as exc_info:
            await _scraper().fetch()
    assert CANARY not in str(exc_info.value)
    assert CANARY not in caplog.text
    assert "2 of 2" in caplog.text


# ── Valid results: current behaviour that must be preserved ─────────────────

async def test_valid_no_availability_returns_empty_list(agent_returns):
    agent_returns('{"status": "no_availability", "slots": []}')
    assert await _scraper().fetch() == []


async def test_valid_slots_found_returns_slots(agent_returns):
    agent_returns('{"status": "slots_found", "slots": [{"date": "2026-11-02", "count": 3}]}')
    slots = await _scraper().fetch()
    assert [(s.appt_date, s.count) for s in slots] == [(date(2026, 11, 2), 3)]


async def test_blocked_with_reason_raises_with_reason(agent_returns):
    agent_returns('{"status": "blocked", "slots": [], "reason": "Cloudflare Turnstile"}')
    with pytest.raises(ScraperError, match="Cloudflare Turnstile"):
        await _scraper().fetch()


async def test_blocked_without_reason_still_raises(agent_returns):
    agent_returns('{"status": "blocked", "slots": []}')
    with pytest.raises(ScraperError):
        await _scraper().fetch()


async def test_one_bad_date_among_valid_ones_is_skipped_and_logged_safely(agent_returns, caplog):
    agent_returns(f'{{"status": "slots_found", "slots": '
                  f'[{{"date": "{CANARY}", "count": 1}}, {{"date": "2026-11-02", "count": 1}}]}}')
    with caplog.at_level(logging.WARNING, logger="app.scrapers.agent_scraper"):
        slots = await _scraper().fetch()
    assert [s.appt_date for s in slots] == [date(2026, 11, 2)]
    assert "1 of 2" in caplog.text and "indexes [0]" in caplog.text
    assert CANARY not in caplog.text
