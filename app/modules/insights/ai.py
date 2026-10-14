"""Relationship intelligence, powered by whichever LLM provider is configured.

All model calls go through ``llm`` (Anthropic / OpenAI / Gemini with automatic
fallback). Every function degrades gracefully: if no provider is available (or
all fail) a deterministic rule-based fallback is returned so the product keeps
working. AI is an enhancement, never a hard dependency.
"""

from __future__ import annotations

import logging

from app import models, warmth

from . import llm

logger = logging.getLogger("networking.ai")


def _clean(out: str | None) -> str | None:
    """Trim wrapping quotes the model sometimes adds around a message."""
    if not out:
        return None
    return out.strip().strip('"«»') or None


import re as _re

_EMOJI_RE = _re.compile(
    "[\U0001f1e6-\U0001f1ff\U0001f300-\U0001faff\U00002600-\U000027bf"
    "\U00002b00-\U00002bff\U0000fe00-\U0000fe0f\U00002190-\U000021ff"
    "\U0000200d\U00002122\U00002139\U00002194-\U000021aa]+"
)


def _strip_emoji(out: str | None) -> str | None:
    """Remove emoji/pictographs from a drafted message and tidy the spacing —
    a hard guarantee that replies never carry emoji, regardless of the model."""
    if not out:
        return out
    out = _EMOJI_RE.sub("", out)
    out = _re.sub(r"[ \t]{2,}", " ", out)
    out = _re.sub(r" +([,.!?;:])", r"\1", out)
    return out.strip()


# ── Prompt context building ──────────────────────────────────────────────────


def _contact_context(contact: models.Contact) -> str:
    """A compact, AI-readable digest of everything known about a contact."""
    lines: list[str] = []
    # Досьє — першим: це скочене резюме всієї історії, найщільніший шар памʼяті.
    # Без нього модель бачила б лише останні кілька реплік і сирі поля.
    if contact.ai_dossier:
        lines.append(f"Dossier (consolidated memory):\n{contact.ai_dossier.strip()}")
        lines.append("")
    lines.append(f"Name: {contact.full_name}")
    lines.append(f"Relationship: {contact.relationship_type.value}")
    lines.append(f"Desired contact cadence: {contact.contact_frequency.value}")
    if contact.company or contact.position:
        lines.append(
            f"Role: {contact.position or '?'} at {contact.company or '?'}"
        )
    if contact.location:
        lines.append(f"Location: {contact.location}")
    if contact.birth_date:
        lines.append(f"Birthday: {contact.birth_date.isoformat()}")

    socials = {
        "Instagram": contact.instagram_url,
        "LinkedIn": contact.linkedin_url,
        "Facebook": contact.facebook_url,
        "Twitter/X": contact.twitter_url,
        "Website": contact.website_url,
    }
    socials = {k: v for k, v in socials.items() if v}
    if socials:
        lines.append("Social: " + ", ".join(f"{k} {v}" for k, v in socials.items()))

    if contact.tags:
        lines.append("Tags: " + ", ".join(t.name for t in contact.tags))
    if contact.tone:
        lines.append(f"Preferred communication tone: {contact.tone}")
    if contact.notes:
        lines.append(f"Notes: {contact.notes}")

    current_facts = [f for f in getattr(contact, "facts", []) if f.is_current]
    if current_facts:
        lines.append("Known facts (current):")
        for fct in current_facts[:20]:
            lines.append(f"  - [{fct.fact_type.value}] {fct.value}")

    score, status = warmth.compute_warmth(contact)
    elapsed = round(warmth.days_since_last_contact(contact))
    lines.append(
        f"Relationship warmth: {score}/100 ({status}); "
        f"{elapsed} days since last contact."
    )

    if contact.key_dates:
        lines.append("Key dates:")
        for kd in contact.key_dates:
            lines.append(f"  - {kd.date.isoformat()}: {kd.label}")

    if contact.interactions:
        lines.append("Recent interactions (newest first):")
        for itx in contact.interactions[:8]:
            when = itx.occurred_at.date().isoformat()
            lines.append(
                f"  - {when} [{itx.channel.value}/{itx.direction.value}] "
                f"sentiment={itx.sentiment:+.1f}: {itx.summary or '(no summary)'}"
            )

    if contact.life_events:
        lines.append("Known life events:")
        for ev in contact.life_events[:6]:
            when = ev.event_date.isoformat() if ev.event_date else "?"
            lines.append(f"  - {when} {ev.event_type}: {ev.title}")

    if contact.social_snapshots:
        lines.append("Social snapshots captured:")
        for snap in contact.social_snapshots[:4]:
            text = (snap.raw_text or snap.title or "").strip().replace("\n", " ")
            lines.append(f"  - {snap.platform} ({snap.url}): {text[:300]}")

    recent_posts = _recent_social_posts(contact)
    if recent_posts:
        lines.append("Recent social posts (newest first):")
        for post in recent_posts:
            when = post.posted_at.date().isoformat() if post.posted_at else "?"
            text = (post.caption or post.alt_text or "").strip().replace("\n", " ")
            lines.append(f"  - {when} [{post.platform}]: {text[:240]}")

    return "\n".join(lines)


def _recent_social_posts(contact: models.Contact, limit: int = 6):
    """Останні дописи людини — щоб чернетка могла зачепитися за свіже."""
    from sqlalchemy import select
    from sqlalchemy.orm import object_session

    from app.modules.integrations.social.models import SocialPost

    session = object_session(contact)
    if session is None:
        return []
    return list(
        session.scalars(
            select(SocialPost)
            .where(SocialPost.contact_id == contact.id)
            .order_by(SocialPost.posted_at.desc().nullslast(), SocialPost.id.desc())
            .limit(limit)
        )
    )


