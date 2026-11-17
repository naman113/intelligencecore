"""Real-Postgres proof of the Phase 16 model-schema DDL run as the warehouse owner role.

Skipped unless WAREHOUSE_TEST_URL is a loopback superuser URL. Uses a scratch database and
prefixed cluster roles (same fixture shape as test_warehouse_db.py).
"""
import os
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

_URL = os.getenv("WAREHOUSE_TEST_URL") or os.getenv("GOLIVE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="WAREHOUSE_TEST_URL not set or not loopback",
)


def _with_db(db, user=None, pw=None):
    p = urlsplit(_URL)
    host = p.netloc.rsplit("@", 1)[-1]
    auth = f"{quote(user)}:{quote(pw)}@" if user else p.netloc.rsplit("@", 1)[0] + "@"
    return urlunsplit((p.scheme, auth + host, "/" + db, "", ""))


@pytest.fixture
async def mw():
    import asyncpg

    from app import warehouse, whlink
    from app.warehouse import Roles
    tag = secrets.token_hex(4)
    db = f"ic_p16_wh_{tag}"
    roles = Roles(owner=f"t16_{tag}_owner", loader=f"t16_{tag}_loader", reader=f"t16_{tag}_reader")
    pws = {"owner": secrets.token_hex(24), "loader": secrets.token_hex(24),
           "reader": secrets.token_hex(24)}
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{db}"')
    sc = await asyncpg.connect(_with_db(db))
    ow = rd = None
    try:
        await warehouse.bootstrap(sc, roles=roles, passwords=pws)
        await sc.execute("CREATE TABLE public.items (id int primary key, v numeric, d date)")
        await sc.execute("INSERT INTO public.items VALUES (1, 10, '2026-01-01'), "
                         "(2, 20, '2026-01-01'), (3, 5, '2026-02-01')")
        for r in (roles.reader, roles.owner):
            await sc.execute(f'GRANT SELECT ON public.items TO "{r}"')
        ow = await asyncpg.connect(_with_db(db, roles.owner, pws["owner"]))
        rd = await asyncpg.connect(_with_db(db, roles.reader, pws["reader"]))
        await whlink.ensure_meta(ow, roles)
        yield {"sc": sc, "ow": ow, "rd": rd, "roles": roles}
    finally:
        for c in (ow, rd):
            if c is not None:
                await c.close()
        await sc.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for r in (roles.owner, roles.loader, roles.reader):
            await admin.execute(f'DROP ROLE IF EXISTS "{r}"')
        await admin.close()


