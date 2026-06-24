"""TLScontact AI-agent scraper for London -> France / Germany visa appointments.

Navigation logic lives in agent_scraper.BaseAgentScraper — this file only
supplies the provider-specific config (start URL, credential prefix).
"""

from __future__ import annotations

from typing import ClassVar

from app.scrapers.agent_scraper import BaseAgentScraper


class TLScontactAgentScraper(BaseAgentScraper):
    """Scrapes availability from visas-fr.tlscontact.com / visas-de.tlscontact.com.

    Requires the user to have a "tlscontact" Credential configured in the DB.
    """

    PROVIDER_LABEL: ClassVar[str] = "TLScontact"
    DEFAULT_START_URL: ClassVar[str] = "https://visas-fr.tlscontact.com/"
