"""Real-Postgres proof for warehouse.bootstrap / check_reader (Phase 14, WH-02).

Skipped unless WAREHOUSE_TEST_URL (or GOLIVE_TEST_DATABASE_URL) is a loopback superuser URL.
Uses a scratch database and prefixed cluster roles; both are dropped afterwards.
Extended by plans 14-04, 14-05 and 14-06.
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
async def wh():
    import asyncpg

    from app.warehouse import Roles
    tag = secrets.token_hex(4)
    db = f"ic_p14_wh_{tag}"
    roles = Roles(owner=f"t14_{tag}_owner", loader=f"t14_{tag}_loader", reader=f"t14_{tag}_reader")
    pws = {"owner": secrets.token_hex(24), "loader": secrets.token_hex(24),
           "reader": secrets.token_hex(24)}
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{db}"')
    sc = await asyncpg.connect(_with_db(db))
    try:
        yield {"sc": sc, "db": db, "roles": roles, "pws": pws, "tag": tag}
    finally:
        await sc.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for r in (roles.owner, roles.loader, roles.reader):
            await admin.execute(f'DROP ROLE IF EXISTS "{r}"')
        await admin.close()


async def _boot(w):
    from app import warehouse
    return await warehouse.bootstrap(w["sc"], roles=w["roles"], passwords=w["pws"])


async def test_bootstrap_twice_idempotent(wh):
    r1 = await _boot(wh)
    r2 = await _boot(wh)
    assert r1["roles"] == r2["roles"] == [wh["roles"].owner, wh["roles"].loader, wh["roles"].reader]
    ext = r2["extensions"]
    assert ext["postgres_fdw"] is True and ext["pg_stat_statements"] is True
    assert isinstance(ext["mysql_fdw"], bool) and isinstance(ext["tds_fdw"], bool)


async def test_bootstrap_role_attributes(wh):
    await _boot(wh)
    r = wh["roles"]
    rows = await wh["sc"].fetch(
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
        "rolreplication, rolbypassrls, rolconnlimit FROM pg_roles WHERE rolname = ANY($1)",
        [r.owner, r.loader, r.reader])
    assert len(rows) == 3
    for row in rows:
        assert row["rolcanlogin"] is True
        for k in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolinherit",
                  "rolreplication", "rolbypassrls"):
            assert row[k] is False, (row["rolname"], k)
        # reader 20 (Phase 14); the Phase 15 sync loader is limited to 5 connections; owner unlimited
        assert row["rolconnlimit"] == {r.reader: 20, r.loader: 5}.get(row["rolname"], -1)


async def test_bootstrap_role_settings(wh):
    await _boot(wh)
    r = wh["roles"]

    async def settings(role):
        v = await wh["sc"].fetch(
            "SELECT unnest(setconfig) AS c FROM pg_db_role_setting s JOIN pg_roles r "
            "ON r.oid = s.setrole WHERE r.rolname = $1", role)
        return {x["c"] for x in v}

    rd = await settings(r.reader)
    for want in ("default_transaction_read_only=on", "statement_timeout=30s",
                 "idle_in_transaction_session_timeout=60s", "search_path=public"):
        assert want in rd, rd
    assert "lock_timeout=5s" in await settings(r.owner)


async def test_bootstrap_schemas_and_extensions(wh):
    await _boot(wh)
    sc, r = wh["sc"], wh["roles"]
    owners = {x["nspname"]: x["o"] for x in await sc.fetch(
        "SELECT nspname, pg_get_userbyid(nspowner) AS o FROM pg_namespace "
        "WHERE nspname IN ('ic_ext','ic_meta','ic_probe')")}
    assert owners["ic_meta"] == r.owner and owners["ic_probe"] == r.owner
    assert owners["ic_ext"] not in (r.owner, r.reader, r.loader)
    assert await sc.fetchval("SELECT has_schema_privilege($1,'ic_meta','USAGE')", r.reader)
    assert await sc.fetchval("SELECT has_schema_privilege($1,'ic_probe','USAGE')", r.reader)
    assert not await sc.fetchval("SELECT has_schema_privilege($1,'ic_ext','USAGE')", r.reader)
    ext = {x["extname"]: x["nspname"] for x in await sc.fetch(
        "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n "
        "ON n.oid = e.extnamespace")}
    assert ext["postgres_fdw"] == "ic_ext" and ext["pg_stat_statements"] == "ic_ext"
    assert ext.get("plpgsql") == "pg_catalog"
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public'") == 0


async def test_bootstrap_types_schema(wh):
    await _boot(wh)
    await _boot(wh)
    sc, r = wh["sc"], wh["roles"]
    assert await sc.fetchval(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'ic_types'") == r.owner
    assert await sc.fetchval("SELECT has_schema_privilege($1,'ic_types','USAGE')", r.reader)
    assert not await sc.fetchval("SELECT has_schema_privilege($1,'ic_types','CREATE')", r.reader)
    assert not await sc.fetchval("SELECT has_schema_privilege($1,'ic_types','USAGE')", r.loader)
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE n.nspname='ic_types' AND a.grantee=0") == 0
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE n.nspname = 'ic_types'") == 0


async def test_bootstrap_grants(wh):
    await _boot(wh)
    sc, r = wh["sc"], wh["roles"]
    db = wh["db"]
    q = sc.fetchval
    assert await q("SELECT has_database_privilege($1,$2,'CREATE')", r.owner, db)
    assert await q("SELECT has_database_privilege($1,$2,'TEMPORARY')", r.owner, db)
    assert not await q("SELECT has_database_privilege($1,$2,'CREATE')", r.reader, db)
    assert not await q("SELECT has_database_privilege($1,$2,'TEMPORARY')", r.reader, db)
    assert await q("SELECT has_foreign_data_wrapper_privilege($1,'postgres_fdw','USAGE')", r.owner)
    assert not await q("SELECT has_foreign_data_wrapper_privilege($1,'postgres_fdw','USAGE')", r.reader)
    # PUBLIC has nothing on the database or on schema public
    assert await q("SELECT count(*) FROM pg_database d, aclexplode(d.datacl) a "
                   "WHERE d.datname=$1 AND a.grantee=0", db) == 0
    assert await q("SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a "
                   "WHERE n.nspname='public' AND a.grantee=0") == 0
    assert await q("SELECT has_schema_privilege($1,'public','USAGE') AND "
                   "has_schema_privilege($1,'public','CREATE')", r.owner)
    assert await q("SELECT has_schema_privilege($1,'public','USAGE')", r.reader)
    assert not await q("SELECT has_schema_privilege($1,'public','CREATE')", r.reader)


async def test_bootstrap_reader_proof_and_passwords(wh):
    import asyncpg

    from app import warehouse
    await _boot(wh)
    r, pws = wh["roles"], wh["pws"]

    async def login(role, pw):
        return await asyncpg.connect(_with_db(wh["db"], role, pw))

    rd = await login(r.reader, pws["reader"])
    try:
        ok, detail = await warehouse.check_reader(rd, roles=r)
        assert ok is True, detail
    finally:
        await rd.close()
    ow = await login(r.owner, pws["owner"])
    try:
        ok, reason = await warehouse.check_reader(ow, roles=r)
        assert ok is False and reason
    finally:
        await ow.close()
    lo = await login(r.loader, pws["loader"])
    await lo.close()

    # a rotated password takes effect on the next bootstrap
    old = pws["reader"]
    wh["pws"]["reader"] = secrets.token_hex(24)
    await _boot(wh)
    with pytest.raises(asyncpg.InvalidPasswordError):
        await login(r.reader, old)
    rd = await login(r.reader, wh["pws"]["reader"])
    await rd.close()


# â”€â”€ Phase 14-04: linker owner-side plumbing (scratch source DB, reused by 14-05 / 14-06) â”€â”€
SRC_SETUP = """
CREATE TYPE public.mood AS ENUM ('ok', 'bad');
CREATE DOMAIN public.code_dom AS varchar(10);
CREATE TABLE public.regions (id serial PRIMARY KEY, name text NOT NULL);
COMMENT ON TABLE public.regions IS 'Sales regions';
COMMENT ON COLUMN public.regions.name IS 'Region name';
CREATE TABLE public.customers (id serial PRIMARY KEY,
  region_id int NOT NULL REFERENCES public.regions(id), name text NOT NULL,
  mood public.mood, code public.code_dom);
