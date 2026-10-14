"""Thin Telegram Bot API client (httpx, no framework).

One class wrapping the handful of methods the bot needs. Kept synchronous —
the poller is a simple long-polling loop in its own process. Every method
returns the parsed ``result`` on success or None on failure (errors are
logged, never raised into the polling loop).
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("networking.tgbot")

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE = "https://api.telegram.org/file/bot{token}/{path}"


class BotClient:
    def __init__(self, token: str | None = None, timeout: float = 65.0):
        self.token = token or settings.telegram_bot_token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def call(self, method: str, **payload):
        if not self.token:
            return None
        # Drop None values so optional params don't reach the API.
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    _API.format(token=self.token, method=method), json=payload
                )
                data = resp.json()
        except Exception as exc:  # pragma: no cover - network
            logger.warning("Telegram %s failed: %s", method, exc)
            return None
        if not data.get("ok"):
            logger.warning("Telegram %s error: %s", method, data.get("description"))
            return None
        return data.get("result")

    # ── Methods ──────────────────────────────────────────────────────────

    def get_updates(self, offset: int | None, timeout: int = 30):
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "business_connection",
                "business_message",
            ],
        )

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        business_connection_id: str | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
        disable_preview: bool = True,
        silent: bool = False,
    ):
        """``silent`` — без звуку (години тиші): повідомлення лягає, але не будить."""
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            link_preview_options={"is_disabled": disable_preview},
            disable_notification=True if silent else None,
        )

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
    ):
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    def delete_message(self, chat_id: int | str, message_id: int):
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def get_chat(self, chat_id: int | str):
        """Up-to-date profile of a user/chat: username, bio, birthdate,
        photo. Works for chats the bot can see; returns None otherwise."""
        return self.call("getChat", chat_id=chat_id)

    def set_my_commands(self, commands: list[dict]):
        """Replace the bot's slash-command menu (the '/' button list)."""
        return self.call("setMyCommands", commands=commands)

    def answer_callback(self, callback_query_id: str, text: str | None = None):
        return self.call(
            "answerCallbackQuery", callback_query_id=callback_query_id, text=text
        )

    def download_file(self, file_id: str) -> bytes | None:
        """Resolve a file_id and download its bytes (voice notes are small)."""
        info = self.call("getFile", file_id=file_id)
        if not info or not info.get("file_path"):
            return None
        url = _FILE.format(token=self.token, path=info["file_path"])
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:  # pragma: no cover - network
            logger.warning("Telegram file download failed: %s", exc)
            return None
