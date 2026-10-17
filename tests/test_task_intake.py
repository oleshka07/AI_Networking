"""Telegram → задачі: розпізнавання обіцянок і прохань, пропозиції, «попроси …»."""

from datetime import date, timedelta

import pytest

from app.modules.tasks.intake import detect_commitment, due_from_text, kind_for
from app.modules.telegram_bot import task_intake
from app.modules.telegram_bot.handlers import dispatch

from test_telegram_bot import FakeClient, _business_update

TODAY = date(2026, 10, 14)  # середа


# ── Розпізнавання ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,kind,due", [
    # українська
    ("Надішлю рахунок до пʼятниці", "promise", date(2026, 10, 16)),
    ("надішли, будь ласка, договір завтра", "request", date(2026, 10, 15)),
    ("Скину фото презентації", "promise", None),
    ("давай зідзвонимось у вівторок", "meeting", date(2026, 10, 20)),
    # російська
    ("Пришлю отчет в понедельник", "promise", date(2026, 10, 19)),
    ("Отправь счет до пятницы", "request", date(2026, 10, 16)),
    # чеська
    ("Pošlu fakturu zítra", "promise", date(2026, 10, 15)),
    ("Pošli mi smlouvu do pátku", "request", date(2026, 10, 16)),
    ("Zavoláme v úterý v 10:00", "meeting", date(2026, 10, 20)),
    # англійська
    ("I'll send the report by Friday", "promise", date(2026, 10, 16)),
    ("Can you send me the invoice?", "request", None),
    ("Let's have a call tomorrow at 15:00", "meeting", date(2026, 10, 15)),
])
def test_detects_commitments(text, kind, due):
    found = detect_commitment(text, TODAY)
    assert found is not None, text
    assert found["type"] == kind
    assert found["due"] == due


@pytest.mark.parametrize("text", [
    "надішлю привіт мамі",          # ні строку, ні предмета
    "передам привіт усім",
    "I'll do it",
    "ок, дякую",
    "Ахахаха, класно",
    "дійду до тебе пізніше",
    "Zavolám ti",                    # без строку й предмета
    "скину кіно ввечері подивитись", # «ввечері» — це сьогодні, тож строк є
])
def test_ignores_chitchat(text):
    found = detect_commitment(text, TODAY)
    if text.startswith("скину кіно"):
        # Межовий випадок: строк є («ввечері»), тож це обіцянка — і це ок.
        assert found and found["type"] == "promise"
    else:
        assert found is None, text


def test_due_parsing():
    assert due_from_text("до кінця тижня", TODAY) == date(2026, 10, 16)
    assert due_from_text("next week", TODAY) == date(2026, 10, 19)
    assert due_from_text("до 20.10", TODAY) == date(2026, 10, 20)
    assert due_from_text("до 01.02", TODAY) == date(2027, 2, 1)  # дата вже минула — наступний рік
    assert due_from_text("до понеділка", TODAY) == date(2026, 10, 19)
    assert due_from_text("без строку", TODAY) is None


def test_whose_task_it_is():
    assert kind_for("promise", outgoing=True) == "duty"          # я пообіцяв
    assert kind_for("request", outgoing=False) == "duty"         # мене попросили
    assert kind_for("request", outgoing=True) == "expectation"   # я попросив
    assert kind_for("promise", outgoing=False) == "expectation"  # мені пообіцяли
    assert kind_for("meeting", outgoing=True) == "task"


# ── Пропозиції в Telegram ───────────────────────────────────────────────────


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.telegram_chat_id", "42", raising=False)
    task_intake._last_suggested.clear()
    return 42


def _owner_update(chat_id=555100, text="Надішлю рахунок до пʼятниці", msg_id=31):
    """Своє повідомлення власника в бізнес-чаті: from ≠ chat."""
    return {
        "update_id": 2,
        "business_message": {
            "message_id": msg_id,
            "business_connection_id": "bc-1",
            "chat": {"id": chat_id, "first_name": "Марта", "username": "marta_demo"},
            "from": {"id": 42, "first_name": "Власник"},
            "text": text,
        },
    }


def _suggestions(fake):
    return [m for m in fake.sent if "Схоже на задачу" in m["text"]]


