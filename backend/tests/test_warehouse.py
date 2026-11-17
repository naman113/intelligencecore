"""Pure unit tests for app.warehouse (Phase 14, WH-02 / WH-03)."""
import logging
import secrets

import pytest

from app import secretbox, warehouse
from app.config import get_settings

ENVS = ("WAREHOUSE_URL", "WH_READER_PASSWORD", "WH_OWNER_PASSWORD", "WH_LOADER_PASSWORD",
        "WAREHOUSE_ADMIN_USER", "WAREHOUSE_ADMIN_PASSWORD")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    warehouse._reset_state_for_tests()
    yield
    get_settings.cache_clear()
    warehouse._reset_state_for_tests()


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_mode_needs_both(monkeypatch):
    assert warehouse.warehouse_mode() is False
    _set(monkeypatch, WAREHOUSE_URL="postgresql://h:1/warehouse")
    assert warehouse.warehouse_mode() is False
    monkeypatch.delenv("WAREHOUSE_URL")
    _set(monkeypatch, WH_READER_PASSWORD="pw")
    assert warehouse.warehouse_mode() is False
    _set(monkeypatch, WAREHOUSE_URL="postgresql://h:1/warehouse")
    assert warehouse.warehouse_mode() is True


def test_connect_kwargs_reader_and_strip(monkeypatch):
    _set(monkeypatch, WAREHOUSE_URL="postgresql+asyncpg://bob:sekret@host:5434/warehouse",
         WH_READER_PASSWORD="rpw", WH_OWNER_PASSWORD="opw", WAREHOUSE_ADMIN_PASSWORD="apw")
    k = warehouse.connect_kwargs("reader")
    assert k == {"dsn": "postgresql://host:5434/warehouse", "user": "wh_reader", "password": "rpw"}
    assert "sekret" not in k["dsn"]
    assert warehouse.connect_kwargs("owner")["password"] == "opw"
    a = warehouse.connect_kwargs("admin")
    assert (a["user"], a["password"]) == ("wh_admin", "apw")
    r = warehouse.connect_kwargs("reader", warehouse.Roles(reader="x_reader"))
    assert r["user"] == "x_reader"
    with pytest.raises(ValueError):
        warehouse.connect_kwargs("nobody")


def test_connect_kwargs_unconfigured():
    with pytest.raises(warehouse.WarehouseNotConfigured):
        warehouse.connect_kwargs("reader")


class FakeConn:
    def __init__(self):
        self.fetched, self.executed = [], []

    async def fetchval(self, q, *a):
        self.fetched.append((q, a))
        return "ALTER ROLE built PASSWORD 'built-secret'"

    async def execute(self, s, *a):
        self.executed.append(s)


async def test_exec_fmt_runs_server_built_statement(caplog):
    c = FakeConn()
    with caplog.at_level(logging.DEBUG):
        await warehouse.exec_fmt(c, "ALTER ROLE %I PASSWORD %L", "wh_reader", "s3cret-value")
    assert c.fetched[0][0] == "SELECT format($1, VARIADIC $2::text[])"
    assert c.fetched[0][1] == ("ALTER ROLE %I PASSWORD %L", ["wh_reader", "s3cret-value"])
    assert c.executed == ["ALTER ROLE built PASSWORD 'built-secret'"]
    assert "s3cret-value" not in caplog.text and "built-secret" not in caplog.text


def test_reader_server_settings():
    assert warehouse.reader_server_settings() == {
        "default_transaction_read_only": "on", "statement_timeout": "30000"}
    warehouse.mark_linked({"TimeZone": "Asia/Kolkata", "search_path": "public, reporting"})
    s = warehouse.reader_server_settings()
    assert s["TimeZone"] == "Asia/Kolkata" and s["search_path"] == "public, reporting"


def test_request_relink_debounce():
    warehouse.request_relink("x")
    warehouse.request_relink("x")
    assert len(warehouse._relink_requests) == 1
    assert warehouse._bootstrap_wanted is False
    warehouse.request_relink("auth", bootstrap=True)
    warehouse.request_relink("auth", bootstrap=True)
    assert len(warehouse._relink_requests) == 3
    assert warehouse._bootstrap_wanted is True


async def test_wait_linked_latch():
    assert await warehouse.wait_linked(0.05) is False
    assert await warehouse.wait_linked(5) is False  # immediate, latched
    warehouse.mark_linked()
    assert await warehouse.wait_linked(5) is True


async def test_wait_linked_after_mark():
    warehouse.mark_linked()
    assert await warehouse.wait_linked(0.01) is True


def test_passwords_redacted(monkeypatch):
    pws = {k: secrets.token_hex(24) for k in
           ("WAREHOUSE_ADMIN_PASSWORD", "WH_OWNER_PASSWORD", "WH_LOADER_PASSWORD", "WH_READER_PASSWORD")}
    _set(monkeypatch, **pws)
    secretbox.refresh_known_secrets()
    for pw in pws.values():
        out = secretbox.redact(f"x {pw} y")
        assert pw not in out
    rec = logging.LogRecord("t", logging.INFO, "f", 1, "pw %s", (pws["WH_READER_PASSWORD"],), None)
    secretbox.RedactFilter().filter(rec)
    assert pws["WH_READER_PASSWORD"] not in rec.getMessage()


async def test_bootstrap_skips_without_admin_password(monkeypatch, caplog):
    _set(monkeypatch, WAREHOUSE_URL="postgresql://h:1/warehouse")
    with caplog.at_level(logging.WARNING):
        out = await warehouse.bootstrap()
    assert "skipped" in out
    assert "skipped" in caplog.text


