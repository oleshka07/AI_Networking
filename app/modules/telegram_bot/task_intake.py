"""Telegram → задачі: пропозиції з переписки і «попроси Марію…».

Що робить:

- **Пропозиція задачі.** У вхідному чи своєму повідомленні видно обіцянку,
  прохання або домовленість про дзвінок — власнику одне повідомлення з
  кнопками «➕ Задача», «📅 У календар», «✖». Не частіше разу на годину на
  чат; у чатах, які веде ALiSiO, — нічого. Режим ``task_settings.tg_suggest_mode``:
  ``suggest`` (типово) / ``auto`` (задача одразу, без повідомлення) / ``off``.
  Пропозиція йде в денну стелю; у години тиші (або коли стеля вичерпана) вона
  чекає ранкового плану, блок «За ніч» (``tasks/quiet.py``).
- **Задача з повідомлення.** ``source='telegram'``, ``external_id='tgb:<chat>:<msg>'``,
  строк із тексту, у нотатках до 300 символів цитати. Повторне натискання
  не створює другої: дедуп за (source, external_id).
- **«Попроси Марію надіслати рахунок до пʼятниці».** Чернетка повідомлення
  Марії через звичайний потік чернеток (відправка — лише кнопкою
  «✅ Надіслати») і задача-очікування «Перевірити: …» зі строком. Кілька
  збігів на імʼя — перепитуємо кнопками, не вгадуємо.

Кнопки несуть лише chat/message id: текст і контакт беруться з історії
взаємодій, куди повідомлення вже записане, тож окремого сховища немає.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.core.database import SessionLocal
from app.modules.contacts.models import Contact

logger = logging.getLogger("networking.tgbot.tasks")

#: Пропозицій на чат — не частіше ніж раз на стільки секунд.
SUGGEST_EVERY_SECONDS = 3600
_last_suggested: dict[int, float] = {}
#: «Попроси …» з кількома збігами чекає вибору кнопкою: token → (що, строк).
_pending_delegate: dict[str, tuple[str, date | None]] = {}

_KIND_LABEL = {"duty": "я винен", "expectation": "чекаю від інших", "task": "задача"}
_DELEGATE_RE = re.compile(
    r"^\s*(?:попроси(?:ти)?|попросіть|попросите|ask|požádej|poproś)\s+"
    r"(?P<name>[^\s,]+)\s*,?\s+(?:щоб\s+|чтобы\s+|to\s+|ať\s+)?(?P<what>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _today() -> date:
    return datetime.now().date()


def _mode(db) -> str:
    from app.modules.tasks.external import get_settings

    return (get_settings(db).tg_suggest_mode or "suggest").strip().lower()


def _rate_ok(chat_id: int) -> bool:
    last = _last_suggested.get(chat_id)
    return last is None or time.time() - last >= SUGGEST_EVERY_SECONDS


def _remember(chat_id: int) -> None:
    _last_suggested[chat_id] = time.time()
    if len(_last_suggested) > 500:
        for key in sorted(_last_suggested, key=_last_suggested.get)[:250]:
            _last_suggested.pop(key, None)


def _ext(chat_id, message_id) -> str:
    return f"tgb:{chat_id}:{message_id}"


def create_from_message(db, *, name: str | None, chat_id, message_id, text: str,
                        kind: str, commitment_type: str, outgoing: bool,
                        due: date | None):
    """Задача з повідомлення; повторно — та сама задача (дедуп за external_id)."""
    from app.modules.tasks import intake
    from app.modules.tasks import service as tasks_service
    from app.modules.tasks.models import Task, TaskKind

    ext = _ext(chat_id, message_id)
    existing = tasks_service.find_external(db, "telegram", ext)
    if existing is not None:
        return existing, False
    quote = " ".join(str(text or "").split())[:300]
    task = Task(
        uid=tasks_service.new_uid(),
        title=intake.title_for(kind, commitment_type, name or "контакт", text, outgoing=outgoing),
        kind=TaskKind(kind),
        source="telegram",
        external_id=ext,
        counterpart=(name[:120] if name else None),
        due_date=due,
        notes=f"«{quote}»",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task, True


def _suggest_keyboard(kind: str, chat_id, message_id, meeting: bool) -> dict:
    code = {"duty": "d", "expectation": "e", "task": "t"}[kind]
    row = [{"text": "➕ Задача", "callback_data": f"tg:add:{code}:{chat_id}:{message_id}"}]
    if meeting:
        row.append({"text": "📅 У календар", "callback_data": f"tg:cal:{chat_id}:{message_id}"})
    row.append({"text": "✖", "callback_data": "tg:x"})
    return {"inline_keyboard": [row]}


def detect(text: str) -> dict | None:
    from app.modules.tasks import intake

    return intake.detect_commitment(text, _today())


def mode(db) -> str:
    return _mode(db)


def task_kind(found: dict, *, outgoing: bool) -> str:
    from app.modules.tasks import intake

    return intake.kind_for(found["type"], outgoing=outgoing)


def task_button(found: dict, chat_id, message_id, *, outgoing: bool) -> dict:
    """Кнопка «➕ Задача» для картки, яка вже йде власнику (контакт на паузі)."""
    from app.modules.tasks import intake

    kind = intake.kind_for(found["type"], outgoing=outgoing)
    code = {"duty": "d", "expectation": "e", "task": "t"}[kind]
    return {"text": "➕ Задача", "callback_data": f"tg:add:{code}:{chat_id}:{message_id}"}


def maybe_suggest(client, admin: int | None, *, name: str | None, chat_id, message_id,
                  text: str, outgoing: bool, found: dict | None = None) -> bool:
    """Пропонує (або в режимі auto — створює) задачу з повідомлення.

    Повертає True, якщо щось зробили. Ніколи не кидає: розбір переписки не
    має ламати основний потік чернеток.
    """
    if not admin or not text or chat_id is None:
        return False
    try:
        from app.modules.tasks import intake, nudges, quiet

        found = found or detect(text)
        if found is None:
            return False
        kind = intake.kind_for(found["type"], outgoing=outgoing)
        with SessionLocal() as db:
            mode = _mode(db)
            if mode == "off":
                return False
            if mode == "auto":
                create_from_message(
                    db, name=name, chat_id=chat_id, message_id=message_id, text=text,
                    kind=kind, commitment_type=found["type"], outgoing=outgoing,
                    due=found.get("due"),
                )
                return True
            if not _rate_ok(chat_id):
                return False
            if quiet.already_suggested(db, quiet.suggestion_ref(chat_id, message_id)):
                return False
            # Уночі — до ранкового плану («За ніч»); стеля вичерпана — теж туди:
            # обіцянка не має загубитись через те, що день був гучний.
            if not nudges.allow_proactive(db):
                quiet.hold_suggestion(
                    db, chat_id=chat_id, message_id=message_id, name=name, kind=kind,
                    commitment_type=found["type"], outgoing=outgoing, due=found.get("due"),
                    meeting=found["type"] == "meeting", text=text,
                )
                _remember(chat_id)
                return True
        due = found.get("due")
        bits = [f"👤 {_esc(name or 'контакт')}", _KIND_LABEL.get(kind, kind)]
        if due:
            bits.append(f"строк {due:%d.%m}")
        who = "ти написав" if outgoing else "тобі написали"
        quote = " ".join(text.split())[:300]
        msg = (
            f"📝 <b>Схоже на задачу</b> ({who})\n" + " · ".join(bits)
            + f"\n«{_esc(quote)}»"
        )
        sent = client.send_message(
            admin, msg,
            reply_markup=_suggest_keyboard(kind, chat_id, message_id, found["type"] == "meeting"),
        )
        if sent is not None:
            _remember(chat_id)
            with SessionLocal() as db:
                quiet.mark_suggested(db, chat_id, message_id)
        return True
    except Exception as exc:  # pragma: no cover - не валимо потік повідомлень
        logger.warning("task suggestion failed: %s", exc)
        return False


def _message_text(db, chat_id, message_id) -> tuple[str | None, Contact | None, bool]:
    """Текст повідомлення, контакт і чи це моє — з історії взаємодій."""
    from app.modules.interactions.models import Direction, Interaction

    row = db.scalars(
        select(Interaction).where(Interaction.external_id == _ext(chat_id, message_id))
    ).first()
    if row is None:
        return None, None, False
    contact = db.get(Contact, row.contact_id)
    return row.summary, contact, row.direction == Direction.outbound


def handle_callback(client, cb_id: str, data: str, admin: int | None, msg: dict) -> None:
    """``tg:add:<d|e|t>:<chat>:<msg>`` · ``tg:cal:<chat>:<msg>`` · ``tg:x``."""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "x":
        client.answer_callback(cb_id, "Ок")
        if msg:
            client.delete_message(msg["chat"]["id"], msg["message_id"])
        return
    try:
        if action == "add":
            code, chat_id, message_id = parts[2], int(parts[3]), int(parts[4])
        else:
            chat_id, message_id = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        client.answer_callback(cb_id)
        return

    with SessionLocal() as db:
        text, contact, outgoing = _message_text(db, chat_id, message_id)
        if not text:
            client.answer_callback(cb_id, "Повідомлення вже не знайти")
            return
        if action == "cal":
            client.answer_callback(cb_id, "Додаю в календар…")
            if admin:
                from .handlers import _handle_calendar_event

                _handle_calendar_event(client, admin, {}, text)
            return
        if action != "add":
            client.answer_callback(cb_id)
            return
        kind = {"d": "duty", "e": "expectation", "t": "task"}.get(code, "task")
        found = detect(text) or {"type": "meeting" if kind == "task" else "promise", "due": None}
        task, created = create_from_message(
            db, name=(contact.full_name if contact else None), chat_id=chat_id,
            message_id=message_id, text=text,
            kind=kind, commitment_type=found["type"], outgoing=outgoing, due=found.get("due"),
        )
        client.answer_callback(cb_id, "➕ Задачу створено" if created else "Задача вже є")
        if msg:
            when = f" · до {task.due_date:%d.%m}" if task.due_date else ""
            client.edit_message_text(
                msg["chat"]["id"], msg["message_id"],
                f"➕ <b>Задача</b> #{task.uid}{when}\n{_esc(task.title)}",
            )


# ── «Попроси Марію …» ───────────────────────────────────────────────────────


def parse_delegate(text: str) -> tuple[str, str] | None:
    """(імʼя як написано, що попросити) або ``None``, якщо це не прохання."""
    m = _DELEGATE_RE.match(text or "")
    if not m:
        return None
    what = m.group("what").strip(" .,;")
    return (m.group("name"), what) if len(what) >= 3 else None


def candidates(db, name: str) -> list[Contact]:
    """Контакти за імʼям у будь-якому відмінку: «Марію» → «Марія».

    Основа — без останньої літери (мінімум три): відмінок міняє закінчення,
    а не корінь. Збіг — початок імені чи прізвища або повне імʼя.
    """
    needle = (name or "").strip().casefold()
    if len(needle) < 2:
        return []
    stem = needle[:-1] if len(needle) >= 4 else needle
    out = []
    for c in db.scalars(select(Contact)).unique():
        tokens = [t.casefold() for t in (c.first_name, c.last_name) if t]
        if any(t.startswith(stem) for t in tokens) or needle == c.full_name.casefold():
            out.append(c)
    return out


def _template(contact: Contact, what: str) -> str:
    return f"{contact.first_name}, прохання: {what.rstrip('.')}."


def delegate(client, admin: int, db, contact: Contact, what: str, due: date | None) -> None:
    """Чернетка прохання людині + задача «Перевірити: …» зі строком."""
    from app.modules.insights import ai
    from app.modules.tasks import service as tasks_service
    from app.modules.tasks.models import Task, TaskKind

    from . import service
    from .handlers import send_draft_card
    from .models import DraftKind, DraftStatus, TelegramDraft

    task = Task(
        uid=tasks_service.new_uid(),
        title=f"Перевірити: {contact.full_name} — {what}"[:300],
        kind=TaskKind.expectation,
        source="bot",
        counterpart=contact.full_name[:120],
        due_date=due,
        notes=f"Попросив: {what}"[:500],
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    when = f"у день строку ({due:%d.%m})" if due else "без строку — додай його в задачі"
    head = f"⏳ Задача #{task.uid}: {_esc(task.title)}\nНагадаю перевірити {when}."

    text = ai.draft_request(contact, what) or _template(contact, what)
    connection = service.get_enabled_connection(db)
    if not contact.telegram_chat_id or connection is None:
        reason = ("у контакту немає Telegram" if not contact.telegram_chat_id
                  else "Telegram Business не підключений")
        client.send_message(admin, f"{head}\n\n✍️ Чернетку не покласти ({reason}). Напиши сам:\n{_esc(text)}")
        return
    draft = TelegramDraft(
        contact_id=contact.id,
        chat_id=contact.telegram_chat_id,
        business_connection_id=connection.connection_id,
        kind=DraftKind.outreach,
        incoming_text=f"Прохання: {what}"[:500],
        draft_text=text,
        status=DraftStatus.pending,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    def _send(card, kb):
        sent = client.send_message(admin, card, reply_markup=kb)
        return (sent or {}).get("message_id") if sent else None

    # Чат без вхідного за 24 год — «Відкрити чат» і «Скопіювати» замість «Надіслати».
    message_id = send_draft_card(
        _send, db, draft,
        f"{head}\n\n✍️ <b>{_esc(contact.full_name)}</b> — чернетка прохання\n\n{_esc(text)}",
    )
    if message_id:
        service.set_admin_message(db, draft, message_id)


def handle_delegate(client, admin: int, text: str) -> bool:
    """«Попроси Марію …». True — оброблено (навіть якщо контакт не знайшовся)."""
    from app.modules.tasks import intake

    parsed = parse_delegate(text)
    if parsed is None:
        return False
    name, what = parsed
    due = intake.due_from_text(what, _today())
    with SessionLocal() as db:
        found = candidates(db, name)
        if not found:
            client.send_message(admin, f"Не знайшов у мережі «{_esc(name)}». Додай контакт або напиши імʼя інакше.")
            return True
        if len(found) > 1:
            token = secrets.token_hex(4)
            _pending_delegate[token] = (what, due)
            if len(_pending_delegate) > 100:
                for key in list(_pending_delegate)[:50]:
                    _pending_delegate.pop(key, None)
            rows = [[{"text": c.full_name[:40], "callback_data": f"dg:{token}:{c.id}"}]
                    for c in found[:6]]
            client.send_message(admin, f"Кого саме попросити: {_esc(what)}?",
                                reply_markup={"inline_keyboard": rows})
            return True
        delegate(client, admin, db, found[0], what, due)
    return True


def handle_delegate_callback(client, cb_id: str, data: str, admin: int | None, msg: dict) -> None:
    """``dg:<token>:<contact_id>`` — вибір людини, коли імʼя було неоднозначне."""
    try:
        _, token, contact_id = data.split(":")
        contact_id = int(contact_id)
    except ValueError:
        client.answer_callback(cb_id)
        return
    pending = _pending_delegate.pop(token, None)
    if pending is None or not admin:
        client.answer_callback(cb_id, "Уже неактуально")
        return
    what, due = pending
    with SessionLocal() as db:
        contact = db.get(Contact, contact_id)
        if contact is None:
            client.answer_callback(cb_id, "Контакту вже немає")
            return
        client.answer_callback(cb_id, contact.full_name)
        if msg:
            client.delete_message(msg["chat"]["id"], msg["message_id"])
        delegate(client, admin, db, contact, what, due)
