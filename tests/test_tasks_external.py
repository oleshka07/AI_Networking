"""Синк із чек-листом Claude: MCP-інструменти профілю sync (docs/CLAUDE_CHECKLIST.md)."""

import json
from datetime import datetime, timedelta, timezone

from app.modules.mcp import tools
from app.modules.tasks.models import Task, TaskStatus


def _call(db, name, **args):
    text, is_error = tools.call_tool(db, name, args)
    assert not is_error, text
    return json.loads(text)


def _err(db, name, **args):
    text, is_error = tools.call_tool(db, name, args)
    assert is_error, text
    return text


def _card(db, ext="demo-supplier-call", **extra):
    args = {"source": "claude", "external_id": ext,
            "title": "Подзвонити постачальнику", "project": "Демо",
            "url": f"https://example.invalid/checklist#t-{ext}", **extra}
    return _call(db, "crm_upsert_task", **args)


def test_sync_profile_lists_exactly_the_sync_tools():
    names = {t["name"] for t in tools.tools_for("sync")}
    assert names == {"crm_upsert_task", "crm_list_task_changes", "crm_link_task",
                     "crm_close_missing", "crm_complete_task", "crm_send_owner_message"}
    interactive = {t["name"] for t in tools.tools_for(None)}
    # Синк — лише за профілем; закрити задачу людина в розмові досі може.
    assert "crm_upsert_task" not in interactive
    assert "crm_complete_task" in interactive


def test_upsert_twice_is_one_task(db):
    first = _card(db)
    second = _card(db, title="Подзвонити постачальнику й погодити дату", duration_min=10)
    assert first["created"] is True and second["created"] is False
    assert first["uid"] == second["uid"]
    rows = db.query(Task).filter(Task.external_id == "demo-supplier-call").all()
    assert len(rows) == 1
    assert rows[0].title.endswith("погодити дату")
    assert rows[0].duration_min == 10
    assert rows[0].source == "claude"
    assert rows[0].external_url.endswith("#t-demo-supplier-call")


def test_partial_update_keeps_other_fields(db):
    _card(db, due_date="2026-10-20", tags=["продажі", "@later"], blocks_work=True)
    _call(db, "crm_upsert_task", source="claude", external_id="demo-supplier-call",
          title="Нова назва")
    task = db.query(Task).one()
    assert task.due_date.isoformat() == "2026-10-20"
    assert task.tags == "продажі,@later"
    assert task.blocks_work is True


def test_new_task_needs_title_and_known_source(db):
    assert "title" in _err(db, "crm_upsert_task", source="claude", external_id="x")
    assert "джерело" in _err(db, "crm_upsert_task", source="trello", external_id="x", title="A")
    assert "80" in _err(db, "crm_upsert_task", source="claude", external_id="x" * 81, title="A")


def test_done_is_sticky_when_closed_on_server(db):
    uid = _card(db)["uid"]
    # Закрили кнопкою в Telegram / на сайті — це сервер.
    _call_text, err = tools.call_tool(db, "crm_complete_task", {"task": uid})
    assert not err
    reopened = _card(db, status="todo")
    assert reopened["conflict"] == "done_on_server"
    assert reopened["status"] == "done"
    assert db.query(Task).one().status == TaskStatus.done


def test_done_from_source_closes_and_source_may_reopen(db):
    _card(db)
    closed = _card(db, status="done")
    assert closed["status"] == "done" and "conflict" not in closed
    task = db.query(Task).one()
    assert task.done_via == "source" and task.done_at is not None
    # Галочку в чек-листі зняли — той самий бік, тож відкриваємо.
    back = _card(db, status="todo")
    assert back["status"] == "todo" and "conflict" not in back
    db.refresh(task)
    assert task.done_at is None and task.done_via is None


def test_list_task_changes_since(db):
    a = _card(db, ext="card-a")["uid"]
    b = _card(db, ext="card-b")["uid"]
    snapshot = _call(db, "crm_list_task_changes")
    assert {r["uid"] for r in snapshot["changes"]} >= {a, b}
    since = snapshot["server_time"]

    assert _call(db, "crm_list_task_changes", since=since)["changes"] == []

    # Після since: одну закрили на сервері, одну видалили, одну створили.
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    ta = db.query(Task).filter(Task.uid == a).one()
    tb = db.query(Task).filter(Task.uid == b).one()
    ta.status = TaskStatus.done
    ta.updated_at = later
    tb.deleted_at = later
    tb.updated_at = later
    db.commit()
    c = _card(db, ext="card-c")["uid"]
    db.query(Task).filter(Task.uid == c).one().updated_at = later
    db.commit()

    rows = {r["uid"]: r for r in _call(db, "crm_list_task_changes", since=since)["changes"]}
    assert set(rows) == {a, b, c}
    assert rows[a]["status"] == "done" and rows[a]["done_at"]
    assert rows[b]["deleted"] is True
    assert rows[c]["source"] == "claude" and rows[c]["external_id"] == "card-c"