CREATE TABLE public.invoices (id serial PRIMARY KEY,
  customer_id int REFERENCES public.customers(id), amount numeric(12,2),
  issued date, CONSTRAINT amount_pos CHECK (amount >= 0));
CREATE VIEW public.v_customers AS SELECT id, name FROM public.customers;
CREATE TABLE public.events (id int, at timestamptz) PARTITION BY RANGE (id);
CREATE TABLE public.events_p1 PARTITION OF public.events FOR VALUES FROM (0) TO (1000);
CREATE SCHEMA reporting;
CREATE TABLE reporting.daily (day date PRIMARY KEY);
CREATE MATERIALIZED VIEW public.mv_granted AS SELECT id FROM public.regions;
CREATE MATERIALIZED VIEW public.mv_hidden AS SELECT id FROM public.regions;
CREATE TABLE public.api_tokens (id serial PRIMARY KEY, token text);
INSERT INTO public.regions(name) VALUES ('north'), ('south');
INSERT INTO public.customers(region_id, name) VALUES (1, 'a'), (2, 'b');
"""


@pytest.fixture
async def src(wh):
    """Scratch source DB in the same cluster with a Phase 11 role-kit reader."""
    import asyncpg

    from app import readonlykit as rk
    from app.whlink import LinkOptions
    tag = wh["tag"]
    db, role = f"ic_p14_src_{tag}", f"t14_{tag}_ro"
    pw = secrets.token_hex(24)
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{db}"')
    sdb = await asyncpg.connect(_with_db(db))
    try:
        await sdb.execute(SRC_SETUP)
        await sdb.execute(rk.render(role, ["public", "reporting"], ["public.api_tokens"],
                                    "30s", pw))
        await sdb.execute(f'REVOKE SELECT ON public.mv_hidden FROM "{role}"')
        await sdb.execute(f'ALTER DATABASE "{db}" SET timezone = \'Asia/Kolkata\'')
        await sdb.execute("ANALYZE")
        # trust auth fails for non-superuser mappings: reach the source by the
        # cluster's own network address and port, never localhost
        host = await sdb.fetchval("SELECT host(inet_server_addr())")
        port = await sdb.fetchval("SELECT inet_server_port()")
        opts = LinkOptions(host, str(port), db, role, pw)
        yield {"db": db, "role": role, "pw": pw, "opts": opts, "sdb": sdb}
    finally:
        await sdb.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


@pytest.fixture
async def linked(wh, src):
    """Bootstrapped warehouse plus owner and reader connections."""
    import asyncpg
    await _boot(wh)
    r, pws = wh["roles"], wh["pws"]
    ow = await asyncpg.connect(_with_db(wh["db"], r.owner, pws["owner"]))
    rd = await asyncpg.connect(_with_db(wh["db"], r.reader, pws["reader"]))
    try:
        yield {**wh, **src, "ow": ow, "rd": rd}
    finally:
        await ow.close()
        await rd.close()


async def test_meta(linked):
    import asyncpg

    from app import whlink
    ow, rd, sc, r = linked["ow"], linked["rd"], linked["sc"], linked["roles"]
    await whlink.ensure_meta(ow, r)
    await whlink.ensure_meta(ow, r)
    q = ("SELECT xmin::text FROM pg_class WHERE oid = '%s'::regclass")
    before = [await sc.fetchval(q % t) for t in ("ic_meta.source_catalog", "ic_meta.link_state")]
    await whlink.ensure_meta(ow, r)
    after = [await sc.fetchval(q % t) for t in ("ic_meta.source_catalog", "ic_meta.link_state")]
    assert before == after
    kinds = {x["relname"]: x["relkind"] for x in await sc.fetch(
        "SELECT relname, relkind::text AS relkind FROM pg_class WHERE relnamespace = 'ic_meta'::regnamespace")}
    for t in ("source_catalog", "link_objects", "link_state"):
        assert kinds[t] == "r"
    assert all(k == "r" or k == "i" for k in kinds.values())
    await rd.fetch("SELECT * FROM ic_meta.source_catalog")
    await rd.fetch("SELECT source, status, tables_linked, linked_at, source_timezone, "
                   "source_search_path FROM ic_meta.link_state")
    for bad in ("SELECT detail FROM ic_meta.link_state",
                "SELECT catalog_hash FROM ic_meta.link_state",
                "SELECT * FROM ic_meta.link_state",
                "SELECT * FROM ic_meta.link_objects"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await rd.fetch(bad)


def _opts_of(arr):
    return dict(x.split("=", 1) for x in arr or [])


async def test_server(linked):
    from dataclasses import replace

    from app import whlink
    ow, sc, r, o = linked["ow"], linked["sc"], linked["roles"], linked["opts"]
    await whlink.ensure_server(ow, "business_db", o)
    await whlink.ensure_server(ow, "business_db", o)
    srv = _opts_of(await sc.fetchval(
        "SELECT srvoptions FROM pg_foreign_server WHERE srvname = 'business_db'"))
    assert srv["host"] == o.host and srv["port"] == o.port and srv["dbname"] == o.dbname
    assert (srv["updatable"], srv["use_remote_estimate"], srv["fetch_size"],
            srv["connect_timeout"]) == ("false", "true", "1000", "10")
    maps = await sc.fetch(
        "SELECT pg_get_userbyid(umuser) AS u FROM pg_user_mapping WHERE umserver = "
        "(SELECT oid FROM pg_foreign_server WHERE srvname = 'business_db')")
    assert [m["u"] for m in maps] == [r.owner]

    sx = "SELECT xmin::text FROM pg_foreign_server WHERE srvname = 'business_db'"
    mx = ("SELECT oid::text || '/' || xmin::text || '/' || coalesce(umoptions::text,'') "
          "FROM pg_user_mapping WHERE umserver = "
          "(SELECT oid FROM pg_foreign_server WHERE srvname = 'business_db')")
    s1, m1 = await sc.fetchval(sx), await sc.fetchval(mx)
    await whlink.ensure_server(ow, "business_db", o)
    assert await sc.fetchval(sx) == s1 and await sc.fetchval(mx) == m1

    o2 = replace(o, port="5433", extra=(("sslmode", "prefer"),))
    await whlink.ensure_server(ow, "business_db", o2)
    srv = _opts_of(await sc.fetchval(
        "SELECT srvoptions FROM pg_foreign_server WHERE srvname = 'business_db'"))
    assert srv["port"] == "5433" and srv["sslmode"] == "prefer"
    await whlink.ensure_server(ow, "business_db", replace(o, port="5433"))
    srv = _opts_of(await sc.fetchval(
        "SELECT srvoptions FROM pg_foreign_server WHERE srvname = 'business_db'"))
    assert "sslmode" not in srv
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_user_mapping WHERE umserver = "
        "(SELECT oid FROM pg_foreign_server WHERE srvname = 'business_db')") == 1

    await whlink.ensure_server(ow, "business_db", o)
    m2 = await sc.fetchval(mx)
    await whlink.ensure_server(ow, "business_db", replace(o, password="changed-" + o.password))
    m3 = await sc.fetchval(mx)
    o_id, x_id = m2.split("/")[0], m3.split("/")[0]
    assert o_id == x_id and m2 != m3


async def test_credential(linked):
    import asyncpg

    from app import warehouse, whlink
    ow, rd, r = linked["ow"], linked["rd"], linked["roles"]
    await whlink.ensure_meta(ow, r)
    await whlink.ensure_server(ow, "business_db", linked["opts"])
    await whlink.ensure_catalog_tables(ow, "business_db", r)
    await whlink.ensure_catalog_tables(ow, "business_db", r)
    assert await rd.fetchval(
        "SELECT count(*) FROM pg_user_mappings WHERE umoptions IS NOT NULL") == 0
    for v in ("user_mapping_options", "user_mappings", "foreign_server_options"):
        assert await rd.fetchval(f"SELECT count(*) FROM information_schema.{v}") == 0
    for bad in ("SELECT * FROM pg_user_mapping",
                "SELECT ic_ext.postgres_fdw_get_connections()",
                f"SELECT * FROM {whlink.catalog_schema('business_db')}.r_class"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await rd.fetch(bad)
    assert await rd.fetchval("SELECT has_server_privilege('business_db','USAGE')") is False
    ok, detail = await warehouse.check_reader(rd, roles=r)
    assert ok is True, detail
    assert await rd.fetchval("SELECT n FROM ic_probe.business_db_ping") > 0

    # a second provisioning run changes nothing in the catalog
    sc = linked["sc"]
    q = ("SELECT oid::text || '/' || xmin::text FROM pg_class "
         "WHERE oid = 'ic_probe.business_db_ping'::regclass")
    before = await sc.fetchval(q)
    await whlink.ensure_catalog_tables(ow, "business_db", r)
    assert await sc.fetchval(q) == before


async def _prepare(linked):
    from app import whlink
    ow, r = linked["ow"], linked["roles"]
    await whlink.ensure_meta(ow, r)
    await whlink.ensure_server(ow, "business_db", linked["opts"])
    await whlink.ensure_catalog_tables(ow, "business_db", r)


async def test_snapshot(linked):
    import asyncpg

    from app import whlink
    from app.schemadiscovery import CATALOG_SQL
    await _prepare(linked)
    snap = await whlink.fetch_snapshot(linked["ow"], "business_db", linked["opts"])
    by = {(t["schema_name"], t["table_name"]): t for t in snap["tables"]}
    names = set(by)
    assert names == {("public", "regions"), ("public", "customers"), ("public", "invoices"),
                     ("public", "v_customers"), ("public", "events"), ("reporting", "daily"),
                     ("public", "mv_granted"), ("public", "mv_hidden")}
    assert ("public", "api_tokens") not in names and ("public", "events_p1") not in names
    for k in (("public", "mv_granted"), ("public", "mv_hidden")):
        assert by[k]["probe"] is True and by[k]["relkind"] == "m"

    cols = {c["name"]: c for c in by[("public", "customers")]["columns"]}
    assert whlink.fdw_type(cols["mood"]) == "enumn:public.mood"
    assert whlink.fdw_type(cols["code"]) == "character varying(10)"
    assert whlink.fdw_type(by[("public", "invoices")]["columns"][2]) == "numeric(12,2)"

    c = await asyncpg.connect(_with_db(linked["db"],
                                       linked["role"], linked["pw"]))
    try:
        v1 = {(r["schema_name"], r["table_name"]): r for r in await c.fetch(CATALOG_SQL)}
    finally:
        await c.close()
    import json
    for k, row in by.items():
        if row["relkind"] == "m":
            continue
        want = dict(v1[k])
        want["relkind"] = want["relkind"].decode() if isinstance(want["relkind"], bytes) else want["relkind"]
        assert row["relkind"] == want["relkind"], k
        assert row["table_comment"] == want["table_comment"], k
        assert row["row_estimate"] == want["row_estimate"], k
        assert row["checks"] == []
        got = whlink.v1_row(row)["columns"]
        assert got == json.loads(want["columns"]), k
    assert {k for k, r in by.items() if r["relkind"] != "m"} == {
        k for k in v1 if v1[k]["relkind"] not in ("m", b"m")}

    assert snap["timezone"] == "Asia/Kolkata"
    assert snap["search_path_raw"] == '"$user", public'


# ── Phase 14-05: link_source, drift, writes ──
def _url(o, password=None):
    return f"postgresql://{o.user}:{password or o.password}@{o.host}:{o.port}/{o.dbname}"


async def _link(linked, url=None):
    from app import whlink
    return await whlink.link_source(linked["ow"], "business_db",
                                    url or _url(linked["opts"]), linked["roles"])


async def _view_names(sc):
    return {(r["schemaname"], r["viewname"]) for r in await sc.fetch(
        "SELECT schemaname, viewname FROM pg_views WHERE viewowner LIKE 't14\\_%\\_owner' "
        "AND schemaname NOT LIKE 'ic\\_%'")}


async def test_link_ok(linked):
    await linked["sdb"].execute("ANALYZE")
    res = await _link(linked)
    assert res["status"] == "ok", res
    assert res["tables_linked"] == 7
    assert await _view_names(linked["sc"]) == {
        ("public", "regions"), ("public", "customers"), ("public", "invoices"),
        ("public", "v_customers"), ("public", "events"), ("reporting", "daily"),
        ("public", "mv_granted")}
    row = await linked["sc"].fetchrow(
        "SELECT status, tables_linked, source_timezone, source_search_path "
        "FROM ic_meta.link_state WHERE source = 'business_db'")
    assert (row["status"], row["tables_linked"], row["source_timezone"],
            row["source_search_path"]) == ("ok", 7, "Asia/Kolkata", "public")
    assert res["session"] == {"TimeZone": "Asia/Kolkata", "search_path": "public"}
    cat = {(r["schema_name"], r["table_name"]) for r in await linked["sc"].fetch(
        "SELECT schema_name, table_name FROM ic_meta.source_catalog")}
    assert ("public", "mv_hidden") not in cat and ("public", "api_tokens") not in cat
    assert len(cat) == 7


async def test_link_reads(linked):
    import asyncpg
    assert (await _link(linked))["status"] == "ok"
    rd, sdb = linked["rd"], linked["sdb"]
    want = await sdb.fetchval("SELECT count(*) FROM public.customers")
    assert await rd.fetchval("SELECT count(*) FROM customers") == want
    assert await rd.fetchval("SELECT count(*) FROM public.customers") == want
    await rd.fetch("SELECT * FROM reporting.daily")
    await sdb.execute("INSERT INTO public.customers(region_id, name, mood) VALUES (1,'m','ok')")
    got = await rd.fetch("SELECT name FROM customers WHERE mood = 'ok'")
    assert [r["name"] for r in got] == ["m"]
    priv = await linked["sc"].fetchval(
        "SELECT private_schema FROM ic_meta.link_objects WHERE remote_table = 'regions'")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await rd.fetch(f'SELECT * FROM "{priv}".regions')
    assert await rd.fetchval("SELECT count(*) FROM invoices") == await rd.fetchval(
        "SELECT count(*) FROM public.invoices")


async def test_write_refused(linked):
    import asyncpg

    from app.guardrails import validate_readonly_sql
    assert (await _link(linked))["status"] == "ok"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await linked["rd"].execute("INSERT INTO public.regions(name) VALUES ('x')")
    priv = await linked["sc"].fetchval(
        "SELECT private_schema FROM ic_meta.link_objects WHERE remote_table = 'regions'")
    with pytest.raises(asyncpg.PostgresError, match="does not allow inserts"):
        await linked["ow"].execute(f'INSERT INTO "{priv}".regions(id, name) VALUES (9, \'x\')')
    with pytest.raises(Exception):
        validate_readonly_sql("INSERT INTO public.regions(name) VALUES ('x')")
    assert await linked["sdb"].fetchval("SELECT count(*) FROM public.regions") == 2


async def _oids(sc):
    return {(r["nspname"], r["relname"]): (r["oid"], r["xmin"]) for r in await sc.fetch(
        "SELECT n.nspname, c.relname, c.oid::int AS oid, c.xmin::text AS xmin FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE (c.relkind IN ('v','f') AND (n.nspname IN ('public','reporting') "
        "OR n.nspname LIKE 'ic\\_fdw\\_%')) OR (n.nspname IN ('ic_meta','ic_probe') "
        "AND c.relkind IN ('r','v'))")}


async def test_drift(linked):
    sdb, sc, rd = linked["sdb"], linked["sc"], linked["rd"]
    role = linked["role"]
    assert (await _link(linked))["status"] == "ok"

    sx = ("SELECT s.xmin::text || '/' || m.xmin::text FROM pg_foreign_server s "
          "JOIN pg_user_mapping m ON m.umserver = s.oid WHERE s.srvname = 'business_db'")
    before, sx1 = await _oids(sc), await sc.fetchval(sx)
    res = await _link(linked)
    assert (res["created"], res["replaced"], res["dropped"], res["catalog_updated"]) == (0, 0, 0, 0)
    after = await _oids(sc)
    assert {k: v for k, v in after.items() if k != ("ic_meta", "link_state")} == \
        {k: v for k, v in before.items() if k != ("ic_meta", "link_state")}
    assert await sc.fetchval(sx) == sx1

    await sdb.execute("ALTER TABLE public.regions ADD COLUMN code text")
    res = await _link(linked)
    assert res["replaced"] == 1 and res["created"] == 0 and res["dropped"] == 0, res
    assert "code" in (await rd.fetchrow("SELECT * FROM regions LIMIT 1")).keys()

    await sdb.execute("CREATE TABLE public.notes (id int)")
    await sdb.execute(f'GRANT SELECT ON public.notes TO "{role}"')
    await sdb.execute("ANALYZE")
    res = await _link(linked)
    assert res["created"] == 1 and res["tables_linked"] == 8, res
    await rd.fetch("SELECT * FROM notes")

    await sdb.execute("DROP TABLE public.notes")
    res = await _link(linked)
    assert res["dropped"] == 1 and res["tables_linked"] == 7, res

    await sdb.execute(f'REVOKE SELECT ON public.invoices FROM "{role}"')
    res = await _link(linked)
    assert res["dropped"] == 1, res
    assert ("public", "invoices") not in await _view_names(sc)
    await sdb.execute(f'GRANT SELECT ON public.invoices TO "{role}"')
    assert (await _link(linked))["created"] == 1

    await sdb.execute(f'GRANT SELECT ON public.mv_hidden TO "{role}"')
    res = await _link(linked)
    assert res["created"] == 1
    await sdb.execute(f'REVOKE SELECT ON public.mv_hidden FROM "{role}"')
    res = await _link(linked)
    assert res["dropped"] == 1 and ("public", "mv_hidden") not in await _view_names(sc)


async def test_drift_catalog(linked):
    import asyncpg

    from app import schemadiscovery as sd
    sdb, sc, role = linked["sdb"], linked["sc"], linked["role"]
    await sdb.execute("CREATE TABLE public.drift_c (id int PRIMARY KEY, region_id int, label text)")
    await sdb.execute(f'GRANT SELECT ON public.drift_c TO "{role}"')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    oid_q = ("SELECT c.oid::int::text || '/' || c.xmin::text AS k FROM pg_class c "
             "WHERE c.relname = 'drift_c' ORDER BY c.relkind")
    sig_before = [r["k"] for r in await sc.fetch(oid_q)]
    assert len(sig_before) == 2
    await sdb.execute(
        "ALTER TABLE public.drift_c ALTER COLUMN label SET NOT NULL, "
        "ADD CONSTRAINT drift_c_region_fk FOREIGN KEY (region_id) REFERENCES public.regions(id), "
        "DROP CONSTRAINT drift_c_pkey")
    await sdb.execute("COMMENT ON COLUMN public.drift_c.label IS 'changed label'")
    await sdb.execute("COMMENT ON TABLE public.drift_c IS 'changed table'")
    res = await _link(linked)
    assert (res["created"], res["replaced"], res["dropped"], res["catalog_updated"]) == (0, 0, 0, 1), res
    assert [r["k"] for r in await sc.fetch(oid_q)] == sig_before

    import json
    row = await sc.fetchrow("SELECT * FROM ic_meta.source_catalog WHERE table_name = 'drift_c'")
    cols = {c["name"]: c for c in json.loads(row["columns"])}
    assert cols["label"]["nullable"] is False and cols["label"]["comment"] == "changed label"
    assert cols["id"]["pk"] is False and cols["region_id"]["fk"]
    assert row["table_comment"] == "changed table"

    c = await asyncpg.connect(_with_db(linked["db"], linked["role"], linked["pw"]))
    try:
        v1 = next(r for r in await c.fetch(sd.CATALOG_SQL) if r["table_name"] == "drift_c")
    finally:
        await c.close()
    v1cols = {c_["name"]: c_ for c_ in json.loads(v1["columns"])}
    assert cols["region_id"]["fk"] == v1cols["region_id"]["fk"]
    for n in ("id", "region_id", "label"):
        mine = sd.signature(sd.structure_of(cols[n], "r"))
        theirs = sd.signature(sd.structure_of(v1cols[n], "r"))
        assert mine == theirs, n

    await sdb.execute("ALTER TABLE public.drift_c ALTER COLUMN label DROP NOT NULL")
    res = await _link(linked)
    assert res["catalog_updated"] == 1 and res["replaced"] == 0
    row = await sc.fetchrow("SELECT columns FROM ic_meta.source_catalog WHERE table_name = 'drift_c'")
    assert {c["name"]: c for c in json.loads(row["columns"])}["label"]["nullable"] is True

    await sdb.execute("DROP TABLE public.drift_c")
    res = await _link(linked)
    assert res["dropped"] == 1
    assert await sc.fetchval(
        "SELECT count(*) FROM ic_meta.source_catalog WHERE table_name = 'drift_c'") == 0


async def test_drift_stale(linked):
    sdb, sc, rd, ow = linked["sdb"], linked["sc"], linked["rd"], linked["ow"]
    assert (await _link(linked))["status"] == "ok"
    await ow.execute("CREATE VIEW public.model_regions AS SELECT * FROM public.regions")
    await sdb.execute("ALTER TABLE public.regions ADD COLUMN code text")
    res = await _link(linked)
    assert res["status"] == "stale" and res["stale"] == ["public.regions"], res
    assert res["replaced"] == 0
    assert await rd.fetchval("SELECT count(*) FROM regions") == 2
    assert await sc.fetchval("SELECT status FROM ic_meta.link_state") == "stale"


async def test_link_second_source(linked):
    import asyncpg

    from app.whlink import LinkOptions
    assert (await _link(linked))["status"] == "ok"
    tag = linked["tag"]
    db2, role2, pw2 = f"ic_p14_src2_{tag}", f"t14_{tag}_ro2", secrets.token_hex(12)
    admin = await asyncpg.connect(_URL)
    await admin.execute(f'CREATE DATABASE "{db2}"')
    s2 = await asyncpg.connect(_with_db(db2))
    try:
        await s2.execute("CREATE TABLE public.widgets (id int PRIMARY KEY, n text); "
                         f"CREATE ROLE \"{role2}\" LOGIN PASSWORD '{pw2}'; "
                         f"GRANT USAGE ON SCHEMA public TO \"{role2}\"; "
                         f"GRANT SELECT ON public.widgets TO \"{role2}\"")
        o = linked["opts"]
        res = await _link(linked, _url(LinkOptions(o.host, o.port, db2, role2, pw2)))
        assert res["status"] == "ok" and res["tables_linked"] == 1, res
        assert await linked["rd"].fetchval("SELECT count(*) FROM widgets") == 0
        assert await linked["sc"].fetchval(
            "SELECT count(*) FROM pg_user_mapping WHERE umserver = "
            "(SELECT oid FROM pg_foreign_server WHERE srvname='business_db')") == 1
        assert await _view_names(linked["sc"]) == {("public", "widgets")}
    finally:
        await s2.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db2}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role2}"')
        await admin.close()


async def test_link_wrong_password(linked):
    import asyncpg
    assert (await _link(linked))["status"] == "ok"
    bad = "wrongpw-" + secrets.token_hex(6)
    res = await _link(linked, _url(linked["opts"], bad))
    assert res["status"] == "error", res
    assert bad not in res["detail"] and linked["pw"] not in res["detail"]
    det = await linked["sc"].fetchval("SELECT detail FROM ic_meta.link_state")
    assert det and bad not in det and linked["pw"] not in det
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await linked["rd"].fetchval("SELECT detail FROM ic_meta.link_state")
    assert await linked["rd"].fetchval("SELECT status FROM ic_meta.link_state") == "error"


# ── Phase 14-06: discovery through the warehouse ──
async def _v1_and_wh(linked):
    import asyncpg

    from app import schemadiscovery as sd
    c = await asyncpg.connect(_with_db(linked["db"], linked["role"], linked["pw"]))
    try:
        v1 = await c.fetch(sd.CATALOG_SQL)
    finally:
        await c.close()
    wh = await linked["rd"].fetch(sd.WAREHOUSE_CATALOG_SQL)
    return ({(r["schema_name"], r["table_name"]): r for r in v1},
            {(r["schema_name"], r["table_name"]): r for r in wh})


def _cols(r):
    import json
    v = r["columns"]
    return json.loads(v) if isinstance(v, str) else v


def _assert_table_equal(a, b):
    from app import schemadiscovery as sd
    ka = a["relkind"].decode() if isinstance(a["relkind"], bytes) else a["relkind"]
    kb = b["relkind"].decode() if isinstance(b["relkind"], bytes) else b["relkind"]
    assert ka == kb
    assert sd._KIND[ka] == sd._KIND[kb]
    assert a["table_comment"] == b["table_comment"]
    assert a["row_estimate"] == b["row_estimate"]
    ca, cb = _cols(a), _cols(b)
    assert ca == cb
    for x, y in zip(ca, cb):
        assert sd.signature(sd.structure_of(x, "t")) == sd.signature(sd.structure_of(y, "t"))


async def test_discovery_parity(linked):
    from app import schemadiscovery as sd
    await linked["sdb"].execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    v1, wh = await _v1_and_wh(linked)
    assert set(v1) == set(wh) and len(wh) == 7
    for k in v1:
        _assert_table_equal(v1[k], wh[k])
        import json
        ck = wh[k]["checks"]
        assert (json.loads(ck) if isinstance(ck, str) else ck) in ([], None)
    rows = await linked["rd"].fetch('SELECT * FROM "public"."regions" LIMIT 3')
    assert len(rows) >= 1
    assert sd.WAREHOUSE_LINK_STATUS_SQL


async def test_discovery_after_drift(linked):
    sdb, role = linked["sdb"], linked["role"]
    await sdb.execute("CREATE TABLE public.disc_d (id int PRIMARY KEY, region_id int, label text)")
    await sdb.execute(f'GRANT SELECT ON public.disc_d TO "{role}"')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "disc_d")], wh[("public", "disc_d")])
    await sdb.execute(
        "ALTER TABLE public.disc_d ALTER COLUMN label SET NOT NULL, "
        "ADD CONSTRAINT disc_d_fk FOREIGN KEY (region_id) REFERENCES public.regions(id), "
        "DROP CONSTRAINT disc_d_pkey")
    await sdb.execute("COMMENT ON COLUMN public.disc_d.label IS 'new comment'")
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "disc_d")], wh[("public", "disc_d")])
    await sdb.execute("DROP TABLE public.disc_d")
    assert (await _link(linked))["status"] == "ok"
    _, wh = await _v1_and_wh(linked)
    assert ("public", "disc_d") not in wh


async def test_discovery_link_status(linked):
    from app import schemadiscovery as sd
    assert (await _link(linked))["status"] == "ok"
    bad = "wrongpw-" + secrets.token_hex(6)
    assert (await _link(linked, _url(linked["opts"], bad)))["status"] == "error"
    rows = await linked["rd"].fetch(sd.WAREHOUSE_LINK_STATUS_SQL)
    assert [r["status"] for r in rows] == ["error"]
    assert (await _link(linked))["status"] == "ok"
    rows = await linked["rd"].fetch(sd.WAREHOUSE_LINK_STATUS_SQL)
    assert [r["status"] for r in rows] == ["ok"]


# ── Phase 14-08: backups never carry credentials (D-22) ──
def _docker(container, *args, data=None):
    import subprocess
    return subprocess.run(["docker", "exec", "-i", container, *args], input=data,
                          capture_output=True, check=True).stdout


async def test_backup_dump_has_no_credentials(linked):
    import subprocess
    container = os.environ.get("WAREHOUSE_TEST_PG_CONTAINER", "ic-p14-postgres-1")
    if subprocess.run(["docker", "inspect", container], capture_output=True).returncode != 0:
        pytest.skip(f"container {container} not found")
    su = urlsplit(_URL).username or "postgres"
    pw = linked["pw"]
    assert len(pw) == 48
    assert (await _link(linked))["status"] == "ok"
    db = f"ic_p14_wh_{linked['tag']}"  # linked["db"] is the source database
    dump = _docker(container, "pg_dump", "-U", su, "-d", db, "-Fc", "-n", "ic_meta", "-n", "model")
    text = _docker(container, "pg_restore", "-f", "-", data=dump).decode("utf-8", "replace")
    assert "source_catalog" in text  # positive control
    assert pw not in text
    assert "create server" not in text.lower()
    assert "user mapping" not in text.lower()
    # negative control: a full dump does carry the mapping password (spike S8)
    full = _docker(container, "pg_dump", "-U", su, "-d", db).decode("utf-8", "replace")
    assert pw in full


# ── Phase 14 gap closure (14-10, WR-04): user-type naming parity with format_type ──
async def test_discovery_parity_user_types(linked):
    sdb, role, db = linked["sdb"], linked["role"], linked["db"]
    has_vec = bool(await sdb.fetchval(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"))
    await sdb.execute("""
