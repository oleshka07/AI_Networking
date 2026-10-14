"""Задачі із зовнішніх джерел: синк із чек-листом Claude через MCP.

Сервер до чек-листа не дістає (база артефакту доступна лише Claude), тож міст
— це Claude: він читає картки і кличе ці функції через MCP-інструменти.
Контракт — docs/CLAUDE_CHECKLIST.md. Головні правила:

- для задач джерела назва, нотатки, проєкт і строк беруться з джерела;
- «done» липке: закрили в одному місці — закрито всюди;
- перевідкрити може лише той бік, що закрив (``Task.done_via``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import service
from .models import Task, TaskKind, TaskSettings, TaskStatus

#: Позначка в нотатках для задач, яких джерело більше не має.
CLOSED_IN_SOURCE = "закрито в джерелі"


class ExternalError(ValueError):
    """Дієва помилка для Claude: що не так і що зробити натомість."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_settings(db: Session) -> TaskSettings:
    """Єдиний рядок налаштувань задач; створюється при першому зверненні."""
    row = db.get(TaskSettings, 1)
    if row is None:
        row = TaskSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def note_sync(db: Session, tool: str, error: str | None = None) -> None:
    """Запамʼятовує звернення синку — для панелі «Синк з чек-листом» на сайті."""
    row = get_settings(db)
    row.sync_last_at = _now()
    row.sync_last_tool = tool[:40]
    if error:
        row.sync_error = error[:500]
        row.sync_error_at = _now()
    db.commit()


def _source(value) -> str:
    source = str(value or "").strip().lower()
    if source not in service.SOURCES:
        raise ExternalError(
            f"Невідоме джерело {value!r}. Можна: {', '.join(service.SOURCES)}."
        )
    return source


def _external_id(value) -> str:
    ext = str(value or "").strip()
    if not ext:
        raise ExternalError("Потрібен external_id — ідентифікатор задачі в джерелі.")
    if len(ext) > 80:
        raise ExternalError(f"external_id довший за 80 символів ({len(ext)}).")
    return ext


