"""ALiSiO CRM: business_message спершу летить їй; handled>0 — чат її, мовчимо."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.modules.integrations import alisio
from app.modules.telegram_bot.handlers import dispatch
from app.modules.telegram_bot.models import TelegramDraft

SECRET = "test-secret"


class FakeClient:
    configured = True

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return {"message_id": 500 + len(self.sent), "chat": {"id": chat_id}}

    def call(self, method, **kw):
        return {}

    def download_file(self, file_id):
        return None


def _update(chat_id=555001, text="Привіт! Є вільні номери на жовтень?", msg_id=7):
    return {
        "update_id": 91,
        "business_message": {
            "message_id": msg_id,
            "business_connection_id": "bc-1",
            "chat": {"id": chat_id, "first_name": "Марта", "username": "marta_hotel"},
            "from": {"id": chat_id, "first_name": "Марта", "username": "marta_hotel"},
            "text": text,
        },
    }


@pytest.fixture
def alisio_on(monkeypatch):
    monkeypatch.setattr(settings, "alisio_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "telegram_chat_id", "42", raising=False)


@pytest.fixture
def alisio_http(monkeypatch):
    """Підміна httpx.post: записує запит, віддає заготовлену відповідь."""
    state = {"requests": [], "status": 200, "body": {"ok": True, "received": 1, "handled": 0}, "raise": None}

    def fake_post(url, *, json=None, headers=None, timeout=None):
        state["requests"].append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if state["raise"]:
            raise state["raise"]
        return httpx.Response(state["status"], json=state["body"], request=httpx.Request("POST", url))

    monkeypatch.setattr(alisio.httpx, "post", fake_post)
    return state


def _drafts() -> list[TelegramDraft]:
    with SessionLocal() as db:
        return list(db.scalars(select(TelegramDraft)))


def _interactions(chat_id=555001) -> int:
    from app.modules.contacts.models import Contact

    with SessionLocal() as db:
        c = db.scalar(select(Contact).where(Contact.telegram_chat_id == chat_id))
        return len(c.interactions) if c else 0


# ── Запит ────────────────────────────────────────────────────────────────────


def test_raw_update_is_forwarded_with_secret_header(alisio_on, alisio_http):
    upd = _update()
    assert alisio.forward_update(upd) == 0
    req = alisio_http["requests"][0]
    assert req["url"] == settings.alisio_webhook_url
    assert req["json"] == upd  # сирий JSON без змін
    assert req["headers"]["X-Telegram-Bot-Api-Secret-Token"] == SECRET
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["timeout"] == 5.0


def test_without_a_secret_nothing_is_sent(monkeypatch, alisio_http):
    monkeypatch.setattr(settings, "alisio_webhook_secret", None)
    assert alisio.forward_update(_update()) == 0
    assert alisio_http["requests"] == []


def test_other_update_kinds_are_not_forwarded(alisio_on, alisio_http):
    for upd in (
        {"update_id": 1, "message": {"text": "/today", "chat": {"id": 42}}},
        {"update_id": 2, "edited_business_message": {"chat": {"id": 1}}},
        {"update_id": 3, "deleted_business_messages": {"chat": {"id": 1}}},
        {"update_id": 4, "business_connection": {"id": "bc-1"}},
    ):
        assert alisio.forward_update(upd) == 0
    assert alisio_http["requests"] == []


@pytest.mark.parametrize("status", [401, 503, 500])
def test_http_errors_mean_not_handled(alisio_on, alisio_http, status):
    alisio_http["status"] = status
    alisio_http["body"] = {"ok": False}
    assert alisio.forward_update(_update()) == 0
    assert len(alisio_http["requests"]) == 1  # без повторів


def test_timeout_and_bad_json_mean_not_handled(alisio_on, alisio_http):
    alisio_http["raise"] = httpx.ReadTimeout("slow")
    assert alisio.forward_update(_update()) == 0
    alisio_http["raise"] = None
    alisio_http["body"] = "not json"
    assert alisio.forward_update(_update()) == 0


def test_handled_count_is_read_from_the_reply(alisio_on, alisio_http):
    alisio_http["body"] = {"ok": True, "received": 1, "handled": 1}
    assert alisio.forward_update(_update()) == 1


# ── Наскрізно через бота ─────────────────────────────────────────────────────


def test_lead_chat_is_left_to_alisio_but_stays_in_history(client, alisio_on, alisio_http):
    alisio_http["body"] = {"ok": True, "received": 1, "handled": 1}
    fake = FakeClient()
    dispatch(fake, _update())
    assert fake.sent == []  # жодної картки власнику
    assert _drafts() == []  # жодної чернетки
    assert _interactions() == 1  # але памʼять є


def test_foreign_chat_is_handled_as_before(client, alisio_on, alisio_http):
    alisio_http["body"] = {"ok": True, "received": 1, "handled": 0}
    fake = FakeClient()
    dispatch(fake, _update(chat_id=555002))
    assert len(fake.sent) == 1 and "Марта" in fake.sent[0]
    assert len(_drafts()) == 1
    assert len(alisio_http["requests"]) == 1


def test_alisio_is_asked_before_any_reply_is_formed(client, alisio_on, alisio_http, monkeypatch):
    from app.modules.telegram_bot import service

    order = []
    alisio_http["body"] = {"ok": True, "received": 1, "handled": 1}
    original = alisio.forward_update

    def spy(update):
        order.append("alisio")
        return original(update)

    monkeypatch.setattr(alisio, "forward_update", spy)
    real_incoming = service.handle_incoming
    monkeypatch.setattr(service, "handle_incoming", lambda *a, **kw: (order.append("draft") or real_incoming(*a, **kw)))
    dispatch(FakeClient(), _update())
    assert order == ["alisio"]  # чернетку навіть не починали


def test_status_route_reports_alisio(client, alisio_on):
    assert client.get("/api/integrations/telegram/status").json()["alisio"] is True