def _style_context(contact: models.Contact) -> str:
    """The owner's voice: global style card + real examples to THIS person."""
    import json as _json

    from sqlalchemy.orm import object_session

    parts: list[str] = []
    session = object_session(contact)
    if session is not None:
        profile = session.get(models.StyleProfile, 1)
        if profile is not None:
            parts.append(f"Стиль Степана (з його реальних повідомлень):\n{profile.summary_md}")
    if contact.style_examples:
        try:
            examples = _json.loads(contact.style_examples)[-6:]
            if examples:
                parts.append(
                    "Реальні повідомлення Степана САМЕ ЦІЙ людині "
                    "(наслідуй їх найбільше):\n"
                    + "\n".join(f"- {e}" for e in examples)
                )
        except (ValueError, TypeError):
            pass
    return "\n\n".join(parts)


def _dialogue_history(contact: models.Contact, limit: int = 12) -> str:
    """Recent message exchange with this contact, oldest first, labeled by
    speaker — the raw material for mimicking the user's own voice."""
    msgs = [
        i
        for i in contact.interactions
        if i.channel == models.Channel.message and i.summary
    ][:limit]
    if not msgs:
        return "(немає історії листування)"
    lines = []
    for i in reversed(msgs):  # interactions are newest-first on the model
        who = "Я" if i.direction == models.Direction.outbound else contact.first_name
        when = i.occurred_at.strftime("%d.%m")
        lines.append(f"[{when}] {who}: {i.summary}")
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────────


def generate_dossier(contact: models.Contact) -> str:
    """A short, warm intelligence brief on the person."""
    if not llm.enabled():
        return _fallback_dossier(contact)
    system = (
        "You are a thoughtful networking assistant who helps the user nurture "
        "genuine relationships. Write concise, human dossiers — never creepy, "
        "never salesy. Base everything strictly on the data given; do not invent "
        "facts. If information is missing, say what would be worth learning."
    )
    prompt = (
        "Write a short relationship dossier (120–200 words) for the contact "
        "below. Cover: who they are, the state and trajectory of the "
        "relationship, and 2–3 concrete things to keep in mind when engaging. "
        "Plain prose, no headings.\n\n"
        f"{_contact_context(contact)}"
    )
    return llm.text(system, prompt, max_tokens=1200, thinking=True) or _fallback_dossier(
        contact
    )


_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "should_contact": {"type": "boolean"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
        "channel": {
            "type": "string",
            "enum": ["call", "message", "email", "meeting", "social", "event", "other"],
        },
        "timing": {"type": "string"},
        "reason": {"type": "string"},
        "talking_points": {"type": "array", "items": {"type": "string"}},
        "draft_message": {"type": "string"},
    },
    "required": [
        "should_contact",
        "urgency",
        "channel",
        "timing",
        "reason",
        "talking_points",
        "draft_message",
    ],
    "additionalProperties": False,
}


def recommend_outreach(contact: models.Contact) -> dict:
    """Structured advice on whether/how/when to reach out, plus a draft."""
    if not llm.enabled():
        return _fallback_recommendation(contact)
    system = (
        "You are a networking strategist. Give practical, specific outreach "
        "advice grounded only in the provided data. Drafts should sound like "
        "the user wrote them: warm, natural, brief. Respond with JSON only."
    )
    prompt = (
        "Based on the contact below, decide whether the user should reach out "
        "now, through which channel, when, and why. Provide 2–4 talking points "
        "and a ready-to-send draft message (2–4 sentences).\n\n"
        f"{_contact_context(contact)}"
    )
    data = llm.json(
        system, prompt, _RECOMMENDATION_SCHEMA, max_tokens=1500, thinking=True
    )
    if isinstance(data, dict):
        data["source"] = "ai"
        return data
    return _fallback_recommendation(contact)


_SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {"type": "number"}}},
    "required": ["scores"],
    "additionalProperties": False,
}


def score_sentiments(texts: list[str]) -> list[float]:
    """Score the tone of each text in [-1, 1] (warm/positive → +1).

    Batched into a single call for efficiency. Returns 0.0 for every item when
    AI is unavailable or on any error (neutral, never blocks sync).
    """
    if not texts:
        return []
    if not llm.enabled():
        return [0.0] * len(texts)
    numbered = "\n".join(f"{i+1}. {t[:400]}" for i, t in enumerate(texts))
    system = (
        "You rate the emotional tone of short interaction snippets (emails, "
        "meeting titles, chat messages) from the point of view of relationship "
        "warmth. Return a JSON array 'scores' with one number per item in the "
        "same order, each between -1 (cold/negative/conflict) and 1 "
        "(warm/positive/friendly); 0 is neutral or purely transactional."
    )
    data = llm.json(
        system, f"Rate these {len(texts)} items:\n{numbered}", _SENTIMENT_SCHEMA,
        max_tokens=1000,
    )
    if isinstance(data, dict) and isinstance(data.get("scores"), list):
        scores = data["scores"]
        out: list[float] = []
        for i in range(len(texts)):
            try:
                out.append(max(-1.0, min(1.0, float(scores[i]))))
            except (IndexError, TypeError, ValueError):
                out.append(0.0)
        return out
    return [0.0] * len(texts)


def suggest_event_message(contact: models.Contact, event: models.LifeEvent) -> str:
    """A short congratulatory / supportive message for a life event."""
    if not llm.enabled():
        return _fallback_event_message(contact, event)
    system = (
        "You write short, sincere, personal messages for meaningful moments. "
        "No clichés, NO emojis at all, match a warm friendly tone. "
        "Reply in the language the contact uses."
    )
    prompt = (
        f"Write a 1–3 sentence message to {contact.full_name} about this event:\n"
        f"  {event.event_type}: {event.title}\n"
        f"  {event.description or ''}\n\n"
        f"Relationship: {contact.relationship_type.value}."
    )
    out = _strip_emoji(llm.text(system, prompt, max_tokens=400))
    return out or _fallback_event_message(contact, event)


