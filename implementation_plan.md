# Schengen Visa Appointment Tracker — Implementation Plan

## Background & Key Research Findings

You asked for **London / VFS Global UK → France (Tourism)** as the first adapter. Research revealed a critical fact:

> [!IMPORTANT]
> **France visa appointments from the UK are NOT handled by VFS Global — they go through TLScontact** (`visas-fr.tlscontact.com`). VFS Global UK handles Italy, Portugal, and some others. The first adapter must therefore be a **TLScontact scraper**, not a VFS one.

**Provider mapping for UK:**
| Destination | Provider | Booking URL |
|---|---|---|
| **France** | **TLScontact** | `https://visas-fr.tlscontact.com/` |
| Italy | VFS Global | `https://visa.vfsglobal.com/gbr/en/ita/` |
| Germany | TLScontact | `https://visas-de.tlscontact.com/` |
| Spain | BLS International | `https://uk.blsspainvisa.com/` |
| Portugal | VFS Global | `https://visa.vfsglobal.com/gbr/en/prt/` |

**Anti-bot reality:** Both TLScontact and VFS Global employ heavy protections (Cloudflare WAF/Turnstile, reCAPTCHA, email OTP, behavioral analysis, dynamic CSS classes). The scraper must:
- Use Playwright with `playwright-stealth` 
- Use realistic headers and randomised delays
- Navigate the full authenticated flow (login required to see the calendar)
- Intercept network responses for structured data where possible
- Fail safe with `ScraperError` on CAPTCHA/block, never reporting blocks as "no availability"

## User Review Required

> [!WARNING]
> **Login credentials required.** The TLScontact portal requires a logged-in session to view the appointment calendar. The scraper will need your TLScontact account email + password stored in `.env`. There is **no public calendar endpoint** — the scraper must automate the login flow.

> [!IMPORTANT]
> **CAPTCHA / OTP challenge.** TLScontact uses reCAPTCHA and may send email OTPs during login. The scraper will attempt stealth browsing, but if CAPTCHA/OTP is triggered, it will raise `ScraperError` and back off. For a fully unattended solution, you may eventually need a CAPTCHA-solving service (e.g., 2Captcha) — but we will NOT include that initially. The first version will work in scenarios where stealth avoids the CAPTCHA.

## Open Questions

> [!IMPORTANT]
> 1. **Do you already have a TLScontact account** for France from London? The scraper needs credentials (`TLSCONTACT_EMAIL` / `TLSCONTACT_PASSWORD` in `.env`).
> 2. **Are you comfortable with the risk** that aggressive checking could get your account flagged? We'll default to a conservative 15-minute interval with ±120s jitter.
> 3. Given the login-wall complexity, would you prefer I also build a **simulated/mock scraper** that returns fake data so you can fully test the UI → scheduler → email pipeline end-to-end without hitting the real site? *(Recommended — lets you verify the entire stack immediately.)*

---

## Proposed Changes

### Project Structure

```
c:\Users\abdul\Desktop\scrapper\
├── app/
│   ├── main.py              # FastAPI app, routes, startup wiring
│   ├── config.py             # pydantic-settings, reads .env
│   ├── db.py                 # SQLAlchemy engine/session, init
│   ├── models.py             # Watch, AvailabilitySnapshot, AlertLog
│   ├── scheduler.py          # APScheduler setup, the check loop
│   ├── notifier.py           # email sending (SMTP / Resend), dedup logic
│   ├── scrapers/
│   │   ├── __init__.py
│   │   ├── base.py           # AbstractScraper + Slot dataclass + ScraperError
│   │   ├── registry.py       # maps (centre, destination, visa_type) → scraper
│   │   ├── tlscontact_fr.py  # FIRST adapter: London → France (Tourism)
│   │   └── mock.py           # Mock scraper for testing the full pipeline
│   ├── templates/
│   │   └── index.html        # Single Jinja2 page
│   └── static/
│       └── style.css
├── .env.example
├── requirements.txt
├── Dockerfile
└── README.md
```

