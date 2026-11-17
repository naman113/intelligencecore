"""Phase 15 (SRC-05, SRC-06, SRC-08): the real ic-sync image against real Postgres scratch databases.

Skipped unless WAREHOUSE_TEST_URL (loopback admin URL of the phase warehouse) and IC_SYNC_TEST_IMAGE
are set. Scratch source and warehouse databases and prefixed roles are created and dropped here.
The loader role runs under CONNECTION LIMIT 5, as wh_loader does.
Order matters: the module shares one source database and one warehouse, tests run top to bottom.
"""
import asyncio
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

_URL = os.getenv("WAREHOUSE_TEST_URL", "")
_IMAGE = os.getenv("IC_SYNC_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not _URL or not _IMAGE or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="WAREHOUSE_TEST_URL / IC_SYNC_TEST_IMAGE not set or not loopback",
)

NET = os.getenv("IC_SYNC_TEST_NETWORK", "ic-net")
CSV_NET = os.getenv("IC_SYNC_TEST_CSV_NETWORK", "ic_p25_syncnet")
WH_HOST = os.getenv("IC_SYNC_TEST_WH_HOST", "ic-p25-warehouse")
CSV_DIR = Path(os.getenv("IC_SYNC_TEST_CSV_DIR") or Path(__file__).resolve().parents[3] / "deploy" / "demo-sources")


def _with_db(db, user=None, pw=None):
    p = urlsplit(_URL)
    host = p.netloc.rsplit("@", 1)[-1]
    auth = f"{quote(user)}:{quote(pw)}@" if user else p.netloc.rsplit("@", 1)[0] + "@"
    return urlunsplit((p.scheme, auth + host, "/" + db, "", ""))


def _run(coro):
    return asyncio.run(coro)


async def _exec(db, *sqls):
    import asyncpg
    c = await asyncpg.connect(_with_db(db))
    try:
        for s in sqls:
            await c.execute(s)
    finally:
        await c.close()


async def _fetch(db, sql, *args, user=None, pw=None):
    import asyncpg
    c = await asyncpg.connect(_with_db(db, user, pw))
    try:
        return await c.fetch(sql, *args)
    finally:
        await c.close()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import asyncpg

    from app import syncrunner, warehouse
    from app.warehouse import Roles
    tag = secrets.token_hex(4)
    sdb, wdb = f"ic_p15s_{tag}", f"ic_p15w_{tag}"
    roles = Roles(owner=f"t15_{tag}_owner", loader=f"t15_{tag}_loader", reader=f"t15_{tag}_reader")
    src_user = f"t15_{tag}_src"
    pws = {"owner": secrets.token_hex(24), "loader": secrets.token_hex(24),
           "reader": secrets.token_hex(24), "src": secrets.token_hex(24)}

    async def setup():
        admin = await asyncpg.connect(_URL)
        await admin.execute(f'CREATE DATABASE "{sdb}"')
        await admin.execute(f'CREATE DATABASE "{wdb}"')
        await admin.execute(f"CREATE ROLE \"{src_user}\" LOGIN PASSWORD '{pws['src']}'")
        await admin.close()
        await _exec(sdb, f'GRANT CONNECT ON DATABASE "{sdb}" TO "{src_user}"',
                    f'GRANT USAGE ON SCHEMA public TO "{src_user}"',
                    f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{src_user}"')
        wc = await asyncpg.connect(_with_db(wdb))
        try:
            await warehouse.bootstrap(wc, roles=roles, passwords=pws)
            await wc.execute(f'ALTER ROLE "{roles.loader}" CONNECTION LIMIT 5')
            await wc.execute(f'GRANT CONNECT ON DATABASE "{wdb}" TO "{roles.loader}"')
            await wc.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{roles.loader}" '
                             f'GRANT SELECT ON TABLES TO "{roles.owner}"')
        finally:
            await wc.close()

    _run(setup())
    mk_names: set[str] = set()

    def schemas(name: str) -> None:
        if name in mk_names:
            return
        mk_names.add(name)
        stmts = []
        for sch in (f"ic_imp_{name}", f"ic_imp_{name}_staging", f"stage_{name}", f"stage_{name}_staging"):
            stmts += [f'CREATE SCHEMA IF NOT EXISTS "{sch}" AUTHORIZATION "{roles.owner}"',
                      f'GRANT USAGE, CREATE ON SCHEMA "{sch}" TO "{roles.loader}"']
        _run(_exec(wdb, *stmts))

    base = {"IC_SRC_HOST": WH_HOST, "IC_SRC_PORT": "5432", "IC_SRC_DB": sdb, "IC_SRC_USER": src_user,
            "IC_SRC_PASSWORD": pws["src"], "IC_WH_HOST": WH_HOST, "IC_WH_PORT": "5432", "IC_WH_DB": wdb,
            "IC_WH_USER": roles.loader, "IC_WH_PASSWORD": pws["loader"]}
    from app.config import get_settings
    os.environ["SYNC_IMAGE"] = _IMAGE
    get_settings.cache_clear()
    state = {"sdb": sdb, "wdb": wdb, "roles": roles, "pws": pws, "base": base, "schemas": schemas,
             "syncrunner": syncrunner, "n": 0}
    yield state

    async def teardown():
        admin = await asyncpg.connect(_URL)
        for d in (sdb, wdb):
            await admin.execute(f'DROP DATABASE IF EXISTS "{d}" WITH (FORCE)')
        for r in (roles.owner, roles.loader, roles.reader, src_user):
            await admin.execute(f'DROP ROLE IF EXISTS "{r}"')
        await admin.close()

    _run(teardown())
    os.environ.pop("SYNC_IMAGE", None)
    get_settings.cache_clear()


