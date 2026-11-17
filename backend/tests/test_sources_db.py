"""Real-Postgres proofs for the Phase 15 registry (SRC-01). Needs GOLIVE_TEST_DATABASE_URL (loopback)."""
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
_GOLIVE_OK = bool(_URL) and (urlsplit(_URL).hostname or "") in ("127.0.0.1", "localhost")
SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg
    if not _GOLIVE_OK:
        pytest.skip("GOLIVE_TEST_DATABASE_URL not set or not loopback")
    name = "ic_src_" + secrets.token_hex(4)
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


async def test_registry_schema_twice(pool):
    await pool.execute(SCHEMA.read_text(encoding="utf-8"))
    assert await pool.fetchval("SELECT count(*) FROM sources") == 1


async def test_registry_business_db_single_row_and_xmin(pool):
    q = "SELECT xmin::text FROM sources WHERE name='business_db'"
    before = await pool.fetchval(q)
    await pool.execute(SCHEMA.read_text(encoding="utf-8"))
    assert await pool.fetchval(q) == before
    r = await pool.fetchrow("SELECT kind, config::text c FROM sources WHERE name='business_db'")
    assert r["kind"] == "postgres" and '"legacy": true' in r["c"]


async def test_registry_inflight_unique(pool):
    import asyncpg
    sid = await pool.fetchval("SELECT id FROM sources WHERE name='business_db'")
    first = await pool.fetchval("INSERT INTO sync_runs (source_id) VALUES ($1) RETURNING id", sid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute("INSERT INTO sync_runs (source_id) VALUES ($1)", sid)
    await pool.execute("UPDATE sync_runs SET status='ok' WHERE id=$1", first)
    await pool.execute("INSERT INTO sync_runs (source_id) VALUES ($1)", sid)


async def test_registry_source_change_pending_unique(pool):
    sql = ("INSERT INTO actions (kind, tier, status, payload) VALUES ('source_change','approve','proposed',$1::jsonb) "
           "ON CONFLICT DO NOTHING RETURNING id")
    assert await pool.fetchval(sql, '{"name":"n1"}') is not None
    assert await pool.fetchval(sql, '{"name":"n1"}') is None
    assert await pool.fetchval(sql, '{"name":"n2"}') is not None


async def test_registry_checks(pool):
    import asyncpg
    sid = await pool.fetchval("SELECT id FROM sources WHERE name='business_db'")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO sources (name, kind) VALUES ('o','oracle')")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO source_tables (source_id, remote_table, warehouse_schema, "
                           "warehouse_table, mode) VALUES ($1,'t','s','t','copy')", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO sync_runs (source_id, status) VALUES ($1,'done')", sid)


async def test_registry_business_db_tables_sync_idempotent(pool, monkeypatch):
    from app import schemadiscovery, sourcesvc

    async def intro(source="business_db"):
        return {"tables": [{"schema": "public", "table": t} for t in ("a", "b", "c")]}

    async def gp():
        return pool
    monkeypatch.setattr(schemadiscovery, "introspect", intro)
    monkeypatch.setattr(schemadiscovery, "business_db_configured", lambda: True)  # Quick 031
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    assert (await sourcesvc.sync_business_db_tables())["inserted"] == 3
    q = "SELECT string_agg(xmin::text, ',' ORDER BY id) FROM source_tables"
    before = await pool.fetchval(q)
    r = await sourcesvc.sync_business_db_tables()
    assert r["unchanged"] == 3 and await pool.fetchval(q) == before
    assert await pool.fetchval("SELECT count(*) FROM source_tables WHERE mode='live'") == 3


async def test_vault_audit_rows_real_db(pool, monkeypatch):
    from app import secretbox, sourcevault, syncrunner
    from app.config import get_settings
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", secrets.token_hex(32))
    get_settings.cache_clear()

    async def gp():
        return pool

    async def ri(*a, **k):
        return {"status": "ok", "read_only": True, "exit_code": 0}

    monkeypatch.setattr(sourcevault, "get_pool", gp)
    monkeypatch.setattr(syncrunner, "run_interactive", ri)
    f = {"host": "db.example", "port": 5432, "database": "erp", "user": "ro", "password": "pw-longsecret1"}
    try:
        await sourcevault.save("erp_pg", "postgres", f)
        q = "SELECT status, decided_by FROM actions WHERE kind='source_credential' ORDER BY id"
        rows = await pool.fetch(q)
        assert len(rows) == 1 and rows[0]["status"] == "executed" and rows[0]["decided_by"] is None
        assert await pool.fetchval("SELECT count(*) FROM source_credentials") == 1
        pid = await pool.fetchval(
            "INSERT INTO people (name, is_primary, active) VALUES ('Owner', true, true) RETURNING id")
        await sourcevault.verify("erp_pg")
        rows = await pool.fetch(q)
        assert len(rows) == 2 and rows[1]["decided_by"] == pid
        await sourcevault.delete("erp_pg")
        assert len(await pool.fetch(q)) == 3
        assert await pool.fetchval("SELECT count(*) FROM source_credentials") == 0
    finally:
        get_settings.cache_clear()
        secretbox._known.clear()
        secretbox._extra_known.clear()


# ── Phase 15 (15-08): warehouse provisioning proofs (real Postgres, WAREHOUSE_TEST_URL) ──
_WH_URL = os.getenv("WAREHOUSE_TEST_URL", "")
_WH_OK = bool(_WH_URL) and (urlsplit(_WH_URL).hostname or "") in ("127.0.0.1", "localhost")


def _wh_dsn(db, user=None, pw=None):
    from urllib.parse import quote
    p = urlsplit(_WH_URL)
    host = p.netloc.rsplit("@", 1)[-1]
    auth = f"{quote(user)}:{quote(pw)}@" if user else p.netloc.rsplit("@", 1)[0] + "@"
    return urlunsplit((p.scheme, auth + host, "/" + db, "", ""))


SRC15_SETUP = """
CREATE TYPE mood AS ENUM ('happy','sad');
CREATE TABLE regions (id int PRIMARY KEY, name text, feeling mood);
INSERT INTO regions VALUES (1,'north','happy'),(2,'south','sad'),(3,'east','happy');
CREATE TABLE orders (id int PRIMARY KEY, region_id int REFERENCES regions(id), total numeric(10,2));
CREATE TABLE items (id int PRIMARY KEY, label text);
"""

PG_PROBE = {"tables": [
    {"schema": "public", "table": "regions", "est_rows": 3, "pk": ["id"], "fks": [],
     "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                 {"name": "name", "type": "text", "nullable": True},
                 {"name": "feeling", "type": "mood", "nullable": True}]},
    {"schema": "public", "table": "orders", "est_rows": 0, "pk": ["id"],
     "fks": [{"columns": ["region_id"], "ref_schema": "public", "ref_table": "regions",
              "ref_columns": ["id"]}],
     "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                 {"name": "region_id", "type": "integer", "nullable": True},
                 {"name": "total", "type": "numeric(10,2)", "nullable": True}]},
    {"schema": "public", "table": "items", "est_rows": 0, "pk": ["id"], "fks": [],
     "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                 {"name": "label", "type": "text", "nullable": True}]}]}


def _tbl(remote, mode, schema="public"):
    return {"remote_schema": schema, "remote_table": remote, "warehouse_schema": None,
            "warehouse_table": remote, "mode": mode, "status": "active", "est_rows": None}


@pytest.fixture
async def whs(monkeypatch):
    import asyncpg

    from app import warehouse, whsources
    from app.warehouse import Roles
    if not _WH_OK:
        pytest.skip("WAREHOUSE_TEST_URL not set or not loopback")
    tag = secrets.token_hex(4)
    db, sdb_name = f"ic_p15_wh_{tag}", f"ic_p15_src_{tag}"
    roles = Roles(owner=f"t15_{tag}_owner", loader=f"t15_{tag}_loader", reader=f"t15_{tag}_reader")
    pws = {k: secrets.token_hex(16) for k in ("owner", "loader", "reader")}
    ro, ro_pw = f"t15_{tag}_ro", secrets.token_hex(16)
    admin = await asyncpg.connect(_WH_URL)
    await admin.execute(f'CREATE DATABASE "{db}"')
    await admin.execute(f'CREATE DATABASE "{sdb_name}"')
    sc = await asyncpg.connect(_wh_dsn(db))
    sdb = await asyncpg.connect(_wh_dsn(sdb_name))
    conns = []
    try:
        await sdb.execute(SRC15_SETUP)
        await sdb.execute(f"CREATE ROLE \"{ro}\" LOGIN PASSWORD '{ro_pw}'")
        await sdb.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ro}"')
        host = await sdb.fetchval("SELECT host(inet_server_addr())")
        port = await sdb.fetchval("SELECT inet_server_port()")
        await warehouse.bootstrap(sc, roles=roles, passwords=pws)

        async def oc(r):
            return await asyncpg.connect(_wh_dsn(db, roles.owner, pws["owner"]))
        monkeypatch.setattr(whsources, "_owner_conn", oc)
        monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
        ow = await oc(roles)
        rd = await asyncpg.connect(_wh_dsn(db, roles.reader, pws["reader"]))
        ld = await asyncpg.connect(_wh_dsn(db, roles.loader, pws["loader"]))
        conns += [ow, rd, ld]
        cred = {"host": host, "port": port, "database": sdb_name, "user": ro, "password": ro_pw}
        yield {"sc": sc, "ow": ow, "rd": rd, "ld": ld, "roles": roles, "db": db, "cred": cred,
               "tag": tag, "admin": admin, "sdb": sdb}
    finally:
        for c in conns + [sc, sdb]:
            try:
                await c.close()
            except Exception:
                pass
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{sdb_name}" WITH (FORCE)')
        for r in (roles.owner, roles.loader, roles.reader):
            await admin.execute(f'DROP ROLE IF EXISTS "{r}"')
        await admin.close()


def _register(monkeypatch, name, kind, tables, probe, cred):
    from app import sourcesvc, sourcevault, whsources

    async def gs(n):
        return {"id": 1, "name": name, "kind": kind, "credential": "cred:" + name,
                "config": {}, "tables": tables}

    async def ld(n):
        return dict(cred)

    async def lp(src):
        return probe
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    monkeypatch.setattr(sourcevault, "load", ld)
    monkeypatch.setattr(whsources, "_load_probe", lp)


async def test_provision_bootstrap_grants(whs):
    sc, r = whs["sc"], whs["roles"]
    assert await sc.fetchval("SELECT rolconnlimit FROM pg_roles WHERE rolname=$1", r.loader) == 5
    assert not await sc.fetchval("SELECT has_database_privilege($1, $2, 'CREATE')", r.loader, whs["db"])
    assert await sc.fetchval(
        "SELECT has_foreign_data_wrapper_privilege($1, 'postgres_fdw', 'USAGE')", r.owner)
    for fdw in ("mysql_fdw", "tds_fdw"):
        if await sc.fetchval("SELECT 1 FROM pg_extension WHERE extname=$1", fdw):
            assert await sc.fetchval(
                "SELECT has_foreign_data_wrapper_privilege($1, $2, 'USAGE')", r.owner, fdw)
    acl = await sc.fetchval(
        "SELECT d.defaclacl::text FROM pg_default_acl d JOIN pg_roles o ON o.oid = d.defaclrole "
        "WHERE o.rolname = $1 AND d.defaclobjtype = 'r' AND d.defaclnamespace = 0", r.loader)
    assert acl and r.owner in acl


async def _provision_pg(whs, monkeypatch, tables=None):
    from app import whsources
    tables = tables or [_tbl("regions", "live"), _tbl("orders", "import")]
    _register(monkeypatch, "pgsrc", "postgres", tables, PG_PROBE, whs["cred"])
    return await whsources.provision("pgsrc", whs["roles"])


async def test_provision_live_postgres(whs, monkeypatch):
    import asyncpg

    from app import whlink, whsources
    res = await _provision_pg(whs, monkeypatch)
    assert res["status"] in ("ok", "partial"), res
    assert res["live"] == 1
    rd, ow = whs["rd"], whs["ow"]
    rows = await rd.fetch("SELECT id, name, feeling::text f FROM src_pgsrc.regions ORDER BY id")
    assert [r["name"] for r in rows] == ["north", "south", "east"] and rows[1]["f"] == "sad"
    priv = whlink.private_schema("src_pgsrc", "public")
    await whsources.ensure_stage("pgsrc", whs["roles"])
    for q in (f'SELECT 1 FROM "{priv}".regions', "SELECT 1 FROM ic_imp_pgsrc.orders",
              "SELECT 1 FROM stage_pgsrc.anything"):
        with pytest.raises(asyncpg.PostgresError) as e:
            await rd.fetch(q)
        assert isinstance(e.value, asyncpg.InsufficientPrivilegeError), q
    # an import table appears after the loader writes it
    await whs["ld"].execute("CREATE TABLE ic_imp_pgsrc.orders (id int, total numeric(10,2), "
                            "_dlt_load_id text, _dlt_id text)")
    await whs["ld"].execute("INSERT INTO ic_imp_pgsrc.orders VALUES (1, 9.5, 'l', 'i')")
    out = await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    assert out["status"] == "ok" and out["refreshed"] == 1
    assert [float(r["total"]) for r in await rd.fetch("SELECT * FROM src_pgsrc.orders")] == [9.5]
    cat = {(r["table_name"], r["relkind"]) for r in await ow.fetch(
        "SELECT table_name, relkind FROM ic_meta.source_catalog WHERE source='pgsrc' "
        "AND schema_name='src_pgsrc'")}
    assert cat == {("regions", "f"), ("orders", "r")}
    assert await ow.fetchval("SELECT count(*) FROM ic_meta.link_objects WHERE source='src_pgsrc' "
                             "AND remote_table='regions'") == 1


async def test_provision_import_view_refresh(whs, monkeypatch):
    from app import whsources
    await _provision_pg(whs, monkeypatch)
    ld, rd = whs["ld"], whs["rd"]
    await ld.execute("CREATE TABLE ic_imp_pgsrc.orders (_dlt_load_id text, a int, b text, "
                     "_dlt_id text)")
    await ld.execute("INSERT INTO ic_imp_pgsrc.orders VALUES ('l', 1, 'x', 'i')")
    await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    cols = [r["column_name"] for r in await rd.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='src_pgsrc' "
        "AND table_name='orders' ORDER BY ordinal_position")]
    assert cols == ["a", "b"]
    await ld.execute("ALTER TABLE ic_imp_pgsrc.orders ADD COLUMN c int")
    out = await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    assert out["status"] == "ok"
    row = await rd.fetchrow("SELECT * FROM src_pgsrc.orders")
    assert list(row.keys()) == ["a", "b", "c"]
    # a dependent object created by someone else blocks an incompatible refresh
    await whs["sc"].execute("CREATE VIEW public.dependent AS SELECT a FROM src_pgsrc.orders")
    await ld.execute("ALTER TABLE ic_imp_pgsrc.orders RENAME COLUMN a TO a2")
    out = await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    assert out["status"] == "stale" and out["stale"] == ["orders"]
    assert list((await rd.fetchrow("SELECT * FROM src_pgsrc.orders")).keys()) == ["a", "b", "c"]
    await whs["sc"].execute("DROP VIEW public.dependent")
    # Phase 17 (CONS-05): a vanished column is refused, never fixed by DROP VIEW; the view stays
    out = await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    assert out["status"] == "stale" and out["stale"] == ["orders"]
    assert list((await rd.fetchrow("SELECT * FROM src_pgsrc.orders")).keys()) == ["a", "b", "c"]


