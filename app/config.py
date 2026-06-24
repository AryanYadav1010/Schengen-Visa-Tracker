"""Application configuration via pydantic-settings, reads from .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Auth / sessions / credential encryption ──────────────
    SECRET_KEY: str = ""

    # ── Email sending (operator-level SMTP/Resend account) ───
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    RESEND_API_KEY: str = ""

    # ── Telegram notifications (operator-level bot) ──────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""

    # ── Google OAuth (per-user Gmail sending) ─────────────────
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/oauth/google/callback"

    # ── Scheduling ───────────────────────────────────────────
    CHECK_INTERVAL_MINUTES: int = 15
    ALERT_COOLDOWN_HOURS: int = 12

    # ── Database ─────────────────────────────────────────────
    DATABASE_URL: str = "sqlite+aiosqlite:///data.db"

    # ── Mock mode ────────────────────────────────────────────
    USE_MOCK_SCRAPER: bool = True

    # ── AI agent scraper (browser-use + Claude) ──────────────
    ANTHROPIC_API_KEY: str = ""
    AGENT_MODEL: str = "claude-sonnet-4-6"
    AGENT_MAX_STEPS: int = 30
    AGENT_TIMEOUT_SECONDS: int = 180

    @property
    def use_resend(self) -> bool:
        return bool(self.RESEND_API_KEY)


settings = Settings()
