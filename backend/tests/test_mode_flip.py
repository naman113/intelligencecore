"""Phase 17 (CONS-05): mode flips. Validation, dependents, the tool, the executor, cutover."""
import pytest

from app import modeflip, sourcetools as st
from app.agents import graph as g
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def row(id_, table, mode="live", **kw):
    r = {"id": id_, "remote_schema": "public", "remote_table": table, "warehouse_schema": "public",
         "warehouse_table": table, "mode": mode, "pending_mode": None, "status": "active",
         "est_rows": 100, "cursor_column": None, "merge_key": None}
    r.update(kw)
    return r


def cat_table(table, cols=None, kind="table", rows=100):
    cols = cols or [{"name": "id", "type": "integer", "pk": True},
                    {"name": "updated_at", "type": "timestamp with time zone", "pk": False},
                    {"name": "note", "type": "text", "pk": False}]
    return {"schema": "public", "table": table, "object_kind": kind, "row_estimate": rows,
            "columns": cols}


def source(tables, name="business_db", kind="postgres", schedule=None):
    return {"name": name, "kind": kind, "status": "active", "tables": tables,
            "schedule": schedule or {}, "freshness_minutes": None}


S = get_settings()
KEYS = {("public", "orders"), ("public", "items")}


def check(src, req, catalog=None, keys=KEYS, **kw):
    catalog = catalog or {"tables": [cat_table("orders"), cat_table("items")]}
    return modeflip.validate_mode_change(src, req, catalog, keys, S, **kw)


def test_to_import_facts_and_default_schedule():
    facts, err = check(source([row(1, "orders")]),
                       [{"table": "orders", "mode": "import", "cursor": "updated_at"}])
    assert err is None
    t = facts["tables"][0]
    assert (t["from"], t["to"], t["cursor"], t["merge_key"]) == ("live", "import", "updated_at", ["id"])
    assert facts["schedule"]["interval_minutes"] == S.sync_default_interval_minutes
    assert facts["freshness_minutes"] == 3 * facts["schedule"]["interval_minutes"]


def test_existing_schedule_kept():
    facts, _ = check(source([row(1, "orders")], schedule={"interval_minutes": 30}),
                     [{"table": "orders", "mode": "import"}])
    assert facts["schedule"] is None


@pytest.mark.parametrize("req,fragment", [
    ([{"table": "nope", "mode": "import"}], "unknown table"),
    ([{"table": "orders", "mode": "live"}], "already live"),
    ([{"table": "orders", "mode": "dual"}], "mode must be"),
    ([{"table": "orders", "mode": "import", "cursor": "note"}], "date, timestamp or integer"),
    ([{"table": "orders", "mode": "import", "cursor": "zzz"}], "not a column"),
    ([{"table": "orders", "mode": "import", "bogus": 1}], "each table is"),
    ([], "non-empty"),
    ([{"table": "orders", "mode": "import", "merge_key": ["zzz"]}], "merge_key column"),
])
def test_refusals(req, fragment):
    _, err = check(source([row(1, "orders")]), req)
    assert fragment in err


def test_pending_refused():
    _, err = check(source([row(1, "orders", pending_mode="import")]),
                   [{"table": "orders", "mode": "live"}])
    assert "pending" in err


def test_inactive_and_csv():
    s = source([row(1, "orders", mode="import")], kind="csv", name="c1")
    _, err = check(s, [{"table": "orders", "mode": "live"}])
    assert "import only" in err
    s["status"] = "paused"
    assert check(s, [{"table": "orders", "mode": "live"}])[1] == "unknown or inactive source"


def test_no_key_refused():
    cat = {"tables": [cat_table("orders", [{"name": "a", "type": "integer", "pk": False}])]}
    _, err = check(source([row(1, "orders")]), [{"table": "orders", "mode": "import"}], cat)
    assert "merge key" in err


