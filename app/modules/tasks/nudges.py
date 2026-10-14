"""Нагадування про задачі: кілька правил замість окремих пінгів.

- **Ранковий план** — блок «Сьогодні» у щоденному прогоні: до трьох задач
  (``blocks_work`` першими) і зустрічі дня з позначкою, чи є підготовка.
- **Блокери** — задача з ``blocks_work``, старша за добу: пінг не частіше
  разу на N годин і лише вдень. Логіка та сама, що в застояних воротах.
- **Строки** — строк сьогодні або прострочений: одним блоком у денній звірці.
- **Підготовка** — напередодні ввечері задача «Підготуватися: …» з брифом на
  кожну завтрашню зустріч із зовнішніми учасниками.
- **Тиша** — вночі нічого, крім брифу перед зустріччю; денна стеля
  проактивних повідомлень.

Години й прапорці — у ``task_settings`` (сторінка «Нагадування» на сайті),
тож зміна розкладу не потребує перезапуску контейнера.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal

from . import service
from .external import get_settings
from .models import Task, TaskKind, TaskSettings, TaskStatus

logger = logging.getLogger("networking.task_nudges")

#: З якого віку задача з blocks_work вважається застояною.
BLOCKER_STALE_HOURS = 24
#: Скільки задач у ранковому плані.
PLAN_SIZE = 3
#: Слова в назві події, через які вона потребує підготовки й без гостей.
_PREP_WORDS_RE = re.compile(
    r"зустріч|дзвін|зідзвон|созвон|встреч|звонок|schůzk|\bmeeting\b|\bcall\b|\bmeet\b",
    re.IGNORECASE,
)


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    """Локальний час сервера без зони — за ним живе весь розклад."""
    return datetime.now()


def _configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


# ── Тиша і денна стеля ──────────────────────────────────────────────────────


def _minutes(hhmm: str | None, default: int) -> int:
    try:
        h, m = str(hhmm).split(":")
        return max(0, min(23, int(h))) * 60 + max(0, min(59, int(m)))
    except (ValueError, AttributeError):
        return default


def in_quiet(cfg: TaskSettings, local_now: datetime | None = None) -> bool:
    """Чи зараз години тиші. Вікно може переходити через північ (22:30–07:30)."""
    local_now = local_now or _local_now()
    start = _minutes(cfg.quiet_from, 22 * 60 + 30)
    end = _minutes(cfg.quiet_to, 7 * 60 + 30)
    now_m = local_now.hour * 60 + local_now.minute
    if start == end:
        return False
    if start < end:
        return start <= now_m < end
    return now_m >= start or now_m < end


def _reset_day(cfg: TaskSettings, local_now: datetime) -> None:
    day = local_now.date().isoformat()
    if cfg.sent_day != day:
        cfg.sent_day = day
        cfg.sent_count = 0


def allow_proactive(db: Session, local_now: datetime | None = None) -> bool:
    """Можна надіслати ще одне проактивне повідомлення? Так — і воно пораховане.

    Ні — у години тиші або коли денна стеля вичерпана. Нагадування, що
    приходить постійно, перестають помічати; краще промовчати.
    """
    local_now = local_now or _local_now()
    cfg = get_settings(db)
    if in_quiet(cfg, local_now):
        return False
    _reset_day(cfg, local_now)
    if cfg.sent_count >= max(0, cfg.daily_cap):
        db.commit()
        return False
    cfg.sent_count += 1
    db.commit()
    return True


def count_proactive(db: Session, local_now: datetime | None = None) -> None:
    """Рахує повідомлення, яке йде за своїм розкладом (ранковий план) — у стелю."""
    local_now = local_now or _local_now()
    cfg = get_settings(db)
    _reset_day(cfg, local_now)
    cfg.sent_count += 1
    db.commit()


# ── Кнопки ──────────────────────────────────────────────────────────────────


def _url_ok(url: str | None) -> bool:
    return bool(url) and str(url).startswith(("https://", "http://"))


def task_keyboard_rows(tasks: list[Task]) -> list[list[dict]]:
    """Кнопки під повідомленням про задачі.

    Одна задача — ряд ✓ Готово / ▶ Взяв у роботу і ряд ⏰ Завтра / 🔗 Відкрити.
    Кілька — по ряду на задачу з номером, щоб повідомлення лишалось одним.
    """
    if not tasks:
        return []
    if len(tasks) == 1:
        t = tasks[0]
        second = [{"text": "⏰ Завтра", "callback_data": f"tk:snz:{t.id}"}]
        if _url_ok(t.external_url):
            second.append({"text": "🔗 Відкрити", "url": t.external_url})
        return [
            [{"text": "✓ Готово", "callback_data": f"tk:done:{t.id}"},
             {"text": "▶ Взяв у роботу", "callback_data": f"tk:cur:{t.id}"}],
            second,
        ]
    rows = []
    for n, t in enumerate(tasks, 1):
        row = [
            {"text": f"✓ {n}", "callback_data": f"tk:done:{t.id}"},
            {"text": f"▶ {n}", "callback_data": f"tk:cur:{t.id}"},
            {"text": f"⏰ {n}", "callback_data": f"tk:snz:{t.id}"},
        ]
        if _url_ok(t.external_url):
            row.append({"text": f"🔗 {n}", "url": t.external_url})
        rows.append(row)
    return rows


def task_keyboard(tasks: list[Task]) -> dict | None:
    rows = task_keyboard_rows(tasks)
    return {"inline_keyboard": rows} if rows else None


def _meta(t: Task) -> str:
    from app.modules.telegram_bot.handlers import _task_meta

    bits = [_task_meta(t)]
    if t.blocks_work:
        bits.insert(0, "⛔ тримає роботу")
    if t.counterpart and t.item_type != "нагадування":
        bits.append(_esc(t.counterpart))
    return " · ".join(bits)


def _numbered(tasks: list[Task]) -> list[str]:
    if len(tasks) == 1:
        return [f"• {_esc(tasks[0].title)}\n  <i>{_meta(tasks[0])}</i>"]
    return [f"{n}. {_esc(t.title)}\n  <i>{_meta(t)}</i>" for n, t in enumerate(tasks, 1)]


# ── Відбір задач ────────────────────────────────────────────────────────────


def open_tasks(db: Session, now: datetime | None = None) -> list[Task]:
    """Живі незакриті задачі всіх видів, не відкладені, у порядку виконання."""
    now = now or _now()
    return [
        t for t in service.list_tasks(db, kind=None)
        if not service.is_snoozed(t, now)
    ]


def plan_tasks(db: Session, now: datetime | None = None) -> list[Task]:
    """До трьох задач на сьогодні: свої (не очікування від інших) і не на паузі."""
    rows = [
        t for t in open_tasks(db, now)
        if t.kind != TaskKind.expectation and t.status != TaskStatus.hold
    ]
    return rows[:PLAN_SIZE]


def stale_blockers(db: Session, now: datetime | None = None) -> list[Task]:
    now = now or _now()
    cutoff = now - timedelta(hours=BLOCKER_STALE_HOURS)
    return [
        t for t in open_tasks(db, now)
        if t.blocks_work and (service.to_utc(t.created_at) or now) <= cutoff
    ]


def deadline_tasks(db: Session, today: date | None = None,
                   now: datetime | None = None) -> list[Task]:
    """Строк сьогодні або прострочений — усі види, включно з «перевірити»."""
    today = today or _local_now().date()
    return [t for t in open_tasks(db, now) if t.due_date and t.due_date <= today]


# ── Ранковий план ───────────────────────────────────────────────────────────


def _events_between(db: Session, start: datetime, end: datetime) -> list[dict]:
    """Події календаря між двома моментами. Google лежить — порожньо, не падаємо."""
    from app.integrations import google

    now = _now()
    minutes_min = max(0, int((start - now).total_seconds() // 60))
    minutes_max = max(minutes_min + 1, int((end - now).total_seconds() // 60))
    try:
        if not google.status(db).get("connected"):
            return []
        return google.upcoming_events(
            db, minutes_min=minutes_min, minutes_max=minutes_max, max_results=25
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # pragma: no cover - мережа, нативні бібліотеки
        logger.warning("calendar unavailable: %s", exc)
        return []


def _local_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day).astimezone()
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def has_prep(db: Session, event_id: str) -> bool:
    task = service.find_external(db, "calendar", (event_id or "")[:80], include_deleted=False)
    return task is not None


def plan_block(db: Session, *, events: list[dict] | None = None,
               local_now: datetime | None = None) -> tuple[str, list[Task]] | None:
    """Блок «Сьогодні» для ранкового повідомлення, або ``None``, якщо нема що сказати."""
    cfg = get_settings(db)
    if not (cfg.enabled and cfg.plan_enabled):
        return None
    local_now = local_now or _local_now()
    tasks = plan_tasks(db)
    if events is None:
        _, day_end = _local_day_bounds(local_now.date())
        events = _events_between(db, _now(), day_end)
    if not tasks and not events:
        return None

    lines = ["🗓 <b>Сьогодні</b>"]
    lines += _numbered(tasks)
    if events:
        lines.append("")
        for e in events[:6]:
            start = e["start"].astimezone() if e["start"].tzinfo else e["start"]
            mark = "підготовка є" if has_prep(db, e.get("id", "")) else "без підготовки"
            lines.append(f"📅 {start:%H:%M} {_esc(e.get('summary'))} · {mark}")
    return "\n".join(lines), tasks


# ── Блокери ─────────────────────────────────────────────────────────────────


def blocker_ping(db: Session, *, local_now: datetime | None = None,
                 now: datetime | None = None) -> tuple[str, list[Task]] | None:
    """Пінг про застояні задачі з ``blocks_work``, або ``None``, якщо мовчимо."""
    cfg = get_settings(db)
    if not (cfg.enabled and cfg.blockers_enabled):
        return None
    local_now = local_now or _local_now()
    now = now or _now()
    if not (cfg.blocker_from_hour <= local_now.hour < cfg.blocker_to_hour):
        return None
    stale = stale_blockers(db, now)
    if not stale:
        return None
    last = service.to_utc(cfg.blocker_pinged_at)
    if last and now - last < timedelta(hours=max(1, cfg.blocker_every_hours)):
        return None
    if not allow_proactive(db, local_now):
        return None
    cfg.blocker_pinged_at = now
    db.commit()

    shown = stale[:3]
    lines = ["⛔ <b>Тримає роботу понад добу</b>"]
    for t in shown:
        days = int((now - (service.to_utc(t.created_at) or now)).total_seconds() // 86400)
        lines.append(f"• {_esc(t.title)} — висить {days} дн")
    if len(stale) > len(shown):
        lines.append(f"…і ще {len(stale) - len(shown)}.")
    return "\n".join(lines), shown


# ── Строки ──────────────────────────────────────────────────────────────────


def deadlines_block(db: Session, *, today: date | None = None) -> tuple[str, list[Task]] | None:
    """Блок «Строки» для денної звірки: сьогодні й прострочені, одним абзацом."""
    cfg = get_settings(db)
    if not (cfg.enabled and cfg.deadlines_enabled):
        return None
    today = today or _local_now().date()
    rows = deadline_tasks(db, today)
    if not rows:
        return None
    overdue = [t for t in rows if t.due_date < today]
    lines = [f"⏰ <b>Строки</b>: сьогодні {len(rows) - len(overdue)}, прострочено {len(overdue)}"]
    for t in rows[:5]:
        when = "сьогодні" if t.due_date == today else f"з {t.due_date:%d.%m}"
        lines.append(f"• {_esc(t.title)} — {when}")
    if len(rows) > 5:
        lines.append(f"…і ще {len(rows) - 5}.")
    return "\n".join(lines), rows[:3]


# ── Підготовка до зустрічей ─────────────────────────────────────────────────


def needs_prep(event: dict) -> bool:
    """Зовнішні учасники або слово «зустріч», «дзвінок», «meeting», «call», «schůzka»."""
    if [e for e in event.get("attendee_emails") or [] if e]:
        return True
    return bool(_PREP_WORDS_RE.search(event.get("summary") or ""))


def _event_contacts(db: Session, event: dict) -> list:
    from sqlalchemy import select

    from app.modules.contacts.models import Contact

    emails = [e for e in event.get("attendee_emails") or [] if e]
    if not emails:
        return []
    return list(db.scalars(select(Contact).where(Contact.email.in_(emails))).unique())


def related_tasks(db: Session, contacts: list, event: dict) -> list[Task]:
    """Відкриті задачі, де в назві чи counterpart є імʼя учасника, компанія або проєкт."""
    needles: set[str] = set()
    for c in contacts:
        # Імʼя й прізвище — основою без останньої літери: «Ірина» знаходить
        # «Ірині», «Демченко» — «Демченку». Компанію — цілком.
        for part in (c.first_name, c.last_name):
            part = (part or "").strip().casefold()
            if len(part) >= 4:
                needles.add(part[:-1])
            elif len(part) == 3:
                needles.add(part)
        company = (c.company or "").strip().casefold()
        if len(company) >= 3:
            needles.add(company)
    summary = (event.get("summary") or "").casefold()
    out = []
    for t in open_tasks(db):
        if t.source == "calendar":
            continue
        hay = f"{t.title} {t.counterpart or ''}".casefold()
        by_person = any(n in hay for n in needles)
        by_project = bool(t.project) and len(t.project) >= 3 and t.project.casefold() in summary
        if by_person or by_project:
            out.append(t)
    return out[:5]


def prep_notes(db: Session, event: dict, contacts: list | None = None) -> str:
    """Короткий бриф простим текстом: хто, теплота, факти, останні розмови.

    Та сама логіка, що в брифі за годину до події (automation/briefs.py), лише
    без HTML — нотатки задачі читають і в Google Tasks.
    """
    contacts = _event_contacts(db, event) if contacts is None else contacts
    start = event["start"].astimezone() if event["start"].tzinfo else event["start"]
    lines = [f"Зустріч {start:%d.%m %H:%M}: {event.get('summary') or 'Зустріч'}"]
    for c in contacts:
        role = " · ".join(filter(None, [c.position, c.company]))
        head = f"Хто: {c.full_name}" + (f" — {role}" if role else "")
        head += f" · {c.warmth_status} {round(c.warmth_score)}"
        lines.append(head)
        facts = [f.value for f in getattr(c, "facts", []) if f.is_current][:3]
        if facts:
            lines.append("Факти: " + "; ".join(facts))
        for i in [i for i in c.interactions if i.summary][:2]:
            lines.append(f"Останнє {i.occurred_at:%d.%m}: {i.summary[:120]}")
    if not contacts and event.get("attendee_emails"):
        lines.append("Учасники: " + ", ".join(event["attendee_emails"][:5]))
    related = related_tasks(db, contacts, event)
    if related:
        lines.append("Повʼязані задачі: " + "; ".join(f"#{t.uid} {t.title}" for t in related))
    if event.get("meet_link"):
        lines.append(f"Посилання: {event['meet_link']}")
    return "\n".join(lines)[:2000]


def create_prep_tasks(db: Session, events: list[dict], day: date) -> list[Task]:
    """Задача «Підготуватися: …» на кожну подію ``day``, що її потребує.

    Дедуп — за id події, разом із видаленими: подію, яку власник позначив
    «без підготовки», ми більше не чіпаємо.
    """
    created = []
    for event in events:
        start = event.get("start")
        if start is None or not event.get("id"):
            continue
        local_start = start.astimezone() if start.tzinfo else start
        if local_start.date() != day or not needs_prep(event):
            continue
        event_id = event["id"][:80]
        if service.find_external(db, "calendar", event_id) is not None:
            continue
        task = Task(
            uid=service.new_uid(),
            title=f"Підготуватися: {event.get('summary') or 'зустріч'}"[:300],
            source="calendar",
            external_id=event_id,
            external_url=(event.get("html_link") or None),
            due_date=day,
            duration_min=15,
            notes=prep_notes(db, event),
        )
        db.add(task)
        db.flush()
        created.append(task)
    db.commit()
    return created


def prep_message(tasks: list[Task]) -> tuple[str, dict]:
    lines = ["📋 <b>Завтра зустрічі — підготовка</b>"]
    rows = []
    for n, t in enumerate(tasks, 1):
        lines.append(f"{n}. {_esc(t.title.removeprefix('Підготуватися: '))}")
        row = [{"text": f"🚫 Без підготовки {n}" if len(tasks) > 1 else "🚫 Без підготовки",
                "callback_data": f"tk:noprep:{t.id}"}]
        if _url_ok(t.external_url):
            row.append({"text": f"🔗 {n}" if len(tasks) > 1 else "🔗 Відкрити", "url": t.external_url})
        rows.append(row)
    lines.append("\nБриф — у нотатках задачі; за годину до початку прийде як завжди.")
    return "\n".join(lines), {"inline_keyboard": rows}


def run_prep(db: Session, *, local_now: datetime | None = None,
             events: list[dict] | None = None) -> list[Task]:
    """Створює задачі підготовки на завтра; повідомлення — якщо дозволяють тиша й стеля."""
    local_now = local_now or _local_now()
    tomorrow = local_now.date() + timedelta(days=1)
    if events is None:
        start, end = _local_day_bounds(tomorrow)
        events = _events_between(db, max(start, _now()), end)
    created = create_prep_tasks(db, events, tomorrow)
    if created and _configured() and allow_proactive(db, local_now):
        from app.modules.automation import telegram

        text, markup = prep_message(created)
        telegram.send_message(text, reply_markup=markup)
    return created


def mark_no_prep(db: Session, task: Task) -> None:
    """«Без підготовки»: задачу прибрано, а рядок лишається — за ним дедуп події."""
    task.deleted_at = _now()
    db.commit()


# ── Щогодинна задача ────────────────────────────────────────────────────────


def run_tick() -> None:
    """Щогодини: блокери й (у свою годину) підготовка. Ніколи не кидає."""
    if not _configured():
        return
    from app.modules.automation import telegram

    try:
        with SessionLocal() as db:
            cfg = get_settings(db)
            if not cfg.enabled:
                return
            local_now = _local_now()
            ping = blocker_ping(db, local_now=local_now)
            if ping:
                text, tasks = ping
                telegram.send_message(text, reply_markup=task_keyboard(tasks))
            if cfg.prep_enabled and local_now.hour == cfg.prep_hour:
                run_prep(db, local_now=local_now)
    except Exception as exc:  # pragma: no cover - фонова задача не має падати
        logger.warning("task nudges tick failed: %s", exc)
