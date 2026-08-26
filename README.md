# 🔭 Schengen Visa Appointment Tracker

A self-hosted web app that monitors Schengen visa appointment availability across visa centres and **emails you the instant a new appointment slot opens**.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)
![SQLite](https://img.shields.io/badge/SQLite-local-lightgrey)

## Features

- **Multi-user accounts** — Email/password auth; each user's watches, provider credentials, and notification links are private to them
- **Live dashboard** — Table of destinations showing earliest available date, slot counts per month, last-checked timestamps
- **Email alerts** — Instant notification when a watched destination gains a new or earlier appointment slot, sent via operator SMTP/Resend or the user's own linked Gmail (Google OAuth)
- **Telegram alerts** — Optional: link a Telegram chat to also get pinged there
- **Smart dedup** — Cooldown prevents duplicate alerts for the same earliest date
- **Exponential backoff** — Handles bot-blocks gracefully without crashing
- **Mock mode** — Test the full pipeline without hitting real visa sites
- **Pluggable scrapers** — Add new centres/countries by adding one file + one registry entry
- **AI-agent-driven real scrapers** — TLScontact (France, Germany), VFS Global (Italy, Portugal, Netherlands, Austria, Greece), BLS International (Spain). Instead of hardcoded selectors, a [browser-use](https://github.com/browser-use/browser-use) agent backed by Claude reads each page and decides how to log in and find the calendar, so it survives site redesigns that would break a scripted scraper. See [Going Live](#going-live-with-real-scrapers) before relying on these — it costs real Anthropic API spend per check and needs validation against the real sites.

## Quick Start

### 1. Clone & create virtual environment

```bash
cd scrapper
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browser (needed for real scrapers)

```bash
playwright install chromium
```

The real scrapers are driven by an AI agent ([browser-use](https://github.com/browser-use/browser-use) + Claude), not hardcoded selectors — see [Going Live](#going-live-with-real-scrapers).

### 4. Configure environment

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Edit `.env` — at minimum set `SECRET_KEY` (sessions + credential encryption) and, if you want real emails, your SMTP settings:

```env
SECRET_KEY=any-long-random-string
SMTP_USER=your-gmail@gmail.com
SMTP_PASS=your-gmail-app-password
```

### 5. Run

```bash
uvicorn app.main:app --reload
```

Visit **http://localhost:8000**, register an account, and log in.

The app ships with `USE_MOCK_SCRAPER=true` by default, so it works immediately with realistic fake data — no provider credentials needed to try it out. Per-provider login credentials (TLScontact/VFS/BLS) are entered per-user from the dashboard once you're ready to run real scrapers, not set globally in `.env`.

## Configuration (.env)

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | Used to sign session cookies and encrypt stored provider credentials — set this to a long random string |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | Operator SMTP username, used as the sending account when a user hasn't linked their own Gmail |
| `SMTP_PASS` | — | SMTP password (Gmail App Password) |
| `RESEND_API_KEY` | — | Optional: use Resend instead of SMTP |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` | — | Optional: lets users link a Telegram chat for alerts |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | — | Optional: lets users link their own Gmail to send alerts from their own account instead of the operator SMTP |
| `CHECK_INTERVAL_MINUTES` | `15` | How often to check (minutes) |
| `ALERT_COOLDOWN_HOURS` | `12` | Don't re-alert same date within this window |
| `DATABASE_URL` | `sqlite+aiosqlite:///data.db` | SQLAlchemy async database URL |
| `USE_MOCK_SCRAPER` | `true` | Use fake data for testing instead of hitting real visa sites |
| `ANTHROPIC_API_KEY` | — | Required for any real (non-mock) scraper — the AI agent needs Claude to drive the browser |
| `AGENT_MODEL` | `claude-sonnet-4-6` | Claude model the agent uses |
| `AGENT_MAX_STEPS` | `30` | Hard cap on agent actions per check (cost/runaway guard) |
| `AGENT_TIMEOUT_SECONDS` | `180` | Hard wall-clock cap per check |

## Docker

The SQLite database (`data.db`) lives inside the container by default — mount it as a volume too, or your watch history and alert dedup log are lost every time the container is recreated.

```bash
docker build -t schengen-tracker .

# macOS/Linux — create an empty data.db first so Docker bind-mounts a file, not a directory
touch data.db
docker run -d -p 8000:8000 \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/data.db:/app/data.db \
  --name tracker schengen-tracker
```

```powershell
# Windows PowerShell
if (-not (Test-Path data.db)) { New-Item -ItemType File data.db }
docker run -d -p 8000:8000 `
  -v "${PWD}\.env:/app/.env" `
  -v "${PWD}\data.db:/app/data.db" `
  --name tracker schengen-tracker
```

> Docker wasn't available to verify the build in this environment — run `docker build` yourself before relying on it for deployment.

## Architecture

```
app/
  main.py            # FastAPI app, routes, startup
  config.py          # pydantic-settings (.env)
  db.py              # SQLAlchemy async engine/session
  models.py          # User, Watch, Credential, AvailabilitySnapshot, AlertLog
  auth.py            # Password hashing, session helpers, credential encryption
  google_oauth.py    # Google OAuth flow (send alerts from a user's own Gmail)
  telegram.py         # Telegram bot polling + chat-link + send
  scheduler.py       # APScheduler check loop
  notifier.py        # Email (SMTP / Resend / Gmail) + Telegram + dedup
  scrapers/
    base.py          # AbstractScraper + Slot + ScraperError
    registry.py      # Maps watch → scraper class (raises ScraperError if unmatched in live mode)
    mock.py          # Mock adapter for testing
    agent_scraper.py # BaseAgentScraper: shared browser-use + Claude agent loop, structured output schema
    tlscontact_fr.py # TLScontact config (France, Germany) — just URL + credential prefix
    vfs_global.py    # VFS Global config (Italy, Portugal, Netherlands, Austria, Greece)
    bls_spain.py     # BLS International config (Spain)
  templates/
    index.html       # Dashboard (Jinja2)
    login.html        # Login page
    register.html      # Registration page
  static/
    style.css        # Vanilla CSS
tests/               # pytest suite (mock scraper, registry, alert cooldown, agent scraper, auth, google oauth, telegram)
```

Real providers no longer use hand-written Playwright selectors. `agent_scraper.BaseAgentScraper` drives a [browser-use](https://github.com/browser-use/browser-use) `Agent` (Claude + a real Chromium session) that reads each page itself and decides how to log in and find the calendar — it returns a structured `{status, slots, reason}` result that gets mapped onto the same `list[Slot]` / `ScraperError` contract every scraper has always used, so the scheduler/registry/notifier code didn't need to change at all.

## Adding a New Scraper

Since every real provider goes through the same AI agent loop, adding one is just config — no navigation code to write:

1. Create `app/scrapers/my_new_provider.py`:
   ```python
   from typing import ClassVar
   from app.scrapers.agent_scraper import BaseAgentScraper

   class MyNewProviderScraper(BaseAgentScraper):
       PROVIDER_LABEL: ClassVar[str] = "My New Provider"
       DEFAULT_START_URL: ClassVar[str] = "https://example-visa-provider.com/login"
       CREDENTIAL_ENV_PREFIX: ClassVar[str] = "MYPROVIDER"  # reads MYPROVIDER_EMAIL/_PASSWORD from settings
   ```

2. Add `MYPROVIDER_EMAIL` / `MYPROVIDER_PASSWORD` to `config.py` and `.env.example`.

3. Register it in `app/scrapers/registry.py`:
   ```python
   from app.scrapers.my_new_provider import MyNewProviderScraper

   _REGISTRY = {
       ("myprovider", "Centre", "Country", "tourism"): MyNewProviderScraper,
   }
   ```

4. Add a `Watch` entry in `DEFAULT_WATCHES` in `main.py` or via the database.

If a provider needs task instructions beyond the generic template (e.g. an unusual multi-step visa-category picker), override `TASK_TEMPLATE` or `fetch()` in the subclass — see `agent_scraper.py` for the default prompt.

## API Endpoints

Every route below except `/register`, `/login`, and `/healthz` requires an authenticated session — the app is multi-user, and each user's watches/credentials are isolated to their own account.

| Method | Path | Description |
|---|---|---|
| `GET/POST /register` | HTML | Create an account |
| `GET/POST /login` | HTML | Sign in |
| `POST /logout` | — | End session |
| `GET /` | HTML | Dashboard |
| `GET /api/state` | JSON | Current user's watches + latest snapshots (auto-refresh) |
| `POST /api/watch` | JSON | Create a watch |
| `PATCH /api/watch/{id}` | JSON | Update a watch's `alert_before_date` |
| `DELETE /api/watch/{id}` | JSON | Delete a watch |
| `POST /api/watch/{id}/toggle` | JSON | Enable/disable a watch |
| `GET /api/credentials` | JSON | Which provider credentials are linked |
| `POST /api/credentials` | JSON | Save (encrypted) provider login credentials |
| `GET /api/telegram/status` | JSON | Telegram link status |
| `POST /api/telegram/link` | JSON | Link a Telegram chat for alerts |
| `POST /api/telegram/unlink` | JSON | Unlink Telegram |
| `GET /oauth/google/start` | Redirect | Begin Google OAuth (send alerts from your own Gmail) |
| `GET /oauth/google/callback` | Redirect | OAuth callback |
| `GET /api/google/status` | JSON | Google account link status |
| `POST /api/google/unlink` | JSON | Unlink Google account |
| `POST /api/test-email` | JSON | Send a test email |
| `POST /api/check-now` | JSON | Check all of the current user's enabled watches now |
| `POST /api/check-now/{id}` | JSON | Check a single watch now |
| `GET /debug/scrape?watch_id=N` | JSON | One-off scrape (for a watch you own) |
| `POST /debug/mock-mode` | JSON | Switch mock mode at runtime |
| `GET /healthz` | JSON | Health check (used by Fly.io) |

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Tests cover the mock scraper's three modes, registry resolution (including the unmatched-provider failure path), alert cooldown/dedup logic, and the agent scraper's credential/timeout/status-mapping logic (with `browser_use.Agent` mocked out — no API key, browser, or network calls needed). They don't touch real visa sites, call a real LLM, or send real emails.

## Going Live With Real Scrapers

`USE_MOCK_SCRAPER=true` is the safe default. Switching to `false` makes the scheduler drive a real AI agent (browser-use + Claude, real headless Chromium) against TLScontact, VFS Global, and BLS International. Before you do:

- **You need real accounts and an Anthropic API key.** Each provider requires a registered account for the relevant centre/destination (`TLSCONTACT_EMAIL`/`PASSWORD` etc. in `.env`), and every check calls Claude (`ANTHROPIC_API_KEY`) to drive the browser — there's no free/local path for the real scrapers.
- **This has never been run against the real sites.** The agent's task prompts (in `agent_scraper.py`) were written from documentation/inspection, not validated end-to-end against a real logged-in session — I don't have an Anthropic API key or your provider credentials in this environment, so the only validation done is unit tests with the agent mocked out. Run `GET /debug/scrape?watch_id=N` against **one** watch first, watch the logs, and read what the agent actually did before trusting the dashboard.
- **Cost and latency are real, and scale with check frequency × watch count.** Each check is several Claude API calls (one per agent action — login, navigate, read calendar, etc.), not a free scripted run. With the default 15-minute interval and several watches enabled, this can add up quickly. Start with one watch, a longer interval, and check your Anthropic usage dashboard before enabling more.
- **Built-in guardrails, but not a guarantee.** The agent has a hard step cap (`AGENT_MAX_STEPS`) and wall-clock timeout (`AGENT_TIMEOUT_SECONDS`) per check, is restricted to the target provider's domain (`allowed_domains`), is explicitly instructed to never solve a CAPTCHA and never proceed past viewing the calendar (no payment/booking/personal-data forms), and is told to treat all page content as untrusted data — not instructions — to resist prompt injection from a compromised or adversarial page. None of this is a hard technical sandbox; it's instruction-following plus hard numeric limits, same caveat as any LLM agent.
- **Credentials are kept out of the agent's context.** `browser-use`'s `sensitive_data` mechanism substitutes your real email/password into form fields directly — the LLM only ever sees placeholder names (`x_username`/`x_password`), never the real values, so credentials can't leak into prompts or transcripts.
- **Anti-bot defenses can still flag accounts.** These sites use Cloudflare/Turnstile, CAPTCHA, and sometimes OTP challenges. The agent fails safe (`status="blocked"` → `ScraperError` → backoff) rather than reporting a block as "no availability," but aggressive check intervals still carry a risk of triggering rate limits or account flags.

Enable one watch at a time, watch its `last_error`/`backoff_until` fields after a `Check Now`, and only flip more watches to live once you've confirmed a given provider's adapter actually returns data for your account.
