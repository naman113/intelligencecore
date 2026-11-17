"""Phase 3 dashboards.py DB operations: statement order and transaction boundaries
via a recording fake asyncpg connection. No DB, LLM or network."""

import json
import logging

import pytest

from app import dashboards as d
from app.dashboards import DashboardError
from _lintclean import BAD_PAGE, QUERIES, clean_page

TOKEN = "TOKEN-FIXED-VALUE-0123456789"
GOOD_Q = [{"id": "sales", "sql": "SELECT 1 AS n", "refresh_minutes": 15}]
CODE = "<html><script>const a = 1;</script></html>"


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.calls.append(("BEGIN", "", ()))

    async def __aexit__(self, *exc):
        self.conn.calls.append(("END", "", ()))
        return False


class RecConn:
    """Records every call in order; responses come from (substring, response) rules."""

    def __init__(self, rules=None):
        self.calls = []
        self.rules = list(rules or [])

    def transaction(self):
        return _Tx(self)

    def _resp(self, method, sql, args, default):
        self.calls.append((method, sql, args))
        for sub, resp in self.rules:
            if sub in sql:
                return resp(*args) if callable(resp) else resp
        return default

    async def fetchrow(self, sql, *args):
        return self._resp("fetchrow", sql, args, None)

    async def fetchval(self, sql, *args):
        return self._resp("fetchval", sql, args, None)

    async def fetch(self, sql, *args):
        return self._resp("fetch", sql, args, [])

    async def execute(self, sql, *args):
        return self._resp("execute", sql, args, "UPDATE 1")

    async def executemany(self, sql, rows):
        return self._resp("executemany", sql, (rows,), None)

    def sqls(self):
        return [c[1] for c in self.calls if c[0] not in ("BEGIN", "END")]

    def index(self, needle):
        for i, c in enumerate(self.calls):
            if needle in c[1]:
                return i
        raise AssertionError(f"no call containing {needle!r}")


@pytest.fixture(autouse=True)
def fixed_token(monkeypatch):
    monkeypatch.setattr(d, "new_view_token", lambda: TOKEN)


# ---------------------------------------------------------------- save_version

def _save_conn(max_version=1, created=True):
    return RecConn([
        ("INSERT INTO dashboards", {"id": 7, "created": created}),
        ("MAX(version)", max_version),
        ("INSERT INTO dashboard_versions", 42),
    ])


def _save(conn, **kw):
    args = dict(slug="sales", title="Sales", code=CODE, queries=GOOD_Q)
    args.update(kw)
    return d.save_version(conn, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"slug": "Bad Slug"},
    {"title": "  "},
    {"code": ""},
    {"queries": "nope"},
    {"queries": [{"id": "BAD", "sql": "SELECT 1"}]},
    {"author": "robot"},
    {"task_run_id": True},
    {"change_note": 5},
])
async def test_save_invalid_input_does_no_io(kw):
    conn = _save_conn()
    with pytest.raises(DashboardError) as ei:
        await _save(conn, **kw)
    assert ei.value.status == 422
    assert conn.calls == []


@pytest.mark.asyncio
async def test_save_statement_order():
    conn = _save_conn()
    await _save(conn)
    kinds = [(c[0], c[1][:30]) for c in conn.calls]
    assert [k[0] for k in kinds] == [
        "BEGIN", "fetchrow", "fetchval", "fetchval", "executemany", "END"]
    assert conn.calls[1][1].startswith("INSERT INTO dashboards")
    assert "ON CONFLICT (slug) DO UPDATE" in conn.calls[1][1]
    assert "(xmax = 0) AS created" in conn.calls[1][1]
    assert "SELECT COALESCE(MAX(version), 0) + 1 FROM dashboard_versions WHERE dashboard_id = $1" \
        in conn.calls[2][1]
    assert "INSERT INTO dashboard_versions" in conn.calls[3][1]
    assert "'draft'" in conn.calls[3][1]
    assert "INSERT INTO dashboard_snapshots" in conn.calls[4][1]


