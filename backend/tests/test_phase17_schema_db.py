"""Phase 17 schema block: pure text rules plus a real-Postgres idempotence proof.

The DB tests need GOLIVE_TEST_DATABASE_URL (loopback); the text tests always run.
"""
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
_GOLIVE_OK = bool(_URL) and (urlsplit(_URL).hostname or "") in ("127.0.0.1", "localhost")
SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
HEADER = "-- Phase 17: model consumers (CONS-01..CONS-06)"
END = "-- End Phase 17: model consumers"
SUB = "-- Phase 17 (CONS-"


def _lines():
    return SCHEMA.read_text(encoding="utf-8").splitlines()


def _region(lines):
    a = lines.index(HEADER)
    b = next(i for i, ln in enumerate(lines) if ln.startswith(END))
    return a, b


def _code(lines):
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("--"))


def test_header_and_end_once_in_order():
    lines = _lines()
    assert lines.count(HEADER) == 1
    assert sum(1 for ln in lines if ln.startswith(END)) == 1
    a, b = _region(lines)
    assert a < b


def test_column_part_is_additive_only():
    lines = _lines()
    a, b = _region(lines)
    end = next((i for i in range(a + 1, b) if lines[i].startswith(SUB)), b)
    part = _code(lines[a:end])
    assert not re.search(r"\bDROP\b", part, re.I)
    assert not re.search(r"ALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE", part, re.I)
    assert not re.search(r"^\s*(UPDATE|INSERT)\b", part, re.I | re.M)


def test_region_has_no_drop_type_cascade():
    lines = _lines()
    a, b = _region(lines)
    region = _code(lines[a:b])
    assert not re.search(r"\bDROP\b", region, re.I)
    assert not re.search(r"ALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE", region, re.I)
    assert not re.search(r"\bCASCADE\b", region, re.I)


def test_updates_only_in_guarded_subblocks():
    lines = _lines()
    a, b = _region(lines)
    sub_seen = False
    stmts = []
    cur = None
    for ln in lines[a:b]:
        if ln.startswith(SUB):
            sub_seen = True
        if ln.lstrip().startswith("--"):
            continue
        if re.match(r"\s*UPDATE\b", ln, re.I):
            cur = [sub_seen, ln]
            stmts.append(cur)
        elif cur is not None:
            cur[1] += "\n" + ln
        if cur is not None and ";" in ln:
            cur = None
    for in_sub, text in stmts:
        assert in_sub, "UPDATE before a Phase 17 (CONS- sub-header"
        assert re.search(r"\bWHERE\b", text, re.I)


NEW_COLS = [
    ("watches", "measure_ref", "jsonb"), ("watches", "stale_since", "timestamp with time zone"),
    ("watches", "data_as_of", "timestamp with time zone"), ("watch_events", "kind", "text"),
    ("metric_history", "stale", "boolean"), ("dashboard_snapshots", "data_as_of", "timestamp with time zone"),
    ("dashboard_snapshots", "reads", "ARRAY"), ("dashboard_snapshots", "sync_aligned", "boolean"),
    ("dashboard_snapshots", "inputs_synced_at", "timestamp with time zone"),
    ("source_tables", "pending_mode", "text"), ("source_tables", "import_reset", "boolean"),
    ("source_tables", "mode_action_id", "bigint"),
]


@pytest.fixture
async def pool():
    import asyncpg
    if not _GOLIVE_OK:
        pytest.skip("GOLIVE_TEST_DATABASE_URL not set or not loopback")
    name = "ic_p17_" + secrets.token_hex(4)
    p = urlsplit(_URL)
    admin = await asyncpg.connect(_URL)
    pl = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        pl = await asyncpg.create_pool(
            urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment)), min_size=1, max_size=3)
        await pl.execute(SCHEMA.read_text(encoding="utf-8"))
        yield pl
    finally:
        if pl:
            await pl.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


_XMIN = ("SELECT c.relname, a.attname, a.xmin::text AS x FROM pg_attribute a "
         "JOIN pg_class c ON c.oid = a.attrelid "
         "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relname = ANY($1) ORDER BY 1, 2")


async def test_columns_twice_and_xmin_stable(pool):
    tables = sorted({t for t, _, _ in NEW_COLS})
    before = await pool.fetch(_XMIN, tables)
    await pool.execute(SCHEMA.read_text(encoding="utf-8"))
    after = await pool.fetch(_XMIN, tables)
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    for t, c, typ in NEW_COLS:
        rows = await pool.fetch(
            "SELECT data_type FROM information_schema.columns WHERE table_name=$1 AND column_name=$2", t, c)
        assert len(rows) == 1, (t, c)
        assert rows[0]["data_type"] == typ, (t, c, rows[0]["data_type"])
    for con in ("watch_events_kind_check", "source_tables_pending_mode_check"):
        assert await pool.fetchval("SELECT count(*) FROM pg_constraint WHERE conname=$1", con) == 1


async def test_constraints_and_defaults(pool):
    import asyncpg
    wid = await pool.fetchval(
        "INSERT INTO watches (thread_id, kind, title, condition) VALUES ('t','kpi','w','c') RETURNING id")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO watch_events (watch_id, kind) VALUES ($1,'bogus')", wid)
    await pool.execute("INSERT INTO watch_events (watch_id) VALUES ($1)", wid)
    assert await pool.fetchval("SELECT kind FROM watch_events WHERE watch_id=$1", wid) == "fire"
    sid = await pool.fetchval("SELECT id FROM sources WHERE name='business_db'")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO source_tables (source_id, remote_table, warehouse_schema, warehouse_table, mode, "
            "pending_mode) VALUES ($1,'t','s','t','import','bogus')", sid)
    await pool.execute("INSERT INTO metric_history (watch_id, metric, value) VALUES ($1,'m',1)", wid)
    assert await pool.fetchval("SELECT stale FROM metric_history WHERE watch_id=$1", wid) is False
