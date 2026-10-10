"""Holos: підключення Business і ВЛАСНІ повідомлення власника летять туди; чужі - ніколи."""

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


def test_client_message_is_never_forwarded(client, holos_on, holos_http):
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
