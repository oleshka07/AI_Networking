"""Нічна черга: що прийшло в години тиші, виходить уранці.

Рішення власника 15.10 (TASK-02): уночі (``task_settings.quiet_from``–``quiet_to``)
власнику не пише ніхто, крім брифу перед ранньою зустріччю. Те, що не можна
загубити, чекає ранку в ``held_messages``:

- **«Від Claude»** — ``crm_send_owner_message`` без ``urgent``;
- **«За ніч»** — пропозиції задач із переписки в Telegram (до пʼяти рядків із
  кнопками, решта — «Ще…»). Блок рахується в денну стелю як одне повідомлення.

Обидва блоки йдуть першими в ранковому плані (``automation/daily.py``). Якщо
плану того дня немає (вимкнений або щоденний прогін не дійшов до Telegram),
черга виходить окремим повідомленням після кінця тиші — ``release_pending``
перевіряє це кожні пʼятнадцять хвилин.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal

from . import nudges
from .external import get_settings
from .models import HeldMessage, TaskSettings

logger = logging.getLogger("networking.quiet")

#: Скільки пропозицій показувати одним блоком («Ще…» — наступні стільки ж).
NIGHT_PAGE = 5
#: Довжина одного повідомлення Telegram з запасом під розмітку.
_TG_LIMIT = 3900

KIND_OWNER = "owner"
KIND_SUGGESTION = "suggestion"

_KIND_LABEL = {"duty": "я винен", "expectation": "чекаю від інших", "task": "задача"}


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utcnow() -> datetime:
    """Мітка видачі черги — щоб повернути саме її, якщо повідомлення не дійшло."""
    return _now()


def _local_now() -> datetime:
    return datetime.now()


# ── Коли черга вийде ────────────────────────────────────────────────────────


def plan_expected(cfg: TaskSettings) -> bool:
    """Чи буде ранковий план, у якому черга вийде першими блоками."""
    return bool(cfg.enabled and cfg.plan_enabled)


def _release_minutes(cfg: TaskSettings) -> int:
    end = nudges._minutes(cfg.quiet_to, 7 * 60 + 30)
    if plan_expected(cfg):
        return max(end, settings.daily_run_hour * 60)
    return end


def release_time(cfg: TaskSettings, local_now: datetime | None = None) -> datetime:
    """Найближчий час, коли черга вийде: ранковий план або кінець тиші.

    Повертає час із зоною — його бачить Claude у ``queued_until``.
    """
    local_now = local_now or _local_now()
    minutes = _release_minutes(cfg)
    at = datetime.combine(local_now.date(), datetime.min.time()) + timedelta(minutes=minutes)
    if at <= local_now:
        at += timedelta(days=1)
    return at.astimezone()


# ── Покласти в чергу ────────────────────────────────────────────────────────


def hold_owner_message(db: Session, text: str) -> HeldMessage:
    row = HeldMessage(kind=KIND_OWNER, payload=json.dumps({"text": text}, ensure_ascii=False))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def suggestion_ref(chat_id, message_id) -> str:
    return f"tgb:{chat_id}:{message_id}"


def already_suggested(db: Session, ref: str) -> bool:
    """Це повідомлення вже пропонували (чекає, показане чи закрите)?"""
    return db.scalars(
        select(HeldMessage.id).where(HeldMessage.kind == KIND_SUGGESTION, HeldMessage.ref == ref)
    ).first() is not None


def hold_suggestion(db: Session, *, chat_id, message_id, name: str | None, kind: str,
                    commitment_type: str, outgoing: bool, due: date | None,
                    meeting: bool, text: str) -> bool:
    """Пропозиція задачі чекає ранку. ``False`` — таку вже пропонували."""
    ref = suggestion_ref(chat_id, message_id)
    if already_suggested(db, ref):
        return False
    payload = {
        "chat_id": chat_id, "message_id": message_id, "name": name, "kind": kind,
        "type": commitment_type, "outgoing": outgoing,
        "due": due.isoformat() if due else None, "meeting": meeting,
        "text": " ".join(str(text or "").split())[:500],
    }
    db.add(HeldMessage(kind=KIND_SUGGESTION, ref=ref,
                       payload=json.dumps(payload, ensure_ascii=False)))
    db.commit()
    return True


def mark_suggested(db: Session, chat_id, message_id) -> None:
    """Денна пропозиція вже пішла — запис для дедупу, щоб уночі не повторити."""
    ref = suggestion_ref(chat_id, message_id)
    if already_suggested(db, ref):
        return
    now = _now()
    db.add(HeldMessage(kind=KIND_SUGGESTION, ref=ref, payload="{}",
                       released_at=now, shown_at=now))
    db.commit()


def _payload(row: HeldMessage) -> dict:
    try:
        return json.loads(row.payload or "{}")
    except ValueError:
        return {}


# ── «Написати власнику» від Claude ──────────────────────────────────────────


def send_owner_message(db: Session, text: str, *, urgent: bool = False,
                       local_now: datetime | None = None) -> dict:
    """``crm_send_owner_message``: надіслати зараз або покласти в чергу до ранку.

    Без ``urgent`` — у тишу в чергу, удень одразу і поза стелею. ``urgent``
    обходить тишу, але рахується в стелю; стеля вичерпана — теж у чергу.
    """
    from app.modules.automation import telegram

    local_now = local_now or _local_now()
    cfg = get_settings(db)
    reason = None
    if urgent:
        if nudges.allow_proactive(db, local_now, ignore_quiet=True):
            if not telegram.send_message(_esc(text)):
                raise RuntimeError("telegram")
            return {"sent": True, "urgent": True}
        reason = "daily_cap"
    elif nudges.in_quiet(cfg, local_now):
        reason = "quiet_hours"
    else:
        if not telegram.send_message(_esc(text)):
            raise RuntimeError("telegram")
        return {"sent": True}
    hold_owner_message(db, text)
    return {"sent": False, "queued_until": release_time(cfg, local_now).isoformat(),
            "reason": reason}


# ── Ранкові блоки ───────────────────────────────────────────────────────────


def _pending(db: Session, kind: str) -> list[HeldMessage]:
    return list(db.scalars(
        select(HeldMessage)
        .where(HeldMessage.kind == kind, HeldMessage.released_at.is_(None))
        .order_by(HeldMessage.id)
    ))


def _unshown(db: Session) -> list[HeldMessage]:
    """Пропозиції, що вже вийшли з черги, але ще не показані рядком («Ще…»)."""
    return list(db.scalars(
        select(HeldMessage)
        .where(HeldMessage.kind == KIND_SUGGESTION, HeldMessage.released_at.is_not(None),
               HeldMessage.shown_at.is_(None), HeldMessage.closed_at.is_(None))
        .order_by(HeldMessage.id)
    ))


def owner_block(rows: list[HeldMessage]) -> str | None:
    texts = [str(_payload(r).get("text") or "").strip() for r in rows]
    texts = [t for t in texts if t]
    if not texts:
        return None
    return "🤖 <b>Від Claude</b>\n" + "\n\n".join(_esc(t[:3800]) for t in texts)


def suggestion_line(n: int, p: dict) -> str:
    bits = [f"👤 {_esc(p.get('name') or 'контакт')}", _KIND_LABEL.get(p.get("kind"), "задача")]
    if p.get("due"):
        try:
            bits.append(f"строк {date.fromisoformat(p['due']):%d.%m}")
        except ValueError:
            pass
    who = "ти написав" if p.get("outgoing") else "тобі написали"
    quote = str(p.get("text") or "")[:160]
    return f"{n}. {' · '.join(bits)} ({who})\n«{_esc(quote)}»"


def suggestion_rows(items: list[tuple[int, HeldMessage]]) -> list[list[dict]]:
    """Ряд кнопок на пропозицію: ➕ n / 📅 n / ✖ n — ті самі, що вдень."""
    rows = []
    for n, row in items:
        p = _payload(row)
        buttons = [{"text": f"➕ Задача {n}", "callback_data": f"tn:add:{row.id}"}]
        if p.get("meeting"):
            buttons.append({"text": f"📅 {n}", "callback_data": f"tn:cal:{row.id}"})
        buttons.append({"text": f"✖ {n}", "callback_data": f"tn:x:{row.id}"})
        rows.append(buttons)
    return rows


def _more_row(left: int) -> list[dict]:
    return [{"text": f"Ще… ({left})", "callback_data": "tn:more"}]


def _page(db: Session, rows: list[HeldMessage], *, start: int = 1) -> tuple[list[str], list[list[dict]]]:
    """Перша сторінка пропозицій: рядки й кнопки; показані позначаються."""
    page = rows[:NIGHT_PAGE]
    now = _now()
    numbered = list(enumerate(page, start))
    lines = [suggestion_line(n, _payload(r)) for n, r in numbered]
    buttons = suggestion_rows(numbered)
    for r in page:
        r.shown_at = now
    left = len(rows) - len(page)
    if left > 0:
        buttons.append(_more_row(left))
    db.commit()
    return lines, buttons


def morning_blocks(db: Session, local_now: datetime | None = None,
                   now: datetime | None = None) -> tuple[str | None, str | None, list[list[dict]]]:
    """Забирає чергу: (блок «Від Claude», блок «За ніч», кнопки «За ніч»).

    Усе, що віддано, позначається часом ``now`` — вдруге не прийде; якщо
    Telegram повідомлення не прийняв, ``restore(db, now)`` повертає чергу.
    Блок «За ніч» рахується в денну стелю одним повідомленням.
    """
    now = now or _now()
    owners = _pending(db, KIND_OWNER)
    suggestions = _pending(db, KIND_SUGGESTION)
    for r in owners + suggestions:
        r.released_at = now
    db.commit()
    owner_text = owner_block(owners)
    night_text, rows = None, []
    if suggestions:
        lines, rows = _page(db, suggestions)
        n = len(suggestions)
        night_text = f"🌙 <b>За ніч: {n} {_plural(n)}</b>\n" + "\n".join(lines)
        nudges.count_proactive(db, local_now)
    return owner_text, night_text, rows


def restore(db: Session, stamp: datetime) -> None:
    """Повідомлення не дійшло — черга, видана о ``stamp``, чекає наступної спроби."""
    for row in db.scalars(select(HeldMessage).where(HeldMessage.released_at == stamp,
                                                     HeldMessage.closed_at.is_(None))):
        row.released_at = None
        row.shown_at = None
    db.commit()


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "пропозиція"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "пропозиції"
    return "пропозицій"


def pending_count(db: Session) -> int:
    return len(db.scalars(select(HeldMessage.id).where(HeldMessage.released_at.is_(None))).all())


def has_pending(db: Session) -> bool:
    return db.scalars(
        select(HeldMessage.id).where(HeldMessage.released_at.is_(None))
    ).first() is not None


def release_pending(local_now: datetime | None = None) -> bool:
    """Кожні 15 хв: черга, яку не забрав ранковий план, виходить сама.

    Мовчить у тишу. Якщо план того дня буде — чекає його (і ще годину
    запасу, на випадок збою прогону); якщо плану немає — виходить одразу
    після кінця тиші. Ніколи не кидає.
    """
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    from app.modules.automation import telegram

    local_now = local_now or _local_now()
    try:
        with SessionLocal() as db:
            cfg = get_settings(db)
            if nudges.in_quiet(cfg, local_now) or not has_pending(db):
                return False
            if plan_expected(cfg) and local_now.hour < settings.daily_run_hour + 1:
                return False
            stamp = _now()
            owner_text, night_text, rows = morning_blocks(db, local_now, now=stamp)
            if send_blocks(telegram, [night_text], rows, head=owner_text):
                return True
            restore(db, stamp)
            return False
    except Exception as exc:  # pragma: no cover - фонова задача не має падати
        logger.warning("quiet release failed: %s", exc)
        return False


def send_blocks(telegram, parts: list[str | None], rows: list[list[dict]], *,
                head: str | None = None) -> bool:
    """Одне повідомлення: ``head`` («Від Claude») першим, далі ``parts``.

    Задовге — ``head`` іде окремим повідомленням попереду, кнопки лишаються
    під своїм текстом.
    """
    parts = [p for p in parts if p]
    if not parts and not head:
        return False
    markup = {"inline_keyboard": rows} if rows else None
    body = "\n\n".join(([head] if head else []) + parts)
    if len(body) <= _TG_LIMIT or not head or not parts:
        return bool(telegram.send_message(body[:4096], reply_markup=markup))
    ok = telegram.send_message(head[:4096])
    return bool(telegram.send_message("\n\n".join(parts)[:4096], reply_markup=markup)) and bool(ok)


# ── Кнопки блоку «За ніч» ───────────────────────────────────────────────────


def _drop_buttons(markup: dict | None, held_id: int | None) -> dict:
    """Клавіатура без рядка цієї пропозиції (або без «Ще…», коли ``held_id`` None)."""
    rows = (markup or {}).get("inline_keyboard") or []
    out = []
    for row in rows:
        keep = []
        for b in row:
            data = b.get("callback_data") or ""
            if held_id is None and data == "tn:more":
                continue
            if held_id is not None and data.startswith("tn:") and data.endswith(f":{held_id}"):
                continue
            keep.append(b)
        if keep:
            out.append(keep)
    return {"inline_keyboard": out}


def handle_callback(client, cb_id: str, data: str, admin: int | None, msg: dict) -> None:
    """``tn:add:<id>`` · ``tn:cal:<id>`` · ``tn:x:<id>`` · ``tn:more``.

    Повідомлення ранкового плану не переписується — з нього зникає лише
    рядок кнопок цієї пропозиції.
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat = (msg or {}).get("chat", {}).get("id")
    message_id = (msg or {}).get("message_id")

    def drop(held_id):
        if chat and message_id:
            client.call("editMessageReplyMarkup", chat_id=chat, message_id=message_id,
                        reply_markup=_drop_buttons(msg.get("reply_markup"), held_id))

    with SessionLocal() as db:
        if action == "more":
            rows = _unshown(db)
            client.answer_callback(cb_id)
            drop(None)
            if rows and admin:
                # Нумерація продовжує ту саму ніч: 6, 7, … після перших пʼяти.
                shown = db.scalars(
                    select(HeldMessage.id).where(HeldMessage.kind == KIND_SUGGESTION,
                                                 HeldMessage.released_at == rows[0].released_at,
                                                 HeldMessage.shown_at.is_not(None))
                ).all()
                lines, buttons = _page(db, rows, start=len(shown) + 1)
                client.send_message(admin, "🌙 <b>Ще за ніч</b>\n" + "\n".join(lines),
                                    reply_markup={"inline_keyboard": buttons})
            return
        try:
            held = db.get(HeldMessage, int(parts[2]))
        except (IndexError, ValueError):
            held = None
        if held is None or held.kind != KIND_SUGGESTION or held.closed_at is not None:
            client.answer_callback(cb_id, "Уже неактуально")
            drop(held.id if held else None)
            return
        p = _payload(held)
        held.closed_at = _now()
        db.commit()
        if action == "x":
            client.answer_callback(cb_id, "Прибрав")
        elif action == "cal":
            client.answer_callback(cb_id, "Додаю в календар…")
            if admin:
                from app.modules.telegram_bot.handlers import _handle_calendar_event

                _handle_calendar_event(client, admin, {}, p.get("text") or "")
        elif action == "add":
            from app.modules.telegram_bot import task_intake

            due = None
            if p.get("due"):
                try:
                    due = date.fromisoformat(p["due"])
                except ValueError:
                    due = None
            task, created = task_intake.create_from_message(
                db, name=p.get("name"), chat_id=p.get("chat_id"),
                message_id=p.get("message_id"), text=p.get("text") or "",
                kind=p.get("kind") or "task", commitment_type=p.get("type") or "promise",
                outgoing=bool(p.get("outgoing")), due=due,
            )
            client.answer_callback(cb_id, "➕ Задачу створено" if created else "Задача вже є")
        else:
            client.answer_callback(cb_id)
            return
        drop(held.id)
