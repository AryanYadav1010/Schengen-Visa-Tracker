import pytest

from app.config import settings
from app.scrapers.base import ScraperError
from app.scrapers.bls_spain import BLSSpainAgentScraper
from app.scrapers.mock import MockScraper
from app.scrapers.registry import get_scraper
from app.scrapers.tlscontact_fr import TLScontactAgentScraper
from app.scrapers.vfs_global import VFSGlobalAgentScraper


@pytest.fixture
def use_mock_mode():
    original = settings.USE_MOCK_SCRAPER
    settings.USE_MOCK_SCRAPER = True
    yield
    settings.USE_MOCK_SCRAPER = original


@pytest.fixture
def use_live_mode():
    original = settings.USE_MOCK_SCRAPER
    settings.USE_MOCK_SCRAPER = False
    yield
    settings.USE_MOCK_SCRAPER = original


def test_mock_mode_always_returns_mock_scraper(use_mock_mode):
    scraper = get_scraper(provider="bls", centre="London", destination="Spain", visa_type="tourism")
    assert isinstance(scraper, MockScraper)


def test_live_mode_resolves_exact_match(use_live_mode):
    scraper = get_scraper(provider="tlscontact", centre="London", destination="France", visa_type="tourism")
    assert isinstance(scraper, TLScontactAgentScraper)


def test_live_mode_resolves_provider_wildcard(use_live_mode):
    scraper = get_scraper(provider="vfs", centre="London", destination="Sweden", visa_type="tourism")
    assert isinstance(scraper, VFSGlobalAgentScraper)


def test_live_mode_resolves_bls_spain(use_live_mode):
    scraper = get_scraper(provider="bls", centre="London", destination="Spain", visa_type="tourism")
    assert isinstance(scraper, BLSSpainAgentScraper)


def test_live_mode_unmatched_provider_raises_scraper_error(use_live_mode):
    with pytest.raises(ScraperError):
        get_scraper(provider="unknown_provider", centre="London", destination="Nowhere", visa_type="tourism")