def _src(env, *sqls):
    _run(_exec(env["sdb"], *sqls))


def _wh(env, sql, *args):
    return _run(_fetch(env["wdb"], sql, *args))


def _sync(env, cmd, name, tables, caps=None, reconcile=False, remote_schema="public", kind="postgres",
          row_limit=None, network=None, timeout=300):
    env["n"] += 1
    job = {"v": 1, "source": name, "kind": kind, "remote_schema": remote_schema,
           "dataset": f"ic_imp_{name}" if cmd == "sync" else f"stage_{name}",
           "pipeline": f"ic_{name}" if cmd == "sync" else f"test_{name}",
           "tables": tables, "reconcile": reconcile}
    if caps:
        job["caps"] = caps
    if row_limit:
        job["row_limit"] = row_limit
    env["schemas"](name)
    res = _run(env["syncrunner"].run(cmd, job, dict(env["base"]), name=f"t15-{name}-{env['n']}",
                                     network=network or NET, timeout=timeout))
    assert res.get("peak_memory_mb") is not None or res["status"] != "ok"
    return res


def _t(res, name):
    return next(t for t in res["tables"] if t["table"] == name)


def T(remote, cursor="updated_at", key=("id",), reconcile=False):
    return {"remote_table": remote, "warehouse_table": remote, "cursor": cursor,
            "merge_key": list(key), "reconcile": reconcile, "initial_value": None}


ITEMS = (
    "CREATE TABLE items (id int primary key, v text, n int, updated_at timestamptz not null)",
    "INSERT INTO items SELECT i, 'v'||i, i, timestamptz '2026-01-01 00:00:00+00' + i * interval '1 second' "
    "FROM generate_series(1, 2500) i",
    "CREATE TABLE lookup (id int primary key, name text)",
    "INSERT INTO lookup SELECT i, 'n'||i FROM generate_series(1, 20) i",
)


def test_icsync_backfill_resumes(env):
    _src(env, *ITEMS)
    caps = {"max_rows": 1000, "chunk_size": 500, "max_seconds": 300}
    r1 = _sync(env, "sync", "t", [T("items")], caps)
    assert r1["status"] == "ok", r1
    assert _t(r1, "items")["rows"] == 1000 and _t(r1, "items")["capped"] is True
    r2 = _sync(env, "sync", "t", [T("items")], caps)
    assert _t(r2, "items")["capped"] is True, r2
    n2 = _wh(env, 'SELECT count(*) c FROM ic_imp_t.items')[0]["c"]
    assert 1990 <= n2 <= 2000
    r3 = _sync(env, "sync", "t", [T("items")], caps)
    assert _t(r3, "items")["capped"] is False, r3
    rows = _wh(env, 'SELECT count(*) c, count(DISTINCT id) d FROM ic_imp_t.items')[0]
    assert rows["c"] == 2500 and rows["d"] == 2500


