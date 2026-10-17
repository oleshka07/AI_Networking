"""Background scheduler for the daily job.

Enabled with SCHEDULER_ENABLED=true. Uses APScheduler's BackgroundScheduler,
which runs in a daemon thread inside the web process — fine for a single-user
internal tool. For multi-process deployments, run the daily job from a cron /
systemd timer calling `python -m app.run_daily` instead.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from .daily import run_daily_job

logger = logging.getLogger("networking.scheduler")

_scheduler = None


def start() -> None:
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED is false).")
        return
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:  # pragma: no cover
        logger.warning("APScheduler unavailable: %s", exc)
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        run_daily_job,
        CronTrigger(hour=settings.daily_run_hour, minute=0),
        id="daily_job",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Вечірнє зведення дня: ПК шле зріз о 20:00 і 22:00, ми звітуємо о 22:10.
    _scheduler.add_job(
        run_daily_digest,
        CronTrigger(hour=settings.evening_digest_hour,
                    minute=settings.evening_digest_minute),
        id="evening_digest",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Коуч по цілях: одна щогодинна задача, рішення — за налаштуваннями в БД.
    from app.modules.coach.jobs import run_coach_tick

    _scheduler.add_job(
        run_coach_tick,
        "cron",
        minute=5,
        id="coach_tick",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Пошта: раз на годину тягнемо нове, класифікуємо і пишемо чернетки.
    from app.modules.inbox.service import run_sync as run_inbox_sync

    _scheduler.add_job(
        run_inbox_sync,
        "cron",
        minute=25,
        id="inbox_sync",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Задачі в Google Календарі: :45, щоб не збігтися з коучем (:05) і поштою (:25).
    from app.modules.tasks.gsync import run_sync as run_gtasks_sync

    _scheduler.add_job(
        run_gtasks_sync,
        "cron",
        minute=45,
        id="gtasks_sync",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Нагадування про задачі: блокери щогодини, підготовка до зустрічей — у свою
    # годину з task_settings. :00, бо підготовка обіцяна «о 18:00».
    from app.modules.tasks.nudges import run_tick as run_task_nudges

    _scheduler.add_job(
        run_task_nudges,
        "cron",
        minute=0,
        id="task_nudges",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Нічна черга («Від Claude», «За ніч»): виходить у ранковому плані, а якщо
    # плану того дня немає — сама, щойно скінчилась тиша. Кожні 15 хв, бо кінець
    # тиші (07:30) не на початку години.
    from app.modules.tasks.quiet import release_pending

    _scheduler.add_job(
        release_pending,
        "cron",
        minute="*/15",
        id="quiet_release",
        replace_existing=True,
        misfire_grace_time=900,
    )
    # Памʼять про людей: досьє й факти переписуються самі, коли назбиралось нове.
    from app.modules.insights.service import run_consolidation

    _scheduler.add_job(
        run_consolidation,
        "cron",
        minute=35,
        id="memory_consolidation",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Соцмережі: раз на добу, пачкою; кожен контакт — не частіше ніж раз на N днів.
    from app.modules.integrations.social.monitor import run_monitor as run_social_monitor

    _scheduler.add_job(
        run_social_monitor,
        CronTrigger(hour=6, minute=15),
        id="social_monitor",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    if settings.meeting_brief_enabled:
        from .briefs import run_brief_check

        _scheduler.add_job(
            run_brief_check,
            "interval",
            minutes=10,
            id="meeting_briefs",
            replace_existing=True,
            misfire_grace_time=300,
        )
    if settings.checkin_enabled:
        from .reviews import run_midday_checkin

        _scheduler.add_job(
            run_midday_checkin,
            CronTrigger(hour=settings.checkin_hour, minute=0),
            id="midday_checkin",
            replace_existing=True,
            misfire_grace_time=3600,
        )
    if settings.weekly_review_enabled:
        from .reviews import run_weekly_review

        _scheduler.add_job(
            run_weekly_review,
            CronTrigger(day_of_week="mon", hour=settings.weekly_review_hour, minute=0),
            id="weekly_review",
            replace_existing=True,
            misfire_grace_time=3600,
        )
    if settings.reflection_enabled:
        from .reviews import run_weekly_reflection

        _scheduler.add_job(
            run_weekly_reflection,
            CronTrigger(day_of_week="thu", hour=settings.reflection_hour, minute=0),
            id="weekly_reflection",
            replace_existing=True,
            misfire_grace_time=3600,
        )
    _scheduler.start()
    logger.info(
        "Scheduler started; daily job at %02d:00 server time.",
        settings.daily_run_hour,
    )


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_daily_digest() -> None:
    """Коротке зведення дня в Telegram (о 22:10 за часом сервера; у тишу — ні)."""
    from app.core.database import SessionLocal
    from app.modules.tasks import nudges
    from app.modules.timereport import service as timereport

    try:
        with SessionLocal() as db:
            if nudges.quiet_now(db):
                logger.info("evening digest skipped: quiet hours")
                return
            sent = timereport.send_daily(db)
        logger.info("evening digest sent=%s", sent)
    except Exception as exc:
        logger.warning("evening digest failed: %s", exc)
