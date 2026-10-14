"""Task model — deliberately few fields, Dividify-style.

Order is computed (status → date → duration), never hand-dragged, so there are
no priority numbers or sort columns to maintain.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, utcnow


class TaskStatus(str, enum.Enum):
    immediate = "immediate"      # робити прямо зараз — завжди перша
    urgent = "urgent"            # не можна пропустити — злітає вгору в день дедлайну
    current = "current"          # я вже над цим працюю
    todo = "todo"
    hold = "hold"                # чекає на когось — вниз списку
    done = "done"


class TaskKind(str, enum.Enum):
    task = "task"                # звичайна задача
    duty = "duty"                # обовʼязок: я комусь винен
    expectation = "expectation"  # очікування: я чекаю від когось


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Стабільний короткий ідентифікатор — ним focus.md на ПК зіставляє рядки.
    uid: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.todo, index=True
    )
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind), default=TaskKind.task, index=True
    )
    # `project` — назва (нею оперує focus.md), `project_id` — реальний звʼязок.
    # Обидва встановлюються в одному місці (з назви), тому розійтися не можуть.
    project: Mapped[str | None] = mapped_column(String(120), nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    item_type: Mapped[str | None] = mapped_column(String(40), nullable=True)   # файл/дзвінок…
    counterpart: Mapped[str | None] = mapped_column(String(120), nullable=True)  # для/від кого
    recur: Mapped[str | None] = mapped_column(String(20), nullable=True)       # monthly/weekly
    # Періодичність нагадувань: словами лежить у `recur`, у днях — тут.
    # Порожньо означає «періодичність ще не названа» — бот її перепитає.
    recur_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_remind_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(300), nullable=True)  # через кому
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    # Дзеркало в Google Tasks: id рядка там і коли востаннє звіряли.
    google_task_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
    )
    google_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Вимикається, коли задачу видалили в Google — щоб ми не пхали її назад.
    google_sync: Mapped[bool] = mapped_column(default=True)

    # Звідки задача: manual, bot, focus, gate, claude, telegram, calendar, email.
    # Рядок, не енум: дев'яте джерело коштує рядок коду, а не ALTER TYPE.
    source: Mapped[str] = mapped_column(String(20), default="manual", index=True)
    # Ідентифікатор у джерелі: id картки чек-листа, tgb:<chat>:<msg>, id події.
    # Пара (source, external_id) унікальна серед непорожніх — тримає сервіс,
    # бо SQLite і Postgres по-різному поводяться з частковими індексами.
    external_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Рідна задача сервера, звʼязана з карткою чек-листа: "claude:<id картки>".
    # Власні source/external_id лишаються — за ними дедуп (подія, повідомлення).
    linked_ref: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Задача тримає роботу Claude або людей — іде першою серед незакритих.
    blocks_work: Mapped[bool] = mapped_column(default=False)
    # До цього часу задача не нагадує (кнопка «⏰ Завтра»).
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Хто закрив: "source" — джерело (чек-лист через синк), "server" — тут
    # (Telegram, сайт, Google). Перевідкрити може лише той бік, що закрив.
    done_via: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


@event.listens_for(Task.status, "set", active_history=True)
def _stamp_done(task: Task, value, oldvalue, initiator) -> None:
    """``done_at`` і ``done_via`` ставляться тут, а не в кожному місці закриття.

    Закривають задачу з десятка місць (кнопка в Telegram, сайт, Google, MCP,
    ворота); забуте в одному з них ``done_at`` тихо ламало б синк змін.
    За замовчуванням закрив сервер; синк з джерела перепише ``done_via`` сам.
    """
    if value == TaskStatus.done and oldvalue != TaskStatus.done:
        if task.done_at is None:
            task.done_at = datetime.now(timezone.utc)
        if task.done_via is None:
            task.done_via = "server"
    elif value != TaskStatus.done and oldvalue == TaskStatus.done:
        task.done_at = None
        task.done_via = None


class TaskNudge(Base):
    """Останнє нагадування в Telegram — рівно один рядок.

    Тримаємо id, щоб нове нагадування прибирало попереднє: у чаті завжди
    одне актуальне повідомлення, а не стрічка застарілих.
    """

    __tablename__ = "task_nudges"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TaskSettings(Base):
    """Один рядок (id=1): правила нагадувань про задачі і стан синку з чек-листом.

    Години живуть у базі, як у коуча: щогодинна задача звіряється з ними,
    тож зміна на сторінці не потребує перезапуску контейнера.
    """

    __tablename__ = "task_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)

    # Ранковий план у щоденному прогоні (DAILY_RUN_HOUR).
    plan_enabled: Mapped[bool] = mapped_column(default=True)
    # Блокери: задача з blocks_work, старша за добу.
    blockers_enabled: Mapped[bool] = mapped_column(default=True)
    blocker_every_hours: Mapped[int] = mapped_column(Integer, default=6)
    blocker_from_hour: Mapped[int] = mapped_column(Integer, default=9)
    blocker_to_hour: Mapped[int] = mapped_column(Integer, default=21)
    blocker_pinged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Строки сьогодні й прострочені — блоком у денній звірці.
    deadlines_enabled: Mapped[bool] = mapped_column(default=True)
    # Підготовка до завтрашніх зустрічей.
    prep_enabled: Mapped[bool] = mapped_column(default=True)
    prep_hour: Mapped[int] = mapped_column(Integer, default=18)
    # Тиша (локальний час сервера, "ГГ:ХХ") і денна стеля проактивних повідомлень.
    quiet_from: Mapped[str] = mapped_column(String(5), default="22:30")
    quiet_to: Mapped[str] = mapped_column(String(5), default="07:30")
    daily_cap: Mapped[int] = mapped_column(Integer, default=6)
    sent_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    # Пропозиції задач із переписки в Telegram: suggest / auto / off.
    tg_suggest_mode: Mapped[str] = mapped_column(String(10), default="suggest")

    # Синк із чек-листом Claude: коли звертався і чим закінчилось.
    sync_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_last_tool: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sync_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
