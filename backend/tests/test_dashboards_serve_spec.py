"""Phase 18 plan 04 (SPEC-02, SPEC-06): spec frame, hash-only CSP, ./spec route, legacy gate."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import dashboardlint, dashboardrender, dashboards, dashboardspec, main
from app import dashboards_serve as ds

P = {"X-IC-Dashboards-Proxy": "1"}
TOKEN = "view-token-123"
NAME = dashboardrender.CURRENT_RENDERER
PIN = dashboardrender.RENDERERS[NAME]
SECRET_SQL = "SELECT secret_col FROM hidden_table WHERE x = 1"


def _stored(title):
    return {
        "title": title, "subtitle": None,
        "layout": {"rows": [["t1", "t2"]]},
        "tiles": [
            {"id": "t1", "type": "stat", "title": title + " tile", "qid": "q_orders",
             "column": "total", "measure": {"name": "revenue", "filters": {"k": "v"}}},
            {"id": "t2", "type": "text", "text": "hello"},
        ]}


QUERIES = [{"id": "q_orders", "sql": SECRET_SQL, "refresh_minutes": 30}]
EXAMPLE_CODE = "<!doctype html><html><body><p>old page</p></body></html>"


class Conn:
    def __init__(self):
        self.versions = {None: ("published", 11), 1: ("retired", 10), 2: ("draft", 12),
                         3: ("published", 11), 4: ("published", 14)}
        self.kinds = {11: ("spec", "Alpha"), 10: ("spec", "Beta"), 12: ("spec", "Gamma"),
                      14: ("html", None)}
        self.renderer = {}

    async def fetchrow(self, sql, *a):
        if sql in (ds.CURRENT_AUTH_SQL, ds.PINNED_AUTH_SQL):
            key = None if sql == ds.CURRENT_AUTH_SQL else a[1]
            if a[0] != "sales" or key not in self.versions:
                return None
            status, vid = self.versions[key]
            return {"view_token": TOKEN, "version_id": vid, "status": status}
        vid = a[0]
        kind, title = self.kinds.get(vid, (None, None))
        if sql == ds.FORMAT_SQL:
            if kind is None:
                return None
            return {"is_spec": kind == "spec",
                    "renderer": self.renderer.get(vid, NAME) if kind == "spec" else None}
        if sql == ds.SPEC_SQL:
            if kind == "spec":
                return {"spec": json.dumps(_stored(title)), "queries": json.dumps(QUERIES),
                        "renderer": NAME}
            if kind == "html":
                return {"spec": None, "queries": json.dumps([]), "renderer": None}
            return None
        if sql == ds.CODE_SQL:
            return {"code": EXAMPLE_CODE, "script_hashes": [], "queries": "[]"}  # Quick 029 F4 re-lint reads queries
        return None

    async def fetchval(self, sql, *a):
        return None


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self, timeout=None):
        c = self.conn

        class A:
            async def __aenter__(self_):
                return c

            async def __aexit__(self_, *e):
                return False
        return A()


@pytest.fixture
def env(monkeypatch):
    conn = Conn()
    state = {"conn": conn, "legacy": False}

    async def get_pool():
        return Pool(conn)
    monkeypatch.setattr(ds, "db", SimpleNamespace(get_pool=get_pool))

    async def ok(c, *, version_id, token):
        return token == "GOOD" and version_id == 12
    monkeypatch.setattr(dashboards, "preview_token_ok", ok)
    monkeypatch.setattr(dashboards, "legacy_html_on", lambda: state["legacy"])
    state["client"] = TestClient(main.app, follow_redirects=False)
    return state


def _dir(csp):
    return {p.split(" ", 1)[0]: p for p in csp.split("; ")}


# ---- frame builders ----------------------------------------------------------

def test_spec_frame_html_shape():
    h = ds.spec_frame_html(NAME)
    assert h.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in h and "<title>Dashboard</title>" in h
    assert "<script>" + ds.PRELUDE + "</script>" in h
    assert "<style>" + PIN["css"] + "</style>" in h
    assert (f'<script src="{dashboardlint.LIB_SRC}" integrity="{dashboardlint.LIB_INTEGRITY}" '
            'crossorigin="anonymous"></script>') in h
    assert '<body><main id="ic-root"></main>' in h
    assert h.rstrip().endswith(
        f'<script src="{PIN["src"]}" integrity="{PIN["csp_hash"]}" '
        'crossorigin="anonymous"></script></body></html>')
    assert h.index(dashboardlint.LIB_SRC) < h.index(PIN["src"])
    assert ds.spec_frame_html(NAME) == h


def test_spec_frame_unknown_renderer():
    assert ds.spec_frame_html("unknown") is None
    assert ds.build_spec_csp("unknown", "x") is None


def test_build_spec_csp_exact():
    assert ds.build_spec_csp(NAME, "127.0.0.1:9282") == (
        f"default-src 'none'; script-src '{ds.PRELUDE_HASH}' '{dashboardrender.LIB_CSP_HASH}' "
        f"'{PIN['csp_hash']}'; style-src '{PIN['css_hash']}'; "
        "connect-src 'self' http://127.0.0.1:9282 https://127.0.0.1:9282; base-uri 'none'; "
        "form-action 'none'; require-trusted-types-for 'script'; trusted-types 'none'; "
        "frame-ancestors 'self'; sandbox allow-scripts")


@pytest.mark.parametrize("host", [None, "a;b", "a b", "", "a:123456", "*"])
def test_build_spec_csp_bad_host(host):
    assert _dir(ds.build_spec_csp(NAME, host))["connect-src"] == "connect-src 'self'"


def test_csp_never_allows_unsafe():
    csp = ds.build_spec_csp(NAME, "h.test")
    for bad in ("unsafe-inline", "unsafe-eval", "allow-same-origin", "img-src"):
        assert bad not in csp
    assert _dir(csp)["style-src"] == f"style-src '{PIN['css_hash']}'"


# ---- /frame ------------------------------------------------------------------

def test_spec_frame_response(env):
    r = env["client"].get("/d/sales/v/2/frame?p=GOOD", headers={**P, "Host": "dash.test"})
    assert r.status_code == 200
    assert r.text == ds.spec_frame_html(NAME)
    assert r.headers["content-security-policy"] == ds.build_spec_csp(NAME, "dash.test")
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-DNS-Prefetch-Control"] == "off"
    for k, v in ds.BASE_HEADERS.items():
        assert r.headers[k] == v


def test_spec_frames_byte_identical_and_constant(env):
    c = env["client"]
    a = c.get(f"/d/sales/frame?t={TOKEN}", headers=P)
    b = c.get("/d/sales/v/2/frame?p=GOOD", headers=P)
    d = c.get(f"/d/sales/v/1/frame?t={TOKEN}", headers=P)
    assert a.content == b.content == d.content
    for needle in ("Alpha", "Beta", "Gamma", "q_orders", "hello"):
        assert needle not in a.text


def test_spec_frame_cache_rules(env):
    c = env["client"]
    assert c.get(f"/d/sales/v/1/frame?t={TOKEN}", headers=P).headers["Cache-Control"] == (
        ds.PINNED_HTML_CACHE)
    assert c.get(f"/d/sales/v/3/frame?t={TOKEN}", headers=P).headers["Cache-Control"] == (
        ds.PINNED_HTML_CACHE)
    assert c.get(f"/d/sales/frame?t={TOKEN}", headers=P).headers["Cache-Control"] == "no-store"


def test_spec_frame_document_dest_404(env):
    r = env["client"].get(f"/d/sales/frame?t={TOKEN}",
                          headers={**P, "Sec-Fetch-Dest": "document"})
    assert r.status_code == 404


def test_spec_frame_unknown_renderer_404(env):
    env["conn"].renderer[11] = "ic-render-9.9.9"
    r = env["client"].get(f"/d/sales/frame?t={TOKEN}", headers=P)
    assert r.status_code == 404


def test_spec_frame_bad_token_404(env):
    assert env["client"].get("/d/sales/frame?t=wrong", headers=P).status_code == 404


# ---- ./spec ------------------------------------------------------------------

def test_spec_route_current(env):
    r = env["client"].get(f"/d/sales/spec?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == dashboardspec.render_spec(_stored("Alpha"), QUERIES)
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert r.headers["content-security-policy"] == ds.DATA_CSP
    assert r.headers["Cache-Control"] == "no-store"


def test_spec_route_draft_preview(env):
    r = env["client"].get("/d/sales/v/2/spec?p=GOOD", headers=P)
    assert r.status_code == 200
    assert r.json()["title"] == "Gamma"


def test_spec_route_pinned_retired(env):
    r = env["client"].get(f"/d/sales/v/1/spec?t={TOKEN}", headers=P)
    assert r.status_code == 200 and r.json()["title"] == "Beta"


def test_spec_route_no_sql_leak(env):
    r = env["client"].get(f"/d/sales/spec?t={TOKEN}", headers=P)
    assert SECRET_SQL not in r.text and "hidden_table" not in r.text
    keys = set()

    def walk(o):
        if isinstance(o, dict):
            keys.update(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r.json())
    assert not keys & {"sql", "measure", "request", "filters"}


@pytest.mark.parametrize("path", [
    "/d/sales/spec?t=wrong", "/d/other/spec?t=" + TOKEN, "/d/sales/v/2/spec?p=BAD",
    "/d/sales/v/4/spec?t=" + TOKEN, "/d/sales/v/abc/spec?t=" + TOKEN, "/d/Bad_Slug/spec?t=x"])
def test_spec_route_uniform_404(env, path):
    r = env["client"].get(path, headers=P)
    assert r.status_code == 404 and r.content == b"Not found\n"
    assert r.headers["Access-Control-Allow-Origin"] == "*"


def test_spec_route_needs_proxy_header(env):
    r = env["client"].get(f"/d/sales/spec?t={TOKEN}")
    assert r.status_code == 404


def test_spec_route_db_error_503(env, monkeypatch):
    async def bad():
        raise OSError("down")
    monkeypatch.setattr(ds, "db", SimpleNamespace(get_pool=bad))
    r = env["client"].get(f"/d/sales/spec?t={TOKEN}", headers=P)
    assert r.status_code == 503 and r.headers["Access-Control-Allow-Origin"] == "*"


# ---- legacy gate -------------------------------------------------------------

def test_needs_conversion_when_legacy_off(env):
    r = env["client"].get(f"/d/sales/v/4/frame?t={TOKEN}", headers=P)
    assert r.status_code == 200
    assert r.text == ds.NEEDS_CONVERSION_HTML
    assert "<script" not in r.text and "old page" not in r.text
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["content-security-policy"] == (
        f"default-src 'none'; style-src '{ds.NEEDS_CONVERSION_CSS_HASH}'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'; sandbox")
    assert ds.NEEDS_CONVERSION_CSS_HASH == ds._sha256_token(ds.NEEDS_CONVERSION_CSS)
    assert "ask the agent to convert" in ds.NEEDS_CONVERSION_HTML


def test_legacy_on_serves_v10_bytes_no_store(env):
    env["legacy"] = True
    r = env["client"].get(f"/d/sales/v/4/frame?t={TOKEN}", headers={**P, "Host": "dash.test"})
    assert r.status_code == 200
    assert r.text == ds.content_html(EXAMPLE_CODE)
    assert r.headers["content-security-policy"] == ds.build_csp(EXAMPLE_CODE, [], host="dash.test")
    assert r.headers["Cache-Control"] == "no-store"


# ---- routes and hygiene ------------------------------------------------------

def test_spec_routes_before_catch_all():
    p = [r.path for r in ds.router.routes]
    last = p.index("/d/{rest:path}")
    assert p.index("/d/{slug}/spec") < last and p.index("/d/{slug}/v/{n}/spec") < last
    assert last == len(p) - 1


def test_source_hygiene():
    src = Path(ds.__file__).read_text(encoding="utf-8")
    assert "guardrail" not in src.lower() and "create_pool" not in src
    assert re.search(r"require-trusted-types-for 'script'; trusted-types 'none'", src)
