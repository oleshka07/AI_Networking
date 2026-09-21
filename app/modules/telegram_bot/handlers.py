"""Telegram update dispatcher: admin commands, Business proxy, callbacks.

All admin-facing text is HTML. Every handler is defensive — an exception in
one update must never kill the polling loop (the poller catches too, but we
keep failures local so one bad update doesn't block the batch).
"""

from __future__ import annotations

import html
import logging
import re as _re

from app.core.config import settings
from app.core.database import SessionLocal
from app.modules.contacts.models import Contact

from . import service
from .models import DraftStatus
from .transcribe import transcribe_ogg

logger = logging.getLogger("networking.tgbot")


def _esc(s: str | None) -> str:
    return html.escape(s or "")


_TASK_STATUS = {
    "immediate": "🔥 зараз", "urgent": "⏰ дедлайн", "current": "▶ в роботі",
    "todo": "○ у черзі", "hold": "⏸ чекає", "done": "✓ готово",
}


def _task_meta(task) -> str:
    """Рядок під назвою задачі: статус, проєкт, скільки займе, дедлайн."""
    status = getattr(task.status, "value", task.status)
    bits = [_TASK_STATUS.get(status, status)]
    if task.project:
        bits.append("#" + _esc(task.project))
    if task.duration_min:
        bits.append(f"{task.duration_min} хв")
    if task.due_date:
        bits.append("до " + task.due_date.isoformat())
    if getattr(task, "counterpart", None) and task.item_type == "нагадування":
        bits.append("відп.: " + _esc(task.counterpart))
    if getattr(task, "recur", None):
        bits.append(_esc(task.recur))
    elif task.item_type == "нагадування":
        bits.append("періодичність не задана")
    if getattr(task, "next_remind_at", None):
        bits.append(f"нагадаю {task.next_remind_at:%d.%m}")
    return " · ".join(bits)


# Reply-keyboard buttons the old Chater bot left stuck in the chat. Tapping
# one used to open Chater's planner — now we clear the keyboard and help.
_LEGACY_BUTTONS = {
    "задачі", "огляд", "промпти", "налаштування", "меню",
    "📋 задачі", "📊 огляд", "📝 промпти", "⚙️ налаштування", "☰ меню",
}
_REMOVE_KEYBOARD = {"remove_keyboard": True}


def _is_legacy_button(text: str) -> bool:
    return text.strip().casefold() in _LEGACY_BUTTONS


def _admin_id() -> int | None:
    try:
        return int(settings.telegram_chat_id) if settings.telegram_chat_id else None
    except (TypeError, ValueError):
        return None


# ── Preview rendering ────────────────────────────────────────────────────────


def _draft_keyboard(draft_id: int, has_draft: bool, outreach: bool = False) -> dict:
    row1 = []
    if has_draft:
        row1.append({"text": "✅ Надіслати", "callback_data": f"d:s:{draft_id}"})
    row1.append({"text": "✋ Вручну", "callback_data": f"d:m:{draft_id}"})
    row1.append({"text": "⏭ Пропустити", "callback_data": f"d:k:{draft_id}"})
    row2 = []
    if has_draft:
        row2.append({"text": "🌐 Переклад", "callback_data": f"d:t:{draft_id}"})
    row2.append({"text": "📇 Контакти", "callback_data": f"d:c:{draft_id}"})
    if outreach:
        row2.append({"text": "🚫 Стоп-лист", "callback_data": f"d:x:{draft_id}"})
    else:
        # Reply cards: let the user switch this person to manual replies.
        row2.append({"text": "🔕 Пауза", "callback_data": f"d:p:{draft_id}"})
    return {"inline_keyboard": [row1, row2] if row2 else [row1]}


def _paused_notice(contact, text: str) -> str:
    return (
        f"🔕 <b>{_contact_link(contact)}</b> — ти відповідаєш сам\n"
        f"«{_esc((text or '')[:500])}»"
    )


def _paused_keyboard(draft_id: int, flags: set | None = None) -> dict:
    flags = flags or set()
    rows = []
    action_row = []
    if "meeting" in flags:
        action_row.append(
            {"text": "➕ У календар", "callback_data": f"d:cal:{draft_id}"}
        )
    if "task" in flags:
        action_row.append(
            {"text": "⏰ Нагадати", "callback_data": f"d:tk:{draft_id}"}
        )
    if action_row:
        rows.append(action_row)
    rows.append(
        [{"text": "✍️ Скласти відповідь", "callback_data": f"d:g:{draft_id}"}]
    )
    rows.append(
        [{"text": "▶️ Увімкнути авто-відповіді", "callback_data": f"d:r:{draft_id}"}]
    )
    return {"inline_keyboard": rows}


def _translate_keyboard(draft_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🇬🇧 EN", "callback_data": f"d:t:{draft_id}:en"},
                {"text": "🇨🇿 CS", "callback_data": f"d:t:{draft_id}:cs"},
                {"text": "🇷🇺 RU", "callback_data": f"d:t:{draft_id}:ru"},
                {"text": "🇺🇦 UK", "callback_data": f"d:t:{draft_id}:uk"},
                {"text": "↩", "callback_data": f"d:t:{draft_id}:back"},
            ]
        ]
    }


def _contact_link(contact) -> str:
    """Clickable name that opens the person's chat: t.me/username is the
    reliable path (tg://user?id renders unclickable for unknown users)."""
    name = _esc(contact.full_name)
    handle = (contact.telegram or "").strip().lstrip("@")
    if handle and " " not in handle and "://" not in handle:
        return f'<a href="https://t.me/{_esc(handle)}">{name}</a>'
    if contact.telegram_chat_id:
        return f'<a href="tg://user?id={contact.telegram_chat_id}">{name}</a>'
    return name


# Channels-message id → contact id, so a reply to it saves new channels.
_channel_msgs: dict[int, int] = {}

# Confirmation message id → resource id, so replying to "збережу на потім"
# (text or voice) attaches the note to that exact link.
_resource_msgs: dict[int, int] = {}


def _channels_text(contact, draft_text: str) -> str:
    """All known channels for reaching this person elsewhere + the draft in
    a tap-to-copy block."""
    lines = [f"📇 <b>{_esc(contact.full_name)} — канали</b>", ""]
    pairs = [
        ("Telegram", contact.telegram),
        ("Телефон", contact.phone),
        ("Email", contact.email),
        ("WhatsApp", contact.whatsapp),
        ("Viber", contact.viber),
        ("Instagram", contact.instagram_url),
        ("LinkedIn", contact.linkedin_url),
        ("Facebook", contact.facebook_url),
        ("Twitter/X", contact.twitter_url),
        ("YouTube", contact.youtube_url),
        ("GitHub", contact.github_url),
        ("Сайт", contact.website_url),
    ]
    known = [(k, v) for k, v in pairs if v]
    if known:
        for k, v in known:
            lines.append(f"• {k}: {_esc(str(v))}")
    else:
        lines.append("<i>Інших каналів не знаю — додай у веб-інтерфейсі.</i>")
    if draft_text.strip():
        lines.append("")
        lines.append("Повідомлення (тапни, щоб скопіювати):")
        lines.append(f"<code>{_esc(draft_text)}</code>")
    lines.append("")
    lines.append(
        "<i>↩️ Reply на це повідомлення: email / телефон / лінк на соцмережу / "
        "фото візитки — збережу в картку контакту.</i>"
    )
    return "\n".join(lines)


def _outreach_card(contact, draft) -> str:
    """Queue card: who, why now, last touch, facts — then the draft."""
    head = f"👤 <b>{_contact_link(contact)}</b>"
    if contact.telegram:
        head += f" ({_esc(contact.telegram)})"
    head += (
        f" · {_esc(contact.relationship_type.value)}"
        f" · {_esc(contact.warmth_status)} {round(contact.warmth_score)}"
    )
    if service.effective_importance(contact) >= 3:
        head += " · ⭐ важливий"
    lines = [head, "", f"📊 <b>Чому зараз:</b> {_esc(draft.incoming_text)}"]

    from app.modules.goals.service import active_goals_for_contact

    goals = active_goals_for_contact(contact)
    if goals:
        lines.append("🎯 <b>Ціль:</b> " + ", ".join(_esc(g.title) for g in goals[:2]))

    last = next((i for i in contact.interactions if i.summary), None)
    if last:
        when = last.occurred_at.strftime("%d.%m.%Y")
        lines.append(f"🕓 <b>Останнє:</b> {when} — {_esc(last.summary[:160])}")

    facts = [f for f in getattr(contact, "facts", []) if f.is_current][:3]
    if facts:
        lines.append("💡 " + "; ".join(_esc(f.value) for f in facts))

    lines.append("")
    if draft.draft_text.strip():
        lines.append(f"✍️ <b>Чернетка:</b>\n{_esc(draft.draft_text)}")
        lines.append("")
        lines.append("<i>Reply текстом/голосом — скоригую чернетку.</i>")
    else:
        lines.append("<i>Чернетки немає (AI недоступний) — напиши сам або пропусти.</i>")
    return "\n".join(lines)


def _render_draft(contact, draft, is_new: bool = False) -> str:
    from .models import DraftKind

    if draft.kind == DraftKind.outreach:
        return _outreach_card(contact, draft)
    return _preview_text(contact, draft, is_new)


def _send_next_outreach_card(client, db, admin: int) -> bool:
    """Advance the queue: draft the next due contact and send its card.
    Returns False when the queue is empty (and tells the admin)."""
    connection = service.get_enabled_connection(db)
    if connection is None:
        client.send_message(
            admin,
            "Ще не бачу Business-з'єднання. Воно підхопиться саме собою з "
            "першим вхідним повідомленням — або перемкни бота в "
            "Налаштування → Telegram Business → Чат-боти (вимкнути/увімкнути).",
        )
        return False
    nxt = service.next_outreach_contact(db)
    if nxt is None:
        from datetime import datetime, timezone

        from app.modules.telegram_bot.models import DraftKind, DraftStatus, TelegramDraft
        from sqlalchemy import func, select

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sent = db.scalar(
            select(func.count(TelegramDraft.id)).where(
                TelegramDraft.kind == DraftKind.outreach,
                TelegramDraft.created_at >= today_start,
                TelegramDraft.status == DraftStatus.sent,
            )
        )
        client.send_message(
            admin,
            f"🎉 Черга на сьогодні порожня. Надіслано: {sent or 0}. "
            "Всі, хто був прострочений і доступний у Telegram, опрацьовані.",
        )
        return False
    contact, reason = nxt
    # Make the card clickable/complete: pull username+birthday from Telegram
    # if we still don't have a handle for this export-imported contact.
    if not (contact.telegram or "").strip():
        try:
            service.enrich_from_telegram(db, client, contact)
        except Exception as exc:  # pragma: no cover
            logger.warning("enrich failed for %s: %s", contact.id, exc)
    draft = service.create_outreach_draft(db, contact, connection, reason)
    sent = client.send_message(
        admin,
        _outreach_card(contact, draft),
        reply_markup=_draft_keyboard(
            draft.id, bool(draft.draft_text.strip()), outreach=True
        ),
    )
    if sent:
        service.set_admin_message(db, draft, sent.get("message_id", 0))
    return True


def _preview_text(contact, draft, is_new: bool, drafting: bool = False) -> str:
    head = f"👤 <b>{_contact_link(contact)}</b>"
    if contact.telegram:
        head += f" ({_esc(contact.telegram)})"
    if is_new:
        head += " · 🆕 новий контакт"
    else:
        head += (
            f" · {_esc(contact.relationship_type.value)}"
            f" · {_esc(contact.warmth_status)} {round(contact.warmth_score)}"
        )
    body = f"\n\n💬 {_esc(draft.incoming_text)}"
    if draft.draft_text.strip():
        body += f"\n\n✍️ <b>Чернетка:</b>\n{_esc(draft.draft_text)}"
        body += "\n\n<i>Reply текстом/голосом — скоригую чернетку.</i>"
    elif drafting:
        body += "\n\n<i>⏳ Claude пише чернетку — картка оновиться сама.</i>"
    else:
        body += "\n\n<i>Чернетки немає (AI недоступний) — відповідай вручну.</i>"
    return head + body