CREATE SCHEMA sales; CREATE SCHEMA hidden_types; CREATE SCHEMA nousage;
CREATE TYPE public."OrderStatus" AS ENUM ('new', 'paid');
CREATE TYPE sales.region_kind AS ENUM ('n', 's');
CREATE TYPE hidden_types.flag AS ENUM ('y', 'n');
CREATE TYPE nousage.tier AS ENUM ('a', 'b');
CREATE TYPE public."order" AS ENUM ('x');
CREATE TYPE public.status AS ENUM ('p1');
CREATE TYPE sales.status AS ENUM ('s1');
CREATE DOMAIN public."Email" AS text;
""")
    if has_vec:
        await sdb.execute("CREATE EXTENSION IF NOT EXISTS vector")
    vec = ", v vector(3)" if has_vec else ""
    await sdb.execute(
        'CREATE TABLE public.ut (id int PRIMARY KEY, st "OrderStatus", sts "OrderStatus"[], '
        'rk sales.region_kind, fl hidden_types.flag, ti nousage.tier, kw "order", '
        f's1 sales.status, s2 public.status, em "Email"{vec})')
    ins = "INSERT INTO public.ut(id) VALUES (1), (2)"
    await sdb.execute(ins)
    await sdb.execute(f'ALTER ROLE "{role}" IN DATABASE "{db}" SET search_path = public, sales, nousage')
    await sdb.execute(f'GRANT USAGE ON SCHEMA sales TO "{role}"')
    await sdb.execute(f'GRANT SELECT ON public.ut TO "{role}"')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "ut")], wh[("public", "ut")])
    got = [c["type"] for c in _cols(wh[("public", "ut")])]
    want = ['integer', '"OrderStatus"', '"OrderStatus"[]', 'region_kind', 'hidden_types.flag',
            'nousage.tier', '"order"', 'sales.status', 'status', '"Email"']
    if has_vec:
        want.append("vector(3)")
    assert got == want
    assert await linked["rd"].fetchval("SELECT count(*) FROM public.ut") == 2


# ── Phase 14 gap closure (14-11, CR-01 residual): enum arrays, domains, refused types ──
async def test_link_types_enum_array_domain_refused(linked):
    import asyncpg

    sdb, role = linked["sdb"], linked["role"]
    has_vec = bool(await sdb.fetchval(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"))
    await sdb.execute("""