async def _snapshot_state(ow):
    objs = await ow.fetch(
        "SELECT c.oid::int o, n.nspname || '.' || c.relname r FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname LIKE 'src\\_%' OR n.nspname LIKE 'ic\\_fdw\\_src\\_%' ORDER BY 2")
    srv = await ow.fetch("SELECT oid::int o, srvname FROM pg_foreign_server ORDER BY 2")
    cat = await ow.fetch("SELECT table_name, xmin::text x FROM ic_meta.source_catalog ORDER BY 1")
    lo = await ow.fetch("SELECT remote_table, linked_at FROM ic_meta.link_objects "
                        "WHERE source LIKE 'src\\_%' ORDER BY 1")
    return ([tuple(r) for r in objs], [tuple(r) for r in srv], [tuple(r) for r in cat],
            [tuple(r) for r in lo])


async def test_provision_rerun_no_ddl(whs, monkeypatch):
    from app import whsources
    first = await _provision_pg(whs, monkeypatch)
    assert first["status"] in ("ok", "partial")
    before = await _snapshot_state(whs["ow"])
    assert before[0] and before[2]
    again = await whsources.provision("pgsrc", whs["roles"])
    assert again["status"] in ("ok", "partial")
    assert await _snapshot_state(whs["ow"]) == before