def build_style_card(sample: list[str]) -> str | None:
    """Distill the owner's writing style from a sample of their real
    messages into a compact style card for draft prompts."""
    if not llm.enabled() or not sample:
        return None
    joined = "\n".join(f"- {s}" for s in sample[:300])
    system = (
        "Ти аналізуєш стиль письма людини за її реальними повідомленнями. "
        "Опиши компактно (до 250 слів, маркованим списком): мови і коли яку "
        "вживає; довжина й ритм речень; привітання/прощання; емодзі та "
        "пунктуація; тон (формальність, гумор); улюблені слова й фрази; "
        "чого НЕ робить. Пиши українською, це інструкція для копірайтера, "
        "який писатиме від її імені."
    )
    return llm.text(system, f"Повідомлення:\n{joined}", max_tokens=1200, thinking=True)


def draft_reply(contact: models.Contact, incoming_text: str) -> str | None:
    """Draft a reply to an incoming Telegram DM, in the user's own voice.

    Returns None when AI is unavailable or fails — the caller then shows the
    incoming message to the admin without a draft (never a canned reply).
    """
    if not llm.enabled():
        return None
    system = (
        "Ти пишеш відповіді в Telegram ВІД ІМЕНІ користувача (Степана). "
        "Пиши так, як пише він сам — дивись на його попередні репліки в історії "
        "та копіюй його лексику, тон і довжину речень.\n"
        "ПРАВИЛА (суворо):\n"
        "1) Мова: відповідай ТІЄЮ Ж мовою, що й НОВЕ повідомлення співрозмовника. "
        "Російське повідомлення → відповідь російською; українське → "
        "українською. Це важливіше за мову прикладів чи історії.\n"
        "2) БЕЗ емодзі та смайликів — жодного, навіть якщо вони є в прикладах.\n"
        "3) По суті: якщо є питання чи прохання — дай конкретну, пряму "
        "відповідь. НЕ лести, без компліментів, без зайвої ввічливості, без "
        "води й без формальних привітань/підписів.\n"
        "4) 1–3 речення, жива месенджерна мова.\n"
        "5) Верни ЛИШЕ текст повідомлення, без лапок і пояснень.\n"
        "Якщо нове повідомлення — лише ввічливе завершення розмови (подяка, "
        "прощання, 'ок'), на яке відповідь не потрібна, поверни рівно [SKIP]."
    )
    style = _style_context(contact)
    prompt = (
        f"{_contact_context(contact)}\n\n"
        + (f"{style}\n\n" if style else "")
        + f"Історія листування:\n{_dialogue_history(contact)}\n\n"
        f"Нове повідомлення від {contact.first_name}:\n«{incoming_text}»\n\n"
        "Напиши відповідь від Степана тією ж мовою, що й це повідомлення."
    )
    raw = llm.text(system, prompt, max_tokens=600)
    return _clean(_strip_emoji(raw)) if raw else None


def draft_outreach(contact: models.Contact, reason: str) -> str | None:
    """Draft a proactive first-touch Telegram message (outreach queue).

    Grounded in the full relationship context; written in the user's own
    voice. Returns None when AI is unavailable — the queue card then asks
    the user to write manually."""
    if not llm.enabled():
        return None
    system = (
        "Ти пишеш перше повідомлення в Telegram ВІД ІМЕНІ Степана, щоб "
        "відновити контакт. Пиши, як він сам — копіюй його лексику й тон з "
        "історії.\n"
        "ПРАВИЛА (суворо):\n"
        "1) Мова: тією ж, якою ви листувалися з цією людиною (російською "
        "листування → пиши російською).\n"
        "2) БЕЗ емодзі та смайликів.\n"
        "3) Коротко (1–3 речення), тепло, але БЕЗ лестощів і надмірної "
        "ввічливості; без вибачень за паузу, якщо це не доречно. Якщо є "
        "свіжий привід (подія, факт) — природно обіграй його.\n"
        "4) Верни ЛИШЕ текст повідомлення, без лапок і пояснень."
    )
    style = _style_context(contact)
    prompt = (
        f"{_contact_context(contact)}\n\n"
        + (f"{style}\n\n" if style else "")
        + f"Історія листування:\n{_dialogue_history(contact)}\n\n"
        f"Привід написати зараз: {reason}\n\n"
        f"Напиши повідомлення від Степана до {contact.first_name}."
    )
    raw = llm.text(system, prompt, max_tokens=600)
    return _clean(_strip_emoji(raw)) if raw else None


_INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "company": {"type": "string"},
        "position": {"type": "string"},
        "note": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_type": {
                        "type": "string",
                        "enum": [
                            "role",
                            "employer",
                            "location",
                            "interest",
                            "family",
                            "relationship",
                            "preference",
                            "other",
                        ],
                    },
                    "value": {"type": "string"},
                },
                "required": ["fact_type", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "first_name", "last_name", "company", "position", "note", "facts"],
    "additionalProperties": False,
}


def draft_request(contact: models.Contact, what: str) -> str | None:
    """Прохання до людини від імені власника («попроси Марію надіслати рахунок»).

    Іде лише чернеткою з кнопкою «Надіслати» — сама система нічого не шле.
    ``None`` без моделі: тоді бот бере простий шаблон."""
    if not llm.enabled() or not (what or "").strip():
        return None
    system = (
        "Ти пишеш коротке прохання в Telegram ВІД ІМЕНІ Степана до людини з "
        "його мережі. Пиши, як він сам — його лексикою й тоном з історії.\n"
        "ПРАВИЛА (суворо):\n"
        "1) Мова: тією ж, якою ви листувалися з цією людиною.\n"
        "2) БЕЗ емодзі та смайликів.\n"
        "3) 1–2 речення: саме прохання і строк, якщо він названий. Без лестощів "
        "і зайвих вибачень.\n"
        "4) Верни ЛИШЕ текст повідомлення, без лапок і пояснень."
    )
    style = _style_context(contact)
    prompt = (
        f"{_contact_context(contact)}\n\n"
        + (f"{style}\n\n" if style else "")
        + f"Історія листування:\n{_dialogue_history(contact)}\n\n"
        f"Що попросити: {what.strip()}\n\n"
        f"Напиши прохання від Степана до {contact.first_name}."
    )
    raw = llm.text(system, prompt, max_tokens=300)
    return _clean(_strip_emoji(raw)) if raw else None