def test_icsync_cursor_ties_guard(env):
    _src(env,
         "CREATE TABLE ties (id int primary key, v text, updated_at timestamptz not null)",
         "INSERT INTO ties SELECT i, 'v', timestamptz '2026-02-01+00' FROM generate_series(1, 1200) i",
         "INSERT INTO ties SELECT 1200 + i, 'v', timestamptz '2026-03-01+00' + i * interval '1 second' "
         "FROM generate_series(1, 100) i")
    caps = {"max_rows": 500, "chunk_size": 250, "max_seconds": 300}
    outcome = "unfinished"
    for _ in range(5):
        r = _sync(env, "sync", "ties", [T("ties")], caps)
        t = _t(r, "ties")
        assert not (t["capped"] and t["rows"] == 0), t
        if t["status"] == "failed":
            assert t["error_class"] == "CursorStalled" and t["capped"] is False
            outcome = "stalled"
            break
        if not t["capped"]:
            outcome = "finished"
            break
    print("ties outcome:", outcome)
    if outcome != "stalled":
        d = _wh(env, "SELECT count(*) c, count(DISTINCT id) d FROM ic_imp_ties.ties")[0]
        assert d["c"] == d["d"]


def test_icsync_row_cap_below_chunk(env):
    _src(env,
         "CREATE TABLE capt (id int primary key, updated_at timestamptz not null)",
         "INSERT INTO capt SELECT i, timestamptz '2026-01-01+00' + i * interval '1 second' "
         "FROM generate_series(1, 1000) i")
    r = _sync(env, "sync", "cap", [T("capt")], {"max_rows": 300, "chunk_size": 50000, "max_seconds": 300})
    t = _t(r, "capt")
    assert t["rows"] <= 300 and t["capped"] is True, t


def test_icsync_incremental_merge(env):
    _src(env, "UPDATE items SET v = 'new'||id, updated_at = timestamptz '2027-01-01+00' + id * interval '1 second' "
              "WHERE id <= 10")
    r = _sync(env, "sync", "t", [T("items")], {"max_rows": 1000, "chunk_size": 500, "max_seconds": 300})
    assert _t(r, "items")["rows"] == 10, r
    c = _wh(env, "SELECT count(*) c, count(*) FILTER (WHERE v LIKE 'new%') nw FROM ic_imp_t.items")[0]
    assert c["c"] == 2500 and c["nw"] == 10


def test_icsync_new_column_evolves(env):
    _src(env, "ALTER TABLE items ADD COLUMN note text",
         "UPDATE items SET note = 'x', updated_at = timestamptz '2028-01-01+00' WHERE id = 11")
    r = _sync(env, "sync", "t", [T("items")], {"max_rows": 1000, "chunk_size": 500, "max_seconds": 300})
    assert r["status"] == "ok", r
    cols = [x["column_name"] for x in _wh(
        env, "SELECT column_name FROM information_schema.columns WHERE table_schema='ic_imp_t' AND table_name='items'")]
    assert "note" in cols


def test_icsync_type_change_fails_table(env):
    _src(env, "ALTER TABLE items ALTER COLUMN n TYPE text USING n::text",
         "UPDATE items SET updated_at = timestamptz '2029-01-01+00' WHERE id = 12")
    r = _sync(env, "sync", "t", [T("items"), T("lookup", cursor=None, reconcile=True)],
              {"max_rows": 1000, "chunk_size": 500, "max_seconds": 300})
    assert r["status"] == "partial" and r["exit_code"] == 2, r
    assert _t(r, "items")["status"] == "failed" and _t(r, "items")["error_class"] == "DataValidationError"
    assert _t(r, "lookup")["status"] == "ok"


def test_icsync_reconcile_deletes(env):
    _src(env, "CREATE TABLE rec (id int primary key, updated_at timestamptz not null)",
         "INSERT INTO rec SELECT i, now() FROM generate_series(1, 20) i")
    tabs = [T("rec", reconcile=True)]
    _sync(env, "sync", "t", tabs, reconcile=True)  # first run loads; nothing to delete
    _src(env, "DELETE FROM rec WHERE id = 5")
    r = _sync(env, "sync", "t", tabs, reconcile=True)
    assert _t(r, "rec")["reconciled_deleted"] == 1, r
    assert _wh(env, "SELECT count(*) c FROM ic_imp_t.rec WHERE id = 5")[0]["c"] == 0
    assert _wh(env, "SELECT count(*) c FROM ic_imp_t.rec")[0]["c"] == 19