def _status(value) -> TaskStatus | None:
    if value in (None, ""):
        return None
    try:
        return TaskStatus(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(s.value for s in TaskStatus)
        raise ExternalError(f"Невідомий статус {value!r}. Можна: {allowed}.") from exc


def _kind(value) -> TaskKind | None:
    if value in (None, ""):
        return None
    try:
        return TaskKind(str(value).strip().lower())
    except ValueError as exc:
        raise ExternalError(f"Невідомий вид {value!r}. Можна: task, duty, expectation.") from exc


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ExternalError(f"Дата має бути YYYY-MM-DD, отримав {value!r}.") from exc


def _tags(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value]
    else:
        parts = [p.strip() for p in str(value).split(",")]
    joined = ",".join(p for p in parts if p)
    return joined[:300] or None


def _clip(value, n: int) -> str | None:
    text = str(value or "").strip()
    return text[:n] if text else None


def _close_from_source(task: Task) -> None:
    task.status = TaskStatus.done
    task.done_via = "source"


def upsert(db: Session, args: dict) -> dict:
    """Створює або оновлює задачу за (source, external_id).

    Повертає ``{"uid", "created", "status"}``; якщо задачу закрили на сервері,
    а з джерела прийшла відкрита — не відкриває і додає ``"conflict"``.
    Поля, яких немає в ``args``, не чіпаються: часткове оновлення безпечне.
    """
    source = _source(args.get("source"))
    ext = _external_id(args.get("external_id"))
    status = _status(args.get("status"))
    kind = _kind(args.get("kind"))

    task = service.find_external(db, source, ext)
    if task is not None and task.deleted_at is not None:
        # Видалили на сервері свідомо (наприклад, «без підготовки») — не воскрешаємо.
        return {"uid": task.uid, "created": False, "status": "deleted",
                "conflict": "deleted_on_server"}

    created = task is None
    if created:
        title = _clip(args.get("title"), 300)
        if not title:
            raise ExternalError("Нова задача потребує title.")
        task = Task(uid=service.new_uid(), title=title, source=source, external_id=ext)
        db.add(task)

    if "title" in args and _clip(args.get("title"), 300):
        task.title = _clip(args.get("title"), 300)
    if kind is not None:
        task.kind = kind
    if "project" in args:
        service.set_project(db, task, _clip(args.get("project"), 120))
    if "due_date" in args:
        task.due_date = _date(args.get("due_date"))
    if "duration_min" in args:
        try:
            minutes = int(args.get("duration_min") or 0)
        except (TypeError, ValueError):
            minutes = 0
        task.duration_min = max(1, min(minutes, 1440)) if minutes else None
    if "tags" in args:
        task.tags = _tags(args.get("tags"))
    if "notes" in args:
        task.notes = _clip(args.get("notes"), 2000)
    if "url" in args:
        task.external_url = _clip(args.get("url"), 500)
    if "blocks_work" in args:
        task.blocks_work = bool(args.get("blocks_work"))
    if "counterpart" in args:
        task.counterpart = _clip(args.get("counterpart"), 120)

    result: dict = {"uid": task.uid, "created": created}
    was_done = task.status == TaskStatus.done and not created
    if status == TaskStatus.done:
        if not was_done:
            _close_from_source(task)
    elif status is not None:
        if was_done and task.done_via != "source":
            # Закрили тут (Telegram, сайт, Google) — перевідкрити може лише сервер.
            result["conflict"] = "done_on_server"
        else:
            task.status = status

    db.commit()
    db.refresh(task)
    result["status"] = task.status.value
    return result


def change_row(t: Task) -> dict:
    """Одна задача у форматі, який читає синк."""
    def iso(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return service.to_utc(value).isoformat()
        return value.isoformat()

    row = {
        "uid": t.uid,
        "source": t.source,
        "external_id": t.external_id,
        "linked": t.linked_ref,
        "url": t.external_url,
        "title": t.title,
        "project": t.project,
        "status": t.status.value if t.status else None,
        "kind": t.kind.value if t.kind else None,
        "blocks_work": bool(t.blocks_work),
        "done_at": iso(t.done_at),
        "due_date": iso(t.due_date),
        "counterpart": t.counterpart,
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
    }
    if t.deleted_at is not None:
        row["deleted"] = True
    return row


def _parse_since(value) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExternalError(f"since має бути ISO-часом, отримав {value!r}.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def list_changes(db: Session, args: dict) -> dict:
    """Усе створене, змінене, закрите чи видалене після ``since``.

    ``server_time`` береться ДО читання: що зміниться під час запиту, прийде
    наступного разу (повтор нешкідливий, синк ідемпотентний). Фільтр — у
    Python, бо SQLite віддає час без зони, а Postgres — із зоною.
    """
    server_time = _now()
    since = _parse_since(args.get("since"))
    raw_sources = args.get("sources") or []
    if isinstance(raw_sources, str):
        raw_sources = [s for s in raw_sources.split(",")]
    sources = {_source(s) for s in raw_sources if str(s).strip()}

    rows = []
    for t in db.scalars(select(Task).order_by(Task.id)):
        if sources:
            linked_source = (t.linked_ref or "").split(":", 1)[0] if t.linked_ref else None
            if t.source not in sources and linked_source not in sources:
                continue
        stamp = service.to_utc(t.updated_at) or service.to_utc(t.created_at)
        if since is not None and (stamp is None or stamp <= since):
            continue
        rows.append(change_row(t))
    return {"server_time": server_time.isoformat(), "changes": rows[:500],
            "truncated": len(rows) > 500}


def link(db: Session, args: dict) -> dict:
    """Звʼязує рідну задачу сервера з карткою джерела (зазвичай чек-листа).

    Власні source/external_id задачі лишаються — за ними дедуп події чи
    повідомлення; картка записується в ``linked_ref``, і upsert із джерела
    далі знаходить саме цю задачу, а не створює другу.
    """
    uid = str(args.get("uid") or "").strip().lstrip("#")
    task = service.get_by_uid(db, uid) if uid else None
    if task is None or task.deleted_at is not None:
        raise ExternalError("Такої задачі немає. uid — з crm_list_task_changes.")
    source = _source(args.get("source"))
    ext = _external_id(args.get("external_id"))

    other = service.find_external(db, source, ext)
    if other is not None and other.id != task.id:
        raise ExternalError(
            f"Картка {source}:{ext} уже звʼязана із задачею #{other.uid}. "
            "Закрий одну з них або звʼяжи іншу картку."
        )
    if task.source == source:
        # Задача з того самого джерела: це її власний ідентифікатор, не звʼязок.
        task.external_id = ext
    else:
        task.linked_ref = service.link_key(source, ext)
    url = _clip(args.get("url"), 500)
    if url:
        task.external_url = url
    db.commit()
    db.refresh(task)
    return {"uid": task.uid, "linked": task.linked_ref or service.link_key(task.source, ext)}


def close_missing(db: Session, args: dict) -> dict:
    """Закриває задачі джерела, яких немає в ``keep_external_ids``.

    Порожній список закрив би все джерело, а це частіше збій читання
    чек-листа, ніж правда, — тож для нього потрібне явне ``confirm_all``.
    """
    source = _source(args.get("source"))
    keep_raw = args.get("keep_external_ids")
    if keep_raw is None:
        raise ExternalError("Потрібен keep_external_ids — список id карток, які лишаються.")
    if isinstance(keep_raw, str):
        keep_raw = [k for k in keep_raw.split(",")]
    keep = {str(k).strip() for k in keep_raw if str(k).strip()}
    if not keep and not args.get("confirm_all"):
        raise ExternalError(
            "Порожній keep_external_ids закрив би всі задачі джерела. Якщо це "
            "справді так — передай confirm_all: true."
        )

    stamp = f"{datetime.now():%d.%m.%y}: {CLOSED_IN_SOURCE}"
    closed = []
    for task in service.tasks_of_source(db, source):
        if task.status == TaskStatus.done:
            continue
        key = service.external_key_of(task, source)
        if key is None or key in keep:
            continue
        _close_from_source(task)
        before = (task.notes or "").rstrip()
        task.notes = f"{before}\n{stamp}" if before else stamp
        closed.append(task.uid)
    db.commit()
    return {"closed": closed, "count": len(closed)}


def sync_counts(db: Session) -> dict:
    """Скільки задач чек-листа відкрито й закрито — для панелі на сайті."""
    open_n = closed_n = 0
    for task in service.tasks_of_source(db, "claude"):
        if task.status == TaskStatus.done:
            closed_n += 1
        else:
            open_n += 1
    return {"open": open_n, "closed": closed_n}