# ── Update handlers ──────────────────────────────────────────────────────────


def handle_business_connection(client, bc: dict) -> None:
    with SessionLocal() as db:
        service.upsert_connection(
            db,
            connection_id=bc.get("id", ""),
            user_chat_id=(bc.get("user") or {}).get("id"),
            is_enabled=bool(bc.get("is_enabled", True)),
        )
    logger.info("Business connection updated: %s", bc.get("id"))


def handle_business_message(client, msg: dict, *, alisio_handled: bool = False) -> None:
    """``alisio_handled`` — чат веде ALiSiO: лише памʼять, жодних чернеток і карток."""
    admin = _admin_id()
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    bc_id = msg.get("business_connection_id")
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = msg.get("text") or msg.get("caption") or ""
    if not text and msg.get("voice"):
        audio = client.download_file(msg["voice"].get("file_id", ""))
        transcript = transcribe_ogg(audio) if audio else None
        text = f"[голосове] {transcript}" if transcript else "[голосове повідомлення]"
    if not text:
        return  # stickers/photos without caption — nothing to draft on

    # The user's own messages (typed on their phone) also arrive here —
    # log them as outgoing history, never draft a reply to yourself.
    if sender.get("id") is not None and sender.get("id") != chat_id:
        with SessionLocal() as db:
            contact, _ = service.find_or_create_contact(
                db,
                chat_id,
                chat.get("first_name") or "Unknown",
                chat.get("last_name"),
                chat.get("username"),
            )
            from datetime import datetime, timezone

            from app import warmth
            from app.modules.interactions import service as itx
            from app.modules.interactions.models import Channel, Direction

            ext = f"tgb:{chat_id}:{msg.get('message_id')}"
            if not itx.interaction_exists(db, contact.id, ext):
                itx.add_synced_interaction(
                    db,
                    contact,
                    occurred_at=datetime.now(timezone.utc),
                    channel=Channel.message,
                    direction=Direction.outbound,
                    summary=text[:300],
                    source="telegram",
                    external_id=ext,
                )
                db.refresh(contact)
                warmth.refresh(contact)
                db.commit()
            # Messaging someone directly closes any reminder about them.
            from app.modules.automation import reminders as _reminders

            _reminders.complete_for_contact(db, contact.id)
        return

    if alisio_handled:
        # Чат ліда ALiSiO: вона відповість сама. Ми лише пишемо в історію,
        # щоб памʼять про людину не мала дірки.
        with SessionLocal() as db:
            service.log_incoming(
                db,
                chat_id=chat_id,
                text=text,
                message_id=msg.get("message_id") or 0,
                first_name=sender.get("first_name") or chat.get("first_name") or "Unknown",
                last_name=sender.get("last_name") or chat.get("last_name"),
                username=sender.get("username") or chat.get("username"),
            )
        return

    with SessionLocal() as db:
        draft, contact, is_new, status = service.handle_incoming(
            db,
            chat_id=chat_id,
            text=text,
            message_id=msg.get("message_id") or 0,
            first_name=sender.get("first_name") or chat.get("first_name") or "Unknown",
            last_name=sender.get("last_name") or chat.get("last_name"),
            username=sender.get("username") or chat.get("username"),
            business_connection_id=bc_id,
        )
        if status == "paused":
            # Logged for context. Only ping when there's actually something to
            # act on (a meeting to schedule or a task to remember) — plain
            # chit-chat from a paused contact stays silent.
            flags = _detect_incoming_intent(text)
            if admin and flags:
                held = service.create_held_reply_draft(
                    db, contact, chat_id=chat_id, text=text,
                    business_connection_id=bc_id,
                )
                sent = client.send_message(
                    admin,
                    _paused_notice(contact, text),
                    reply_markup=_paused_keyboard(held.id, flags),
                )
                if sent:
                    service.set_admin_message(db, held, sent.get("message_id", 0))
            return
        if draft is None:
            return  # conversation-ender: logged, no reply needed
        if admin:
            sent = client.send_message(
                admin,
                _preview_text(contact, draft, is_new, drafting=(status == "drafting")),
                reply_markup=_draft_keyboard(draft.id, bool(draft.draft_text.strip())),
            )
            if sent:
                service.set_admin_message(db, draft, sent.get("message_id", 0))


def _handle_task_callback(client, cb_id: str, data: str, admin: int | None) -> None:
    """Кнопки під задачею: ✓ готово / ▶ взяв у роботу. Далі показує наступну."""
    from app.modules.tasks import service as tasks_service
    from app.modules.tasks.models import Task, TaskStatus

    try:
        _, action, task_id = data.split(":")
        task_id = int(task_id)
    except ValueError:
        client.answer_callback(cb_id)
        return

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            client.answer_callback(cb_id, "Уже неактуально")
            return
        if action == "done":
            task.status = TaskStatus.done
            note = "✓ Готово"
        else:
            task.status = TaskStatus.current
            note = "▶ В роботі"
        db.commit()
        client.answer_callback(cb_id, note)
        if not admin:
            return
        nxt = tasks_service.next_task(db)
        if nxt is None:
            client.send_message(admin, f"{note}: {_esc(task.title)}\n\nБільше задач немає 🎉")
            return
        client.send_message(
            admin,
            f"{note}: {_esc(task.title)}\n\n🎯 <b>Далі</b>\n"
            + _esc(nxt.title) + "\n" + _task_meta(nxt),
            reply_markup={"inline_keyboard": [[
                {"text": "✓ Готово", "callback_data": f"tk:done:{nxt.id}"},
                {"text": "▶ Взяв у роботу", "callback_data": f"tk:cur:{nxt.id}"},
            ]]},
        )


def handle_callback(client, cb: dict) -> None:
    data = cb.get("data") or ""
    cb_id = cb.get("id", "")
    msg = cb.get("message") or {}
    admin = _admin_id()

    if data == "q:start":
        client.answer_callback(cb_id, "Починаю обхід")
        if admin:
            with SessionLocal() as db:
                _send_next_outreach_card(client, db, admin)
        return

    if data.startswith("tk:"):
        _handle_task_callback(client, cb_id, data, admin)
        return

    if data.startswith("r:"):
        from app.modules.resources import service as res
        from app.modules.resources.models import ResourceStatus

        parts = data.split(":")
        try:
            action, rid = parts[1], int(parts[2])
        except (IndexError, ValueError):
            client.answer_callback(cb_id)
            return
        with SessionLocal() as db:
            r = res.get_resource(db, rid)
            if r is None:
                client.answer_callback(cb_id, "Уже немає")
                return
            res.set_status(
                db, r,
                ResourceStatus.done if action == "d" else ResourceStatus.dropped,
            )
            left = res.open_count(db)
        client.answer_callback(
            cb_id, f"✅ Готово · лишилось {left}" if action == "d" else "🗑 Прибрав"
        )
        return

    if data.startswith("m:"):
        _handle_mail_callback(client, cb_id, data, admin)
        return

    if not data.startswith("d:"):
        client.answer_callback(cb_id)
        return
    try:
        parts = data.split(":")
        action = parts[1]
        draft_id = int(parts[2])
        arg = parts[3] if len(parts) > 3 else None
    except (ValueError, IndexError):
        client.answer_callback(cb_id)
        return

    with SessionLocal() as db:
        draft = service.get_draft(db, draft_id)
        if draft is None or draft.status != DraftStatus.pending:
            client.answer_callback(cb_id, "Уже неактуально")
            return
        from .models import DraftKind

        is_outreach = draft.kind == DraftKind.outreach
        handled = False

        # ── Non-terminal actions (the card stays pending) ────────────────
        if action == "p":  # pause auto-replies for this person
            contact = db.get(Contact, draft.contact_id)
            if contact:
                contact.auto_reply_paused = True
                db.commit()
            client.answer_callback(cb_id, "🔕 Пауза: далі відповідаєш сам")
            return
        if action == "r":  # resume auto-replies
            contact = db.get(Contact, draft.contact_id)
            if contact:
                contact.auto_reply_paused = False
                db.commit()
            client.answer_callback(cb_id, "▶️ Авто-відповіді увімкнено")
            if msg and contact:
                client.edit_message_text(
                    msg["chat"]["id"],
                    msg["message_id"],
                    _paused_notice(contact, draft.incoming_text)
                    + "\n\n▶️ Авто-відповіді знову увімкнені.",
                )
            return
        if action == "g":  # compose a reply on demand for a held (paused) draft
            contact = db.get(Contact, draft.contact_id)
            client.answer_callback(cb_id, "Складаю…")
            from app.modules.insights import ai as _ai

            new_text = _ai.draft_reply(contact, draft.incoming_text) or ""
            if new_text.strip() == "[SKIP]":
                new_text = ""
            service.update_draft_text(db, draft, new_text)
            if msg:
                client.edit_message_text(
                    msg["chat"]["id"],
                    msg["message_id"],
                    _render_draft(contact, draft),
                    reply_markup=_draft_keyboard(draft.id, bool(new_text.strip())),
                )
            return
        if action == "cal":  # add the incoming message's meeting to the calendar
            client.answer_callback(cb_id, "Додаю в календар…")
            if admin:
                _handle_calendar_event(client, admin, {}, draft.incoming_text or "")
            return
        if action == "tk":  # remember the incoming request as a reminder
            contact = db.get(Contact, draft.contact_id)
            from datetime import datetime, timezone

            from app.modules.automation import reminders as _reminders

            now = datetime.now(timezone.utc)
            due = _reminder_due_from_text(draft.incoming_text or "", now)
            what = (draft.incoming_text or "").strip()[:120] or (
                f"відповісти {contact.full_name if contact else ''}".strip()
            )
            _reminders.create_reminder(db, contact, what, due)
            when = f"{due.date().isoformat()} о {due.strftime('%H:%M')}"
            client.answer_callback(cb_id, f"⏰ Нагадаю {when}")
            if admin:
                client.send_message(
                    admin,
                    f"⏰ Нагадаю <b>{when}</b>: {_esc(what)}"
                    + (f"\n👤 {_contact_link(contact)}" if contact else ""),
                )
            return
        if action == "c":
            contact = db.get(Contact, draft.contact_id)
            client.answer_callback(cb_id)
            if contact and admin:
                sent = client.send_message(
                    admin, _channels_text(contact, draft.draft_text)
                )
                if sent:
                    _channel_msgs[sent.get("message_id", 0)] = contact.id
                    if len(_channel_msgs) > 200:
                        for k in list(_channel_msgs)[:100]:
                            _channel_msgs.pop(k, None)
            return
        if action == "t":
            contact = db.get(Contact, draft.contact_id)
            if arg is None:
                client.answer_callback(cb_id, "Обери мову")
                if msg:
                    client.edit_message_text(
                        msg["chat"]["id"],
                        msg["message_id"],
                        _render_draft(contact, draft),
                        reply_markup=_translate_keyboard(draft.id),
                    )
                return
            kb = _draft_keyboard(draft.id, True, outreach=is_outreach)
            if arg == "back":
                client.answer_callback(cb_id)
                if msg:
                    client.edit_message_text(
                        msg["chat"]["id"],
                        msg["message_id"],
                        _render_draft(contact, draft),
                        reply_markup=kb,
                    )
                return
            from app.modules.insights import ai as _ai

            translated = _ai.translate_message(draft.draft_text, arg)
            if translated is None:
                client.answer_callback(cb_id, "Не вдалося перекласти")
                return
            service.update_draft_text(db, draft, translated)
            client.answer_callback(cb_id, "Перекладено")
            if msg:
                client.edit_message_text(
                    msg["chat"]["id"],
                    msg["message_id"],
                    _render_draft(contact, draft),
                    reply_markup=kb,
                )
            return
        if action == "x":
            contact = db.get(Contact, draft.contact_id)
            if contact:
                service.stop_list(db, contact)
            service.mark(db, draft, DraftStatus.skipped)
            client.answer_callback(cb_id, "У стоп-листі 🚫 (повернути — у веб-UI)")
            if msg:
                client.delete_message(msg["chat"]["id"], msg["message_id"])
            if is_outreach and admin:
                _send_next_outreach_card(client, db, admin)
            return

        if action == "s":
            ok = service.approve_and_send(db, client, draft)
            client.answer_callback(cb_id, "Надіслано ✅" if ok else "Помилка надсилання")
            if ok and msg:
                client.delete_message(msg["chat"]["id"], msg["message_id"])
            handled = ok
        elif action == "m":
            service.mark(db, draft, DraftStatus.manual)
            client.answer_callback(cb_id, "Ок, відповідай сам")
            if msg:
                client.delete_message(msg["chat"]["id"], msg["message_id"])
            handled = True
        elif action == "k":
            service.mark(db, draft, DraftStatus.skipped)
            client.answer_callback(cb_id, "Пропущено")
            if msg:
                client.delete_message(msg["chat"]["id"], msg["message_id"])
            handled = True
        else:
            client.answer_callback(cb_id)

        # The outreach queue flows card-to-card: any decision advances it.
        if is_outreach and handled and admin:
            _send_next_outreach_card(client, db, admin)


