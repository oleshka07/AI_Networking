"""The daily job: refresh warmth, sync Google, and send a digest.

Runs on a schedule (see scheduler.py) and can be triggered manually via the
API. Keeps everything in one place so it's easy to reason about.
"""

from __future__ import annotations

import logging

from app import crud, dashboard

from . import telegram
from app.core.config import settings
from app.core.database import SessionLocal
from app.integrations import chater, google

logger = logging.getLogger("networking.daily")


def _headline_changed(old: str | None, new: str | None) -> bool:
    """True when a social headline/title meaningfully changed (ignoring case
    and whitespace). Used to flag profile changes as life events."""
    if not old or not new or len(new.strip()) < 3:
        return False
    norm = lambda s: " ".join(s.casefold().split())  # noqa: E731
    return norm(old) != norm(new)


def build_digest_html(data) -> str:
    s = data.stats

    def row(title, items):
        if not items:
            return f"<p style='color:#888'>{title}: nothing.</p>"
        lis = "".join(items)
        return (
            f"<h3 style='margin:18px 0 6px'>{title}</h3>"
            f"<ul style='margin:0;padding-left:18px'>{lis}</ul>"
        )

    suggestions = [
        f"<li><b>{sg.contact.full_name}</b> — {sg.reason}</li>"
        for sg in data.suggestions
    ]
    upcoming = [
        f"<li><b>{u.contact.full_name}</b> — {u.label} "
        f"({'today' if u.days_away == 0 else f'in {u.days_away}d'})</li>"
        for u in data.upcoming
    ]
    events = [
        f"<li><b>{e.title}</b> — {e.event_type}</li>" for e in data.pending_events
    ]

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;
            margin:0 auto;color:#1a1a1a">
  <h2 style="margin-bottom:4px">Your networking digest</h2>
  <p style="color:#666;margin-top:0">
    {s.total_contacts} contacts · {s.due_now} due now · avg warmth {s.average_warmth}
  </p>
  {row("Reach out today", suggestions)}
  {row("Upcoming dates", upcoming)}
  {row("New life events", events)}
  <p style="color:#aaa;font-size:12px;margin-top:24px">
    Sent by your Networking AI service.
  </p>
