"""ALiSiO CRM: той самий Telegram-бот служить двом системам.

Ми тримаємо getUpdates і відповідаємо на вхідні, як і досі. ALiSiO веде
продажі готелям і сама відповідає від імені власника, але лише в чатах людей,
заведених у ній лідами. Хто лід, знає тільки ALiSiO, тому кожне
``business_message`` спершу пересилається їй, і лише її відповідь вирішує, чи
ми взагалі торкаємось цього чату.

Контракт: POST сирого JSON оновлення з секретом у заголовку; у відповіді
``{"ok": true, "received": 1, "handled": 1}``. ``handled > 0`` — чат її,
мовчимо. Будь-яка помилка (401 — не той секрет, 503 — на її боці ще немає
токена бота, таймаут) = ``handled 0``: працюємо як раніше, не повторюємо,
ALiSiO дедуплікує за id повідомлення.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("networking.alisio")

TIMEOUT_SECONDS = 5.0
#: Пересилаємо лише цей вид оновлень; решта (message, edited_business_message,
#: deleted_business_messages, business_connection) ALiSiO не потрібна.
FORWARDED_KINDS = ("business_message",)


def configured() -> bool:
    return bool(settings.alisio_webhook_secret and settings.alisio_webhook_url)


def should_forward(update: dict) -> bool:
    return any(kind in update for kind in FORWARDED_KINDS)


def forward_update(update: dict) -> int:
    """Пересилає оновлення в ALiSiO і повертає ``handled`` з її відповіді.

    0 — чат не її (або ALiSiO недоступна): діємо як раніше.
    """
    if not configured() or not should_forward(update):
        return 0
    update_id = update.get("update_id")
    chat_id = ((update.get("business_message") or {}).get("chat") or {}).get("id")
    try:
        resp = httpx.post(
            settings.alisio_webhook_url,
            json=update,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": settings.alisio_webhook_secret,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        logger.warning("alisio: timeout after %ss, update=%s chat=%s -> handled=0", TIMEOUT_SECONDS, update_id, chat_id)
        return 0
    except httpx.HTTPError as exc:
        logger.warning("alisio: request failed (%s), update=%s chat=%s -> handled=0", exc, update_id, chat_id)
        return 0

    if resp.status_code == 401:
        logger.error("alisio: 401 — неправильний ALISIO_WEBHOOK_SECRET; update=%s -> handled=0", update_id)
        return 0
    if resp.status_code == 503:
        logger.warning("alisio: 503 — на боці ALiSiO ще не покладено токен бота; update=%s -> handled=0", update_id)
        return 0
    if resp.status_code != 200:
        logger.warning("alisio: HTTP %s, update=%s -> handled=0", resp.status_code, update_id)
        return 0
    try:
        data = resp.json()
    except ValueError:
        logger.warning("alisio: відповідь не JSON, update=%s -> handled=0", update_id)
        return 0
    try:
        handled = int(data.get("handled") or 0)
    except (TypeError, ValueError, AttributeError):
        handled = 0
    logger.info(
        "alisio: update=%s chat=%s received=%s handled=%s",
        update_id, chat_id, (data or {}).get("received") if isinstance(data, dict) else None, handled,
    )
    return handled