async def test_write_catalog_keeps_every_registered_table(whs, monkeypatch):
    from app import whsources
    tables = [_tbl("regions", "live"), _tbl("orders", "import"), _tbl("items", "import")]
    await _provision_pg(whs, monkeypatch, tables)
    ow, ld = whs["ow"], whs["ld"]
    await ld.execute("CREATE TABLE ic_imp_pgsrc.orders (id int, region_id int, total numeric(10,2), "
                     "_dlt_load_id text)")
    await whsources.write_catalog(ow, "pgsrc")
    q = "SELECT table_name, xmin::text x FROM ic_meta.source_catalog WHERE source='pgsrc' ORDER BY 1"
    before = {r["table_name"]: r["x"] for r in await ow.fetch(q)}
    assert set(before) == {"items", "orders", "regions"}
    await ld.execute("ALTER TABLE ic_imp_pgsrc.orders ADD COLUMN note text")
    await whsources.write_catalog(ow, "pgsrc")
    after = {r["table_name"]: r["x"] for r in await ow.fetch(q)}
    assert set(after) == set(before)
    assert after["orders"] != before["orders"]
    assert after["regions"] == before["regions"] and after["items"] == before["items"]
    cols = await ow.fetchval("SELECT columns::text FROM ic_meta.source_catalog "
                             "WHERE source='pgsrc' AND table_name='orders'")
    assert "note" in cols and "_dlt_load_id" not in cols