# ── Linker pure logic (Phase 14-04, WH-04 / WH-05) ──────────────────────────
from app import whlink  # noqa: E402


def test_linker_parse_basic_and_no_secret_in_repr():
    o = whlink.parse_source_url("postgresql://demo_ro:demo_ro@ic-demo-db:5432/demo_business")
    assert (o.host, o.port, o.dbname, o.user, o.password) == (
        "ic-demo-db", "5432", "demo_business", "demo_ro", "demo_ro")
    o2 = whlink.parse_source_url("postgresql://u:SECRETPW@h/db")
    assert "SECRETPW" not in repr(o2) and "SECRETPW" not in str(o2)


def test_linker_parse_percent_ipv6_default_port_and_prefixes():
    o = whlink.parse_source_url("postgresql://u:p%40ss%3Aw@h/db")
    assert o.password == "p@ss:w" and o.port == "5432"
    o = whlink.parse_source_url("postgresql://u:p@[::1]:6543/db")
    assert (o.host, o.port) == ("::1", "6543")
    for pre in ("postgresql+asyncpg://", "postgres://"):
        assert whlink.parse_source_url(pre + "u:p@h/db").host == "h"


def test_linker_parse_multi_host():
    o = whlink.parse_source_url("postgresql://u:p@h1:5432,h2:5433/db")
    assert o.host == "h1,h2" and o.port == "5432,5433"


def test_linker_parse_extra_allowlist(caplog):
    with caplog.at_level(logging.INFO, logger="whlink"):
        o = whlink.parse_source_url(
            "postgresql://u:SECRETPW@h/db?sslmode=require&target_session_attrs=any&options=-c%20x")
    assert dict(o.extra) == {"sslmode": "require", "target_session_attrs": "any"}
    assert "options" in caplog.text and "SECRETPW" not in caplog.text and "-c" not in caplog.text


def test_linker_parse_rejects_without_leaking():
    for bad in ("postgresql://u:SECRETPW@/var/run/postgresql/db",
                "postgresql://u:SECRETPW@%2Fvar%2Frun/db",
                "postgresql://u:SECRETPW@h/",
                "postgresql://u:SECRETPW@h/db?sslmode=bogus"):
        with pytest.raises(ValueError) as e:
            whlink.parse_source_url(bad)
        assert "SECRETPW" not in str(e.value) and "postgresql://" not in str(e.value)


def test_linker_rewrite_loopback():
    for h in ("localhost", "127.0.0.1", "127.0.1.1", "::1"):
        assert whlink.rewrite_loopback(h) == ("host.docker.internal", True)
    assert whlink.rewrite_loopback("db.example.com") == ("db.example.com", False)


def _T(typname, typtype="b", nspname="public", typelem=0, typbasetype=0, base_fmt=None,
       labels=None, ext=None, visible=True):
    return {"typname": typname, "nspname": nspname, "typtype": typtype, "typelem": typelem,
            "typbasetype": typbasetype, "base_fmt": base_fmt, "labels": labels, "ext": ext,
            "visible": visible}


def _tmap():
    return {
        100: _T("mood", "e", labels=["ok", "bad"]),
        101: _T("_mood", "b", typelem=100),
        102: _T("dom_mood", "d", typbasetype=100),
        103: _T("_dom_mood", "b", typelem=102),
        110: _T("code", "d", typbasetype=23, base_fmt="character varying(10)"),
        111: _T("code2", "d", typbasetype=110),
        112: _T("_code2", "b", typelem=111),
        120: _T("pair", "c"), 121: _T("rng", "r"), 122: _T("mrng", "m"), 123: _T("anyx", "p"),
        124: _T("weird", "b"),
        130: _T("vector", "b", ext="vector"), 131: _T("_vector", "b", typelem=130),
        132: _T("dvec", "d", typbasetype=130), 133: _T("hid", "b", ext="hstore", visible=False),
        140: _T("loop", "d", typbasetype=140),
        150: _T("citext", "b", ext="citext"), 151: _T("_citext", "b", typelem=150),
        152: _T("dcit", "d", typbasetype=150), 153: _T("ci_hid", "b", ext="citext", visible=False),
        154: _T("isn", "b", ext="isn"),
    }


def test_linker_resolve_type():
    r = whlink.resolve_type
    tm = _tmap()
    x = r(100, tm)
    assert (x["rkind"], x["enum_key"], x["enum_labels"], x["is_array"]) == (
        "enum", "public.mood", ["ok", "bad"], False)
    for tid in (101, 102, 103):
        x = r(tid, tm)
        assert x["rkind"] == "enum" and x["enum_key"] == "public.mood"
        assert x["enum_labels"] == ["ok", "bad"]
        assert x["is_array"] == (tid in (101, 103))
    x = r(110, tm)
    assert (x["rkind"], x["rbase"], x["is_array"]) == ("builtin", "character varying(10)", False)
    assert r(111, tm)["rbase"] == "character varying(10)"
    x = r(112, tm)
    assert x["rkind"] == "builtin" and x["rbase"] == "character varying(10)" and x["is_array"]
    for tid in (130, 131, 132, 154):
        x = r(tid, tm)
        assert x["rkind"] == "unlinked"
        assert x["reason"].startswith("extension type has no local equivalent: ")
    assert r(154, tm)["reason"].endswith("isn")
    for tid, why in ((120, "composite type is not linked"), (121, "range type is not linked"),
                     (122, "multirange type is not linked"), (123, "pseudo type is not linked"),
                     (124, "base type outside an extension is not linked"),
                     (999, "type not found in source catalog"),
                     (140, "domain chain too deep")):
        x = r(tid, tm)
        assert x["rkind"] == "unlinked" and x["reason"] == why