def test_large_needs_cursor():
    cat = {"tables": [cat_table("orders", rows=S.sync_reconcile_max_rows + 1)]}
    _, err = check(source([row(1, "orders")]), [{"table": "orders", "mode": "import"}], cat)
    assert "cursor" in err


def test_business_db_view_and_live_link():
    cat = {"tables": [cat_table("orders", kind="view")]}
    _, err = check(source([row(1, "orders")]), [{"table": "orders", "mode": "import"}], cat)
    assert "base tables" in err
    _, err = check(source([row(1, "orders", mode="import")]), [{"table": "orders", "mode": "live"}],
                   keys=set())
    assert "no live link" in err
    facts, err = check(source([row(1, "orders", mode="import")]), [{"table": "orders", "mode": "live"}])
    assert err is None and facts["tables"][0]["to"] == "live"


def test_two_schemas_refused():
    cat = {"tables": [cat_table("orders"), {**cat_table("x"), "schema": "other"}]}
    rows_ = [row(1, "orders"), row(2, "x", warehouse_schema="other", remote_schema="other")]
    _, err = check(source(rows_), [{"table": "orders", "mode": "import"},
                                   {"table": "x", "mode": "import"}], cat)
    assert "one remote schema" in err


def test_existing_import_schema_counts():
    cat = {"tables": [cat_table("orders"), {**cat_table("x"), "schema": "other"}]}
    rows_ = [row(1, "orders", mode="import"),
             row(2, "x", warehouse_schema="other", remote_schema="other")]
    _, err = check(source(rows_), [{"table": "x", "mode": "import"}], cat)
    assert "one remote schema" in err


def test_too_many():
    rows_ = [row(i, f"t{i}") for i in range(21)]
    _, err = check(source(rows_), [{"table": f"t{i}", "mode": "import"} for i in range(21)])
    assert "at most" in err


def test_mssql_live_rules():
    r = row(1, "orders", mode="import", est_rows=50)
    s = source([r], name="erp", kind="mssql")
    cat = {"tables": [cat_table("orders", rows=50)]}
    _, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=())
    assert "tds_fdw" in err
    facts, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=("tds_fdw",))
    assert err is None
    cat = {"tables": [cat_table("orders", rows=10 ** 7)]}
    _, err = check(s, [{"table": "orders", "mode": "live"}], cat, has_ext=("tds_fdw",))
    assert "small lookup" in err


def test_registered_live_needs_read_only():
    s = source([row(1, "orders", mode="import")], name="erp")
    _, err = check(s, [{"table": "orders", "mode": "live"}], read_only=False)
    assert "read-only" in err


# ── dependents ──
class Pool:
    def __init__(self, data):
        self.data = data

    async def fetch(self, sql, *a):
        for k, v in self.data.items():
            if k in sql:
                if isinstance(v, Exception):
                    raise v
                return v
        return []


async def test_dependents_counts_case_folded_and_search_path(monkeypatch):
    pool = Pool({
        "FROM watches": [{"id": 1, "title": "w1", "compiled_sql": 'SELECT count(*) FROM "Order"'},
                         {"id": 2, "title": "w2", "compiled_sql": "SELECT 1 FROM other"},
                         {"id": 3, "title": "bad", "compiled_sql": "DROP TABLE x"}],
        "FROM dashboard_versions": [{"slug": "d", "title": "Dash",
                                     "queries": '[{"id":"a","sql":"SELECT * FROM public.\\"Order\\""}]'}],
        "FROM events": [{"id": 9, "title": "ins", "sql": 'SELECT * FROM public."order" o'}],
        "FROM model_tables": [{"name": "m", "relation": "public.Order", "derived_sql": None}],
        "FROM model_measures": [{"name": "rev", "base_relation": "public.order"}],
    })

    async def gp():
        return pool
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(modeflip, "_search_path", lambda: ["public"])
    d = await modeflip.dependents("public", "Order")
    assert (d["watches"], d["dashboards"], d["insights"], d["model_objects"]) == (1, 1, 1, 2)
    assert d["titles"]["watches"] == ["w1"]