@pytest.mark.asyncio
async def test_save_upsert_args_and_conflict_branch():
    conn = _save_conn()
    await _save(conn)
    sql, args = conn.calls[1][1], conn.calls[1][2]
    assert args == ("sales", "Sales", TOKEN)
    after = sql.split("DO UPDATE SET", 1)[1]
    assert "title =" not in after and "view_token =" not in after
    assert "updated_at = now()" in after


@pytest.mark.asyncio
async def test_save_version_number_json_and_hashes():
    conn = _save_conn(max_version=3)
    await _save(conn, change_note="n", author="operator", task_run_id=9)
    args = conn.calls[3][2]
    assert args[0] == 7 and args[1] == 3
    assert args[2] == "Sales"
    assert args[3] == d.normalize_code(CODE)
    assert args[4] == d.script_hashes(d.normalize_code(CODE))
    assert isinstance(args[4], list)
    assert json.loads(args[5]) == d.validate_queries(GOOD_Q)
    assert args[6:] == ("n", "operator", 9)
    rows = conn.calls[4][2][0]
    assert rows == [(42, "sales", 15)]


@pytest.mark.asyncio
async def test_save_no_queries_skips_executemany():
    conn = _save_conn()
    await _save(conn, queries=[])
    assert not any(c[0] == "executemany" for c in conn.calls)


@pytest.mark.asyncio
async def test_save_never_touches_current_version_id():
    conn = _save_conn()
    await _save(conn)
    assert not any("current_version_id" in s for s in conn.sqls())


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [True, False])
async def test_save_return_value(created):
    conn = _save_conn(max_version=2, created=created)
    out = await _save(conn)
    assert out == {
        "dashboard_id": 7, "slug": "sales", "version_id": 42, "version": 2,
        "status": "draft", "created": created,
        "script_hashes": d.script_hashes(CODE), "path": "/d/sales/v/2/"}


@pytest.mark.asyncio
async def test_save_normalizes_crlf():
    conn = _save_conn()
    await _save(conn, code="<p>a</p>\r\n<p>b</p>\r")
    assert "\r" not in conn.calls[3][2][3]


@pytest.mark.asyncio
async def test_save_does_not_log_token(caplog):
    conn = _save_conn()
    with caplog.at_level(logging.DEBUG):
        await _save(conn)
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------- publish

def _publish_conn(current=None, old=None, target=True):
    return RecConn([
        ("FROM dashboards", {"id": 1, "current_version_id": current}),
        ("FROM dashboard_versions", {"id": 11, "version": 2, "title": "T2", "status": "draft", "code": clean_page(), "queries": QUERIES} if target else None),
        ("SET status = 'retired'", old),
    ])


@pytest.mark.asyncio
async def test_publish_statement_order():
    conn = _publish_conn(current=10, old={"id": 10, "version": 1})
    out = await d.publish(conn, dashboard_id=1, version_id=11)
    assert conn.calls[0][0] == "BEGIN" and conn.calls[-1][0] == "END"
    first = conn.calls[1][1]
    assert "FROM dashboards" in first and "FOR UPDATE" in first
    lookup = conn.calls[2][1]
    assert "dashboard_versions" in lookup and "id = $2" in lookup and "dashboard_id = $1" in lookup
    retire = conn.index("SET status = 'retired'")
    pub = conn.index("SET status = 'published'")
    ptr = conn.index("SET current_version_id = $2")
    assert 2 < retire < pub < ptr
    assert "retired_at = now()" in conn.calls[retire][1]
    assert "status = 'published'" in conn.calls[retire][1]
    assert "published_at = now()" in conn.calls[pub][1] and "retired_at = NULL" in conn.calls[pub][1]
    assert conn.calls[ptr][2] == (1, 11, "T2")
    assert out == {"changed": True, "version_id": 11, "version": 2,
                   "from_version_id": 10, "from_version": 1}
    assert not any("INSERT INTO actions" in s for s in conn.sqls())


