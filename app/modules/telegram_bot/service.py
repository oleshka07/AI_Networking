"""Telegram Business proxy — draft lifecycle.

Flow (ported from Chater, now powered by our brain):
  incoming DM → match/create contact → log interaction → Claude draft
  (tone + facts + history) → TelegramDraft(pending) → admin preview.
  Approve → send via business connection (as the user) → log outgoing
  interaction → warmth refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import warmth
from app.core.config import settings
from app.modules.contacts.models import Contact, Relationship, Tag
from app.modules.insights import ai
from app.modules.interactions import service as interactions_service
from app.modules.interactions.models import Channel, Direction

from .models import BusinessConnection, DraftKind, DraftStatus, TelegramDraft

logger = logging.getLogger("networking.tgbot")


# ── Business connections ─────────────────────────────────────────────────────


def upsert_connection(
    db: Session, connection_id: str, user_chat_id: int | None, is_enabled: bool
) -> BusinessConnection:
    conn = db.scalar(
        select(BusinessConnection).where(
            BusinessConnection.connection_id == connection_id
        )
    )
    if conn is None:
        conn = BusinessConnection(connection_id=connection_id)
        db.add(conn)
    conn.user_chat_id = user_chat_id
    conn.is_enabled = is_enabled
    db.commit()
    return conn


# ── Contact matching ─────────────────────────────────────────────────────────


def find_or_create_contact(
    db: Session,
    chat_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
) -> tuple[Contact, bool]:
    """Match an incoming DM to a contact by chat id, then username; create a
    new contact otherwise. Backfills telegram_chat_id so the next match is
    O(1) by id. Returns (contact, is_new)."""
    contact = db.scalar(select(Contact).where(Contact.telegram_chat_id == chat_id))
    if contact:
        _backfill_identity(db, contact, first_name, last_name, username)
        return contact, False

    if username:
        contact = db.scalar(
            select(Contact).where(Contact.telegram.ilike(f"%{username}%"))
        )
        if contact:
            contact.telegram_chat_id = chat_id
            _backfill_identity(db, contact, first_name, last_name, username)
            return contact, False

    contact = Contact(
        first_name=first_name or "Unknown",
        last_name=last_name,
        telegram=f"@{username}" if username else None,
        telegram_chat_id=chat_id,
        relationship_type=Relationship.acquaintance,
    )
    tag = db.scalar(select(Tag).where(Tag.name == "telegram"))
    if tag is None:
        tag = Tag(name="telegram")
        db.add(tag)
        db.flush()
    contact.tags = [tag]
    db.add(contact)
    db.commit()
    db.refresh(contact)
    logger.info("New contact from Telegram DM: %s (%s)", contact.full_name, chat_id)
    return contact, True


def _backfill_identity(
    db: Session,
    contact: Contact,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> None:
    """Fill in missing username/name from a live Telegram update — makes
    export-imported contacts clickable the moment they write to you."""
    changed = False
    if username and not (contact.telegram or "").strip():
        contact.telegram = f"@{username}"
        changed = True
    if first_name and contact.first_name in ("Unknown", "", None):
        contact.first_name = first_name
        changed = True
    if last_name and not contact.last_name:
        contact.last_name = last_name
        changed = True
    if changed:
        db.commit()


def _birthdate_from_getchat(info: dict):
    """Bot API birthdate object {day, month, year?} -> date (year defaults
    to a leap-safe placeholder when the person hid the year)."""
    from datetime import date

    bd = info.get("birthdate") or {}
    day, month = bd.get("day"), bd.get("month")
    if not (day and month):
        return None
    year = bd.get("year") or 1904
    try:
        return date(year, month, day)
    except ValueError:
        return None


def enrich_from_telegram(db: Session, client, contact: Contact) -> bool:
    """Pull username / birthday / bio from Telegram (getChat) into the
    contact. Returns True if anything was saved. Best-effort: getChat may
    not see a user the bot has never interacted with."""
    if not contact.telegram_chat_id:
        return False
    info = client.get_chat(contact.telegram_chat_id)
    if not isinstance(info, dict):
        return False
    changed = False
    username = info.get("username")
    if username and not (contact.telegram or "").strip():
        contact.telegram = f"@{username}"
        changed = True
    if not contact.birth_date:
        bd = _birthdate_from_getchat(info)
        if bd:
            contact.birth_date = bd
            changed = True
    bio = (info.get("bio") or "").strip()
    if bio and not (contact.notes or "").strip():
        contact.notes = bio
        changed = True
    if changed:
        db.commit()
        db.refresh(contact)
    return changed


def pending_enrichment(db: Session, limit: int = 25) -> list[Contact]:
    """Contacts reachable in Telegram but still without a username."""
    rows = db.scalars(
        select(Contact).where(
            Contact.telegram_chat_id.is_not(None),
            Contact.do_not_contact.is_(False),
        )
    ).unique()
    out = []
    for c in rows:
        if not (c.telegram or "").strip():
            out.append(c)
        if len(out) >= limit:
            break
    return out


# ── Conversation closers ─────────────────────────────────────────────────────

# Words/emoji that, alone, mean "conversation over — no reply needed".
_CLOSER_VOCAB = {
    "ок", "окей", "оки", "ok", "okay", "добре", "гуд", "good",
    "дякую", "дяки", "дяка", "спасібо", "спасибо", "спс", "пасіб", "пасибі",
    "thanks", "thank", "you", "thx", "ty", "мерсі",
    "пока", "бувай", "бувайте", "бб", "bye", "goodbye", "чао", "чмок",
    "все", "всьо", "ясно", "зрозуміло", "зрозумів", "зрозуміла", "понял", "поняла",
    "супер", "клас", "круто", "кул", "cool", "nice", "великий", "величезний",
    "давай", "домовились", "домовилися", "згода", "заметано",
    "добраніч", "надобраніч", "гарного", "дня", "вечора", "вихідних",
    "до", "зустрічі", "звязку", "зв'язку", "побачення", "завтра",
    "взаємно", "навзаєм", "тобі", "вам", "і", "теж", "також",
    "👍", "👌", "🙏", "❤️", "🤝", "😉", "🔥", "💪", "✌️", "🫡", "😊", "))", ")))",
}


def is_closer(text: str) -> bool:
    """True when the message is just a polite conversation-ender (thanks,
    bye, ok) that needs no reply — drafting one creates endless loops."""
    import re

    cleaned = re.sub(r"[.,!?;:()\-–—]", " ", text.casefold())
    tokens = [tok for tok in cleaned.split() if tok]
    if not tokens or len(tokens) > 5:
        return False
    return all(tok in _CLOSER_VOCAB for tok in tokens)


# ── Draft lifecycle ──────────────────────────────────────────────────────────


def log_incoming(
    db: Session,
    *,
    chat_id: int,
    text: str,
    message_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
) -> tuple[Contact, bool]:
    """Контакт + вхідна взаємодія в історії (ідемпотентно за id повідомлення).

    Окремо від чернеток, бо для чатів, які веде ALiSiO, нам потрібна лише
    памʼять, без жодної відповіді.
    """
    contact, is_new = find_or_create_contact(
        db, chat_id, first_name, last_name, username
    )
    external_id = f"tgb:{chat_id}:{message_id}"
    if not interactions_service.interaction_exists(db, contact.id, external_id):
        interactions_service.add_synced_interaction(
            db,
            contact,
            occurred_at=datetime.now(timezone.utc),
            channel=Channel.message,
            direction=Direction.inbound,
            summary=text[:300],
            source="telegram",
            external_id=external_id,
        )
        db.refresh(contact)
        warmth.refresh(contact)
        db.commit()
    db.refresh(contact)
    return contact, is_new


def handle_incoming(
    db: Session,
    *,
    chat_id: int,
    text: str,
    message_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    business_connection_id: str | None,
) -> tuple[TelegramDraft | None, Contact, bool, str]:
    """Process an incoming business DM end-to-end (steps 1–4 above).

    Returns (draft, contact, is_new, status). status is one of:
    "drafted" (a reply is ready), "closer"/"skip" (logged, no reply needed),
    "paused" (auto-reply off for this contact — logged; ``draft`` is a held,
    text-less draft the user can fill on demand)."""
    contact, is_new = log_incoming(
        db, chat_id=chat_id, text=text, message_id=message_id,
        first_name=first_name, last_name=last_name, username=username,
    )

    # Conversation closers ("ок, дякую", "все, пока") need no reply — a
    # drafted answer to a goodbye creates infinite politeness loops.
    if is_closer(text):
        return None, contact, is_new, "closer"

    # Auto-reply paused for this person: keep the log/context, but don't spend
    # an AI call drafting. The caller decides whether to surface anything —
    # it only pings when the message is actionable (a meeting or a task).
    if contact.auto_reply_paused:
        return None, contact, is_new, "paused"

    draft = TelegramDraft(
        contact_id=contact.id,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
        kind=DraftKind.reply,
        incoming_text=text,
        draft_text="",
        status=DraftStatus.pending,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)

    # Хто пише чернетку — модель через ключ (зараз) чи Claude Code через MCP
    # (за хвилину, у фоні). У другому випадку картка йде без тексту зі
    # статусом "drafting", а текст допише crm_fill_reply_draft.
    from app.modules.aijobs import brain

    draft_text = brain.draft_reply(db, draft)
    if draft_text is None and brain.deferred():
        return draft, contact, is_new, "drafting"
    draft_text = draft_text or ""
    if draft_text.strip() == "[SKIP]":
        # The model judged this a conversation-ender in context.
        db.delete(draft)
        db.commit()
        return None, contact, is_new, "skip"
    draft.draft_text = draft_text
    db.commit()
    db.refresh(draft)
    return draft, contact, is_new, "drafted"


def create_held_reply_draft(
    db: Session, contact: Contact, *, chat_id: int, text: str,
    business_connection_id: str | None,
) -> TelegramDraft:
    """A text-less reply draft for a paused contact — created only when the
    caller surfaces an actionable heads-up, so the ✍️ button can compose later."""
    d = TelegramDraft(
        contact_id=contact.id,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
        kind=DraftKind.reply,
        incoming_text=text,
        draft_text="",
        status=DraftStatus.pending,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def get_draft(db: Session, draft_id: int) -> TelegramDraft | None:
    return db.get(TelegramDraft, draft_id)


def find_pending_by_admin_message(
    db: Session, admin_message_id: int
) -> TelegramDraft | None:
    return db.scalar(
        select(TelegramDraft).where(
            TelegramDraft.admin_message_id == admin_message_id,
            TelegramDraft.status == DraftStatus.pending,
        )
    )


def set_admin_message(db: Session, draft: TelegramDraft, message_id: int) -> None:
    draft.admin_message_id = message_id
    db.commit()


def update_draft_text(db: Session, draft: TelegramDraft, text: str) -> None:
    draft.draft_text = text
    db.commit()


def fill_reply_draft(db: Session, draft: TelegramDraft, text: str) -> str:
    """Дописує текст у чернетку, складену у фоні, і оновлює картку в чаті.

    ``[SKIP]`` — чернетку пропускаємо (вхідне не потребує відповіді), картку
    прибираємо. Повертає "filled" | "skipped" | "stale" (чернетка вже не pending).
    """
    from app.modules.automation import telegram

    if draft.status != DraftStatus.pending:
        return "stale"
    text = (text or "").strip()
    if text == "[SKIP]":
        draft.status = DraftStatus.skipped
        db.commit()
        if draft.admin_message_id and settings.telegram_chat_id:
            telegram._call(
                "deleteMessage", chat_id=settings.telegram_chat_id, message_id=draft.admin_message_id
            )
        return "skipped"
    draft.draft_text = text
    db.commit()
    db.refresh(draft)
    if draft.admin_message_id and settings.telegram_chat_id:
        from .handlers import _draft_keyboard, _preview_text

        contact = db.get(Contact, draft.contact_id)
        telegram._call(
            "editMessageText",
            chat_id=settings.telegram_chat_id,
            message_id=draft.admin_message_id,
            text=_preview_text(contact, draft, False),
            parse_mode="HTML",
            reply_markup=_draft_keyboard(draft.id, bool(text)),
        )
    return "filled"


def mark(db: Session, draft: TelegramDraft, status: DraftStatus) -> None:
    draft.status = status
    if status == DraftStatus.sent:
        draft.sent_at = datetime.now(timezone.utc)
    db.commit()


#: Telegram Business дає боту писати від імені власника лише в чат, де було
#: вхідне повідомлення за останні стільки годин (``can_reply`` у Bot API).
REPLY_WINDOW_HOURS = 24


def can_reply(db: Session, contact: Contact, now: datetime | None = None) -> bool:
    """Чи бот зможе надіслати від імені власника: вхідне в Telegram за 24 год."""
    from datetime import timedelta

    from app.modules.interactions.models import Interaction

    last = db.scalars(
        select(Interaction.occurred_at)
        .where(
            Interaction.contact_id == contact.id,
            Interaction.direction == Direction.inbound,
            Interaction.source == "telegram",
        )
        .order_by(Interaction.occurred_at.desc())
        .limit(1)
    ).first()
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last < timedelta(hours=REPLY_WINDOW_HOURS)


def chat_url(contact: Contact, chat_id: int | None = None) -> str | None:
    """Посилання, що відкриває чат: t.me/<username>, інакше tg://user?id=<id>."""
    handle = (contact.telegram or "").strip().lstrip("@")
    if handle and " " not in handle and "://" not in handle and "/" not in handle:
        return f"https://t.me/{handle}"
    chat = chat_id or contact.telegram_chat_id
    return f"tg://user?id={chat}" if chat else None


