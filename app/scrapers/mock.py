"""Mock scraper for end-to-end pipeline testing without hitting real sites.

Behaviour is controlled by environment / internal state:
 - By default, returns a handful of fake slots spread over the next ~90 days.
 - A class-level toggle can simulate "no availability" or ScraperError.
 - The /debug/mock-mode endpoint can flip these at runtime for testing.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from app.scrapers.base import AbstractScraper, ScraperError, Slot


class MockScraper(AbstractScraper):
    """Returns fake appointment data for testing the full pipeline."""

    # ── Class-level toggles (shared across all instances) ────
    _force_error: bool = False        # raise ScraperError on next fetch
    _force_empty: bool = False        # return [] (no availability)
    _error_message: str = "Simulated CAPTCHA / Cloudflare block"

    async def fetch(self) -> list[Slot]:
        # 1. Simulate a blocked / error state
        if MockScraper._force_error:
            raise ScraperError(MockScraper._error_message)

        # 2. Simulate no availability
        if MockScraper._force_empty:
            return []

        # 3. Generate realistic-looking fake slots
        slots: list[Slot] = []
        today = date.today()

        # Scatter 3–8 appointment days across the next 90 days
        num_days = random.randint(3, 8)
        offsets = sorted(random.sample(range(5, 90), num_days))

        for offset in offsets:
            appt_date = today + timedelta(days=offset)
            count = random.randint(1, 5)
            slots.append(
                Slot(
                    appt_date=appt_date,
                    count=count,
                    booking_url=self.booking_url or f"https://example.com/book/{self.destination.lower()}",
                )
            )

        return slots

    # ── Class methods to toggle mock behaviour at runtime ────

    @classmethod
    def set_force_error(cls, on: bool, message: str = "") -> None:
        cls._force_error = on
        if message:
            cls._error_message = message

    @classmethod
    def set_force_empty(cls, on: bool) -> None:
        cls._force_empty = on

    @classmethod
    def reset(cls) -> None:
        cls._force_error = False
        cls._force_empty = False
        cls._error_message = "Simulated CAPTCHA / Cloudflare block"

    @classmethod
    def current_mode(cls) -> str:
        """Return the current sandbox mode: 'error' | 'empty' | 'normal'."""
        if cls._force_error:
            return "error"
        if cls._force_empty:
            return "empty"
        return "normal"