> [!NOTE]
> The original spec called the first adapter `vfs_uk.py`. Since France uses TLScontact, the file is renamed to `tlscontact_fr.py`. The architecture remains the same — one file + one registry entry per provider/destination.

---

### Component 1: Configuration

#### [NEW] [config.py](file:///c:/Users/abdul/Desktop/scrapper/app/config.py)
- `pydantic-settings` `Settings` class reading from `.env`
- Fields: `ALERT_EMAIL`, `SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASS`, `RESEND_API_KEY` (optional), `CHECK_INTERVAL_MINUTES` (default 15), `ALERT_COOLDOWN_HOURS` (default 12), `DATABASE_URL` (default `sqlite:///data.db`), `TLSCONTACT_EMAIL`, `TLSCONTACT_PASSWORD`, `USE_MOCK_SCRAPER` (default `true` for safe initial testing)

#### [NEW] [.env.example](file:///c:/Users/abdul/Desktop/scrapper/.env.example)
- Template with all config keys and comments

---

### Component 2: Database & Models

#### [NEW] [db.py](file:///c:/Users/abdul/Desktop/scrapper/app/db.py)
- SQLAlchemy async engine + session factory for SQLite
- `init_db()` function to create tables on startup
- Uses `aiosqlite` driver

#### [NEW] [models.py](file:///c:/Users/abdul/Desktop/scrapper/app/models.py)
Three tables:

**`Watch`** — what to monitor
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | auto |
| `centre` | String | e.g. "London" |
| `destination` | String | e.g. "France" |
| `visa_type` | String | "tourism" / "business" / "long_stay" |
| `provider` | String | "tlscontact" / "vfs" / "bls" |
| `enabled` | Boolean | toggle on/off |
| `booking_url` | String | direct link to the booking page |
| `last_checked_at` | DateTime | last successful scrape time |
| `last_error` | String (nullable) | last error message, null if OK |
| `backoff_until` | DateTime (nullable) | exponential backoff deadline |
| `backoff_count` | Integer | consecutive error count, resets on success |

**`AvailabilitySnapshot`** — each scrape result
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | auto |
| `watch_id` | FK → Watch | |
| `checked_at` | DateTime | |
| `earliest_date` | Date (nullable) | null = no availability |
| `slots_json` | Text | JSON list of `{date, count}` |
| `is_error` | Boolean | was this a ScraperError? |
| `error_message` | String (nullable) | |

**`AlertLog`** — dedup tracking
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | auto |
| `watch_id` | FK → Watch | |
| `alerted_at` | DateTime | |
| `earliest_date` | Date | the date we alerted about |
| `email_sent_to` | String | |

---

### Component 3: Scraper Interface + First Adapter

#### [NEW] [base.py](file:///c:/Users/abdul/Desktop/scrapper/app/scrapers/base.py)
```python
@dataclass
class Slot:
    appt_date: date
    count: int           # number of slots that day (1 if unknown)
    booking_url: str

class ScraperError(Exception):
    """Raised on bot-block, CAPTCHA, timeout — NOT 'no availability'."""
    pass

class AbstractScraper(ABC):
    centre: str
    destination: str
    visa_type: str

    @abstractmethod
    async def fetch(self) -> list[Slot]:
        """Return slots in next ~90 days, or [] if genuinely none.
        Raise ScraperError on blocks/failures."""
```

#### [NEW] [registry.py](file:///c:/Users/abdul/Desktop/scrapper/app/scrapers/registry.py)
- Dict mapping `(provider, centre, destination, visa_type)` → scraper class
- `get_scraper(watch: Watch) -> AbstractScraper` factory function
- Falls back to `MockScraper` when `USE_MOCK_SCRAPER=true`