async def test_dependents_survives_failed_reads(monkeypatch):
    async def gp():
        return Pool({"FROM watches": RuntimeError("x"), "FROM events": RuntimeError("y")})
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(modeflip, "_search_path", lambda: ["public"])
    d = await modeflip.dependents("public", "orders")
    assert d["watches"] == 0 and d["insights"] == 0


# ── propose and the tool ──
@pytest.fixture
def prop(monkeypatch):
    inserted = []

    async def ctx(name):
        return {"source": source([row(1, "orders")]), "catalog": {"tables": [cat_table("orders")]},
                "keys": KEYS, "exts": set(), "read_only": True}

    async def deps(s, t):
        return {"watches": 2, "dashboards": 1, "insights": 0, "model_objects": 0, "titles": {}}

    async def ins(payload, run_id):
        inserted.append(payload)
        return (7, None) if len(inserted) == 1 else (None, 7)
    monkeypatch.setattr(modeflip, "_context", ctx)
    monkeypatch.setattr(modeflip, "dependents", deps)
    monkeypatch.setattr(modeflip.sourcesvc, "insert_proposal", ins)
    return inserted


async def test_propose_writes_one_action(prop):
    out = await modeflip.propose("business_db", [{"table": "orders", "mode": "import"}])
    assert "action #7" in out and "awaiting owner approval" in out
    p = prop[0]
    assert p["op"] == "mode" and p["name"] == "business_db"
    assert modeflip.KEEP_LIVE_NOTE in p["notes"] and modeflip.OFFHOURS_NOTE in p["notes"]
    assert p["dependents"]["public.orders"]["watches"] == 2
    assert "last sync" in p["freshness"]
    again = await modeflip.propose("business_db", [{"table": "orders", "mode": "import"}])
    assert again.startswith("REFUSED") and "#7" in again


async def test_propose_refusal_text(prop):
    out = await modeflip.propose("business_db", [{"table": "orders", "mode": "live"}])
    assert out.startswith("REFUSED") and "already live" in out and prop == []


async def test_tool_refuses_secret_and_logs(monkeypatch, prop):
    logged = []

    async def log_tool(name, args, result, start, refused=False):
        logged.append((name, result, refused))
    monkeypatch.setattr(st, "_log_tool", log_tool)
    out = await st.propose_mode_change.ainvoke(
        {"source": "business_db", "tables": [{"table": "postgres://u:p@h/db", "mode": "import"}]})
    assert out == st.REFUSAL_TEXT and prop == []
    out = await st.propose_mode_change.ainvoke(
        {"source": "business_db", "tables": [{"table": "orders", "mode": "import"}]})
    assert "action #7" in out and logged[-1][0] == "propose_mode_change"


def test_registration():
    assert "propose_mode_change" in g.SOURCE_TOOL_NAMES
    assert "propose_mode_change" in g._tool_registry()
    assert "propose_mode_change" in g.AGENT_TOOL_SUPERSET["supervisor"]
    for k, v in g.AGENT_TOOL_SUPERSET.items():
        if k != "supervisor":
            assert "propose_mode_change" not in v
    assert g.AGENT_TOOL_SUPERSET["supervisor"][-2:] == ["discover_schema", "propose_context_change"]
    assert "propose_mode_change" in g.DEFAULT_PROMPTS["supervisor"]


def test_not_in_playbooks():
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "app" / "playbooks.py"
    lines = [ln for ln in src.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("#")]
    assert not any("propose_mode_change" in ln for ln in lines)


# ── executor ──────────────────────────────────────────────────────────────
import asyncio as _asyncio  # noqa: E402

import asyncpg  # noqa: E402

from app import whsources  # noqa: E402
from app.warehouse import DEFAULT_ROLES  # noqa: E402


