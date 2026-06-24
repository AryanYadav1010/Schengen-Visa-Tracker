"""BLS International AI-agent scraper for United Kingdom -> Spain visa appointments.

Navigation logic lives in agent_scraper.BaseAgentScraper — this file only
supplies the provider-specific config (start URL, credential prefix).
"""

from __future__ import annotations

from typing import ClassVar

from app.scrapers.agent_scraper import BaseAgentScraper


class BLSSpainAgentScraper(BaseAgentScraper):
    """Scrapes availability from uk.blsspainvisa.com.

    Requires the user to have a "bls" Credential configured in the DB.
    """

    PROVIDER_LABEL: ClassVar[str] = "BLS Spain"
    DEFAULT_START_URL: ClassVar[str] = "https://uk.blsspainvisa.com/account/login"
