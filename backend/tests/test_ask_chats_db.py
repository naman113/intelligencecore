"""Quick 003 real-Postgres coverage of the Ask chat routes: separate histories,
list order and titles, rename, read marks, delete (hide, move watches), retry.

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the Phase 6
real-Postgres tests). Each test uses a throwaway database with schema.sql
applied twice, so the Quick 003 additions are also checked for idempotency:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9163/agent_memory
"""
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pool(monkeypatch):
    import asyncpg

    from app import askapi

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "ask_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    p = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = await asyncpg.connect(url)
        await conn.execute(sql)
        await conn.execute(sql)
        await conn.close()
        p = await asyncpg.create_pool(url, min_size=1, max_size=4)

        async def gp():
            return p
        monkeypatch.setattr(askapi, "db", types.SimpleNamespace(get_pool=gp))
        yield p
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _answer(pool, thread, text, status="done", error=None):
    """What the worker does after a run: an assistant row and the run's outcome."""
    rid = await pool.fetchval(
        "SELECT max(id) FROM task_runs WHERE thread_id = $1", thread)
    if text is not None:
        await pool.execute(
            "INSERT INTO messages (thread_id, role, content) VALUES ($1, 'assistant', $2)",
            thread, text)
    await pool.execute(
        "UPDATE task_runs SET status = $2, error = $3, ended_at = now() WHERE id = $1",
        rid, status, error)
    return rid


async def test_two_chats_keep_separate_histories_and_list_order(pool):
    from app import askapi

    first = await askapi.ask({"message": "How much diesel did the barges use?"})
    assert first["thread_id"] == "ui:owner"
    chat = await askapi.create_chat({"operator": "owner"})
    assert chat["id"].startswith("ui:owner:") and chat["title"] == "New chat"
    second = await askapi.ask({"message": "Build a dashboard: fuel by machine",
                               "chat": chat["id"]})
    assert second["thread_id"] == chat["id"]

    a = await askapi.thread(chat="ui:owner")
    b = await askapi.thread(chat=chat["id"])
    assert [m["content"] for m in a["messages"]] == ["How much diesel did the barges use?"]
    assert [m["content"] for m in b["messages"]] == ["Build a dashboard: fuel by machine"]
    assert a["pending"] and b["pending"] and b["run"]["status"] == "queued"
    assert b["run"]["step"] in ("Starting",) or b["run"]["step"].startswith("Waiting")
    assert a["run"]["id"] != b["run"]["id"] and a["now"]

    listed = (await askapi.list_chats(operator="owner"))["chats"]
    assert [c["id"] for c in listed] == [chat["id"], "ui:owner"]  # newest activity first
    assert listed[0]["title"] == "Build a dashboard: fuel by machine"
    assert listed[1]["title"] == "How much diesel did the barges use?" and listed[1]["is_main"]
    assert all(c["run"]["status"] == "queued" for c in listed)
    # Another operator's chats never show up.
    await askapi.ask({"message": "hello", "operator": "alice"})
    assert {c["id"] for c in (await askapi.list_chats(operator="owner"))["chats"]} == \
        {chat["id"], "ui:owner"}


async def test_rename_unread_and_read_marks(pool):
    from app import askapi

    chat = await askapi.create_chat({"operator": "owner", "title": "Barges"})
    assert chat["title"] == "Barges" and chat["named"]
    await askapi.ask({"message": "q", "chat": chat["id"]})
    await _answer(pool, chat["id"], "answer one")
    item = next(c for c in (await askapi.list_chats())["chats"] if c["id"] == chat["id"])
    assert item["unread"] and item["run"]["status"] == "done"
    await askapi.mark_read(chat["id"], {"upto": item["last_assistant_id"]})
    await askapi.mark_read(chat["id"], {"upto": 1})  # never moves back
    item = next(c for c in (await askapi.list_chats())["chats"] if c["id"] == chat["id"])
    assert not item["unread"]

    assert (await askapi.rename_chat(chat["id"], {"title": "  Fuel   check "}))["title"] == "Fuel check"
    item = next(c for c in (await askapi.list_chats())["chats"] if c["id"] == chat["id"])
    assert item["title"] == "Fuel check"
    await askapi.rename_chat(chat["id"], {"title": ""})
    item = next(c for c in (await askapi.list_chats())["chats"] if c["id"] == chat["id"])
    assert item["title"] == "q" and not item["named"]
    # The main chat can be renamed before it has a row.
    assert (await askapi.rename_chat("ui:owner", {"title": "Main"}))["title"] == "Main"
    # The main chat's legacy NULL read mark means nothing unread until it is first marked.
    await pool.execute("UPDATE threads SET last_read_id = NULL WHERE thread_id = 'ui:owner'")
    await askapi.ask({"message": "x"})
    await _answer(pool, "ui:owner", "y")
    main = next(c for c in (await askapi.list_chats())["chats"] if c["is_main"])
    assert not main["unread"]