def test_list_task_changes_filters_sources(db):
    _card(db, ext="card-a")
    db.add(Task(uid="tg0001", title="Надіслати рахунок", source="telegram",
                external_id="tgb:1:2"))
    db.commit()
    rows = _call(db, "crm_list_task_changes", sources=["telegram"])["changes"]
    assert [r["uid"] for r in rows] == ["tg0001"]


def test_link_task_prevents_second_import(db):
    db.add(Task(uid="tg0002", title="Надіслати договір", source="telegram",
                external_id="tgb:10:20"))
    db.commit()
    linked = _call(db, "crm_link_task", uid="tg0002", source="claude",
                   external_id="os-dogovir", url="https://example.invalid/c#t-os-dogovir")
    assert linked["linked"] == "claude:os-dogovir"

    # Наступний прохід синку оновлює ту саму задачу, а не створює другу.
    again = _call(db, "crm_upsert_task", source="claude", external_id="os-dogovir",
                  title="Надіслати договір до пʼятниці")
    assert again["uid"] == "tg0002" and again["created"] is False
    task = db.query(Task).one()
    assert task.source == "telegram" and task.external_id == "tgb:10:20"
    assert task.title.endswith("до пʼятниці")

    # Іншу задачу на ту саму картку не звʼяжемо.
    db.add(Task(uid="tg0003", title="Інше", source="telegram", external_id="tgb:10:21"))
    db.commit()
    assert "уже звʼязана" in _err(db, "crm_link_task", uid="tg0003", source="claude",
                                   external_id="os-dogovir")


def test_close_missing(db):
    keep = _card(db, ext="card-keep")["uid"]
    gone = _card(db, ext="card-gone")["uid"]
    db.add(Task(uid="tg0004", title="Звʼязана", source="telegram",
                external_id="tgb:1:1", linked_ref="claude:card-linked"))
    manual = Task(uid="mn0001", title="Своя", source="manual")
    db.add(manual)
    db.commit()

    res = _call(db, "crm_close_missing", source="claude", keep_external_ids=["card-keep"])
    assert set(res["closed"]) == {gone, "tg0004"}
    by_uid = {t.uid: t for t in db.query(Task).all()}
    assert by_uid[keep].status != TaskStatus.done
    assert by_uid[gone].status == TaskStatus.done
    assert "закрито в джерелі" in by_uid[gone].notes
    assert by_uid["mn0001"].status != TaskStatus.done  # чуже джерело не чіпаємо

    # Повторний прохід нічого не закриває вдруге.
    assert _call(db, "crm_close_missing", source="claude",
                 keep_external_ids=["card-keep"])["count"] == 0


def test_close_missing_refuses_empty_list_without_confirmation(db):
    _card(db)
    assert "confirm_all" in _err(db, "crm_close_missing", source="claude", keep_external_ids=[])
    assert db.query(Task).one().status != TaskStatus.done
    _call(db, "crm_close_missing", source="claude", keep_external_ids=[], confirm_all=True)
    assert db.query(Task).one().status == TaskStatus.done


def test_deleted_on_server_is_not_resurrected(db):
    uid = _card(db)["uid"]
    task = db.query(Task).filter(Task.uid == uid).one()
    task.deleted_at = datetime.now(timezone.utc)
    db.commit()
    res = _card(db)
    assert res["conflict"] == "deleted_on_server"
    assert db.query(Task).count() == 1


def test_sync_calls_are_recorded_for_the_site_panel(db):
    from app.modules.tasks import external

    _card(db)
    row = external.get_settings(db)
    assert row.sync_last_tool == "crm_upsert_task" and row.sync_last_at is not None
    _err(db, "crm_upsert_task", source="claude", external_id="x")
    db.refresh(row)
    assert "title" in row.sync_error


def test_done_at_is_stamped_wherever_a_task_closes(db):
    task = Task(uid="ab0001", title="Будь-яка")
    db.add(task)
    db.commit()
    task.status = TaskStatus.done
    db.commit()
    assert task.done_at is not None and task.done_via == "server"
    task.status = TaskStatus.todo
    db.commit()
    assert task.done_at is None and task.done_via is None