CREATE DOMAIN public.dom_mood AS public.mood;
CREATE DOMAIN public.pos_int AS int;
CREATE DOMAIN public.small_pos AS public.pos_int;
CREATE TYPE public.pair AS (a int, b int);
""")
    vec = ""
    if has_vec:
        await sdb.execute("CREATE EXTENSION IF NOT EXISTS vector")
        vec = ", v vector(3)"
    await sdb.execute(
        "CREATE TABLE public.tt (id int PRIMARY KEY, moods public.mood[], dm public.dom_mood, "
        f"dms public.dom_mood[], di public.small_pos, cp public.pair{vec})")
    vals = [("1", "'{ok}'", "'ok'", "'{ok,bad}'", "7", "'(1,2)'"),
            ("2", "'{bad}'", "'bad'", "'{bad}'", "3", "'(3,4)'"),
            ("3", "'{ok,bad}'", "NULL", "NULL", "9", "NULL")]
    for i, v in enumerate(vals):
        extra = f", '[{i},1,2]'" if has_vec else ""
        await sdb.execute(f"INSERT INTO public.tt VALUES ({', '.join(v)}{extra})")
    await sdb.execute(f'GRANT SELECT ON public.tt TO "{role}"')
    await sdb.execute("ANALYZE")
    res = await _link(linked)
    assert res["status"] == "ok" and not res.get("stale"), res
    rd = linked["rd"]
    await rd.execute("SELECT set_config('search_path', $1, false)", res["session"]["search_path"])

    from app import whlink
    qs = ["SELECT id FROM tt WHERE 'ok' = ANY(moods) ORDER BY id",
          "SELECT id FROM tt WHERE moods @> '{ok}' ORDER BY id",
          "SELECT id FROM tt WHERE dm::text IN ('ok','bad') ORDER BY id",
          "SELECT id FROM tt WHERE 'bad' = ANY(dms::text[]) ORDER BY id",
          "SELECT count(*) FILTER (WHERE 'bad' = ANY(moods)) FROM tt",
          "SELECT id FROM tt WHERE di > 5 ORDER BY id",
          "SELECT id, dm::text FROM tt ORDER BY dm, id"]
    src = await asyncpg.connect(_with_db(linked["db"], role, linked["pw"]))
    try:
        for q in qs:
            a = sorted(tuple(r.values()) for r in await src.fetch(q))
            b = sorted(tuple(r.values()) for r in await rd.fetch(q))
            assert a == b, q
    finally:
        await src.close()
    with pytest.raises(asyncpg.UndefinedColumnError):
        await rd.fetch("SELECT cp FROM tt")
    if has_vec:
        with pytest.raises(asyncpg.UndefinedColumnError):
            await rd.fetch("SELECT v FROM tt")
    sc = linked["sc"]
    cols = [r["attname"] for r in await sc.fetch(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'public.tt'::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum")]
    assert cols == ["id", "moods", "dm", "dms", "di"]
    import json
    unl = json.loads(await sc.fetchval(
        "SELECT unlinked_columns::text FROM ic_meta.link_objects WHERE remote_schema = 'public' "
        "AND remote_table = 'tt'"))
    want = [{"column": "cp", "type": "pair", "reason": "composite type is not linked"}]
    if has_vec:
        want.append({"column": "v", "type": "vector(3)",
                     "reason": "extension type has no local equivalent: vector"})
    assert unl == want
    assert json.loads(await sc.fetchval(
        "SELECT unlinked_columns::text FROM ic_meta.link_objects WHERE remote_table = 'customers'"
    )) == []
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "tt")], wh[("public", "tt")])
    again = await whlink.apply_changes(linked["ow"], "business_db", await _snapshot_of(linked),
                                       linked["roles"])
    assert (again["created"], again["replaced"], again["dropped"]) == (0, 0, 0)


async def _snapshot_of(linked):
    from app import whlink
    return await whlink.fetch_snapshot(linked["ow"], "business_db", linked["opts"])


# ── Phase 14 gap closure (14-12): citext / hstore parity ──
async def test_link_types_extension(linked):
    import asyncpg

    sdb, role, db = linked["sdb"], linked["role"], linked["db"]
    await sdb.execute("""