async def test_provision_tds_owner_mapping(whs, monkeypatch):
    import asyncpg

    from app import whlink, whsources
    sc, ow, rd, roles = whs["sc"], whs["ow"], whs["rd"], whs["roles"]
    if not await sc.fetchval("SELECT 1 FROM pg_available_extensions WHERE name='tds_fdw'"):
        pytest.skip("tds_fdw not in this warehouse image")
    probe = {"tables": [{"schema": "dbo", "table": "lookup", "est_rows": 10, "columns": [
        {"name": "id", "type": "int", "nullable": False}, {"name": "label", "type": "nvarchar(40)"}]}]}
    cred = {"host": "127.0.0.1", "port": 1, "database": "d", "user": "u", "password": "pw-ms"}
    _register(monkeypatch, "mssrc", "mssql", [_tbl("lookup", "live", "dbo")], probe, cred)
    res = await whsources.provision("mssrc", roles)
    assert res["status"] == "ok" and res["live"] == 1, res
    # (a) the reader reaches tds_fdw's connection attempt with the owner's mapping
    with pytest.raises(asyncpg.PostgresError) as e:
        await rd.fetch("SELECT * FROM src_mssrc.lookup")
    msg = str(e.value)
    print("WRAPPER ERROR (a):", type(e.value).__name__, msg[:300])
    assert "user mapping not found" not in msg and "permission denied" not in msg
    # (b) the private foreign table is not readable by the reader
    priv = whlink.private_schema("src_mssrc", "dbo")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await rd.fetch(f'SELECT * FROM "{priv}".lookup')
    # (c) exactly one mapping, for the owner
    maps = await sc.fetch("SELECT u.usename FROM pg_user_mappings u WHERE u.srvname='src_mssrc'")
    assert [m["usename"] for m in maps] == [roles.owner]
    # (d) wrapper properties and privileges
    w = whsources.tds_wrapper_name("lookup")
    fn = await sc.fetchrow(
        "SELECT p.oid, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) owner FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname=$1 AND p.proname=$2",
        priv, w)
    assert fn["prosecdef"] and fn["owner"] == roles.owner
    assert "search_path=pg_catalog, pg_temp" in list(fn["proconfig"])
    assert await sc.fetchval("SELECT has_function_privilege($1, $2::oid, 'EXECUTE')",
                             roles.reader, fn["oid"])
    scratch = f"t15_{whs['tag']}_scratch"
    await sc.execute(f'CREATE ROLE "{scratch}"')
    try:
        assert not await sc.fetchval("SELECT has_function_privilege($1, $2::oid, 'EXECUTE')",
                                     scratch, fn["oid"])
    finally:
        await sc.execute(f'DROP ROLE "{scratch}"')
    # (e) control, informational: a plain owner view over the foreign table
    await ow.execute(f'CREATE VIEW public.ic_ctl AS SELECT id, label FROM "{priv}".lookup')
    await ow.execute(f'GRANT SELECT ON public.ic_ctl TO "{roles.reader}"')
    try:
        try:
            await rd.fetch("SELECT * FROM public.ic_ctl")
            print("CONTROL (e): no error")
        except asyncpg.PostgresError as ce:
            print("CONTROL (e):", type(ce).__name__, str(ce)[:300])
    finally:
        await ow.execute("DROP VIEW public.ic_ctl")