def parse_voice_intake(text: str) -> dict | None:
    """Turn a dictated note into structured contact data. None when AI is
    off or nothing useful was extracted."""
    if not llm.enabled() or not text.strip():
        return None
    system = (
        "Ти асистент нетворкінг-CRM. Користувач надиктував нотатку про людину "
        "(нове знайомство або оновлення). Витягни структуровано: ім'я людини, "
        "компанію/посаду якщо є, стабільні факти (інтереси, локація, сім'я, "
        "домовленості) і коротку нотатку-резюме. Пиши значення мовою нотатки. "
        "Поля, яких немає в нотатці, лиши порожніми рядками. Respond with "
        "JSON only."
    )
    data = llm.json(system, f"Нотатка:\n{text}", _INTAKE_SCHEMA, max_tokens=800)
    if isinstance(data, dict) and (data.get("name") or "").strip():
        return data
    return None


_ASSISTANT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "remind", "note", "cadence", "importance", "calendar",
                "idea", "task", "none",
            ],
        },
        "person": {"type": "string"},
        "text": {"type": "string"},
        "due_date": {"type": "string"},  # ISO YYYY-MM-DD, or "" if none
        "frequency": {
            "type": "string",
            "enum": [
                "weekly", "biweekly", "monthly",
                "quarterly", "biannual", "yearly", "",
            ],
        },
        "importance": {"type": "integer"},  # 1..3, or 0 if not specified
        # Calendar fields (action == "calendar")
        "title": {"type": "string"},
        "start_time": {"type": "string"},  # "HH:MM" 24h, or ""
        "end_time": {"type": "string"},    # "HH:MM" 24h, or ""
        "attendee_email": {"type": "string"},
        "google_meet": {"type": "boolean"},
        # Періодичність задачі-нагадування (action == "task") — СЛОВАМИ, як
        # сказав власник: «раз на місяць», «щотижня». Порожньо, якщо не сказав.
        "recurrence": {"type": "string"},
    },
    "required": [
        "action", "person", "text", "due_date", "frequency", "importance",
        "title", "start_time", "end_time", "attendee_email", "google_meet",
        "recurrence",
    ],
    "additionalProperties": False,
}


def parse_assistant_intent(text: str, today: str) -> dict | None:
    """Classify a free-text message to the bot as an assistant *command*.

    Returns a dict with ``action`` in {remind, note, cadence, importance,
    none}. ``action == "none"`` means it's NOT a command (a question or a
    network search) — the caller should fall back to search. ``None`` when AI
    is unavailable (caller also falls back to search).

    ``today`` is the current date (e.g. "2026-07-07, понеділок") so the model
    can resolve relative times like "через 3 тижні" into ``due_date``.
    """
    if not llm.enabled() or not text.strip():
        return None
    system = (
        "Ти — асистент персонального нетворкінг-CRM. Користувач пише боту "
        "вільним текстом. Визнач, чи це КОМАНДА-дія над контактом, чи ні.\n"
        f"Сьогодні {today}.\n"
        "Дії (action):\n"
        "• remind — поставити нагадування («нагадай написати Олегу через 3 "
        "тижні», «пінг Марії в пʼятницю»). Заповни person, text (що зробити), "
        "due_date (ISO дата, обчисли від сьогодні).\n"
        "• note — записати нотатку/факт про людину («запиши до Олега, що він "
        "переїхав у Берлін»). Заповни person і text.\n"
        "• cadence — змінити частоту контакту («спілкуватися з Олегом раз на "
        "місяць»). Заповни person і frequency.\n"
        "• importance — змінити важливість 1..3 («Олег дуже важливий»=3). "
        "Заповни person і importance.\n"
        "• calendar — додати подію/зустріч у календар («додай зустріч на "
        "сьогодні о 14:00 з Сергієм», «подія завтра з 9 до 11 відкриття "
        "рахунків»). Заповни: title (коротка назва події, напр. «Зустріч з "
        "Сергієм» або «Відкриття рахунків банку»), due_date (ISO дата події), "
        "start_time («HH:MM» 24-год), end_time (якщо вказано діапазон, інакше "
        "\"\"), attendee_email (email гостя, якщо є), google_meet (true, якщо "
        "просять через Google Meet/онлайн). person — імʼя людини, якщо згадана.\n"
        "• idea — користувач ділиться ІДЕЄЮ/думкою/задумом, щоб зберегти й "
        "розвивати («є ідея...», «ідея:...», «думка:...», а також довгі "
        "роздуми про продукт/проєкт/можливість). Це НЕ про конкретну людину, "
        "яку він щойно зустрів. Нічого не заповнюй — весь текст збережеться "
        "як є.\n"
        "• task — завести ЗАДАЧУ-НАГАДУВАННЯ, про яку бот нагадуватиме сам "
        "(«впиши задачу: оновити прайс, відповідальний Марія, нагадуй раз на "
        "місяць», «додай задачу платити за домен щороку»). Заповни: text "
        "(суть задачі без слова «задача»), person (відповідальний, якщо "
        "названий), recurrence (періодичність СЛОВАМИ, як він сказав; "
        "порожньо, якщо не сказав).\n"
        "• none — це НЕ команда: питання чи пошук по мережі («хто з моїх "
        "у крипті?», «знайди Марію»), або нотатка про нового знайомого "
        "(«познайомився з Андрієм...»). Став action=none.\n"
        "Пріоритет, якщо сумнівно: зустріч (є час/дата) → calendar; прохання "
        "нагадати → remind; роздум/задум → idea; інакше none.\n"
        "Порожні поля лишай порожніми рядками (або 0 для importance). "
        "Respond with JSON only."
    )
    data = llm.json(system, text.strip(), _ASSISTANT_SCHEMA, max_tokens=400)
    if isinstance(data, dict) and data.get("action") in {
        "remind", "note", "cadence", "importance", "calendar", "idea",
        "task", "none",
    }:
        return data
    return None


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["contact_id", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def search_network(query: str, corpus: list[str]) -> list[dict] | None:
    """Natural-language search over the user's own network.

    ``corpus`` is one compact line per contact. Returns [{contact_id, why}]
    ranked by relevance, or None when AI is unavailable."""
    if not llm.enabled():
        return None
    system = (
        "Ти шукаєш людей у особистій мережі користувача. Нижче — по одному "
        "рядку на контакт. Поверни до 8 найрелевантніших запиту контактів, "
        "для кожного — one-line пояснення 'чому' мовою запиту, спираючись "
        "ЛИШЕ на надані дані. Якщо збігів немає — порожній список. "
        "Respond with JSON only."
    )
    prompt = "Запит: " + query + "\n\nМережа:\n" + "\n".join(corpus)
    data = llm.json(system, prompt, _SEARCH_SCHEMA, max_tokens=1500, thinking=True)
    if isinstance(data, dict) and isinstance(data.get("matches"), list):
        return data["matches"]
    return None


_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "company": {"type": "string"},
        "position": {"type": "string"},
        "website_url": {"type": "string"},
        "linkedin_url": {"type": "string"},
        "instagram_url": {"type": "string"},
        "telegram": {"type": "string"},
    },
    "required": [
        "email", "phone", "company", "position",
        "website_url", "linkedin_url", "instagram_url", "telegram",
    ],
    "additionalProperties": False,
}


