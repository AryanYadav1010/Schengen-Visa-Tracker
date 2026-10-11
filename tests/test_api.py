"""HTTP API: authentication, per-user isolation, input validation, and the mock pipeline
end-to-end (check → snapshot → queued alert → recorded delivery failure).

Runs the real app (lifespan included) against the throwaway DB from conftest.py.
SMTP points at a closed local port, so every delivery attempt fails fast — on purpose.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

DB_PATH = settings.DATABASE_URL.removeprefix("sqlite+aiosqlite:///")
WATCH = {"centre": "London", "destination": "Italy", "visa_type": "tourism", "provider": "vfs"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # runs startup (init_db, scheduler) and shutdown
        yield c


def sign_in(client, email, password="correct-horse-battery"):
    client.cookies.clear()
    r = client.post("/register", data={"email": email, "password": password}, follow_redirects=False)
    if r.status_code == 400:  # already registered by an earlier test
        r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303


def db(query):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(query).fetchall()


def test_health_reports_db_and_scheduler_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_api_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/watch", json=WATCH).status_code == 401


def test_wrong_password_is_rejected(client):
    sign_in(client, "alice@example.test")
    client.cookies.clear()
    r = client.post("/login", data={"email": "alice@example.test", "password": "nope-nope-nope"},
                    follow_redirects=False)
    assert r.status_code == 400


@pytest.mark.parametrize("body", [
    {**WATCH, "provider": "nope"},
    {**WATCH, "centre": {"a": 1}},
    {**WATCH, "alert_before_date": "next tuesday"},
    {k: v for k, v in WATCH.items() if k != "centre"},
])
def test_invalid_watch_input_is_a_422_not_a_500(client, body):
    sign_in(client, "alice@example.test")
    r = client.post("/api/watch", json=body)
    assert r.status_code == 422 and r.json()["ok"] is False


def test_malformed_json_is_a_422_not_a_500(client):
    sign_in(client, "alice@example.test")
    r = client.post("/api/watch", content=b'{"centre":', headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_rejected_credentials_are_not_echoed_back(client):
    sign_in(client, "alice@example.test")
    r = client.post("/api/credentials", json={"provider": "nope", "email": "a@x.test", "password": "s3cret-pw"})
    assert r.status_code == 422 and "s3cret-pw" not in r.text


def test_watch_lifecycle_and_cutoff_clearing(client):
    sign_in(client, "alice@example.test")
    watch_id = client.post("/api/watch", json={**WATCH, "alert_before_date": ""}).json()["watch"]["id"]
    assert client.patch(f"/api/watch/{watch_id}", json={"alert_before_date": "2026-12-01"}).json()["watch"][
        "alert_before_date"] == "2026-12-01"
    assert client.patch(f"/api/watch/{watch_id}", json={"alert_before_date": ""}).json()["watch"][
        "alert_before_date"] is None
    scrape = client.get(f"/debug/scrape?watch_id={watch_id}").json()   # owner, mock mode
    assert scrape["scraper"].startswith("<MockScraper") and scrape["count"] == len(scrape["slots"]) > 0
    assert client.post(f"/api/watch/{watch_id}/toggle").json() == {"ok": True, "enabled": False}
    assert client.delete(f"/api/watch/{watch_id}").json() == {"ok": True}


def test_check_now_persists_snapshot_and_records_failed_delivery(client):
    sign_in(client, "carol@example.test")
    watch_id = client.post("/api/watch", json=WATCH).json()["watch"]["id"]

    [result] = client.post("/api/check-now").json()["results"]

    assert result["status"] == "ok" and result["alert_queued"] is True
    assert db(f"SELECT is_error FROM availability_snapshots WHERE watch_id={watch_id}") == [(0,)]
    assert db(f"SELECT status, attempts, last_error FROM alert_outbox WHERE watch_id={watch_id}") == [
        ("pending", 1, "ConnectionRefusedError")]


def test_users_cannot_see_or_touch_each_others_watches(client):
    sign_in(client, "alice@example.test")
    watch_id = client.post("/api/watch", json=WATCH).json()["watch"]["id"]

    sign_in(client, "mallory@example.test")
    assert client.get("/api/state").json()["watches"] == []
    assert client.patch(f"/api/watch/{watch_id}", json={}).status_code == 404
    assert client.post(f"/api/watch/{watch_id}/toggle").status_code == 404
    assert client.post(f"/api/check-now/{watch_id}").status_code == 404
    assert client.get(f"/debug/scrape?watch_id={watch_id}").status_code == 404
    assert client.delete(f"/api/watch/{watch_id}").status_code == 404
    assert client.post("/debug/mock-mode", json={"mode": "error"}).status_code == 403
    assert db(f"SELECT count(*) FROM watches WHERE id={watch_id}") == [(1,)]


def test_http_client_request_urls_are_not_logged():
    # httpx logs full URLs at INFO; Telegram's contains the bot token.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_admin_match_is_case_insensitive(client, monkeypatch):
    # Emails are stored lowercased; an ADMIN_EMAIL configured with capitals must still match.
    monkeypatch.setattr(settings, "ADMIN_EMAIL", " Admin@Example.TEST ")
    sign_in(client, "admin@example.test")
    assert client.post("/debug/mock-mode", json={"mode": "normal"}).status_code == 200
