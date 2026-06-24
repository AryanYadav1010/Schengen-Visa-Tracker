import httpx
import pytest

from app import telegram
from app.config import settings


def test_match_start_command_extracts_code():
    assert telegram.match_start_command("/start abc123") == "abc123"


def test_match_start_command_strips_surrounding_whitespace():
    assert telegram.match_start_command("  /start abc123  ") == "abc123"


def test_match_start_command_returns_none_for_bare_start():
    assert telegram.match_start_command("/start") is None


def test_match_start_command_returns_none_for_unrelated_text():
    assert telegram.match_start_command("hello there") is None


def test_match_start_command_returns_none_for_empty_text():
    assert telegram.match_start_command("") is None


def test_generate_link_code_produces_distinct_codes():
    codes = {telegram.generate_link_code() for _ in range(20)}
    assert len(codes) == 20


@pytest.fixture
def bot_token():
    original = settings.TELEGRAM_BOT_TOKEN
    settings.TELEGRAM_BOT_TOKEN = "fake-bot-token"
    yield
    settings.TELEGRAM_BOT_TOKEN = original


async def test_send_telegram_message_posts_to_api(bot_token, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda: FakeAsyncClient())

    await telegram.send_telegram_message("12345", "hello")

    assert "fake-bot-token" in captured["url"]
    assert captured["json"] == {"chat_id": "12345", "text": "hello"}


async def test_send_telegram_message_requires_bot_token():
    settings.TELEGRAM_BOT_TOKEN = ""
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        await telegram.send_telegram_message("12345", "hello")


async def test_send_telegram_message_propagates_http_errors(bot_token, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("bad request", request=None, response=None)

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda: FakeAsyncClient())

    with pytest.raises(httpx.HTTPStatusError):
        await telegram.send_telegram_message("12345", "hello")