def test_linker_fdw_type():
    f = whlink.fdw_type
    tm = _tmap()
    for t in ("numeric(12,2)", "timestamp with time zone", "integer[]"):
        assert f({"type": t, "type_builtin": True}) == t

    def col(tid):
        return {"type": "x", "type_builtin": False, **whlink.resolve_type(tid, tm)}
    assert f(col(100)) == "enumn:public.mood"
    assert f(col(101)) == "enumn:public.mood[]"
    assert f(col(103)) == "enumn:public.mood[]"
    assert f(col(110)) == "character varying(10)"
    assert f(col(112)) == "character varying(10)[]"
    for tid in (120, 121, 122, 123, 124, 130, 999):
        assert f(col(tid)) is None


def test_linker_linkable_columns():
    tm = _tmap()
    cols = [{"name": "id", "type": "integer", "type_builtin": True},
            {"name": "cp", "type": "pair", "type_builtin": False, **whlink.resolve_type(120, tm)},
            {"name": "m", "type": "mood", "type_builtin": False, **whlink.resolve_type(100, tm)}]
    linked, unl = whlink.linkable_columns(cols)
    assert [c["name"] for c in linked] == ["id", "m"]
    assert unl == [{"column": "cp", "type": "pair", "reason": "composite type is not linked"}]


def test_linker_col_hash_builtin_stable():
    c = [{"name": "id", "type": "integer", "type_builtin": True},
         {"name": "amt", "type": "numeric(12,2)", "type_builtin": True}]
    assert whlink.col_hash(c) == (
        "97574d6bd08e3a1cd3855b6fb437ef318cc12e10d7a47e0c7b27a138c8d462b6")


def _cols(**over):
    base = [{"name": "id", "type": "integer", "nullable": False, "pk": True, "fk": None,
             "comment": None, "type_builtin": True},
            {"name": "m", "type": "mood", "nullable": True, "pk": False, "fk": None,
             "comment": "c", "type_builtin": False, "rkind": "enum",
             "enum_key": "public.mood", "enum_labels": ["ok", "bad"], "is_array": False}]
    return base


def test_linker_col_hash():
    h = whlink.col_hash(_cols())
    c = _cols(); c[1]["comment"] = "other"; c[1]["nullable"] = False; c[0]["pk"] = False
    c[0]["fk"] = {"table": "t", "column": "id"}
    assert whlink.col_hash(c) == h
    c = _cols(); c.append({"name": "z", "type": "text", "type_builtin": True})
    assert whlink.col_hash(c) != h
    assert whlink.col_hash(_cols()[:1]) != h
    c = _cols(); c[0]["name"] = "id2"
    assert whlink.col_hash(c) != h
    c = _cols(); c[0]["type"] = "bigint"
    assert whlink.col_hash(c) != h


def _row():
    return {"relkind": "r", "table_comment": None, "row_estimate": 10, "checks": [],
            "columns": _cols()}


def test_linker_catalog_row_hash():
    base = whlink.catalog_row_hash(_row())
    r = _row(); r["columns"] = list(reversed(r["columns"][::-1]))
    assert whlink.catalog_row_hash({k: _row()[k] for k in reversed(list(_row()))}) == base
    changes = [
        lambda r: r.update(relkind="v"),
        lambda r: r.update(table_comment="x"),
        lambda r: r.update(row_estimate=11),
        lambda r: r.update(checks=["CHECK (a)"]),
        lambda r: r["columns"][0].update(nullable=True),
        lambda r: r["columns"][0].update(pk=False),
        lambda r: r["columns"][0].update(fk={"table": "t", "column": "id"}),
        lambda r: r["columns"][1].update(comment="zz"),
        lambda r: r["columns"][1].update(name="m2"),
        lambda r: r["columns"][1].update(type="mood2"),
    ]
    seen = {base}
    for ch in changes:
        r = _row(); ch(r)
        h = whlink.catalog_row_hash(r)
        assert h not in seen
        seen.add(h)


def test_linker_plan_changes_deterministic():
    snap = [{"schema_name": "public", "table_name": "b", "col_hash": "1"},
            {"schema_name": "public", "table_name": "a", "col_hash": "1"},
            {"schema_name": "public", "table_name": "c", "col_hash": "2"},
            {"schema_name": "reporting", "table_name": "d", "col_hash": "1"}]
    linked = [{"remote_schema": "public", "remote_table": "a", "col_hash": "1"},
              {"remote_schema": "public", "remote_table": "c", "col_hash": "9"},
              {"remote_schema": "public", "remote_table": "gone2", "col_hash": "1"},
              {"remote_schema": "public", "remote_table": "gone1", "col_hash": "1"}]
    p = whlink.plan_changes(snap, linked)
    assert p["create"] == [("public", "b"), ("reporting", "d")]
    assert p["replace"] == [("public", "c")]
    assert p["drop"] == [("public", "gone1"), ("public", "gone2")]
    assert p["unchanged"] == 1


def test_linker_schema_names():
    import hashlib
    s = whlink.private_schema("business_db", "public")
    assert s == "ic_fdw_business_db_" + hashlib.md5(b"public").hexdigest()[:12]
    assert len(whlink.private_schema("x" * 100, "public")) <= 63
    assert whlink.catalog_schema("business_db") == "ic_fdw_business_db_catalog"
    for bad in ("ic_meta", "ic_anything", "pg_temp", "pg_catalog", "information_schema", ""):
        assert not whlink.mirrored_schema_allowed(bad)
    assert whlink.mirrored_schema_allowed("public") and whlink.mirrored_schema_allowed("reporting")