def handle_admin_message(client, msg: dict) -> None:
    """Messages in the admin's own chat with the bot: commands, or a reply
    to a draft preview (text/voice) that reformulates the draft."""
    admin = _admin_id()
    chat_id = (msg.get("chat") or {}).get("id")
    if admin is None or chat_id != admin:
        # Someone else talking to the bot directly — ignore politely.
        return

    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    text = msg.get("text") or ""

    # Reply to a preview → reformulate that draft.
    if reply_to:
        instruction = text
        if not instruction and msg.get("voice"):
            audio = client.download_file(msg["voice"].get("file_id", ""))
            instruction = transcribe_ogg(audio) if audio else None
            if not instruction:
                client.send_message(
                    admin,
                    "Не зміг розшифрувати голосове (потрібен DEEPGRAM_API_KEY, OPENAI_API_KEY "
                    "або GEMINI_API_KEY).",
                )
                return
        if not instruction:
            return
        # Reply to a 📇 channels message → save new channels to the contact.
        if reply_to in _resource_msgs:
            _handle_resource_note(
                client, admin, _resource_msgs[reply_to], instruction
            )
            return
        if reply_to in _channel_msgs:
            _handle_channel_reply(client, admin, _channel_msgs[reply_to], msg)
            return

        with SessionLocal() as db:
            draft = service.find_pending_by_admin_message(db, reply_to)
            if draft is None:
                return
            new_text = service.reformulate(db, draft, instruction)
            if new_text is None:
                client.send_message(admin, "Не вдалося переписати чернетку.")
                return
            contact = db.get(Contact, draft.contact_id)
            from .models import DraftKind as _DK

            kb = _draft_keyboard(draft.id, True, outreach=draft.kind == _DK.outreach)
            edited = client.edit_message_text(
                admin,
                reply_to,
                _render_draft(contact, draft),
                reply_markup=kb,
            )
            if not edited:
                sent = client.send_message(
                    admin,
                    _render_draft(contact, draft),
                    reply_markup=kb,
                )
                if sent:
                    service.set_admin_message(db, draft, sent.get("message_id", 0))
        return

    if _is_legacy_button(text):
        client.send_message(
            admin,
            "Старе меню Chater більше не використовується — прибрав клавіатуру. "
            "Актуальні команди: /help",
            reply_markup=_REMOVE_KEYBOARD,
        )
        return

    if text.startswith("/"):
        _handle_command(client, admin, text)
        return

    # Voice note (not a reply) = intake: "запиши, що я познайомився з ..."
    if msg.get("voice"):
        audio = client.download_file(msg["voice"].get("file_id", ""))
        transcript = transcribe_ogg(audio) if audio else None
        if not transcript:
            client.send_message(
                admin,
                "Не зміг розшифрувати голосове (потрібен DEEPGRAM_API_KEY, OPENAI_API_KEY або "
                "GEMINI_API_KEY).",
            )
            return
        # Route voice like text: time report / assistant command (calendar,
        # reminder, note, cadence, importance) first — only then contact
        # intake ("познайомився з ..."). Fixes voice calendar requests going
        # to intake and failing with "не зміг розібрати, про кого це".
        if _looks_like_timereport(transcript):
            _request_timereport(client, admin)
            return
        if _handle_coach_answer(client, admin, transcript):
            return
        if _handle_assistant_intent(client, admin, transcript):
            return
        _handle_voice_intake(client, admin, transcript)
        return

    # Plain text: time-tracking request → assistant command → network search.
    if text.strip():
        if _handle_link_capture(client, admin, text.strip()):
            return
        if _looks_like_timereport(text):
            _request_timereport(client, admin)
            return
        if _handle_coach_answer(client, admin, text.strip()):
            return
        if _handle_assistant_intent(client, admin, text.strip()):
            return
        _handle_network_search(client, admin, text.strip())


def _handle_coach_answer(client, admin: int, text: str) -> bool:
    """Якщо висить питання коуча — це відповідь на нього.

    Календарні прохання та ідеї пропускаємо далі: у них свої сильні маркери,
    і перехопити їх під виглядом відповіді було б гірше, ніж пропустити.
    """
    if _looks_like_calendar(text) or _looks_like_idea(text):
        return False
    from app.modules.coach import service as coach_service

    with SessionLocal() as db:
        if coach_service.pending_checkin(db) is None:
            return False
        reply = coach_service.record_answer(db, text)
    if not reply:
        return False
    client.send_message(admin, _esc(reply))
    return True


def _handle_channel_reply(client, admin: int, contact_id: int, msg: dict) -> None:
    """Reply to a channels card: text with links/email/phone, or a
    business-card photo — everything lands in the contact record."""
    with SessionLocal() as db:
        contact = db.get(Contact, contact_id)
        if contact is None:
            return

        parsed: dict = {}
        photo = msg.get("photo") or []
        document = msg.get("document") or {}
        if photo or str(document.get("mime_type", "")).startswith("image/"):
            file_id = (
                photo[-1].get("file_id", "") if photo else document.get("file_id", "")
            )
            image = client.download_file(file_id) if file_id else None
            if image:
                from app.modules.insights import ai as _ai

                parsed = _ai.parse_business_card(image) or {}
            if not parsed:
                client.send_message(
                    admin,
                    "Не зміг розпізнати візитку. Надішли дані текстом — "
                    "email, телефон чи лінк.",
                )
                return
        else:
            text = msg.get("text") or msg.get("caption") or ""
            if not text and msg.get("voice"):
                audio = client.download_file(msg["voice"].get("file_id", ""))
                text = transcribe_ogg(audio) if audio else ""
            parsed = service.parse_channel_text(text or "")

        saved = service.apply_channels(db, contact, parsed)
        if saved:
            client.send_message(
                admin,
                f"✅ Зберіг для <b>{_contact_link(contact)}</b>: "
                + ", ".join(saved),
            )
        else:
            client.send_message(
                admin,
                "Не знайшов у повідомленні email/телефону/лінків. "
                "Приклад: instagram.com/nick, +420123456789, nick@mail.com",
            )


def _handle_voice_intake(client, admin: int, transcript: str) -> None:
    from app.modules.insights import ai as _ai

    parsed = _ai.parse_voice_intake(transcript)
    if parsed is None:
        client.send_message(
            admin,
            "Почув: «" + _esc(transcript[:300]) + "»\n\n"
            "Не зміг розібрати, про кого це (або AI недоступний). "
            "Спробуй назвати ім'я людини явно.",
        )
        return
    with SessionLocal() as db:
        contact, created, facts_added = service.apply_intake(db, parsed)
        status = "🆕 створив контакт" if created else "оновив контакт"
        lines = [
            f"✅ {status}: <b>{_contact_link(contact)}</b>",
        ]
        role = " · ".join(filter(None, [contact.position, contact.company]))
        if role:
            lines.append(f"💼 {_esc(role)}")
        if facts_added:
            lines.append(f"💡 Фактів записано: {facts_added}")
        note = (parsed.get("note") or "").strip()
        if note:
            lines.append(f"📝 {_esc(note[:200])}")
        client.send_message(admin, "\n".join(lines))


_WEEKDAYS_UK = [
    "понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя",
]


def _due_from_iso(s: str):
    """ISO date string → a due datetime at 09:00 UTC, or None if unparseable."""
    from datetime import date as _date, datetime, time, timezone

    s = (s or "").strip()
    if not s:
        return None
    try:
        d = _date.fromisoformat(s[:10])
    except ValueError:
        return None
    return datetime.combine(d, time(9, 0, tzinfo=timezone.utc))


_UA_REL_DAYS = (("післязавтра", 2), ("позавтра", 2), ("завтра", 1), ("сьогодні", 0))
# Weekday stems, matched at a word boundary so "понеділка" doesn't hit the
# "неділ" (Sunday) stem hiding inside it.
_UA_WEEKDAYS = {
    "понеділ": 0, "вівтор": 1, "серед": 2, "четвер": 3,
    "пʼятниц": 4, "пятниц": 4, "субот": 5, "неділ": 6,
}


def _looks_like_calendar(text: str) -> bool:
    """Keyword net so a calendar request is caught even if the AI under-
    classifies it or its JSON call failed."""
    t = text.lower()
    if "календар" in t:
        return True
    verbs = ("додай", "добав", "додати", "постав", "заплануй", "створи", "признач")
    nouns = ("зустріч", "поді", "мітинг", "meeting", "дзвінок", "созвон")
    return any(v in t for v in verbs) and any(n in t for n in nouns)


def _cal_date_from_text(text: str, today):
    import re
    from datetime import timedelta

    t = text.lower()
    for word, off in _UA_REL_DAYS:
        if word in t:
            return today + timedelta(days=off)
    for name, idx in _UA_WEEKDAYS.items():
        if re.search(r"\b" + name, t):
            return today + timedelta(days=((idx - today.weekday()) % 7 or 7))
    return None


def _cal_hm(s):
    """Parse a 'HH:MM' / 'HH' string to (hour, minute), or None."""
    import re

    if not s:
        return None
    m = re.match(r"\s*(\d{1,2})[:.\s](\d{2})", str(s))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"\s*(\d{1,2})\s*$", str(s))
    if m and int(m.group(1)) < 24:
        return int(m.group(1)), 0
    return None


def _cal_time_from_text(text: str):
    """Find a start time in free Ukrainian text: 'о 15:00', 'на 14.30', '15:00'."""
    import re

    t = text.lower()
    m = re.search(r"\b(?:о|на|в|у)\s*(\d{1,2})[:.](\d{2})", t) or re.search(
        r"(\d{1,2}):(\d{2})", t
    )
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    # \b before the preposition so "до 16" / "по 17" don't match as "о 16".
    m = re.search(r"\b(?:о|на|в|у)\s+(\d{1,2})(?:\s*(?:годин\w*|год))?(?!\d)", t)
    if m and int(m.group(1)) < 24:
        return int(m.group(1)), 0
    return None


def _is_bare_time(text: str) -> bool:
    """True if the message is essentially just a time — '19', '19:00',
    'о 19', 'в 19:30', '9 год' — used to finish a pending calendar event."""
    import re

    return bool(
        re.fullmatch(
            r"(?:о|на|в|у)?\s*\d{1,2}(?:[:.]\d{2})?\s*(?:годин\w*|год)?",
            text.strip().lower(),
        )
    )


