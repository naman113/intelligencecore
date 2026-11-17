"""Real-Postgres proof that schema.sql widens task_runs_kind_check to 'sync' (WH-07).

Skipped unless GOLIVE_TEST_DATABASE_URL is set and points at a loopback host.
"""
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="GOLIVE_TEST_DATABASE_URL not set or not loopback",
)

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"

DEF_SQL = ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
           "WHERE conrelid='task_runs'::regclass AND conname='task_runs_kind_check'")
COUNT_SQL = ("SELECT count(*) FROM pg_constraint "
             "WHERE conrelid='task_runs'::regclass AND conname='task_runs_kind_check'")
OID_SQL = ("SELECT oid::int FROM pg_constraint "
           "WHERE conrelid='task_runs'::regclass AND conname='task_runs_kind_check'")


@pytest.fixture
async def conn():
    import asyncpg

    name = "ic_p14_kind_" + secrets.token_hex(4)
    p = urlsplit(_URL)
    admin = await asyncpg.connect(_URL)
    c = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        c = await asyncpg.connect(urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment)))
        await c.execute(SCHEMA.read_text(encoding="utf-8"))
        yield c
    finally:
        if c:
            await c.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_fresh_db_applied_twice_has_one_widened_constraint(conn):
    await conn.execute(SCHEMA.read_text(encoding="utf-8"))
    assert await conn.fetchval(COUNT_SQL) == 1
    d = await conn.fetchval(DEF_SQL)
    assert "sync" in d and "user" in d and "playbook" in d


async def test_v1_constraint_is_widened(conn):
    await conn.execute("ALTER TABLE task_runs DROP CONSTRAINT task_runs_kind_check")
    await conn.execute("ALTER TABLE task_runs ADD CONSTRAINT task_runs_kind_check "
                       "CHECK (kind IN ('user','playbook'))")
    assert "sync" not in await conn.fetchval(DEF_SQL)
    await conn.execute(SCHEMA.read_text(encoding="utf-8"))
    assert await conn.fetchval(COUNT_SQL) == 1
    assert "sync" in await conn.fetchval(DEF_SQL)


async def test_kind_values(conn):
    import asyncpg

    await conn.execute("INSERT INTO threads(thread_id, phone) VALUES ('t14','t14')")
    for k in ("sync", "user", "playbook"):
        await conn.execute("INSERT INTO task_runs(thread_id, kind) VALUES ('t14', $1)", k)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("INSERT INTO task_runs(thread_id, kind) VALUES ('t14', 'bogus')")


async def test_reapply_does_not_rewrite_constraint(conn):
    before = await conn.fetchval(OID_SQL)
    await conn.execute(SCHEMA.read_text(encoding="utf-8"))
    assert await conn.fetchval(OID_SQL) == before
