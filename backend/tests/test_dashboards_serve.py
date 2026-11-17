"""Phase 4 plan 03: /d/ serving helpers and routes."""
import json
import logging
import re
from pathlib import Path

import pytest
from starlette.requests import Request

from app import dashboards, dashboards_serve as ds

APP_ROOT = Path(__file__).resolve().parents[2]
DOC = APP_ROOT / "docs" / "DASHBOARD_CONTRACT.md"
FENCE = "`" * 3
LIB = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="


def _example_html() -> str:
    m = re.search(FENCE + r"html\n(.*?)" + FENCE, DOC.read_text(encoding="utf-8"), re.S)
    assert m
    return m.group(1)


def _req(qs: str) -> Request:
    return Request({"type": "http", "query_string": qs.encode(), "headers": []})


def _directives(csp: str) -> dict:
    return {p.split(" ", 1)[0]: p for p in csp.split("; ")}


def test_build_csp_contract_example():
    html = _example_html()
    stored = dashboards.script_hashes(html)
    toks = " ".join(f"'{h}'" for h in [*stored, ds.PRELUDE_HASH])
    assert ds.build_csp(html, stored) == (
        f"default-src 'none'; script-src {toks}; style-src 'unsafe-inline'; img-src data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
        "sandbox allow-scripts")
    assert f"'{LIB}'" in toks and len(stored) == 2


def test_w1_drops_foreign_integrity():
    html = _example_html()
    evil = "sha256-" + "A" * 43 + "="
    csp = ds.build_csp(html, dashboards.script_hashes(html) + [evil])
    assert evil not in csp


def test_bad_tokens_dropped_and_dupes_collapsed():
    html = _example_html()
    good = dashboards.script_hashes(html)
    inline = sorted(dashboards.inline_script_hashes(html))[0]
    csp = ds.build_csp(html, good + good + ["sha384-" + "A" * 64, f"'{inline}'", "x", None, 5])
    sp = _directives(csp)["script-src"]
    assert sp.count(inline) == 1 and "sha384" not in sp and "''" not in sp


def test_no_recompute_and_none_and_no_self():
    html = _example_html()
    assert _directives(ds.build_csp(html, [LIB]))["script-src"] == (
        f"script-src '{LIB}' '{ds.PRELUDE_HASH}'")
    for empty in ([], None):
        assert _directives(ds.build_csp(html, empty))["script-src"] == (
            f"script-src '{ds.PRELUDE_HASH}'")
    sp = _directives(ds.build_csp(html, dashboards.script_hashes(html)))["script-src"]
    assert "'self'" not in sp