class FakeConn:
    """Recording owner connection; format() is emulated so the DDL text is visible."""

    def __init__(self, rows=None):
        self.stmts = []
        self.rows = rows or {}

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT format($1"):
            tpl, vals = args[0], list(args[1])
            parts = tpl.split("%")
            out, i = [parts[0]], 0
            for p in parts[1:]:
                out.append('"%s"' % vals[i] if p[0] == "I" else "'%s'" % vals[i])
                out.append(p[1:])
                i += 1
            return "".join(out)
        if "pg_try_advisory_lock" in sql:
            return True
        return None

    async def fetchrow(self, sql, *args):
        for k, v in self.rows.items():
            if k in sql:
                return v
        return None

    async def execute(self, sql, *a):
        self.stmts.append(sql)

    def transaction(self):
        class T:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False
        return T()

    async def close(self):
        pass


class FakePool:
    def __init__(self):
        self.log = []

    async def fetchval(self, sql, *a):
        return 55

    async def execute(self, sql, *a):
        self.log.append((sql, a))


@pytest.fixture
def ex(monkeypatch):
    st_ = {"pool": FakePool(), "conn": FakeConn(), "views": [], "enq": [], "live": True,
           "ctx_source": source([row(1, "orders"), row(2, "items", mode="import")])}

    async def gp():
        return st_["pool"]

    async def ctx(name):
        return {"source": {**st_["ctx_source"], "id": 3},
                "catalog": {"tables": [cat_table("orders"), cat_table("items")]},
                "keys": KEYS, "exts": set(), "read_only": True}

    async def oc(roles):
        return st_["conn"]

    async def lock(conn, name):
        return st_["live"]

    async def noop(*a, **k):
        return None

    async def rvp(conn, sch, wt, ss, sr, *a, **k):
        st_["views"].append((sch, wt, ss, sr))
        if st_.get("rvp_exc"):
            raise st_["rvp_exc"]
        return st_.get("rvp_res", {"status": "ok"})

    async def lr(conn, name, r, cred, roles=DEFAULT_ROLES, **k):
        return whsources.LiveRel("ic_fdw_x", r["remote_table"])

    async def enq(sid, kind, **k):
        st_["enq"].append((sid, kind))
        return 1
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(modeflip, "_context", ctx)
    monkeypatch.setattr(whsources, "_owner_conn", oc)
    monkeypatch.setattr(whsources, "_lock", lock)
    monkeypatch.setattr(whsources, "_unlock", noop)
    monkeypatch.setattr(whsources, "ensure_import_schemas", noop)
    monkeypatch.setattr(whsources, "write_catalog", noop)
    monkeypatch.setattr(whsources, "replace_view_preserving", rvp)
    monkeypatch.setattr(whsources, "live_relation", lr)
    monkeypatch.setattr("app.syncsched.enqueue_now", enq)
    monkeypatch.setattr("app.warehouse._lock", lambda: _asyncio.Lock())
    return st_


def payload(*items):
    return {"op": "mode", "name": "business_db", "tables": list(items), "schedule": None}


async def test_apply_to_import_marks_pending_no_view_ddl(ex):
    ok = await modeflip.apply(payload({"table": "orders", "schema": "public", "to": "import",
                                       "cursor": "updated_at", "merge_key": ["id"]}))
    assert ok is True
    sql, args = ex["pool"].log[0]
    assert "pending_mode = 'import'" in sql and "import_reset = true" in sql
    assert args[0] == 1 and args[1] == "updated_at" and args[2] == ["id"]
    assert ex["views"] == [] and ex["conn"].stmts == []
    assert ex["enq"] == [(3, "backfill")]
    assert any("schedule" in s for s, _ in ex["pool"].log)  # defaulted schedule written


async def test_apply_to_live_replaces_view_then_marks(ex):
    ok = await modeflip.apply(payload({"table": "items", "schema": "public", "to": "live"}))
    assert ok is True
    assert ex["views"] == [("public", "items", "ic_fdw_x", "items")]
    sql, _ = ex["pool"].log[0]
    assert "mode = 'live'" in sql and "pending_mode = NULL" in sql
    assert ex["enq"] == []
    assert not [s for s in ex["conn"].stmts if "DROP" in s]


async def test_apply_refused_replace_leaves_mode_and_continues(ex):
    ex["rvp_res"] = {"status": "refused", "reason": "column note is gone"}
    ok = await modeflip.apply(payload({"table": "items", "schema": "public", "to": "live"},
                                      {"table": "orders", "schema": "public", "to": "import",
                                       "merge_key": ["id"]}))
    assert ok is False
    sqls = [s for s, _ in ex["pool"].log]
    assert any("last_error" in s and "mode = 'live'" not in s for s in sqls)
    assert any("pending_mode = 'import'" in s for s in sqls)  # the other table still applied
    assert not any("mode = 'live', pending_mode = NULL" in s for s in sqls)


async def test_apply_postgres_error_sets_last_error(ex):
    ex["rvp_exc"] = asyncpg.UndefinedTableError("boom")
    ok = await modeflip.apply(payload({"table": "items", "schema": "public", "to": "live"}))
    assert ok is False
    sql, args = ex["pool"].log[0]
    assert "last_error" in sql and "UndefinedTableError" in args[1]


async def test_apply_busy(ex):
    ex["live"] = False
    ok = await modeflip.apply(payload({"table": "items", "schema": "public", "to": "live"}))
    assert ok is False and ex["views"] == []
    assert ex["pool"].log[0][1][1] == modeflip.BUSY_TEXT


async def test_apply_revalidation_failure_changes_nothing(ex):
    ok = await modeflip.apply(payload({"table": "orders", "schema": "public", "to": "live"}))
    assert ok is False and ex["pool"].log == [] and ex["views"] == []


async def test_dispatch_op_mode(monkeypatch):
    from app import sourcesvc
    seen = []

    async def fake(p):
        seen.append(p)
        return True

    async def boom(p):
        raise AssertionError("revalidate must not run")
    monkeypatch.setattr(modeflip, "apply", fake)
    monkeypatch.setattr(sourcesvc, "_revalidate", boom)
    assert await sourcesvc.apply_source_change({"op": "mode", "name": "x"}) is True and seen


# ── drift path (real whsources code, recording connection) ──
async def test_bdb_live_relation_equal_hash_no_ddl():
    c = FakeConn({"FROM ic_meta.link_objects": {"private_schema": "ic_fdw_bdb_public", "col_hash": "h"},
                  "FROM ic_meta.source_catalog": {"col_hash": "h"}})
    rel = await whsources.live_relation(
        c, "business_db", {"remote_schema": "public", "remote_table": "orders"}, None)
    assert tuple(rel[:2]) == ("ic_fdw_bdb_public", "orders") and c.stmts == []


async def test_bdb_live_relation_drift_recreates_foreign_table_only(monkeypatch):
    c = FakeConn({"FROM ic_meta.link_objects": {"private_schema": "ic_fdw_bdb_public", "col_hash": "old"},
                  "FROM ic_meta.source_catalog": {"col_hash": "new"}})
    snap_row = {"schema_name": "public", "table_name": "orders", "col_hash": "new", "columns": []}
    made = []

    async def snap(conn, source, opts=None):
        return {"tables": [snap_row]}

    async def cft(conn, source, t, roles, mirror_names=True, **k):
        c.stmts.append("CREATE FOREIGN TABLE")
        made.append(mirror_names)
        return "ic_fdw_bdb_public", [], []

    async def upsert(conn, source, schema, table, private, h, unl=None, **k):
        c.stmts.append("UPSERT " + h)
    monkeypatch.setattr(whsources.whlink, "fetch_snapshot", snap)
    monkeypatch.setattr(whsources.whlink, "_create_foreign_table", cft)
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", upsert)
    monkeypatch.setattr(whsources.whlink, "parse_source_url", lambda u: type("O", (), {"user": "u"})())
    monkeypatch.setattr(whsources.warehouse, "_business_url", lambda: "postgres://u:p@h/d")
    rel = await whsources.live_relation(
        c, "business_db", {"remote_schema": "public", "remote_table": "orders"}, None)
    assert tuple(rel[:2]) == ("ic_fdw_bdb_public", "orders")
    joined = " | ".join(c.stmts)
    drop = 'DROP FOREIGN TABLE IF EXISTS "ic_fdw_bdb_public"."orders"'
    assert joined.index(drop) < joined.index("CREATE FOREIGN TABLE")
    assert "UPSERT new" in joined and "DROP VIEW" not in joined and "CASCADE" not in joined
    assert made == [True]


async def test_modeflip_issues_no_ddl_itself():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "app" / "modeflip.py").read_text(encoding="utf-8")
    assert "exec_fmt" not in src and "execute(\"CREATE" not in src and "execute(\"DROP" not in src


