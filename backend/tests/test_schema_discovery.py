import json

import pytest

from app import contexttools, schemadiscovery as sd
from app.guardrails import validate_readonly_sql


def _col(name, type_="integer", nullable=False, pk=False, fk=None, comment=None):
    return {"name": name, "type": type_, "nullable": nullable, "pk": pk,
            "fk": fk, "comment": comment}


def _row(schema, table, cols, checks=None, relkind="r"):
    return {"schema_name": schema, "table_name": table, "relkind": relkind,
            "table_comment": None, "row_estimate": 10,
            "columns": json.dumps(cols), "checks": json.dumps(checks) if checks else None}


class _Settings:
    sql_row_limit = 500


def _patch_sql(monkeypatch, rows, record=None):
    async def fake(sql):
        if record is not None:
            record.append(sql)
        return rows
    monkeypatch.setattr(sd, "run_readonly_sql", fake)
    monkeypatch.setattr(sd, "get_settings", lambda: _Settings())


def test_catalog_sql_passes_guardrail():
    validate_readonly_sql(sd.CATALOG_SQL)


def test_catalog_has_no_information_schema_query():
    src = open(sd.__file__, encoding="utf-8").read()
    assert "FROM information_schema" not in src
    assert "asyncpg.connect" not in src and "create_pool" not in src


async def test_introspect_decodes_json(monkeypatch):
    _patch_sql(monkeypatch, [_row("public", "customers", [_col("id", pk=True)], ["CHECK (x > 0)"])])
    r = await sd.introspect()
    assert r["source"] == "business_db" and r["truncated"] is False
    t = r["tables"][0]
    assert t["columns"][0]["pk"] is True and t["checks"] == ["CHECK (x > 0)"]
    assert t["object_kind"] == "table"


async def test_introspect_truncated_and_system_filter(monkeypatch):
    rows = [_row("pg_temp_3", "x", []), _row("information_schema", "y", []),
            _row("public", "v", [], relkind="v")]
    _patch_sql(monkeypatch, rows)
    monkeypatch.setattr(sd, "get_settings", lambda: type("S", (), {"sql_row_limit": 3})())
    r = await sd.introspect()
    assert r["truncated"] is True
    assert [t["table"] for t in r["tables"]] == ["v"]
    assert r["tables"][0]["object_kind"] == "view"


def test_refs():
    assert sd.table_ref("business_db", "public", "customers") == "business_db.public.customers"
    assert sd.column_ref("business_db", "public", "customers", "region_id") == \
        "business_db.public.customers.region_id"


def test_signature_stable_and_sensitive():
    a = {"type": "integer", "nullable": False, "pk": False, "fk": None}
    b = {"fk": None, "pk": False, "nullable": False, "type": "integer"}
    s = sd.signature(a)
    assert len(s) == 16 and int(s, 16) >= 0 and s == sd.signature(b)
    assert s != sd.signature({**a, "type": "text"})
    assert s != sd.signature({**a, "nullable": True})
    assert s != sd.signature({**a, "pk": True})
    assert s != sd.signature({**a, "fk": {"table": "r", "column": "id"}})


async def test_sample_values_quoting_and_truncation(monkeypatch):
    sql = []
    _patch_sql(monkeypatch, [{"a": "x" * 100, "b": None}], sql)
    out = await sd.sample_values("public", 'we"ird')
    assert sql == ['SELECT * FROM "public"."we""ird" LIMIT 3']
    assert out == {"a": ["x" * 60]}


async def test_sample_values_swallows_errors(monkeypatch):
    async def boom(sql):
        raise RuntimeError("x")
    monkeypatch.setattr(sd, "run_readonly_sql", boom)
    assert await sd.sample_values("public", "t") == {}


