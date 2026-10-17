"""Ворота — задачі, які блокують ціль, але їх не зробиш у редакторі коду.

Окремої таблиці навмисно немає: ворота — це звичайна задача з
``item_type="gate"``. Один список задач замість двох, тому вони самі собою
потрапляють у /tasks, у тижневий підсумок і в трекер цілей.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.coach.models import Checkin
from app.modules.goals.models import ACTIVE_STATUSES, Goal
from app.modules.tasks.models import Task, TaskStatus
from app.modules.tasks.service import new_uid

logger = logging.getLogger("networking.gates")

GATE_TYPE = "gate"
#: Скільки годин мовчати між пінгами в Telegram про застояні ворота.
NUDGE_EVERY_HOURS = 6
#: З якого віку ворота вважаються застояними.
STALE_AFTER_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def age_hours(task: Task) -> float:
    created = _aware(task.created_at) or _now()
    return (_now() - created).total_seconds() / 3600


def match_goal(db: Session, hint: str | None) -> Goal | None:
    """Знаходить ціль за підрядком у назві — щоб не тягати id у скрипт."""
    needle = (hint or "").strip().lower()
    if not needle:
        return None
    goals = list(db.scalars(select(Goal)))
    exact = [g for g in goals if g.title.lower() == needle]
    if exact:
        return exact[0]
    partial = [g for g in goals if needle in g.title.lower()]
    if partial:
        # Серед збігів віддаємо перевагу активній і пріоритетнішій.
        partial.sort(key=lambda g: (g.status not in ACTIVE_STATUSES, -g.priority))
        return partial[0]
    return None


def create_gate(
    db: Session,
    *,
    text: str,
    goal_hint: str | None = None,
    goal_id: int | None = None,
    project: str | None = None,
) -> Task:
    goal = db.get(Goal, goal_id) if goal_id else match_goal(db, goal_hint)
    task = Task(
        uid=new_uid(),
        title=" ".join(text.split())[:300],
        item_type=GATE_TYPE,
        source="gate",
        status=TaskStatus.todo,
        project=(project or None),
        goal_id=goal.id if goal else None,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def open_gates(db: Session) -> list[Task]:
    """Відкриті ворота, найстаріші — першими."""
    rows = list(
        db.scalars(
            select(Task).where(
                Task.deleted_at.is_(None),
                Task.item_type == GATE_TYPE,
                Task.status != TaskStatus.done,
            )
        )
    )
    rows.sort(key=lambda t: _aware(t.created_at) or _now())
    return rows


def stale_gates(db: Session, hours: int = STALE_AFTER_HOURS) -> list[Task]:
    return [t for t in open_gates(db) if age_hours(t) >= hours]


def close_gate(db: Session, uid: str) -> Task | None:
    task = db.scalars(
        select(Task).where(Task.uid == uid, Task.item_type == GATE_TYPE)
    ).first()
    if task is None:
        return None
    task.status = TaskStatus.done
    db.commit()
    db.refresh(task)
    return task


def goal_title(db: Session, task: Task) -> str | None:
    if not task.goal_id:
        return None
    goal = db.get(Goal, task.goal_id)
    return goal.title if goal else None


def gate_line(db: Session, task: Task) -> str:
    """Один рядок про ворота: скільки висить і що блокує."""
    days = int(age_hours(task) // 24)
    age = f"{days} дн" if days else f"{int(age_hours(task))} год"
    title = goal_title(db, task)
    tail = f" Блокує: {title}." if title else ""
    return f"{task.title} — висить {age}.{tail}"


def _last_nudge_at(db: Session) -> datetime | None:
    row = db.scalars(
        select(Checkin)
        .where(Checkin.source == "gate")
        .order_by(Checkin.asked_at.desc())
        .limit(1)
    ).first()
    return _aware(row.asked_at) if row else None


def nudge(db: Session) -> str | None:
    """Текст пінга про застояні ворота, або ``None``, якщо мовчимо.

    Мовчимо у двох випадках: застояних воріт немає або нагадували недавно.
    Пінг раз на кілька годин — це нагадування; частіше — фон, який перестають
    помічати.
    """
    stale = stale_gates(db)
    if not stale:
        return None
    # Уночі мовчимо (тиша з налаштувань «Нагадування»), і вікно 6 год не
    # витрачається: пінг прийде першим після ранку.
    from app.modules.tasks import nudges as task_nudges

    if task_nudges.in_quiet(task_nudges.get_settings(db)):
        return None
    last = _last_nudge_at(db)
    if last and _now() - last < timedelta(hours=NUDGE_EVERY_HOURS):
        return None
    # Пінг воріт — у денну стелю разом із блокерами й пропозиціями задач.
    if not task_nudges.allow_proactive(db):
        return None

    lines = ["Ворота, які висять понад добу:"]
    for task in stale[:5]:
        lines.append(f"- {gate_line(db, task)}")
    if len(stale) > 5:
        lines.append(f"...і ще {len(stale) - 5}.")
    text = "\n".join(lines)

    db.add(
        Checkin(
            asked_at=_now(),
            answered_at=_now(),
            source="gate",
            kind="progress",
            summary=f"пінг про {len(stale)} воріт",
            question=text,
        )
    )
    db.commit()
    return text