CREATE SCHEMA exts;
CREATE EXTENSION citext SCHEMA exts;
CREATE EXTENSION hstore SCHEMA exts;
CREATE TABLE public.ct (id int PRIMARY KEY, email exts.citext, emails exts.citext[], tags exts.hstore);
INSERT INTO public.ct VALUES (1, 'A@x.com', '{A@x.com,b@y.com}', 'k=>v'),
  (2, 'b@X.com', '{c@z.com}', 'k=>w'), (3, 'c@z.com', NULL, NULL), (4, 'a@X.COM', NULL, NULL);
""")
    await sdb.execute(f'ALTER ROLE "{role}" IN DATABASE "{db}" SET search_path = public, exts')
    await sdb.execute(f'GRANT USAGE ON SCHEMA exts TO "{role}"')
    await sdb.execute(f'GRANT SELECT ON public.ct TO "{role}"')
    await sdb.execute("ANALYZE")

    async def both(q, rd, src, ordered=False):
        a = [tuple(r.values()) for r in await src.fetch(q)]
        b = [tuple(r.values()) for r in await rd.fetch(q)]
        if not ordered:
            a, b = sorted(a), sorted(b)
        assert a == b, q
        return a

    # Part A: the source role sees the extensions
    res = await _link(linked)
    assert res["status"] == "ok" and not res.get("stale"), res
    assert res["session"]["search_path"].endswith("ic_types"), res["session"]
    sc, rd = linked["sc"], linked["rd"]
    exts = {r["extname"]: r["nspname"] for r in await sc.fetch(
        "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n "
        "ON n.oid = e.extnamespace")}
    assert exts["citext"] == "ic_types" and exts["hstore"] == "ic_types"
    await rd.execute("SELECT set_config('search_path', $1, false)", res["session"]["search_path"])
    src = await asyncpg.connect(_with_db(db, role, linked["pw"]))
    try:
        assert await both("SELECT id FROM ct WHERE email = 'a@x.com' ORDER BY id", rd, src) == [(1,), (4,)]
        assert await both("SELECT id FROM ct WHERE email IN ('a@x.com','B@X.COM') ORDER BY id",
                          rd, src) == [(1,), (2,), (4,)]
        await both("SELECT id FROM ct WHERE 'B@Y.COM' = ANY(emails) ORDER BY id", rd, src)
        await both("SELECT id FROM ct WHERE emails @> '{C@Z.COM}' ORDER BY id", rd, src)
        await both("SELECT id FROM ct WHERE tags -> 'k' = 'v' ORDER BY id", rd, src)
        await both("SELECT id FROM ct WHERE tags ? 'k' ORDER BY id", rd, src)
        assert await both("SELECT count(DISTINCT email) FROM ct", rd, src) == [(3,)]
        await both("SELECT id FROM ct ORDER BY email, id", rd, src, ordered=True)
        await both("SELECT lower(email::text), count(*) FROM ct GROUP BY 1 ORDER BY 1", rd, src)
    finally:
        await src.close()
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "ct")], wh[("public", "ct")])
    import json
    assert json.loads(await sc.fetchval(
        "SELECT unlinked_columns::text FROM ic_meta.link_objects WHERE remote_table = 'ct'")) == []
    again = await _link(linked)
    assert (again["created"], again["replaced"], again["dropped"]) == (0, 0, 0)

    # Part B: the source role loses sight of the extensions
    await sdb.execute(f'REVOKE USAGE ON SCHEMA exts FROM "{role}"')
    res = await _link(linked)
    assert res["status"] == "ok", res
    assert res["replaced"] >= 1, res
    assert "ic_types" not in res["session"]["search_path"]
    await rd.execute("SELECT set_config('search_path', $1, false)", res["session"]["search_path"])
    src = await asyncpg.connect(_with_db(db, role, linked["pw"]))
    try:
        assert await both("SELECT id FROM ct WHERE email = 'a@x.com' ORDER BY id", rd, src) == []
        assert await both("SELECT id FROM ct WHERE email = 'A@x.com' ORDER BY id", rd, src) == [(1,)]
        assert await both("SELECT count(DISTINCT email) FROM ct", rd, src) == [(3,)]
        await both("SELECT id FROM ct ORDER BY email, id", rd, src, ordered=True)
        assert await both("SELECT id FROM ct WHERE tags::text = '\"k\"=>\"v\"' ORDER BY id",
                          rd, src) == [(1,)]
    finally:
        await src.close()
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "ct")], wh[("public", "ct")])


# ── Phase 15 (Phase 14 gap A): user types with their own typmod output function ──
async def _make_typmod_type(sdb, name, typmod_out_fn, in_fn="timestamptztypmodin"):
    await sdb.execute(f"""