def test_linker_search_path_from():
    f = whlink.search_path_from
    assert f('"$user", public', "demo_ro", {"public"}) == "public"
    assert f('"$user", public', "demo_ro", {"public", "demo_ro"}) == "demo_ro, public"
    assert f("pg_catalog, ic_meta, public", "u", {"public", "ic_meta"}) == "public"
    assert f("", "u", {"public"}) == "public"
    assert f("other", "u", {"public"}) == "public"
    assert f('"My Schema", public', "u", {"My Schema", "public"}) == '"My Schema", public'


def test_linker_type_search_path():
    f = whlink.type_search_path
    assert f('"$user", public', "app", {"app", "public", "pg_catalog"}) == ["pg_catalog", "app", "public"]
    assert f('"$user", public', "app", {"public"}) == ["pg_catalog", "public"]
    assert f("public, pg_catalog", "r", {"public", "pg_catalog"}) == ["public", "pg_catalog"]
    assert f('"Sales", public', "r", {"Sales", "public"}) == ["pg_catalog", "Sales", "public"]
    assert f("public, sales, nousage", "r", {"public", "sales"}) == ["pg_catalog", "public", "sales"]
    assert f("public, public, sales", "r", {"public", "sales"}) == ["pg_catalog", "public", "sales"]
    assert f("", "r", set()) == ["pg_catalog"]
    assert f(None, "", set()) == ["pg_catalog"]
    assert f("pg_temp, pg_catalog, public", "r", {"public"}) == ["pg_catalog", "public"]
    assert f('"$user"', "", {"public"}) == ["pg_catalog"]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.1.1", "::1"])
def test_linker_rewrite_options_each_loopback(host):
    o = whlink.rewrite_options(whlink.LinkOptions(host, "5432", "d", "u", "p"))
    assert o.host == "host.docker.internal" and o.host_rewritten is True


def test_linker_rewrite_options_multi_and_untouched():
    o = whlink.LinkOptions("db1,localhost", "5432,5432", "d", "u", "p")
    r = whlink.rewrite_options(o)
    assert r.host == "db1,host.docker.internal" and r.host_rewritten
    o = whlink.LinkOptions("db1", "5432", "d", "u", "p")
    assert whlink.rewrite_options(o) is o


@pytest.mark.parametrize("bad", ["mysql://u:p@h/d", "", "postgresql://u:p@h:99999/d",
                                 "postgresql://:p@h/d"])
def test_linker_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        whlink.parse_source_url(bad)


def test_linker_private_schema_stable_per_remote_schema():
    assert whlink.private_schema("business_db", "a") != whlink.private_schema("business_db", "b")
    assert whlink.private_schema("business_db", "a") == whlink.private_schema("business_db", "a")


# ── Phase 14-05: warehouse loop, relink_now, ensure_fresh ──
import asyncio  # noqa: E402


class _FakeConn:
    def __init__(self, meta_missing=False):
        self.meta_missing = meta_missing
        self.closed = False

    async def fetchval(self, sql, *a):
        return self.meta_missing

    async def fetchrow(self, sql, *a):
        return {"status": "ok", "source_timezone": "Asia/Kolkata", "source_search_path": "public"}

    async def close(self):
        self.closed = True


@pytest.fixture
def loop_env(monkeypatch):
    from app import guardrails, whlink
    _set(monkeypatch, WAREHOUSE_URL="postgresql://h:1/warehouse", WH_READER_PASSWORD="r",
         TENANT_DATABASE_URL="postgresql://u:p@db:5432/biz")
    st = {"boot": 0, "link": 0, "connect": [], "reset": 0, "meta_missing": False,
          "fail_login": None, "delay": 0.0, "result": None}

    async def fake_connect(role, **kw):
        st["connect"].append(role)
        if st["fail_login"] == role:
            import asyncpg
            raise asyncpg.InvalidPasswordError("password authentication failed")
        return _FakeConn(st["meta_missing"] and role == "owner")

    async def fake_boot(*a, **k):
        st["boot"] += 1
        st["fail_login"] = None
        st["meta_missing"] = False
        return {"roles": []}

    async def fake_link(conn, source, url, roles=warehouse.DEFAULT_ROLES):
        st["link"] += 1
        if st["delay"]:
            await asyncio.sleep(st["delay"])
        return st["result"] or {"status": "ok", "tables_linked": 9,
                                "session": {"TimeZone": "UTC", "search_path": "public"}}

    async def fake_reset():
        st["reset"] += 1

    monkeypatch.setattr(warehouse, "connect", fake_connect)
    monkeypatch.setattr(warehouse, "bootstrap", fake_boot)
    monkeypatch.setattr(whlink, "link_source", fake_link)
    async def fake_keep():
        return frozenset()

    monkeypatch.setattr(warehouse, "_load_keep_keys", fake_keep)
    monkeypatch.setattr(guardrails, "reset_business_pool", fake_reset)
    return st


async def test_tick_not_in_warehouse_mode_does_nothing(monkeypatch):
    calls = []

    async def fake_connect(*a, **k):
        calls.append(1)

    monkeypatch.setattr(warehouse, "connect", fake_connect)
    await warehouse._tick()
    assert calls == []


async def test_tick_first_bootstraps_then_links(loop_env):
    await warehouse._tick()
    assert loop_env["boot"] == 1 and loop_env["link"] == 1 and loop_env["reset"] == 1
    assert warehouse._linked is True
    assert warehouse._session["TimeZone"] == "UTC"
    assert warehouse.last_link()["status"] == "ok"


async def test_tick_second_within_sweep_does_not_relink(loop_env):
    await warehouse._tick()
    await warehouse._tick()
    assert loop_env["link"] == 1 and loop_env["boot"] == 1
    warehouse.request_relink("setup")
    await warehouse._tick()
    assert loop_env["link"] == 2