async def test_delete_hides_and_moves_watches(pool):
    from app import askapi

    chat = await askapi.create_chat({"operator": "owner"})
    await askapi.ask({"message": "watch fuel", "chat": chat["id"]})
    with pytest.raises(HTTPException) as e:  # an answer is still running
        await askapi.delete_chat(chat["id"])
    assert e.value.status_code == 409
    await _answer(pool, chat["id"], "watch #1 set up")
    w = await pool.fetchval(
        "INSERT INTO watches (thread_id, kind, title, condition) VALUES ($1, 'user', 't', 'c') "
        "RETURNING id", chat["id"])
    done = await pool.fetchval(
        "INSERT INTO watches (thread_id, kind, title, condition, status) "
        "VALUES ($1, 'user', 'old', 'c', 'done') RETURNING id", chat["id"])
    act = await pool.fetchval(
        "INSERT INTO actions (kind, payload, tier, status) VALUES ('create_watch', $1::jsonb, "
        "'draft', 'proposed') RETURNING id", json.dumps({"thread_id": chat["id"], "title": "k"}))

    out = await askapi.delete_chat(chat["id"])
    assert out == {"deleted": chat["id"], "watches_moved": 1, "main": "ui:owner"}
    assert await pool.fetchval("SELECT thread_id FROM watches WHERE id = $1", w) == "ui:owner"
    assert await pool.fetchval("SELECT thread_id FROM watches WHERE id = $1", done) == chat["id"]
    assert await pool.fetchval("SELECT payload->>'thread_id' FROM actions WHERE id = $1", act) == \
        "ui:owner"
    # Hidden, not dropped: the rows stay for the audit trail.
    assert await pool.fetchval("SELECT count(*) FROM messages WHERE thread_id = $1", chat["id"]) == 2
    assert chat["id"] not in {c["id"] for c in (await askapi.list_chats())["chats"]}
    for call in (lambda: askapi.thread(chat=chat["id"]),
                 lambda: askapi.ask({"message": "again", "chat": chat["id"]}),
                 lambda: askapi.delete_chat(chat["id"]),
                 lambda: askapi.rename_chat(chat["id"], {"title": "x"}),
                 lambda: askapi.mark_read(chat["id"], {"upto": 1}),
                 lambda: askapi.ask({"message": "x", "chat": "ui:owner:zzzzzzzzzz"})):
        with pytest.raises(HTTPException) as e:
            await call()
        assert e.value.status_code == 404


async def test_retry_only_the_latest_failed_answer(pool):
    from app import askapi

    chat = await askapi.create_chat({"operator": "owner"})
    asked = await askapi.ask({"message": "Edit the dashboard: add filters", "chat": chat["id"]})
    rid = await _answer(pool, chat["id"], None, status="failed", error="timed out after 600 s")
    t = await askapi.thread(chat=chat["id"])
    assert not t["pending"] and t["run"]["status"] == "failed"
    assert t["run"]["error"] == "timed out after 600 s" and t["last_run"]["id"] == rid

    out = await askapi.retry({"chat": chat["id"], "run_id": rid})
    assert out["retry_of"] == rid and out["run_id"] > rid
    row = await pool.fetchrow("SELECT message_id, status FROM task_runs WHERE id = $1",
                              out["run_id"])
    assert row["message_id"] == asked["message_id"] and row["status"] == "queued"
    assert await pool.fetchval("SELECT count(*) FROM messages WHERE thread_id = $1",
                               chat["id"]) == 1  # the question is not repeated
    with pytest.raises(HTTPException) as e:  # double click: no longer the latest
        await askapi.retry({"chat": chat["id"], "run_id": rid})
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:  # another chat's run
        await askapi.retry({"chat": "ui:owner", "run_id": out["run_id"]})
    assert e.value.status_code == 404
    await _answer(pool, chat["id"], "done now")
    with pytest.raises(HTTPException) as e:  # a finished run is not retried
        await askapi.retry({"chat": chat["id"], "run_id": out["run_id"]})
    assert e.value.status_code == 409


async def test_waiting_for_approval_count(pool):
    from app import askapi

    await askapi.ask({"message": "Track open invoices daily"})
    rid = await _answer(pool, "ui:owner", "Proposed a KPI for your approval.")
    for status in ("proposed", "proposed", "executed"):
        await pool.execute(
            "INSERT INTO actions (kind, payload, tier, status, task_run_id) "
            "VALUES ('create_watch', '{}'::jsonb, 'draft', $1, $2)", status, rid)
    t = await askapi.thread()
    assert t["waiting_approval"] == 2 and t["run"]["status"] == "done"
