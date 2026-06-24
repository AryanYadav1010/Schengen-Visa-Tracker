import pytest

from app.scrapers.base import ScraperError
from app.scrapers.mock import MockScraper


@pytest.fixture(autouse=True)
def reset_mock_state():
    MockScraper.reset()
    yield
    MockScraper.reset()


async def test_fetch_returns_slots_by_default():
    scraper = MockScraper(centre="London", destination="France", visa_type="tourism", booking_url="https://example.com")
    slots = await scraper.fetch()
    assert len(slots) >= 3
    assert all(s.booking_url == "https://example.com" for s in slots)
    assert MockScraper.current_mode() == "normal"


async def test_force_empty_returns_no_slots():
    MockScraper.set_force_empty(True)
    scraper = MockScraper(centre="London", destination="France", visa_type="tourism")
    slots = await scraper.fetch()
    assert slots == []
    assert MockScraper.current_mode() == "empty"


async def test_force_error_raises_scraper_error():
    MockScraper.set_force_error(True, "Simulated block for testing")
    scraper = MockScraper(centre="London", destination="France", visa_type="tourism")
    with pytest.raises(ScraperError, match="Simulated block for testing"):
        await scraper.fetch()
    assert MockScraper.current_mode() == "error"


async def test_reset_restores_normal_mode():
    MockScraper.set_force_error(True)
    MockScraper.reset()
    assert MockScraper.current_mode() == "normal"
    scraper = MockScraper(centre="London", destination="Italy", visa_type="tourism")
    slots = await scraper.fetch()
    assert slots != []