@pytest.mark.skipif(not os.getenv("IC_TEST_MYSQL_HOST"), reason="IC_TEST_MYSQL_HOST not set")
async def test_provision_live_mysql(whs, monkeypatch):
    from app import whsources
    if not await whs["sc"].fetchval("SELECT 1 FROM pg_available_extensions WHERE name='mysql_fdw'"):
        pytest.fail("mysql_fdw is not in this warehouse image")
    probe = {"tables": [{"schema": "northwind_ops", "table": "carriers", "est_rows": 8, "columns": [
        {"name": "id", "type": "int", "nullable": False, "pk": True},
        {"name": "name", "type": "varchar(60)", "nullable": False},
        {"name": "mode", "type": "enum('road','sea','air')", "nullable": False}]}]}
    cred = {"host": os.environ["IC_TEST_MYSQL_HOST"], "port": 3306, "database": "northwind_ops",
            "user": "src_ro", "password": "src_ro_demo"}
    _register(monkeypatch, "myops", "mysql", [_tbl("carriers", "live", "northwind_ops")], probe, cred)
    res = await whsources.provision("myops", whs["roles"])
    assert res["status"] == "ok" and res["live"] == 1, res
    rows = await whs["rd"].fetch("SELECT id, name, mode FROM src_myops.carriers ORDER BY id")
    assert len(rows) == 8