CREATE TYPE public.{name};
CREATE FUNCTION public.{name}_in(cstring, oid, integer) RETURNS public.{name}
  AS 'timestamptz_in' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION public.{name}_out(public.{name}) RETURNS cstring
  AS 'timestamptz_out' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION public.{name}_recv(internal, oid, integer) RETURNS public.{name}
  AS 'timestamptz_recv' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION public.{name}_send(public.{name}) RETURNS bytea
  AS 'timestamptz_send' LANGUAGE internal IMMUTABLE STRICT;
CREATE TYPE public.{name} (INPUT = public.{name}_in, OUTPUT = public.{name}_out,
  RECEIVE = public.{name}_recv, SEND = public.{name}_send,
  TYPMOD_IN = {in_fn}, TYPMOD_OUT = {typmod_out_fn},
  INTERNALLENGTH = 8, PASSEDBYVALUE, ALIGNMENT = double);
""")


async def test_discovery_parity_typmodout(linked):
    sdb, role = linked["sdb"], linked["role"]
    await _make_typmod_type(sdb, "tstz2", "timestamptztypmodout")
    await sdb.execute(
        "CREATE TABLE public.tm (id int PRIMARY KEY, t tstz2(3), ta tstz2(3)[])")
    await sdb.execute(f'GRANT SELECT ON public.tm TO "{role}"')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    v1, wh = await _v1_and_wh(linked)
    _assert_table_equal(v1[("public", "tm")], wh[("public", "tm")])
    got = [c["type"] for c in _cols(wh[("public", "tm")])]
    assert got == ["integer", "tstz2(3) with time zone", "tstz2(3) with time zone[]"]
    assert [c["type"] for c in _cols(v1[("public", "tm")])] == got


async def test_link_typmodout_unsupported_recorded(linked):
    sdb, role = linked["sdb"], linked["role"]
    await sdb.execute("""