def mark_sent_manually(db: Session, draft: TelegramDraft) -> None:
    """«Надіслав сам»: текст іде в історію (як crm_log_interaction), чернетка закрита.

    Якщо Business-зʼєднання вже принесло цей вихідний (власник писав із
    телефона), другого запису не робимо.
    """
    contact = db.get(Contact, draft.contact_id) if draft.contact_id else None
    if contact is not None:
        since = draft.created_at
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        seen = [
            i for i in contact.interactions
            if i.direction == Direction.outbound and i.source == "telegram"
            and since is not None
            and (i.occurred_at if i.occurred_at.tzinfo else i.occurred_at.replace(tzinfo=timezone.utc)) >= since
        ]
        external_id = f"tgb:{draft.chat_id}:own:{draft.id}"
        if not seen and not interactions_service.interaction_exists(db, contact.id, external_id):
            interactions_service.add_synced_interaction(
                db,
                contact,
                occurred_at=datetime.now(timezone.utc),
                channel=Channel.message,
                direction=Direction.outbound,
                summary=draft.draft_text[:300],
                source="telegram",
                external_id=external_id,
            )
            db.refresh(contact)
            warmth.refresh(contact)
            db.commit()
        from app.modules.automation import reminders as _reminders

        _reminders.complete_for_contact(db, contact.id)
    mark(db, draft, DraftStatus.sent)