def test_lib_hashes_match_vendor_json():
    data = json.loads((APP_ROOT / "dashboards" / "vendor.json").read_text(encoding="utf-8"))
    found = set()

    def walk(o):
        if isinstance(o, dict):
            if "csp_hash" in o:
                found.add(o["csp_hash"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    # Phase 18: the in-repo renderer is pinned separately in dashboardrender.py.
    walk({"libraries": [x for x in data["libraries"] if x["name"] != "ic-render"]})
    assert ds.LIB_CSP_HASHES == found


def test_constants_and_nginx_parity():
    assert ds.DATA_CSP == "default-src 'none'; frame-ancestors 'none'; sandbox"
    assert ds.PROXY_HEADER == "x-ic-dashboards-proxy"
    conf = (APP_ROOT / "dashboards" / "nginx.conf").read_text(encoding="utf-8")
    hdrs = [h.lower() for h in re.findall(r'proxy_set_header\s+(\S+)\s+"1";', conf)]
    assert ds.PROXY_HEADER in hdrs


@pytest.mark.parametrize("qs,want", [
    ("t=abc", "abc"), ("t=", None), ("t=a&t=b", None), ("", None),
    ("t=" + "a" * 129, None), ("%74=abc", "abc")])
def test_one_param(qs, want):
    assert ds.one_param(_req(qs), "t") == want


def test_token_matches():
    assert ds.token_matches("é", "abc") is False
    assert ds.token_matches("abc", "abc") is True
    assert ds.token_matches("abd", "abc") is False
    assert ds.token_matches(None, "abc") is False


def test_not_found_and_unavailable():
    for r, code, body in ((ds.not_found(), 404, b"Not found\n"),
                          (ds.unavailable(), 503, b"Service unavailable\n")):
        assert r.status_code == code and r.body == body
        assert r.media_type == "text/plain"
        for k, v in ds.BASE_HEADERS.items():
            assert r.headers[k] == v
    assert ds.BASE_HEADERS["Cross-Origin-Opener-Policy"] == "same-origin"


def _rec(args):
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                             '%s - "%s %s HTTP/%s" %d', args, None)


def test_mask_filter():
    from uvicorn.logging import AccessFormatter
    f = ds.MaskDashboardQuery()
    rec = _rec(("1.2.3.4:5", "GET", "/d/sales/?t=SECRET&p=X", "1.1", 200))
    msg = rec.msg
    assert f.filter(rec) is True
    assert rec.args == ("1.2.3.4:5", "GET", "/d/sales/?REDACTED", "1.1", 200)
    assert rec.msg == msg
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(rec)
    assert "SECRET" not in line and "?REDACTED" in line
    for args in (("c", "GET", "/api/logs?limit=5", "1.1", 200),
                 ("c", "GET", "/d/sales/", "1.1", 200), ("c", "GET", "/d/x?t=1")):
        r = _rec(args)
        assert f.filter(r) is True and r.args == args


_BYPASS = [
    ("//d/x/?t=PROBE", "//d/x/?REDACTED"),
    ("///d?t=PROBE", "///d?REDACTED"),
    ("/d/x%3Ft%3DPROBE", "/d/x?REDACTED"),
    ("/d/x%3ft%3DPROBE", "/d/x?REDACTED"),
    ("/d/x%253Ft%3DPROBE", "/d/x?REDACTED"),
    ("/./d/x/?t=PROBE", "/./d/x/?REDACTED"),
    ("/x/../d/x/?t=PROBE", "/x/../d/x/?REDACTED"),
    ("/D/x/?t=PROBE", "/D/x/?REDACTED"),
    ("/api/x%3Ft%3DPROBE", "/api/x?REDACTED"),
    ("/d/sales/?t=SECRET&p=X", "/d/sales/?REDACTED"),
]


@pytest.mark.parametrize("path,expected", _BYPASS)
def test_mask_filter_bypass_forms(path, expected):
    f = ds.MaskDashboardQuery()
    rec = _rec(("c", "GET", path, "1.1", 200))
    msg = rec.msg
    assert f.filter(rec) is True
    assert rec.args == ("c", "GET", expected, "1.1", 200)
    assert rec.msg == msg


@pytest.mark.parametrize("args", [
    ("c", "GET", "/api/logs?limit=5", "1.1", 200),
    ("c", "GET", "/d/sales/", "1.1", 200),
    ("c", "GET", "/health", "1.1", 200),
    ("c", "GET", "/dashboard?x=1", "1.1", 200),
    ("c", "GET", "/api/chats/1%40g.us?limit=5", "1.1", 200),
    ("c", "GET", "/d/x?t=1"),
    ("c", "GET", 123, "1.1", 200),
])
def test_mask_filter_leaves_other_lines(args):
    f = ds.MaskDashboardQuery()
    rec = _rec(args)
    assert f.filter(rec) is True and rec.args == args


@pytest.mark.parametrize("raw,q,probe", [
    ("//d/x/", b"t=PROBE1", "PROBE1"),
    ("/d/x%3Ft=PROBE2", b"", "PROBE2"),
    ("/d/x%3ft=PROBE3", b"", "PROBE3"),
    ("/d/x%253Ft=PROBE4", b"", "PROBE4"),
    ("/./d/x/", b"t=PROBE5", "PROBE5"),
    ("/x/../d/x/", b"t=PROBE6", "PROBE6"),
])
def test_mask_filter_uvicorn_derived(raw, q, probe):
    import urllib.parse

    from uvicorn.logging import AccessFormatter
    from uvicorn.protocols.utils import get_path_with_query_string
    full = get_path_with_query_string({"path": urllib.parse.unquote(raw), "query_string": q})
    rec = _rec(("1.2.3.4:5", "GET", full, "1.1", 200))
    assert ds.MaskDashboardQuery().filter(rec) is True
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(rec)
    assert "?REDACTED" in line and probe not in line


def test_mask_installed_once():
    ds.install_access_log_mask()
    ds.install_access_log_mask()
    fl = logging.getLogger("uvicorn.access").filters
    assert len([x for x in fl if isinstance(x, ds.MaskDashboardQuery)]) == 1


def test_source_hygiene():
    src = Path(ds.__file__).read_text(encoding="utf-8")
    assert "guardrails" not in src and "create_pool" not in src


# ---------------------------------------------------------------- routes

from types import SimpleNamespace  # noqa: E402

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import guardrails, main  # noqa: E402

P = {"X-IC-Dashboards-Proxy": "1"}
TOKEN = "view-token-123"
CODE = _example_html()
STORED = dashboards.script_hashes(CODE)
LINT_Q = json.dumps([{"id": "invoiced_by_customer", "sql": "SELECT 1 AS n", "refresh_minutes": 60}])
DATA_TEXT = '{"query_id": "orders", "columns": [], "rows": [], "refreshed_at": null, "data_as_of": null, "error": null}'


class Conn:
    def __init__(self, versions, fail=None):
        self.versions = versions  # {None|int: (status, version_id)}
        self.calls = []
        self.fail = fail
        self.code_by_vid = {}

    async def fetchrow(self, sql, *a):
        self.calls.append(sql)
        if self.fail:
            raise self.fail
        if sql == ds.FORMAT_SQL:
            return {"is_spec": False, "renderer": None}
        if sql == ds.CODE_SQL:
            code = self.code_by_vid.get(a[0], CODE)
            return {"code": code, "script_hashes": dashboards.script_hashes(code),
                    "queries": LINT_Q}
        key = None if sql == ds.CURRENT_AUTH_SQL else a[1]
        if a[0] != "sales" or key not in self.versions:
            return None
        status, vid = self.versions[key]
        return {"view_token": TOKEN, "version_id": vid, "status": status}

    async def fetchval(self, sql, *a):
        self.calls.append(sql)
        if sql == ds.DATA_SQL:
            return DATA_TEXT if a[1] == "orders" else None
        return None


class Pool:
    def __init__(self, conn, timeouts=None, enter_exc=None):
        self.conn = conn
        self.timeouts = timeouts if timeouts is not None else []
        self.enter_exc = enter_exc

    def acquire(self, timeout=None):
        c = self.conn
        self.timeouts.append(timeout)
        exc = self.enter_exc

        class A:
            async def __aenter__(self_):
                if exc is not None:
                    raise exc
                return c

            async def __aexit__(self_, *e):
                return False
        return A()


@pytest.fixture(autouse=True)
def _legacy_on(monkeypatch):
    # Phase 18: these tests pin the v1.0 HTML behaviour, which needs the legacy gate open.
    monkeypatch.setattr(dashboards, "legacy_html_on", lambda: True)


@pytest.fixture
def env(monkeypatch):
    conn = Conn({None: ("published", 11), 1: ("retired", 10), 2: ("draft", 12),
                 3: ("published", 11)})
    state = {"pool_calls": 0, "conn": conn, "timeouts": [], "enter_exc": None}

    async def get_pool():
        state["pool_calls"] += 1
        return Pool(state["conn"], state["timeouts"], state["enter_exc"])
    monkeypatch.setattr(ds, "db", SimpleNamespace(get_pool=get_pool))

    async def ok(c, *, version_id, token):
        return token == "GOOD" and version_id == 12
    monkeypatch.setattr(dashboards, "preview_token_ok", ok)
    state["client"] = TestClient(main.app, follow_redirects=False)
    return state


def _heads_ok(r):
    for k, v in ds.BASE_HEADERS.items():
        assert r.headers[k] == v


def _404(r):
    assert r.status_code == 404 and r.content == b"Not found\n"
    assert r.headers["content-type"].startswith("text/plain")
    _heads_ok(r)


def test_current_page(env):
    r = env["client"].get(f"/d/sales/?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.text == ds.WRAPPER_HTML
    assert r.headers["content-security-policy"] == ds.WRAPPER_CSP
    _heads_ok(r)
    f = env["client"].get(f"/d/sales/frame?t={TOKEN}", headers={**P, "Host": "dash.test"})
    assert f.status_code == 200 and f.text == ds.content_html(CODE)
    assert f.headers["content-security-policy"] == ds.build_csp(CODE, STORED, host="dash.test")
    _heads_ok(f)


def test_no_proxy_header_no_db(env):
    _404(env["client"].get(f"/d/sales/?t={TOKEN}"))
    assert env["pool_calls"] == 0
    for v in ("0", "yes"):
        _404(env["client"].get(f"/d/sales/?t={TOKEN}", headers={"X-IC-Dashboards-Proxy": v}))


@pytest.mark.parametrize("qs", ["", "t=wrong", "t=", f"t={TOKEN}&t={TOKEN}",
                                f"t=x&t={TOKEN}", "t=" + "a" * 200, f"p={TOKEN}"])
def test_bad_tokens(env, qs):
    _404(env["client"].get(f"/d/sales/?{qs}", headers=P))
    assert ds.CODE_SQL not in env["conn"].calls


@pytest.mark.parametrize("path", ["/d/other/", "/d/Bad_Slug/", "/d/bad-/"])
def test_bad_slug(env, path):
    _404(env["client"].get(f"{path}?t={TOKEN}", headers=P))


def test_never_published(env):
    env["conn"].versions.pop(None)
    _404(env["client"].get(f"/d/sales/?t={TOKEN}", headers=P))


def test_pinned(env):
    c = env["client"]
    assert c.get(f"/d/sales/v/1/?t={TOKEN}", headers=P).status_code == 200
    assert c.get(f"/d/sales/v/3/?t={TOKEN}", headers=P).status_code == 200
    _404(c.get(f"/d/sales/v/2/?t={TOKEN}", headers=P))


def test_draft_preview(env):
    c = env["client"]
    r = c.get("/d/sales/v/2/?p=GOOD", headers=P)
    assert r.status_code == 200 and "content-security-policy" in r.headers
    _404(c.get("/d/sales/v/2/?p=BAD", headers=P))
    _404(c.get("/d/sales/v/3/?p=GOOD", headers=P))
    _404(c.get("/d/sales/?p=GOOD", headers=P))


@pytest.mark.parametrize("n", ["abc", "0", "01", "1234567890"])
def test_bad_version(env, n):
    _404(env["client"].get(f"/d/sales/v/{n}/?t={TOKEN}", headers=P))


def test_data_routes(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tenant path touched")
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom, raising=False)
    monkeypatch.setattr(guardrails, "get_business_pool", boom, raising=False)
    c = env["client"]
    r = c.get(f"/d/sales/data/orders?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.text == DATA_TEXT
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-security-policy"] == ds.DATA_CSP
    _heads_ok(r)
    assert c.get(f"/d/sales/v/1/data/orders?t={TOKEN}", headers=P).status_code == 200
    _404(c.get(f"/d/sales/data/nope?t={TOKEN}", headers=P))
    n = len(env["conn"].calls)
    _404(c.get(f"/d/sales/data/Bad-Id?t={TOKEN}", headers=P))
    assert len(env["conn"].calls) == n


def test_redirects(env):
    c = env["client"]
    r = c.get(f"/d/sales?t={TOKEN}", headers=P)
    assert r.status_code == 301 and r.headers["location"] == f"/d/sales/?t={TOKEN}"
    r = c.get("/d/sales/v/3?t=abc&x=1", headers=P)
    assert r.status_code == 301 and r.headers["location"] == "/d/sales/v/3/?t=abc&x=1"
    r = c.get("/d/sales", headers=P)
    assert r.headers["location"] == "/d/sales/"
    assert env["pool_calls"] == 0
    _404(c.get("/d/Bad?t=x", headers=P))
    _404(c.get("/d/sales"))


def test_catch_all_methods(env):
    c = env["client"]
    for path in ("/d", "/d/", "/d/a/b/c/d/e"):
        _404(c.get(path, headers=P))
    for m in ("post", "head", "options"):
        r = getattr(c, m)("/d/sales/", headers=P)
        assert r.status_code == 404
        assert r.headers["Referrer-Policy"] == "no-referrer"


def test_503(env):
    c = env["client"]
    for exc in (OSError("x"), asyncpg.PostgresConnectionError("x"),
                asyncpg.CannotConnectNowError("x")):
        env["conn"].fail = exc
        r = c.get(f"/d/sales/?t={TOKEN}", headers=P)
        assert r.status_code == 503 and r.headers["content-type"].startswith("text/plain")
        _heads_ok(r)


def test_pool_failure_503(env, monkeypatch):
    async def bad():
        raise OSError("down")
    monkeypatch.setattr(ds, "db", SimpleNamespace(get_pool=bad))
    assert env["client"].get(f"/d/sales/?t={TOKEN}", headers=P).status_code == 503


def test_no_secrets_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    c = env["client"]
    c.get(f"/d/sales/?t={TOKEN}", headers=P)
    c.get("/d/sales/v/2/?p=GOOD", headers=P)
    c.get("/d/sales/v/2/?p=BAD", headers=P)
    env["conn"].fail = OSError("x")
    c.get(f"/d/sales/?t={TOKEN}", headers=P)
    for rec in (r for r in caplog.records if r.name != "httpx"):
        m = rec.getMessage()
        assert TOKEN not in m and "GOOD" not in m and "BAD" not in m


def _flat():
    out = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inherited = list(r.include_context.dependencies)
            for sub in r.original_router.routes:
                out.append((sub.path, inherited + list(sub.dependencies)))
        elif hasattr(r, "path"):
            out.append((r.path, list(getattr(r, "dependencies", []))))
    return out


def _has_key(deps):
    return any(getattr(d, "dependency", None) is main._require_ops_key for d in deps)


def test_route_audit():
    flat = _flat()
    d = [(p, deps) for p, deps in flat if p == "/d" or p.startswith("/d/")]
    assert {p for p, _ in d} == {
        "/d", "/d/{slug}", "/d/{slug}/", "/d/{slug}/data/{qid}", "/d/{slug}/v/{n}",
        "/d/{slug}/v/{n}/", "/d/{slug}/v/{n}/data/{qid}", "/d/{rest:path}",
        "/d/{slug}/frame", "/d/{slug}/v/{n}/frame",
        "/d/{slug}/spec", "/d/{slug}/v/{n}/spec"}
    assert not any(_has_key(deps) for _, deps in d)
    api = [deps for p, deps in flat if p.startswith("/api/dashboards")]
    assert api and all(_has_key(deps) for deps in api)


def test_ops_api_still_gated():
    assert TestClient(main.app).get("/api/dashboards").status_code == 401


# ---- Phase 9 (DEPLOY-02) ----------------------------------------------------

def test_p9_constants():
    assert ds.PINNED_HTML_CACHE == "private, max-age=86400, immutable"
    assert ds.ACQUIRE_TIMEOUT == 2.0


@pytest.mark.parametrize("path", ["/d/sales/v/3/frame", "/d/sales/v/1/frame"])
def test_p9_pinned_html_immutable(env, path):
    r = env["client"].get(f"{path}?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["Cache-Control"] == "no-store"  # Phase 18: legacy HTML is never cached
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "content-security-policy" in r.headers


@pytest.mark.parametrize("path,qs", [
    ("/d/sales/", f"t={TOKEN}"),
    ("/d/sales/frame", f"t={TOKEN}"),
    ("/d/sales/v/2/", "p=GOOD"),
    ("/d/sales/v/2/frame", "p=GOOD"),
    ("/d/sales/data/orders", f"t={TOKEN}"),
    ("/d/sales/v/1/data/orders", f"t={TOKEN}"),
    ("/d/sales/v/3/data/orders", f"t={TOKEN}"),
])
def test_p9_other_200_no_store(env, path, qs):
    r = env["client"].get(f"{path}?{qs}", headers=P)
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"


def test_p9_404_301_503_no_store(env):
    c = env["client"]
    _404(c.get("/d/sales/v/1/?t=wrong", headers=P))
    r = c.get(f"/d/sales/v/1?t={TOKEN}", headers=P)
    assert r.status_code == 301 and r.headers["Cache-Control"] == "no-store"
    env["conn"].fail = OSError("x")
    r = c.get(f"/d/sales/v/3/?t={TOKEN}", headers=P)
    assert r.status_code == 503 and r.headers["Cache-Control"] == "no-store"


def test_p9_acquire_timeout_passed(env):
    env["client"].get(f"/d/sales/?t={TOKEN}", headers=P)
    env["client"].get(f"/d/sales/v/3/data/orders?t={TOKEN}", headers=P)
    assert env["timeouts"] == [2.0, 2.0]


def test_p9_acquire_timeout_503(env, caplog):
    env["enter_exc"] = TimeoutError()
    caplog.set_level(logging.WARNING)
    r = env["client"].get(f"/d/sales/?t={TOKEN}", headers=P)
    assert r.status_code == 503
    _heads_ok(r)
    recs = [x for x in caplog.records if x.name == "dashboards.serve"]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING and not recs[0].exc_info


# ---- Phase 12 (AGENT-07): wrapper + opaque-origin frame ---------------------

import base64  # noqa: E402
import hashlib  # noqa: E402


def _h(t):
    return "sha256-" + base64.b64encode(hashlib.sha256(t.encode()).digest()).decode()


def test_p12_wrapper_constants():
    assert ds.WRAPPER_SCRIPT == "document.querySelector('iframe').src = './frame' + location.search;"
    assert ds.WRAPPER_SCRIPT_HASH == _h(ds.WRAPPER_SCRIPT)
    w = ds.WRAPPER_HTML
    assert w.count("<iframe") == 1 and 'sandbox="allow-scripts"' in w
    assert 'referrerpolicy="no-referrer"' in w and ds.WRAPPER_SCRIPT in w
    for bad in ("allow-same-origin", "allow-top-navigation", "allow-popups", "allow-modals",
                "allow-downloads", "?t=", "?p="):
        assert bad not in w
    c = ds.WRAPPER_CSP
    for need in ("default-src 'none'", f"script-src '{ds.WRAPPER_SCRIPT_HASH}'",
                 "frame-src 'self'", "child-src 'self'", "base-uri 'none'",
                 "form-action 'none'", "frame-ancestors 'none'"):
        assert need in c
    assert "sandbox" not in c
    assert ds.PRELUDE_HASH == _h(ds.PRELUDE)


def test_p12_content_csp_shape():
    csp = ds.build_csp(CODE, STORED)
    assert f"'{ds.PRELUDE_HASH}'" in csp
    assert csp.endswith("frame-ancestors 'self'; sandbox allow-scripts")
    assert "allow-same-origin" not in csp


@pytest.mark.parametrize("host,want", [
    ("dash.example.test", "'self' http://dash.example.test https://dash.example.test"),
    ("localhost:9122", "'self' http://localhost:9122 https://localhost:9122"),
    (None, "'self'"), ("a;b", "'self'"), ("a b", "'self'"), ("a'b", "'self'"),
    ("a/b", "'self'"), ("*", "'self'"), ("a\nb", "'self'"), ("a" * 260, "'self'"),
    ("a:", "'self'"), ("", "'self'"), ("a:123456", "'self'"),
])
def test_p12_connect_src_host(host, want):
    base = ds.build_csp(CODE, STORED)
    csp = ds.build_csp(CODE, STORED, host=host)
    assert _directives(csp)["connect-src"] == f"connect-src {want}"
    assert csp.replace(_directives(csp)["connect-src"], "") == base.replace(
        _directives(base)["connect-src"], "")


def test_p12_content_html_prelude_and_crossorigin():
    lib = ds.dashboardlint.LIB_SRC
    for tag in (f'<script src="{lib}" integrity="sha256-x"></script>',
                f'<script integrity="sha256-x" src="{lib}"></script>'):
        out = ds.content_html("<!DOCTYPE html><html>" + tag + "<script>1</script></html>")
        assert out.startswith("<!DOCTYPE html><script>" + ds.PRELUDE + "</script><html>")
        assert out.count('crossorigin="anonymous"') == 1
        assert out.count("<script>1</script>") == 1
    already = f'<script src="{lib}" crossorigin="use-credentials"></script>'
    assert ds.content_html(already).count("crossorigin") == 1
    other = '<script src="/x.js"></script>'
    assert "crossorigin" not in ds.content_html(other)
    assert ds.content_html("<p>x</p>").startswith("<script>" + ds.PRELUDE)


def test_p12_example_gets_crossorigin():
    assert 'crossorigin="anonymous"' in ds.content_html(CODE)


def test_p12_wrapper_no_store(env):
    for path in ("/d/sales/v/3/", "/d/sales/v/1/"):
        r = env["client"].get(f"{path}?t={TOKEN}", headers=P)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert TOKEN not in r.text


def test_p12_wrapper_draft_and_bad_tokens(env):
    c = env["client"]
    r = c.get("/d/sales/v/2/?p=GOOD", headers=P)
    assert r.status_code == 200 and r.text == ds.WRAPPER_HTML
    for path in ("/d/sales/v/2/?p=BAD", "/d/sales/v/2/frame?p=BAD", "/d/sales/frame",
                 "/d/sales/frame?t=wrong", "/d/sales/v/3/frame?t=wrong"):
        _404(c.get(path, headers=P))


def test_p12_frame_sec_fetch_dest(env):
    c = env["client"]
    n = env["pool_calls"]
    _404(c.get(f"/d/sales/frame?t={TOKEN}", headers={**P, "Sec-Fetch-Dest": "document"}))
    assert env["pool_calls"] == n
    for dest in ("iframe", "frame"):
        r = c.get(f"/d/sales/frame?t={TOKEN}", headers={**P, "Sec-Fetch-Dest": dest})
        assert r.status_code == 200


def test_p12_frame_pinned_cache_and_draft(env):
    c = env["client"]
    r = c.get("/d/sales/v/2/frame?p=GOOD", headers=P)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.text == ds.content_html(CODE)
    r = c.get(f"/d/sales/v/1/frame?t={TOKEN}", headers=P)
    assert r.headers["Cache-Control"] == "no-store"


def test_p12_data_cors(env):
    c = env["client"]
    ok = c.get(f"/d/sales/data/orders?t={TOKEN}", headers=P)
    nf = c.get(f"/d/sales/data/nope?t={TOKEN}", headers=P)
    bad = c.get("/d/sales/data/orders?t=wrong", headers=P)
    env["conn"].fail = OSError("x")
    un = c.get(f"/d/sales/data/orders?t={TOKEN}", headers=P)
    assert [r.status_code for r in (ok, nf, bad, un)] == [200, 404, 404, 503]
    for r in (ok, nf, bad, un):
        assert r.headers["Access-Control-Allow-Origin"] == "*"
    env["conn"].fail = None
    page = c.get(f"/d/sales/?t={TOKEN}", headers=P)
    for r in (ok, nf, bad, un, c.get(f"/d/sales/frame?t={TOKEN}", headers=P), page):
        assert "access-control-allow-credentials" not in r.headers
    assert "access-control-allow-origin" not in page.headers


def test_p12_dns_prefetch_off(env):
    c = env["client"]
    for path in ("/d/sales/", "/d/sales/frame"):
        r = c.get(f"{path}?t={TOKEN}", headers=P)
        assert r.headers["X-DNS-Prefetch-Control"] == "off"


# ---- AGENT-07: the frame serves the canonical tree the linter checked -------------------
from app import dashboardhtml  # noqa: E402


def _unedit(served: str) -> str:
    return (served.replace("<script>" + ds.PRELUDE + "</script>", "", 1)
            .replace(' crossorigin="anonymous"', "", 1))


def test_agent07_frame_body_is_the_linted_canonical_form():
    canon = dashboardhtml.canonicalize(CODE)
    assert canon.stable
    assert _unedit(ds.content_html(CODE)) == canon.html
    assert dashboardhtml.canonicalize(canon.html).html == canon.html


def test_agent07_markup_the_parser_did_not_build_is_not_served():
    code = "<!doctype html><html><body><select><div id=\"z\">x</div></select></body></html>"
    out = ds.content_html(code)
    assert "<div" not in out and 'id="z"' not in out and ">x</select>" in out


def test_agent07_attribute_entities_served_decoded_and_reescaped():
    out = ds.content_html('<!doctype html><p title="a&#34;b&lt;c">x</p>')
    assert '<p title="a&quot;b&lt;c">' in out


def test_agent07_csp_hashes_come_from_served_bytes(monkeypatch):
    seen = []
    real = dashboards.inline_script_hashes

    def spy(text):
        seen.append(text)
        return real(text)
    monkeypatch.setattr(dashboards, "inline_script_hashes", spy)
    ds.build_csp(CODE, STORED)
    assert seen == [dashboardhtml.serve_form(CODE)]


def test_agent07_unstable_code_is_never_served(env, monkeypatch):
    monkeypatch.setattr(dashboardhtml, "serve_form", lambda code: None)
    c = env["client"]
    _404(c.get(f"/d/sales/frame?t={TOKEN}", headers=P))
    _404(c.get("/d/sales/v/2/frame?p=GOOD", headers=P))
    assert not ds.servable(CODE)
    with pytest.raises(ValueError):
        ds.content_html(CODE)
    assert "script-src 'sha256-" in ds.build_csp(CODE, STORED)  # only the fixed hashes remain
    assert _directives(ds.build_csp(CODE, STORED))["script-src"] == (
        f"script-src '{LIB}' '{ds.PRELUDE_HASH}'")


def test_agent07_serve_form_is_cached_and_total():
    assert dashboardhtml.serve_form(CODE) is dashboardhtml.serve_form(CODE)
    assert dashboardhtml.serve_form("<p>x") == "<html><head></head><body><p>x</p></body></html>"


# ---- Quick 015: every version's frame carries the CSP of its own code ---------------------
def _code_for(vid: int) -> str:
    return CODE.replace("</body>", f"<script>window.icVersion = {vid};</script></body>", 1)


class PerVersionConn(Conn):
    """CODE_SQL answers with the code and stored hashes of the version id it is asked for."""

    def __init__(self, versions):
        super().__init__(versions)
        self.code_ids = []

    async def fetchrow(self, sql, *a):
        if sql == ds.CODE_SQL:
            self.calls.append(sql)
            self.code_ids.append(a[0])
            code = _code_for(a[0])
            return {"code": code, "script_hashes": dashboards.script_hashes(code), "queries": LINT_Q}
        return await super().fetchrow(sql, *a)


def _script_src(csp: str) -> set:
    return set(re.findall(r"'(sha256-[^']+)'", _directives(csp)["script-src"]))


def test_q015_each_version_frame_gets_its_own_csp(env):
    conn = PerVersionConn({None: ("published", 11), 1: ("retired", 10), 2: ("draft", 12),
                           3: ("published", 11)})
    env["conn"] = conn
    c = env["client"]
    h = {**P, "Host": "dash.test"}
    seen = {}
    for path, vid in ((f"/d/sales/frame?t={TOKEN}", 11), (f"/d/sales/v/1/frame?t={TOKEN}", 10),
                      (f"/d/sales/v/3/frame?t={TOKEN}", 11), ("/d/sales/v/2/frame?p=GOOD", 12)):
        r = c.get(path, headers=h)
        code = _code_for(vid)
        assert r.status_code == 200, path
        assert conn.code_ids[-1] == vid, path
        assert r.text == ds.content_html(code), path
        assert r.headers["content-security-policy"] == ds.build_csp(
            code, dashboards.script_hashes(code), host="dash.test"), path
        seen[vid] = _script_src(r.headers["content-security-policy"])
    own = {vid: set(dashboards.script_hashes(_code_for(vid))) for vid in seen}
    for a in seen:
        assert own[a] <= seen[a]
        for b in seen:
            if a != b:
                assert own[b] - own[a], "versions must differ for this check to mean anything"
                assert not (seen[a] & (own[b] - own[a])), f"v{a} frame admits a v{b}-only script"


# ---- Quick 029 F4: published and retired versions are re-linted at /frame ----
BAD_CODE = CODE.replace("</body>", "<marquee>x</marquee></body>", 1)


@pytest.fixture
def lintenv(env):
    ds._lint_rules.cache_clear()
    ds._lint_warned.clear()
    yield env
    ds._lint_rules.cache_clear()
    ds._lint_warned.clear()


def test_f4_published_and_retired_lint_failing_get_404_one_warning(lintenv, caplog):
    assert BAD_CODE != CODE
    lintenv["conn"].code_by_vid.update({11: BAD_CODE, 10: BAD_CODE})
    c = lintenv["client"]
    with caplog.at_level(logging.WARNING):
        for path in ("/d/sales/v/3/frame", "/d/sales/v/3/frame", "/d/sales/v/1/frame",
                     "/d/sales/frame"):
            _404(c.get(f"{path}?t={TOKEN}", headers=P))
    warns = [r for r in caplog.records if "fails the current lint" in r.getMessage()]
    assert len(warns) == 2          # one per version id (11 and 10), not per request
    assert TOKEN not in caplog.text


def test_f4_clean_version_unchanged(lintenv):
    r = lintenv["client"].get(f"/d/sales/v/3/frame?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.text == ds.content_html(CODE)
    assert "content-security-policy" in r.headers


def test_f4_draft_is_not_relinted_with_valid_preview_token(lintenv):
    """Pinned decision: draft frames are gated by lint-gated preview tokens, and the
    nav check mints hostile-draft tokens straight in the DB on purpose."""
    lintenv["conn"].code_by_vid[12] = BAD_CODE
    r = lintenv["client"].get("/d/sales/v/2/frame?p=GOOD", headers=P)
    assert r.status_code == 200


def test_f4_lint_runs_once_per_version_content(lintenv, monkeypatch):
    from app import dashboardlint
    n = {"c": 0}
    real = dashboardlint.lint

    def counting(code, queries):
        n["c"] += 1
        return real(code, queries)
    monkeypatch.setattr(dashboardlint, "lint", counting)
    c = lintenv["client"]
    for _ in range(3):
        assert c.get(f"/d/sales/v/3/frame?t={TOKEN}", headers=P).status_code == 200
    assert n["c"] == 1


def test_f4_lint_exception_fails_closed(lintenv, monkeypatch):
    from app import dashboardlint

    def boom(code, queries):
        raise RuntimeError("x")
    monkeypatch.setattr(dashboardlint, "lint", boom)
    _404(lintenv["client"].get(f"/d/sales/v/3/frame?t={TOKEN}", headers=P))