def parse_business_card(image: bytes, mime: str = "image/jpeg") -> dict | None:
    """Read a business-card photo (vision) into contact channels. Missing
    fields come back as empty strings; None when AI is off/fails."""
    if not llm.enabled() or not image:
        return None
    system = (
        "Ти читаєш фото візитки. Витягни контактні дані точно як надруковано. "
        "Поля, яких немає — порожній рядок. Respond with JSON only."
    )
    data = llm.vision_json(
        system, "Зчитай контакти з візитки.", image, mime, _CARD_SCHEMA, max_tokens=600
    )
    return data if isinstance(data, dict) else None


_TRANSLATE_LANGS = {
    "en": "англійську",
    "cs": "чеську",
    "ru": "російську",
    "uk": "українську",
}


def translate_message(text: str, lang: str) -> str | None:
    """Translate a drafted messenger message, preserving casual tone, emoji
    and length. Returns None when AI is unavailable or fails."""
    if not llm.enabled() or not text.strip():
        return None
    language = _TRANSLATE_LANGS.get(lang, lang)
    system = (
        "Ти перекладаєш коротке месенджерне повідомлення. Збережи неформальний "
        "тон, емодзі та довжину — це має звучати природно для носія мови, а не "
        "як машинний переклад. Верни ЛИШЕ перекладений текст."
    )
    return _clean(
        llm.text(system, f"Переклади на {language}:\n\n{text}", max_tokens=600)
    )


def refine_reply(
    contact: models.Contact, current_draft: str, instruction: str
) -> str | None:
    """Rewrite a drafted message according to the user's instruction
    (typed or transcribed from a voice note)."""
    if not llm.enabled():
        return None
    system = (
        "Ти редагуєш чернетку Telegram-повідомлення від імені Степана. "
        "Збережи його стиль — коротко, природно, месенджерно. Не змінюй мову "
        "чернетки (крім випадку, коли інструкція прямо просить). БЕЗ емодзі. "
        "Без лестощів. Верни ЛИШЕ фінальний текст повідомлення, без лапок і "
        "пояснень."
    )
    prompt = (
        f"Кому: {contact.full_name}"
        + (f" (тон: {contact.tone})" if contact.tone else "")
        + f"\n\nПоточна чернетка:\n«{current_draft}»\n\n"
        f"Інструкція від Степана: {instruction}\n\n"
        "Перепиши чернетку згідно з інструкцією."
    )
    raw = llm.text(system, prompt, max_tokens=600)
    return _clean(_strip_emoji(raw)) if raw else None


_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_type": {
                        "type": "string",
                        "enum": [
                            "role",
                            "employer",
                            "location",
                            "interest",
                            "family",
                            "relationship",
                            "preference",
                            "other",
                        ],
                    },
                    "value": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": ["fact_type", "value", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


def extract_facts(contact: models.Contact) -> list[dict]:
    """Read everything known about a contact and extract durable facts."""
    if not llm.enabled():
        return []
    system = (
        "You extract durable, stable facts about a person from a relationship "
        "CRM record — role, employer, location, interests, family, key "
        "relationships and communication preferences. Only report facts clearly "
        "supported by the data. Each fact must be a short, standalone statement "
        "(e.g. 'Works at Acme as CTO', 'Lives in Berlin', 'Into trail running'). "
        "Do not invent anything. Respond with JSON only."
    )
    prompt = (
        "From the contact record below, extract the stable facts worth "
        "remembering long-term. Skip transient chatter and one-off events.\n\n"
        f"{_contact_context(contact)}"
    )
    data = llm.json(system, prompt, _FACTS_SCHEMA, max_tokens=1500, thinking=True)
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        return [f for f in data["facts"] if f.get("value")]
    return []


