"""Holos: підключення Business і ВЛАСНІ повідомлення власника летять туди; клієнтські - лише коли Holos попросив."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.modules.integrations import holos
from app.modules.telegram_bot.handlers import dispatch

URL = "https://holos.example/api/webhooks/telegram/relay/12345"
SECRET = "relay-secret"
OWNER = 900100
CLIENT = 555001


class FakeClient:
    configured = True

    def __init__(self):
        self.sent = []
        self.calls = []

    def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return {"message_id": 500 + len(self.sent), "chat": {"id": chat_id}}

    def call(self, method, **kw):
        self.calls.append((method, kw))
        if method == "getBusinessConnection":
            return {"id": kw["business_connection_id"], "user": {"id": OWNER}, "rights": {"can_manage_stories": True}, "is_enabled": True}
        return {}

    def download_file(self, file_id):
        return None


@pytest.fixture
def holos_on(monkeypatch):
    monkeypatch.setattr(settings, "holos_relay_url", URL)
    monkeypatch.setattr(settings, "holos_relay_secret", SECRET)
    monkeypatch.setattr(settings, "alisio_webhook_secret", None)
    monkeypatch.setattr(settings, "telegram_chat_id", "42", raising=False)
    # фоновий потік - синхронно, щоб тест бачив запит одразу
    monkeypatch.setattr(holos, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(holos, "_want_clients", False)


@pytest.fixture
def holos_http(monkeypatch):
    state = {"requests": [], "status": 200, "body": {"ok": True, "received": 1, "handled": 0, "did": "note"}, "raise": None}

    def fake_post(url, *, json=None, headers=None, timeout=None):
        state["requests"].append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if state["raise"]:
            raise state["raise"]
        return httpx.Response(state["status"], json=state["body"], request=httpx.Request("POST", url))

    monkeypatch.setattr(holos.httpx, "post", fake_post)
    return state


def _msg(sender, text="Я завжди кажу клієнтам: спершу фото до, потім кошторис, інакше сперечаємось про невидиме.", msg_id=7):
    return {
        "update_id": 91,
        "business_message": {
            "message_id": msg_id,
            "date": 1760000000,
            "business_connection_id": "bc-1",
            "chat": {"id": CLIENT, "first_name": "Марта", "username": "marta_hotel"},
            "from": {"id": sender, "first_name": "Олег" if sender == OWNER else "Марта", "username": "oleg" if sender == OWNER else "marta_hotel"},
            "text": text,
        },
    }


def test_own_message_goes_to_holos_with_secret_and_without_names(client, holos_on, holos_http):
    dispatch(FakeClient(), _msg(OWNER))
    assert len(holos_http["requests"]) == 1
    req = holos_http["requests"][0]
    assert req["url"] == URL
    assert req["headers"]["X-Telegram-Bot-Api-Secret-Token"] == SECRET
    assert req["timeout"] == 5.0
    bm = req["json"]["business_message"]
    assert bm["from"] == {"id": OWNER}
    assert bm["chat"] == {"id": CLIENT}
    assert bm["business_connection_id"] == "bc-1" and bm["message_id"] == 7 and bm["date"] == 1760000000
    assert "фото до" in bm["text"]
    assert "Марта" not in str(req["json"]) and "marta_hotel" not in str(req["json"])


def test_client_message_is_not_forwarded_until_holos_asks(client, holos_on, holos_http):
    fake = FakeClient()
    dispatch(fake, _msg(CLIENT, text="Скільки коштує прибирання квартири 60 м²?"))
    assert holos_http["requests"] == []
    assert len(fake.sent) == 1  # бот, як і раніше, показав власнику картку з чернеткою


def test_connection_update_is_forwarded_as_is(client, holos_on, holos_http):
    bc = {"id": "bc-1", "user": {"id": OWNER, "first_name": "Олег"}, "is_enabled": True, "rights": {"can_manage_stories": True}}
    dispatch(FakeClient(), {"update_id": 5, "business_connection": bc})
    assert holos_http["requests"][0]["json"] == {"business_connection": bc}


def test_nothing_is_sent_without_config(client, monkeypatch, holos_http):
    monkeypatch.setattr(settings, "holos_relay_url", None)
    monkeypatch.setattr(settings, "holos_relay_secret", None)
    monkeypatch.setattr(settings, "alisio_webhook_secret", None)
    monkeypatch.setattr(holos, "_spawn", lambda fn, *a: fn(*a))
    dispatch(FakeClient(), _msg(OWNER))
    dispatch(FakeClient(), {"update_id": 5, "business_connection": {"id": "bc-1", "user": {"id": OWNER}}})
    assert holos_http["requests"] == []
    assert holos.configured() is False


@pytest.mark.parametrize("status", [401, 500])
def test_http_errors_are_swallowed_without_retries(holos_on, holos_http, status):
    holos_http["status"] = status
    assert holos._post({"business_connection": {"id": "bc-1"}}) in ("401", f"http {status}")
    assert len(holos_http["requests"]) == 1


def test_timeout_does_not_raise(holos_on, holos_http):
    holos_http["raise"] = httpx.ReadTimeout("slow")
    assert holos._post({"business_connection": {"id": "bc-1"}}) == "timeout"


def test_owner_message_still_reaches_history_and_bot_flow(client, holos_on, holos_http):
    """Пересилання в Holos нічого не забирає в бота: власне повідомлення лягає в історію контакту."""
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.modules.contacts.models import Contact

    dispatch(FakeClient(), _msg(OWNER))
    with SessionLocal() as db:
        c = db.scalar(select(Contact).where(Contact.telegram_chat_id == CLIENT))
        assert c is not None and len(c.interactions) == 1


def test_startup_sync_sends_stored_connections_fresh_from_telegram(client, holos_on, holos_http):
    from app.core.database import SessionLocal
    from app.modules.telegram_bot import service

    with SessionLocal() as db:
        service.upsert_connection(db, connection_id="bc-1", user_chat_id=OWNER, is_enabled=True)
    fake = FakeClient()
    assert holos.sync_connections(fake) == 1
    assert ("getBusinessConnection", {"business_connection_id": "bc-1"}) in fake.calls
    assert holos_http["requests"][0]["json"]["business_connection"]["rights"]["can_manage_stories"] is True


def test_status_route_reports_holos(client, holos_on):
    assert client.get("/api/integrations/telegram/status").json()["holos"] is True


def test_client_messages_follow_holos_answer(client, holos_on, holos_http):
    """Holos каже clients:true у відповіді - клієнтів шлемо (без імен), clients:false - перестаємо."""
    fake = FakeClient()
    holos_http["body"] = {"ok": True, "did": "note", "clients": True}
    dispatch(fake, _msg(OWNER))  # будь-яка відповідь Holos несе його бажання
    assert holos.wants_clients() is True
    dispatch(fake, _msg(CLIENT, text="Скільки коштує прибирання після ремонту?", msg_id=8))
    assert len(holos_http["requests"]) == 2
    req = holos_http["requests"][1]["json"]
    assert req["business_message"]["from"] == {"id": CLIENT}
    assert req["business_message"]["text"].startswith("Скільки коштує")
    assert "Марта" not in str(req) and "marta_hotel" not in str(req)
    assert len(fake.sent) == 1  # бот, як і раніше, показав власнику картку з чернеткою

    holos_http["body"] = {"ok": True, "did": "skip:not-owner", "clients": False}
    dispatch(fake, _msg(CLIENT, text="А вікна теж миєте?", msg_id=9))  # ще летить: бот дізнається з відповіді
    dispatch(fake, _msg(CLIENT, text="А в суботу можна?", msg_id=10))
    assert len(holos_http["requests"]) == 3
    assert holos.wants_clients() is False


def test_answer_without_clients_field_keeps_last_known(holos_on, holos_http):
    holos_http["body"] = {"ok": True, "did": "connection", "clients": True}
    holos._post({"business_connection": {"id": "bc-1"}})
    holos_http["body"] = {"ok": True, "did": "note"}  # старий Holos без поля
    holos._post({"business_connection": {"id": "bc-1"}})
    assert holos.wants_clients() is True


def test_voice_without_transcript_is_not_forwarded(client, holos_on, holos_http, monkeypatch):
    monkeypatch.setattr(holos, "_want_clients", True)
    holos.forward_client_message({"business_connection_id": "bc-1", "message_id": 1}, "[голосове повідомлення]")
    holos.forward_own_message({"business_connection_id": "bc-1", "message_id": 2}, "[голосове повідомлення]")
    assert holos_http["requests"] == []


def test_clients_not_forwarded_without_config_even_if_asked(client, monkeypatch, holos_http):
    monkeypatch.setattr(settings, "holos_relay_url", None)
    monkeypatch.setattr(settings, "holos_relay_secret", None)
    monkeypatch.setattr(holos, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(holos, "_want_clients", True)
    holos.forward_client_message({"business_connection_id": "bc-1", "message_id": 1}, "Скільки коштує прибирання?")
    assert holos_http["requests"] == []
