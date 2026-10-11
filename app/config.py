"""Application configuration via pydantic-settings, reads from .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Each field is read from, in priority order: a real environment variable (e.g. a Fly
    secret), then the .env file, then the default below. Values are type-converted
    ("15" → 15, "true" → True). Unknown keys are ignored."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Auth / sessions / credential encryption ──────────────
    SECRET_KEY: str = ""
    ADMIN_EMAIL: str = ""  # if set, only this account may flip the process-wide /debug/mock-mode switch

    # ── Email sending (operator-level SMTP/Resend account) ───
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_TIMEOUT_SECONDS: float = 20  # per socket operation; without it smtplib can block forever
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
    ALERT_COOLDOWN_HOURS: int = 12  # don't re-email the same earliest date within this window

    # ── Database ─────────────────────────────────────────────
    # Relative path = inside the container. In production point it at the volume:
    # sqlite+aiosqlite:////data/data.db
    DATABASE_URL: str = "sqlite+aiosqlite:///data.db"

    # ── Mock mode ────────────────────────────────────────────
    USE_MOCK_SCRAPER: bool = True  # fake slots, no provider sites touched (the safe default)

    # ── AI agent scraper (browser-use + Claude) ──────────────
    ANTHROPIC_API_KEY: str = ""
    AGENT_MODEL: str = "claude-sonnet-4-6"
    AGENT_MAX_STEPS: int = 30          # cost / runaway guard: max agent actions per check
    AGENT_TIMEOUT_SECONDS: int = 180   # hard wall-clock cap per check

    @property
    def use_resend(self) -> bool:
        """Operator email goes through Resend when a key is set, otherwise SMTP."""
        return bool(self.RESEND_API_KEY)


# Built once at import; every module reads this same object.
settings = Settings()
