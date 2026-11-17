"""Phase 10 probes: dashboards origin and context store (DEPLOY-03). No stack needed."""

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from app import configservice as cs
from app import dashboardrender
from app import selftest
from app.config import get_settings

CSP = {"content-security-policy": "default-src 'none'; script-src 'self'"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in ("UI_PROFILE", "DASHBOARDS_PUBLIC_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    cs._last_tests.clear()
    yield
    get_settings.cache_clear()
    cs._last_tests.clear()


class FakeConn:
    def __init__(self, row=None, ext="0.8.7", missing=(), hnsw=1, counts=(5, 1), fail=None):
        self.sql, self.row, self.ext, self.missing = [], row, ext, set(missing)
        self.hnsw, self.counts, self.closed, self.fail = hnsw, counts, False, fail

    async def fetchval(self, q, *a):
        self.sql.append(q)
        if "pg_extension" in q:
            return self.ext
        if "to_regclass" in q:
            return a[0].split(".", 1)[1] not in self.missing
        if "pg_indexes" in q:
            return self.hnsw
        raise AssertionError(q)

    async def fetchrow(self, q, *a):
        self.sql.append(q)
        if "FROM dashboards" in q:
            if self.fail:
                raise self.fail
            return self.row
        return {"approved": self.counts[0], "unembedded": self.counts[1]}

    async def close(self):
        self.closed = True


def wire(monkeypatch, handler, conn=None):
    monkeypatch.setattr(cs, "_probe_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=4, follow_redirects=False))

    async def connect():
        return conn
    monkeypatch.setattr(cs, "_probe_connect", connect)


PUB = {"slug": "sales", "view_token": "tok-SECRET"}


RENDERER_BYTES = (Path(__file__).resolve().parents[2] / "dashboards" / "vendor"
                  / "ic-render-1.0.0" / "render.js").read_bytes()


def lib_ok(page_status=200, page_headers=CSP, probe_status=404, renderer_status=200,
           renderer_bytes=RENDERER_BYTES):
    def h(req):
        if req.url.path == dashboardrender.RENDERERS[dashboardrender.CURRENT_RENDERER]["src"]:
            return httpx.Response(renderer_status, content=renderer_bytes)
        if "/_lib/" in req.url.path:
            return httpx.Response(200)
        if req.url.path.startswith("/d/sales/"):
            return httpx.Response(page_status, headers=page_headers)
        return httpx.Response(probe_status)
    return h


def run(coro):
    return asyncio.run(coro)


def test_groups_and_optional(monkeypatch):
    assert cs.GROUPS["dashboards"] == "Dashboards origin"
    assert cs.GROUPS["context_store"] == "Context store"
    assert cs._group_configured("dashboards") and cs._group_configured("context_store")
    st = run(cs.status())
    assert st["dashboards"]["state"] == "untested"
    assert st["context_store"]["state"] == "untested"
    monkeypatch.setenv("UI_PROFILE", "second_brain")
    get_settings.cache_clear()
    assert cs.is_optional("dashboards")
    monkeypatch.setenv("UI_PROFILE", "intelligence")
    get_settings.cache_clear()
    assert not cs.is_optional("dashboards")
    assert not cs.is_optional("context_store") and cs.is_optional("tracing")
    assert not cs.is_optional("llm")
    st = run(cs.status())
    for g in cs.GROUPS:
        assert st[g]["optional"] == cs.is_optional(g)


def test_dispatch(monkeypatch):
    async def a():
        return True, "A"

    async def b():
        return True, "B"
    monkeypatch.setattr(cs, "_test_dashboards", a)
    monkeypatch.setattr(cs, "_test_context_store", b)
    assert run(cs.test_group("dashboards"))["detail"] == "A"
    assert run(cs.test_group("context_store"))["detail"] == "B"


def test_dashboards_published_ok_and_token_not_leaked(monkeypatch, caplog):
    wire(monkeypatch, lib_ok(), FakeConn(row=PUB))
    caplog.set_level(logging.DEBUG)
    ok, d = run(cs._test_dashboards())
    assert ok and "sales" in d and "tok-SECRET" not in d
    assert all("tok-SECRET" not in r.getMessage() for r in caplog.records)
    assert any(r.name == "httpx" and "/d/sales/" in r.getMessage()
               and "t=REDACTED" in r.getMessage() for r in caplog.records)


def test_dashboards_renderer_ok_in_detail(monkeypatch):
    wire(monkeypatch, lib_ok(), FakeConn(row=PUB))
    ok, d = run(cs._test_dashboards())
    assert ok and "renderer ok" in d


def test_dashboards_renderer_missing(monkeypatch):
    wire(monkeypatch, lib_ok(renderer_status=404), FakeConn(row=PUB))
    ok, d = run(cs._test_dashboards())
    assert not ok and "chart library ok; renderer ic-render-1.0.0 missing or stale" in d
    assert "rebuild the dashboards image" in d


def test_dashboards_renderer_stale_bytes(monkeypatch):
    wire(monkeypatch, lib_ok(renderer_bytes=b"// old build"), FakeConn(row=PUB))
    ok, d = run(cs._test_dashboards())
    assert not ok and "missing or stale" in d


def test_dashboards_no_csp(monkeypatch):
    wire(monkeypatch, lib_ok(page_headers={}), FakeConn(row=PUB))
    assert run(cs._test_dashboards())[0] is False


def test_dashboards_probe_slug(monkeypatch):
    wire(monkeypatch, lib_ok(probe_status=404), FakeConn(row=None))
    ok, d = run(cs._test_dashboards())
    assert ok and "reaches the gateway" in d
    wire(monkeypatch, lib_ok(probe_status=502), FakeConn(row=None))
    ok, d = run(cs._test_dashboards())
    assert not ok and "gateway" in d


def test_dashboards_base_fallback_and_unreachable(monkeypatch):
    def h(req):
        if req.url.host == "127.0.0.1":
            raise httpx.ConnectError("refused")
        return lib_ok()(req)
    wire(monkeypatch, h, FakeConn(row=None))
    assert run(cs._test_dashboards())[0] is True

    def dead(req):
        raise httpx.ConnectError("x")
    wire(monkeypatch, dead, FakeConn(row=None))
    ok, d = run(cs._test_dashboards())
    assert not ok and "unreachable" in d
    assert all(b in d for b in cs.DASHBOARDS_PROBE_BASES)


def test_dashboards_public_informational(monkeypatch):
    monkeypatch.setenv("DASHBOARDS_PUBLIC_URL", "https://dash.example.com/")
    get_settings.cache_clear()

    def h(req):
        if req.url.host == "dash.example.com":
            raise httpx.ConnectError("hairpin")
        return lib_ok()(req)
    wire(monkeypatch, h, FakeConn(row=None))
    ok, d = run(cs._test_dashboards())
    assert ok and "public address" in d


def test_redact_filter():
    rec = logging.LogRecord("httpx", logging.INFO, "", 0, 'HTTP Request: %s %s "%s %d %s"',
                            ("GET", httpx.URL("http://dashboards/d/s/?t=abc&p=def&x=1"),
                             "HTTP/1.1", 200, "OK"), None)
    assert cs.RedactTokenQuery().filter(rec) is True
    m = rec.getMessage()
    assert "t=REDACTED" in m and "p=REDACTED" in m and "x=1" in m
    assert "abc" not in m and "def" not in m
    plain = logging.LogRecord("httpx", logging.INFO, "", 0, "hello %s", ("w",), None)
    cs.RedactTokenQuery().filter(plain)
    assert plain.getMessage() == "hello w"
    bad = logging.LogRecord("httpx", logging.INFO, "", 0, "%d", ("notint",), None)
    assert cs.RedactTokenQuery().filter(bad) is True


def test_install_idempotent():
    cs.install_httpx_token_mask()
    cs.install_httpx_token_mask()
    fl = [f for f in logging.getLogger("httpx").filters if isinstance(f, cs.RedactTokenQuery)]
    assert len(fl) == 1


def test_context_store(monkeypatch):
    c = FakeConn()
    wire(monkeypatch, None, c)
    ok, d = run(cs._test_context_store())
    assert ok and "pgvector 0.8.7" in d and "HNSW" in d and "5 approved" in d
    assert "keyword search only" in d  # blank key
    assert c.closed
    for q in c.sql:
        u = q.strip().upper()
        assert u.startswith("SELECT")
        for w in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"):
            assert w not in u
    for kw, frag in (({"ext": None}, "pgvector"), ({"missing": ["context_tables"]}, "context_tables"),
                     ({"hnsw": 0}, "HNSW")):
        c = FakeConn(**kw)
        wire(monkeypatch, None, c)
        ok, d = run(cs._test_context_store())
        assert not ok and frag in d and c.closed


def test_context_store_connect_failure(monkeypatch):
    async def boom():
        raise OSError("down")
    monkeypatch.setattr(cs, "_probe_connect", boom)
    ok, d = run(cs._test_context_store())
    assert not ok and "memory DB unreachable" in d


def test_selftest_lines(monkeypatch, capsys):
    async def lo():
        return 0

    async def st():
        return {g: {"state": "ok", "detail": "", "optional": False} for g in cs.GROUPS}

    async def tg(g):
        return {"group": g, "ok": g != "dashboards", "detail": "d"}
    monkeypatch.setattr(cs, "load_overrides", lo)
    monkeypatch.setattr(cs, "status", st)
    monkeypatch.setattr(cs, "test_group", tg)
    monkeypatch.setattr(cs, "is_optional", lambda g: g == "dashboards")
    code = run(selftest.main())
    out = capsys.readouterr().out
    assert "Dashboards origin" in out and "Context store" in out
    assert "(optional)" in out and code == 0


# ── Phase 14 (WH-08): Warehouse probe group ──
import asyncpg  # noqa: E402

from app import guardrails, warehouse  # noqa: E402


class WhConn:
    def __init__(self, ping_fail=None, link=None, exts=("mysql_fdw", "tds_fdw")):
        self.sql, self.closed, self.ping_fail = [], False, ping_fail
        self.link = link if link is not None else {
            "status": "ok", "tables_linked": 9, "linked_at": None}
        self.exts = exts

    async def fetchval(self, q, *a):
        self.sql.append(q)
        if "business_db_ping" in q:
            if self.ping_fail:
                raise self.ping_fail
            return 9
        raise AssertionError(q)

    async def fetchrow(self, q, *a):
        self.sql.append(q)
        assert "detail" not in q and "catalog_hash" not in q
        return self.link

    async def fetch(self, q, *a):
        self.sql.append(q)
        return [{"name": n} for n in self.exts]

    async def close(self):
        self.closed = True


def wh_wire(monkeypatch, conn=None, mode=True, url="postgresql://x", connect_exc=None,
            check=(True, "read-only")):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: mode)
    monkeypatch.setenv("TENANT_DATABASE_URL", url)
    monkeypatch.delenv("BUSINESS_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    async def connect(role, **kw):
        assert role == "reader"
        if connect_exc:
            raise connect_exc
        return conn

    async def check_reader(c):
        return check
    monkeypatch.setattr(warehouse, "connect", connect)
    monkeypatch.setattr(warehouse, "check_reader", check_reader)

    async def no_run(*a, **k):
        raise AssertionError("run_readonly_sql must not be used")
    monkeypatch.setattr(guardrails, "run_readonly_sql", no_run)


def test_wh_group_off_and_on(monkeypatch):
    assert cs.GROUPS["warehouse"] == "Warehouse"
    for prof in ("intelligence", "second_brain"):
        monkeypatch.setenv("UI_PROFILE", prof)
        get_settings.cache_clear()
        monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
        assert not cs._group_configured("warehouse") and cs.is_optional("warehouse")
        monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
        assert cs._group_configured("warehouse") and not cs.is_optional("warehouse")
    assert cs._group_configured("dashboards") and not cs.is_optional("llm")
    assert cs.is_optional("tracing")


def test_wh_dispatch(monkeypatch):
    async def w():
        return True, "W"
    monkeypatch.setattr(cs, "_test_warehouse", w)
    assert run(cs.test_group("warehouse"))["detail"] == "W"


def test_wh_not_configured_no_connection(monkeypatch):
    wh_wire(monkeypatch, mode=False)
    ok, d = run(cs._test_warehouse())
    assert not ok and "not configured" in d


def test_wh_happy(monkeypatch):
    c = WhConn()
    wh_wire(monkeypatch, c)
    ok, d = run(cs._test_warehouse())
    assert ok and "read-only" in d and "9 tables" in d
    assert "mysql_fdw available" in d and "tds_fdw available" in d
    assert c.closed


def test_wh_extensions_missing(monkeypatch):
    wh_wire(monkeypatch, WhConn(exts=()))
    ok, d = run(cs._test_warehouse())
    assert ok and "mysql_fdw not installed" in d and "tds_fdw not installed" in d


def test_wh_connect_errors(monkeypatch):
    wh_wire(monkeypatch, connect_exc=OSError("secret-host"))
    ok, d = run(cs._test_warehouse())
    assert (ok, d) == (False, "warehouse unreachable as wh_reader (OSError)")
    wh_wire(monkeypatch, connect_exc=asyncpg.InvalidPasswordError("pw"))
    ok, d = run(cs._test_warehouse())
    assert not ok and "login failed" in d and "pw" not in d


def test_wh_check_reader_fails(monkeypatch):
    c = WhConn()
    wh_wire(monkeypatch, c, check=(False, "x"))
    ok, d = run(cs._test_warehouse())
    assert not ok and "x" in d and c.closed


def test_wh_ping_fails(monkeypatch):
    wh_wire(monkeypatch, WhConn(ping_fail=OSError("dsn")))
    ok, d = run(cs._test_warehouse())
    assert not ok and "business database unreachable through the warehouse" in d
    assert "dsn" not in d


def test_wh_no_business_url(monkeypatch):
    c = WhConn()
    wh_wire(monkeypatch, c, url="")
    ok, d = run(cs._test_warehouse())
    assert ok and "business database not configured yet" in d
    assert not any("business_db_ping" in q for q in c.sql)


def test_wh_guardrails_gap_detected(monkeypatch):
    wh_wire(monkeypatch, WhConn())
    monkeypatch.setattr(guardrails, "_FORBIDDEN_TABLES",
                        guardrails._FORBIDDEN_TABLES - {"pg_user_mappings"})
    ok, d = run(cs._test_warehouse())
    assert not ok and "pg_user_mappings" in d


def test_wh_probes_all_refused_by_real_guardrails():
    for _, sql in cs.WAREHOUSE_FORBIDDEN_PROBES:
        with pytest.raises(guardrails.SQLGuardrailError):
            guardrails.validate_readonly_sql(sql)


def test_wh_selftest_line(monkeypatch, capsys):
    async def lo():
        return 0

    async def st():
        return {g: {"state": "ok", "detail": "", "optional": False} for g in cs.GROUPS}

    async def tg(g):
        return {"group": g, "ok": True, "detail": "d"}
    monkeypatch.setattr(cs, "load_overrides", lo)
    monkeypatch.setattr(cs, "status", st)
    monkeypatch.setattr(cs, "test_group", tg)
    assert run(selftest.main()) == 0
    assert "Warehouse — d" in capsys.readouterr().out


def test_dashboards_probe_url_env_first(monkeypatch):
    # Quick 044: DASHBOARDS_PROBE_URL is tried before the two defaults (Railway private host).
    monkeypatch.setenv("DASHBOARDS_PROBE_URL", "http://ic-dashboards.railway.internal/")
    assert cs.dashboards_probe_bases() == ("http://ic-dashboards.railway.internal",) + cs.DASHBOARDS_PROBE_BASES
    seen = []

    def h(req):
        seen.append(req.url.host)
        if req.url.host != "ic-dashboards.railway.internal":
            raise httpx.ConnectError("refused")
        return lib_ok()(req)
    wire(monkeypatch, h, FakeConn(row=None))
    assert run(cs._test_dashboards())[0] is True
    assert seen[0] == "ic-dashboards.railway.internal"
    monkeypatch.delenv("DASHBOARDS_PROBE_URL")
    assert cs.dashboards_probe_bases() == cs.DASHBOARDS_PROBE_BASES