def _cal_title_from_text(text: str) -> str:
    """Best-effort event title from free text: strip command words, date,
    time, email, Meet, and 'з <Name>'. Empty when nothing meaningful remains."""
    import re

    t = text
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", t)
    t = re.sub(r"через\s+google\s*meet", " ", t, flags=re.I)
    t = re.sub(r"\b(google\s*meet|онлайн|гугл\s*міт)\b", " ", t, flags=re.I)
    t = re.sub(
        r"через\s+(?:\d+\s*)?(?:годин\w*|год|хвилин\w*|хв)|через\s+півгодини",
        " ", t, flags=re.I,
    )
    t = re.sub(r"\bнов\w+\b", " ", t, flags=re.I)
    t = re.sub(
        r"(?:з|від)\s*\d{1,2}(?:[:.]\d{2})?\s*(?:до|по|-|–|—)\s*\d{1,2}(?:[:.]\d{2})?",
        " ", t, flags=re.I,
    )
    t = re.sub(r"\b(?:о|на|в|у)\s*\d{1,2}(?:[:.]\d{2})?(?:\s*годин\w*)?", " ", t, flags=re.I)
    t = re.sub(r"\b(ок|ok|окей)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(будь\s+ласка|для\s+мене)\b", " ", t, flags=re.I)
    t = re.sub(
        r"\b(додай|добав\w*|додати|постав\w*|заплануй|створи|признач\w*)\b",
        " ", t, flags=re.I,
    )
    t = re.sub(r"\b(зустріч\w*|поді\w*|мітинг\w*|дзвінок|созвон)\b", " ", t, flags=re.I)
    t = re.sub(r"календар\w*", " ", t, flags=re.I)
    t = re.sub(r"\b(сьогодні|завтра|післязавтра|позавтра)\b", " ", t, flags=re.I)
    t = re.sub(r"\bз\s+[А-ЯІЇЄA-Z][\w'’]+", " ", t)  # "з Романом"
    t = re.sub(r"[,\.]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-—")
    t = re.sub(r"^(?:на|в|у|о|з|про)\s+", "", t).strip()
    return t if 2 <= len(t) <= 60 else ""


def _cal_range_from_text(text: str):
    """Parse a time range: 'з 15 до 16', 'з 9:30 до 11', '15-16', '15:00–16:30'.
    Returns ((sh, sm), (eh, em)) or None."""
    import re

    t = text.lower()
    for pat in (
        r"(?:з|від|c)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|по|-|–|—)\s*(\d{1,2})(?:[:.](\d{2}))?",
        r"(\d{1,2})(?:[:.](\d{2}))?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?",
    ):
        m = re.search(pat, t)
        if m:
            sh, sm = int(m.group(1)), int(m.group(2) or 0)
            eh, em = int(m.group(3)), int(m.group(4) or 0)
            if sh < 24 and eh < 24 and sm < 60 and em < 60:
                return (sh, sm), (eh, em)
    return None


_TASK_VERBS = (
    "можеш", "зможеш", "потрібно", "треба", "зроби", "зробити", "підготуй",
    "підготувати", "надішли", "надіслати", "скинь", "перешли", "зателефонуй",
    "подзвони", "передай", "глянь", "подивись", "перевір", "нагадай",
)
_MEETING_NOUNS = (
    "зустріч", "мітинг", "созвон", "зідзвон", "дзвінок", "meet", "meeting",
    "побач", "каві", "каву", "ланч", "обід",
)


def _detect_incoming_intent(text: str) -> set:
    """Lightweight, no-AI signal detection for a paused contact's message:
    does it look like a meeting to schedule and/or a task to remember?"""
    t = text.lower()
    flags = set()
    rng = _cal_range_from_text(text)
    has_time = _cal_time_from_text(text) is not None or rng is not None
    date_word = any(
        w in t for w in (
            "сьогодні", "завтра", "післязавтра", "понеділок", "вівторок",
            "серед", "четвер", "пятниц", "пʼятниц", "субот", "неділ",
        )
    )
    meeting_noun = any(w in t for w in _MEETING_NOUNS)
    if has_time and (meeting_noun or date_word or rng):
        flags.add("meeting")
    if any(v in t for v in _TASK_VERBS):
        flags.add("task")
    return flags