def test_icsync_analyze_and_layout(env):
    roles = env["roles"]
    assert _wh(env, "SELECT last_analyze FROM pg_stat_user_tables WHERE schemaname='ic_imp_t' "
                    "AND relname='lookup'")[0]["last_analyze"] is not None
    # last_analyze lands asynchronously in the stats collector; items was not analyzed after its failure
    cols = _wh(env, "SELECT column_name FROM information_schema.columns WHERE table_schema='ic_imp_t' "
                    "AND table_name IN ('items','lookup') AND column_name LIKE '\\_dlt\\_%'")
    assert cols == []
    owners = {r["tableowner"] for r in _wh(
        env, "SELECT tableowner FROM pg_tables WHERE schemaname='ic_imp_t' AND tablename IN ('items','lookup')")}
    assert owners == {roles.loader}
    rows = _run(_fetch(env["wdb"], "SELECT count(*) c FROM ic_imp_t.lookup",
                       user=roles.owner, pw=env["pws"]["owner"]))
    assert rows[0]["c"] == 20


def test_icsync_test_command_caps(env):
    r = _sync(env, "test", "t", [T("items", cursor=None), T("lookup", cursor=None)], row_limit=1000)
    assert r["status"] == "ok" or r["status"] == "partial", r
    lk = _t(r, "lookup")
    assert lk["status"] == "ok" and lk["columns"]
    n = _wh(env, "SELECT count(*) c FROM stage_t.lookup")[0]["c"]
    assert n <= 1000
    it = _t(r, "items")
    if it["status"] == "ok":
        assert _wh(env, "SELECT count(*) c FROM stage_t.items")[0]["c"] <= 1000


def _csv(env, mode, name, path, disposition="replace", types=None):
    env["n"] += 1
    env["schemas"](name)
    job = {"v": 1, "source": name, "kind": "csv", "dataset": f"ic_imp_{name}" if mode == "load" else f"stage_{name}",
           "pipeline": f"ic_{name}" if mode == "load" else f"test_{name}", "row_limit": 1000 if mode == "test" else None,
           "csv": {"mode": mode, "table": "prices", "column_types": types, "disposition": disposition,
                   "delimiter": ",", "encoding": "utf-8"}}
    sec = {k: v for k, v in env["base"].items() if k.startswith("IC_WH_")}
    return _run(env["syncrunner"].run("csv", job, sec, name=f"t15-csv-{env['n']}", network=CSV_NET,
                                      timeout=300, stdin_path=path))


def test_icsync_csv(env):
    f1, f2 = CSV_DIR / "supplier_prices.csv", CSV_DIR / "supplier_prices_v2.csv"
    t = _csv(env, "test", "csvt", f1)
    assert t["status"] == "ok", t
    types = t["column_types"]
    assert types["supplier_id"] == "int64" and "price" in types
    ld = _csv(env, "load", "csvt", f1, "replace", types)
    assert ld["status"] == "ok", ld
    assert _wh(env, "SELECT count(*) c FROM ic_imp_csvt.prices")[0]["c"] == 200
    oid1 = _wh(env, "SELECT 'ic_imp_csvt.prices'::regclass::oid o")[0]["o"]
    _csv(env, "load", "csvt", f2, "replace", types)
    assert _wh(env, "SELECT count(*) c FROM ic_imp_csvt.prices")[0]["c"] == 150
    assert _wh(env, "SELECT 'ic_imp_csvt.prices'::regclass::oid o")[0]["o"] == oid1  # A1 held
    _csv(env, "load", "csvt", f2, "append", types)
    assert _wh(env, "SELECT count(*) c FROM ic_imp_csvt.prices")[0]["c"] == 300
    bad = CSV_DIR.parent.parent / ".tracker" / "bad_prices.csv"
    bad.parent.mkdir(exist_ok=True)
    bad.write_text("supplier_id,product_code,price,currency,valid_from\n2,SKU,notanumber,USD,2026-02-02\n")
    try:
        badtypes = dict(types, price="double")
        r = _csv(env, "load", "csvt", bad, "replace", badtypes)
        assert r["status"] == "failed", r
        assert r["tables"][0]["error_class"] == "ArrowInvalid", r
    finally:
        bad.unlink(missing_ok=True)


def test_no_containers_left():
    out = subprocess.run(["docker", "ps", "-a", "--filter", "label=ic.sync=1", "-q"],
                         capture_output=True, text=True).stdout.strip()
    assert out == ""