# ── cutover ──────────────────────────────────────────────────────────────
@pytest.fixture
def adv(monkeypatch):
    s = {"rows": [], "pool": None, "records": [], "views": [], "dropped": [], "probe_fail": False,
         "cast_exc": None, "live": True}

    class P(FakePool):
        async def fetch(self, sql, *a):
            return s["rows"]

        async def fetchrow(self, sql, *a):
            return {"credential": None, "config": {}}

    s["pool"] = P()
    s["conn"] = FakeConn()

    async def probe_fetchval(sql, *args, _orig=s["conn"].fetchval):
        if sql.startswith("SELECT 1 FROM") and s["probe_fail"]:
            raise asyncpg.UndefinedTableError("gone")
        if sql.startswith('SELECT 1 FROM'):
            return 1
        if "SELECT format('SELECT 1 FROM" in sql:
            return 'SELECT 1 FROM "%s"."%s" LIMIT 1' % args
        return await _orig(sql, *args)
    s["conn"].fetchval = probe_fetchval

    async def gp():
        return s["pool"]

    async def oc(roles):
        return s["conn"]

    async def lock(conn, name):
        return s["live"]

    async def noop(*a, **k):
        return None

    async def rvp(conn, sch, wt, ss, sr, *a, **k):
        s["views"].append((sch, wt, ss, sr))
        if ss.startswith("ic_imp_") and s["cast_exc"]:
            raise s["cast_exc"]
        return {"status": "ok"}

    async def lr(conn, name, r, cred, roles=DEFAULT_ROLES, **k):
        return whsources.LiveRel("ic_fdw_x", r["remote_table"])

    async def drop(conn, name, r, roles=DEFAULT_ROLES):
        s["dropped"].append(r["warehouse_table"])

    async def rec(kind, name="", **k):
        s["records"].append((kind, name, k.get("status"), k.get("summary")))

    async def cr(source, need):
        return None, None
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(whsources, "_owner_conn", oc)
    monkeypatch.setattr(whsources, "_lock", lock)
    monkeypatch.setattr(whsources, "_unlock", noop)
    monkeypatch.setattr(whsources, "write_catalog", noop)
    monkeypatch.setattr(whsources, "replace_view_preserving", rvp)
    monkeypatch.setattr(whsources, "live_relation", lr)
    monkeypatch.setattr(whsources, "drop_live_relation", drop)
    monkeypatch.setattr(modeflip, "_credentials", cr)
    monkeypatch.setattr("app.jobruns.record", rec)
    monkeypatch.setattr("app.warehouse._lock", lambda: _asyncio.Lock())
    return s


