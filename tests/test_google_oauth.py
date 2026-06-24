import base64

import pytest

from app import google_oauth
from app.config import settings


@pytest.fixture(autouse=True)
def oauth_settings():
    originals = {
        "GOOGLE_OAUTH_CLIENT_ID": settings.GOOGLE_OAUTH_CLIENT_ID,
        "GOOGLE_OAUTH_CLIENT_SECRET": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "GOOGLE_OAUTH_REDIRECT_URI": settings.GOOGLE_OAUTH_REDIRECT_URI,
    }
    settings.GOOGLE_OAUTH_CLIENT_ID = "test-client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "test-client-secret"
    settings.GOOGLE_OAUTH_REDIRECT_URI = "http://localhost:8000/oauth/google/callback"
    yield
    for key, value in originals.items():
        setattr(settings, key, value)


def test_build_auth_url_contains_client_id_and_redirect_uri():
    url = google_oauth.build_auth_url("some-state")
    assert "test-client-id" in url
    assert "localhost%3A8000" in url or "localhost:8000" in url
    assert "state=some-state" in url


def test_build_auth_url_requires_client_credentials():
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_ID"):
        google_oauth.build_auth_url("state")


class FakeCredentials:
    def __init__(self, token="fake-access-token"):
        self.token = token
        self.refresh_token = "fake-refresh-token"

    def refresh(self, request):
        self.token = "refreshed-access-token"


def test_send_via_gmail_builds_and_sends_message(monkeypatch):
    captured = {}

    monkeypatch.setattr(google_oauth.Credentials, "__init__", lambda self, **kwargs: None)
    monkeypatch.setattr(google_oauth.Credentials, "refresh", lambda self, request: None)

    class FakeMessages:
        def send(self, userId, body):
            captured["userId"] = userId
            captured["raw"] = body["raw"]

            class _Exec:
                def execute(self_inner):
                    return {"id": "msg123"}

            return _Exec()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(google_oauth, "build", lambda *args, **kwargs: FakeService())

    import asyncio

    asyncio.run(google_oauth.send_via_gmail("refresh-tok", "to@example.com", "Subject", "plain body", "<p>html</p>"))

    assert captured["userId"] == "me"
    decoded = base64.urlsafe_b64decode(captured["raw"]).decode("utf-8")
    assert "Subject" in decoded
    assert "to@example.com" in decoded