async def test_resolve(monkeypatch):
    fk = {"table": "regions", "column": "id"}
    sql = []
    _patch_sql(monkeypatch, [_row("public", "customers", [_col("region_id", fk=fk)])], sql)
    r = await sd.resolve("business_db.public.customers.region_id")
    assert r["kind"] == "column"
    assert r["structure"] == {"type": "integer", "nullable": False, "pk": False, "fk": fk}
    assert r["references_ref"] == "business_db.public.regions.id"
    assert len(r["signature"]) == 16
    t = await sd.resolve("business_db.public.customers")
    assert t["kind"] == "table"
    assert await sd.resolve("business_db.public.nope") is None
    assert await sd.resolve("garbage'; drop table x") is None
    assert all("garbage" not in s for s in sql)


# ---- diff ----

def _live():
    return {"source": "business_db", "tables": [
        {"schema": "public", "table": "customers", "object_kind": "table",
         "comment": None, "row_estimate": 5, "checks": [],
         "columns": [_col("id", pk=True), _col("name", "text")]},
    ]}


def _known(**kw):
    base = {"tables": {}, "columns": {}, "pending": set()}
    base.update(kw)
    return base


def _sig(c):
    return sd.signature(sd.structure_of(c, "table"))


def test_diff_all_new():
    d = sd.diff(_live(), _known())
    t = d["tables"][0]
    assert t["needs_table_description"] is True and len(t["columns"]) == 2
    assert d["counts"]["new"] == 3
    assert {c["status"] for c in t["columns"]} == {"new"}


def test_diff_known_and_new_column_does_not_reopen_table():
    live = _live()
    known = _known(
        tables={"business_db.public.customers": {"ref": "x", "description": "d", "status": "active"}},
        columns={"business_db.public.customers.id": {
            "description": "d", "status": "active", "signature": _sig(_col("id", pk=True))}})
    d = sd.diff(live, known)
    t = d["tables"][0]
    assert t["needs_table_description"] is False
    assert [c["name"] for c in t["columns"]] == ["name"]
    assert d["counts"]["known"] == 2


def test_diff_changed_column():
    known = _known(
        tables={"business_db.public.customers": {"description": "d", "status": "active"}},
        columns={"business_db.public.customers.name": {
            "description": "old", "status": "active", "signature": "deadbeefdeadbeef"},
            "business_db.public.customers.id": {
                "description": "d", "status": "active", "signature": _sig(_col("id", pk=True))}})
    d = sd.diff(_live(), known)
    c = d["tables"][0]["columns"][0]
    assert c["status"] == "changed" and c["current_description"] == "old"
    assert d["counts"]["changed"] == 1


def test_diff_pending_hidden_missing_and_zero_after_all_done():
    known = _known(
        tables={"business_db.public.customers": {"description": "d", "status": "active"},
                "business_db.public.gone": {"description": "g", "status": "active"}},
        columns={"business_db.public.customers.id": {
            "description": "d", "status": "hidden", "signature": None}},
        pending={("column", "business_db.public.customers.name")})
    d = sd.diff(_live(), known)
    assert d["tables"] == []
    assert d["counts"]["pending"] == 1 and d["counts"]["hidden"] == 1
    assert d["missing"] == ["business_db.public.gone"]


def test_diff_is_pure():
    live, known = _live(), _known()
    assert sd.diff(live, known) == sd.diff(live, known)


class _FakePool:
    def __init__(self):
        self.sql = []

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return []


async def test_known_state_select_only(monkeypatch):
    pool = _FakePool()

    async def gp():
        return pool
    monkeypatch.setattr(sd, "get_pool", gp)
    k = await sd.known_state()
    assert k["pending"] == set() and len(pool.sql) == 3
    for s in pool.sql:
        u = s.upper()
        assert "INSERT" not in u and "UPDATE" not in u and u.lstrip().startswith("SELECT")


async def test_discover_caps_and_remaining(monkeypatch):
    rows = [_row("public", f"t{i}", [_col("id")]) for i in range(4)]
    _patch_sql(monkeypatch, rows)

    async def gp():
        return _FakePool()
    monkeypatch.setattr(sd, "get_pool", gp)
    r = await sd.discover(max_tables=2)
    assert len(r["tables"]) == 2 and r["remaining"] == 2 and r["truncated"] is False
    text = sd.format_discovery(r)
    assert "business_db.public.t0" in text and "2 more table" in text