def approve_and_send(db: Session, client, draft: TelegramDraft) -> bool:
    """Send the draft to the contact via the business connection (as the
    user), log the outgoing interaction, refresh warmth."""
    if not draft.draft_text.strip():
        return False
    result = client.send_message(
        draft.chat_id,
        draft.draft_text,
        business_connection_id=draft.business_connection_id,
        parse_mode=None,
    )
    if result is None:
        return False

    contact = db.get(Contact, draft.contact_id) if draft.contact_id else None
    if contact:
        message_id = result.get("message_id", 0) if isinstance(result, dict) else 0
        external_id = f"tgb:{draft.chat_id}:out:{message_id or draft.id}"
        if not interactions_service.interaction_exists(db, contact.id, external_id):
            interactions_service.add_synced_interaction(
                db,
                contact,
                occurred_at=datetime.now(timezone.utc),
                channel=Channel.message,
                direction=Direction.outbound,
                summary=draft.draft_text[:300],
                source="telegram",
                external_id=external_id,
            )
            db.refresh(contact)
            warmth.refresh(contact)
            db.commit()
        # Reaching out fulfils any open reminder for this person.
        from app.modules.automation import reminders as _reminders

        _reminders.complete_for_contact(db, contact.id)

    mark(db, draft, DraftStatus.sent)
    return True