def pending_row(source_name="erp", kind="postgres", **kw):
    r = {"id": 9, "source_id": 3, "source_name": source_name, "source_kind": kind,
         "warehouse_schema": "src_erp", "warehouse_table": "orders", "remote_schema": "public",
         "remote_table": "orders"}
    r.update(kw)
    return r


async def test_advance_cuts_over_and_drops_live_for_registered(adv):
    adv["rows"] = [pending_row()]
    out = await modeflip.advance()
    assert out["switched"] == ["src_erp.orders"]
    assert adv["views"] == [("src_erp", "orders", "ic_imp_erp", "orders")]
    assert adv["dropped"] == ["orders"]
    sql, _ = adv["pool"].log[-1]
    assert "mode = 'import'" in sql and "pending_mode = NULL" in sql
    assert adv["records"] == [("mode_switch", "src_erp.orders", "done", "Switched src_erp.orders to import")]


async def test_advance_business_db_keeps_foreign_table(adv):
    adv["rows"] = [pending_row("business_db", warehouse_schema="public")]
    out = await modeflip.advance()
    assert out["switched"] == ["public.orders"] and adv["dropped"] == []


async def test_advance_only_selects_finished_backfills():
    sql = modeflip._PENDING_SQL
    assert "t.pending_mode = 'import'" in sql and "t.backfill_done" in sql
    assert "t.status = 'active'" in sql and "s.status = 'active'" in sql


