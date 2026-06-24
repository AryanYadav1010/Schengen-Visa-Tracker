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

_REGISTRY: dict[tuple[str, str, str, str], Type[AbstractScraper]] = {
    ("tlscontact", "London", "France", "tourism"): TLScontactAgentScraper,
    ("tlscontact", "London", "Germany", "tourism"): TLScontactAgentScraper,

    # VFS Global mappings
    ("vfs", "London", "Italy", "tourism"): VFSGlobalAgentScraper,
    ("vfs", "London", "Portugal", "tourism"): VFSGlobalAgentScraper,
    ("vfs", "London", "Netherlands", "tourism"): VFSGlobalAgentScraper,
    ("vfs", "London", "Austria", "tourism"): VFSGlobalAgentScraper,
    ("vfs", "London", "Greece", "tourism"): VFSGlobalAgentScraper,
    ("vfs", "*", "*", "*"): VFSGlobalAgentScraper,

    # BLS International mappings
    ("bls", "London", "Spain", "tourism"): BLSSpainAgentScraper,
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
    if settings.USE_MOCK_SCRAPER:
        return MockScraper(
            centre=centre,
            destination=destination,
            visa_type=visa_type,
            booking_url=booking_url,
        )

    # Try exact match first
    key = (provider, centre, destination, visa_type)
    cls = _REGISTRY.get(key)

    # Try wildcard visa_type
    if cls is None:
        cls = _REGISTRY.get((provider, centre, destination, "*"))

    # Try wildcard destination
    if cls is None:
        cls = _REGISTRY.get((provider, centre, "*", visa_type))

    # Try provider-level wildcard
    if cls is None:
        cls = _REGISTRY.get((provider, "*", "*", "*"))

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