@pytest.mark.asyncio
async def test_publish_first_ever_has_no_from():
    conn = _publish_conn(current=None, old=None)
    out = await d.publish(conn, dashboard_id=1, version_id=11)
    assert out["from_version_id"] is None and out["from_version"] is None


@pytest.mark.asyncio
async def test_publish_already_current_is_noop():
    conn = _publish_conn(current=11)
    out = await d.publish(conn, dashboard_id=1, version_id=11)
    assert out["changed"] is False
    assert not any(c[0] == "execute" or c[1].startswith("UPDATE") for c in conn.calls)


@pytest.mark.asyncio
async def test_publish_missing_dashboard_or_target_404():
    conn = RecConn()
    with pytest.raises(DashboardError) as ei:
        await d.publish(conn, dashboard_id=1, version_id=11)
    assert ei.value.status == 404
    with pytest.raises(DashboardError) as ei:
        await d.publish(_publish_conn(current=10, target=False), dashboard_id=1, version_id=11)
    assert ei.value.status == 404


# --------------------------------------------------------------- resolve_decider

@pytest.mark.asyncio
async def test_resolve_decider_explicit_known():
    conn = RecConn([("FROM people WHERE id = $1", 5)])
    assert await d.resolve_decider(conn, 5) == 5


@pytest.mark.asyncio
async def test_resolve_decider_explicit_unknown_422():
    with pytest.raises(DashboardError) as ei:
        await d.resolve_decider(RecConn(), 5)
    assert ei.value.status == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, "5", 1.5])
async def test_resolve_decider_rejects_non_int(bad):
    with pytest.raises(DashboardError) as ei:
        await d.resolve_decider(RecConn([("people", 5)]), bad)
    assert ei.value.status == 422


@pytest.mark.asyncio
async def test_resolve_decider_primary_fallback():
    conn = RecConn([("is_primary AND active", 8)])
    assert await d.resolve_decider(conn, None) == 8


@pytest.mark.asyncio
async def test_resolve_decider_none_available_422():
    with pytest.raises(DashboardError) as ei:
        await d.resolve_decider(RecConn(), None)
    assert ei.value.status == 422
    assert str(ei.value) == ("Add yourself on the People page and mark yourself primary "
                             "first, so the change records who decided.")


# ------------------------------------------------------- operator_set_version

def _op_conn(status="draft", decider=5):
    return RecConn([
        ("WHERE slug = $1 FOR UPDATE", {"id": 1, "slug": "sales", "title": "Sales",
                                        "current_version_id": 10}),
        ("WHERE dashboard_id = $1 AND version = $2",
         {"id": 11, "version": 2, "title": "T2", "status": status, "change_note": "cn"}),
        ("FROM dashboards WHERE id = $1 FOR UPDATE", {"id": 1, "current_version_id": 10}),
        ("WHERE id = $2 AND dashboard_id = $1", {"id": 11, "version": 2, "title": "T2", "status": status, "code": clean_page(), "queries": QUERIES}),
        ("SET status = 'retired'", {"id": 10, "version": 1}),
        ("FROM people WHERE id = $1", decider),
        ("is_primary AND active", decider),
        ("INSERT INTO actions", 99),
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("op,ok,bad", [
    ("publish", "draft", ["retired", "published"]),
    ("rollback", "retired", ["draft", "published"]),
])
async def test_operator_status_gate(op, ok, bad):
    out = await d.operator_set_version(_op_conn(ok), slug="sales", version=2, op=op, decided_by=5)
    assert out["action_id"] == 99
    for status in bad:
        with pytest.raises(DashboardError) as ei:
            await d.operator_set_version(_op_conn(status), slug="sales", version=2, op=op,
                                         decided_by=5)
        assert ei.value.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"op": "unpublish"}, {"version": True}, {"version": "2"}, {"slug": "Bad Slug"},
])
async def test_operator_bad_input_422_no_io(kw):
    args = dict(slug="sales", version=2, op="publish")
    args.update(kw)
    conn = _op_conn()
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(conn, **args)
    assert ei.value.status == 422 and conn.calls == []


