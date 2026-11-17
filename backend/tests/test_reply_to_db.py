"""Quick 021 real-Postgres coverage: messages.reply_to (schema applied twice, so the
ALTER is checked for idempotency), GET /api/ask returning it, and
whatsapp.reply_target reading a message's WhatsApp id and a group sender.

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the other real-Postgres
tests), e.g. DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9163/agent_memory
Made-up numbers and ids.
"""
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
GROUP = "120363000000000001@g.us"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pool(monkeypatch):
    import asyncpg

    from app import askapi

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "q021_test_" + secrets.token_hex(4)
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


async def test_ask_thread_returns_reply_to_for_two_pending_questions(pool):
    from app import askapi

    first = await askapi.ask({"message": "is any machine burning diesel"})
    second = await askapi.ask({"message": "how many cases are open"})
    thread = first["thread_id"]
    # what the worker writes after each run (worker._process_one), in order
    for mid, text in ((first["message_id"], "About 10 machines."),
                      (second["message_id"], "There are 3 open cases.")):
        await pool.execute(
            "INSERT INTO messages (thread_id, role, content, reply_to) "
            "VALUES ($1, 'assistant', $2, $3)", thread, text, mid)
    out = await askapi.thread()
    by_text = {m["content"]: m for m in out["messages"]}
    assert by_text["About 10 machines."]["reply_to"] == first["message_id"]
    assert by_text["There are 3 open cases."]["reply_to"] == second["message_id"]
    assert by_text["how many cases are open"]["reply_to"] is None


async def test_reply_target_reads_the_whatsapp_id_and_group_sender(pool):
    from app import whatsapp

    await pool.execute("INSERT INTO threads (thread_id, phone) VALUES ($1, $1)", GROUP)
    mid = await pool.fetchval(
        "INSERT INTO messages (thread_id, role, content, wa_message_id) "
        "VALUES ($1, 'user', $2, $3) RETURNING id",
        GROUP, '[replying to the assistant: "ok?"] @bot status?', "3EB0AAAA0001")
    await pool.execute(
        """INSERT INTO events (source, external_id, actor_raw, content, provenance, triage_status,
                               thread_ref, occurred_at)
           VALUES ('whatsapp', '3EB0AAAA0001', '15550003333', '@bot status?', 'external', 'done', $1,
                   now())""",
        GROUP)
    assert await whatsapp.reply_target(pool, mid, GROUP) == {
        "thread_id": GROUP, "id": "3EB0AAAA0001", "text": "@bot status?", "chat": GROUP,
        "participant": "15550003333"}
    assert await whatsapp.reply_target(pool, mid, "15550001111") is None
