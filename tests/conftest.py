"""Test-wide isolation from the developer's real .env and from the network.

pydantic-settings gives real environment variables priority over values read
from .env, so setting every secret-bearing setting here — before pytest imports
any test module, and therefore before app.config builds `settings` — guarantees
the suite runs with blank credentials and mock mode, whatever .env contains.
"""

import os
import tempfile

os.environ.update(
    {
        "SECRET_KEY": "test-secret-key",
        "ADMIN_EMAIL": "",
        "SMTP_USER": "",
        "SMTP_PASS": "",
        "SMTP_HOST": "127.0.0.1",  # closed port: an unstubbed send fails fast instead of emailing anyone
        "SMTP_PORT": "9",
        "RESEND_API_KEY": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_BOT_USERNAME": "",
        "GOOGLE_OAUTH_CLIENT_ID": "",
        "GOOGLE_OAUTH_CLIENT_SECRET": "",
        "ANTHROPIC_API_KEY": "",
        "USE_MOCK_SCRAPER": "true",
        # A throwaway file, not :memory: (each new connection to :memory: is a new, empty DB)
        "DATABASE_URL": f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='scrapper-tests-')}/app.db",
        # browser-use: no telemetry, and keep its config dir out of ~/.config
        "ANONYMIZED_TELEMETRY": "false",
        "BROWSER_USE_CONFIG_DIR": tempfile.mkdtemp(prefix="browseruse-tests-"),
    }
)
