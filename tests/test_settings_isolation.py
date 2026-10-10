"""Guard: tests must never run with the developer's real credentials (see conftest.py)."""

from app.config import settings


def test_settings_are_isolated_from_dotenv():
    assert settings.USE_MOCK_SCRAPER is True
    assert settings.SMTP_PASS == ""
    assert settings.ANTHROPIC_API_KEY == ""
    assert settings.TELEGRAM_BOT_TOKEN == ""
    assert "scrapper-tests-" in settings.DATABASE_URL  # throwaway DB, never the real data.db