CREATE FUNCTION public.my_typmod_out(integer) RETURNS cstring
  AS 'varchartypmodout' LANGUAGE internal IMMUTABLE STRICT;
""")
    await _make_typmod_type(sdb, "tstz3", "public.my_typmod_out", "varchartypmodin")
    await sdb.execute("CREATE TABLE public.tm2 (id int PRIMARY KEY, u tstz3(10))")
    await sdb.execute(f'GRANT SELECT ON public.tm2 TO "{role}"')
    await sdb.execute("ANALYZE")
    r = await _link(linked)
    assert r["status"] == "ok"
    assert {"schema": "public", "table": "tm2", "column": "u",
            "function": "public.my_typmod_out"} in r["unsupported_typmod"]
    _, wh = await _v1_and_wh(linked)
    assert [c["type"] for c in _cols(wh[("public", "tm2")])] == ["integer", "tstz3(14)"]  # raw atttypmod (varchartypmodin adds 4); v1.0 prints tstz3(10)
    r2 = await _link(linked)
    assert (r2["created"], r2["replaced"], r2["dropped"], r2["catalog_updated"]) == (0, 0, 0, 0)


# ── Phase 15 (Phase 14 gap B): source enum and domain names through the warehouse ──
_TN_SETUP = """
CREATE TYPE public."OrderStatus" AS ENUM ('new', 'paid', 'void');
CREATE TYPE public.order_state AS ENUM ('open', 'closed');
CREATE DOMAIN public."Email" AS text;
CREATE TABLE public.o (id int PRIMARY KEY, st "OrderStatus", sts "OrderStatus"[],
  os public.order_state, m public.mood, em "Email");
INSERT INTO public.o VALUES
  (1, 'paid', '{paid,new}', 'open', 'ok', 'a@b.c'),
  (2, 'new', '{new}', 'closed', 'bad', 'x@y.z'),
  (3, 'void', '{void,paid}', 'open', NULL, NULL);
"""

_TN_QUERIES = [
    "SELECT id FROM o WHERE st = 'paid'::\"OrderStatus\" ORDER BY id",
    "SELECT id FROM o WHERE st = ANY(ARRAY['paid','void']::\"OrderStatus\"[]) ORDER BY id",
    "SELECT id FROM o WHERE os = ANY('{open}'::order_state[]) ORDER BY id",
    "SELECT id FROM o WHERE m = 'ok'::public.mood ORDER BY id",
    "SELECT enum_range(NULL::order_state)::text",
    "SELECT id FROM o WHERE sts @> ARRAY['paid']::\"OrderStatus\"[] ORDER BY id",
    "SELECT id FROM o WHERE em = 'a@b.c'::\"Email\" ORDER BY id",
]


async def _tn_source(linked, extra=""):
    sdb, role = linked["sdb"], linked["role"]
    await sdb.execute(_TN_SETUP + extra)
    await sdb.execute(f'GRANT SELECT ON public.o TO "{role}"')
    await sdb.execute("ANALYZE")


async def _tn_run(linked, sql):
    """Rows (or the error class name) for sql directly on the source as the kit role and
    through the warehouse as the reader on the recorded session path, read-only."""
    import asyncpg
    sp = await linked["sc"].fetchval(
        "SELECT source_search_path FROM ic_meta.link_state WHERE source = 'business_db'")
    src = await asyncpg.connect(_with_db(linked["db"], linked["role"], linked["pw"]))
    rd = linked["rd"]
    out = []
    try:
        for conn, warehouse in ((src, False), (rd, True)):
            try:
                async with conn.transaction(readonly=True):
                    if warehouse:
                        await conn.execute("SELECT set_config('search_path', $1, true)", sp)
                    out.append([tuple(r.values()) for r in await conn.fetch(sql)])
            except asyncpg.PostgresError as e:
                out.append(type(e).__name__)
    finally:
        await src.close()
    return out[0], out[1]


async def _tn_assert_parity(linked, queries=None):
    for q in queries or _TN_QUERIES:
        a, b = await _tn_run(linked, q)
        assert not isinstance(a, str), (q, a)
        assert a == b, (q, a, b)


async def test_type_names_parity(linked):
    await _tn_source(linked)
    res = await _link(linked)
    assert res["status"] == "ok" and not res["stale"], res
    assert res["types"]["created"] >= 4 and not res["types"]["unsupported"], res["types"]
    await _tn_assert_parity(linked)
    sc, r = linked["sc"], linked["roles"]
    rows = {(x["schema_name"], x["type_name"]): x["kind"] for x in await sc.fetch(
        "SELECT schema_name, type_name, kind FROM ic_meta.link_types "
        "WHERE source = 'business_db'")}
    assert rows[("public", "OrderStatus")] == "enum" and rows[("public", "Email")] == "domain"
    assert rows[("public", "order_state")] == "enum" and rows[("public", "mood")] == "enum"
    owners = {x["typname"]: x["o"] for x in await sc.fetch(
        "SELECT typname, pg_get_userbyid(typowner) AS o FROM pg_type WHERE typnamespace = "
        "'public'::regnamespace AND typtype IN ('e','d')")}
    assert set(owners.values()) == {r.owner}
    assert await sc.fetchval("SELECT has_schema_privilege($1,'public','USAGE')", r.reader)
    assert not await sc.fetchval("SELECT has_schema_privilege($1,'public','CREATE')", r.reader)
    labels = await sc.fetchval(
        "SELECT array_agg(enumlabel ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid = "
        "'public.\"OrderStatus\"'::regtype")
    assert labels == ["new", "paid", "void"]
    import asyncpg
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await linked["rd"].fetch("SELECT * FROM ic_meta.link_types")
    from app import whlink
    assert await whlink._meta_grants_ok(linked["ow"], r.reader)


async def test_type_names_label_changes(linked):
    sdb, sc = linked["sdb"], linked["sc"]
    await _tn_source(linked)
    assert (await _link(linked))["status"] == "ok"
    await sdb.execute("ALTER TYPE public.\"OrderStatus\" ADD VALUE 'refunded'")
    await sdb.execute("INSERT INTO public.o(id, st) VALUES (4, 'refunded')")
    res = await _link(linked)
    assert res["status"] == "ok" and res["types"]["replaced"] == 1, res
    await _tn_assert_parity(linked, _TN_QUERIES + [
        "SELECT id FROM o WHERE st = 'refunded'::\"OrderStatus\" ORDER BY id"])
    await sdb.execute("ALTER TYPE public.order_state RENAME VALUE 'open' TO 'opened'")
    res = await _link(linked)
    assert res["status"] == "ok" and res["types"]["replaced"] == 1, res
    queries = [q.replace("{open}", "{opened}") for q in _TN_QUERIES]
    await _tn_assert_parity(linked, queries)

    async def state():
        return (
            [tuple(x) for x in await sc.fetch(
                "SELECT type_name, def_hash FROM ic_meta.link_types ORDER BY 1")],
            [x["o"] for x in await sc.fetch(
                "SELECT oid::int AS o FROM pg_type WHERE typname IN "
                "('OrderStatus','order_state','mood','Email') ORDER BY 1")],
            [x["o"] for x in await sc.fetch(
                "SELECT c.oid::int AS o FROM pg_class c WHERE c.relkind = 'f' ORDER BY 1")])
    before = await state()
    res = await _link(linked)
    assert (res["created"], res["replaced"], res["dropped"]) == (0, 0, 0)
    t = res["types"]
    assert (t["created"], t["replaced"], t["dropped"]) == (0, 0, 0)
    assert before == await state()


async def test_type_names_visibility(linked):
    sdb, role, db = linked["sdb"], linked["role"], linked["db"]
    await sdb.execute("""