# ── Phase 15 plan 15-09: scheduler sweep against real Postgres ──
async def _due_source(pool, monkeypatch):
    from app import syncsched

    async def gp():
        return pool
    monkeypatch.setattr(syncsched, "get_pool", gp)
    sid = await pool.fetchval(
        "INSERT INTO sources (name, kind, credential, warehouse_schema) "
        "VALUES ('shop','mysql','cred:shop','src_shop') RETURNING id")
    await pool.execute(
        "INSERT INTO source_tables (source_id, remote_table, warehouse_schema, warehouse_table, mode, "
        "backfill_done) VALUES ($1,'orders','src_shop','orders','import',true)", sid)
    return syncsched, sid


async def test_sweep_enqueue_dedupe(pool, monkeypatch):
    syncsched, sid = await _due_source(pool, monkeypatch)
    first = await syncsched.enqueue_due()
    second = await syncsched.enqueue_due()
    assert len(first) == 1 and second == []
    assert await pool.fetchval("SELECT count(*) FROM sync_runs WHERE source_id=$1", sid) == 1
    assert await pool.fetchval("SELECT count(*) FROM task_runs WHERE kind='sync' AND thread_id='sync'") == 1
    assert await pool.fetchval(
        "SELECT t.id = s.task_run_id FROM sync_runs s JOIN task_runs t ON t.id = s.task_run_id") is True
    assert await pool.fetchval("SELECT next_sync_at > now() FROM sources WHERE id=$1", sid) is True


