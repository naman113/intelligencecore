"""Real-Postgres proof for the Phase 11 role kit (GOLIVE-03).

Skipped unless GOLIVE_TEST_DATABASE_URL is set and points at a loopback host.
Uses a scratch database and a unique cluster-wide role; both are dropped after.
"""
import os
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="GOLIVE_TEST_DATABASE_URL not set or not loopback",
)

SETUP = """
CREATE TABLE public.customers (id serial primary key, name text);
CREATE TABLE public.orders (id serial primary key, customer_id int, total numeric);
CREATE TABLE public.api_tokens (id serial primary key, token text);
INSERT INTO public.customers(name) VALUES ('a');
INSERT INTO public.api_tokens(token) VALUES ('secret');
CREATE SCHEMA reporting;
CREATE VIEW reporting.v_orders AS SELECT * FROM public.orders;
"""


def _with_db(db, user=None, pw=None):
    p = urlsplit(_URL)
    host = p.netloc.rsplit("@", 1)[-1]
    auth = f"{quote(user)}:{quote(pw)}@" if user else p.netloc.rsplit("@", 1)[0] + "@"
    return urlunsplit((p.scheme, auth + host, "/" + db, "", ""))


@pytest.fixture
async def scratch():
    import asyncpg

    from app import readonlykit as rk
    tag = secrets.token_hex(4)
    db, role = f"ic_rokit_{tag}", f"ic_ro_test_{tag}"
    pw = secrets.token_hex(12)
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{db}"')
    adm_db = await asyncpg.connect(_with_db(db))
    try:
        await adm_db.execute(SETUP)
        sql = rk.render(role, ["public", "reporting"], ["public.api_tokens"], "30s", pw)
        await adm_db.execute(sql)
        yield {"adm": adm_db, "db": db, "role": role, "pw": pw, "sql_nopw": rk.render(
            role, ["public", "reporting"], ["public.api_tokens"], "30s", "")}
    finally:
        await adm_db.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def _as_role(s):
    import asyncpg
    return await asyncpg.connect(_with_db(s["db"], s["role"], s["pw"]))