async def test_advance_cast_failure_stays_live_and_is_not_retried(adv):
    adv["cast_exc"] = asyncpg.InvalidTextRepresentationError("bad")
    adv["rows"] = [pending_row()]
    out = await modeflip.advance()
    assert out["failed"] == ["src_erp.orders"] and adv["dropped"] == []
    sql, args = adv["pool"].log[-1]
    assert "pending_mode = CASE WHEN $3" in sql and args[2] is True
    assert args[1].startswith("switch to import stopped: InvalidTextRepresentationError")
    assert adv["records"][0][2] == "failed"
    assert len(adv["views"]) == 1  # no revert needed: the cast check ran before any replace


async def test_advance_probe_failure_reverts_to_live(adv):
    adv["probe_fail"] = True
    adv["rows"] = [pending_row()]
    out = await modeflip.advance()
    assert out["failed"] == ["src_erp.orders"] and adv["dropped"] == []
    assert adv["views"] == [("src_erp", "orders", "ic_imp_erp", "orders"),
                            ("src_erp", "orders", "ic_fdw_x", "orders")]
    assert "back on live" in adv["pool"].log[-1][1][1]


async def test_advance_busy_skips_and_never_raises(adv):
    adv["live"] = False
    adv["rows"] = [pending_row()]
    out = await modeflip.advance()
    assert out["busy"] == ["src_erp.orders"] and adv["views"] == []


async def test_advance_conn_error_does_not_raise(adv, monkeypatch):
    async def oc(roles):
        raise OSError("down")
    monkeypatch.setattr(whsources, "_owner_conn", oc)
    adv["rows"] = [pending_row()]
    out = await modeflip.advance()
    assert out["busy"] == ["src_erp.orders"]


async def test_q027_dependents_count_reads_through_model_views(monkeypatch):
    pool = Pool({
        "FROM watches": [{"id": 1, "title": "via view", "compiled_sql": "SELECT * FROM model.v2"}],
        "FROM dashboard_versions": [{"slug": "d", "title": "Dash",
                                     "queries": '[{"id":"a","sql":"SELECT * FROM model.v1"}]'}],
        "FROM events": [{"id": 9, "title": "ins", "sql": "SELECT * FROM model.unrelated"}],
        "FROM model_tables": [
            {"name": "v2", "relation": "model.v2", "derived_sql": "SELECT * FROM model.v1"},
            {"name": "v1", "relation": "model.v1", "derived_sql": "SELECT * FROM public.orders"},
            {"name": "unrelated", "relation": "model.unrelated", "derived_sql": "SELECT * FROM public.other"}],
    })

    async def gp():
        return pool
    monkeypatch.setattr(modeflip, "get_pool", gp)
    monkeypatch.setattr(modeflip, "_search_path", lambda: ["public"])
    d = await modeflip.dependents("public", "orders")
    assert (d["watches"], d["dashboards"], d["insights"], d["model_objects"]) == (1, 1, 0, 2)
