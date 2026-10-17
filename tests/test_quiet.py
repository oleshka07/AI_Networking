"""Нічна тиша й черга (TASK-02): «Від Claude», «За ніч», денна стеля, «Відкрити чат», crm_update_contact."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.mcp import tools
from app.modules.tasks import external, nudges, quiet
from app.modules.tasks.models import HeldMessage, Task, TaskKind
from app.modules.telegram_bot import task_intake
from app.modules.telegram_bot.handlers import dispatch

from test_telegram_bot import FakeClient, _business_update


class RecClient(FakeClient):
    """FakeClient, що памʼятає й інші виклики Bot API (editMessageReplyMarkup)."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def call(self, method, **kw):
        self.calls.append((method, kw))
        return {}


def _at(monkeypatch, hour, minute=0, days=0):
    t = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    monkeypatch.setattr(nudges, "_local_now", lambda: t)
    monkeypatch.setattr(quiet, "_local_now", lambda: t)
    return t


@pytest.fixture
def tg(monkeypatch):
    """Бот налаштований; усе, що піде власнику через automation.telegram, — у список."""
    from app.core.config import settings
    from app.modules.automation import telegram

    monkeypatch.setattr(settings, "telegram_bot_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "42", raising=False)
    monkeypatch.setattr(nudges, "_events_between", lambda *a, **kw: [])
    sent = []
    monkeypatch.setattr(telegram, "send_message",
                        lambda text, **kw: sent.append((text, kw)) or True)
    task_intake._last_suggested.clear()
    return sent


def _owner(db, text, **args):
    out, err = tools.call_tool(db, "crm_send_owner_message", {"text": text, **args})
    assert not err, out
    return json.loads(out)


# ── В2: crm_send_owner_message ──────────────────────────────────────────────


def test_owner_message_at_night_waits_for_morning_plan(db, tg, monkeypatch):
    from app.modules.automation import daily

    _at(monkeypatch, 23, 0)
    res = _owner(db, "Підсумок синку: три нові задачі")
    assert res["sent"] is False and res["reason"] == "quiet_hours"
    until = datetime.fromisoformat(res["queued_until"])
    assert until.tzinfo is not None
    assert (until.hour, until.minute) == (8, 0)   # ранковий план (DAILY_RUN_HOUR), не 07:30
    assert tg == []

    from test_task_nudges import _task

    _task(db, "Тримає роботу", blocks_work=True)   # план теж є — порядок блоків має значення
    _at(monkeypatch, 8, 0, days=1)
    daily.run_daily_job()
    (body, _), = tg
    assert body.startswith("🤖 <b>Від Claude</b>\nПідсумок синку")  # першим блоком
    assert body.index("Від Claude") < body.index("Сьогодні")
    db.expire_all()
    assert external.get_settings(db).sent_count == 0    # не в стелі

    tg.clear()
    daily.run_daily_job()
    assert "Від Claude" not in tg[0][0]                   # удруге не приходить


def test_owner_message_by_day_goes_now_and_outside_the_cap(db, tg, monkeypatch):
    _at(monkeypatch, 15, 0)
    assert _owner(db, "Готово: чек-лист звірено") == {"sent": True}
    assert len(tg) == 1
    db.expire_all()
    assert nudges.sent_today(external.get_settings(db)) == 0


def test_urgent_bypasses_quiet_but_eats_the_cap(db, tg, monkeypatch):
    _at(monkeypatch, 23, 30)
    cfg = external.get_settings(db)
    cfg.daily_cap = 1
    db.commit()
    assert _owner(db, "Тримає роботу: потрібен доступ", urgent=True) == {"sent": True, "urgent": True}
    second = _owner(db, "Ще одне термінове", urgent=True)
    assert second["sent"] is False and second["reason"] == "daily_cap"
    assert len(tg) == 1


def test_queue_released_after_quiet_when_no_plan(db, tg, monkeypatch):
    cfg = external.get_settings(db)
    cfg.plan_enabled = False
    db.commit()
    _at(monkeypatch, 2, 0)
    res = _owner(db, "Нічний підсумок")
    assert datetime.fromisoformat(res["queued_until"]).strftime("%H:%M") == "07:30"
    assert quiet.release_pending(_at(monkeypatch, 7, 15)) is False   # ще тиша
    assert quiet.release_pending(_at(monkeypatch, 7, 45)) is True
    (body, _), = tg
    assert "Від Claude" in body and "Нічний підсумок" in body
    assert quiet.release_pending(_at(monkeypatch, 8, 0)) is False    # черга порожня


def test_queue_waits_for_the_plan_but_not_forever(db, tg, monkeypatch):
    _at(monkeypatch, 1, 0)
    _owner(db, "Чекає плану")
    assert quiet.release_pending(_at(monkeypatch, 7, 45)) is False   # план о 08:00
    assert quiet.release_pending(_at(monkeypatch, 8, 30)) is False
    assert quiet.release_pending(_at(monkeypatch, 9, 0)) is True     # план не прийшов — сама
    assert "Чекає плану" in tg[0][0]


# ── В3: нічні пропозиції задач ──────────────────────────────────────────────


def _suggestions(fake):
    return [m for m in fake.sent if "Схоже на задачу" in m["text"]]


def test_night_suggestion_lands_in_the_morning_plan(client, tg, monkeypatch):
    from app.core.database import SessionLocal
    from app.modules.automation import daily

    _at(monkeypatch, 23, 10)
    fake = RecClient()
    update = _business_update(chat_id=556100, text="Надішли, будь ласка, договір до понеділка", msg_id=5)
    dispatch(fake, update)
    assert _suggestions(fake) == []                 # о 23:10 нічого не прийшло
    task_intake._last_suggested.clear()
    dispatch(fake, update)                          # те саме повідомлення вдруге
    with SessionLocal() as db:
        (held,) = db.query(HeldMessage).all()       # дедуп за tgb:<chat>:<msg>
        assert held.ref == "tgb:556100:5" and held.released_at is None

    _at(monkeypatch, 8, 0, days=1)
    daily.run_daily_job()
    (body, kw), = tg
    assert "🌙 <b>За ніч: 1 пропозиція</b>" in body and "договір" in body
    markup = kw["reply_markup"]
    (row,) = [r for r in markup["inline_keyboard"] if r[0]["callback_data"].startswith("tn:")]
    assert [b["text"] for b in row] == ["➕ Задача 1", "✖ 1"]
    with SessionLocal() as db:
        assert nudges.sent_today(external.get_settings(db)) == 1   # блок «За ніч» = 1

    dispatch(fake, {"update_id": 9, "callback_query": {
        "id": "cb", "data": row[0]["callback_data"],
        "message": {"chat": {"id": 42}, "message_id": 901, "reply_markup": markup}}})
    assert fake.callbacks[-1] == "➕ Задачу створено"
    assert fake.edited == []                        # план не переписано
    method, kw = fake.calls[-1]
    assert method == "editMessageReplyMarkup"
    assert all(not b["callback_data"].startswith("tn:") for r in kw["reply_markup"]["inline_keyboard"] for b in r)
    with SessionLocal() as db:
        (task,) = db.query(Task).filter(Task.source == "telegram").all()
        assert task.external_id == "tgb:556100:5" and task.kind == TaskKind.duty
        assert db.query(HeldMessage).one().closed_at is not None


def test_day_suggestion_is_not_repeated_at_night(client, tg, monkeypatch):
    from app.core.database import SessionLocal

    fake = RecClient()
    update = _business_update(chat_id=556200, text="Скинь рахунок завтра", msg_id=7)
    dispatch(fake, update)
    assert len(_suggestions(fake)) == 1
    _at(monkeypatch, 23, 0)
    task_intake._last_suggested.clear()
    dispatch(fake, update)
    with SessionLocal() as db:
        assert db.query(HeldMessage).filter(HeldMessage.released_at.is_(None)).count() == 0


def test_more_button_pages_the_night(db, tg, monkeypatch):
    for n in range(7):
        quiet.hold_suggestion(db, chat_id=557000 + n, message_id=1, name=f"Контакт {n}",
                              kind="duty", commitment_type="request", outgoing=False,
                              due=None, meeting=n == 0, text=f"Надішли файл {n}")
    owner_text, night_text, rows = quiet.morning_blocks(db)
    assert owner_text is None
    assert night_text.startswith("🌙 <b>За ніч: 7 пропозицій</b>")
    assert night_text.count("\n«") == 5
    assert [b["text"] for b in rows[0]] == ["➕ Задача 1", "📅 1", "✖ 1"]
    assert rows[-1] == [{"text": "Ще… (2)", "callback_data": "tn:more"}]

    fake = RecClient()
    quiet.handle_callback(fake, "cb", "tn:more", 42,
                          {"chat": {"id": 42}, "message_id": 5, "reply_markup": {"inline_keyboard": rows}})
    (more,) = fake.sent
    assert more["text"].startswith("🌙 <b>Ще за ніч</b>\n6. ")
    assert len(more["reply_markup"]["inline_keyboard"]) == 2
    method, kw = fake.calls[-1]
    assert {"text": "Ще… (2)", "callback_data": "tn:more"} not in sum(kw["reply_markup"]["inline_keyboard"], [])


def test_dismiss_one_night_suggestion(db, monkeypatch):
    quiet.hold_suggestion(db, chat_id=558000, message_id=3, name="Демо", kind="duty",
                          commitment_type="request", outgoing=False, due=date(2026, 10, 20),
                          meeting=False, text="Надішли договір до 20.10")
    _, night_text, rows = quiet.morning_blocks(db)
    assert "строк 20.10" in night_text
    fake = RecClient()
    data = rows[0][-1]["callback_data"]
    quiet.handle_callback(fake, "cb", data, 42, {"chat": {"id": 42}, "message_id": 6,
                                                 "reply_markup": {"inline_keyboard": rows}})
    assert fake.callbacks == ["Прибрав"]
    quiet.handle_callback(fake, "cb", data, 42, {"chat": {"id": 42}, "message_id": 6})
    assert fake.callbacks[-1] == "Уже неактуально"
    db.expire_all()
    assert db.query(Task).count() == 0


def test_paused_contact_card_waits_for_morning(client, tg, monkeypatch):
    from app.core.database import SessionLocal
    from app.modules.contacts.models import Contact, Frequency

    with SessionLocal() as db:
        db.add(Contact(first_name="Петро", telegram_chat_id=556600,
                       contact_frequency=Frequency.monthly, auto_reply_paused=True))
        db.commit()
    _at(monkeypatch, 0, 30)
    fake = RecClient()
    update = _business_update(chat_id=556600, text="Давай зідзвонимось у вівторок о 15:00", msg_id=12)
    dispatch(fake, update)
    dispatch(fake, update)                         # повторна доставка того самого
    assert fake.sent == []                         # уночі картка не будить
    with SessionLocal() as db:
        held = db.query(HeldMessage).one()
        assert json.loads(held.payload)["meeting"] is True


# ── В4: тиша для всіх, стеля — лише пропозиції й пінги ──────────────────────


def test_seventh_blocker_ping_does_not_come(db, monkeypatch):
    from test_task_nudges import _local, _task

    _task(db, "Тримає роботу", blocks_work=True,
          created_at=datetime.now(timezone.utc) - timedelta(days=2))
    cfg = external.get_settings(db)
    cfg.daily_cap = 6
    db.commit()
    noon = _local(12)
    for _ in range(6):
        assert nudges.allow_proactive(db, noon)    # шість пропозицій і пінгів за день
    assert nudges.blocker_ping(db, local_now=noon) is None
    cfg.sent_count = 5
    db.commit()
    assert nudges.blocker_ping(db, local_now=noon) is not None


def test_gate_ping_counts_in_the_cap(db):
    from app.modules.gates import service as gates
    from test_task_nudges import _task

    _task(db, "Старі ворота", item_type="gate", source="gate",
          created_at=datetime.now(timezone.utc) - timedelta(days=2))
    cfg = external.get_settings(db)
    cfg.daily_cap = 0
    db.commit()
    assert gates.nudge(db) is None
    cfg.daily_cap = 6
    db.commit()
    assert gates.nudge(db) is not None
    db.expire_all()
    assert nudges.sent_today(external.get_settings(db)) == 1


def test_brief_at_night_only_before_early_meeting(db, tg, monkeypatch):
    from app.core.config import settings
    from app.integrations import google
    from app.modules.automation import briefs

    monkeypatch.setattr(settings, "meeting_brief_enabled", True, raising=False)
    briefs._briefed.clear()
    today = datetime.now()
    early = today.replace(hour=8, minute=0, second=0, microsecond=0)
    late = today.replace(hour=23, minute=45, second=0, microsecond=0)
    events = [
        {"id": "e-early", "summary": "Ранній дзвінок", "start": early, "end": None, "attendee_emails": []},
        {"id": "e-late", "summary": "Пізня зустріч", "start": late, "end": None, "attendee_emails": []},
    ]
    monkeypatch.setattr(google, "upcoming_events", lambda db, **kw: events)
    assert briefs.allowed_in_quiet(events[0]) and not briefs.allowed_in_quiet(events[1])
    _at(monkeypatch, 23, 0)
    assert briefs.run_brief_check() == 1
    assert "Ранній дзвінок" in tg[0][0]
    db.expire_all()
    assert nudges.sent_today(external.get_settings(db)) == 0   # брифи не в стелі


def test_midday_checkin_and_reviews_are_quiet_at_night(db, tg, monkeypatch):
    from app.core.config import settings
    from app.modules.automation import reviews

    from test_task_nudges import _task

    for flag in ("checkin_enabled", "weekly_review_enabled", "reflection_enabled"):
        monkeypatch.setattr(settings, flag, True, raising=False)
    _task(db, "Здати звіт", due_date=date.today())   # удень звірці є що сказати
    assert reviews.run_midday_checkin() is True
    tg.clear()
    _at(monkeypatch, 23, 0)
    assert reviews.run_midday_checkin() is False
    assert reviews.run_weekly_review() is False
    assert reviews.run_weekly_reflection() is False
    assert tg == []


def test_settings_page_shows_counter_and_queue(client, monkeypatch):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        nudges.allow_proactive(db)
        quiet.hold_owner_message(db, "Чекає ранку")
    cfg = client.get("/api/tasks/settings").json()
    assert cfg["sent_today"] == 1 and cfg["held_count"] == 1
    assert cfg["quiet_from"] == "22:30" and cfg["daily_cap"] == 6


# ── В5: «Відкрити чат» для чатів без вхідного за 24 год ─────────────────────


@pytest.fixture
def bot_api(monkeypatch):
    """automation.telegram._call: що пішло в Bot API; відповідь — ok з message_id."""
    from app.core.config import settings
    from app.modules.automation import telegram

    monkeypatch.setattr(settings, "telegram_bot_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "42", raising=False)
    calls = []

    def fake_call(method, **payload):
        calls.append(payload)
        if calls and getattr(fake_call, "fail_first", False) and len(calls) == 1:
            return {"ok": False, "description": "BUTTON_USER_PRIVACY_RESTRICTED"}
        return {"ok": True, "result": {"message_id": 500 + len(calls)}}

    monkeypatch.setattr(telegram, "_call", fake_call)
    fake_call.calls = calls
    return fake_call


def _contact(db, chat_id, username=None, inbound_hours_ago=None):
    from app.modules.contacts.models import Contact, Frequency
    from app.modules.interactions import service as itx
    from app.modules.interactions.models import Channel, Direction
    from app.modules.telegram_bot.models import BusinessConnection

    c = Contact(first_name="Ганна", last_name="Демо", telegram_chat_id=chat_id,
                telegram=username, contact_frequency=Frequency.monthly)
    db.add(c)
    if db.query(BusinessConnection).count() == 0:
        db.add(BusinessConnection(connection_id="bc-1", user_chat_id=42, is_enabled=True))
    db.commit()
    if inbound_hours_ago is not None:
        itx.add_synced_interaction(
            db, c, occurred_at=datetime.now(timezone.utc) - timedelta(hours=inbound_hours_ago),
            channel=Channel.message, direction=Direction.inbound, summary="Привіт",
            source="telegram", external_id=f"tgb:{chat_id}:1",
        )
        db.commit()
    return c


def _buttons(payload):
    return [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]


def test_cold_chat_card_has_open_and_copy_but_no_send(db, bot_api):
    c = _contact(db, 559001, username="hanna_demo", inbound_hours_ago=30)
    out, err = tools.call_tool(db, "crm_save_message_draft", {"contact": f"#{c.id}", "text": "Ганно, як справи з макетом?"})
    assert not err and "24 год" in out
    buttons = _buttons(bot_api.calls[-1])
    assert buttons[0] == {"text": "🔗 Відкрити чат", "url": "https://t.me/hanna_demo"}
    assert buttons[1] == {"text": "📋 Скопіювати текст", "copy_text": {"text": "Ганно, як справи з макетом?"}}
    assert "✅ Надіслати" not in [b["text"] for b in buttons]
    assert "✔ Надіслав сам" in [b["text"] for b in buttons]


def test_warm_chat_card_keeps_send(db, bot_api):
    c = _contact(db, 559002, username="hanna_demo", inbound_hours_ago=2)
    out, err = tools.call_tool(db, "crm_save_message_draft", {"contact": f"#{c.id}", "text": "Дякую!"})
    assert not err
    assert _buttons(bot_api.calls[-1])[0]["text"] == "✅ Надіслати"


def test_tg_link_refused_card_is_resent_without_it(db, bot_api):
    bot_api.fail_first = True
    c = _contact(db, 559003)                      # без username — лише tg://user?id
    out, err = tools.call_tool(db, "crm_save_message_draft", {"contact": f"#{c.id}", "text": "Привіт"})
    assert not err
    first, second = bot_api.calls
    assert _buttons(first)[0]["url"] == "tg://user?id=559003"
    assert all("url" not in b for b in _buttons(second))
    assert "📋 Скопіювати текст" in [b["text"] for b in _buttons(second)]


def test_sent_myself_logs_the_text_and_closes_the_draft(client, bot_api):
    from app.core.database import SessionLocal
    from app.modules.interactions.models import Direction, Interaction
    from app.modules.telegram_bot.models import DraftStatus, TelegramDraft

    with SessionLocal() as db:
        c = _contact(db, 559004, username="hanna_demo")
        tools.call_tool(db, "crm_save_message_draft", {"contact": f"#{c.id}", "text": "Нагадую про зустріч"})
        draft_id = db.query(TelegramDraft).one().id
        contact_id = c.id
    fake = RecClient()
    dispatch(fake, {"update_id": 11, "callback_query": {
        "id": "cb", "data": f"d:o:{draft_id}", "message": {"chat": {"id": 42}, "message_id": 501}}})
    assert fake.callbacks[-1] == "✔ Записав у історію"
    with SessionLocal() as db:
        assert db.get(TelegramDraft, draft_id).status == DraftStatus.sent
        (out,) = db.query(Interaction).filter(Interaction.contact_id == contact_id,
                                              Interaction.direction == Direction.outbound).all()
        assert out.summary == "Нагадую про зустріч"


# ── В6: crm_update_contact ──────────────────────────────────────────────────


def _update(db, **args):
    out, err = tools.call_tool(db, "crm_update_contact", args)
    assert not err, out
    return json.loads(out)


def test_update_contact_fields(db):
    from app.modules.contacts.models import Frequency
    from app.modules.insights.models import ContactFact

    c = _contact(db, 559100)
    res = _update(db, contact=f"#{c.id}", importance="high", cadence_days=12,
                  note="Любить короткі повідомлення зранку")
    assert res["changed"]["importance"] == "high"
    assert res["changed"]["contact_frequency"] == "biweekly" and res["changed"]["cadence_days"] == 14
    db.refresh(c)
    assert c.importance == 3 and c.contact_frequency == Frequency.biweekly
    fact = db.get(ContactFact, res["changed"]["note_fact_id"])
    assert fact.value.startswith("Любить короткі") and fact.contact_id == c.id
    assert _update(db, contact=f"#{c.id}", importance="high")["changed"] == {}   # без змін


def test_do_not_contact_drops_from_due_list(db):
    c = _contact(db, 559200)
    c.created_at = datetime.now(timezone.utc) - timedelta(days=200)
    db.commit()
    due, _ = tools.call_tool(db, "crm_list_due_contacts", {})
    assert "Ганна Демо" in due
    assert _update(db, contact=f"#{c.id}", cadence_days=0)["changed"] == {"do_not_contact": True}
    due, _ = tools.call_tool(db, "crm_list_due_contacts", {})
    assert "Ганна Демо" not in due
    assert _update(db, contact=f"#{c.id}", do_not_contact=False)["changed"] == {"do_not_contact": False}


def test_update_contact_refuses_nonsense(db):
    c = _contact(db, 559300)
    for args in ({"importance": "vip"}, {"cadence_days": -3},
                 {"cadence_days": 0, "do_not_contact": False}):
        out, err = tools.call_tool(db, "crm_update_contact", {"contact": f"#{c.id}", **args})
        assert err, args


def test_queue_survives_telegram_refusal(db, tg, monkeypatch):
    from app.modules.automation import telegram

    cfg = external.get_settings(db)
    cfg.plan_enabled = False
    db.commit()
    _at(monkeypatch, 3, 0)
    _owner(db, "Не загубитись")
    quiet.hold_suggestion(db, chat_id=559900, message_id=1, name="Демо", kind="duty",
                          commitment_type="request", outgoing=False, due=None,
                          meeting=False, text="Надішли файл завтра")
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: False)
    assert quiet.release_pending(_at(monkeypatch, 7, 45)) is False
    db.expire_all()
    assert quiet.pending_count(db) == 2                 # нічого не загубилось
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: tg.append((text, kw)) or True)
    assert quiet.release_pending(_at(monkeypatch, 8, 0)) is True
    assert "Не загубитись" in tg[0][0] and "За ніч: 1 пропозиція" in tg[0][0]
