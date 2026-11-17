"""Quick 007 real-Postgres coverage of chat attachments: the worker's write
order (user message, run claimed, tool call, reply row, run closed) maps the
file onto the reply; a legacy 'saved to the workspace' row shows its file; a
missing file reads as no longer available; another chat never sees the files.

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the other
real-Postgres tests); each test uses a throwaway database with schema.sql
applied twice:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9173/agent_memory
"""
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from app import askapi, chatfiles, sandbox

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
A, B = "ui:owner:abcdefghij", "ui:owner:klmnopqrst"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pool(monkeypatch, tmp_path):
    import asyncpg

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "chatfiles_test_" + secrets.token_hex(4)
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
        monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
        yield p
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _turn(pool, thread, question, files, reply, *, status="done"):
    """One answer, in the worker's write order. `files` is [(filename, result)]."""
    await pool.execute("INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0) "
                       "ON CONFLICT DO NOTHING", thread)
    mid = await pool.fetchval("INSERT INTO messages (thread_id, role, content) "
                              "VALUES ($1, 'user', $2) RETURNING id", thread, question)
    rid = await pool.fetchval("INSERT INTO task_runs (thread_id, message_id) VALUES ($1, $2) "
                              "RETURNING id", thread, mid)
    await pool.execute("UPDATE task_runs SET status = 'running', started_at = now() WHERE id = $1",
                       rid)
    for name, result in files:
        await pool.execute(
            "INSERT INTO tool_calls (task_run_id, tool, args, result_digest, latency_ms) "
            "VALUES ($1, 'send_document', $2::jsonb, $3, 5)",
            rid, json.dumps({"filename": name, "caption": ""}), result)
    reply_id = None
    if reply is not None:
        reply_id = await pool.fetchval("INSERT INTO messages (thread_id, role, content) "
                                       "VALUES ($1, 'assistant', $2) RETURNING id", thread, reply)
    await pool.execute("UPDATE task_runs SET status = $2, ended_at = now() WHERE id = $1",
                       rid, status)
    return rid, reply_id


def _put(root, run_id, name, data=b"PK\x03\x04"):
    d = root / f"run_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)


async def test_reply_carries_its_file_and_other_chat_sees_none(pool, tmp_path):
    await pool.execute("INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0)", B)
    rid, reply = await _turn(pool, A, "small excel report please",
                             [("Fleet.xlsx", chatfiles.attached_result("Fleet.xlsx"))],
                             "Your report is attached.")
    _put(tmp_path, rid, "Fleet.xlsx")
    rb, reply_b = await _turn(pool, B, "how many trucks?", [], "12 trucks.")

    out = await askapi.thread(chat=A)
    msgs = {m["id"]: m for m in out["messages"]}
    (card,) = msgs[reply]["attachments"]
    assert card["name"] == "Fleet.xlsx" and card["run_id"] == rid and card["available"]
    assert card["url"] == f"/api/runs/{rid}/artifacts/Fleet.xlsx" and card["bytes"] == 4

    other = await askapi.thread(chat=B)
    assert all(m.get("attachments", []) == [] for m in other["messages"])


async def test_legacy_row_and_missing_file(pool, tmp_path):
    legacy = ("saved to the workspace as Fleet_Details_Report.xlsx — this run did not come "
              "from WhatsApp, so the operator downloads it from the run's Artifacts list")
    r1, reply1 = await _turn(pool, A, "fleet report", [("Fleet_Details_Report.xlsx", legacy),
                                                      ("Fleet_Details_Report.xlsx", legacy)],
                             "It hasn't reached you.")
    _put(tmp_path, r1, "Fleet_Details_Report.xlsx")
    r2, reply2 = await _turn(pool, A, "again", [("Old.pdf", chatfiles.attached_result("Old.pdf"))],
                             "Attached.")
    out = await askapi.thread(chat=A)
    msgs = {m["id"]: m for m in out["messages"]}
    (legacy_card,) = msgs[reply1]["attachments"]
    assert legacy_card["available"] and legacy_card["run_id"] == r1
    (gone,) = msgs[reply2]["attachments"]
    assert gone["available"] is False and gone["url"] is None


async def test_failed_run_file_lands_on_next_reply(pool, tmp_path):
    r1, none = await _turn(pool, A, "report", [("R.pdf", chatfiles.attached_result("R.pdf"))],
                           None, status="failed")
    _put(tmp_path, r1, "R.pdf")
    r2, reply2 = await _turn(pool, A, "report", [], "Here is the summary.")
    out = await askapi.thread(chat=A)
    msgs = {m["id"]: m for m in out["messages"]}
    assert [a["run_id"] for a in msgs[reply2]["attachments"]] == [r1]