async def test_stuck_run_reaped_then_enqueue(pool, monkeypatch):
    syncsched, sid = await _due_source(pool, monkeypatch)
    (rid,) = await syncsched.enqueue_due()
    await pool.execute("UPDATE sync_runs SET status='running' WHERE id=$1", rid)
    await pool.execute("UPDATE task_runs SET status='failed' WHERE kind='sync'")
    await pool.execute("UPDATE sources SET next_sync_at = now() WHERE id=$1", sid)
    assert await syncsched.enqueue_due() == []  # the stuck row still blocks
    assert await syncsched.reap_stuck_runs() == 1
    assert await pool.fetchval("SELECT status FROM sync_runs WHERE id=$1", rid) == "failed"
    again = await syncsched.enqueue_due()
    assert len(again) == 1 and again[0] != rid
    assert await pool.fetchval("SELECT count(*) FROM task_runs WHERE kind='sync'") == 2


# ── Phase 15 (15-12, SRC-09): discovery over a registered source (real Postgres) ──
async def test_discovery_registered_source(whs, monkeypatch):
    from app import schemadiscovery as sd, whsources
    from app.guardrails import run_readonly_sql  # noqa: F401  (module under test uses it)
    tables = [_tbl("regions", "live"), _tbl("orders", "import")]
    await _provision_pg(whs, monkeypatch, tables)
    await whs["ld"].execute("CREATE TABLE ic_imp_pgsrc.orders (id int, region_id int, "
                            "total numeric(10,2), _dlt_load_id text, _dlt_id text)")
    await whsources.refresh_import_views("pgsrc", roles=whs["roles"])
    await whsources.write_catalog(whs["ow"], "pgsrc")
    sc_name = await whs["ow"].fetchval(
        "SELECT schema_name FROM ic_meta.source_catalog WHERE source='pgsrc' LIMIT 1")

    async def reader_sql(sql, *a, **k):
        return [dict(r) for r in await whs["rd"].fetch(sql)]
    monkeypatch.setattr(sd, "run_readonly_sql", reader_sql)

    class _S:
        sql_row_limit = 500
    monkeypatch.setattr(sd, "get_settings", lambda: _S())

    async def active(source):
        return source == "pgsrc"
    monkeypatch.setattr(sd, "_registered", active)
    cat = await sd.introspect("pgsrc")
    by = {t["table"]: t for t in cat["tables"]}
    assert set(by) == {"regions", "orders"}
    assert by["regions"]["schema"] == sc_name
    types = {c["name"]: c["type"] for c in by["regions"]["columns"]}
    assert types["id"] == "integer" and types["name"] == "text"
    assert {c["name"]: c["type"] for c in by["orders"]["columns"]}["total"] == "numeric(10,2)"
    r = await sd.resolve(f"pgsrc.{sc_name}.orders.total", cat)
    assert r["kind"] == "column" and r["source"] == "pgsrc"


