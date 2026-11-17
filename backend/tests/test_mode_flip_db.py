"""Real-Postgres proof of the Phase 17 view mechanics (CONS-05).

Skipped unless WAREHOUSE_TEST_URL is a loopback superuser URL (same rule as test_warehouse_db).
Uses a scratch database and a prefixed reader role; both are dropped afterwards.
"""
import os
import secrets
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app import whsources
from app.warehouse import Roles

_URL = os.getenv("WAREHOUSE_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="WAREHOUSE_TEST_URL not set or not loopback",
)


def _with_db(db):
    p = urlsplit(_URL)
    return urlunsplit((p.scheme, p.netloc, "/" + db, "", ""))


@pytest.fixture
async def db():
    tag = secrets.token_hex(4)
    name = f"ic_p17_flip_{tag}"
    roles = Roles(reader=f"t17_{tag}_reader")
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.execute(f'CREATE ROLE "{roles.reader}"')
    c = await asyncpg.connect(_with_db(name))
    try:
        await c.execute("""
            CREATE TYPE "OrderStatus" AS ENUM ('open','paid');
            CREATE SCHEMA live_src; CREATE SCHEMA ic_imp_x; CREATE SCHEMA model;
            CREATE TABLE live_src.orders(id integer, name text, amount numeric(12,2),
                                         st "OrderStatus", ts timestamp);
            INSERT INTO live_src.orders VALUES
              (1,'a',10.50,'paid','2026-01-01 10:00:00'),
              (2,'b',20.00,'open','2026-06-30 23:30:00');
            CREATE VIEW public.orders AS SELECT * FROM live_src.orders;
            CREATE VIEW model.v_orders WITH (security_invoker = true) AS SELECT * FROM public.orders;
            CREATE MATERIALIZED VIEW model.mv_orders AS SELECT id, name FROM model.v_orders;
            CREATE TABLE ic_imp_x.orders(id bigint, name character varying, amount numeric,
                                         st text, ts timestamp with time zone, extra text);
            INSERT INTO ic_imp_x.orders VALUES
              (1,'a',10.50,'paid','2026-01-01 10:00:00+00','x'),
              (2,'b',20.00,'open','2026-06-30 23:30:00+00','y');
        """)
        yield {"c": c, "roles": roles}
    finally:
        await c.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{roles.reader}"')
        await admin.close()


async def _flip(d, **kw):
    return await whsources.replace_view_preserving(
        d["c"], "public", "orders", "ic_imp_x", "orders", d["roles"], **kw)


async def test_naive_replace_fails(db):
    with pytest.raises(asyncpg.PostgresError) as e:
        await db["c"].execute("CREATE OR REPLACE VIEW public.orders AS SELECT * FROM ic_imp_x.orders")
    assert "cannot change" in str(e.value)


async def test_preserving_replace_keeps_signature(db):
    c = db["c"]
    before = await whsources.view_columns(c, "public", "orders")
    out = await _flip(db)
    assert out["status"] == "ok" and out["appended"] == ["extra"]
    after = await whsources.view_columns(c, "public", "orders")
    assert after[:5] == before and after[5]["name"] == "extra"
    assert await c.fetchval("SELECT count(*) FROM model.v_orders") == 2
    await c.execute("REFRESH MATERIALIZED VIEW model.mv_orders")
    assert await c.fetchval("""SELECT count(*) FROM public.orders WHERE st = 'paid'::"OrderStatus" """) == 1
    assert await c.fetchval("SELECT has_table_privilege($1, 'public.orders', 'SELECT')",
                            db["roles"].reader)


async def test_fenced_check_catches_bad_enum(db):
    c = db["c"]
    await c.execute("INSERT INTO ic_imp_x.orders VALUES (3,'c',1,'bogus','2026-01-01 00:00:00+00','z')")
    # an unfenced count over the source does not evaluate the cast
    assert await c.fetchval("SELECT count(*) FROM ic_imp_x.orders") == 3
    with pytest.raises(asyncpg.PostgresError) as e:
        await _flip(db)
    assert "enum" in str(e.value)
    # old view untouched
    cols = await whsources.view_columns(c, "public", "orders")
    assert len(cols) == 5 and await c.fetchval("SELECT count(*) FROM public.orders") == 2


async def test_timezone_round_trip(db):
    c = db["c"]
    await c.execute("SET TimeZone = 'Asia/Kolkata'")
    await _flip(db)
    got = await c.fetch("SELECT ts FROM public.orders ORDER BY id")
    import datetime as dt
    assert [r["ts"] for r in got] == [dt.datetime(2026, 1, 1, 10, 0), dt.datetime(2026, 6, 30, 23, 30)]


async def test_vanished_column_refused(db):
    c = db["c"]
    await c.execute("ALTER TABLE ic_imp_x.orders DROP COLUMN name")
    before = await whsources.view_columns(c, "public", "orders")
    out = await _flip(db)
    assert out["status"] == "refused" and "name" in out["reason"]
    assert await whsources.view_columns(c, "public", "orders") == before


async def test_back_to_live(db):
    c = db["c"]
    before = await whsources.view_columns(c, "public", "orders")
    await _flip(db)
    # the remote table also carries the column the import appended
    await c.execute("ALTER TABLE live_src.orders ADD COLUMN extra text")
    out = await whsources.replace_view_preserving(
        c, "public", "orders", "live_src", "orders", db["roles"])
    assert out["status"] == "ok"
    assert (await whsources.view_columns(c, "public", "orders"))[:5] == before
    assert await c.fetchval("SELECT count(*) FROM model.v_orders") == 2
