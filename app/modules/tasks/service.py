"""Tasks service: CRUD, Dividify-style ordering, and sync with the PC agent."""

from __future__ import annotations

import html
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Task, TaskKind, TaskNudge, TaskStatus
from .schemas import SyncRequest, TaskIn, TaskUpdate

# Вага статусу. Immediate завжди перша; hold — вниз; решта нарівні,
# бо «я вже працюю» не має підвищувати пріоритет (лише індикатор).
_RANK = {
    TaskStatus.immediate: 0,
    TaskStatus.urgent: 2,      # 1 — коли настав дедлайн (див. order_key)
    TaskStatus.current: 3,
    TaskStatus.todo: 3,
    TaskStatus.hold: 5,
    TaskStatus.done: 9,
}
_FAR = 10_000  # «дати немає» — в кінець
#: Далека дата для сортування задач без нагадування — теж у кінець.
_FAR_DT = datetime(9999, 1, 1, tzinfo=timezone.utc)


def new_uid() -> str:
    return secrets.token_hex(3)  # 6 символів — читабельно в focus.md


def order_key(t: Task, today: date | None = None):
    """Тримає роботу → статус → дата → тривалість. Коротші вперед: швидкі перемоги.

    ``blocks_work`` іде першим серед незакритих: поки задача стоїть, стоїть
    і чиясь робота, тож вона важить більше за будь-який статус.
    """
    today = today or datetime.now(timezone.utc).date()
    rank = _RANK.get(t.status, 3)
    days = (t.due_date - today).days if t.due_date else _FAR
    if t.status == TaskStatus.urgent and t.due_date and days <= 0:
        rank = 1  # у день дедлайну підіймається майже на самий верх
    blocker = 0 if (t.blocks_work and t.status != TaskStatus.done) else 1
    return (blocker, rank, days, t.duration_min or _FAR, t.id)


def is_snoozed(t: Task, now: datetime | None = None) -> bool:
    """Задачу відклали кнопкою «⏰ Завтра» — до цього часу вона не нагадує."""
    until = to_utc(t.snoozed_until)
    return until is not None and until > (now or datetime.now(timezone.utc))


