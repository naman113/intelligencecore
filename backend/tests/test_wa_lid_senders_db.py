"""Quick 017 real-Postgres coverage of wasenders.py SQL: the LID map upsert
(a LID-only message never clears a known phone, a revealed phone fills it in),
the blocked-senders upsert (count, newest snippet, both identities kept) and
its cap, the newest-first list, "Allow this sender" forgetting every row of
that person (by phone, LID or mapped LID), and recipient_for over the table.

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the other
real-Postgres tests); each test uses a throwaway database with schema.sql
applied twice:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9163/agent_memory
"""
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from app import configservice, wasenders

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
PHONE, LID, STRANGER = "15550001111", "987650000012345", "987650000054321"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pool(monkeypatch):
    import asyncpg

    import app.db as dbmod

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "wasenders_test_" + secrets.token_hex(4)
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
        monkeypatch.setattr(dbmod, "get_pool", gp)
        yield p
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_lid_map_upsert(pool):
    async with pool.acquire() as conn:
        assert await wasenders.note_lid(conn, LID) == ""
        assert await wasenders.phone_for_lid(conn, LID) == ""
        assert await wasenders.note_lid(conn, LID, "+1 555 000 1111") == PHONE
        assert await wasenders.note_lid(conn, LID) == PHONE, "a LID-only message keeps the phone"
        row = await conn.fetchrow("SELECT phone, source FROM wa_lid_map WHERE lid = $1", LID)
        assert (row["phone"], row["source"]) == (PHONE, "message")
        assert await wasenders.note_lid(conn, LID, PHONE, source="lookup") == PHONE
        assert await conn.fetchval("SELECT source FROM wa_lid_map WHERE lid = $1", LID) == "lookup"
        assert await conn.fetchval("SELECT count(*) FROM wa_lid_map") == 1


async def test_recipient_for_over_the_table(pool):
    async with pool.acquire() as conn:
        await wasenders.note_lid(conn, LID, PHONE)
        await wasenders.note_lid(conn, STRANGER)
    assert await wasenders.recipient_for(LID) == PHONE
    assert await wasenders.recipient_for(f"{LID}@lid") == PHONE
    assert await wasenders.recipient_for(STRANGER) == f"{STRANGER}@lid"
    assert await wasenders.recipient_for(f"{STRANGER}@lid") == f"{STRANGER}@lid"
    assert await wasenders.recipient_for("15550009999") == "15550009999"


async def test_blocked_upsert_cap_list_and_allow(pool, monkeypatch):
    monkeypatch.setattr(wasenders, "BLOCKED_KEEP", 3)
    async with pool.acquire() as conn:
        await wasenders.record_blocked(conn, lid=STRANGER, name="Someone", text="hello there, a long private message")
        await wasenders.record_blocked(conn, lid=STRANGER, phone="", text="second")
        for i in range(4):
            await wasenders.record_blocked(conn, phone=f"1555000200{i}", text=f"m{i}")
        assert await conn.fetchval("SELECT count(*) FROM wa_blocked_senders") == 3
        await wasenders.record_blocked(conn, phone=PHONE, lid=LID, text="hi", chat_jid="g@g.us", group_subject="Ops")
        await wasenders.note_lid(conn, LID, PHONE)
    rows = await wasenders.list_blocked(5)
    assert [r["sender"] for r in rows][0] == LID, "newest first, keyed on the LID"
    assert rows[0]["phone"] == PHONE and rows[0]["group_subject"] == "Ops"
    assert isinstance(rows[0]["last_at"], str)
    assert len(rows) == 3
    assert all(len(r["snippet"]) <= wasenders.SNIPPET_CHARS + 1 for r in rows)

    async with pool.acquire() as conn:
        await wasenders.record_blocked(conn, lid=STRANGER, text="again")
        await wasenders.record_blocked(conn, lid=STRANGER, text="and again")
        assert await conn.fetchval("SELECT count FROM wa_blocked_senders WHERE sender = $1", STRANGER) == 2
        assert await conn.fetchval("SELECT snippet FROM wa_blocked_senders WHERE sender = $1", STRANGER) == "and again"

    saved = []

    async def fake_save_many(values):
        saved.append(values)
        return list(values)
    monkeypatch.setattr(configservice, "save_many", fake_save_many)
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: types.SimpleNamespace(allowed_numbers="15550009999"))

    r = await wasenders.allow_sender(PHONE)   # the row is keyed on the LID; the phone maps to it
    assert r["changed"] is True and saved == [{"allowed_numbers": f"15550009999,{PHONE}"}]
    left = [x["sender"] for x in await wasenders.list_blocked(10)]
    assert LID not in left and STRANGER in left