async def test_tick_relinks_after_sweep_interval(loop_env, monkeypatch):
    await warehouse._tick()
    warehouse._last_sweep_at -= 10_000
    await warehouse._tick()
    assert loop_env["link"] == 2


async def test_tick_owner_login_failure_rebootstraps(loop_env):
    await warehouse._tick()
    loop_env["fail_login"] = "owner"
    warehouse._last_sweep_at -= 10_000
    await warehouse._tick()
    assert loop_env["boot"] == 2 and loop_env["link"] == 2


async def test_tick_reader_login_failure_rebootstraps(loop_env):
    await warehouse._tick()
    loop_env["fail_login"] = "reader"
    await warehouse._tick()
    assert loop_env["boot"] == 2 and loop_env["link"] == 2


async def test_tick_missing_ic_meta_bootstraps_and_links(loop_env):
    await warehouse._tick()
    loop_env["meta_missing"] = True
    await warehouse._tick()
    assert loop_env["boot"] == 2 and loop_env["link"] == 2


async def test_tick_without_business_url_bootstraps_only(loop_env, monkeypatch):
    monkeypatch.delenv("TENANT_DATABASE_URL")
    monkeypatch.setenv("BUSINESS_DATABASE_URL", "")
    get_settings.cache_clear()
    await warehouse._tick()
    assert loop_env["boot"] == 1 and loop_env["link"] == 0
    assert warehouse._linked is False


async def test_relink_now_not_configured(loop_env, monkeypatch):
    monkeypatch.delenv("TENANT_DATABASE_URL")
    monkeypatch.setenv("BUSINESS_DATABASE_URL", "")
    get_settings.cache_clear()
    assert (await warehouse.relink_now("setup"))["status"] == "not_configured"
    assert (await warehouse.ensure_fresh())["status"] == "not_configured"
    assert loop_env["link"] == 0


async def test_relink_now_links_and_times_out_without_raising(loop_env):
    r = await warehouse.relink_now("setup")
    assert r["status"] == "ok" and loop_env["link"] == 1
    loop_env["delay"] = 1.0
    r = await warehouse.relink_now("setup", timeout=0.1)
    assert r["status"] == "pending"  # WR-05: the link keeps running, the loop is told it is due


async def test_relink_now_owner_login_error_never_raises(loop_env):
    loop_env["fail_login"] = "owner"
    r = await warehouse.relink_now("setup")
    assert r["status"] == "error"


async def test_fresh_default_relinks_every_call(loop_env):
    await warehouse.ensure_fresh()
    await warehouse.ensure_fresh()
    assert loop_env["link"] == 2


async def test_fresh_max_age_skips_young_link(loop_env):
    await warehouse.ensure_fresh()
    r = await warehouse.ensure_fresh(max_age=60)
    assert r["status"] == "fresh" and loop_env["link"] == 1


async def test_fresh_waits_for_inflight_link_then_reports_fresh(loop_env):
    loop_env["delay"] = 0.2
    first = asyncio.create_task(warehouse.relink_now("sweep"))
    await asyncio.sleep(0.05)
    r = await warehouse.ensure_fresh()
    await first
    assert r["status"] == "fresh" and loop_env["link"] == 1


async def test_fresh_timeout_status(loop_env):
    loop_env["delay"] = 1.0
    r = await warehouse.ensure_fresh(timeout=0.1)
    assert r["status"] == "timeout"


async def test_load_link_state_marks_linked(loop_env):
    assert await warehouse.load_link_state() is True
    assert warehouse._linked is True and warehouse._session["TimeZone"] == "Asia/Kolkata"


def test_linker_ext_types():
    assert whlink.TYPE_EXTENSIONS == ("citext", "hstore", "ltree", "cube")
    assert whlink.TYPES_SCHEMA == "ic_types"
    tm = _tmap()
    x = whlink.resolve_type(150, tm)
    assert (x["rkind"], x["ext_name"], x["ext_typname"], x["ext_visible"], x["is_array"]) == (
        "ext", "citext", "citext", True, False)
    x = whlink.resolve_type(151, tm)
    assert x["rkind"] == "ext" and x["is_array"] and x["ext_visible"] is True
    assert whlink.resolve_type(152, tm)["rkind"] == "ext"
    x = whlink.resolve_type(153, tm)
    assert x["rkind"] == "ext" and x["ext_visible"] is False
    assert whlink.resolve_type(133, tm)["ext_name"] == "hstore"
    assert whlink.fdw_type(whlink.resolve_type(150, tm)) == "ic_types.citext"
    assert whlink.fdw_type(whlink.resolve_type(151, tm)) == "ic_types.citext[]"
    assert whlink.resolve_type(130, tm)["rkind"] == "unlinked"


def test_linker_ext_path_decision():
    def col(vis):
        return {"name": "c", "rkind": "ext", "ext_visible": vis, "ext_typname": "citext"}
    tabs = [{"columns": [col(True), col(False), {"name": "i", "rkind": "builtin"}]}]
    assert whlink.ext_path_decision(tabs) is True
    assert tabs[0]["columns"][0]["rkind"] == "ext"
    assert tabs[0]["columns"][1]["rkind"] == "unlinked"
    assert "hidden from the source role" in tabs[0]["columns"][1]["reason"]
    tabs = [{"columns": [col(False)]}]
    assert whlink.ext_path_decision(tabs) is False
    assert tabs[0]["columns"][0]["rkind"] == "ext"
    assert whlink.ext_path_decision([{"columns": []}]) is False


