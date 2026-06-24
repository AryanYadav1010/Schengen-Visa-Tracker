"""VFS Global AI-agent scraper for United Kingdom -> various Schengen destinations.

Navigation logic lives in agent_scraper.BaseAgentScraper — this file only
supplies the provider-specific config (start URL, credential prefix).
"""

from __future__ import annotations

from typing import ClassVar

from app.scrapers.agent_scraper import BaseAgentScraper


class VFSGlobalAgentScraper(BaseAgentScraper):
    """Scrapes availability from visa.vfsglobal.com.

    Requires the user to have a "vfs" Credential configured in the DB.
    """

    PROVIDER_LABEL: ClassVar[str] = "VFS Global"
    DEFAULT_START_URL: ClassVar[str] = "https://visa.vfsglobal.com/gbr/en/"