def snooze_until_tomorrow(now: datetime | None = None) -> datetime:
    """Завтра 09:00 за часом сервера (як і решта розкладу), у UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone()
    tomorrow = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(timezone.utc)


def snooze(db: Session, task: Task) -> Task:
    task.snoozed_until = snooze_until_tomorrow()
    db.commit()
    db.refresh(task)
    return task


# ── Задачі із зовнішніх джерел ──────────────────────────────────────────────

#: Звідки бувають задачі. Рядок у базі; новий — дописати сюди.
SOURCES = ("manual", "bot", "focus", "gate", "claude", "telegram", "calendar", "email")


def link_key(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


def find_external(db: Session, source: str, external_id: str,
                  *, include_deleted: bool = True) -> Task | None:
    """Задача за (source, external_id) — своя або рідна, звʼязана з карткою.

    Пара унікальна серед непорожніх, і тримає це сервіс, а не індекс: SQLite і
    Postgres по-різному поводяться з частковими унікальними індексами. Якщо
    рядків раптом кілька — беремо найстаріший, решта лишається видимою.
    """
    if not source or not external_id:
        return None
    from sqlalchemy import or_

    stmt = select(Task).where(
        or_(
            (Task.source == source) & (Task.external_id == external_id),
            Task.linked_ref == link_key(source, external_id),
        )
    )
    if not include_deleted:
        stmt = stmt.where(Task.deleted_at.is_(None))
    return db.scalars(stmt.order_by(Task.id)).first()


def tasks_of_source(db: Session, source: str) -> list[Task]:
    """Живі задачі джерела: власні й рідні, звʼязані з ним через ``linked_ref``."""
    from sqlalchemy import or_

    return list(
        db.scalars(
            select(Task).where(
                Task.deleted_at.is_(None),
                or_(Task.source == source, Task.linked_ref.like(f"{source}:%")),
            )
        )
    )


def external_key_of(t: Task, source: str) -> str | None:
    """Ідентифікатор задачі в джерелі ``source`` (власний або через звʼязок)."""
    if t.source == source and t.external_id:
        return t.external_id
    if t.linked_ref and t.linked_ref.startswith(source + ":"):
        return t.linked_ref[len(source) + 1:]
    return None


def set_project(db: Session, task: Task, name: str | None) -> None:
    """Назва проєкту → рядок у projects. Єдине місце, де ставляться обидва поля."""
    from app.modules.projects import service as projects

    project = projects.ensure(db, name)
    task.project = project.name if project else None
    task.project_id = project.id if project else None


def list_tasks(db: Session, *, include_done: bool = False,
               kind: TaskKind | None = TaskKind.task) -> list[Task]:
    """За замовчуванням — лише звичайні задачі: обовʼязки й очікування
    живуть окремими списками і в загальну чергу не потрапляють."""
    stmt = select(Task).where(Task.deleted_at.is_(None))
    if not include_done:
        stmt = stmt.where(Task.status != TaskStatus.done)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind)
    return sorted(db.scalars(stmt).unique(), key=order_key)


def next_task(db: Session) -> Task | None:
    """Що робити прямо зараз — перша за алгоритмом."""
    tasks = list_tasks(db)
    return tasks[0] if tasks else None


def get_by_uid(db: Session, uid: str) -> Task | None:
    return db.scalar(select(Task).where(Task.uid == uid))


def create_task(db: Session, payload: TaskIn) -> Task:
    fields = payload.model_dump()
    project_name = fields.pop("project", None)
    task = Task(uid=new_uid(), **fields)
    db.add(task)
    set_project(db, task, project_name)
    db.commit()
    db.refresh(task)
    return task


def update_task(db: Session, task: Task, payload: TaskUpdate) -> Task:
    fields = payload.model_dump(exclude_unset=True)
    if "project" in fields:
        set_project(db, task, fields.pop("project"))
    for field, value in fields.items():
        setattr(task, field, value)
    db.commit()
    db.refresh(task)
    return task


def soft_delete(db: Session, task: Task) -> None:
    task.deleted_at = datetime.now(timezone.utc)
    db.commit()


def nudge_text(db: Session) -> str | None:
    """Що зараз у роботі і що далі — коротко, для Telegram."""
    active = db.scalar(
        select(Task).where(Task.deleted_at.is_(None), Task.status == TaskStatus.current)
    )
    queue = [t for t in list_tasks(db) if t.status != TaskStatus.current]
    if not active and not queue:
        return None

    lines = []
    if active:
        lines.append(f"🎯 <b>Зараз:</b> {html.escape(active.title)}")
    else:
        lines.append("⚠️ <b>Задачу не взято.</b>")
    if queue:
        lines.append("")
        lines.append("<b>Далі:</b>")
        lines += [f"• {html.escape(t.title)}" for t in queue[:3]]
    return "\n".join(lines)


def send_nudge(db: Session) -> bool:
    """Надсилає нагадування в Telegram, прибравши попереднє.

    У чаті завжди рівно одне актуальне повідомлення — старі не накопичуються.
    """
    from app.modules.automation import telegram

    text = nudge_text(db)
    if not text:
        return False

    row = db.get(TaskNudge, 1)
    if row and row.chat_id and row.message_id:
        telegram.delete_message(row.chat_id, row.message_id)

    sent = telegram.send_and_get_id(text)
    if not sent:
        return False
    chat_id, message_id = sent
    if row is None:
        row = TaskNudge(id=1)
        db.add(row)
    row.chat_id, row.message_id = chat_id, message_id
    db.commit()
    return True


def sync(db: Session, payload: SyncRequest) -> tuple[list[Task], int, int]:
    """Злиття з ПК. Перемагає той, хто редагував пізніше (одинокий користувач).

    ponytail: час правки — спільний на весь focus.md, не по задачах. Для одного
    користувача цього досить; на кілька пристроїв знадобиться час на задачу.
    """
    local_time = payload.updated_at or datetime.now(timezone.utc)
    if local_time.tzinfo is None:
        local_time = local_time.replace(tzinfo=timezone.utc)

    applied = 0
    seen: set[str] = set()
    parent_of: dict[str, str] = {}   # uid задачі → uid її батька

    for incoming in payload.tasks:
        task = get_by_uid(db, incoming.uid) if incoming.uid else None
        if task is None:
            task = Task(uid=incoming.uid or new_uid(), source="focus")
            db.add(task)
            applied += 1
        else:
            server_time = task.updated_at
            if server_time and server_time.tzinfo is None:
                server_time = server_time.replace(tzinfo=timezone.utc)
            if server_time and server_time > local_time:
                seen.add(task.uid)
                continue  # на сервері свіжіше — правку з ПК ігноруємо
            applied += 1
        task.title = incoming.title
        task.status = incoming.status
        task.kind = incoming.kind
        task.tags = incoming.tags
        task.item_type = incoming.item_type
        task.counterpart = incoming.counterpart
        task.recur = incoming.recur
        set_project(db, task, incoming.project)
        if incoming.due_date is not None:
            task.due_date = incoming.due_date
        if incoming.duration_min is not None:
            task.duration_min = incoming.duration_min
        task.goal_id = incoming.goal_id
        task.deleted_at = None
        seen.add(task.uid)
        parent_of[task.uid] = incoming.parent_uid

    # Другим проходом — бо батько міг бути створений у цьому ж запиті.
    db.flush()
    for uid, parent_uid in parent_of.items():
        task = get_by_uid(db, uid)
        parent = get_by_uid(db, parent_uid) if parent_uid else None
        task.parent_id = parent.id if parent and parent.id != task.id else None

    deleted = 0
    if payload.authoritative:
        # ПК щойно редагували → чого там немає, те видалено
        for task in db.scalars(select(Task).where(Task.deleted_at.is_(None))):
            if task.uid not in seen:
                task.deleted_at = datetime.now(timezone.utc)
                deleted += 1

    db.commit()
    # kind=None → віддаємо ВСЕ (задачі + обовʼязки + очікування): агент розкладе
    # їх по своїх списках у focus.md.
    return list_tasks(db, include_done=True, kind=None), applied, deleted


# ── GTD contexts ─────────────────────────────────────────────────────────────
# Where/with-what a task can actually be done. Stored in the existing `tags`
# field as "@дзвінки" (focus.md on the PC can write them too), plus derived
# from item_type so old tasks get a context without re-tagging.

_CONTEXT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "@дзвінки": ("дзвін", "дзвонит", "телефон", "call", "подзвон", "созвон"),
    "@компʼютер": ("комп", "ноут", "computer", "pc", "лист", "email", "файл"),
    "@місто": ("місто", "місті", "вулиц", "errand", "магазин", "банк", "пошт"),
    "@дім": ("дім", "дома", "вдома", "home", "хата"),
    "@офіс": ("офіс", "office", "робот"),
    "@люди": ("люди", "написат", "поговорит", "зустріч", "meeting"),
}


def normalize_context(raw: str | None) -> str | None:
    """'дзвінки' / '@Дзвінок' / 'call' → '@дзвінки'. Unknown words keep their
    own shape ('@клієнти') so ad-hoc contexts still work."""
    if not raw:
        return None
    word = raw.strip().lstrip("@").strip().lower()
    if not word:
        return None
    for canonical, stems in _CONTEXT_SYNONYMS.items():
        if any(word.startswith(s) or s.startswith(word) for s in stems if len(word) > 2):
            return canonical
    return "@" + word


def task_contexts(task: Task) -> set[str]:
    """Contexts a task belongs to: explicit @tags plus one derived from
    item_type (so 'дзвінок' lands in @дзвінки without any tagging)."""
    out: set[str] = set()
    for tag in (task.tags or "").split(","):
        tag = tag.strip()
        if tag.startswith("@"):
            ctx = normalize_context(tag)
            if ctx:
                out.add(ctx)
    derived = normalize_context(task.item_type) if task.item_type else None
    if derived:
        out.add(derived)
    return out


def list_contexts(db: Session) -> dict[str, int]:
    """Context → how many open tasks are doable in it, biggest first."""
    counts: dict[str, int] = {}
    for t in list_tasks(db):
        for ctx in task_contexts(t):
            counts[ctx] = counts.get(ctx, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def tasks_in_context(db: Session, context: str) -> list[Task]:
    """Open tasks doable in this context, in the usual execution order."""
    ctx = normalize_context(context)
    if not ctx:
        return []
    return [t for t in list_tasks(db) if ctx in task_contexts(t)]


# ── Задачі-нагадування (періодичні) ─────────────────────────────────────────

#: Слова періодичності -> інтервал у днях. Ключі шукаються підрядком.
_RECUR_DAYS: tuple[tuple[str, int], ...] = (
    ("щодня", 1), ("кожен день", 1), ("щоденно", 1), ("daily", 1),
    ("раз на тиждень", 7), ("щотижня", 7), ("щотижнево", 7), ("weekly", 7),
    ("раз на два тижні", 14), ("раз на 2 тижні", 14), ("кожні два тижні", 14),
    ("раз на місяць", 30), ("щомісяця", 30), ("щомісячно", 30), ("monthly", 30),
    ("раз на квартал", 90), ("щокварталу", 90), ("quarterly", 90),
    ("раз на пів року", 182), ("раз на півроку", 182),
    ("раз на рік", 365), ("щороку", 365), ("yearly", 365),
)


def parse_recur(text: str | None) -> tuple[str, int] | None:
    """Витягує періодичність зі слів: («раз на місяць», 30).

    Спершу шукає явні формати «раз на N днів/тижнів/місяців», далі — сталі
    вирази. ``None`` — періодичності в тексті немає, її треба перепитати.
    """
    import re

    if not text:
        return None
    low = " ".join(text.lower().split())

    m = re.search(r"(?:раз|кожн\w*)\s+(?:на\s+)?(\d+)\s*(день|дн\w*|тижн\w*|місяц\w*)", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * (7 if unit.startswith("тижн") else 30 if unit.startswith("місяц") else 1)
        if 1 <= days <= 3650:
            return m.group(0), days

    for phrase, days in _RECUR_DAYS:
        if phrase in low:
            return phrase, days
    return None


def create_reminder_task(
    db: Session,
    *,
    title: str,
    counterpart: str | None = None,
    recur: str | None = None,
    recur_days: int | None = None,
    goal_id: int | None = None,
) -> Task:
    """Задача, про яку бот нагадуватиме сам.

    Без ``recur_days`` задача створюється, але нагадувань не буде, поки
    періодичність не назвуть — бот її перепитає.
    """
    task = Task(
        uid=new_uid(),
        title=title.strip()[:300],
        counterpart=(counterpart or None),
        recur=(recur or None),
        recur_days=recur_days,
        goal_id=goal_id,
        item_type="нагадування",
        source="bot",
        next_remind_at=(
            datetime.now(timezone.utc) + timedelta(days=recur_days)
            if recur_days
            else None
        ),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def set_recurrence(db: Session, task: Task, recur: str, recur_days: int) -> Task:
    """Проставляє періодичність і призначає перше нагадування."""
    task.recur = recur[:20]
    task.recur_days = recur_days
    task.next_remind_at = datetime.now(timezone.utc) + timedelta(days=recur_days)
    db.commit()
    db.refresh(task)
    return task


def to_utc(value: datetime | None) -> datetime | None:
    """SQLite віддає час без зони — зводимо все до UTC перед порівнянням."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reminder_tasks(db: Session, *, include_done: bool = False) -> list[Task]:
    """Усі задачі-нагадування, найближчі за датою нагадування — першими."""
    stmt = select(Task).where(Task.deleted_at.is_(None), Task.item_type == "нагадування")
    if not include_done:
        stmt = stmt.where(Task.status != TaskStatus.done)
    rows = list(db.scalars(stmt))
    rows.sort(key=lambda t: (t.next_remind_at is None, to_utc(t.next_remind_at) or _FAR_DT, t.id))
    return rows


def due_reminder_tasks(db: Session) -> list[Task]:
    """Задачі, яким час нагадати. Пауза (hold) нагадувань не отримує."""
    now = datetime.now(timezone.utc)
    return [
        t
        for t in reminder_tasks(db)
        if t.next_remind_at is not None
        and t.status not in (TaskStatus.done, TaskStatus.hold)
        and to_utc(t.next_remind_at) <= now
        and not is_snoozed(t, now)
    ]


def log_reminder_answer(db: Session, task: Task, summary: str) -> Task:
    """Дописує «дд.мм.рр: суть» у нотатки і переносить наступне нагадування."""
    line = f"{datetime.now():%d.%m.%y}: {' '.join(summary.split())}"
    before = (task.notes or "").rstrip()
    task.notes = f"{before}\n{line}" if before else line
    # Закриту задачу не переплановуємо — інакше «готово» саме себе воскресило б.
    if task.recur_days and task.status != TaskStatus.done:
        task.next_remind_at = datetime.now(timezone.utc) + timedelta(days=task.recur_days)
    db.commit()
    db.refresh(task)
    return task
