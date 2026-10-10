"""Long-polling loop for the Telegram bot (own process, not the web app).

Gated by TELEGRAM_POLLING_ENABLED so a deploy is always safe: with the flag
off the process idles (never a second poller fighting Chater for the token —
Telegram allows only one getUpdates consumer per token, otherwise 409)."""

from __future__ import annotations

import logging
import time

from app.core.config import settings

from .client import BotClient
from .handlers import dispatch

logger = logging.getLogger("networking.tgbot")


def run() -> None:
    if not settings.telegram_polling_enabled:
        logger.warning(
            "TELEGRAM_POLLING_ENABLED is false — bot idle. Enable it only "
            "after the Chater poller is stopped (one getUpdates per token)."
        )
        while True:
            time.sleep(3600)

    client = BotClient()
    if not client.configured:
        logger.warning("TELEGRAM_BOT_TOKEN not set; bot idle.")
        while True:
            time.sleep(3600)

    me = client.call("getMe")
    logger.info(
        "Telegram bot polling started as @%s (business updates on).",
        (me or {}).get("username", "?"),
    )
    # Replace whatever command menu the old Chater bot left on this token.
    client.set_my_commands(
        [
            {"command": "queue", "description": "Кому написати сьогодні"},
            {"command": "today", "description": "Дайджест дня"},
            {"command": "upcoming", "description": "Що попереду"},
            {"command": "birthdays", "description": "Найближчі дні народження"},
            {"command": "events", "description": "Що нового в людей"},
            {"command": "reconnect", "description": "Відновити звʼязок"},
            {"command": "reflect", "description": "Подумати про стосунки"},
            {"command": "reminders", "description": "Активні нагадування"},
            {"command": "due", "description": "Прострочені контакти"},
            {"command": "goals", "description": "Трекер цілей"},
            {"command": "goal", "description": "Питання коуча по цілі"},
            {"command": "focus", "description": "Топ-3 цілі без руху"},
            {"command": "week", "description": "Підсумок тижня"},
            {"command": "inbox", "description": "Пошта, що чекає"},
            {"command": "embed", "description": "Індексувати мережу для пошуку"},
            {"command": "find", "description": "Пошук контакту за іменем"},
            {"command": "timeline", "description": "Історія стосунку"},
            {"command": "similar", "description": "Схожі та повʼязані люди"},
            {"command": "pause", "description": "Відповідаю сам (без чернеток)"},
            {"command": "resume", "description": "Повернути авто-відповіді"},
            {"command": "paused", "description": "Хто на паузі"},
            {"command": "time", "description": "Трекінг часу за тиждень (ПК)"},
            {"command": "ideas", "description": "Мої ідеї"},
            {"command": "later", "description": "Посилання на потім"},
            {"command": "stalled", "description": "Проєкти без наступної дії"},
            {"command": "contexts", "description": "Контексти задач"},
            {"command": "review", "description": "Тижневий огляд"},
            {"command": "activity", "description": "Що робив бот + стан"},
            {"command": "enrich", "description": "Підтягнути дані з Telegram"},
            {"command": "help", "description": "Довідка"},
        ]
    )
    # Holos: підключення Business, зроблене до ввімкнення пересилання, - одразу після старту (у фоні).
    from app.modules.integrations import holos

    holos._spawn(holos.sync_connections, client)
    offset: int | None = None
    while True:
        try:
            updates = client.get_updates(offset)
            if updates is None:
                time.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                dispatch(client, update)
        except Exception as exc:  # pragma: no cover - resilience
            logger.warning("Polling loop error: %s", exc)
            time.sleep(5)