@pytest.mark.asyncio
async def test_operator_unknown_slug_or_version_404():
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(RecConn(), slug="sales", version=2, op="publish")
    assert ei.value.status == 404
    conn = RecConn([("WHERE slug = $1 FOR UPDATE", {"id": 1, "slug": "sales", "title": "S",
                                                    "current_version_id": None})])
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(conn, slug="sales", version=2, op="publish")
    assert ei.value.status == 404


@pytest.mark.asyncio
async def test_operator_no_decider_refused_no_flip():
    conn = _op_conn(decider=None)
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(conn, slug="sales", version=2, op="rollback"
                                     if False else "publish")
    assert ei.value.status == 422
    assert not any("INSERT INTO actions" in s or "SET current_version_id" in s
                   for s in conn.sqls())


@pytest.mark.asyncio
async def test_operator_call_order_and_audit_row():
    conn = _op_conn("draft")
    out = await d.operator_set_version(conn, slug="sales", version=2, op="publish",
                                       decided_by=5, note="go")
    marks = [c[0] for c in conn.calls]
    assert marks[0] == "BEGIN" and marks[-1] == "END"
    i_lock = conn.index("WHERE slug = $1 FOR UPDATE")
    i_target = conn.index("WHERE dashboard_id = $1 AND version = $2")
    i_dec = conn.index("FROM people WHERE id = $1")
    i_ptr = conn.index("SET current_version_id")
    i_act = conn.index("INSERT INTO actions")
    assert 0 < i_lock < i_target < i_dec < i_ptr < i_act < len(conn.calls) - 1
    assert marks[i_dec + 1] == "BEGIN"            # publish's nested transaction
    assert marks[i_ptr + 1] == "END"
    assert sum("INSERT INTO actions" in s for s in conn.sqls()) == 1
    sql = conn.calls[i_act][1]
    assert "'publish_dashboard'" in sql and "'operator'" in sql and "'executed'" in sql
    payload = json.loads(conn.calls[i_act][2][0])
    assert set(payload) == {"op", "dashboard_id", "slug", "title", "version_id", "version",
                            "from_version_id", "from_version", "change_note", "note",
                            "decided_via"}
    assert payload["decided_via"] == "ops_api" and payload["op"] == "publish"
    assert payload["note"] == "go" and payload["from_version"] == 1
    assert conn.calls[i_act][2][1] == 5
    assert out == {"slug": "sales", "version": 2, "from_version": 1, "action_id": 99,
                   "decided_by": 5}


@pytest.mark.asyncio
async def test_operator_primary_fallback_and_rollback_op():
    conn = _op_conn("retired", decider=8)
    out = await d.operator_set_version(conn, slug="sales", version=2, op="rollback")
    payload = json.loads(conn.calls[conn.index("INSERT INTO actions")][2][0])
    assert payload["op"] == "rollback" and out["decided_by"] == 8
    assert "view_token" not in payload and TOKEN not in json.dumps(payload)


# ------------------------------------------------------------------ rotate_token

@pytest.mark.asyncio
async def test_rotate_token(caplog):
    conn = RecConn([("UPDATE dashboards SET view_token", {
        "slug": "sales", "view_token": TOKEN, "token_rotated_at": "ts"})])
    with caplog.at_level(logging.DEBUG):
        out = await d.rotate_token(conn, slug="sales")
    sql, args = conn.calls[0][1], conn.calls[0][2]
    assert "view_token = $2" in sql and "token_rotated_at = now()" in sql
    assert "RETURNING slug, view_token, token_rotated_at" in sql
    assert args == ("sales", TOKEN)
    assert out == {"slug": "sales", "view_token": TOKEN, "token_rotated_at": "ts",
                   "path": "/d/sales/"}
    assert "sales" in caplog.text and TOKEN not in caplog.text
    assert not any("INSERT INTO actions" in s for s in conn.sqls())