def reformulate(
    db: Session, draft: TelegramDraft, instruction: str
) -> str | None:
    """Rewrite the draft per the admin's instruction (typed or voice)."""
    contact = db.get(Contact, draft.contact_id) if draft.contact_id else None
    if contact is None:
        return None
    new_text = ai.refine_reply(contact, draft.draft_text, instruction)
    if new_text:
        update_draft_text(db, draft, new_text)
    return new_text


# ── Outreach queue ───────────────────────────────────────────────────────────


def get_enabled_connection(db: Session) -> BusinessConnection | None:
    """The user's Telegram Business connection (needed to send as the user)."""
    return db.scalar(
        select(BusinessConnection)
        .where(BusinessConnection.is_enabled.is_(True))
        .order_by(BusinessConnection.updated_at.desc())
    )


def _drafted_today_ids(db: Session) -> set:
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return set(
        db.scalars(
            select(TelegramDraft.contact_id).where(
                TelegramDraft.kind == DraftKind.outreach,
                TelegramDraft.created_at >= today_start,
            )
        )
    )


def _reachable(db: Session):
    return list(
        db.scalars(
            select(Contact).where(
                Contact.telegram_chat_id.is_not(None),
                Contact.do_not_contact.is_(False),
            )
        ).unique()
    )


def effective_importance(contact: Contact) -> int:
    """Explicit importance (1..3) or, when unset (0), derived from goal links
    and dialogue depth — people tied to an active goal, and long real
    conversations, outrank one-off exchanges."""
    from app.modules.goals.service import has_active_goal

    if has_active_goal(contact):
        return max(contact.importance, 3)
    if contact.importance:
        return contact.importance
    n = len(contact.interactions)
    if n >= 60:
        return 3
    if n >= 15:
        return 2
    return 1