_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": ["event_type", "title", "description", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def detect_life_events(
    contact: models.Contact, snapshot: models.SocialSnapshot
) -> list[dict]:
    """Read a social snapshot and extract candidate life events."""
    if not llm.enabled():
        return []
    text = (snapshot.raw_text or snapshot.title or "").strip()
    if not text:
        return []
    system = (
        "You detect meaningful life events from social media text — new job, "
        "promotion, relocation, marriage/engagement, new baby, a launch, an "
        "award, a loss. Only report events clearly supported by the text. "
        "Respond with JSON only."
    )
    prompt = (
        f"Contact: {contact.full_name} ({contact.relationship_type.value}).\n"
        f"Source: {snapshot.platform} — {snapshot.url}\n\n"
        f"Content:\n{text[:4000]}\n\n"
        "List any significant life events you can confidently detect."
    )
    data = llm.json(system, prompt, _EVENTS_SCHEMA, max_tokens=1200, thinking=True)
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return data["events"]
    return []


# ── Rule-based fallbacks (used when AI is unavailable) ────────────────────────


def _fallback_dossier(contact: models.Contact) -> str:
    score, status = warmth.compute_warmth(contact)
    elapsed = round(warmth.days_since_last_contact(contact))
    role = ""
    if contact.position or contact.company:
        role = f" — {contact.position or 'works'} at {contact.company or 'an organisation'}"
    bits = [
        f"{contact.full_name} is a {contact.relationship_type.value} contact{role}.",
        f"The relationship is currently {status} ({score}/100); it has been about "
        f"{elapsed} days since your last contact, and your target cadence is "
        f"{contact.contact_frequency.value}.",
    ]
    if contact.notes:
        bits.append(f"Notes on file: {contact.notes}")
    if warmth.is_due(contact):
        bits.append("They are due for a check-in — a light, genuine message would help.")
    bits.append("(Configure an AI provider key to generate richer AI dossiers.)")
    return " ".join(bits)


def _fallback_recommendation(contact: models.Contact) -> dict:
    due = warmth.is_due(contact)
    ratio = warmth.overdue_ratio(contact)
    urgency = "high" if ratio >= 2 else "medium" if due else "low"
    channel = "message"
    if contact.relationship_type in (models.Relationship.client, models.Relationship.investor):
        channel = "email"
    elif contact.relationship_type in (models.Relationship.friend, models.Relationship.family):
        channel = "call"

    first = contact.first_name
    draft = (
        f"Hi {first}, you popped into my mind today — it's been a while. "
        "How are things going? Would love to catch up soon."
    )
    points = ["Ask an open question about their current focus."]
    if contact.company:
        points.append(f"Reference their work at {contact.company}.")
    if contact.birth_date:
        points.append("Remember their birthday is on file.")

    return {
        "should_contact": due,
        "urgency": urgency,
        "channel": channel,
        "timing": "this week" if due else "no rush",
        "reason": (
            f"It's been {round(warmth.days_since_last_contact(contact))} days; "
            f"target cadence is {contact.contact_frequency.value}."
        ),
        "talking_points": points,
        "draft_message": draft,
        "source": "rule-based",
    }


def _fallback_event_message(contact: models.Contact, event: models.LifeEvent) -> str:
    return (
        f"Hi {contact.first_name}, I just heard about {event.title.lower()} — "
        "congratulations, that's wonderful news! Really happy for you."
    )


_REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "reflections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "reflection": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["contact_id", "reflection", "action"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reflections"],
    "additionalProperties": False,
}


def reflect_on_relationships(items: list[dict]) -> list[dict] | None:
    """Given [{id, context}] for a few contacts, return a short reflection and
    one concrete next action per person. None when AI is unavailable.

    ``context`` is a compact digest (history, facts, cadence). Mirrors Mesh's
    "Reflect on your relationship with X, Y, Z" prompt."""
    if not llm.enabled() or not items:
        return None
    blocks = "\n\n".join(
        f"[contact_id={it['id']}]\n{it['context']}" for it in items
    )
    system = (
        "Ти — вдумливий асистент з нетворкінгу. Для КОЖНОЇ людини нижче дай "
        "коротку рефлексію про стосунок (1–2 речення: де ви зараз, що варто "
        "памʼятати) і ОДНУ конкретну наступну дію (коротко, дієслово). Пиши "
        "українською, тепло й по-людськи, без води. Respond with JSON only."
    )
    data = llm.json(system, blocks, _REFLECT_SCHEMA, max_tokens=1200, thinking=True)
    if isinstance(data, dict) and isinstance(data.get("reflections"), list):
        return data["reflections"]
    return None


def analyze_time_report(markdown: str, goals: list[str]) -> str | None:
    """Turn a raw weekly time report into focused, goal-aware insights.

    ``goals`` are the user's active networking/project goal titles, so the
    analysis ties time spent to what actually matters. Returns HTML-safe
    plain text (no markup) or None when AI is unavailable."""
    if not llm.enabled() or not (markdown or "").strip():
        return None
    goals_block = (
        "Активні цілі користувача:\n" + "\n".join(f"- {g}" for g in goals)
        if goals
        else "Активних цілей не задано."
    )
    system = (
        "Ти — коуч з продуктивності. Нижче — тижневий звіт часу з ПК "
        "(ActivityWatch, по проєктах/застосунках) і цілі користувача. "
        "Дай стислий аналіз УКРАЇНСЬКОЮ, по суті, без води:\n"
        "1) Куди реально пішов час (топ-напрями, приблизні частки).\n"
        "2) Що працює НА цілі, а що — відволікання/не в фокусі.\n"
        "3) 2–3 конкретні поради, що прибрати або зменшити наступного тижня.\n"
        "Пиши звичайним текстом без Markdown і без емодзі-заголовків; "
        "короткі абзаци або марковані пункти через «•»."
    )
    user = f"{goals_block}\n\nЗВІТ:\n{markdown[:8000]}"
    return _clean(llm.text(system, user, max_tokens=900, thinking=True))


