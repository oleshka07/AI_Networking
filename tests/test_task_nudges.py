"""Нагадування про задачі: тиша, стеля, блокери, строки, ранковий план, підготовка."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.tasks import external, nudges
from app.modules.tasks import service as tasks_service
from app.modules.tasks.models import Task, TaskKind, TaskStatus

NOW = datetime.now(timezone.utc)


def _task(db, title, **kw):
    t = Task(uid=tasks_service.new_uid(), title=title, **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _local(hour, minute=0, day=None):
    day = day or date.today()
    return datetime(day.year, day.month, day.day, hour, minute)


# ── Тиша і денна стеля ──────────────────────────────────────────────────────


@pytest.mark.parametrize("hm,quiet", [
    ((23, 0), True), ((22, 30), True), ((3, 0), True), ((7, 29), True),
    ((7, 30), False), ((12, 0), False), ((22, 29), False),
])
def test_quiet_window_wraps_midnight(db, hm, quiet):
    cfg = external.get_settings(db)
    assert nudges.in_quiet(cfg, _local(*hm)) is quiet


def test_daily_cap_and_reset(db):
    cfg = external.get_settings(db)
    cfg.daily_cap = 2
    db.commit()
    noon = _local(12)
    assert nudges.allow_proactive(db, noon)
    assert nudges.allow_proactive(db, noon)
    assert not nudges.allow_proactive(db, noon)       # стеля вичерпана
    assert not nudges.allow_proactive(db, _local(23))  # і тиша
    tomorrow = _local(12, day=date.today() + timedelta(days=1))
    assert nudges.allow_proactive(db, tomorrow)       # новий день — новий рахунок


# ── Відкладення ─────────────────────────────────────────────────────────────


def test_snoozed_task_does_not_remind(db):
    t = _task(db, "Відкладена", blocks_work=True, created_at=NOW - timedelta(days=3),
              due_date=date.today())
    tasks_service.snooze(db, t)
    assert tasks_service.to_utc(t.snoozed_until) > NOW
    assert t not in nudges.open_tasks(db)
    assert nudges.stale_blockers(db) == []
    assert nudges.deadline_tasks(db) == []
    # Знялось відкладення — задача знову в грі.
    t.snoozed_until = NOW - timedelta(minutes=1)
    db.commit()
    assert t in nudges.stale_blockers(db)


def test_snooze_is_tomorrow_nine_local():
    until = tasks_service.snooze_until_tomorrow().astimezone()
    assert until.hour == 9 and until.minute == 0
    assert until.date() == (datetime.now().astimezone() + timedelta(days=1)).date()


def test_snoozed_reminder_task_is_not_due(db):
    t = _task(db, "Оплатити домен", item_type="нагадування", recur_days=30,
              next_remind_at=NOW - timedelta(hours=1))
    assert t in tasks_service.due_reminder_tasks(db)
    tasks_service.snooze(db, t)
    assert t not in tasks_service.due_reminder_tasks(db)


# ── Блокери ─────────────────────────────────────────────────────────────────


def test_blocker_ping_rules(db):
    _task(db, "Свіжий блокер", blocks_work=True, created_at=NOW - timedelta(hours=2))
    assert nudges.blocker_ping(db, local_now=_local(10), now=NOW) is None  # ще не доба

    old = _task(db, "Підписати договір", blocks_work=True,
                created_at=NOW - timedelta(hours=30))
    assert nudges.blocker_ping(db, local_now=_local(22), now=NOW) is None  # поза вікном
    assert nudges.blocker_ping(db, local_now=_local(8), now=NOW) is None

    first = nudges.blocker_ping(db, local_now=_local(10), now=NOW)
    assert first and "Підписати договір" in first[0] and first[1] == [old]
    # Не частіше разу на 6 годин.
    assert nudges.blocker_ping(db, local_now=_local(11), now=NOW + timedelta(hours=1)) is None
    assert nudges.blocker_ping(db, local_now=_local(15), now=NOW + timedelta(hours=5, minutes=59)) is None
    assert nudges.blocker_ping(db, local_now=_local(16), now=NOW + timedelta(hours=6, minutes=1))


def test_blocker_closed_is_silent(db):
    t = _task(db, "Закрита", blocks_work=True, created_at=NOW - timedelta(days=2))
    t.status = TaskStatus.done
    db.commit()
    assert nudges.blocker_ping(db, local_now=_local(10), now=NOW) is None


def test_blocks_work_goes_first(db):
    _task(db, "Звичайна сьогодні", due_date=date.today(), duration_min=5)
    _task(db, "Негайна", status=TaskStatus.immediate)
    _task(db, "Тримає роботу", blocks_work=True, duration_min=300)
    assert [t.title for t in tasks_service.list_tasks(db)][0] == "Тримає роботу"


# ── Строки і ранковий план ──────────────────────────────────────────────────


def test_deadlines_block(db):
    today = date.today()
    _task(db, "Сьогодні", due_date=today)
    _task(db, "Вчора", due_date=today - timedelta(days=1))
    _task(db, "Перевірити рахунок", due_date=today, kind=TaskKind.expectation)
    _task(db, "Завтра", due_date=today + timedelta(days=1))
    text, tasks = nudges.deadlines_block(db, today=today)
    assert "сьогодні 2, прострочено 1" in text
    assert "Завтра" not in text and "Перевірити рахунок" in text
    assert len(tasks) == 3


def test_deadlines_silent_when_nothing_due(db):
    _task(db, "Без строку")
    assert nudges.deadlines_block(db) is None


def test_plan_block_top_three_and_meetings(db):
    for n in range(5):
        _task(db, f"Задача {n}", duration_min=10 + n)
    _task(db, "Чекаю від когось", kind=TaskKind.expectation, duration_min=1)
    _task(db, "Тримає роботу", blocks_work=True, duration_min=600)
    prep = _task(db, "Підготуватися: Демо", source="calendar", external_id="ev-1")
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    events = [{"id": "ev-1", "summary": "Демо", "start": start, "attendee_emails": []},
              {"id": "ev-2", "summary": "Обід", "start": start, "attendee_emails": []}]
    text, tasks = nudges.plan_block(db, events=events)
    assert [t.title for t in tasks][0] == "Тримає роботу"
    assert len(tasks) == 3 and all(t.kind != TaskKind.expectation for t in tasks)
    assert "Демо · підготовка є" in text and "Обід · без підготовки" in text
    assert prep is not None


def test_plan_block_silent_and_switchable(db):
    assert nudges.plan_block(db, events=[]) is None
    _task(db, "Щось")
    cfg = external.get_settings(db)
    cfg.plan_enabled = False
    db.commit()
    assert nudges.plan_block(db, events=[]) is None


def test_keyboard_has_snooze_and_card_link(db):
    t = _task(db, "З карткою", external_url="https://example.invalid/c#t-demo")
    rows = nudges.task_keyboard([t])["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["✓ Готово", "▶ Взяв у роботу"]
    assert rows[1][0]["callback_data"] == f"tk:snz:{t.id}"
    assert rows[1][1]["url"].endswith("#t-demo")
    bare = _task(db, "Без картки")
    assert len(nudges.task_keyboard([bare])["inline_keyboard"][1]) == 1


# ── Підготовка до зустрічей ─────────────────────────────────────────────────


def _event(eid, summary, emails=(), day_offset=1, hour=11):
    local = datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0)
    start = (local + timedelta(days=day_offset)).astimezone(timezone.utc)
    return {"id": eid, "summary": summary, "start": start, "attendee_emails": list(emails),
            "html_link": f"https://calendar.example.invalid/{eid}"}


def test_prep_task_created_once_per_event(db):
    from app.modules.contacts.models import Contact

    c = Contact(first_name="Ірина", last_name="Демченко", email="iryna@example.invalid",
                company="Демо Груп")
    db.add(c)
    db.commit()
    related = _task(db, "Надіслати Ірині пропозицію")
    events = [
        _event("ev-ext", "Обговорення пілоту", ["iryna@example.invalid"]),
        _event("ev-word", "Call with the team"),
        _event("ev-lunch", "Обід"),
        _event("ev-today", "Зустріч сьогодні", ["x@example.invalid"], day_offset=0),
    ]
    tomorrow = date.today() + timedelta(days=1)
    created = nudges.create_prep_tasks(db, events, tomorrow)
    assert {t.external_id for t in created} == {"ev-ext", "ev-word"}
    task = next(t for t in created if t.external_id == "ev-ext")
    assert task.title == "Підготуватися: Обговорення пілоту"
    assert task.source == "calendar" and task.due_date == tomorrow
    assert task.external_url.endswith("/ev-ext")
    assert "Ірина Демченко" in task.notes and f"#{related.uid}" in task.notes

    assert nudges.create_prep_tasks(db, events, tomorrow) == []  # не дублюється
    assert db.query(Task).filter(Task.source == "calendar").count() == 2


def test_no_prep_button_is_final(db):
    events = [_event("ev-x", "Зустріч із клієнтом")]
    tomorrow = date.today() + timedelta(days=1)
    (task,) = nudges.create_prep_tasks(db, events, tomorrow)
    nudges.mark_no_prep(db, task)
    assert nudges.create_prep_tasks(db, events, tomorrow) == []
    assert nudges.has_prep(db, "ev-x") is False


def test_run_prep_sends_one_message_and_respects_quiet(db, monkeypatch):
    from app.modules.automation import telegram

    sent = []
    monkeypatch.setattr(nudges, "_configured", lambda: True)
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: sent.append((text, kw)) or True)
    events = [_event("ev-1", "Зустріч A", ["a@example.invalid"]),
              _event("ev-2", "Зустріч B", ["b@example.invalid"])]
    created = nudges.run_prep(db, local_now=_local(18), events=events)
    assert len(created) == 2 and len(sent) == 1
    markup = sent[0][1]["reply_markup"]["inline_keyboard"]
    assert markup[0][0]["callback_data"] == f"tk:noprep:{created[0].id}"

    # У години тиші задачі все одно створюються, а повідомлення — ні.
    more = [_event("ev-3", "Зустріч C", ["c@example.invalid"])]
    nudges.run_prep(db, local_now=_local(23), events=more)
    assert len(sent) == 1
    assert db.query(Task).filter(Task.source == "calendar").count() == 3


def test_snooze_and_noprep_buttons(client, monkeypatch):
    from app.core.database import SessionLocal
    from app.modules.telegram_bot import handlers

    answers = []

    class Bot:
        def answer_callback(self, cb_id, text=None):
            answers.append(text)

        def send_message(self, *a, **kw):
            raise AssertionError("⏰ і «без підготовки» не шлють «далі»")

    t = client.post("/api/tasks", json={"title": "Відкласти"}).json()
    handlers._handle_task_callback(Bot(), "cb", f"tk:snz:{t['id']}", 42)
    assert answers[-1].startswith("⏰")
    with SessionLocal() as db:
        assert db.get(Task, t["id"]).snoozed_until is not None

    handlers._handle_task_callback(Bot(), "cb", f"tk:noprep:{t['id']}", 42)
    assert answers[-1] == "🚫 Без підготовки"
    assert client.get("/api/tasks").json() == []


def test_midday_checkin_carries_deadlines(db, monkeypatch):
    from app.core.config import settings
    from app.modules.automation import reviews, telegram

    monkeypatch.setattr(settings, "checkin_enabled", True, raising=False)
    monkeypatch.setattr(reviews, "_configured", lambda: True)
    monkeypatch.setattr(reviews, "midday_checkin_text", lambda db: None)
    sent = []
    monkeypatch.setattr(telegram, "send_message", lambda text, **kw: sent.append((text, kw)) or True)

    assert reviews.run_midday_checkin() is False  # нема строків — тиша
    _task(db, "Строк сьогодні", due_date=date.today())
    assert reviews.run_midday_checkin() is True
    text, kw = sent[-1]
    assert "Строки" in text and "Строк сьогодні" in text
    assert kw["reply_markup"]["inline_keyboard"]


def test_settings_api(client):
    got = client.get("/api/tasks/settings").json()
    assert got["prep_hour"] == 18 and got["quiet_from"] == "22:30"
    assert got["tg_suggest_mode"] == "suggest"
    assert got["sync"]["open"] == 0 and got["sync"]["last_at"] is None
    upd = client.patch("/api/tasks/settings",
                       json={"prep_hour": 17, "tg_suggest_mode": "auto"}).json()
    assert upd["prep_hour"] == 17 and upd["tg_suggest_mode"] == "auto"
    assert client.patch("/api/tasks/settings", json={"quiet_from": "25:00"}).status_code == 422
    assert client.patch("/api/tasks/settings", json={"tg_suggest_mode": "loud"}).status_code == 422


def test_site_quick_actions_and_all_kinds(client):
    from datetime import datetime as _dt

    a = client.post("/api/tasks", json={"title": "Своя", "blocks_work": True,
                                        "external_url": "https://example.invalid/c#t-x"}).json()
    client.post("/api/tasks", json={"title": "Чекаю", "kind": "expectation"})
    assert a["source"] == "manual" and a["blocks_work"] is True
    assert a["external_url"].endswith("#t-x")
    assert len(client.get("/api/tasks").json()) == 1            # як і раніше: лише task
    assert len(client.get("/api/tasks?kind=all").json()) == 2   # адмінка бачить усе
    assert len(client.get("/api/tasks?kind=").json()) == 2
    assert client.get("/api/tasks?kind=nope").status_code == 422

    until = tasks_service.snooze_until_tomorrow().isoformat()
    snoozed = client.patch(f"/api/tasks/{a['id']}", json={"snoozed_until": until}).json()
    assert _dt.fromisoformat(snoozed["snoozed_until"].replace("Z", "+00:00"))
    cleared = client.patch(f"/api/tasks/{a['id']}", json={"snoozed_until": None}).json()
    assert cleared["snoozed_until"] is None
    done = client.patch(f"/api/tasks/{a['id']}", json={"status": "done"}).json()
    assert done["done_at"] is not None
