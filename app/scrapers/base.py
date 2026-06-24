"""Scraper contract: AbstractScraper interface, Slot dataclass, ScraperError."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass
class Slot:
    """A single available appointment slot (or group of slots on one day)."""

    appt_date: date
    count: int          # number of slots that day (1 if unknown)
    booking_url: str    # direct link to the official booking page


class ScraperError(Exception):
    """Raised on bot-block, CAPTCHA, timeout, or network failure.

    This must NEVER be raised when the site simply has no availability.
    An empty list[] means "no appointments"; ScraperError means "we couldn't
    determine availability — the scheduler should back off and retry."
    """


class AbstractScraper(ABC):
    """Base class every scraper adapter must implement.

    Adding a new centre/country = subclass this + one registry entry.
    """

    centre: str         # e.g. "London"
    destination: str    # e.g. "France"
    visa_type: str      # "tourism" | "business" | "long_stay"

    def __init__(
        self,
        centre: str,
        destination: str,
        visa_type: str,
        booking_url: str = "",
        credential_email: str = "",
        credential_password: str = "",
    ):
        self.centre = centre
        self.destination = destination
        self.visa_type = visa_type
        self.booking_url = booking_url
        self.credential_email = credential_email
        self.credential_password = credential_password

    @abstractmethod
    async def fetch(self) -> list[Slot]:
        """Return all available slots in the next ~90 days, or [] if none.

        Raise ScraperError on bot-block / CAPTCHA / timeout so the scheduler
        can back off rather than treating a block as 'no availability'.
        """

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"centre={self.centre!r} dest={self.destination!r} "
            f"type={self.visa_type!r}>"
        )
