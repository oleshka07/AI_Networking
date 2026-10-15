"""Розклад коуча: питання дня, нагадування, тижневий підсумок.

Планувальник будить одну задачу щогодини, а вже вона звіряється з
налаштуваннями в БД. Так години можна міняти на сторінці без перезапуску
контейнера — і без трьох окремих cron-задач, які довелося б перереєстровувати.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from app.core.config import settings
from app.core.database import SessionLocal

from . import service

logger = logging.getLogger("networking.coach")


def _esc(s: str) -> str:
    return html.escape(str(s))


def _configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_question(*, force: bool = False) -> bool:
    """Задає питання дня. ``force`` — на вимогу, повз перевірку «вже питали»."""
    if not _configured():
        return False
    from app.modules.automation import telegram

    with SessionLocal() as db:
        opened = service.open_question(db, force=force)
        if opened is None:
            return False
        _, question = opened
    if not question:
        return False  # питання допише і надішле Claude Code у фоні
    return telegram.send_message(_esc(question))


def send_nudge() -> bool:
    """Колюче нагадування, якщо питання дня висить без відповіді."""
    if not _configured():
        return False
    from app.modules.automation import telegram
    from app.modules.goals.models import Goal
    from app.modules.insights import ai

    with SessionLocal() as db:
        checkin = service.pending_checkin(db)
        if checkin is None or checkin.nudges:
            return False
        goal = db.get(Goal, checkin.goal_id)
        if goal is None:
            return False
        from app.modules.aijobs import brain

        # На нагадування окремий запуск Claude не витрачаємо — є готовий текст.
        text = (ai.coach_nudge(goal.title) if brain.mode() == "api" else None) or (
            f"Питання про «{goal.title}» висить без відповіді. "
            "Ігнор — теж відповідь, і вона зрозуміла."
        )
        service.mark_nudged(db, checkin)
    return telegram.send_message(_esc(text))


def send_task_reminders() -> bool:
    """Нагадує про задачу, якій настав час. За раз — рівно одну.

    Кілька відкритих питань одночасно зробили б неоднозначним питання «на що
    саме він відповів», тож наступна задача почекає до наступної години.
    """
    if not _configured():
        return False
    from app.modules.automation import telegram
    from app.modules.tasks import service as tasks_service

    with SessionLocal() as db:
        if service.pending_checkin(db) is not None:
            return False
        due = tasks_service.due_reminder_tasks(db)
        if not due:
            return False
        task = due[0]
        who = f" · відповідальний: {task.counterpart}" if task.counterpart else ""
        extra = f"\nЩе чекають: {len(due) - 1}" if len(due) > 1 else ""
        text = f"Нагадування: {_esc(task.title)}{_esc(who)}\nЩо по ній?{extra}"
        service.open_task_question(db, task, text)
    return telegram.send_message(text)


def week_block(db) -> tuple[str, bool]:
    """Цифри тижня простим текстом (без HTML) і ознака «тиждень без руху»."""
    data = service.board(db)
    changes = service.week_changes(db)
    entries = service.week_entries(db)

    def line(label: str, b: dict) -> str:
        parts = [f"{k}: {v}" for k, v in b["counts"].items() if v]
        head = f"{label}: {b['total']} цілей, done {b['done']} ({b['done_pct']}%)"
        return head + (f"\n  {', '.join(parts)}" if parts else "")

    lines = [f"Тиждень до {datetime.now():%d.%m.%Y}"]
    if data["theme"]:
        lines.append(data["theme"])
    lines += ["", line("Pers", data["pers"]), line("Work", data["work"]),
              line("Разом", data["all"]), ""]

    if changes:
        lines.append("Зміни статусів за тиждень:")
        for c in changes:
            title = c.goal.title if c.goal else f"ціль #{c.goal_id}"
            lines.append(f"- {title}: {c.status_before} -> {c.status_after}")
    else:
        lines.append("Статуси за тиждень не змінилися.")

    if entries:
        lines += ["", "Записи зʼявилися по цілях:"]
        seen = set()
        for e in entries:
            title = e.goal.title if e.goal else f"ціль #{e.goal_id}"
            if title in seen:
                continue
            seen.add(title)
            lines.append(f"- {title}")
    else:
        lines += ["", "Записів по жодній цілі за тиждень немає."]

    from app.modules.gates import service as gates_service

    gates = gates_service.stale_gates(db)
    if gates:
        lines += ["", "Ворота, що висять понад добу:"]
        for g in gates[:5]:
            lines.append(f"- {gates_service.gate_line(db, g)}")

    stalled = not changes and not entries
    return "\n".join(lines), stalled


def default_verdict(stalled: bool) -> str:
    return (
        "Тиждень злитий: жодного руху по жодній цілі. У понеділок обери одну "
        "ціль і зроби по ній перший крок."
        if stalled
        else "У понеділок візьми одну ціль і зрушь її."
    )


def render_weekly(block: str, verdict: str) -> str:
    """HTML для Telegram: перший рядок жирним, решта — екранований текст."""
    lines = _esc(block).split("\n")
    if lines:
        lines[0] = f"<b>{lines[0]}</b>"
    return "\n".join(lines) + ("\n\n" + _esc(verdict) if verdict else "")


def weekly_text(db) -> str:
    """Тижневий зріз з вердиктом — одразу, моделлю через ключ (для /week)."""
    from app.modules.insights import ai

    block, stalled = week_block(db)
    verdict = ai.coach_week_verdict(block, stalled) or default_verdict(stalled)
    return render_weekly(block, verdict)


def send_weekly() -> bool:
    if not _configured():
        return False
    from app.modules.aijobs import brain
    from app.modules.automation import telegram

    with SessionLocal() as db:
        block, stalled = week_block(db)
        verdict = brain.weekly_verdict(db, block, stalled)
    if verdict is None:
        return False  # підсумок складе і надішле Claude Code у фоні
    return telegram.send_message(render_weekly(block, verdict or default_verdict(stalled)))


def run_coach_tick() -> None:
    """Щогодинна задача: сама вирішує, чи зараз час діяти."""
    with SessionLocal() as db:
        cfg = service.get_settings(db)
        enabled = cfg.enabled
        ask_hour, nudge_hour = cfg.ask_hour, cfg.nudge_hour
        weekly_weekday, weekly_hour = cfg.weekly_weekday, cfg.weekly_hour
    if not enabled:
        return

    now = datetime.now()
    # Нічна тиша з налаштувань «Нагадування» діє і на коуча (рішення власника
    # 15.10): уночі не питаємо; задачі-нагадування дочекаються ранку самі.
    from app.modules.tasks import nudges as task_nudges

    with SessionLocal() as db:
        if task_nudges.in_quiet(task_nudges.get_settings(db), now):
            return
    try:
        if now.weekday() == weekly_weekday and now.hour == weekly_hour:
            send_weekly()
        if now.hour == ask_hour:
            send_question()
        if now.hour == nudge_hour:
            send_nudge()
        send_task_reminders()
    except Exception as exc:  # pragma: no cover - фонова задача не має падати
        logger.warning("coach tick failed: %s", exc)
