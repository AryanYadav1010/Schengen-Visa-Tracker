"""Scraper registry — maps (provider, centre, destination, visa_type) → scraper class.

Adding a new centre/country = adding one scraper file + one entry here.
The scheduler never hard-codes scraper logic; it always goes through this registry.
"""

from __future__ import annotations

from typing import Type

from app.config import settings
from app.scrapers.base import AbstractScraper, ScraperError
from app.scrapers.mock import MockScraper
from app.scrapers.tlscontact_fr import TLScontactAgentScraper
from app.scrapers.vfs_global import VFSGlobalAgentScraper
from app.scrapers.bls_spain import BLSSpainAgentScraper

# An exact (provider, centre, destination, visa_type) route, or ("provider", "*", "*", "*")
# for "every route of this provider". Unlisted routes fail loudly in live mode.
_REGISTRY: dict[tuple[str, str, str, str], Type[AbstractScraper]] = {
    # TLScontact only for the routes it serves from London
    ("tlscontact", "London", "France", "tourism"): TLScontactAgentScraper,
    ("tlscontact", "London", "Germany", "tourism"): TLScontactAgentScraper,
    # VFS Global (from London: Italy, Portugal, Netherlands, Austria, Greece, ...) and
    # BLS International (Spain) use one adapter for every route
    ("vfs", "*", "*", "*"): VFSGlobalAgentScraper,
    ("bls", "*", "*", "*"): BLSSpainAgentScraper,
}


def get_scraper(
    provider: str,
    centre: str,
    destination: str,
    visa_type: str,
    booking_url: str = "",
    credential_email: str = "",
    credential_password: str = "",
) -> AbstractScraper:
    """Return the right scraper instance for a watch.

    Falls back to MockScraper when USE_MOCK_SCRAPER=true (the default).
    """
    if settings.USE_MOCK_SCRAPER:  # mock mode: every watch gets fake data, no network
        return MockScraper(
            centre=centre,
            destination=destination,
            visa_type=visa_type,
            booking_url=booking_url,
        )

    # Exact route first, then the provider-wide wildcard
    cls = (_REGISTRY.get((provider, centre, destination, visa_type))
           or _REGISTRY.get((provider, "*", "*", "*")))

    if cls is None:
        # No real scraper registered for this combo — fail loudly rather than
        # silently returning fake MockScraper data for a "live" watch.
        raise ScraperError(
            f"No live scraper registered for provider={provider!r} "
            f"centre={centre!r} destination={destination!r} visa_type={visa_type!r}"
        )

    return cls(
        centre=centre,
        destination=destination,
        visa_type=visa_type,
        booking_url=booking_url,
        credential_email=credential_email,
        credential_password=credential_password,
    )


_PROVIDER_DEFAULT_URLS: dict[str, str] = {
    "tlscontact": TLScontactAgentScraper.DEFAULT_START_URL,
    "vfs": VFSGlobalAgentScraper.DEFAULT_START_URL,
    "bls": BLSSpainAgentScraper.DEFAULT_START_URL,
}


def default_booking_url(provider: str) -> str:
    """The provider's own site, used when a watch has no custom booking_url set."""
    return _PROVIDER_DEFAULT_URLS.get(provider, "")