def test_linker_session_search_path():
    f = whlink.session_search_path
    assert f("public", True) == "public, ic_types"
    assert f("public", False) == "public"
    assert f("public, ic_types", True) == "public, ic_types"
    assert f('"Sales", public', True) == '"Sales", public, ic_types'


# ── Phase 15 (Phase 14 gap A): typmod output names ──
# Vectors captured 2026-10-26 from a postgis/postgis:16-3.5 container:
# (atttypmod, format_type output). Typmod out functions there: geometry_typmod_out,
# geography_typmod_out.
_POSTGIS_VECTORS = [
    (1107460, "geometry(Point,4326)"),
    (1107462, "geometry(PointZ,4326)"),
    (7, "geometry(PointZM)"),
    (1107480, "geography(MultiPolygon,4326)"),
    (987392, "geometry(Geometry,3857)"),
    (-1, "geometry"),
    (12, "geometry(Polygon)"),
    (1107461, "geometry(PointM,4326)"),
    (8, "geometry(LineString)"),
]


def test_linker_postgis_typmod_out_vectors():
    for tm, want in _POSTGIS_VECTORS:
        base = want.split("(")[0]
        assert base + whlink.postgis_typmod_out(tm) == want


def test_linker_compose_user_type():
    f = whlink.compose_user_type
    bo = {("timestamptztypmodout", 3): "(3) with time zone"}
    assert f("tstz2", 3, "timestamptztypmodout", False, bo) == (
        "tstz2(3) with time zone", None)
    assert f("tstz2", 3, "timestamptztypmodout", True, bo) == (
        "tstz2(3) with time zone[]", None)
    assert f("geometry", 1107460, "public.geometry_typmod_out", True, {}) == (
        "geometry(Point,4326)[]", None)
    assert f("geography", 1107480, "postgis.geography_typmod_out", False, {}) == (
        "geography(MultiPolygon,4326)", None)
    assert f("geometry", 1107460, "public.postgis_typmod_out", False, {})[0] == (
        "geometry(Point,4326)")
    assert f("vector", 3, "-", False, {}) == ("vector(3)", None)
    assert f("vector", 3, None, True, {}) == ("vector(3)[]", None)
    assert f("tstz2", -1, "timestamptztypmodout", False, bo) == ("tstz2", None)
    assert f("tstz2", -1, "public.x", True, bo) == ("tstz2[]", None)


def test_linker_compose_user_type_not_allowlisted():
    f = whlink.compose_user_type
    assert f("t", 10, "public.my_typmod_out", False, {}) == ("t(10)", "public.my_typmod_out")
    assert f("t", 10, "evil_out", False, {}) == ("t(10)", "evil_out")
    # allowlisted name but no captured output: treated as unsupported, never executed
    assert f("t", 10, "varchartypmodout", False, {}) == ("t(10)", "varchartypmodout")


def test_linker_typmod_dash_hash_unchanged():
    # a '-' typmodout column keeps the pre-change type string and so the same col_hash
    name, bad = whlink.compose_user_type("public.vector", 3, "-", False, {})
    assert (name, bad) == ("public.vector(3)", None)
    c = [{"name": "e", "type": name, "type_builtin": False, "rkind": "unlinked"}]
    old = [{"name": "e", "type": "public.vector(3)", "type_builtin": False, "rkind": "unlinked"}]
    assert whlink.col_hash(c) == whlink.col_hash(old)


# ── Phase 15 (Phase 14 gap B): source type names, pure part ──
def _tm_b():
    return {
        100: _T("mood", "e", labels=["ok", "bad"]),
        101: _T("_mood", "b", typelem=100),
        102: _T("dom_mood", "d", typbasetype=100),
        110: _T("code", "d", typbasetype=23, base_fmt="character varying(10)"),
        111: _T("code2", "d", typbasetype=110),
        112: _T("OrderStatus", "e", nspname="sales", labels=["new", "paid"]),
        113: _T("hid", "e", nspname="nousage", labels=["a"]),
        114: _T("dom_hid", "d", nspname="public", typbasetype=113),
        115: _T("res", "e", nspname="ic_types", labels=["a"]),
        116: _T("dom_res", "d", typbasetype=115),
        120: _T("pair", "c"),
        121: _T("dom_pair", "d", typbasetype=120),
        130: _T("citext", "b", ext="citext"),
        131: _T("dom_ci", "d", typbasetype=130),
        132: _T("mood_ext", "e", ext="someext", labels=["x"]),
        133: _T("dom_arr", "d", typbasetype=23, base_fmt="integer[]"),
    }


def test_linker_user_types_from():
    ut, unsup = whlink.user_types_from(_tm_b(), {"public", "sales"})
    keys = [(t["schema"], t["name"], t["kind"]) for t in ut]
    assert keys == [("nousage", "hid", "enum"), ("public", "code", "domain"),
                    ("public", "code2", "domain"), ("public", "dom_arr", "domain"),
                    ("public", "dom_ci", "domain"),
                    ("public", "dom_hid", "domain"), ("public", "dom_mood", "domain"),
                    ("public", "mood", "enum"), ("sales", "OrderStatus", "enum")]
    d = {(t["schema"], t["name"]): t for t in ut}
    assert d[("public", "mood")]["labels"] == ["ok", "bad"]
    assert d[("public", "code2")]["base"] == {"rkind": "builtin",
                                              "rbase": "character varying(10)",
                                              "is_array": False}
    assert d[("public", "dom_mood")]["base"]["enum_schema"] == "public"
    # base enum of a mirrored domain is included although its schema is not usable
    assert ("nousage", "hid") in d
    assert d[("public", "dom_ci")]["base"]["ext_name"] == "citext"
    assert d[("public", "dom_arr")]["base"]["is_array"] is True
    # extension member, composite, unusable schema, reserved base
    assert ("public", "mood_ext") not in d and ("ic_types", "res") not in d
    assert ("public", "dom_pair") not in d and ("public", "dom_res") not in d
    reasons = {(u["schema"], u["name"]): u["reason"] for u in unsup}
    assert reasons[("public", "dom_pair")] == "composite type is not linked"
    assert reasons[("public", "dom_res")] == "base enum is in a reserved schema"
    ut2, _ = whlink.user_types_from(_tm_b(), {"public"})
    assert ("sales", "OrderStatus") not in {(t["schema"], t["name"]) for t in ut2}