#### [NEW] [tlscontact_fr.py](file:///c:/Users/abdul/Desktop/scrapper/app/scrapers/tlscontact_fr.py)
- Playwright Chromium (headless) with `playwright-stealth`
- **Flow:**
  1. Navigate to `https://visas-fr.tlscontact.com/` 
  2. Handle Cloudflare challenge page (wait for it to resolve)
  3. Click "Login" / navigate to login form
  4. Enter email + password from config
  5. Wait for dashboard / appointment booking page
  6. Navigate to appointment calendar
  7. Parse available dates from the calendar DOM
  8. Intercept XHR responses for structured slot data if possible
  9. Return `list[Slot]`
- On CAPTCHA / OTP / timeout → raise `ScraperError("CAPTCHA detected")`
- On Cloudflare block → raise `ScraperError("Cloudflare blocked")`
- Configurable timeout (default 60s)
- Realistic viewport (1920×1080), user-agent rotation

#### [NEW] [mock.py](file:///c:/Users/abdul/Desktop/scrapper/app/scrapers/mock.py)
- Returns configurable fake slots for testing
- Can simulate "no availability" → "new slots" transitions
- Can simulate `ScraperError` on demand

---

### Component 4: Scheduler

#### [NEW] [scheduler.py](file:///c:/Users/abdul/Desktop/scrapper/app/scheduler.py)
- APScheduler `AsyncIOScheduler` started on FastAPI `lifespan`
- Runs `check_all_watches()` every `CHECK_INTERVAL_MINUTES`
- For each enabled watch not in backoff:
  - Add random jitter ±60–120s (stagger per watch)
  - Call `scraper.fetch()`
  - On success: store `AvailabilitySnapshot`, clear backoff, update `last_checked_at`
  - On `ScraperError`: store error snapshot, increment `backoff_count`, set `backoff_until = now + 2^count * base_minutes` (capped at 60 min)
  - Never crash the scheduler — all exceptions caught and logged
- Concurrency: `max_instances=1`, `asyncio.Semaphore(3)` for parallel scrapes
- Change detection logic:
  - Compare new `earliest_date` to previous snapshot's `earliest_date`
  - "New opening" = previously null → now has date, OR new date is earlier than last alerted date
  - If new opening detected → call `notifier.send_alert(watch, slots)`

---

### Component 5: Email Notifications

#### [NEW] [notifier.py](file:///c:/Users/abdul/Desktop/scrapper/app/notifier.py)
- **SMTP path** (default): `smtplib.SMTP` with TLS, Gmail App Password
- **Resend path** (if `RESEND_API_KEY` set): `httpx.post` to Resend API
- `send_alert(watch, slots)`:
  - Check cooldown: query `AlertLog` for this `watch_id` + `earliest_date` within last `ALERT_COOLDOWN_HOURS` → skip if found
  - Build email:
    - Subject: `🔔 New {destination} ({visa_type}) appointment — {centre}`
    - Body: earliest date, slot counts per month, booking URL, checked-at timestamp
    - Both plain text and minimal HTML versions
  - Send email
  - Log to `AlertLog`
- `send_test_email()`: sends a test message to verify SMTP config

---

### Component 6: FastAPI App & Routes

#### [NEW] [main.py](file:///c:/Users/abdul/Desktop/scrapper/app/main.py)
- FastAPI app with `lifespan` handler (init DB, start scheduler, install Playwright browser)
- Mount `/static` for CSS
- Jinja2 templates

**Routes:**
| Method | Path | Purpose |
|---|---|---|
| `GET /` | Render `index.html` | Main page with table |
| `GET /api/state` | JSON | All watches + latest snapshots for auto-refresh |
| `POST /api/watch/{id}/toggle` | JSON | Enable/disable a watch |
| `POST /api/settings` | JSON | Update centre, visa_type, alert email, interval |
| `POST /api/test-email` | JSON | Send test email |
| `POST /api/check-now` | JSON | Trigger immediate check for all/one watch |
| `GET /debug/scrape?watch_id=...` | JSON | Run one scrape, return raw Slot data |

