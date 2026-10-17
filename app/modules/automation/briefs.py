"""Pre-meeting briefs: a contact dossier in Telegram before each meeting.

Every ~10 minutes the scheduler asks Google Calendar for events starting in
roughly an hour, matches attendees to contacts by email, and sends a short
brief (who, warmth, facts, last interactions) to the user's Telegram.
Pattern proven by Clay/Cloze — the single most-loved personal-CRM feature.
"""

from __future__ import annotations

import html
import logging

from app.core.config import settings
from app.core.database import SessionLocal

logger = logging.getLogger("networking.briefs")

# Event ids we've already briefed (process-lifetime; a restart may re-brief
# an event once — harmless).
_briefed: set[str] = set()

#: У години тиші бриф приходить лише перед зустріччю, що починається раніше
#: за цей час (хв від півночі): інакше перед ранковою зустріччю його не буде.
EARLY_MEETING_BEFORE = 8 * 60 + 30


def allowed_in_quiet(event: dict) -> bool:
    """Ранню зустріч (до 08:30 за місцевим часом) тиша не глушить."""
    start = event["start"]
    local = start.astimezone() if start.tzinfo else start
    return local.hour * 60 + local.minute < EARLY_MEETING_BEFORE


def _esc(s: str | None) -> str:
    return html.escape(s or "")


def format_brief(event: dict, contacts: list) -> str:
    when = event["start"].strftime("%H:%M")
    lines = [f"📅 <b>Зустріч о {when}: {_esc(event['summary'])}</b>"]
    if event.get("meet_link"):
        lines.append(_esc(event["meet_link"]))
    for c in contacts:
        lines.append("")
        head = f"👤 <b>{_esc(c.full_name)}</b>"
        role = " · ".join(filter(None, [c.position, c.company]))
        if role:
            head += f" — {_esc(role)}"
        head += f" · {_esc(c.warmth_status)} {round(c.warmth_score)}"
        lines.append(head)
        facts = [f for f in getattr(c, "facts", []) if f.is_current][:3]
        if facts:
            lines.append("💡 " + "; ".join(_esc(f.value) for f in facts))
        recent = [i for i in c.interactions if i.summary][:2]
        for i in recent:
            lines.append(
                f"🕓 {i.occurred_at.strftime('%d.%m')}: {_esc(i.summary[:120])}"
            )
        events = [e for e in c.life_events if e.status.value == "new"][:2]
        for e in events:
            lines.append(f"🎉 {_esc(e.title)}")
    return "\n".join(lines)


def format_reminder(event: dict) -> str:
    """A simple time reminder for a calendar event without a matched contact
    (Chater-style: '⏰ 09:00–11:00 📅 Title · Через ~1 годину')."""
    start = event["start"].strftime("%H:%M")
    end = event.get("end")
    when = f"{start}–{end.strftime('%H:%M')}" if end else start
    lines = [f"⏰ <b>{when}</b>", f"📅 {_esc(event.get('summary'))}"]
    if event.get("meet_link"):
        lines.append(_esc(event["meet_link"]))
    lines.append("\nЧерез ~1 годину")
    return "\n".join(lines)


def run_brief_check() -> int:
    """Send briefs for events starting ~lead minutes from now. Returns how
    many briefs were sent. Never raises."""
    if not settings.meeting_brief_enabled:
        return 0
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return 0

    from app.integrations import google
    from app.models import Contact
    from app.modules.automation import telegram
    from sqlalchemy import select

    from app.modules.tasks import nudges

    lead = settings.meeting_brief_lead_minutes
    sent = 0
    with SessionLocal() as db:
        quiet = nudges.quiet_now(db)
        try:
            events = google.upcoming_events(
                db, minutes_min=max(lead - 15, 5), minutes_max=lead + 15
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("upcoming_events failed: %s", exc)
            return 0
        for event in events:
            if not event["id"] or event["id"] in _briefed:
                continue
            if quiet and not allowed_in_quiet(event):
                continue
            emails = [e for e in event.get("attendee_emails", []) if e]
            contacts = (
                list(
                    db.scalars(
                        select(Contact).where(Contact.email.in_(emails))
                    ).unique()
                )
                if emails
                else []
            )
            # Meeting with a known contact → rich brief; any other calendar
            # event (e.g. a personal task) → a simple time reminder.
            msg = format_brief(event, contacts) if contacts else format_reminder(event)
            if telegram.send_message(msg):
                _briefed.add(event["id"])
                sent += 1
                # Bound the memory of a long-lived process.
                if len(_briefed) > 200:
                    for eid in list(_briefed)[:100]:
                        _briefed.discard(eid)
    return sent