def test_linker_user_types_cap(caplog):
    tm = {1000 + i: _T(f"t{i:04d}", "e", labels=["a"]) for i in range(whlink.MAX_MIRRORED_TYPES + 7)}
    with caplog.at_level("WARNING", logger="whlink"):
        ut, _ = whlink.user_types_from(tm, {"public"})
    assert len(ut) == whlink.MAX_MIRRORED_TYPES
    assert ut[0]["name"] == "t0000"
    assert sum("beyond the first" in r.message for r in caplog.records) == 1
    assert "7 user types" in caplog.text


def test_linker_type_def_hash():
    h = whlink.type_def_hash
    e = {"kind": "enum", "labels": ["a", "b"], "base": None}
    assert h(e) == h(dict(e))
    assert h(e) != h({**e, "labels": ["a", "b", "c"]})
    assert h(e) != h({**e, "labels": ["a", "x"]})
    assert h(e) != h({**e, "labels": ["b", "a"]})
    d = {"kind": "domain", "labels": None, "base": {"rkind": "builtin", "rbase": "text",
                                                    "is_array": False}}
    assert h(d) == h(dict(d))
    assert h(d) != h({**d, "base": {"rkind": "builtin", "rbase": "character varying(5)",
                                    "is_array": False}})
    assert h(d) != h(e)


def test_linker_mirror_plan():
    a = {"schema": "public", "name": "a", "kind": "enum", "labels": ["x"], "base": None}
    b = {"schema": "public", "name": "b", "kind": "enum", "labels": ["x", "y"], "base": None}
    c = {"schema": "s", "name": "c", "kind": "enum", "labels": ["z"], "base": None}
    stored = {("public", "a"): whlink.type_def_hash(a),
              ("public", "b"): whlink.type_def_hash({**b, "labels": ["x"]}),
              ("s", "gone"): "h"}
    p = whlink.mirror_plan([a, b, c], stored)
    assert [t["name"] for t in p["create"]] == ["c"]
    assert [t["name"] for t in p["replace"]] == ["b"]
    assert [t["name"] for t in p["unchanged"]] == ["a"]
    assert p["drop"] == [("s", "gone")]


def test_linker_enumn_token_changes_enum_table_hash_only():
    tm = _tmap()
    enum_col = {"name": "m", "type": "mood", "type_builtin": False, **whlink.resolve_type(100, tm)}
    assert whlink.fdw_type(enum_col).startswith("enumn:")
    assert enum_col["enum_schema"] == "public" and enum_col["enum_name"] == "mood"


# ── Phase 15 (Phase 14 gap B): recording fake connection for the DDL part ──
class _TypeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _TypeConn:
    """Answers the few catalog lookups the type mirrors make and records every statement."""

    def __init__(self, stored=None, existing_types=()):
        self.sql: list[str] = []
        self.stored = dict(stored or {})  # (schema, name) -> (kind, hash)
        self.existing = set(existing_types)

    def transaction(self):
        return _TypeTx()

    async def execute(self, q, *a):
        self.sql.append(q)

    async def fetchval(self, q, *a):
        if q.startswith("SELECT format($1"):
            t, *vals = a
            if vals and isinstance(vals[0], list):
                vals = vals[0]
            it = iter(vals)
            import re as _re
            return _re.sub(r"%([IL])", lambda m: ('"%s"' % next(it).replace('"', '""'))
                           if m.group(1) == "I" else "'%s'" % next(it).replace("'", "''"), t)
        if "to_regtype" in q:
            return "x" if (a[0], a[1]) in self.existing else None
        if "FROM pg_namespace" in q:
            return None
        if "has_schema_privilege" in q:
            return False
        return None

    async def fetch(self, q, *a):
        if "FROM ic_meta.link_types" in q:
            return [{"schema_name": k[0], "type_name": k[1], "kind": v[0], "def_hash": v[1]}
                    for k, v in self.stored.items()]
        return []

    async def fetchrow(self, q, *a):
        return None


def _enum(schema, name, labels):
    return {"schema": schema, "name": name, "kind": "enum", "labels": labels, "base": None,
            "visible": True}


def _dom(schema, name, base):
    return {"schema": schema, "name": name, "kind": "domain", "labels": None, "base": base,
            "visible": True}


async def test_linker_type_mirrors_create_order_and_quoting():
    c = _TypeConn()
    ts = [_dom("public", "Email", {"rkind": "builtin", "rbase": "text", "is_array": False}),
          _enum("public", "OrderStatus", ["new", "it's"]),
          _dom("public", "dm", {"rkind": "enum", "enum_key": "public.OrderStatus",
                                "enum_schema": "public", "enum_name": "OrderStatus",
                                "is_array": True})]
    res = await whlink.ensure_type_mirrors(c, "business_db", ts)
    assert res["created"] == 3 and res["replaced"] == 0 and not res["unsupported"]
    ddl = [q for q in c.sql if q.startswith(("CREATE TYPE", "CREATE DOMAIN", "CREATE SCHEMA"))]
    assert ddl[0].startswith("CREATE SCHEMA")
    kinds = [q.split(" AS")[0] for q in ddl if q.startswith(("CREATE TYPE", "CREATE DOMAIN"))]
    assert kinds[0] == 'CREATE TYPE "public"."OrderStatus"'
    assert "'it''s'" in " ".join(ddl)
    assert any(q.endswith('AS "public"."OrderStatus"[]') for q in ddl)
    assert not any("cascade" in q.lower() for q in c.sql)


