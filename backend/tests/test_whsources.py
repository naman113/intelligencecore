"""Unit tests for app.whsources (Phase 15, 15-08): pure parts and recorded DDL."""
import inspect

import pytest

from app import warehouse, whsources
from app.config import get_settings
from app.warehouse import DEFAULT_ROLES


class FakeConn:
    """Records executed statements; emulates the server-side format() used by exec_fmt."""

    def __init__(self, answers=None):
        self.stmts: list[str] = []
        self.answers = answers or {}

    def _fmt(self, tpl, *vals):
        parts = tpl.split("%")
        out = [parts[0]]
        i = 0
        for p in parts[1:]:
            c, rest = p[0], p[1:]
            v = str(vals[i])
            i += 1
            out.append(('"%s"' % v) if c == "I" else ("'%s'" % v.replace("'", "''")))
            out.append(rest)
        return "".join(out)

    def _answer(self, sql, args):
        for k, v in self.answers.items():
            if k in sql:
                return v(*args) if callable(v) else v
        return None

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT format($1"):
            if len(args) == 2 and isinstance(args[1], list):
                return self._fmt(args[0], *args[1])
            return self._fmt(*args)
        if sql.startswith("SELECT format('SELECT %s FROM"):
            return "SELECT %s FROM %s.%s" % (", ".join('"%s"' % c for c in args[0]), args[1], args[2])
        return self._answer(sql, args)

    async def fetch(self, sql, *args):
        return self._answer(sql, args) or []

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
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


async def test_bootstrap_p15_statements():
    c = FakeConn()
    out = await warehouse._bootstrap_p15(c, DEFAULT_ROLES, {"mysql_fdw": True, "tds_fdw": False})
    joined = "\n".join(c.stmts)
    assert 'GRANT USAGE ON FOREIGN DATA WRAPPER mysql_fdw TO "wh_owner"' in joined
    assert "tds_fdw" not in joined
    assert 'ALTER DEFAULT PRIVILEGES FOR ROLE "wh_loader" GRANT SELECT ON TABLES TO "wh_owner"' in joined
    assert 'ALTER ROLE "wh_loader" CONNECTION LIMIT 5' in joined
    assert "idle_in_transaction_session_timeout = '10min'" in joined
    assert "ON DATABASE" not in joined
    assert out["fdw_grants"] == ["mysql_fdw"]


@pytest.mark.parametrize("src,want", [
    ("int", "integer"), ("int(11)", "integer"), ("int unsigned", "bigint"),
    ("bigint unsigned", "numeric(20,0)"), ("tinyint(1)", "smallint"), ("smallint", "smallint"),
    ("decimal(12,2)", "numeric(12,2)"), ("double", "double precision"), ("float", "real"),
    ("varchar(80)", "character varying(80)"), ("char(3)", "character(3)"),
    ("text", "text"), ("longtext", "text"), ("enum('a','b')", "text"), ("set('x')", "text"),
    ("date", "date"), ("datetime", "timestamp without time zone"),
    ("datetime(3)", "timestamp(3) without time zone"), ("timestamp", "timestamp without time zone"),
    ("time", "time without time zone"), ("year", "smallint"), ("json", "json"),
    ("blob", "bytea"), ("varbinary(16)", "bytea"),
    ("geometry", None), ("bit(8)", None), ("weird", None), ("", None)])
def test_mysql_pg_type(src, want):
    assert whsources.mysql_pg_type(src) == want


@pytest.mark.parametrize("src,want", [
    ("int", "integer"), ("bigint", "bigint"), ("smallint", "smallint"), ("tinyint", "smallint"),
    ("bit", "boolean"), ("decimal(12,2)", "numeric(12,2)"), ("numeric(5,0)", "numeric(5,0)"),
    ("money", "numeric(19,4)"), ("float", "double precision"), ("real", "real"),
    ("nvarchar(50)", "character varying(50)"), ("nvarchar(max)", "text"), ("char(3)", "character(3)"),
    ("date", "date"), ("datetime", "timestamp without time zone"),
    ("datetime2(7)", "timestamp without time zone"), ("smalldatetime", "timestamp without time zone"),
    ("datetimeoffset(7)", "timestamp with time zone"), ("time(7)", "time without time zone"),
    ("uniqueidentifier", "uuid"), ("varbinary(max)", "bytea"), ("rowversion", "bytea"),
    ("xml", "text"), ("sql_variant", None), ("geography", None), ("nope", None)])
def test_mssql_pg_type(src, want):
    assert whsources.mssql_pg_type(src) == want


def test_mssql_live_allowed():
    n = get_settings().mssql_live_max_rows
    assert not whsources.mssql_live_allowed(None)
    assert whsources.mssql_live_allowed(0)
    assert whsources.mssql_live_allowed(n)
    assert not whsources.mssql_live_allowed(n + 1)
    assert "small lookup tables" in whsources.MSSQL_LIVE_REFUSAL
    assert whsources.MSSQL_TDS_MISSING == "the warehouse has no tds_fdw; use import"


def test_tds_wrapper():
    a = whsources.tds_wrapper_name("lookup")
    assert a == whsources.tds_wrapper_name("lookup") and a.startswith("ic_rows_")
    assert len(a) == len("ic_rows_") + 16
    t = whsources.tds_wrapper_templates()
    assert t[0].startswith("CREATE FUNCTION") and "SECURITY DEFINER" in t[0]
    assert "SET search_path = pg_catalog, pg_temp" in t[0]
    assert t[1].startswith("REVOKE ALL ON FUNCTION") and "FROM PUBLIC" in t[1]
    assert t[2].startswith("GRANT EXECUTE") and t[3].startswith("CREATE VIEW")
    assert not any("USER MAPPING" in x for x in t)


def test_mapped_col_hash():
    a = [{"name": "a", "type": "integer"}, {"name": "b", "type": "text"}]
    assert whsources.mapped_col_hash(a) == whsources.mapped_col_hash(list(a))
    assert whsources.mapped_col_hash(a) != whsources.mapped_col_hash(
        [a[0], {"name": "b", "type": "bigint"}])


def test_import_view_columns():
    attrs = [{"name": "b", "attnum": 3}, {"name": "_dlt_id", "attnum": 2},
             {"name": "gone", "attnum": 4, "dropped": True}, {"name": "a", "attnum": 1}]
    assert whsources.import_view_columns(attrs) == ["a", "b"]


PROBE = {"tables": [
    {"schema": "public", "table": "customers", "est_rows": 5, "pk": ["id"],
     "columns": [{"name": "id", "type": "integer", "nullable": False, "pk": True},
                 {"name": "name", "type": "text", "nullable": True}]},
    {"schema": "public", "table": "orders", "est_rows": 9, "pk": ["id"],
     "fks": [{"columns": ["cid"], "ref_schema": "public", "ref_table": "customers",
              "ref_columns": ["id"]},
             {"columns": ["x"], "ref_schema": "public", "ref_table": "other",
              "ref_columns": ["id"]}],
     "columns": [{"name": "id", "type": "integer", "nullable": False},
                 {"name": "cid", "type": "integer", "nullable": True},
                 {"name": "x", "type": "integer", "nullable": True}]}]}


def _tabs():
    return [{"remote_schema": "public", "remote_table": "customers", "warehouse_table": "customers",
             "mode": "live", "status": "active"},
            {"remote_schema": "public", "remote_table": "orders", "warehouse_table": "orders",
             "mode": "import", "status": "active"}]


def test_catalog_rows():
    rows = whsources.catalog_rows("erp", _tabs(), PROBE,
                                  {"orders": [{"name": "id", "type": "integer"},
                                              {"name": "new_col", "type": "text"}]})
    by = {r["table_name"]: r for r in rows}
    assert by["customers"]["relkind"] == "f" and by["orders"]["relkind"] == "r"
    assert by["orders"]["schema_name"] == "src_erp" and by["orders"]["row_estimate"] == 9
    cols = {c["name"]: c for c in by["orders"]["columns"]}
    assert cols["cid"]["fk"] == {"table": "customers", "column": "id"}
    assert cols["x"]["fk"] is None and cols["new_col"]["nullable"] is True
    assert by["customers"]["columns"][0]["pk"] is True
    assert by["orders"]["row_hash"] and by["orders"]["col_hash"]


async def test_write_catalog_passes_full_set(monkeypatch):
    tabs = _tabs() + [{"remote_schema": "public", "remote_table": "items",
                       "warehouse_table": "items", "mode": "import", "status": "active"}]

    async def gs(name):
        return {"id": 1, "kind": "postgres", "config": {}, "tables": tabs}
    seen = {}

    async def rc(conn, name, rows, linked):
        seen.update(rows=rows, linked=linked)
        return 3
    monkeypatch.setattr(whsources.sourcesvc, "get_source", gs)
    monkeypatch.setattr(whsources.whlink, "refresh_catalog", rc)
    n = await whsources.write_catalog(FakeConn(), "erp", PROBE)
    assert n == 3 and len(seen["rows"]) == 3
    assert seen["linked"] == {("src_erp", "customers"), ("src_erp", "orders"), ("src_erp", "items")}
    assert list(inspect.signature(whsources.write_catalog).parameters) == ["conn", "name", "probe"]


MS_PROBE = {"tables": [
    {"schema": "dbo", "table": "lookup", "est_rows": 10,
     "columns": [{"name": "id", "type": "int"}, {"name": "label", "type": "nvarchar(40)"}]},
    {"schema": "dbo", "table": "big", "est_rows": 10_000_000,
     "columns": [{"name": "id", "type": "int"}]}]}
CRED = {"host": "h", "port": 1433, "database": "d", "user": "u", "password": "s3cret",
        "options": {"encrypt": "no"}}  # Quick 028: live SQL Server needs an explicit encrypt=no


def _ms_tables(*names):
    return [{"remote_schema": "dbo", "remote_table": n, "warehouse_table": n, "mode": "live"}
            for n in names]


async def test_mssql_live_wrapper_ddl(monkeypatch):
    seen = {}

    async def up(conn, srv, schema, table, private, h, unl=None, **k):
        seen["link"] = (srv, schema, table, h)
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mssql(conn, "ms", CRED, _ms_tables("lookup", "big"),
                                      MS_PROBE, DEFAULT_ROLES)
    sql = "\n".join(conn.stmts)
    assert res["created"] == 1 and any("big" in u and "small lookup" in u for u in res["unlinked"])
    assert "FOREIGN DATA WRAPPER tds_fdw" in sql and "CREATE USER MAPPING FOR CURRENT_USER" in sql
    assert "SECURITY DEFINER" in sql and "SET search_path = pg_catalog, pg_temp" in sql
    assert "REVOKE ALL ON FUNCTION" in sql and "GRANT EXECUTE ON FUNCTION" in sql
    assert 'TO "wh_reader"' in sql and 'CREATE VIEW "src_ms"."lookup"' in sql
    assert "character varying(40)" in sql
    order = [sql.index(k) for k in ("CREATE FOREIGN TABLE", "CREATE FUNCTION",
                                    "REVOKE ALL ON FUNCTION", "GRANT EXECUTE", "CREATE VIEW")]
    assert order == sorted(order)
    assert all("FOR CURRENT_USER" in s for s in conn.stmts if "CREATE USER MAPPING" in s)
    assert "CASCADE" not in sql.upper() and "IMPORT FOREIGN SCHEMA" not in sql.upper()
    assert seen["link"][0] == "src_ms"


async def test_mssql_tds_missing():
    conn = FakeConn({"pg_extension": None})
    res = await whsources._live_mssql(conn, "ms", CRED, _ms_tables("lookup"), MS_PROBE,
                                      DEFAULT_ROLES)
    assert res["unlinked"] == ["lookup: " + whsources.MSSQL_TDS_MISSING] and not conn.stmts


async def test_mssql_removal_drop_order(monkeypatch):
    async def dl(conn, srv, s, t):
        pass
    monkeypatch.setattr(whsources.whlink, "_delete_link_object", dl)
    rows = [{"remote_schema": "dbo", "remote_table": "lookup", "col_hash": "x",
             "private_schema": "ic_fdw_priv"}]
    conn = FakeConn({"FROM ic_meta.link_objects": rows, "pg_extension": 1})
    await whsources._live_mssql(conn, "ms", CRED, [], MS_PROBE, DEFAULT_ROLES)
    drops = [s for s in conn.stmts if s.startswith("DROP")]
    assert [d.split()[1] for d in drops] == ["VIEW", "FUNCTION", "FOREIGN"]
    assert not any("CASCADE" in d for d in drops)


async def test_mysql_live_ddl_and_rerun(monkeypatch):
    async def up(*a, **k):
        pass
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    probe = {"tables": [{"schema": "ops", "table": "carriers", "est_rows": 8, "columns": [
        {"name": "id", "type": "int(11)"}, {"name": "g", "type": "geometry"}]}]}
    t = [{"remote_schema": "ops", "remote_table": "carriers", "warehouse_table": "carriers",
          "mode": "live"}]
    cred = dict(CRED, database="ops")
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mysql(conn, "my", cred, t, probe, DEFAULT_ROLES)
    sql = "\n".join(conn.stmts)
    assert "truncatable 'false'" in sql and "mysql_fdw" in sql and '"g"' not in sql
    assert "SECURITY DEFINER" in sql
    assert res["created"] == 1
    h = whsources.mapped_col_hash([{"name": "id", "type": "integer"}, {"name": "g", "type": None}])
    rows = [{"remote_schema": "ops", "remote_table": "carriers", "col_hash": h,
             "private_schema": "p"}]
    conn2 = FakeConn({"FROM ic_meta.link_objects": rows, "pg_extension": 1})
    res2 = await whsources._live_mysql(conn2, "my", cred, t, probe, DEFAULT_ROLES)
    assert res2["created"] == 0 and res2["replaced"] == 0
    assert not any(s.startswith(("CREATE FOREIGN", "DROP")) for s in conn2.stmts)


async def test_provision_busy_and_redaction(monkeypatch):
    async def gs(name):
        return {"id": 1, "kind": "postgres", "credential": "cred:c", "config": {}, "tables": []}

    async def ld(n):
        return dict(CRED)

    async def lp(src):
        return None
    conn = FakeConn({"pg_try_advisory_lock": False})

    async def oc(roles):
        return conn
    monkeypatch.setattr(whsources.sourcesvc, "get_source", gs)
    monkeypatch.setattr(whsources.sourcevault, "load", ld)
    monkeypatch.setattr(whsources, "_load_probe", lp)
    monkeypatch.setattr(whsources, "_owner_conn", oc)
    monkeypatch.setattr(whsources.warehouse, "warehouse_mode", lambda: True)
    assert (await whsources.provision("x"))["status"] == "busy"
    conn.answers = {"pg_try_advisory_lock": True}

    async def boom(*a, **k):
        raise RuntimeError("login failed for s3cret")
    monkeypatch.setattr(whsources, "_provision", boom)
    out = await whsources.provision("x")
    assert out["status"] == "error" and "s3cret" not in out["detail"]


async def test_provision_without_warehouse(monkeypatch):
    monkeypatch.setattr(whsources.warehouse, "warehouse_mode", lambda: False)
    assert (await whsources.provision("x"))["detail"] == "warehouse not configured"

# Phase 15 gap 2 (WR-14): live links under warehouse names
def _mixed_tables(kind_schema):
    return [{"remote_schema": kind_schema, "remote_table": "LookupCodes",
             "warehouse_table": "lookup_codes", "mode": "live"}]


async def test_mysql_live_mixed_case_name(monkeypatch):
    seen = {}

    async def up(conn, srv, schema, table, private, h, unl=None, **k):
        seen.update(table=table, kw=k)
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    probe = {"tables": [{"schema": "ops", "table": "LookupCodes", "est_rows": 8,
                         "columns": [{"name": "id", "type": "int(11)"}]}]}
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mysql(conn, "my", dict(CRED, database="ops"),
                                      _mixed_tables("ops"), probe, DEFAULT_ROLES)
    sql = "\n".join(conn.stmts)
    assert res["created"] == 1 and not res["unlinked"]
    assert "CREATE FOREIGN TABLE" in sql and "table_name 'LookupCodes'" in sql
    assert 'CREATE VIEW "src_my"."lookup_codes"' in sql
    assert whsources.tds_wrapper_name("lookup_codes") in sql
    assert seen["table"] == "LookupCodes" and seen["kw"] == {"view_name": "lookup_codes"}


async def test_mssql_live_mixed_case_name(monkeypatch):
    async def up(*a, **k):
        pass
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    probe = {"tables": [{"schema": "dbo", "table": "LookupCodes", "est_rows": 8,
                         "columns": [{"name": "id", "type": "int"}]}]}
    conn = FakeConn({"pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mssql(conn, "ms", CRED, _mixed_tables("dbo"), probe,
                                      DEFAULT_ROLES)
    sql = "\n".join(conn.stmts)
    assert res["created"] == 1
    assert "table_name 'LookupCodes'" in sql and 'CREATE VIEW "src_ms"."lookup_codes"' in sql


async def test_live_view_rename_is_replace_and_drop_uses_recorded_name(monkeypatch):
    async def up(*a, **k):
        pass

    async def dl(*a, **k):
        pass
    monkeypatch.setattr(whsources.whlink, "_upsert_link_object", up)
    monkeypatch.setattr(whsources.whlink, "_delete_link_object", dl)
    probe = {"tables": [{"schema": "dbo", "table": "LookupCodes", "est_rows": 8,
                         "columns": [{"name": "id", "type": "int"}]}]}
    h = whsources.mapped_col_hash([{"name": "id", "type": "integer"}])
    rows = [{"remote_schema": "dbo", "remote_table": "LookupCodes", "col_hash": h,
             "private_schema": "p", "view_name": "old_name"}]
    conn = FakeConn({"FROM ic_meta.link_objects": rows, "pg_extension": 1, "pg_namespace": 1})
    res = await whsources._live_mssql(conn, "ms", CRED, _mixed_tables("dbo"), probe,
                                      DEFAULT_ROLES)
    assert res["replaced"] == 1
    drops = [x for x in conn.stmts if x.startswith("DROP")]
    assert drops[0] == 'DROP VIEW IF EXISTS "src_ms"."old_name"'
    assert 'DROP FOREIGN TABLE IF EXISTS "p"."LookupCodes"' in drops
    # removal of a row with a recorded name drops that view
    conn2 = FakeConn({"FROM ic_meta.link_objects": rows, "pg_extension": 1})
    await whsources._live_mssql(conn2, "ms", CRED, [], probe, DEFAULT_ROLES)
    assert [x for x in conn2.stmts if x.startswith("DROP VIEW")] == [
        'DROP VIEW IF EXISTS "src_ms"."old_name"']