def _reminder_due_from_text(text: str, now):
    """When to remind, from a deadline in the text.

    'до понеділка' / 'до пʼятниці' → the evening (18:00) before that day;
    'до кінця тижня' → Thu evening; no deadline → tomorrow 09:00. Never in
    the past — a same-day/tomorrow deadline reminds this evening or soon."""
    from datetime import datetime, timedelta

    today = now.date()
    d = _cal_date_from_text(text, today)  # сьогодні/завтра/weekday/…
    tl = text.lower()
    if d is None and "кінц" in tl and "тижн" in tl:
        d = today + timedelta(days=(4 - today.weekday()) % 7)  # Friday
    if d is None:
        return (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    remind_day = d - timedelta(days=1)  # the evening before the deadline
    if remind_day <= today:
        if now.hour < 18:
            return now.replace(hour=18, minute=0, second=0, microsecond=0)
        return now + timedelta(hours=2)
    return datetime(
        remind_day.year, remind_day.month, remind_day.day, 18, 0, tzinfo=now.tzinfo
    )


def _cal_email_from_text(text: str):
    import re

    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    return m.group(0) if m else None


def _relative_delta_from_text(text: str):
    """A relative offset like 'через 1 годину' / 'через 30 хвилин' / 'через
    півгодини' → a timedelta (from now). None if not present."""
    import re
    from datetime import timedelta

    t = text.lower()
    if "півгодин" in t:
        return timedelta(minutes=30)
    m = re.search(r"через\s+(\d+)?\s*(годин\w*|год|hour|хвилин\w*|хв|min)", t)
    if not m:
        return None
    num = int(m.group(1)) if m.group(1) else 1
    if m.group(2).startswith(("годин", "год", "hour")):
        return timedelta(hours=num)
    return timedelta(minutes=num)


def _human_delta(td) -> str:
    mins = int(td.total_seconds() // 60)
    h, m = divmod(mins, 60)
    parts = []
    if h:
        parts.append(f"{h} год")
    if m:
        parts.append(f"{m} хв")
    return " ".join(parts) or "0 хв"


def _build_local_window(d, hm, endhm):
    """(start_local, end_local, when_label) from a date + start/optional-end
    time, as naive wall-clock strings interpreted in the calendar's timezone."""
    from datetime import datetime, timedelta

    sh, sm = hm
    start_dt = datetime(d.year, d.month, d.day, sh, sm)
    end_dt = datetime(d.year, d.month, d.day, endhm[0], endhm[1]) if endhm else None
    if end_dt is None or end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)
    start_local = start_dt.strftime("%Y-%m-%dT%H:%M:00")
    end_local = end_dt.strftime("%Y-%m-%dT%H:%M:00")
    when = f"{d.isoformat()} о {sh:02d}:{sm:02d}" + (
        f"–{end_dt.strftime('%H:%M')}" if endhm else ""
    )
    return start_local, end_local, when


# A calendar event we've started but are waiting on the time for, per admin.
# Process-lifetime, single-user — a restart just forgets a half-finished ask.
_pending_event: dict[int, dict] = {}
_PENDING_TTL = 900  # seconds


def _create_calendar_event(
    client, admin, *, summary, start_local, end_local, email, meet, person, when_label
) -> bool:
    from app.integrations import google

    with SessionLocal() as db:
        if not email and person:
            c = service.find_contact_by_name(db, person)
            if c and c.email:
                email = c.email
        if not google.status(db).get("connected"):
            client.send_message(
                admin,
                "📅 Google-календар не підключений. Підключи у веб-інтерфейсі: "
                "Integrations → Google.",
            )
            return True
        result = google.create_event(
            db,
            summary=summary,
            start_local=start_local,
            end_local=end_local,
            attendee_email=email or None,
            add_meet=meet,
        )
    if not result:
        client.send_message(
            admin,
            "⚠️ Не вдалося створити подію. Найімовірніше, треба перепідключити "
            "Google — ми додали право на запис у календар. Веб: Integrations → "
            "Google → Disconnect, потім Connect.",
        )
        return True

    lines = [f"✅ Подію створено: <b>{_esc(summary)}</b>", f"🗓 {when_label}"]
    if email:
        lines.append(f"👤 Гість: {_esc(email)}")
    if result.get("meet_link"):
        lines.append(f"🎥 {_esc(result['meet_link'])}")
    client.send_message(admin, "\n".join(lines))
    return True


def _complete_pending_event(client, admin, hm) -> bool:
    """Finish a calendar event we asked the time for, using the remembered
    title / date / guest."""
    from datetime import date as _date

    p = _pending_event.pop(admin, None)
    if not p:
        return False
    start_local, end_local, when = _build_local_window(
        _date.fromisoformat(p["date"]), hm, p.get("endhm")
    )
    return _create_calendar_event(
        client, admin, summary=p["title"], start_local=start_local,
        end_local=end_local, email=p.get("email"), meet=p.get("meet", False),
        person=p.get("person", ""), when_label=when,
    )


def _handle_calendar_event(client, admin: int, intent: dict, text: str) -> bool:
    """Create a Google Calendar event from a natural-language request. Parses
    date/time in Python (not trusting the model); if the time is missing it
    remembers the half-built event and asks for it. Always returns True — a
    calendar request never falls through to network search."""
    import time as _time
    from datetime import date as _date, datetime, timedelta, timezone

    title = (
        (intent.get("title") or "").strip()
        or _cal_title_from_text(text)
        or ("Зустріч" if "зустріч" in text.lower() else "Подія")
    )
    email = (intent.get("attendee_email") or "").strip() or _cal_email_from_text(text)
    meet = bool(intent.get("google_meet")) or any(
        w in text.lower() for w in ("google meet", "meet", "онлайн", "гугл міт")
    )
    person = (intent.get("person") or "").strip()

    # Relative time ("через 1 годину") → an absolute instant from now. Sent as
    # a UTC-offset timestamp so the calendar stores the right moment regardless
    # of its own timezone.
    rel = _relative_delta_from_text(text)
    if rel is not None:
        now = datetime.now(timezone.utc)
        start_dt, end_dt = now + rel, now + rel + timedelta(hours=1)
        return _create_calendar_event(
            client, admin, summary=title,
            start_local=start_dt.strftime("%Y-%m-%dT%H:%M:00+00:00"),
            end_local=end_dt.strftime("%Y-%m-%dT%H:%M:00+00:00"),
            email=email, meet=meet, person=person,
            when_label=f"через {_human_delta(rel)}",
        )

    today = datetime.now(timezone.utc).date()
    ds = (intent.get("due_date") or "").strip()[:10]
    try:
        d = _date.fromisoformat(ds)
    except ValueError:
        d = None
    if d is None:
        d = _cal_date_from_text(text, today) or today

    rng = _cal_range_from_text(text)
    hm = (
        _cal_hm(intent.get("start_time"))
        or _cal_time_from_text(text)
        or (rng[0] if rng else None)
    )
    endhm = _cal_hm(intent.get("end_time")) or (rng[1] if rng else None)

    if hm is None:
        _pending_event[admin] = {
            "date": d.isoformat(), "title": title, "email": email,
            "meet": meet, "person": person, "endhm": endhm, "ts": _time.time(),
        }
        client.send_message(
            admin,
            f"📅 Готую подію «{_esc(title)}» на {d.isoformat()}. "
            "О котрій? Напиши час, напр. «о 15:00».",
        )
        return True

    start_local, end_local, when = _build_local_window(d, hm, endhm)
    return _create_calendar_event(
        client, admin, summary=title, start_local=start_local,
        end_local=end_local, email=email, meet=meet, person=person,
        when_label=when,
    )


def _request_timereport(client, admin: int) -> None:
    from app.modules.timereport import service as tr

    with SessionLocal() as db:
        tr.request_report(db, "week")
    client.send_message(
        admin,
        "🔄 Запросив трекінг часу з ПК. Якщо комп'ютер увімкнений — звіт із "
        "AI-аналізом (куди йшов час і що не в фокусі цілей) прийде за 1–5 хв.\n"
        "Якщо ПК вимкнений — прийде, щойно його увімкнеш.",
    )


def _looks_like_timereport(text: str) -> bool:
    t = text.lower()
    if "трекінг" in t or "трекинг" in t:
        return True
    time_word = any(w in t for w in ("час", "часу", "продуктивн", "ефективн"))
    ask_word = any(
        w in t for w in ("статистик", "звіт", "куди", "скільки", "аналіз", "трек")
    )
    week_word = any(w in t for w in ("тижд", "тиждень", "тижня", "week"))
    return time_word and (ask_word or week_word)


_KIND_LABEL = {
    "watch": "🎬 Подивитись",
    "read": "📖 Прочитати",
    "tool": "🛠 Спробувати",
    "reference": "📎 Довідка",
    "other": "🔖 Інше",
}


def _handle_link_capture(client, admin: int, text: str) -> bool:
    """A message with a link → park it on the 'later' shelf instead of letting
    it rot in a browser tab. The rest of the text becomes the note (why)."""
    from app.modules.resources import service as res

    urls = res.extract_urls(text)
    if not urls:
        return False
    note = text
    for u in urls:
        note = note.replace(u, " ")
    note = " ".join(note.split()).strip(" -—:·") or None

    saved = []
    with SessionLocal() as db:
        for u in urls[:5]:
            existing = res.find_by_url(db, u)
            saved.append(existing or res.create_resource(db, url=u, note=note))
        lines = ["🔖 <b>Відклав на потім</b>"]
        for r in saved:
            lines.append(
                f"• [{r.id}] {_KIND_LABEL.get(r.kind.value, '')} "
                f"— {_esc(r.title)}"
            )
        lines.append("\n/later — список · відповідь на це повідомлення "
                     "(текстом чи голосом) додасть нотатку.")
        sent = client.send_message(admin, "\n".join(lines))
    if sent and saved:
        _resource_msgs[sent.get("message_id", 0)] = saved[0].id
        if len(_resource_msgs) > 200:
            for k in list(_resource_msgs)[:100]:
                _resource_msgs.pop(k, None)
    return True


def _handle_resource_note(client, admin: int, resource_id: int, note: str) -> None:
    """Reply to a capture confirmation → attach/extend the note and re-guess
    the kind from what the user just said."""
    from app.modules.resources import service as res

    with SessionLocal() as db:
        r = res.get_resource(db, resource_id)
        if r is None:
            return
        r.note = f"{r.note}\n{note}" if r.note else note
        r.kind = res.guess_kind(r.url, note)
        if r.title.startswith(("http", "www")) or " — " in r.title:
            r.title = res.title_from(None, note) or r.title
        db.commit()
        db.refresh(r)
        client.send_message(
            admin,
            f"📝 Записав: {_KIND_LABEL.get(r.kind.value, '')} — "
            f"<b>{_esc(r.title)}</b>",
        )


def _later_keyboard(items) -> dict:
    """One tap per row-of-numbers to close an item off."""
    row, rows = [], []
    for i, r in enumerate(items[:8], start=1):
        row.append({"text": f"✅ {i}", "callback_data": f"r:d:{r.id}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows} if rows else {}


def _send_later_list(client, admin: int, db, kind=None) -> None:
    from app.modules.resources import service as res

    items = res.list_open(db, kind=kind)
    if not items:
        client.send_message(
            admin,
            "🔖 Список порожній. Кинь сюди посилання (можна з поясненням, "
            "напр. «подивитись про воронки») — і воно чекатиме тут, а не у "
            "вкладках.",
        )
        return
    lines = [f"🔖 <b>На потім</b> — {len(items)}"]
    for i, r in enumerate(items[:8], start=1):
        age = res.age_days(r)
        age_s = "сьогодні" if age == 0 else f"{age}д тому"
        lines.append("")
        lines.append(
            f"<b>{i}.</b> {_KIND_LABEL.get(r.kind.value, '')} "
            f"— {_esc(r.title)} <i>({age_s})</i>"
        )
        if r.url:
            lines.append(_esc(r.url))
    if len(items) > 8:
        lines.append(f"\n…і ще {len(items) - 8}.")
    lines.append("\n✅ — позначити переглянутим.")
    client.send_message(admin, "\n".join(lines), reply_markup=_later_keyboard(items))


def _looks_like_idea(text: str) -> bool:
    """Keyword net for an idea dump — used only when the AI classifier didn't
    already tag it. Anchored at the start so a mid-sentence 'ідея' in a search
    query doesn't hijack it."""
    import re

    t = text.strip().lower()
    if re.match(
        r"(є\s+ідея|нова\s+ідея|ідея|думка|задум|brainstorm|брейншторм)\b", t
    ):
        return True
    return any(
        k in t for k in (
            "запиши ідею", "збережи ідею", "нова ідея",
            "продовж ідею", "продовжити ідею", "продовжуючи ідею",
            "додай до ідеї", "доповни ідею", "розвин ідею", "розвинь ідею",
            "до тієї ідеї", "до останньої ідеї",
        )
    )


def _is_idea_append(text: str) -> bool:
    t = text.lower()
    return any(
        k in t for k in (
            "продовж", "додай до ідеї", "доповни ідею", "розвин ідею",
            "до тієї ідеї", "до останньої ідеї", "продовжуючи ідею",
        )
    )


def _handle_idea(client, admin: int, text: str) -> bool:
    """Capture a free-form idea, or grow the latest one ('продовж ідею …')."""
    import re

    from app.modules.ideas import service as ideas

    text = (text or "").strip()
    with SessionLocal() as db:
        if _is_idea_append(text):
            idea = ideas.latest_active(db)
            if idea is not None:
                extra = re.sub(
                    r"^\s*(продовжуючи\s+ідею|продовж\w*\s+ідею|додай\s+до\s+"
                    r"ідеї|доповни\s+ідею|розвин\w*\s+ідею|до\s+(?:тієї|"
                    r"останньої)\s+ідеї)[\s:—-]*",
                    "", text, flags=re.I,
                )
                ideas.append_to_idea(db, idea, extra or text)
                client.send_message(
                    admin,
                    f"➕ Доповнив ідею «{_esc(idea.title)}» — тепер "
                    f"{len(idea.body)} символів.\n/idea {idea.id} — відкрити.",
                )
                return True
        idea = ideas.create_idea(db, text)
        client.send_message(
            admin,
            f"💡 Записав ідею: «{_esc(idea.title)}» ({len(idea.body)} симв.).\n"
            f"/ideas — список · /idea {idea.id} — повністю · «продовж ідею …» "
            "— доповнити.",
        )
    return True


#: Явні маркери «заведи задачу» — щоб це працювало і без AI.
_TASK_CAPTURE_RE = _re.compile(
    r"\b(?:впиши|впісши|додай|запиши|заведи|створи)\s+задач\w*", _re.IGNORECASE
)


def _looks_like_task_capture(text: str) -> bool:
    return bool(_TASK_CAPTURE_RE.search(text or ""))


def _task_title_from_text(text: str) -> str:
    """Прибирає службовий початок і хвіст про періодичність/відповідального."""
    out = _TASK_CAPTURE_RE.sub("", text or "", count=1)
    out = _re.sub(r"^\s*[:\-–—,]\s*", "", out)
    out = _re.sub(
        r"[,;]?\s*(?:відповідальн\w+|нагадуй|нагадувати|періодичн\w+)\b.*$",
        "",
        out,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    out = " ".join(out.split()).strip(" .,:;-")
    # «платити за домен щороку» -> «платити за домен»: сама періодичність
    # живе в окремому полі, у назві вона зайва.
    from app.modules.tasks.service import parse_recur

    found = parse_recur(out)
    if found:
        out = _re.sub(_re.escape(found[0]) + r"\s*$", "", out, flags=_re.IGNORECASE)
    return out.strip(" .,:;-")


def _task_owner_from_text(text: str) -> str:
    m = _re.search(
        r"відповідальн\w*\s*[:\-–—]?\s*([A-ZА-ЯЇІЄҐ][\w'ʼ-]+(?:\s+[A-ZА-ЯЇІЄҐ][\w'ʼ-]+)?)",
        text or "",
    )
    return m.group(1).strip() if m else ""


def _handle_task_capture(client, admin: int, intent: dict, text: str) -> bool:
    """«Впиши задачу: X, відповідальний Y, нагадуй раз на місяць».

    Задача заводиться завжди; якщо періодичність не названа — бот питає її
    окремим повідомленням і чекає на відповідь, бо без неї нагадувань не буде.
    """
    from app.modules.coach import service as coach_service
    from app.modules.tasks import service as tasks_service

    title = (intent.get("text") or "").strip() or _task_title_from_text(text)
    if not title:
        return False
    owner = (intent.get("person") or "").strip() or _task_owner_from_text(text)
    parsed = tasks_service.parse_recur(intent.get("recurrence") or "") or \
        tasks_service.parse_recur(text)

    with SessionLocal() as db:
        task = tasks_service.create_reminder_task(
            db,
            title=title,
            counterpart=owner or None,
            recur=parsed[0] if parsed else None,
            recur_days=parsed[1] if parsed else None,
        )
        who = f", відповідальний: {owner}" if owner else ""
        if parsed:
            client.send_message(
                admin,
                f"Задача: {_esc(task.title)}{_esc(who)}\n"
                f"Нагадуватиму {_esc(parsed[0])}, наступне "
                f"{task.next_remind_at:%d.%m.%y}.",
            )
            return True
        question = (
            f"Задача: {_esc(task.title)}{_esc(who)}\n"
            "Як часто нагадувати? Щодня, раз на тиждень, раз на місяць, "
            "раз на квартал?"
        )
        coach_service.ask_task_frequency(db, task, question)
    client.send_message(admin, question)
    return True



_MAIL_URGENCY = {"high": "терміново", "normal": "звичайне", "low": "може почекати"}


def _mail_keyboard(thread_pk: int, has_draft: bool) -> dict:
    row = []
    if has_draft:
        row.append({"text": "Надіслати", "callback_data": f"m:s:{thread_pk}"})
        row.append({"text": "Переписати", "callback_data": f"m:d:{thread_pk}"})
    else:
        row.append({"text": "Скласти чернетку", "callback_data": f"m:d:{thread_pk}"})
    row.append({"text": "Пропустити", "callback_data": f"m:x:{thread_pk}"})
    return {"inline_keyboard": [row]}


def _mail_card(client, admin: int, db, thread) -> None:
    """Один лист із чернеткою і кнопками."""
    from app.modules.inbox import service as inbox

    who = thread.from_name or thread.from_email
    days = inbox.age_days(thread)
    age = "сьогодні" if days == 0 else f"{days} дн тому"
    lines = [
        f"<b>{_esc(thread.subject)}</b>",
        f"від {_esc(who)} · {age} · {_MAIL_URGENCY.get(thread.urgency, thread.urgency)}",
    ]
    if thread.topic:
        lines.append(_esc(thread.topic))
    if thread.draft_text:
        lines += ["", "<b>Чернетка:</b>", _esc(thread.draft_text)]
        if thread.draft_id:
            lines.append("")
            lines.append("Вона вже лежить у чернетках Gmail.")
    else:
        lines += ["", _esc((thread.snippet or "")[:400])]
    client.send_message(
        admin, "\n".join(lines), reply_markup=_mail_keyboard(thread.id, bool(thread.draft_text))
    )


def _handle_mail_callback(client, cb_id: str, data: str, admin: int | None) -> None:
    from app.modules.inbox import service as inbox

    parts = data.split(":")
    try:
        action, thread_pk = parts[1], int(parts[2])
    except (IndexError, ValueError):
        client.answer_callback(cb_id)
        return

    with SessionLocal() as db:
        thread = inbox.get(db, thread_pk)
        if thread is None:
            client.answer_callback(cb_id, "Листа вже немає")
            return

        if action == "x":
            inbox.set_status(db, thread, "ignored")
            client.answer_callback(cb_id, "Прибрав з черги")
            return

        if action == "d":
            client.answer_callback(cb_id, "Пишу чернетку...")
            thread = inbox.make_draft(db, thread)
            if not thread.draft_text:
                client.send_message(admin, "Не вдалося скласти чернетку.")
                return
            _mail_card(client, admin, db, thread)
            return

        if action == "s":
            if not thread.draft_text:
                client.answer_callback(cb_id, "Немає чернетки")
                return
            sent = inbox.send_reply(db, thread)
        client.answer_callback(cb_id, "Надіслано" if sent else "Не вдалося надіслати")


def _handle_assistant_intent(client, admin: int, text: str) -> bool:
    """Nexus-style: interpret a free-text message as a command over a contact
    (reminder / note / cadence / importance) and act on it. Returns True when
    handled; False means 'not a command' → caller falls back to search."""
    import time as _time
    from datetime import datetime, timezone

    from app.modules.insights import ai as _ai

    # Waiting on a time to finish a calendar event? A bare time reply ends it.
    if admin in _pending_event and _is_bare_time(text):
        if _time.time() - _pending_event[admin].get("ts", 0) <= _PENDING_TTL:
            hm = _cal_hm(text.strip()) or _cal_time_from_text(text)
            if hm:
                return _complete_pending_event(client, admin, hm)
        else:
            _pending_event.pop(admin, None)

    now = datetime.now(timezone.utc)
    today = f"{now.date().isoformat()}, {_WEEKDAYS_UK[now.weekday()]}"
    intent = _ai.parse_assistant_intent(text, today) or {}
    action = intent.get("action")
    if action == "idea":
        return _handle_idea(client, admin, text)
    if action == "task" or _looks_like_task_capture(text):
        return _handle_task_capture(client, admin, intent, text)
    if action not in ("remind", "note", "cadence", "importance"):
        # Not a contact command. Calendar / idea may have no contact at all —
        # route them here, with keyword nets so they still work when the AI
        # under-classifies or its JSON call failed.
        if action == "calendar" or _looks_like_calendar(text):
            return _handle_calendar_event(client, admin, intent, text)
        if _looks_like_idea(text):
            return _handle_idea(client, admin, text)
        return False

    person = (intent.get("person") or "").strip()
    with SessionLocal() as db:
        contact = service.find_contact_by_name(db, person) if person else None
        if contact is None:
            # Couldn't resolve the person → let network search try instead.
            return False

        if action == "remind":
            due = _due_from_iso(intent.get("due_date", ""))
            if due is None:
                return False
            from app.modules.automation import reminders as _reminders

            what = (intent.get("text") or "").strip() or (
                f"написати {contact.full_name}"
            )
            _reminders.create_reminder(db, contact, what, due)
            client.send_message(
                admin,
                f"⏰ Нагадаю: <b>{_esc(what)}</b>\n"
                f"👤 {_contact_link(contact)} · {due.date().isoformat()}",
            )
            return True

        if action == "note":
            note = (intent.get("text") or "").strip()
            if not note:
                return False
            from datetime import datetime as _dt, timezone as _tz

            stamp = _dt.now(_tz.utc).strftime("%d.%m.%Y")
            contact.notes = (
                f"{contact.notes}\n[{stamp}] {note}"
                if contact.notes
                else f"[{stamp}] {note}"
            )
            db.commit()
            client.send_message(
                admin,
                f"📝 Записав до <b>{_contact_link(contact)}</b>:\n{_esc(note)}",
            )
            return True

        if action == "cadence":
            from app.modules.contacts.models import Frequency

            freq = (intent.get("frequency") or "").strip()
            try:
                contact.contact_frequency = Frequency(freq)
            except ValueError:
                return False
            from app import warmth

            warmth.refresh(contact)
            db.commit()
            client.send_message(
                admin,
                f"🔁 Каденція для <b>{_contact_link(contact)}</b>: "
                f"{_esc(freq)}.",
            )
            return True

        if action == "importance":
            imp = int(intent.get("importance") or 0)
            if imp not in (1, 2, 3):
                return False
            contact.importance = imp
            db.commit()
            stars = "⭐" * imp
            client.send_message(
                admin,
                f"{stars} Важливість <b>{_contact_link(contact)}</b>: {imp}/3.",
            )
            return True

    return False


def _handle_network_search(client, admin: int, query: str) -> None:
    from app import crud
    from app.modules.insights import ai as _ai

    from app.modules.search import service as search_service

    with SessionLocal() as db:
        # Semantic first: rank by embedding, then narrow the AI 'why' to the
        # top candidates (cheaper + better than dumping the whole network).
        hits = search_service.semantic_search(db, query, k=12)
        if hits:
            by_id = {cid: crud.get_contact(db, cid) for cid, _ in hits}
            by_id = {k: v for k, v in by_id.items() if v is not None}
            corpus = []
            for cid, _score in hits:
                c = by_id.get(cid)
                if c is None:
                    continue
                facts = "; ".join(
                    f.value for f in getattr(c, "facts", []) if f.is_current
                )[:200]
                corpus.append(
                    f"{c.id} | {c.full_name} | "
                    f"{' · '.join(filter(None, [c.position, c.company]))} | "
                    f"{c.location or ''} | {', '.join(t.name for t in c.tags)} | "
                    f"{facts} | {(c.notes or '')[:120]}"
                )
            matches = _ai.search_network(query, corpus)
            if matches is None:
                # AI off → return the semantic ranking directly.
                lines = [f"🔎 <b>«{_esc(query)}»</b> (семантичний пошук)"]
                for cid, _score in hits[:8]:
                    c = by_id.get(cid)
                    if c:
                        lines.append(f"• {_contact_link(c)}")
                client.send_message(admin, "\n".join(lines))
                return
            _render_search_matches(client, admin, query, matches, by_id)
            return

        # No embeddings yet → whole-network corpus (or substring if AI off).
        contacts = crud.list_contacts(db, sort="name")
        corpus = []
        by_id = {}
        for c in contacts:
            by_id[c.id] = c
            facts = "; ".join(
                f.value for f in getattr(c, "facts", []) if f.is_current
            )[:200]
            corpus.append(
                f"{c.id} | {c.full_name} | "
                f"{' · '.join(filter(None, [c.position, c.company]))} | "
                f"{c.location or ''} | {', '.join(t.name for t in c.tags)} | "
                f"{facts} | {(c.notes or '')[:120]}"
            )
        matches = _ai.search_network(query, corpus)
        if matches is None:
            # AI off → plain substring search as a fallback.
            found = crud.list_contacts(db, search=query, sort="name")
            if not found:
                client.send_message(admin, f"Нічого не знайшов за «{_esc(query)}».")
                return
            lines = [f"<b>Збіги за «{_esc(query)}»</b> (простий пошук)"]
            for c in found[:10]:
                lines.append(f"• {_contact_link(c)}")
            client.send_message(admin, "\n".join(lines))
            return
        _render_search_matches(client, admin, query, matches, by_id)


def _render_search_matches(client, admin, query, matches, by_id):
    if not matches:
        client.send_message(admin, f"У мережі не знайшов нікого під «{_esc(query)}».")
        return
    lines = [f"🔎 <b>«{_esc(query)}»</b>"]
    for m in matches[:8]:
        c = by_id.get(m.get("contact_id"))
        if c is None:
            continue
        role = " · ".join(filter(None, [c.position, c.company]))
        lines.append("")
        lines.append(
            f"👤 <b>{_contact_link(c)}</b>" + (f" — {_esc(role)}" if role else "")
        )
        lines.append(f"<i>{_esc(m.get('why', ''))}</i>")
    client.send_message(admin, "\n".join(lines))


def _fmt_date(dt) -> str:
    from datetime import timezone

    if dt is None:
        return "?"
    if getattr(dt, "tzinfo", None) is None and hasattr(dt, "date"):
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%d.%m.%Y")


def _timeline_text(db, contact) -> str:
    """Everything we know about the relationship, in one glance: history
    endpoints, open reminders, live facts, recent life events, notes."""
    from app import warmth
    from app.modules.automation import reminders as _reminders
    from app.modules.telegram_bot.service import effective_importance

    role = " · ".join(filter(None, [contact.position, contact.company]))
    imp = effective_importance(contact)
    header = f"🧭 <b>{_contact_link(contact)}</b>"
    if role:
        header += f"\n💼 {_esc(role)}"
    header += (
        f"\n📶 {contact.warmth_status} ({round(contact.warmth_score)})"
        + (f" · {'⭐' * imp}" if imp else "")
    )
    lines = [header]

    itx = sorted(contact.interactions, key=lambda i: i.occurred_at)
    if itx:
        lines.append("")
        lines.append("<b>Історія</b>")
        first = itx[0]
        lines.append(
            f"• Перша: {_fmt_date(first.occurred_at)}"
            + (f" — {_esc((first.summary or '')[:80])}" if first.summary else "")
        )
        if len(itx) > 1:
            last = itx[-1]
            lines.append(
                f"• Остання: {_fmt_date(last.occurred_at)}"
                + (f" — {_esc((last.summary or '')[:80])}" if last.summary else "")
            )
        lines.append(f"• Всього дотиків: {len(itx)}")

    open_r = [r for r in _reminders.open_reminders(db) if r.contact_id == contact.id]
    if open_r:
        lines.append("")
        lines.append("<b>⏰ Нагадування</b>")
        for r in open_r[:5]:
            lines.append(f"• {_esc(r.text)} ({_fmt_date(r.due_at)})")

    facts = [f for f in getattr(contact, "facts", []) if f.is_current]
    if facts:
        lines.append("")
        lines.append("<b>Факти</b>")
        for f in facts[:8]:
            lines.append(f"• {_esc(f.value[:120])}")

    events = sorted(
        contact.life_events, key=lambda e: e.created_at, reverse=True
    )
    if events:
        lines.append("")
        lines.append("<b>Події</b>")
        for e in events[:4]:
            lines.append(f"• {_esc(e.title)} ({_fmt_date(e.created_at)})")

    if contact.birth_date:
        lines.append("")
        lines.append(f"🎂 День народження: {contact.birth_date.strftime('%d.%m')}")

    if contact.notes:
        lines.append("")
        lines.append("<b>Нотатки</b>")
        lines.append(f"<i>{_esc(contact.notes[-400:])}</i>")

    return "\n".join(lines)


def _handle_command(client, admin: int, text: str) -> None:
    from app import crud, dashboard, models, warmth
    from app.modules.automation import telegram as tg

    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower().lstrip("/").split("@")[0]

    with SessionLocal() as db:
        if cmd in ("start", "help", "menu"):
            client.send_message(
                admin,
                "<b>Networking AI</b>\n"
                "/queue — почати обхід (кому написати, з чернетками)\n"
                "/next — що робити прямо зараз (одна задача + кнопки)\n"
                "/tasks — усі задачі в порядку виконання\n"
                "/goals — трекер цілей: цифри і що в роботі\n"
                "/goal — питання коуча по одній цілі просто зараз\n"
                "/focus — топ-3 найважливіші цілі без руху\n"
                "/week — підсумок тижня просто зараз\n"
                "/inbox — пошта, що чекає на відповідь\n"
                "/mail &lt;№&gt; — лист із готовою чернеткою\n"
                "/gsync — оновити задачі в Google Календарі\n"
                "/embed — проіндексувати мережу для розумного пошуку\n"
                "/enrich — підтягнути юзернейми/дні народження з Telegram\n"
                "/today — дайджест дня\n"
                "/upcoming — що попереду (дні народження, події)\n"
                "/birthdays — найближчі дні народження\n"
                "/events — що нового в людей\n"
                "/reconnect — з ким варто відновити звʼязок\n"
                "/reflect — подумати про стосунки (AI)\n"
                "/reminders — активні нагадування\n"
                "/due — всі прострочені\n"
                "/find &lt;ім'я&gt; — пошук за іменем\n"
                "/timeline &lt;ім'я&gt; — вся історія стосунку\n"
                "/similar &lt;ім'я&gt; — схожі та повʼязані люди\n"
                "/pause &lt;ім'я&gt; — я відповідаю цьому сам (без чернеток)\n"
                "/resume &lt;ім'я&gt; — повернути авто-відповіді\n"
                "/paused — хто на паузі\n"
                "/time — трекінг часу за тиждень (з ПК) + AI-аналіз\n"
                "/ideas — мої ідеї · /idea &lt;№&gt; — відкрити\n"
                "/later — посилання «на потім» (кинь сюди лінк)\n"
                "/stalled — проєкти без наступної дії\n"
                "/contexts — контексти · /next @дзвінки — що можу зараз\n"
                "/review — тижневий огляд одним ритуалом\n"
                "/activity — що робив бот + стан системи\n\n"
                "🤖 Пиши або надиктовуй боту — він сам розбере намір:\n"
                "• кинь посилання (+ «подивитись про воронки») → у /later\n"
                "• «є ідея зробити …» → збережу як ідею (можна довго й багато)\n"
                "• «продовж ідею …» → доповню останню\n"
                "• «нагадай написати Олегу через 3 тижні»\n"
                "• «запиши до Марії, що вона переїхала в Берлін»\n"
                "• «додай зустріч на сьогодні о 14:00 з Сергієм»\n"
                "• «зустріч через годину через Google Meet»\n\n"
                "💬 Або постав питання — пошук по мережі "
                "(«хто з моїх шарить у крипті?»)\n"
                "🎤 Голосове — запишу в базу («познайомився з Андрієм, "
                "робить фінтех...»)\n\n"
                "Вхідні з Telegram Business приходять сюди з чернеткою "
                "відповіді: ✅ надіслати · reply — скоригувати.",
                reply_markup=_REMOVE_KEYBOARD,
            )
        elif cmd in ("queue", "obhid"):
            _send_next_outreach_card(client, db, admin)
        elif cmd == "enrich":
            pending = service.pending_enrichment(db, limit=25)
            if not pending:
                client.send_message(
                    admin, "Усі доступні контакти вже мають юзернейми ✅"
                )
                return
            done = 0
            for c in pending:
                try:
                    if service.enrich_from_telegram(db, client, c):
                        done += 1
                except Exception:
                    pass
            remaining = len(service.pending_enrichment(db, limit=1000))
            client.send_message(
                admin,
                f"🔄 Оновив {done} із {len(pending)} (username/ДН/біо з Telegram). "
                f"Ще без юзернейма: ~{remaining}. Запусти /enrich ще раз для наступної пачки."
                + ("" if done else "\n\n⚠️ Нічого не витягнулось — можливо, getChat "
                   "не бачить цих людей (бот з ними ще не взаємодіяв). "
                   "Тоді юзернейми підтягнуться самі, коли вони тобі напишуть."),
            )
        elif cmd == "embed":
            from app.modules.search import service as search_service
            from app.modules.insights import llm as _llm

            if not _llm.embeddings_enabled():
                client.send_message(
                    admin,
                    "Ембединги вимкнені — додай OPENAI_API_KEY (або VOYAGE_API_KEY) у .env.",
                )
                return
            done = search_service.embed_pending(db, limit=200)
            remaining = search_service.pending_count(db)
            client.send_message(
                admin,
                f"🧠 Проіндексував {done}. Ще без індексу: ~{remaining}. "
                + ("Готово — семантичний пошук увімкнено." if not remaining
                   else "Запусти /embed ще раз для наступної пачки."),
            )
        elif cmd == "goals":
            from app.modules.coach import service as coach_service
            from app.modules.goals.service import active_goals

            goals = active_goals(db)
            if not goals:
                client.send_message(admin, "Немає активних цілей. Створи у веб-інтерфейсі (Цілі).")
                return
            data = coach_service.board(db)
            lines = ["🎯 <b>Активні цілі</b>"]
            if data["theme"]:
                lines.append(_esc(data["theme"]))
            lines.append(
                f"Pers {data['pers']['done']}/{data['pers']['total']} · "
                f"Work {data['work']['done']}/{data['work']['total']} · "
                f"разом done {data['all']['done_pct']}%"
            )
            lines.append("")
            for g in goals:
                touched = coach_service.last_touch(g)
                tail = f" · {touched:%d.%m}" if touched else " · без записів"
                lines.append(f"• {_esc(g.title)} — {g.status}, {g.priority}{tail}")
            client.send_message(admin, "\n".join(lines))
        elif cmd == "goal":
            from app.modules.coach.jobs import send_question

            if not send_question(force=True):
                client.send_message(
                    admin,
                    "Питати нема про що: немає активних цілей або не "
                    "налаштований Telegram.",
                )
        elif cmd in ("contexts", "context", "ctx"):
            from app.modules.tasks import service as tasks_service

            counts = tasks_service.list_contexts(db)
            if not counts:
                client.send_message(
                    admin,
                    "Контекстів ще немає. Додай задачі тег на кшталт "
                    "<code>@дзвінки</code> — і зможеш питати «що я можу "
                    "зробити прямо зараз» через /next @дзвінки.",
                )
                return
            lines = ["🧭 <b>Контексти</b> — де це можна зробити"]
            for ctx, n in counts.items():
                lines.append(f"• {_esc(ctx)} — {n}")
            lines.append("\n/next &lt;контекст&gt; — перша задача звідти.")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("next", "task"):
            from app.modules.tasks import service as tasks_service

            if arg.strip():
                ctx = tasks_service.normalize_context(arg)
                in_ctx = tasks_service.tasks_in_context(db, arg)
                if not in_ctx:
                    client.send_message(
                        admin,
                        f"У контексті {_esc(ctx or arg)} задач немає. "
                        "/contexts — що доступно.",
                    )
                    return
                task = in_ctx[0]
                client.send_message(
                    admin,
                    f"🎯 <b>Зараз</b> · {_esc(ctx or arg)}"
                    + (f" (ще {len(in_ctx) - 1})" if len(in_ctx) > 1 else "")
                    + "\n\n" + _esc(task.title) + "\n" + _task_meta(task),
                    reply_markup={"inline_keyboard": [[
                        {"text": "✓ Готово", "callback_data": f"tk:done:{task.id}"},
                        {"text": "▶ Взяв у роботу", "callback_data": f"tk:cur:{task.id}"},
                    ]]},
                )
                return
            task = tasks_service.next_task(db)
            if task is None:
                client.send_message(admin, "Задач немає — усе закрито 🎉")
                return
            client.send_message(
                admin,
                "🎯 <b>Зараз</b>\n\n" + _esc(task.title) + "\n" + _task_meta(task),
                reply_markup={"inline_keyboard": [[
                    {"text": "✓ Готово", "callback_data": f"tk:done:{task.id}"},
                    {"text": "▶ Взяв у роботу", "callback_data": f"tk:cur:{task.id}"},
                ]]},
            )
        elif cmd == "tasks":
            from app.modules.tasks import service as tasks_service

            if arg.strip():
                tasks = tasks_service.tasks_in_context(db, arg)
                head = f"📋 <b>Задачі</b> · {_esc(tasks_service.normalize_context(arg) or arg)}"
            else:
                tasks = tasks_service.list_tasks(db)
                head = "📋 <b>Задачі</b> — у порядку виконання"
            if not tasks:
                client.send_message(admin, "Задач немає — усе закрито 🎉")
                return
            lines = [head]
            for t in tasks[:15]:
                lines.append(f"• {_esc(t.title)} — {_task_meta(t)}")
            client.send_message(admin, "\n".join(lines))
        elif cmd == "focus":
            from datetime import date

            from app.modules.coach import service as coach_service
            from app.modules.goals.service import active_goals

            goals = active_goals(db)
            if not goals:
                client.send_message(admin, "Активних цілей немає.")
                return
            # Найпріоритетніші з тих, що найдовше без руху — саме вони тихо
            # провалюються, поки увага йде на дрібне і термінове.
            goals.sort(
                key=lambda g: (
                    coach_service.last_touch(g) or date.min,
                    -g.priority,
                )
            )
            lines = ["🎯 <b>Фокус</b> — найважливіше без руху"]
            for g in goals[:3]:
                touched = coach_service.last_touch(g)
                since = f"тиша з {touched:%d.%m}" if touched else "жодного запису"
                lines.append(f"• {_esc(g.title)} — {g.priority}, {g.status}, {since}")
            client.send_message(admin, "\n".join(lines))
        elif cmd == "week":
            from app.modules.coach.jobs import weekly_text

            client.send_message(admin, weekly_text(db))
        elif cmd in ("inbox", "mail", "email"):
            from app.modules.inbox import service as inbox

            if arg.strip().isdigit():
                thread = inbox.get(db, int(arg.strip()))
                if thread is None:
                    client.send_message(admin, "Такого листа немає в черзі.")
                    return
                _mail_card(client, admin, db, thread)
                return
            rows = inbox.waiting(db)
            if not rows:
                client.send_message(admin, "Пошта розібрана — нічого не чекає.")
                return
            st = inbox.stats(db)
            lines = [
                f"📬 <b>Чекають відповіді: {st['waiting']}</b>"
                + (f" · термінових {st['urgent']}" if st["urgent"] else "")
            ]
            for t in rows[:8]:
                days = inbox.age_days(t)
                age = "сьогодні" if days == 0 else f"{days} дн"
                mark = "!" if t.urgency == "high" else "·"
                draft = " ✍" if t.draft_text else ""
                who = t.from_name or t.from_email
                lines.append(
                    f"{mark} <code>{t.id}</code> {_esc(who)} — "
                    f"{_esc(t.topic or t.subject)} ({age}){draft}"
                )
            lines.append("")
            lines.append("<code>/mail &lt;номер&gt;</code> — відкрити з чернеткою")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("gsync", "calendar"):
            from app.modules.tasks.gsync import sync as run_gsync

            report = run_gsync(db)
            if report.get("skipped"):
                client.send_message(
                    admin,
                    "Google не підключений. Зайди в Integrations і підключи його.",
                )
                return
            client.send_message(
                admin,
                f"Задачі в Google: надіслано {report['pushed']}, "
                f"закрито з телефона {report['completed_from_google']}, "
                f"відвʼязано {report['unlinked']}.",
            )
        elif cmd == "overdue":
            _handle_command(client, admin, "/due")
        elif cmd in ("today", "network", "digest", "weekly", "monthly"):
            client.send_message(
                admin,
                tg.digest_text(dashboard.build_dashboard(db)),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🚀 Почати обхід", "callback_data": "q:start"}]
                    ]
                },
            )
        elif cmd == "due":
            contacts = [c for c in crud.list_contacts(db, sort="due") if warmth.is_due(c)]
            if not contacts:
                client.send_message(admin, "Ніхто не прострочений ✦")
                return
            lines = ["<b>Прострочені</b>"]
            for c in contacts[:20]:
                d = round(warmth.days_since_last_contact(c))
                lines.append(f"• {_esc(c.full_name)} — {d}д ({c.warmth_status})")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("upcoming", "naperec", "ahead"):
            data = dashboard.build_dashboard(db)
            if not data.upcoming:
                client.send_message(admin, "Найближчим часом — жодних дат попереду ✦")
                return
            lines = ["🔮 <b>Наперед</b>"]
            for u in data.upcoming:
                when = "сьогодні" if u.days_away == 0 else f"через {u.days_away}д"
                label = (
                    "🎂 день народження" if u.label == "Birthday" else _esc(u.label)
                )
                lines.append(f"• {_esc(u.contact.full_name)} — {label} ({when})")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("birthdays", "bdays", "dr"):
            data = dashboard.build_dashboard(db)
            bdays = [u for u in data.upcoming if u.label == "Birthday"]
            if not bdays:
                client.send_message(admin, "Найближчим часом днів народження немає 🎂")
                return
            lines = ["🎂 <b>Дні народження</b>"]
            for u in bdays:
                when = "сьогодні! 🎉" if u.days_away == 0 else f"через {u.days_away}д"
                lines.append(f"• {_esc(u.contact.full_name)} — {when}")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("events", "updates"):
            from sqlalchemy import select

            events = list(
                db.scalars(
                    select(models.LifeEvent)
                    .where(models.LifeEvent.status == models.LifeEventStatus.new)
                    .order_by(models.LifeEvent.created_at.desc())
                )
            )
            if not events:
                client.send_message(admin, "Нових оновлень по людях поки немає ✦")
                return
            lines = ["📰 <b>Що нового в людей</b>"]
            for e in events[:20]:
                who = e.contact.full_name if e.contact else "?"
                lines.append(f"• <b>{_esc(who)}</b> — {_esc(e.title)}")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("reconnect", "reconnects"):
            from app.modules.telegram_bot.service import effective_importance

            due = [
                c
                for c in crud.list_contacts(db, sort="due")
                if warmth.is_due(c) and not c.do_not_contact
            ]
            if not due:
                client.send_message(admin, "Нікого повторно піймати не треба зараз ✦")
                return
            due.sort(
                key=lambda c: (effective_importance(c), warmth.overdue_ratio(c)),
                reverse=True,
            )
            lines = ["🔁 <b>Варто відновити звʼязок</b>"]
            for c in due[:15]:
                d = round(warmth.days_since_last_contact(c))
                star = "⭐ " if effective_importance(c) >= 3 else ""
                lines.append(f"• {star}{_esc(c.full_name)} — {d}д ({c.warmth_status})")
            client.send_message(
                admin,
                "\n".join(lines),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🚀 Почати обхід", "callback_data": "q:start"}]
                    ]
                },
            )
        elif cmd in ("reminders", "remind"):
            from app.modules.automation import reminders as _reminders

            open_r = _reminders.open_reminders(db)
            if not open_r:
                client.send_message(
                    admin,
                    "Активних нагадувань немає.\n\n"
                    "Постав природною мовою: «нагадай написати Олегу через "
                    "3 тижні».",
                )
                return
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            lines = ["⏰ <b>Нагадування</b>"]
            for r in open_r[:20]:
                due = r.due_at if r.due_at.tzinfo else r.due_at.replace(tzinfo=timezone.utc)
                days = (due.date() - now.date()).days
                when = (
                    "сьогодні"
                    if days == 0
                    else (f"прострочено на {-days}д" if days < 0 else f"через {days}д")
                )
                who = r.contact.full_name if r.contact else "—"
                lines.append(f"• {_esc(r.text)} — <b>{_esc(who)}</b> ({when})")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("review", "ohliad", "weekreview"):
            from app.modules.automation import reviews as _rev

            text_out = _rev.full_review_text(db)
            for i in range(0, len(text_out), 3800):
                client.send_message(admin, text_out[i : i + 3800])
        elif cmd in ("stalled", "stuck", "zastriali"):
            from app.modules.projects import service as _proj

            stalled = _proj.stalled_projects(db)
            if not stalled:
                client.send_message(
                    admin,
                    "✅ У кожного активного проєкту є наступна дія. Так тримати.",
                )
                return
            lines = ["⚠️ <b>Проєкти без наступної дії</b>"]
            for prj, reason in stalled[:15]:
                why = (
                    "усе чекає на когось"
                    if reason == "waiting"
                    else "жодної відкритої задачі"
                )
                lines.append(f"• <b>{_esc(prj.name)}</b> — <i>{why}</i>")
            lines.append(
                "\nЯка найближча конкретна дія по кожному? Поки її немає — "
                "проєкт стоїть."
            )
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("later", "read", "watch", "links"):
            from app.modules.resources.models import ResourceKind

            kind = None
            a = arg.strip().lower()
            if cmd == "watch" or a.startswith("вiдео") or a.startswith("відео"):
                kind = ResourceKind.watch
            elif cmd == "read" or a.startswith("читат"):
                kind = ResourceKind.read
            _send_later_list(client, admin, db, kind=kind)
        elif cmd in ("ideas", "idea"):
            from app.modules.ideas import service as ideas

            arg_s = arg.strip()
            if arg_s.isdigit():
                idea = ideas.get_idea(db, int(arg_s))
                if idea is None:
                    client.send_message(admin, "Немає такої ідеї.")
                    return
                header = f"💡 <b>{_esc(idea.title)}</b>\n\n"
                body = _esc(idea.body)
                first = True
                for i in range(0, len(body), 3500):
                    chunk = body[i : i + 3500]
                    client.send_message(admin, (header + chunk) if first else chunk)
                    first = False
                return
            items = ideas.list_ideas(db)
            if not items:
                client.send_message(
                    admin,
                    "Ідей поки немає. Просто напиши або надиктуй «є ідея …» — "
                    "і я збережу. «Продовж ідею …» — доповнити останню.",
                )
                return
            lines = ["💡 <b>Ідеї</b>"]
            for i in items[:25]:
                lines.append(f"• [{i.id}] {_esc(i.title)}")
            lines.append("\n/idea &lt;номер&gt; — відкрити повністю.")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("time", "track", "tracking", "chas"):
            _request_timereport(client, admin)
        elif cmd in ("activity", "status"):
            from datetime import datetime, timedelta, timezone
            from sqlalchemy import select

            now = datetime.now(timezone.utc)
            week = now - timedelta(days=7)

            def _aware(dt):
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

            all_contacts = list(db.scalars(select(models.Contact)).unique())
            new_c = [c for c in all_contacts if _aware(c.created_at) >= week]
            events = list(
                db.scalars(
                    select(models.LifeEvent).order_by(
                        models.LifeEvent.created_at.desc()
                    )
                )
            )
            recent_ev = [e for e in events if _aware(e.created_at) >= week]

            lines = ["🛠 <b>Активність бота</b>"]
            lines.append("")
            lines.append(f"👥 Нових контактів за тиждень: <b>{len(new_c)}</b>")
            for c in new_c[:5]:
                src = ", ".join(t.name for t in c.tags) or "—"
                lines.append(f"  • {_esc(c.full_name)} <i>({_esc(src)})</i>")
            lines.append(f"🎉 Нових подій за тиждень: <b>{len(recent_ev)}</b>")
            for e in recent_ev[:3]:
                who = e.contact.full_name if e.contact else "?"
                lines.append(f"  • {_esc(who)} — {_esc(e.title)}")

            from app.modules.automation import reminders as _reminders
            from app.modules.search import service as search_service
            from app.modules.insights import llm as _llm
            from app.integrations import google as _google

            open_r = len(_reminders.open_reminders(db))
            g = _google.status(db)
            prov = _llm.active_provider() or "вимкнено"
            pending = search_service.pending_count(db)

            lines.append("")
            lines.append("<b>Стан</b>")
            lines.append(f"📇 Всього контактів: {len(all_contacts)}")
            lines.append(f"⏰ Відкритих нагадувань: {open_r}")
            lines.append(f"🤖 AI-провайдер: {_esc(prov)}")
            lines.append(f"🧠 Не проіндексовано: {pending}")
            lines.append(
                "📧 Google: " + ("підключено ✅" if g.get("connected") else "ні")
            )
            lines.append(
                "✈️ Telegram: "
                + ("налаштовано ✅" if settings.telegram_configured else "ні")
            )
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("similar", "related"):
            if not arg.strip():
                client.send_message(admin, "Використання: /similar &lt;ім'я&gt;")
                return
            contact = service.find_contact_by_name(db, arg.strip())
            if contact is None:
                client.send_message(admin, f"Не знайшов контакт «{_esc(arg)}».")
                return
            from app.modules.search import service as search_service

            lines = [f"👥 <b>Схожі на {_esc(contact.full_name)}</b>"]
            hits = search_service.similar_contacts(db, contact.id, k=5)
            shown: set[int] = set()
            for cid, score in hits:
                c = crud.get_contact(db, cid)
                if c is None:
                    continue
                shown.add(c.id)
                role = " · ".join(filter(None, [c.position, c.company]))
                lines.append(
                    f"• {_contact_link(c)}" + (f" — {_esc(role)}" if role else "")
                )
            # Explicit links: same company or a shared goal (no AI needed).
            related = []
            if contact.company:
                for c in crud.list_contacts(db, sort="name"):
                    if (
                        c.id != contact.id
                        and c.id not in shown
                        and (c.company or "").strip().casefold()
                        == contact.company.strip().casefold()
                    ):
                        related.append((c, f"колега — {contact.company}"))
            for g in getattr(contact, "goals", []):
                for c in g.contacts:
                    if c.id != contact.id and c.id not in shown:
                        related.append((c, f"ціль — {g.title}"))
            if related:
                lines.append("")
                lines.append("<b>Прямі звʼязки</b>")
                seen: set[int] = set()
                for c, why in related[:8]:
                    if c.id in seen:
                        continue
                    seen.add(c.id)
                    lines.append(f"• {_contact_link(c)} — <i>{_esc(why)}</i>")
            if len(lines) == 1:
                lines.append(
                    "Поки нікого схожого. Проіндексуй мережу — /embed — "
                    "для семантичних збігів."
                )
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("reflect", "reflection"):
            from app.modules.automation import reviews

            contacts = reviews.pick_reflection_contacts(
                db, settings.reflection_count
            )
            if not contacts:
                client.send_message(admin, "Поки нема над чим рефлексувати ✦")
                return
            text = reviews.reflection_text(db, contacts)
            client.send_message(
                admin,
                text or "AI недоступний — рефлексія потребує ключа (OpenAI/Anthropic).",
            )
        elif cmd in ("pause", "resume"):
            if not arg.strip():
                client.send_message(
                    admin, f"Використання: /{cmd} &lt;ім'я&gt;"
                )
                return
            contact = service.find_contact_by_name(db, arg.strip())
            if contact is None:
                client.send_message(admin, f"Не знайшов контакт «{_esc(arg)}».")
                return
            contact.auto_reply_paused = cmd == "pause"
            db.commit()
            if cmd == "pause":
                client.send_message(
                    admin,
                    f"🔕 <b>{_contact_link(contact)}</b> — авто-відповіді на паузі. "
                    "Повідомлення логуються, чернетки не складаються. "
                    "Увімкнути назад: /resume.",
                )
            else:
                client.send_message(
                    admin,
                    f"▶️ <b>{_contact_link(contact)}</b> — авто-відповіді знову "
                    "увімкнені.",
                )
        elif cmd in ("paused", "muted"):
            from sqlalchemy import select

            paused = list(
                db.scalars(
                    select(models.Contact).where(
                        models.Contact.auto_reply_paused.is_(True)
                    )
                ).unique()
            )
            if not paused:
                client.send_message(
                    admin,
                    "Нікого на паузі. Постав: /pause &lt;ім'я&gt; — тоді бот "
                    "лише логуватиме їхні повідомлення, без чернеток.",
                )
                return
            lines = ["🔕 <b>На паузі (відповідаєш сам)</b>"]
            for c in paused:
                lines.append(f"• {_contact_link(c)}")
            client.send_message(admin, "\n".join(lines))
        elif cmd in ("timeline", "history"):
            if not arg.strip():
                client.send_message(admin, "Використання: /timeline &lt;ім'я&gt;")
                return
            contact = service.find_contact_by_name(db, arg.strip())
            if contact is None:
                client.send_message(admin, f"Не знайшов контакт «{_esc(arg)}».")
                return
            client.send_message(admin, _timeline_text(db, contact))
        elif cmd == "find":
            if not arg.strip():
                client.send_message(admin, "Використання: /find &lt;ім'я чи компанія&gt;")
                return
            results = crud.list_contacts(db, search=arg.strip(), sort="name")
            if not results:
                client.send_message(admin, f"Нічого не знайшов за «{_esc(arg)}».")
                return
            lines = [f"<b>Збіги за «{_esc(arg)}»</b>"]
            for c in results[:15]:
                sub = " · ".join(filter(None, [c.position, c.company]))
                lines.append(
                    f"• {_esc(c.full_name)}"
                    + (f" — {_esc(sub)}" if sub else "")
                    + f" ({c.warmth_status} {round(c.warmth_score)})"
                )
            client.send_message(admin, "\n".join(lines))
        else:
            client.send_message(admin, "Невідома команда. Спробуй /help.")


# ── Entry point ──────────────────────────────────────────────────────────────


def dispatch(client, update: dict) -> None:
    try:
        if "business_connection" in update:
            handle_business_connection(client, update["business_connection"])
        elif "business_message" in update:
            # Спершу питаємо ALiSiO, чи це її лід — до будь-якої власної відповіді.
            from app.modules.integrations import alisio

            handled = alisio.forward_update(update)
            handle_business_message(
                client, update["business_message"], alisio_handled=handled > 0
            )
        elif "callback_query" in update:
            handle_callback(client, update["callback_query"])
        elif "message" in update or "edited_message" in update:
            handle_admin_message(
                client, update.get("message") or update.get("edited_message")
            )
    except Exception as exc:  # pragma: no cover - resilience
        logger.exception("Update %s failed: %s", update.get("update_id"), exc)