def analyze_day(digest: str, goals: list[str]) -> str | None:
    """Кілька речень порад до денного зведення. Коротко — увечері довге не читають."""
    if not llm.enabled() or not (digest or "").strip():
        return None
    goals_block = ("Цілі: " + "; ".join(goals)) if goals else "Цілей не задано."
    system = (
        "Ти — коуч з продуктивності. Нижче — коротке зведення дня з ПК і цілі "
        "користувача. Відповідай УКРАЇНСЬКОЮ, максимум 3 речення або 3 пункти "
        "через «•». Скажи: що з дня вело до цілей, що було відволіканням, "
        "і ОДНУ конкретну дію на завтра. Без markdown, без емодзі, без води."
    )
    return _clean(llm.text(system, f"{goals_block}\n\n{digest}",
                           max_tokens=260, thinking=False))


# ── Коуч по цілях ──────────────────────────────────────────────────────────

_COACH_PERSONA = (
    "Ти — особистий коуч власника по його річних цілях. Мова: українська. "
    "Пиши коротко й по суті, без емодзі, без лестощів і без вступів на "
    "кшталт «чудово» чи «класно». Ти не хвалиш — ти щодня тиснеш на прогрес."
)


def coach_question(goal: str, status: str, notes: str, theme: str = "") -> str | None:
    """Одне питання про стан цілі. Спирається на останні записи в журналі."""
    if not llm.enabled():
        return None
    system = (
        f"{_COACH_PERSONA}\n"
        "Задай ОДНЕ питання про стан цієї цілі. Якщо в журналі є записи, "
        "спирайся на останній: питай, що зрушило з того часу. "
        "Максимум два речення, без передмов."
        + (f"\nТема року власника: {theme}." if theme else "")
    )
    user = (
        f"Ціль: {goal}\n"
        f"Статус: {status}\n"
        f"Журнал:\n{notes or 'записів ще немає'}"
    )
    return _strip_emoji(_clean(llm.text(system, user, max_tokens=200)))


def coach_nudge(goal: str) -> str | None:
    """Колюче нагадування, якщо на питання дня не відповіли."""
    if not llm.enabled():
        return None
    system = (
        f"{_COACH_PERSONA}\n"
        "Власник за день не відповів на питання про ціль. Нагадай прямо й "
        "колюче: без образ, але так, щоб мовчання не виглядало безкоштовним. "
        "Одне-два речення."
    )
    return _strip_emoji(_clean(llm.text(system, f"Ціль: {goal}", max_tokens=160)))


_COACH_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Суть відповіді для журналу, 3-15 слів, без дати.",
        },
        "status": {
            "type": "string",
            "description": (
                "Новий статус, якщо зі слів це однозначно: зробив -> done, "
                "майже -> almost, почав/рухається -> in progress, скасував -> "
                "cancelled, переніс -> postponed to next year / postponed to "
                "far future. Якщо неясно — порожній рядок."
            ),
        },
        "reply": {
            "type": "string",
            "description": "Коротка реакція власнику, одне-два речення.",
        },
    },
    "required": ["summary", "status", "reply"],
}


def coach_review(goal: str, status: str, notes: str, answer: str) -> dict | None:
    """Розбирає відповідь власника: що записати в журнал і чи міняти статус.

    Повертає ``{"summary", "status", "reply"}``. ``status`` — порожній рядок,
    якщо зі слів не видно однозначної зміни; викликач валідує його сам.
    """
    if not llm.enabled() or not answer.strip():
        return None
    system = (
        f"{_COACH_PERSONA}\n"
        "Власник відповів на питання про ціль. Витягни суть у summary "
        "(3-15 слів, без дати, без емодзі). Постав status тільки якщо зміна "
        "однозначна, інакше порожній рядок. У reply — коротка реакція без "
        "похвали; якщо відповідь розмита, постав уточнювальне питання. "
        "Respond with JSON only."
    )
    user = (
        f"Ціль: {goal}\n"
        f"Поточний статус: {status}\n"
        f"Журнал:\n{notes or 'записів ще немає'}\n\n"
        f"Відповідь власника: {answer.strip()}"
    )
    data = llm.json(system, user, _COACH_REVIEW_SCHEMA, max_tokens=400)
    if not isinstance(data, dict):
        return None
    return {
        "summary": _strip_emoji(str(data.get("summary") or "")) or "",
        "status": str(data.get("status") or "").strip().lower(),
        "reply": _strip_emoji(str(data.get("reply") or "")) or "",
    }


def coach_week_verdict(block: str, stalled: bool) -> str | None:
    """Два-три речення підсумку тижня поверх готових цифр."""
    if not llm.enabled():
        return None
    system = (
        f"{_COACH_PERSONA}\n"
        + (
            "Тиждень пройшов без жодного руху по цілях. Скажи це прямо, без "
            "пом'якшення, і назви одну дію на понеділок."
            if stalled
            else "Дай короткий підсумок тижня: що реально зрушило і де провал. "
            "Назви одну дію на понеділок."
        )
        + " Два-три речення."
    )
    return _strip_emoji(_clean(llm.text(system, block, max_tokens=260)))


# ── Пошта: класифікація і чернетки ──────────────────────────────────────────