</div>"""


def run_daily_job(send_digest: bool = True) -> dict:
    """Returns a summary dict describing what happened."""
    db = SessionLocal()
    summary: dict = {"warmth_refreshed": 0, "sync": None, "digest_sent": False}
    try:
        summary["warmth_refreshed"] = crud.refresh_all_warmth(db)

        # Self-healing: clean up any duplicate Chater-imported contacts.
        try:
            summary["duplicates_removed"] = chater.dedupe(db)
        except Exception as exc:  # pragma: no cover
            logger.warning("daily dedupe failed: %s", exc)

        g_status = google.status(db)
        if g_status["connected"]:
            report = google.sync(db)
            summary["sync"] = report.as_dict()

        # Social sweep: re-scrape socials of the most-due contacts so fresh
        # life events become outreach hooks in the digest (best effort).
        try:
            summary["social_swept"] = sweep_social(db)
        except Exception as exc:  # pragma: no cover
            logger.warning("social sweep failed: %s", exc)

        # Keep the semantic-search index fresh (best effort).
        try:
            from app.modules.search import service as search_service

            summary["embedded"] = search_service.embed_pending(db, limit=200)
        except Exception as exc:  # pragma: no cover
            logger.warning("embed_pending failed: %s", exc)

        data = dashboard.build_dashboard(db)
        summary["due_now"] = data.stats.due_now

        if send_digest:
            # Email digest (via Gmail, if connected).
            recipient = settings.digest_email_to or (
                g_status.get("account_email") if g_status["connected"] else None
            )
            if recipient and g_status["connected"]:
                html = build_digest_html(data)
                summary["digest_sent"] = google.send_email(
                    db, recipient, "Your networking digest", html
                )
                summary["digest_to"] = recipient
            else:
                summary["digest_skipped"] = (
                    "No recipient or Google not connected (needed to send mail)."
                )
            # Telegram digest (independent of email). Silence-aware: an
            # empty digest trains the user to ignore the bot — skip it.
            nothing_to_say = (
                not data.suggestions
                and not data.upcoming
                and not data.pending_events
            )
            # Ранковий план задач — у тому самому повідомленні, без окремого пушу.
            plan = None
            try:
                from app.modules.tasks import nudges

                plan = nudges.plan_block(db)
            except Exception as exc:  # pragma: no cover - план не валить прогін
                logger.warning("morning plan failed: %s", exc)
            # Що чекало кінця тиші: «Від Claude» і «За ніч» — першими блоками.
            from app.modules.tasks import quiet

            held = quiet.has_pending(db)
            quiet_digest = settings.digest_quiet_when_empty and nothing_to_say
            if quiet_digest and not plan and not held:
                summary["telegram_skipped"] = "quiet (nothing actionable)"
            elif settings.telegram_configured and settings.telegram_chat_id:
                parts, rows = [], []
                owner_text = night_text = None
                stamp = quiet.utcnow()
                if held:
                    owner_text, night_text, night_rows = quiet.morning_blocks(db, now=stamp)
                    summary["held_released"] = True
                if plan:
                    plan_text, plan_tasks = plan
                    parts.append(plan_text)
                    rows += nudges.task_keyboard_rows(plan_tasks)
                    summary["plan_tasks"] = len(plan_tasks)
                if night_text:
                    parts.append(night_text)
                    rows += night_rows
                if not quiet_digest:
                    parts.append(telegram.digest_text(data))
                    rows.append([{"text": "🚀 Почати обхід", "callback_data": "q:start"}])
                summary["telegram_sent"] = quiet.send_blocks(telegram, parts, rows, head=owner_text)
                if held and not summary["telegram_sent"]:
                    quiet.restore(db, stamp)  # Telegram не прийняв — черга чекає наступної спроби
                    summary["held_released"] = False
        logger.info("Daily job finished: %s", summary)
    finally:
        db.close()
    return summary


def sweep_social(db) -> int:
    """Re-scrape social links of the N most-due contacts; record detected
    life events (deduped by title) so they surface as outreach hooks."""
    from app import ai, crud, models, social, warmth
    from sqlalchemy import select

    if not (settings.scraper_provider and settings.scraper_api_key):
        return 0
    n = settings.social_sweep_daily
    if n <= 0:
        return 0

    contacts = [
        c
        for c in db.scalars(select(models.Contact)).unique()
        if not c.do_not_contact
        and warmth.is_due(c)
        and (c.instagram_url or c.linkedin_url or c.facebook_url or c.twitter_url)
    ]
    contacts.sort(key=warmth.overdue_ratio, reverse=True)

    swept = 0
    for c in contacts[:n]:
        url = c.instagram_url or c.linkedin_url or c.facebook_url or c.twitter_url
        try:
            data = social.fetch_snapshot(url)
            # Capture the previous headline BEFORE overwriting, so we can
            # detect profile changes (new job title, bio, handle) — Mesh's
            # "career moves surface automatically".
            from app.modules.integrations.social.models import SocialSnapshot

            prev = db.scalar(
                select(SocialSnapshot).where(
                    SocialSnapshot.contact_id == c.id,
                    SocialSnapshot.url == data.url,
                )
            )
            old_title = (prev.title or "").strip() if prev and prev.title else None
            snapshot = crud.upsert_snapshot(
                db,
                c,
                platform=data.platform,
                url=data.url,
                title=data.title,
                raw_text=data.raw_text,
            )
            db.refresh(c)
            known_titles = {e.title.casefold() for e in c.life_events}
            new_title = (data.title or "").strip()
            if _headline_changed(old_title, new_title):
                change = f"Оновлення профілю: {new_title}"[:200]
                if change.casefold() not in known_titles:
                    crud.add_life_event(
                        db,
                        c,
                        event_type="profile_change",
                        title=change,
                        description=f"Було: {old_title}",
                        source=data.platform,
                    )
                    known_titles.add(change.casefold())
            for ev in ai.detect_life_events(c, snapshot):
                title = (ev.get("title") or "").strip()
                if not title or title.casefold() in known_titles:
                    continue
                crud.add_life_event(
                    db,
                    c,
                    event_type=ev.get("event_type", "update"),
                    title=title,
                    description=ev.get("description"),
                    source=data.platform,
                )
            swept += 1
        except Exception as exc:  # pragma: no cover
            logger.warning("sweep failed for %s: %s", c.full_name, exc)
    return swept