def birthday_reason(contact: Contact) -> str:
    return "сьогодні день народження 🎉 — привітай!"


def followup_reason(contact: Contact, days: int) -> str:
    return (
        f"ти написав {days} дн. тому — відповіді не було; "
        "варте м'якого нагадування"
    )


def _awaiting_reply_days(contact: Contact) -> int | None:
    """Days since our last unanswered Telegram message, or None.

    Only the FIRST unanswered message triggers a follow-up (a trailing run
    of 2+ outbound messages means we already nudged — stop nagging)."""
    msgs = [
        i
        for i in contact.interactions
        if i.channel.value == "message" and i.source == "telegram"
    ]
    if not msgs or msgs[0].direction.value != "outbound":
        return None
    run = 0
    for m in msgs:
        if m.direction.value == "outbound":
            run += 1
        else:
            break
    if run >= 2:
        return None
    last = msgs[0].occurred_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - last).days
    return days if days >= settings.followup_after_days else None


def outreach_reason(contact: Contact) -> str:
    elapsed = round(warmth.days_since_last_contact(contact))
    target = warmth.target_days(contact.contact_frequency)
    overdue = max(elapsed - target, 0)
    reason = (
        f"{elapsed} днів без контакту при цілі «{contact.contact_frequency.value}»"
        + (f" — прострочено на {overdue} дн." if overdue else "")
    )
    fresh_events = [
        e for e in contact.life_events if e.status.value == "new"
    ]
    if fresh_events:
        reason += f" Свіжий привід: {fresh_events[0].title}."
    return reason