async def test_linker_type_mirrors_unchanged_issues_no_ddl():
    e = _enum("public", "mood", ["ok", "bad"])
    c = _TypeConn(stored={("public", "mood"): ("enum", whlink.type_def_hash(e))})
    res = await whlink.ensure_type_mirrors(c, "business_db", [e])
    assert (res["created"], res["replaced"], res["dropped"]) == (0, 0, 0)
    assert c.sql == []


async def test_linker_type_mirrors_name_collision_is_unsupported():
    c = _TypeConn(existing_types=[("public", "mood")])
    res = await whlink.ensure_type_mirrors(c, "business_db", [_enum("public", "mood", ["a"])])
    assert res["created"] == 0
    assert res["unsupported"][0]["reason"] == "the name is already used in the warehouse"
    assert not any(q.startswith("CREATE TYPE") for q in c.sql)


async def test_linker_mirror_names_false_creates_no_type_under_source_names():
    # the hash-named mirror path only ever creates in the types schema
    c = _TypeConn()
    ts, name = await whlink._ensure_enum(c, "src_a", "public.mood", ["x", "y"],
                                         whlink.DEFAULT_ROLES)
    assert ts == "ic_fdw_src_a_types" and name.startswith("e_")
    creates = [q for q in c.sql if q.startswith("CREATE TYPE")]
    assert creates and all('"ic_fdw_src_a_types"' in q for q in creates)


async def test_linker_domain_base_must_be_safe():
    c = _TypeConn()
    bad = _dom("public", "d", {"rkind": "builtin", "rbase": "text; DROP TABLE x",
                               "is_array": False})
    res = await whlink.ensure_type_mirrors(c, "business_db", [bad])
    assert res["created"] == 0 and res["unsupported"]
    assert not any("DROP TABLE" in q for q in c.sql)


# Phase 15 gap 2 (WR-14): view names separate from remote names
class _ViewConn:
    """Records exec_fmt calls as (format, args); has_view_col answers the pg_attribute probes."""

    def __init__(self, has_view_col=False):
        self.calls, self.ex, self.has_view_col = [], [], has_view_col

    async def fetchval(self, q, *a):
        if q == "SELECT format($1, VARIADIC $2::text[])":
            self.calls.append((a[0], tuple(a[1])))
            return a[0]
        if "pg_attribute" in q:
            return 1 if ("view_name" not in q or self.has_view_col) else None
        return 1  # schema exists / grants fine

    async def execute(self, q, *a):
        self.ex.append((q, a))


async def test_create_view_with_distinct_view_name():
    c = _ViewConn()
    cols = [{"name": "id"}, {"name": "Qty"}]
    await whlink._create_view(c, "ic_fdw_x", "src_s", "OrderLines", cols,
                              whlink.DEFAULT_ROLES, view="order_lines")
    create = [x for x in c.calls if x[0].startswith("CREATE VIEW")][0]
    assert create[1] == ("src_s", "order_lines", "id", "Qty", "ic_fdw_x", "OrderLines")
    grant = [x for x in c.calls if x[0].startswith("GRANT SELECT ON")][0]
    assert grant[1][:2] == ("src_s", "order_lines")


async def test_create_view_default_name_unchanged():
    c = _ViewConn()
    await whlink._create_view(c, "ic_fdw_x", "src_s", "t", [{"name": "a"}], whlink.DEFAULT_ROLES)
    create = [x for x in c.calls if x[0].startswith("CREATE VIEW")][0]
    assert create[1] == ("src_s", "t", "a", "ic_fdw_x", "t")


async def test_drop_objects_view_name():
    c = _ViewConn()
    await whlink._drop_objects(c, "src_s", "ic_fdw_x", "OrderLines", view="order_lines")
    assert c.calls == [("DROP VIEW IF EXISTS %I.%I", ("src_s", "order_lines")),
                       ("DROP FOREIGN TABLE IF EXISTS %I.%I", ("ic_fdw_x", "OrderLines"))]
    c2 = _ViewConn()
    await whlink._drop_objects(c2, "src_s", "ic_fdw_x", "t")
    assert c2.calls[0][1] == ("src_s", "t")


async def test_upsert_link_object_view_name():
    c = _ViewConn()
    await whlink._upsert_link_object(c, "s", "public", "OrderLines", "p", "h",
                                     view_name="order_lines")
    q, a = c.ex[0]
    assert "view_name = EXCLUDED.view_name" in q and a[-1] == "order_lines"
    c2 = _ViewConn()
    await whlink._upsert_link_object(c2, "s", "public", "t", "p", "h")
    assert c2.ex[0][1][-1] is None


async def test_ensure_meta_adds_view_name_once(monkeypatch):
    async def _ok(conn, reader):
        return True
    monkeypatch.setattr(whlink, "_meta_grants_ok", _ok)
    c = _ViewConn(has_view_col=False)
    await whlink.ensure_meta(c)
    assert [q for q, _ in c.ex if "ADD COLUMN view_name" in q] == [
        "ALTER TABLE ic_meta.link_objects ADD COLUMN view_name text"]
    c2 = _ViewConn(has_view_col=True)
    await whlink.ensure_meta(c2)
    assert not [q for q, _ in c2.ex if "ADD COLUMN view_name" in q]