@pytest.mark.asyncio
async def test_rotate_token_unknown_404():
    with pytest.raises(DashboardError) as ei:
        await d.rotate_token(RecConn(), slug="nope")
    assert ei.value.status == 404


# --------------------------------------------------------------------- readers

LIST_ROW = {"id": 1, "slug": "s", "title": "T", "current_version": 2, "current_status": "published",
            "latest_version": 3, "latest_status": "draft", "version_count": 3,
            "current_last_error": None, "last_refreshed_at": None, "updated_at": "now"}


async def test_list_dashboards_hides_code_and_token_and_clamps():
    for given, want in ((0, 1), (10000, 500), (50, 50)):
        conn = RecConn([("FROM dashboards", [LIST_ROW])])
        rows = await d.list_dashboards(conn, limit=given)
        sql, args = conn.calls[0][1], conn.calls[0][2]
        assert "code" not in sql.lower() and "view_token" not in sql
        assert "ORDER BY d.updated_at DESC" in sql
        assert args == (want,)
    assert len(conn.calls) == 2  # the list query plus the Phase 18 format lookup
    assert "code" not in rows[0] and "view_token" not in rows[0]
    assert set(rows[0]) == set(LIST_ROW) | {"current_format", "needs_conversion"}


async def test_list_dashboards_last_refreshed_is_current_version_only():
    conn = RecConn([("FROM dashboards", [])])
    await d.list_dashboards(conn)
    sql = conn.calls[0][1]
    assert "max(" in sql.lower() and "dashboard_snapshots" in sql
    assert "d.current_version_id" in sql


async def test_get_dashboard_unknown_and_invalid_slug():
    with pytest.raises(DashboardError) as e:
        await d.get_dashboard(RecConn(), slug="x")
    assert e.value.status == 404
    with pytest.raises(DashboardError) as e:
        await d.get_dashboard(RecConn(), slug="Bad Slug")
    assert e.value.status == 422


async def test_get_dashboard_shape_and_published_by_sql():
    dash = {"id": 1, "slug": "x", "title": "T", "view_token": "tok", "token_rotated_at": None,
            "current_version": 2, "created_at": "c", "updated_at": "u"}
    ver = [{"version": 2, "status": "published", "title": "T", "change_note": None,
            "author": "agent", "created_at": "c", "published_at": "p", "retired_at": None,
            "last_error": None, "published_by": 5, "published_by_name": "Ann"}]
    conn = RecConn([("FROM dashboards d", dash), ("FROM dashboard_versions v", ver)])
    out = await d.get_dashboard(conn, slug="x")
    assert out["dashboard"]["path"] == "/d/x/"
    assert out["dashboard"]["view_token"] == "tok"
    assert out["versions"][0]["path"] == "/d/x/v/2/"
    assert "code" not in out["versions"][0]
    vsql = [s for s in conn.sqls() if "FROM dashboard_versions v" in s][0]
    assert "a.payload->>'version_id' = v.id::text" in vsql
    assert "::bigint" not in vsql
    assert "LEFT JOIN people" in vsql
    assert "v.code" not in vsql


async def test_get_version_errors():
    with pytest.raises(DashboardError) as e:
        await d.get_version(RecConn(), slug="x", version=1)
    assert e.value.status == 404
    with pytest.raises(DashboardError) as e:
        await d.get_version(RecConn(), slug="x", version=True)
    assert e.value.status == 422
    conn = RecConn([("FROM dashboards", {"id": 1, "current_version_id": 9})])
    with pytest.raises(DashboardError) as e:
        await d.get_version(conn, slug="x", version=2)
    assert e.value.status == 404