_EMAIL_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_reply": {
            "type": "boolean",
            "description": (
                "true, якщо лист чекає на відповідь чи дію власника. "
                "Розсилки, сповіщення сервісів, автовідповіді, рахунки без "
                "питань, підтвердження — false."
            ),
        },
        "urgency": {"type": "string", "enum": ["high", "normal", "low"]},
        "topic": {
            "type": "string",
            "description": "Про що лист і чого від мене хочуть, 5-12 слів.",
        },
    },
    "required": ["needs_reply", "urgency", "topic"],
}


def triage_email(subject: str, sender: str, body: str) -> dict | None:
    """Чи чекає лист відповіді, наскільки терміново і про що він.

    ``None`` — коли AI недоступний; викликач тоді лишає тред некласифікованим
    і показує його як є, а не вгадує.
    """
    if not llm.enabled():
        return None
    system = (
        "Ти розбираєш вхідну пошту власника і вирішуєш, що з неї потребує "
        "його відповіді. Будь суворим: більшість листів відповіді не "
        "потребують. high — коли є дедлайн, чекають на рішення або людина "
        "чекає вже не перший день. Respond with JSON only."
    )
    user = f"Від: {sender}\nТема: {subject}\n\n{(body or '')[:4000]}"
    data = llm.json(system, user, _EMAIL_TRIAGE_SCHEMA, max_tokens=300)
    if not isinstance(data, dict):
        return None
    urgency = str(data.get("urgency") or "normal").lower()
    return {
        "needs_reply": bool(data.get("needs_reply")),
        "urgency": urgency if urgency in {"high", "normal", "low"} else "normal",
        "topic": _strip_emoji(str(data.get("topic") or ""))[:300],
    }


def draft_email_reply(
    subject: str,
    sender: str,
    body: str,
    contact: models.Contact | None = None,
) -> str | None:
    """Чернетка відповіді на лист — голосом власника, готова до відправки.

    Якщо відправник є в базі, підмішуємо його досьє й приклади стилю: та сама
    механіка, що й для чернеток у Telegram.
    """
    if not llm.enabled():
        return None
    system = (
        "Ти пишеш відповіді на email ВІД ІМЕНІ власника (Степана).\n"
        "ПРАВИЛА (суворо):\n"
        "1) Мова: тією ж мовою, що й лист, на який відповідаєш.\n"
        "2) БЕЗ емодзі.\n"
        "3) По суті: відповідай на поставлені питання конкретно. Без лестощів, "
        "без води, без канцеляриту й формальних розшаркувань.\n"
        "4) Діловий, але живий тон. Коротко: стільки речень, скільки треба, "
        "і жодного зайвого.\n"
        "5) Якщо для відповіді бракує інформації — прямо напиши, чого саме "
        "бракує, замість того щоб вигадувати.\n"
        "6) Верни ЛИШЕ текст листа, без теми, без лапок і без пояснень."
    )
    parts = []
    if contact is not None:
        parts.append(_contact_context(contact))
        style = _style_context(contact)
        if style:
            parts.append(style)
    parts.append(f"Лист від {sender}\nТема: {subject}\n\n{(body or '')[:5000]}")
    raw = llm.text(system, "\n\n".join(parts), max_tokens=800)
    return _clean(_strip_emoji(raw)) if raw else None


# ── Соцмережі: нові дописи -> факти в досьє + приводи для контакту ──────────

_SOCIAL_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_type": {
                        "type": "string",
                        "enum": [
                            "role", "employer", "location", "interest",
                            "family", "relationship", "preference", "other",
                        ],
                    },
                    "value": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "post_id": {"type": "string"},
                },
                "required": ["fact_type", "value", "confidence", "post_id"],
                "additionalProperties": False,
            },
        },
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "post_id": {"type": "string"},
                },
                "required": ["event_type", "title", "description", "post_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["facts", "hooks"],
    "additionalProperties": False,
}


def digest_social_posts(contact: models.Contact, posts: list[dict]) -> dict:
    """Читає пачку нових дописів і розкладає на два кошики.

    ``facts`` — стале, що варто памʼятати (нова робота, переїзд, хобі).
    ``hooks`` — приводи написати саме зараз (запуск, нагорода, подія, дитина).
    Кожен елемент несе ``post_id`` — для провенансу факту й дедупу приводу.
    Порожні списки, якщо AI недоступний або в дописах нічого суттєвого.
    """
    if not llm.enabled() or not posts:
        return {"facts": [], "hooks": []}
    system = (
        "Ти читаєш нові дописи людини в соцмережах для особистого CRM власника. "
        "Виділи ДВА типи речей. facts — сталі факти про людину, які варто "
        "памʼятати довго (роль, роботодавець, місто, інтереси, сімʼя). hooks — "
        "події, з приводу яких доречно написати їй найближчими днями (запуск, "
        "нагорода, виступ, нова робота, дитина, весілля, переїзд, втрата). "
        "Будь суворим: селфі, цитати й реклама — це ні те, ні інше. Не вигадуй. "
        "Мова значень — українська. Respond with JSON only."
    )
    lines = [f"Контакт: {contact.full_name} ({contact.relationship_type.value})", ""]
    for p in posts[:20]:
        when = p.get("posted_at") or "?"
        text = (p.get("caption") or "").strip()
        alt = (p.get("alt_text") or "").strip()
        body = text or alt or "(без підпису)"
        if text and alt:
            body = f"{text}\n  [опис зображення: {alt}]"
        lines.append(f"[post_id={p.get('external_id')}] {when} · {p.get('media_type') or 'post'}\n{body[:800]}")
        lines.append("")
    data = llm.json(system, "\n".join(lines), _SOCIAL_DIGEST_SCHEMA, max_tokens=1500)
    if not isinstance(data, dict):
        return {"facts": [], "hooks": []}
    return {
        "facts": [f for f in data.get("facts") or [] if isinstance(f, dict) and f.get("value")],
        "hooks": [h for h in data.get("hooks") or [] if isinstance(h, dict) and h.get("title")],
    }