def next_outreach_contact(db: Session) -> tuple[Contact, str] | None:
    """The next queue card: (contact, reason).

    Priority: due user-set reminders → birthdays today → unanswered
    follow-ups → most overdue. A contact is offered at most once per day
    (outreach drafts are the ledger), whatever the user decided about them."""
    from app.modules.automation import reminders as _reminders

    drafted_today = _drafted_today_ids(db)
    pool = [c for c in _reachable(db) if c.id not in drafted_today]
    by_id = {c.id: c for c in pool}

    # 1) Explicit reminders the user set ("remind me to write Oleg") — the
    #    strongest signal, since the user asked for it by name.
    for r in _reminders.due_reminders(db):
        c = by_id.get(r.contact_id)
        if c is not None:
            return c, f"нагадування: {r.text}"

    today = datetime.now(timezone.utc).date()
    for c in pool:
        if (
            c.birth_date
            and c.birth_date.month == today.month
            and c.birth_date.day == today.day
        ):
            return c, birthday_reason(c)

    followups = []
    for c in pool:
        days = _awaiting_reply_days(c)
        if days is not None:
            followups.append((days, c))
    if followups:
        followups.sort(key=lambda x: x[0], reverse=True)
        days, c = followups[0]
        return c, followup_reason(c, days)

    due = [c for c in pool if warmth.is_due(c)]
    if not due:
        return None
    due.sort(
        key=lambda c: (effective_importance(c), warmth.overdue_ratio(c)),
        reverse=True,
    )
    return due[0], outreach_reason(due[0])


def stop_list(db: Session, contact: Contact) -> None:
    """Add to the stop-list: never suggested by the queue/digest again
    (reversible from the web UI — the do_not_contact flag on the contact)."""
    contact.do_not_contact = True
    db.commit()


