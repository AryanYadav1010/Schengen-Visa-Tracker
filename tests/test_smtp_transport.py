"""F-03: the SMTP transport must be bounded in time and must not block the event loop."""

from __future__ import annotations

import asyncio
import smtplib
import socket
import time

import pytest

from app import notifier
from app.config import settings


class _RecordingSMTP:
    """Stands in for smtplib.SMTP; optionally blocks (like a slow server) inside __enter__."""

    instances: list["_RecordingSMTP"] = []
    block_seconds = 0.0

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        _RecordingSMTP.instances.append(self)

    def __enter__(self):
        time.sleep(self.block_seconds)  # deliberately blocking, like a real socket call
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self): pass
    def starttls(self): pass
    def login(self, user, password): pass
    def sendmail(self, from_addr, to_addrs, msg): pass


@pytest.fixture
def fake_smtp(monkeypatch):
    _RecordingSMTP.instances = []
    _RecordingSMTP.block_seconds = 0.0
    monkeypatch.setattr(notifier.smtplib, "SMTP", _RecordingSMTP)
    return _RecordingSMTP


def test_smtp_connection_is_opened_with_the_configured_timeout(fake_smtp, caplog):
    with caplog.at_level("INFO", logger="app.notifier"):
        notifier._send_smtp("to@example.test", "s", "plain", "<p>html</p>")
    assert "to@example.test" not in caplog.text   # recipient addresses stay out of the logs
    assert fake_smtp.instances[0].timeout == settings.SMTP_TIMEOUT_SECONDS
    assert settings.SMTP_TIMEOUT_SECONDS > 0


async def test_smtp_send_does_not_block_the_event_loop(fake_smtp):
    fake_smtp.block_seconds = 0.3
    ticks, done = 0, False

    async def heartbeat():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    async def send():
        nonlocal done
        await notifier._send_email("to@example.test", "s", "plain", "<p>html</p>")
        done = True

    await asyncio.gather(send(), heartbeat())
    # If the 0.3 s blocking call ran on the loop, the heartbeat could not tick while it ran.
    assert ticks >= 5


def test_real_smtplib_gives_up_on_a_silent_server(monkeypatch):
    """No fakes: a local TCP server that accepts but never sends the SMTP greeting."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SMTP_PORT", server.getsockname()[1])
    monkeypatch.setattr(settings, "SMTP_TIMEOUT_SECONDS", 0.3)
    try:
        started = time.monotonic()
        # smtplib wraps the socket TimeoutError raised while reading the greeting
        # into SMTPServerDisconnected("... timed out"); either way it fails fast.
        with pytest.raises((TimeoutError, smtplib.SMTPServerDisconnected), match="timed out"):
            notifier._send_smtp("to@example.test", "s", "plain", "<p>html</p>")
        assert time.monotonic() - started < 3
    finally:
        server.close()
