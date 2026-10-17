"""MCP: чисті функції протоколу + наскрізний прогін живим клієнтом."""

import json

import pytest

from app.modules.mcp import protocol, tools

# ── Чисті функції ────────────────────────────────────────────────────────────


def test_version_is_echoed_when_known_else_latest():
    assert protocol.negotiate_version("2024-11-05") == "2024-11-05"
    assert protocol.negotiate_version("2099-01-01") == "2025-06-18"
    assert protocol.negotiate_version(None) == "2025-06-18"


def test_token_format_guard_runs_before_the_database():
    assert protocol.valid_token_format("a" * 64)
    assert not protocol.valid_token_format("")
    assert not protocol.valid_token_format(None)
    assert not protocol.valid_token_format("A" * 64)      # лише нижній регістр hex
    assert not protocol.valid_token_format("a" * 63)
    assert not protocol.valid_token_format("g" * 64)


def test_sse_only_when_the_client_refuses_json():
    assert protocol.wants_sse("text/event-stream")
    assert not protocol.wants_sse("application/json, text/event-stream")
    assert not protocol.wants_sse("*/*")
    assert not protocol.wants_sse(None)


def test_envelopes_and_tool_results():
    assert protocol.rpc_result(1, {"a": 1}) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}
    err = protocol.rpc_error(2, -32601, "nope")
    assert err["error"] == {"code": -32601, "message": "nope"}
    assert protocol.tool_fail("x")["isError"] is True
    assert "isError" not in protocol.tool_ok("x")
    assert protocol.is_notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert not protocol.is_notification({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    frame = protocol.sse_frame({"a": "б"})
    assert frame.startswith("event: message\ndata: ") and frame.endswith("\n\n") and "б" in frame


def test_every_tool_schema_is_sound():
    assert protocol.validate_tool_specs(tools.TOOLS) == []
    assert set(tools.HANDLERS) == {t["name"] for t in tools.TOOLS}
    assert 10 <= len(tools.tools_for(None)) <= 28  # звичайний набір: 22 + 6 синку/«написати мені»
    assert len(tools.TOOLS) > len(tools.tools_for(None))  # фонові — лише за профілем
    for spec in tools.TOOLS:
        assert spec["name"].startswith("crm_")
        assert set(spec["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


def test_schema_validator_catches_real_mistakes():
    bad = [{"name": "x", "description": "d", "inputSchema": {"type": "object", "properties": {}, "required": ["missing"]}},
           {"name": "x", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]
    problems = protocol.validate_tool_specs(bad)
    assert any("required 'missing'" in p for p in problems)
    assert any("duplicate" in p for p in problems)


def test_argument_normalisation():
    assert tools._int("5", 10, 1, 30) == 5
    assert tools._int("999", 10, 1, 30) == 30
    assert tools._int("сміття", 10, 1, 30) == 10
    assert tools._parse_date("2026-09-12").isoformat() == "2026-09-12"
    with pytest.raises(tools.ToolError):
        tools._parse_date("12.09.2026")
    assert tools._parse_datetime("2026-09-12").hour == 9
    assert tools._parse_datetime("2026-09-12T18:30").minute == 30


# ── Наскрізно ────────────────────────────────────────────────────────────────


@pytest.fixture
def mcp(client):
    """Видає токен через панель і дає зручний виклик JSON-RPC."""
    status = client.post("/api/integrations/mcp/issue").json()
    token = status["url"].rsplit("/", 1)[-1]

    def rpc(method, params=None, *, req_id=1, token_=None, headers=None, path=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if req_id is not None:
            body["id"] = req_id
        return client.post(path or f"/mcp/{token_ or token}", json=body, headers=headers or {})

    def call(name, **arguments):
        r = rpc("tools/call", {"name": name, "arguments": arguments}).json()["result"]
        return r["content"][0]["text"], bool(r.get("isError"))

    return {"token": token, "rpc": rpc, "call": call, "client": client}


def test_handshake_ping_and_tool_list(mcp):
    r = mcp["rpc"]("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}).json()
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["serverInfo"]["name"] == "networking_ai"
    assert "crm_get_owner_voice" in r["result"]["instructions"]
    assert mcp["rpc"]("ping").json()["result"] == {}
    listed = mcp["rpc"]("tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == {t["name"] for t in tools.tools_for(None)}
    assert "crm_get_owner_voice" in {t["name"] for t in listed}
    assert "crm_fill_reply_draft" not in {t["name"] for t in listed}  # фоновий — лише за профілем


def test_notification_gets_202_with_empty_body(mcp):
    r = mcp["rpc"]("notifications/initialized", req_id=None)
    assert r.status_code == 202 and r.content == b""


def test_unknown_method_is_a_protocol_error_but_unknown_tool_is_a_tool_error(mcp):
    assert mcp["rpc"]("resources/list").json()["error"]["code"] == -32601
    text, failed = mcp["call"]("crm_no_such_tool")
    assert failed and "Невідомий інструмент" in text


def test_wrong_short_and_missing_tokens_are_401(mcp):
    c = mcp["client"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert c.post("/mcp/" + "b" * 64, json=body).status_code == 401
    assert c.post("/mcp/short", json=body).status_code == 401
    r = c.post("/mcp", json=body)
    assert r.status_code == 401
    assert r.json()["error"]["message"].startswith("Невірний")


def test_bearer_header_and_query_param_also_work(mcp):
    c, token = mcp["client"], mcp["token"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert c.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert c.post("/mcp", json=body, headers={"X-Networking-Token": token}).status_code == 200
    assert c.post(f"/mcp?token={token}", json=body).status_code == 200


def test_get_is_405_delete_and_options_are_204(mcp):
    c, token = mcp["client"], mcp["token"]
    r = c.get(f"/mcp/{token}")
    assert r.status_code == 405 and "POST" in r.headers["allow"]
    assert c.delete(f"/mcp/{token}").status_code == 204
    r = c.options(f"/mcp/{token}", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"


def test_sse_client_gets_an_event_stream(mcp):
    r = mcp["rpc"]("ping", headers={"Accept": "text/event-stream"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("event: message\ndata: ")
    assert json.loads(r.text.split("data: ", 1)[1].strip())["result"] == {}


def test_reissue_kills_the_old_address_and_revoke_kills_all(mcp):
    c, old = mcp["client"], mcp["token"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    new = c.post("/api/integrations/mcp/issue").json()["url"].rsplit("/", 1)[-1]
    assert new != old
    assert c.post(f"/mcp/{old}", json=body).status_code == 401
    assert c.post(f"/mcp/{new}", json=body).status_code == 200
    c.post("/api/integrations/mcp/revoke")
    assert c.post(f"/mcp/{new}", json=body).status_code == 401
    assert c.get("/api/integrations/mcp/status").json()["configured"] is False


def test_last_seen_is_stamped_once_claude_calls(mcp):
    assert mcp["client"].get("/api/integrations/mcp/status").json()["last_seen"] is None
    mcp["rpc"]("ping")
    assert mcp["client"].get("/api/integrations/mcp/status").json()["last_seen"] is not None


# ── Інструменти на реальних даних ────────────────────────────────────────────


def test_memory_tools_read_the_contact(mcp):
    c = mcp["client"]
    cid = c.post("/api/contacts", json={"first_name": "Марія", "last_name": "Коваль", "company": "Nova", "email": "m@nova.io"}).json()["id"]
    c.post(f"/api/contacts/{cid}/facts", json={"fact_type": "interest", "value": "Бігає трейли"})

    text, failed = mcp["call"]("crm_search_contacts", query="nova")
    assert not failed and f"#{cid} Марія Коваль" in text

    text, failed = mcp["call"]("crm_get_contact_memory", contact="Марія")
    assert not failed and "Бігає трейли" in text and "Name: Марія Коваль" in text

    text, failed = mcp["call"]("crm_get_owner_voice")
    assert not failed and "без емодзі" in text


def test_ambiguous_name_asks_for_an_id_instead_of_guessing(mcp):
    c = mcp["client"]
    a = c.post("/api/contacts", json={"first_name": "Олена", "last_name": "Іванова"}).json()["id"]
    b = c.post("/api/contacts", json={"first_name": "Олена", "last_name": "Петрова"}).json()["id"]
    text, failed = mcp["call"]("crm_get_contact_memory", contact="Олена")
    assert failed and f"#{a}" in text and f"#{b}" in text and "Передай #id" in text


def test_message_draft_without_telegram_explains_the_alternative(mcp):
    c = mcp["client"]
    c.post("/api/contacts", json={"first_name": "Ігор", "email": "igor@x.io"})
    text, failed = mcp["call"]("crm_save_message_draft", contact="Ігор", text="Привіт")
    assert failed and "crm_save_email_draft" in text and "crm_log_interaction" in text


def test_interaction_fact_and_reminder_are_written_verbatim(mcp):
    c = mcp["client"]
    cid = c.post("/api/contacts", json={"first_name": "Ігор"}).json()["id"]

    text, failed = mcp["call"]("crm_log_interaction", contact=f"#{cid}", summary="домовились про демо", channel="call")
    assert not failed
    assert c.get(f"/api/contacts/{cid}").json()["interactions"][0]["summary"] == "домовились про демо"

    text, failed = mcp["call"]("crm_add_fact", contact=f"#{cid}", fact_type="employer", value="Працює в Globex")
    assert not failed and "Працює в Globex" in text
    text, failed = mcp["call"]("crm_add_fact", contact=f"#{cid}", fact_type="вигаданий", value="x")
    assert failed and "fact_type має бути" in text

    text, failed = mcp["call"]("crm_set_reminder", contact="Ігор", text="нагадати про демо", due_at="2030-01-15")
    assert not failed and "15.01.2030 09:00" in text


def test_tasks_goals_gates_ideas_links_round_trip(mcp):
    call = mcp["call"]
    text, failed = call("crm_create_task", title="Подзвонити в банк", due_date="2030-02-01", project="Rozum")
    assert not failed and "#" in text
    uid = text.split("#", 1)[1].split()[0]
    text, _ = call("crm_list_tasks")
    assert "Подзвонити в банк" in text and "#Rozum" in text
    text, failed = call("crm_complete_task", task=f"#{uid}")
    assert not failed and text.startswith("Закрито")
    text, _ = call("crm_complete_task", task=f"#{uid}")
    assert "уже закрита" in text

    gid = mcp["client"].post("/api/goals", json={"title": "Підключення клієнтів", "priority": 100, "area": "work"}).json()["id"]
    text, failed = call("crm_log_goal_progress", goal=f"#{gid}", summary="перший клієнт підписав", status="almost")
    assert not failed and "in progress → almost" not in text and "not started → almost" in text
    text, failed = call("crm_log_goal_progress", goal=f"#{gid}", summary="x", status="готово")
    assert failed and "status має бути" in text

    text, failed = call("crm_create_gate", text="записати скринкаст", goal="підключення")
    assert not failed and "блокує «Підключення клієнтів»" in text
    g_uid = text.split("#", 1)[1].split()[0]
    text, _ = call("crm_list_gates")
    assert "записати скринкаст" in text
    text, failed = call("crm_close_gate", gate=g_uid)
    assert not failed

    text, failed = call("crm_create_idea", text="зробити коуча для команди")
    assert not failed and "Ідею збережено" in text
    text, failed = call("crm_save_link", url="https://example.com/article", note="про GTD")
    assert not failed
    text, _ = call("crm_save_link", url="https://example.com/article")
    assert "вже в списку" in text


def test_inbox_tools_save_verbatim_and_send(mcp, monkeypatch):
    from app.core.database import SessionLocal
    from app.modules.inbox.models import EmailThread
    from app.modules.integrations.google import client as google
    from datetime import datetime, timezone

    sent = []
    monkeypatch.setattr(google, "create_draft", lambda db, **kw: "gmail-draft-1")
    monkeypatch.setattr(google, "send_email", lambda db, to, subject, body_html, **kw: sent.append((to, body_html)) or True)
    with SessionLocal() as db:
        db.add(EmailThread(thread_id="t1", subject="Умови", from_email="m@nova.io", from_name="Марія",
                           body="Надішліть умови", last_at=datetime.now(timezone.utc),
                           needs_reply=True, urgency="high", topic="просить умови"))
        db.commit()

    call = mcp["call"]
    text, _ = call("crm_list_inbox")
    assert "! #1 Марія — просить умови" in text
    text, _ = call("crm_get_email", email="#1")
    assert "Надішліть умови" in text

    text, failed = call("crm_send_email_draft", email="#1")
    assert failed and "немає чернетки" in text

    text, failed = call("crm_save_email_draft", email="#1", text="Надсилаю умови у вкладенні.")
    assert not failed and "в чернетках Gmail" in text
    text, failed = call("crm_send_email_draft", email="#1")
    assert not failed and sent == [("m@nova.io", "Надсилаю умови у вкладенні.")]
    text, _ = call("crm_list_inbox")
    assert "розібрана" in text


def test_tool_crash_is_reported_inside_the_result(mcp, monkeypatch):
    def boom(db, args):
        raise RuntimeError("щось пішло не так")

    monkeypatch.setitem(tools.HANDLERS, "crm_list_gates", boom)
    text, failed = mcp["call"]("crm_list_gates")
    assert failed and "RuntimeError" in text
    # Транспорт живий — наступний виклик проходить.
    assert mcp["rpc"]("ping").status_code == 200