---

### Component 7: Frontend

#### [NEW] [index.html](file:///c:/Users/abdul/Desktop/scrapper/app/templates/index.html)
- Server-rendered Jinja2 template
- Clean, modern design inspired by schengenappointments.com
- **Table columns:** Destination 🏳️ | Earliest Available | Month 1 | Month 2 | Month 3 | Toggle
- Color coding: green for available, red/grey for none, amber for errors
- "Last checked X min ago" badge per row
- Booking link per row
- **Collapsible Settings panel:** residence centre dropdown, visa type tabs, alert email input, check interval, save button
- **Buttons:** "Check Now" (manual trigger), "Send Test Email"
- Error states: "temporarily blocked, retrying" with backoff indicator
- Vanilla JS: `setInterval(fetch('/api/state'), 60000)` to auto-refresh table without page reload
- Toggle switches call `POST /api/watch/{id}/toggle`

#### [NEW] [style.css](file:///c:/Users/abdul/Desktop/scrapper/app/static/style.css)
- Clean, readable design on white background
- Mobile-responsive table (horizontal scroll on small screens)
- Color tokens for status (green/red/amber/grey)
- Smooth transitions for toggles, settings panel expand/collapse
- Google Font (Inter) for modern typography
- Subtle hover effects on rows

---

### Component 8: Packaging

#### [NEW] [requirements.txt](file:///c:/Users/abdul/Desktop/scrapper/requirements.txt)
```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0
aiosqlite>=0.20
pydantic-settings>=2.5
playwright>=1.48
playwright-stealth>=1.0
apscheduler>=3.10
httpx>=0.27
jinja2>=3.1
python-multipart>=0.0.12
```

#### [NEW] [Dockerfile](file:///c:/Users/abdul/Desktop/scrapper/Dockerfile)
- Python 3.11-slim base
- Install system deps for Playwright Chromium
- `pip install -r requirements.txt`
- `playwright install chromium --with-deps`
- Copy app code
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`

#### [NEW] [README.md](file:///c:/Users/abdul/Desktop/scrapper/README.md)
- Setup steps (venv, install, playwright install, .env config)
- Run locally with `uvicorn`
- Docker build & run
- Configuration reference
- Adding new scrapers guide

---

## Milestones (execution order)

1. **Skeleton** — FastAPI + SQLite + empty `index.html` renders at `/`. Verify with `uvicorn`.
2. **Scraper interface + mock adapter** — `base.py`, `registry.py`, `mock.py`. Verify via `GET /debug/scrape?watch_id=1`.
3. **TLScontact France adapter** — `tlscontact_fr.py` with Playwright stealth. Verify via debug route with real credentials.
4. **Persistence + change detection** — `AvailabilitySnapshot` storage, earliest-date comparison logic.
5. **Scheduler** — APScheduler with jitter, backoff, semaphore-limited concurrency.
6. **Email alerts + dedup** — SMTP sending, cooldown logic, `AlertLog`, "Send Test Email" button.
7. **Full UI + polish** — Complete table, settings panel, auto-refresh, error states, README, Dockerfile.

---

## Verification Plan

### Automated Tests
- `GET /debug/scrape?watch_id=1` with mock scraper → returns valid JSON slots
- `POST /api/test-email` → delivers to inbox
- Manually toggle `USE_MOCK_SCRAPER=false` and test with real TLScontact credentials

### Manual Verification
- `uvicorn app.main:app` starts; visiting `/` shows the table
- Enabling a watch → scheduler checks on interval → "last checked" updates
- Mock scraper: transition from "no availability" to "5 Aug" → exactly one email sent
- Repeat within cooldown → no duplicate email
- Mock `ScraperError` → row shows "temporarily blocked, retrying" + backoff
- "Check Now" button → immediate scrape + table update
- Mobile layout: table scrolls horizontally, settings panel works