def test_format_discovery_samples_label():
    d = sd.diff(_live(), _known())
    d["tables"][0]["columns"][0]["samples"] = ["a"]
    d["remaining"] = 0
    d["truncated"] = True
    text = sd.format_discovery(d)
    assert "untrusted data" in text and "row cap" in text


# ---- tool ----

async def test_tool_unknown_source_refused(monkeypatch):
    logged = []

    async def rec(name, args, result, start, refused=False):
        logged.append((name, args, refused))

    async def no(*a, **k):
        raise AssertionError("must not query")
    monkeypatch.setattr(contexttools, "_log_tool", rec)
    monkeypatch.setattr(sd, "run_readonly_sql", no)
    out = await contexttools.discover_schema.ainvoke({"source": "ERP"})
    assert "business_db" in out
    assert logged == [("discover_schema", {"source": "ERP"}, True)]


async def test_tool_ok_and_error(monkeypatch):
    logged = []

    async def rec(name, args, result, start, refused=False):
        logged.append((result, refused))
    monkeypatch.setattr(contexttools, "_log_tool", rec)

    async def fake_discover(src):
        d = sd.diff(_live(), _known())
        d["remaining"] = 0
        d["truncated"] = False
        d["source"] = src
        return d
    monkeypatch.setattr(sd, "discover", fake_discover)
    out = await contexttools.discover_schema.ainvoke({"source": " Business_DB "})
    assert "business_db.public.customers" in out and len(out) <= 20000

    async def boom(src):
        raise RuntimeError("bad")
    monkeypatch.setattr(sd, "discover", boom)
    out = await contexttools.discover_schema.ainvoke({"source": "business_db"})
    assert out.startswith("TOOL ERROR:")
    assert logged[-1][0].startswith("TOOL ERROR:")


def test_no_write_paths():
    src = open(sd.__file__, encoding="utf-8").read()
    for bad in ("INSERT", "UPDATE ", "contextstore"):
        assert bad not in src
    # contexttools may INSERT INTO actions (proposals, plan 02-05) but never
    # writes a context row and never calls the writer.
    src = open(contexttools.__file__, encoding="utf-8").read()
    assert "contextstore" not in src
    assert src.count("INSERT INTO") == 1 and "INSERT INTO actions" in src
    assert "UPDATE " not in src


# ── Phase 14 (WH-05) ──
def test_foreign_tables_discovered_and_signature_unchanged():
    assert "'f'" in sd.CATALOG_SQL.split("relkind IN")[1].split(")")[0]
    assert sd._KIND["f"] == "foreign"
    col = {"type": "integer", "nullable": False, "pk": True, "fk": None}
    assert sd.signature(sd.structure_of(col, "foreign")) == sd.signature(sd.structure_of(col, "table"))


# ---- Phase 14 (WH-05): warehouse mode ----

def _wh_mode(monkeypatch, on=True):
    from app import warehouse
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: on)
    monkeypatch.setattr(sd, "business_db_configured", lambda: True)  # Quick 031: a business DB is set


def _patch_wh_sql(monkeypatch, status_rows, cat_rows=None, record=None):
    async def fake(sql):
        if record is not None:
            record.append(sql)
        if sql == sd.WAREHOUSE_LINK_STATUS_SQL:
            return status_rows
        return cat_rows if cat_rows is not None else []
    monkeypatch.setattr(sd, "run_readonly_sql", fake)
    monkeypatch.setattr(sd, "get_settings", lambda: _Settings())


