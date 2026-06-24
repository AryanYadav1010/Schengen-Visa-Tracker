"""Telegram bot integration: send alerts, and poll for /start <code> linking messages.

One operator-level bot (TELEGRAM_BOT_TOKEN in .env), same pattern as SMTP — every
user links their own chat by messaging that bot, identified by a one-time code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.models import User

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT_SECONDS = 30

_poll_task: asyncio.Task | None = None
_START_RE = re.compile(r"^/start\s+(\S+)")


def generate_link_code() -> str:
    return secrets.token_hex(4)


def match_start_command(text: str) -> str | None:
    """Return the code in a '/start <code>' message, or None if it doesn't match."""
    if not text:
        return None
    match = _START_RE.match(text.strip())
    return match.group(1) if match else None


async def send_telegram_message(chat_id: str, text: str) -> None:
    """Send a message via the bot. Raises on HTTP/network failure — caller decides how to handle."""
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

    url = _API_BASE.format(token=settings.TELEGRAM_BOT_TOKEN) + "/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        resp.raise_for_status()


async def _process_update(update: dict) -> None:
    message = update.get("message") or {}
    text = message.get("text", "")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    code = match_start_command(text)
    if code is None or chat_id is None:
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_link_code == code))
        user = result.scalar_one_or_none()
        if user is None:
            logger.info("Telegram /start with unknown/expired code, ignoring")
            return

        user.telegram_chat_id = str(chat_id)
        user.telegram_link_code = None
        await session.commit()
        logger.info("Linked Telegram chat %s to user %d", chat_id, user.id)

    try:
        await send_telegram_message(str(chat_id), "✅ Linked! You'll get visa appointment alerts here.")
    except Exception:
        logger.exception("Failed to send Telegram link confirmation")


async def poll_loop() -> None:
    """Long-poll getUpdates and process /start <code> messages. Runs until cancelled."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram polling disabled")
        return

    url = _API_BASE.format(token=settings.TELEGRAM_BOT_TOKEN) + "/getUpdates"
    offset = 0
    logger.info("Telegram poller started")

    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"offset": offset, "timeout": _POLL_TIMEOUT_SECONDS},
                    timeout=_POLL_TIMEOUT_SECONDS + 10,
                )
                resp.raise_for_status()
                data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                await _process_update(update)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram poll iteration failed, retrying shortly")
            await asyncio.sleep(5)


def start_polling() -> None:
    global _poll_task
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.info("Skipping Telegram poller — TELEGRAM_BOT_TOKEN not configured")
        return
    _poll_task = asyncio.create_task(poll_loop())


def stop_polling() -> None:
    global _poll_task
    if _poll_task is not None:
        _poll_task.cancel()
        _poll_task = None
        logger.info("Telegram poller stopped")