async def test_get_version_decodes_queries_and_flags_current():
    row = {"id": 9, "version": 2, "status": "published", "title": "T", "change_note": None,
           "author": "agent", "created_at": "c", "published_at": "p", "retired_at": None,
           "last_error": None, "code": CODE, "script_hashes": ["sha256-x"],
           "queries": json.dumps(GOOD_Q)}
    snaps = [{"query_id": "sales", "refresh_minutes": 15, "row_count": 0, "refreshed_at": None,
              "error": None, "next_refresh_at": "n"}]
    conn = RecConn([("FROM dashboards", {"id": 1, "current_version_id": 9}),
                    ("FROM dashboard_versions", row), ("FROM dashboard_snapshots", snaps)])
    out = await d.get_version(conn, slug="x", version=2)
    assert out["queries"] == GOOD_Q and out["is_current"] is True
    assert out["code"] == CODE and out["script_hashes"] == ["sha256-x"]
    assert out["snapshots"][0]["query_id"] == "sales"
    snap_sql = [s for s in conn.sqls() if "FROM dashboard_snapshots" in s][0]
    assert "data" not in snap_sql.replace("dashboard_snapshots", "")


async def test_get_version_queries_list_passthrough():
    row = {"id": 9, "version": 1, "status": "draft", "title": "T", "change_note": None,
           "author": "agent", "created_at": "c", "published_at": None, "retired_at": None,
           "last_error": None, "code": CODE, "script_hashes": [], "queries": GOOD_Q}
    conn = RecConn([("FROM dashboards", {"id": 1, "current_version_id": None}),
                    ("FROM dashboard_versions", row)])
    out = await d.get_version(conn, slug="x", version=1)
    assert out["queries"] == GOOD_Q and out["is_current"] is False and out["snapshots"] == []


@pytest.mark.asyncio
async def test_publish_precondition_rejects_stale_version():
    conn2 = RecConn([
        ("FROM dashboards", {"id": 1, "current_version_id": 12}),
        ("FROM dashboard_versions", {"id": 11, "version": 3, "title": "T", "status": "retired", "code": clean_page(), "queries": QUERIES}),
    ])
    with pytest.raises(DashboardError) as ei:
        await d.publish(conn2, dashboard_id=1, version_id=11)
    assert ei.value.status == 409
    assert not any(c[1].startswith("UPDATE") for c in conn2.calls)


# ----------------------------------------------------- Quick 029 F4: lint at publish

def _bad_conn(current=10):
    c = _publish_conn(current=current, old={"id": 10, "version": 1})
    c.rules[1] = ("FROM dashboard_versions", {"id": 11, "version": 2, "title": "T2", "status": "draft",
                                              "code": BAD_PAGE, "queries": json.dumps(QUERIES)})
    return c


@pytest.mark.asyncio
async def test_publish_refuses_lint_failing_target_and_updates_nothing():
    conn = _bad_conn()
    with pytest.raises(DashboardError) as ei:
        await d.publish(conn, dashboard_id=1, version_id=11)
    assert ei.value.status == 422
    assert "cannot publish version 2" in str(ei.value) and "tags" in str(ei.value)
    assert not any(s.lstrip().startswith("UPDATE") for s in conn.sqls())


@pytest.mark.asyncio
async def test_publish_clean_target_with_jsonb_text_queries_proceeds():
    conn = _publish_conn(current=10, old={"id": 10, "version": 1})
    conn.rules[1][1]["queries"] = json.dumps(QUERIES)
    assert (await d.publish(conn, dashboard_id=1, version_id=11))["changed"] is True


@pytest.mark.asyncio
async def test_publish_already_current_does_not_lint(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("linted")
    monkeypatch.setattr(d, "lint_refusal", boom)
    conn = _publish_conn(current=11)
    assert (await d.publish(conn, dashboard_id=1, version_id=11))["changed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("op,status", [("publish", "draft"), ("rollback", "retired")])
async def test_operator_set_version_refuses_lint_failing(op, status):
    conn = _op_conn(status)
    for i, (needle, row) in enumerate(conn.rules):
        if needle == "WHERE id = $2 AND dashboard_id = $1":
            conn.rules[i] = (needle, {**row, "code": BAD_PAGE})
    with pytest.raises(DashboardError) as ei:
        await d.operator_set_version(conn, slug="sales", version=2, op=op, decided_by=5)
    assert ei.value.status == 422
    assert not any("INSERT INTO actions" in s for s in conn.sqls())