def test_warehouse_catalog_sql_passes_guardrail():
    validate_readonly_sql(sd.WAREHOUSE_CATALOG_SQL)
    validate_readonly_sql(sd.WAREHOUSE_LINK_STATUS_SQL)
    for needle in ("ic_meta.source_catalog", "has_table_privilege", "ORDER BY"):
        assert needle in sd.WAREHOUSE_CATALOG_SQL
    assert "ic_meta.link_state" in sd.WAREHOUSE_LINK_STATUS_SQL


async def test_introspect_warehouse_uses_snapshot_sql(monkeypatch):
    _wh_mode(monkeypatch)
    rec = []
    _patch_wh_sql(monkeypatch, [{"status": "ok"}], [_row("public", "t", [_col("id")])], rec)
    r = await sd.introspect()
    assert rec == [sd.WAREHOUSE_LINK_STATUS_SQL, sd.WAREHOUSE_CATALOG_SQL]
    assert r["tables"][0]["table"] == "t" and r["tables"][0]["object_kind"] == "table"


async def test_introspect_direct_uses_catalog_sql(monkeypatch):
    _wh_mode(monkeypatch, False)
    rec = []
    _patch_sql(monkeypatch, [], rec)
    await sd.introspect()
    assert rec == [sd.CATALOG_SQL]


async def test_link_status_never_linked_raises(monkeypatch):
    from app.guardrails import BusinessDbUnreachable
    _wh_mode(monkeypatch)
    rec = []
    _patch_wh_sql(monkeypatch, [], None, rec)
    with pytest.raises(BusinessDbUnreachable):
        await sd.introspect()
    assert sd.WAREHOUSE_CATALOG_SQL not in rec


async def test_link_status_error_raises(monkeypatch):
    from app.guardrails import BusinessDbUnreachable
    _wh_mode(monkeypatch)
    _patch_wh_sql(monkeypatch, [{"status": "error"}])
    with pytest.raises(BusinessDbUnreachable):
        await sd.introspect()
    with pytest.raises(BusinessDbUnreachable):
        await sd.resolve_checked("business_db.public.t")


@pytest.mark.parametrize("status", ["ok", "stale"])
async def test_link_status_usable(monkeypatch, status):
    _wh_mode(monkeypatch)
    _patch_wh_sql(monkeypatch, [{"status": status}], [_row("public", "t", [_col("id")])])
    assert len((await sd.introspect())["tables"]) == 1


async def test_link_status_discover_never_linked_raises(monkeypatch):
    from app import warehouse
    from app.guardrails import BusinessDbUnreachable
    _wh_mode(monkeypatch)
    _patch_wh_sql(monkeypatch, [])

    async def fresh(max_age=0.0, timeout=20.0):
        return {"status": "error"}
    monkeypatch.setattr(warehouse, "ensure_fresh", fresh)
    with pytest.raises(BusinessDbUnreachable):
        await sd.discover()


async def test_link_status_not_run_outside_warehouse_mode(monkeypatch):
    _wh_mode(monkeypatch, False)
    rec = []
    _patch_sql(monkeypatch, [], rec)
    await sd.introspect()
    assert sd.WAREHOUSE_LINK_STATUS_SQL not in rec


async def test_discover_ensure_fresh_every_call_resolve_never(monkeypatch):
    from app import warehouse
    calls = []

    async def fresh(max_age=0.0, timeout=20.0):
        calls.append(max_age)
        return {"status": "fresh"}
    monkeypatch.setattr(warehouse, "ensure_fresh", fresh)
    _patch_wh_sql(monkeypatch, [{"status": "ok"}], [_row("public", "t", [_col("id")])])

    async def gp():
        return _FakePool()
    monkeypatch.setattr(sd, "get_pool", gp)
    _wh_mode(monkeypatch)
    await sd.discover()
    await sd.discover()
    assert calls == [0, 0]
    await sd.resolve("business_db.public.t")
    await sd.resolve_checked("business_db.public.t")
    assert calls == [0, 0]
    _wh_mode(monkeypatch, False)
    _patch_sql(monkeypatch, [_row("public", "t", [_col("id")])])
    await sd.discover()
    assert calls == [0, 0]