def test_own_promise_offers_a_task_and_button_creates_it(client, admin):
    fake = FakeClient()
    dispatch(fake, _owner_update())
    (card,) = _suggestions(fake)
    buttons = card["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["➕ Задача", "✖"]
    add = buttons[0]["callback_data"]
    assert add == "tg:add:d:555100:31"

    for _ in range(2):  # повторне натискання не дублює
        dispatch(fake, {"update_id": 3, "callback_query": {
            "id": "cb", "data": add, "message": {"chat": {"id": 42}, "message_id": 900}}})
    from app.core.database import SessionLocal
    from app.modules.tasks.models import Task, TaskKind

    with SessionLocal() as db:
        (task,) = db.query(Task).filter(Task.source == "telegram").all()
        assert task.external_id == "tgb:555100:31"
        assert task.kind == TaskKind.duty
        assert task.counterpart.startswith("Марта")
        assert task.title.startswith("Обіцяв Марта")
        assert task.due_date is not None and task.due_date.weekday() == 4
        assert task.notes == "«Надішлю рахунок до пʼятниці»"
    assert fake.callbacks[-1] == "Задача вже є"


def test_incoming_request_from_any_contact(client, admin):
    fake = FakeClient()
    dispatch(fake, _business_update(chat_id=555200, text="Надішли, будь ласка, договір до понеділка", msg_id=5))
    (card,) = _suggestions(fake)
    assert "тобі написали" in card["text"] and "я винен" in card["text"]
    # Друге прохання з того самого чату за годину — мовчимо.
    dispatch(fake, _business_update(chat_id=555200, text="І ще скинь рахунок завтра", msg_id=6))
    assert len(_suggestions(fake)) == 1


def test_meeting_offers_calendar(client, admin):
    fake = FakeClient()
    dispatch(fake, _business_update(chat_id=555300, text="Давай зідзвонимось у вівторок о 15:00", msg_id=8))
    (card,) = _suggestions(fake)
    labels = [b["text"] for b in card["reply_markup"]["inline_keyboard"][0]]
    assert labels == ["➕ Задача", "📅 У календар", "✖"]


def test_chitchat_is_silent(client, admin):
    fake = FakeClient()
    dispatch(fake, _owner_update(text="надішлю привіт мамі"))
    dispatch(fake, _business_update(chat_id=555400, text="Ахахах, класно", msg_id=9))
    assert _suggestions(fake) == []


def test_alisio_chats_are_left_alone(client, admin, monkeypatch):
    from app.modules.integrations import alisio

    monkeypatch.setattr(alisio, "forward_update", lambda update: 1)
    fake = FakeClient()
    dispatch(fake, _owner_update())
    dispatch(fake, _business_update(chat_id=555500, text="Надішли рахунок завтра", msg_id=10))
    assert _suggestions(fake) == []


def test_modes_auto_and_off(client, admin):
    from app.core.database import SessionLocal
    from app.modules.tasks import external
    from app.modules.tasks.models import Task

    with SessionLocal() as db:
        external.get_settings(db).tg_suggest_mode = "auto"
        db.commit()
    fake = FakeClient()
    dispatch(fake, _owner_update(msg_id=41))
    assert _suggestions(fake) == []
    with SessionLocal() as db:
        assert db.query(Task).filter(Task.source == "telegram").count() == 1
        external.get_settings(db).tg_suggest_mode = "off"
        db.commit()
    dispatch(fake, _owner_update(msg_id=42, text="Скину договір завтра"))
    with SessionLocal() as db:
        assert db.query(Task).filter(Task.source == "telegram").count() == 1
    assert _suggestions(fake) == []


def test_paused_contact_gets_task_button_on_the_same_card(client, admin):
    from app.core.database import SessionLocal
    from app.modules.contacts.models import Contact, Frequency

    with SessionLocal() as db:
        db.add(Contact(first_name="Петро", telegram_chat_id=555600,
                       contact_frequency=Frequency.monthly, auto_reply_paused=True))
        db.commit()
    fake = FakeClient()
    dispatch(fake, _business_update(chat_id=555600, text="Пришли счет до пятницы", msg_id=12))
    assert len(fake.sent) == 1 and "ти відповідаєш сам" in fake.sent[0]["text"]
    first_row = fake.sent[0]["reply_markup"]["inline_keyboard"][0]
    assert first_row[0]["callback_data"] == "tg:add:d:555600:12"


def test_dismiss_button(client, admin):
    fake = FakeClient()
    dispatch(fake, {"update_id": 5, "callback_query": {
        "id": "cb", "data": "tg:x", "message": {"chat": {"id": 42}, "message_id": 77}}})
    assert fake.deleted == [{"chat_id": 42, "message_id": 77}]


# ── «Попроси Марію …» ───────────────────────────────────────────────────────


def _contacts(*rows):
    from app.core.database import SessionLocal
    from app.modules.contacts.models import Contact, Frequency
    from app.modules.telegram_bot.models import BusinessConnection

    with SessionLocal() as db:
        for first, last, chat in rows:
            db.add(Contact(first_name=first, last_name=last, telegram_chat_id=chat,
                           contact_frequency=Frequency.monthly))
        db.add(BusinessConnection(connection_id="bc-1", user_chat_id=42, is_enabled=True))
        db.commit()


def test_parse_delegate():
    assert task_intake.parse_delegate("попроси Марію надіслати рахунок до пʼятниці") == (
        "Марію", "надіслати рахунок до пʼятниці")
    assert task_intake.parse_delegate("попроси Петра, щоб скинув звіт") == ("Петра", "скинув звіт")
    assert task_intake.parse_delegate("ask Anna to send the deck") == ("Anna", "send the deck")
    assert task_intake.parse_delegate("нагадай написати Олегу") is None


def test_delegate_makes_draft_and_expectation(client, admin):
    _contacts(("Марія", "Демченко", 700001))
    fake = FakeClient()
    dispatch(fake, {"update_id": 6, "message": {"chat": {"id": 42},
                    "text": "попроси Марію надіслати рахунок до пʼятниці"}})
    card = fake.sent[-1]
    assert "чернетка прохання" in card["text"] and "Марія, прохання: надіслати рахунок" in card["text"]
    # Марія ще не писала за 24 год — бот від імені власника сюди не напише.
    buttons = [b["text"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[:2] == ["🔗 Відкрити чат", "📋 Скопіювати текст"]
    assert "✅ Надіслати" not in buttons

    from app.core.database import SessionLocal
    from app.modules.tasks.models import Task, TaskKind
    from app.modules.telegram_bot.models import DraftStatus, TelegramDraft

    with SessionLocal() as db:
        (task,) = db.query(Task).all()
        assert task.kind == TaskKind.expectation
        assert task.title.startswith("Перевірити: Марія Демченко")
        assert task.counterpart == "Марія Демченко"
        assert task.due_date is not None and task.due_date.weekday() == 4
        draft = db.query(TelegramDraft).one()
        assert draft.status == DraftStatus.pending and draft.chat_id == 700001


def test_delegate_ambiguous_asks_instead_of_guessing(client, admin):
    _contacts(("Марія", "Демченко", 700002), ("Марія", "Коваль", 700003))
    fake = FakeClient()
    dispatch(fake, {"update_id": 7, "message": {"chat": {"id": 42},
                    "text": "попроси Марію скинути договір завтра"}})
    ask = fake.sent[-1]
    assert ask["text"].startswith("Кого саме попросити")
    choices = ask["reply_markup"]["inline_keyboard"]
    assert len(choices) == 2

    from app.core.database import SessionLocal
    from app.modules.tasks.models import Task

    with SessionLocal() as db:
        assert db.query(Task).count() == 0  # не вгадуємо

    dispatch(fake, {"update_id": 8, "callback_query": {
        "id": "cb", "data": choices[1][0]["callback_data"],
        "message": {"chat": {"id": 42}, "message_id": 55}}})
    with SessionLocal() as db:
        (task,) = db.query(Task).all()
        assert task.counterpart == "Марія Коваль"
        assert task.due_date == date.today() + timedelta(days=1)


def test_delegate_unknown_person(client, admin):
    fake = FakeClient()
    dispatch(fake, {"update_id": 9, "message": {"chat": {"id": 42},
                    "text": "попроси Зенона надіслати звіт"}})
    assert "Не знайшов" in fake.sent[-1]["text"]