# Phase 15 gap 2 (15-20): mixed-case live tables keep the remote name inside the foreign table
async def test_provision_live_postgres_mixed_case(whs, monkeypatch):
    from app import whlink, whsources
    sdb, ow, rd, roles = whs["sdb"], whs["ow"], whs["rd"], whs["roles"]
    await sdb.execute(
        'CREATE TABLE "OrderLines" (id int PRIMARY KEY, "OrderID" int, "Qty" int);'
        "INSERT INTO \"OrderLines\" VALUES (1, 10, 2), (2, 10, 3), (3, 11, 5);"
        'CREATE TABLE "Order" (id int PRIMARY KEY, label text);'
        "INSERT INTO \"Order\" VALUES (1, 'a'), (2, 'b');")
    await sdb.execute(f'GRANT SELECT ON "OrderLines", "Order" TO "{whs["cred"]["user"]}"')
    probe = {"tables": [
        {"schema": "public", "table": "OrderLines", "est_rows": 3, "pk": ["id"], "fks": [],
         "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                     {"name": "OrderID", "type": "integer"}, {"name": "Qty", "type": "integer"}]},
        {"schema": "public", "table": "Order", "est_rows": 2, "pk": ["id"], "fks": [],
         "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                     {"name": "label", "type": "text"}]}]}

    def tabs(*pairs):
        return [dict(_tbl(r, "live"), warehouse_table=w) for r, w in pairs]
    both = tabs(("OrderLines", "order_lines"), ("Order", "order_tbl"))
    _register(monkeypatch, "pgsrc", "postgres", both, probe, whs["cred"])
    res = await whsources.provision("pgsrc", roles)
    assert res["status"] in ("ok", "partial"), res
    assert res["live"] == 2, res
    assert await rd.fetchval("SELECT count(*) FROM src_pgsrc.order_lines") == 3
    assert await rd.fetchval('SELECT sum("Qty") FROM src_pgsrc.order_lines') == 10
    assert await rd.fetchval("SELECT count(*) FROM src_pgsrc.order_tbl") == 2
    plan = "\n".join(r[0] for r in await rd.fetch("EXPLAIN SELECT * FROM src_pgsrc.order_lines"))
    assert "Foreign Scan" in plan
    priv = whlink.private_schema("src_pgsrc", "public")
    opts = await ow.fetchval(
        "SELECT ftoptions::text FROM pg_foreign_table f JOIN pg_class c ON c.oid = f.ftrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = $1 "
        "AND c.relname = 'OrderLines'", priv)
    assert "table_name=OrderLines" in opts
    assert await ow.fetchval(
        "SELECT view_name FROM ic_meta.link_objects WHERE source='src_pgsrc' "
        "AND remote_table='OrderLines'") == "order_lines"
    before = await _snapshot_state(ow)
    again = await whsources.provision("pgsrc", roles)
    assert again["status"] in ("ok", "partial")
    assert await _snapshot_state(ow) == before
    # only "Order" desired: the order_lines view and its link row go
    _register(monkeypatch, "pgsrc", "postgres", tabs(("Order", "order_tbl")), probe, whs["cred"])
    await whsources.provision("pgsrc", roles)
    assert await ow.fetchval("SELECT to_regclass('src_pgsrc.order_lines')") is None
    assert await ow.fetchval("SELECT to_regclass('src_pgsrc.order_tbl')") is not None
    assert await ow.fetchval(
        "SELECT count(*) FROM ic_meta.link_objects WHERE source='src_pgsrc' "
        "AND remote_table='OrderLines'") == 0