CREATE SCHEMA other; CREATE TYPE other.kind_x AS ENUM ('x1');
CREATE TYPE public.status AS ENUM ('p1'); CREATE TYPE other.status AS ENUM ('s1');
CREATE TABLE other.tx (id int PRIMARY KEY, k other.kind_x, s other.status);
INSERT INTO other.tx VALUES (1, 'x1', 's1');""")
    await sdb.execute(f'GRANT USAGE ON SCHEMA other TO "{role}"')
    await sdb.execute(f'GRANT SELECT ON other.tx TO "{role}"')
    await sdb.execute(f'ALTER ROLE "{role}" IN DATABASE "{db}" SET search_path = public')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    # not on the source role's path: fails unqualified in both, works qualified in both
    a, b = await _tn_run(linked, "SELECT 'x1'::kind_x::text")
    assert a == b == "UndefinedObjectError"
    a, b = await _tn_run(linked, "SELECT 'x1'::other.kind_x::text")
    assert a == b == [("x1",)]
    a, b = await _tn_run(linked, "SELECT 'p1'::status::text")
    assert a == b == [("p1",)]
    sp = await linked["sc"].fetchval(
        "SELECT source_search_path FROM ic_meta.link_state WHERE source = 'business_db'")
    assert "other" not in sp


async def test_type_names_shadowing(linked):
    sdb, role, db = linked["sdb"], linked["role"], linked["db"]
    await sdb.execute("""
CREATE SCHEMA other;
CREATE TYPE public.status AS ENUM ('p1'); CREATE TYPE other.status AS ENUM ('s1');
CREATE TABLE other.tx (id int PRIMARY KEY, s other.status);
INSERT INTO other.tx VALUES (1, 's1');""")
    await sdb.execute(f'GRANT USAGE ON SCHEMA other TO "{role}"')
    await sdb.execute(f'GRANT SELECT ON other.tx TO "{role}"')
    await sdb.execute(f'ALTER ROLE "{role}" IN DATABASE "{db}" SET search_path = other, public')
    await sdb.execute("ANALYZE")
    assert (await _link(linked))["status"] == "ok"
    sp = await linked["sc"].fetchval(
        "SELECT source_search_path FROM ic_meta.link_state WHERE source = 'business_db'")
    assert sp.split(", ")[0] == "other"
    a, b = await _tn_run(linked, "SELECT 's1'::status::text")
    assert a == b == [("s1",)]
    a, b = await _tn_run(linked, "SELECT 'p1'::status::text")
    assert a == b == "InvalidTextRepresentationError"
    a, b = await _tn_run(linked, "SELECT 'p1'::public.status::text")
    assert a == b == [("p1",)]


async def test_type_names_upgrade(linked, monkeypatch):
    from app import whlink
    sc, ow = linked["sc"], linked["ow"]
    await _tn_source(linked)
    orig = whlink.fdw_type

    def old_token(col):
        r = orig(col)
        return "enum:" + r[len("enumn:"):] if r and r.startswith("enumn:") else r
    monkeypatch.setattr(whlink, "fdw_type", old_token)
    res = await whlink.link_source(ow, "business_db", _url(linked["opts"]), linked["roles"],
                                   mirror_names=False)
    assert res["status"] == "ok"
    ts = whlink.types_schema("business_db")
    n_old = await sc.fetchval(
        "SELECT count(*) FROM pg_type WHERE typnamespace = $1::regnamespace AND typtype = 'e'",
        ts)
    assert n_old >= 3
    monkeypatch.setattr(whlink, "fdw_type", orig)
    res = await _link(linked)
    assert res["status"] == "ok" and res["replaced"] == 2, res  # customers (mood) and o
    await _tn_assert_parity(linked)
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_type WHERE typnamespace = $1::regnamespace AND typtype = 'e'",
        ts) == 0
    again = await _link(linked)
    assert (again["created"], again["replaced"], again["dropped"]) == (0, 0, 0)


async def test_type_names_blocked(linked):
    sdb, sc, ow = linked["sdb"], linked["sc"], linked["ow"]
    await _tn_source(linked)
    assert (await _link(linked))["status"] == "ok"
    await ow.execute("CREATE VIEW public.model_o AS SELECT id, os FROM public.o")
    await sdb.execute("ALTER TYPE public.order_state RENAME VALUE 'open' TO 'opened'")
    res = await _link(linked)
    assert res["status"] == "stale" and "public.o" in res["stale"], res
    assert res["types"]["blocked"] == ["public.order_state"]
    labels = await sc.fetchval(
        "SELECT array_agg(enumlabel ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid = "
        "'public.order_state'::regtype")
    assert labels == ["open", "closed"]
    assert await sc.fetchval("SELECT to_regclass('public.model_o')") is not None
    assert await sc.fetchval("SELECT status FROM ic_meta.link_state") == "stale"
    # once the dependent object goes away the next link completes the change
    await ow.execute("DROP VIEW public.model_o")
    res = await _link(linked)
    assert res["status"] == "ok", res
    labels = await sc.fetchval(
        "SELECT array_agg(enumlabel ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid = "
        "'public.order_state'::regtype")
    assert labels == ["opened", "closed"]


async def test_type_names_new_source_opt_out(linked):
    from app import whlink
    sc, ow = linked["sc"], linked["ow"]
    await _tn_source(linked)
    res = await whlink.link_source(ow, "src_b", _url(linked["opts"]), linked["roles"],
                                   mirror_names=False)
    assert res["status"] == "ok", res
    assert await sc.fetchval("SELECT count(*) FROM ic_meta.link_types") == 0
    ts = whlink.types_schema("src_b")
    outside = await sc.fetch(
        "SELECT n.nspname, t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE t.typtype IN ('e','d') AND n.nspname NOT LIKE 'pg\\_%' "
        "AND n.nspname <> 'information_schema' AND n.nspname <> $1", ts)
    assert outside == [], outside
    assert await sc.fetchval(
        "SELECT count(*) FROM pg_type WHERE typnamespace = $1::regnamespace AND typtype = 'e'",
        ts) >= 3


async def test_exec_fmt_wide_statement_over_100_values(wh):
    """CR-01: a statement with more than 100 format() values must run."""
    from app.warehouse import exec_fmt
    sc = wh["sc"]
    cols = [f"c{i}" for i in range(150)]
    await exec_fmt(sc, "CREATE TABLE %I (" + ", ".join(["%I int"] * 150) + ")", "wide_t", *cols)
    n = await sc.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name='wide_t'")
    assert n == 150
