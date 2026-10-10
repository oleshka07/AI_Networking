"""Holos (holos.rozum.one): той самий бот ставить сторіс у профіль і дає ідеї для постів.

Telegram Business дає підключити лише одного чат-бота, і це наш бот. Holos його не забирає:
``getUpdates`` лишається тут, а ми самі пересилаємо Holos те, що йому потрібно (docs/HOLOS.md):

- ``business_connection`` — щоб Holos знав id підключення. Сторіс у профіль Holos ставить сам нашим
  токеном (``postStory``); надсилати з двох систем одним токеном Telegram дозволяє, обмеження лише на
  одне місце для оновлень, і воно в нас.
- текст ВЛАСНИХ повідомлень власника в бізнес-чатах — для ідей постів. Повідомлення співрозмовників
  не шлемо зовсім (а Holos їх і відкидає), імен і юзернеймів співрозмовника теж: лише id чату, з якого
  Holos робить відбиток для дедуплікації.

Пересилаємо у фоновому потоці: відповідь людям не чекає Holos. Таймаут 5 с, без повторів: Holos
дедуплікує за id повідомлення, а підключення ми ще раз шлемо на кожному старті бота.
Порожні ``HOLOS_RELAY_URL`` / ``HOLOS_RELAY_SECRET`` — нічого не пересилається.

Контракт: POST JSON у формі Telegram Update з секретом у заголовку ``X-Telegram-Bot-Api-Secret-Token``;
у відповідь ``{"ok": true, "received": 1, "handled": 0, "did": "..."}`` (``handled`` завжди 0: чатів
власника Holos не веде).
"""

from __future__ import annotations

import logging
import threading

import httpx

from app.core.config import settings

logger = logging.getLogger("networking.holos")

TIMEOUT_SECONDS = 5.0


def configured() -> bool:
    return bool(settings.holos_relay_url and settings.holos_relay_secret)


def _post(update: dict) -> str:
    """Надіслати оновлення в Holos; повертає ``did`` з відповіді або причину збою (для логу й тестів)."""
    if not configured():
        return "off"
    kind = next((k for k in ("business_connection", "business_message") if k in update), "?")
    try:
        resp = httpx.post(
            settings.holos_relay_url,
            json=update,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": settings.holos_relay_secret,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        logger.warning("holos: timeout after %ss (%s)", TIMEOUT_SECONDS, kind)
        return "timeout"
    except httpx.HTTPError as exc:
        logger.warning("holos: request failed (%s), %s", exc, kind)
        return "error"
    if resp.status_code == 401:
        logger.error("holos: 401 — неправильний HOLOS_RELAY_SECRET (візьми його в Holos: Канали → Telegram → «Свій бот»)")
        return "401"
    if resp.status_code != 200:
        logger.warning("holos: HTTP %s (%s)", resp.status_code, kind)
        return f"http {resp.status_code}"
    try:
        did = str((resp.json() or {}).get("did") or "ok")
    except ValueError:
        did = "ok"
    logger.info("holos: %s -> %s", kind, did)
    return did


def _spawn(fn, *args) -> None:
    """Фоновий потік: бот відповідає людям, не чекаючи Holos. Тести підміняють на синхронний виклик."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def forward_connection(bc: dict) -> None:
    """Підключення до Telegram Business (нове, змінені права, вимкнене) — як прийшло від Telegram."""
    if not configured() or not (bc or {}).get("id"):
        return
    _spawn(_post, {"business_connection": bc})


def own_message_update(msg: dict, text: str) -> dict:
    """Мінімум для Holos: чиє підключення, id повідомлення, хто написав (власник), id чату й текст.
    Без імен, юзернеймів і фото співрозмовника."""
    return {
        "business_message": {
            "business_connection_id": msg.get("business_connection_id"),
            "message_id": msg.get("message_id"),
            "date": msg.get("date"),
            "from": {"id": (msg.get("from") or {}).get("id")},
            "chat": {"id": (msg.get("chat") or {}).get("id")},
            "text": text,
        }
    }


def forward_own_message(msg: dict, text: str) -> None:
    """Повідомлення, яке написав САМ власник. Порожнє чи без підключення — не шлемо."""
    if not configured() or not text or not msg.get("business_connection_id"):
        return
    _spawn(_post, own_message_update(msg, text))


def sync_connections(client) -> int:
    """На старті бота: перешле в Holos збережені підключення Business (свіжий стан — із Telegram).
    Так Holos дізнається про підключення, зроблене ще до того, як увімкнули пересилання."""
    if not configured():
        return 0
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.modules.telegram_bot.models import BusinessConnection

    with SessionLocal() as db:
        ids = [c.connection_id for c in db.scalars(select(BusinessConnection)) if c.connection_id]
    sent = 0
    for conn_id in ids:
        bc = client.call("getBusinessConnection", business_connection_id=conn_id)
        if bc and bc.get("id"):
            _post({"business_connection": bc})
            sent += 1
    logger.info("holos: підключень Business переслано на старті: %s", sent)
    return sent