async def test_ensure_model_schema_idempotent(mw):
    from app import whlink
    ow, sc, r = mw["ow"], mw["sc"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    await whlink.ensure_model_schema(ow, r)
    owner = await sc.fetchval("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'model'")
    assert owner == r.owner
    assert await sc.fetchval("SELECT has_schema_privilege($1, 'model', 'USAGE')", r.reader)


async def test_view_is_security_invoker_and_readable(mw):
    from app import whlink
    ow, rd, sc, r = mw["ow"], mw["rd"], mw["sc"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    cols = await whlink.create_model_view(
        ow, "item_totals", "SELECT d, SUM(v) AS total FROM public.items GROUP BY d", r)
    assert cols == {"d": "date", "total": "numeric"}
    opts = await sc.fetchval("SELECT reloptions FROM pg_class WHERE oid = 'model.item_totals'::regclass")
    assert "security_invoker=true" in opts
    rows = await rd.fetch("SELECT * FROM model.item_totals ORDER BY d")
    assert len(rows) == 2
    # replacing with the same columns is fine; changed columns are refused with fixed text
    await whlink.create_model_view(
        ow, "item_totals", "SELECT d, SUM(v) AS total FROM public.items GROUP BY d", r)
    with pytest.raises(whlink.ModelDDLRefused) as ei:
        await whlink.create_model_view(ow, "item_totals", "SELECT d FROM public.items", r)
    assert "columns changed" in str(ei.value)


async def test_check_reader_refs(mw):
    from app import whlink
    ow, r = mw["ow"], mw["roles"]
    await whlink.check_reader_refs(ow, ["public.items"], r)
    for bad in (["ic_meta.link_state"], ["public.missing"], ["items"], ["pg_catalog.pg_class"],
                ["information_schema.tables"]):
        with pytest.raises(whlink.ModelDDLRefused) as ei:
            await whlink.check_reader_refs(ow, bad, r)
        assert "Error" not in str(ei.value) and "permission" not in str(ei.value).lower()


async def test_link_state_detail_not_reachable_through_a_view(mw):
    """Research Pitfall 4: even a manual security_invoker view cannot widen the reader's access."""
    import asyncpg

    from app import whlink
    ow, rd, r = mw["ow"], mw["rd"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.check_reader_refs(ow, ["ic_meta.link_state"], r)
    tr = ow.transaction()
    await tr.start()
    try:
        await ow.execute("CREATE VIEW model.probe_ls WITH (security_invoker = true) AS "
                         "SELECT detail FROM ic_meta.link_state")
        await ow.execute(f'GRANT SELECT ON model.probe_ls TO "{r.reader}"')
        # the reader sees the view in the same session only after commit; probe via SET ROLE-free path
        await tr.commit()
        tr = None
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await rd.fetch("SELECT * FROM model.probe_ls")
    finally:
        if tr is not None:
            await tr.rollback()
        await ow.execute("DROP VIEW IF EXISTS model.probe_ls")


async def test_matview_refresh_and_index(mw):
    from app import whlink
    ow, rd, r = mw["ow"], mw["rd"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    cols = await whlink.create_model_matview(ow, "item_mv", "SELECT id, v FROM public.items", r)
    assert set(cols) == {"id", "v"}
    assert len(await rd.fetch("SELECT * FROM model.item_mv")) == 3
    await whlink.refresh_model_matview(ow, "item_mv")
    await whlink.create_model_index(ow, "ix_item_mv_id", "item_mv", ["id"])
    await whlink.create_model_index(ow, "ix_item_mv_id", "item_mv", ["id"])
    assert await ow.fetchval("SELECT count(*) FROM pg_indexes WHERE schemaname = 'model' "
                             "AND indexname = 'ix_item_mv_id'") == 1
    await whlink.create_model_view(ow, "item_v", "SELECT id FROM public.items", r)
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.create_model_index(ow, "ix_bad", "item_v", ["id"])
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.create_model_index(ow, "ix_bad", "items", ["id"])
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.create_model_index(ow, "ix_bad", "item_mv", ["nope"])


async def test_drop_never_cascades(mw):
    from app import whlink
    ow, r = mw["ow"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    await whlink.create_model_view(ow, "base_v", "SELECT id, v FROM public.items", r)
    await whlink.create_model_view(ow, "dep_v", "SELECT id FROM model.base_v", r)
    with pytest.raises(whlink.ModelDDLRefused) as ei:
        await whlink.drop_model_object(ow, "view", "base_v")
    assert "depend" in str(ei.value)
    names = {x["relname"] for x in await ow.fetch(
        "SELECT relname FROM pg_class WHERE relnamespace = 'model'::regnamespace")}
    assert {"base_v", "dep_v"} <= names
    await whlink.drop_model_object(ow, "view", "dep_v")
    await whlink.drop_model_object(ow, "view", "base_v")
    names = {x["relname"] for x in await ow.fetch(
        "SELECT relname FROM pg_class WHERE relnamespace = 'model'::regnamespace")}
    assert "base_v" not in names and "dep_v" not in names


async def test_model_schema_name_clash_refused(mw):
    from app import whlink
    ow, r = mw["ow"], mw["roles"]
    await ow.execute("INSERT INTO ic_meta.link_objects (source, remote_schema, remote_table, mirrored_schema) "
                     "VALUES ('business_db', 'model', 't', 'model')")
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.ensure_model_schema(ow, r)


async def test_ddl_runs_under_a_statement_timeout(mw, monkeypatch):
    from app import whlink
    ow, r = mw["ow"], mw["roles"]
    await whlink.ensure_model_schema(ow, r)
    seen = []
    real = whlink.exec_fmt

    async def spy(conn, template, *values):
        if template.startswith("CREATE OR REPLACE VIEW"):
            seen.append(await conn.fetchval("SHOW statement_timeout"))
        await real(conn, template, *values)

    monkeypatch.setattr(whlink, "exec_fmt", spy)
    await whlink.create_model_view(ow, "t_v", "SELECT id FROM public.items", r)
    assert seen == ["2min"]


async def test_invalid_names_refused(mw):
    from app import whlink
    ow, r = mw["ow"], mw["roles"]
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.create_model_view(ow, "Bad Name;", "SELECT 1", r)
    with pytest.raises(whlink.ModelDDLRefused):
        await whlink.drop_model_object(ow, "table", "x")