def create_outreach_draft(
    db: Session,
    contact: Contact,
    connection: BusinessConnection,
    reason: str | None = None,
) -> TelegramDraft:
    reason = reason or outreach_reason(contact)
    draft = TelegramDraft(
        contact_id=contact.id,
        chat_id=contact.telegram_chat_id,
        business_connection_id=connection.connection_id,
        kind=DraftKind.outreach,
        incoming_text=reason,  # the "why now" shown on the card
        draft_text=ai.draft_outreach(contact, reason) or "",
        status=DraftStatus.pending,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


# ── Voice intake ─────────────────────────────────────────────────────────────


def find_contact_by_name(db: Session, name: str) -> Contact | None:
    """Fuzzy-match a contact by a spoken/typed name (substring both ways).

    Prefers the tightest name match, then the best-known contact. Shared by
    voice intake and the natural-language assistant so they resolve people
    the same way. Returns None on no match or empty name."""
    needle = (name or "").strip().casefold()
    if not needle:
        return None
    matches = [
        c
        for c in db.scalars(select(Contact)).unique()
        if needle in c.full_name.casefold() or c.full_name.casefold() in needle
    ]
    if not matches:
        return None
    matches.sort(key=lambda c: (len(c.full_name), -effective_importance(c)))
    return matches[0]


def apply_intake(db: Session, parsed: dict) -> tuple[Contact, bool, int]:
    """Apply a parsed voice note: find-or-create the contact by name, add
    facts (deduped by the facts layer), append the note. Returns
    (contact, created, facts_added)."""
    from app.modules.insights import service as insights_service
    from app.modules.insights.models import FactType

    raw_name = (parsed.get("name") or "").strip()
    first = (parsed.get("first_name") or "").strip() or raw_name.split(" ")[0]
    last = (parsed.get("last_name") or "").strip() or (
        raw_name.split(" ", 1)[1] if " " in raw_name else None
    )

    contact = find_contact_by_name(db, raw_name)
    created = False
    if contact is None:
        contact = Contact(first_name=first or "Unknown", last_name=last)
        tag = db.scalar(select(Tag).where(Tag.name == "voice"))
        if tag is None:
            tag = Tag(name="voice")
            db.add(tag)
            db.flush()
        contact.tags = [tag]
        db.add(contact)
        db.flush()
        created = True

    if parsed.get("company") and not contact.company:
        contact.company = parsed["company"][:200]
    if parsed.get("position") and not contact.position:
        contact.position = parsed["position"][:200]

    note = (parsed.get("note") or "").strip()
    if note:
        stamp = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        contact.notes = (
            f"{contact.notes}\n[{stamp}] {note}" if contact.notes else f"[{stamp}] {note}"
        )
    db.commit()
    db.refresh(contact)

    added = 0
    for f in parsed.get("facts") or []:
        value = (f.get("value") or "").strip()
        if not value:
            continue
        try:
            fact_type = FactType(str(f.get("fact_type", "other")).lower())
        except ValueError:
            fact_type = FactType.other
        insights_service.add_fact(
            db, contact, fact_type=fact_type, value=value, source="voice"
        )
        added += 1
    db.refresh(contact)
    return contact, created, added


# ── Channel intake (reply to the 📇 message) ─────────────────────────────────

_CHANNEL_FIELDS = {
    "email": "Email",
    "phone": "Телефон",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "instagram_url": "Instagram",
    "linkedin_url": "LinkedIn",
    "facebook_url": "Facebook",
    "twitter_url": "Twitter/X",
    "youtube_url": "YouTube",
    "github_url": "GitHub",
    "website_url": "Сайт",
    "company": "Компанія",
    "position": "Посада",
}


def parse_channel_text(text: str) -> dict:
    """Pull channels out of free text: emails, phones, social links, @handles."""
    import re

    out: dict[str, str] = {}
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        out.setdefault("email", email)
    for phone in re.findall(r"\+?\d[\d\s\-()]{7,}\d", text):
        out.setdefault("phone", re.sub(r"[\s\-()]", "", phone))
    for url in re.findall(r"(?:https?://)?[\w.-]+\.[a-z]{2,}/[\w\-./?=%@+]*", text, re.I):
        low = url.lower()
        full = url if "://" in url else f"https://{url}"
        if "instagram.com" in low:
            out.setdefault("instagram_url", full)
        elif "linkedin.com" in low:
            out.setdefault("linkedin_url", full)
        elif "facebook.com" in low or "fb.com" in low:
            out.setdefault("facebook_url", full)
        elif "twitter.com" in low or "x.com" in low:
            out.setdefault("twitter_url", full)
        elif "youtube.com" in low or "youtu.be" in low:
            out.setdefault("youtube_url", full)
        elif "github.com" in low:
            out.setdefault("github_url", full)
        elif "t.me" in low:
            handle = full.rstrip("/").rsplit("/", 1)[-1]
            out.setdefault("telegram", f"@{handle}")
        elif "wa.me" in low:
            out.setdefault("whatsapp", full.rstrip("/").rsplit("/", 1)[-1])
        else:
            out.setdefault("website_url", full)
    for handle in re.findall(r"(?<![\w@.])@([A-Za-z]\w{3,31})\b", text):
        out.setdefault("telegram", f"@{handle}")
    return out


def apply_channels(db: Session, contact: Contact, data: dict) -> list[str]:
    """Write parsed channels into the contact. Returns human labels saved."""
    saved = []
    for field, label in _CHANNEL_FIELDS.items():
        value = (data.get(field) or "").strip()
        if not value:
            continue
        setattr(contact, field, value[:300])
        saved.append(label)
    if saved:
        db.commit()
        db.refresh(contact)
    return saved