async def test_kit_twice_and_role_is_read_only(scratch):
    import asyncpg

    from app import readonlykit as rk
    await scratch["adm"].execute(scratch["sql_nopw"])  # second run keeps the password
    c = await _as_role(scratch)
    try:
        ok, d = await rk.check_role(c)
        assert ok, d
        assert "revoked tables unreadable: public.api_tokens" in d
        with pytest.raises(asyncpg.PostgresError):
            await c.execute("INSERT INTO public.orders(total) VALUES (1)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await c.fetch("SELECT * FROM public.api_tokens")
        assert await c.fetchval("SELECT count(*) FROM reporting.v_orders") == 0
        com = await c.fetchval(
            "SELECT shobj_description((SELECT oid FROM pg_roles WHERE rolname=current_user),'pg_authid')")
        assert "revoke_select: public.api_tokens" in com
    finally:
        await c.close()


async def test_negative_write_grant(scratch):
    from app import readonlykit as rk
    r = scratch["role"]
    await scratch["adm"].execute(f'GRANT INSERT ON public.orders TO "{r}"')
    c = await _as_role(scratch)
    try:
        ok, d = await rk.check_role(c)
        assert not ok and d.startswith("ROLE CAN WRITE")
    finally:
        await c.close()
        await scratch["adm"].execute(f'REVOKE INSERT ON public.orders FROM "{r}"')


async def test_negative_public_grant(scratch):
    from app import readonlykit as rk
    await scratch["adm"].execute("GRANT SELECT ON public.api_tokens TO PUBLIC")
    c = await _as_role(scratch)
    try:
        ok, d = await rk.check_role(c)
        assert not ok and "still readable" in d and "PUBLIC" in d
    finally:
        await c.close()
        await scratch["adm"].execute("REVOKE SELECT ON public.api_tokens FROM PUBLIC")


async def test_negative_read_all_data(scratch):
    from app import readonlykit as rk
    r = scratch["role"]
    await scratch["adm"].execute(f'GRANT pg_read_all_data TO "{r}"')
    c = await _as_role(scratch)
    try:
        ok, d = await rk.check_role(c)
        assert not ok and "pg_read_all_data" in d
    finally:
        await c.close()
        await scratch["adm"].execute(f'REVOKE pg_read_all_data FROM "{r}"')


VIEWS = """
CREATE VIEW public.token_report AS SELECT * FROM public.api_tokens;
CREATE VIEW reporting.token_report2 AS SELECT token FROM public.token_report;
CREATE MATERIALIZED VIEW public.token_mv AS SELECT * FROM public.api_tokens;
"""


async def test_views_over_revoked_tables(scratch):
    import asyncpg

    from app import readonlykit as rk
    r = scratch["role"]
    adm = scratch["adm"]
    await adm.execute(VIEWS)
    # simulate the pre-fix grant state: views granted, only the table revoked
    await adm.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public, reporting TO "{r}"')
    await adm.execute(f'REVOKE ALL ON public.api_tokens FROM "{r}"')
    c = await _as_role(scratch)
    try:
        assert await c.fetchval("SELECT token FROM public.token_report") == "secret"  # the hole
        ok, d = await rk.check_role(c)
        assert not ok
        assert "public.token_report reads revoked public.api_tokens and is still readable" in d
    finally:
        await c.close()
    await adm.execute(scratch["sql_nopw"])
    c = await _as_role(scratch)
    try:
        for rel in ("public.token_report", "reporting.token_report2", "public.token_mv"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.fetch(f"SELECT * FROM {rel}")
        assert await c.fetchval("SELECT count(*) FROM reporting.v_orders") == 0
        ok, d = await rk.check_role(c)
        assert ok, d
        assert "revoked tables unreadable: public.api_tokens" in d
        assert "views over revoked tables unreadable:" in d
        for rel in ("public.token_report", "reporting.token_report2", "public.token_mv"):
            assert rel in d
        com = await c.fetchval(
            "SELECT shobj_description((SELECT oid FROM pg_roles WHERE rolname=current_user),'pg_authid')")
        assert "revoke_select: public.api_tokens" in com and "token_" not in com.split("revoke_select:")[1]
    finally:
        await c.close()


async def test_kit_idempotent_with_views(scratch):
    from app import readonlykit as rk
    await scratch["adm"].execute(VIEWS)
    await scratch["adm"].execute(scratch["sql_nopw"])
    await scratch["adm"].execute(scratch["sql_nopw"])
    c = await _as_role(scratch)
    try:
        ok, d = await rk.check_role(c)
        assert ok, d
    finally:
        await c.close()


async def test_narrowing_scope_revokes_dropped_schema(scratch):
    import asyncpg

    from app import readonlykit as rk
    r = scratch["role"]
    await scratch["adm"].execute(rk.render(r, ["public"], ["public.api_tokens"], "30s", ""))
    c = await _as_role(scratch)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await c.fetch("SELECT * FROM reporting.v_orders")
        assert not await c.fetchval("SELECT has_schema_privilege(current_user, 'reporting', 'USAGE')")
        assert await c.fetchval("SELECT count(*) FROM public.customers") == 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await c.fetch("SELECT * FROM public.api_tokens")
    finally:
        await c.close()


async def test_direct_grants_on_out_of_scope_schema_revoked(scratch):
    import asyncpg

    from app import readonlykit as rk
    r = scratch["role"]
    adm = scratch["adm"]
    await adm.execute("CREATE SCHEMA extra; CREATE TABLE extra.t (id int); INSERT INTO extra.t VALUES (1)")
    await adm.execute(f'GRANT USAGE ON SCHEMA extra TO "{r}"; GRANT SELECT ON extra.t TO "{r}"')
    c = await _as_role(scratch)
    try:
        assert await c.fetchval("SELECT count(*) FROM extra.t") == 1
    finally:
        await c.close()
    await adm.execute(rk.render(r, ["public"], ["public.api_tokens"], "30s", ""))
    c = await _as_role(scratch)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await c.fetch("SELECT * FROM extra.t")
    finally:
        await c.close()


async def test_saved_url_query_parameters_reach_the_server(scratch):
    import asyncpg

    from app import readonlykit as rk
    admin_url = _with_db(scratch["db"], "admin", "x") + (
        "?sslmode=disable&application_name=ic_q029&options=-c%20search_path%3Dreporting&connect_timeout=5")
    u = rk.build_url(admin_url, scratch["role"], scratch["pw"])
    c = await asyncpg.connect(u)
    try:
        assert await c.fetchval("SHOW application_name") == "ic_q029"
        assert await c.fetchval("SHOW search_path") == "reporting"
    finally:
        await c.close()
