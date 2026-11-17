"""Dashboard serving routes (/d/), Phase 4 (SERVE-01..03).

Serves agent-authored dashboards ONLY to requests that came through the dashboards
nginx origin (contract section 8). The proxy header is not authentication; it only
stops agent HTML from ever rendering on the gateway or ui origin, where the ops key
sits in localStorage.

The view/preview token is the gate. Data routes are never cached (DASH-04 rotation
is live); pinned HTML may be cached for up to a day (PINNED_HTML_CACHE), so a
rotated token's already-fetched HTML stays viewable in that browser for that
long. Tokens are never logged, and never echoed.

Data comes only from dashboard_snapshots. This module must never import the tenant
SQL gateway module or open a tenant pool (SERVE-02).

No ops-key dependency: viewers do not hold the ops key.

Phase 18: every response is no-store, including legacy HTML frames, so turning
dashboard_legacy_html off takes effect on the next load. Spec versions are served by a
constant app-owned frame plus the token-gated ./spec JSON route.
"""
import base64
import functools
import hashlib
import hmac
import html
import json
import logging
import re

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import Response

from . import dashboardhtml, dashboardlint, dashboards, dashsettings, db, kpihistory

log = logging.getLogger("dashboards.serve")

PROXY_HEADER = "x-ic-dashboards-proxy"

# A test keeps this equal to vendor.json's csp_hash set (the backend image has no
# dashboards/ directory).
LIB_CSP_HASHES = frozenset({"sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo=",
                            "sha256-515ub9x4JPVTtBbkxrV9rLXo4+Qvz0LRoAy16ivCYy0="})
# Legacy HTML frames admit only the Chart.js 4.5.1 hash, exactly as before Phase 22: the
# ic-chartlib bundle belongs to the v2 renderer frame only.
LEGACY_LIB_CSP_HASHES = frozenset({"sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="})

_HASH_RE = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
VERSION_RE = re.compile(r"^[1-9][0-9]{0,8}$")
TOKEN_MAX = dashboards.PREVIEW_TOKEN_MAX

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}

# Defense in depth if a data URL is ever opened as a document.
DATA_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"

# Phase 9 (DEPLOY-02): bounded to one day so CSP fixes and token rotation reach
# viewers promptly (WR-02). Versions are immutable (DB trigger) and the CSP derives from
# stored code, so pinned published/retired HTML can be cached. The URL carries a
# bearer token, so "private" keeps shared caches out. ACQUIRE_TIMEOUT (WR-07): a flood
# must not queue forever on the shared memory pool; a timeout is an OSError -> 503.
PINNED_HTML_CACHE = "private, max-age=86400, immutable"
ACQUIRE_TIMEOUT = 2.0

# ── Phase 12 (AGENT-07): wrapper page + opaque-origin content frame ──────────────
# The page URLs serve a constant wrapper whose CSP allows frames only from this origin;
# the agent HTML is served at .../frame and rendered in a sandbox="allow-scripts" frame
# (opaque origin: no parent DOM access, no top navigation, no popups). The wrapper body
# is constant, so no token appears in it; the token reaches the frame only through
# location.search.
def _sha256_token(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


WRAPPER_SCRIPT = "document.querySelector('iframe').src = './frame' + location.search;"
WRAPPER_SCRIPT_HASH = _sha256_token(WRAPPER_SCRIPT)
WRAPPER_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Dashboard</title>"
    "<style>html,body{margin:0;height:100%;background:#080C14}"
    "iframe{display:block;border:0;width:100%;height:100%}</style></head>"
    '<body><iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Dashboard">'
    "</iframe><script>" + WRAPPER_SCRIPT + "</script></body></html>\n")
WRAPPER_CSP = (
    f"default-src 'none'; script-src '{WRAPPER_SCRIPT_HASH}'; style-src 'unsafe-inline'; "
    "frame-src 'self'; child-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'")

# ── Phase 22 (DCAP-14, C-12): spec-only wrapper with a strict listen-only message channel ──
# Only spec v2 versions (ic-render-2.0.0) get this wrapper; v1 spec and legacy HTML versions
# keep WRAPPER_HTML above, byte for byte. The script only listens: it accepts two message
# shapes from the frame's own window (a URL fragment to mirror, and an Ask link to show).
SPEC_WRAPPER_SCRIPT = r"""(function () {
  'use strict';
  var FRAME_RE = /^\.\/(v\/[1-9][0-9]{0,8}\/)?frame$/;
  var HASH_RE = /^[A-Za-z0-9_=,.:;%~-]{0,600}$/;
  var TILE_RE = /^[a-z][a-z0-9_]{0,47}$/;
  var BASE_RE = /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/;
  var SLUG_RE = /^\/d\/([a-z0-9-]+)\//;
  var CONTROL_RE = /[\u0000-\u001F\u007F-\u009F]/;
  var BIDI_RE = /[\u202A-\u202E\u2066-\u2069]/;
  var LONE_RE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?:^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  var frame = document.querySelector('iframe');
  var link = document.getElementById('ic-ask');
  var body = document.body;
  var askBase = body.getAttribute('data-ask-base') || '';
  if (!BASE_RE.test(askBase)) { askBase = ''; }
  var framePath = body.getAttribute('data-frame') || '';
  var m = SLUG_RE.exec(location.pathname);
  var slug = m ? m[1] : '';

  if (FRAME_RE.test(framePath)) {
    frame.src = framePath + location.search + location.hash;
  }

  function exactKeys(d, keys) {
    var got = Object.keys(d).sort();
    if (got.length !== keys.length) { return false; }
    for (var i = 0; i < keys.length; i++) {
      if (got[i] !== keys[i]) { return false; }
    }
    return true;
  }

  function titleOk(t) {
    if (typeof t !== 'string') { return false; }
    var n = Array.from(t).length;
    if (n < 1 || n > 120) { return false; }
    return !CONTROL_RE.test(t) && !BIDI_RE.test(t) && !LONE_RE.test(t);
  }

  window.addEventListener('message', function (e) {
    if (!frame || e.source !== frame.contentWindow) { return; }
    var d = e.data;
    if (Object.prototype.toString.call(d) !== '[object Object]') { return; }
    if (d.ic !== 1) { return; }
    if (d.type === 'state') {
      if (!exactKeys(d, ['hash', 'ic', 'type'])) { return; }
      if (typeof d.hash !== 'string' || !HASH_RE.test(d.hash)) { return; }
      history.replaceState(null, '', '#' + d.hash);
      return;
    }
    if (d.type === 'ask') {
      if (!exactKeys(d, ['ic', 'tile', 'title', 'type'])) { return; }
      if (!askBase || !slug || !link) { return; }
      if (typeof d.tile !== 'string' || !TILE_RE.test(d.tile)) { return; }
      if (!titleOk(d.title)) { return; }
      link.textContent = 'Ask about: ' + d.title;
      link.href = askBase + '/?ask=' + encodeURIComponent('Explain "' + d.title + '" on the dashboard ' + slug);
      link.hidden = false;
    }
  });
})();"""
SPEC_WRAPPER_SCRIPT_HASH = _sha256_token(SPEC_WRAPPER_SCRIPT)
SPEC_WRAPPER_CSP = WRAPPER_CSP.replace(WRAPPER_SCRIPT_HASH, SPEC_WRAPPER_SCRIPT_HASH)
_SPEC_WRAPPER_STYLE = (
    "html,body{margin:0;height:100%;background:#080C14}"
    "iframe{display:block;border:0;width:100%;height:100%}"
    "#ic-ask{position:fixed;right:12px;bottom:12px;font:13px system-ui,sans-serif;background:#0E1420;"
    "color:#F0F4F8;border:1px solid #1E2D42;border-radius:6px;padding:8px 12px;text-decoration:none}"
    "#ic-ask[hidden]{display:none}")
_ASK_BASE_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
_FRAME_PATH_RE = re.compile(r"^\./(v/[1-9][0-9]{0,8}/)?frame$")


def spec_wrapper_html(ask_base: str, frame_path: str) -> str:
    """The v2 wrapper. data-ask-base and data-frame are its only per-request values (C-12);
    no token appears in it. A bad ask_base becomes empty (no Ask link); a bad frame_path raises."""
    if not isinstance(frame_path, str) or not _FRAME_PATH_RE.fullmatch(frame_path):
        raise ValueError("bad frame path")
    if not isinstance(ask_base, str) or not _ASK_BASE_RE.fullmatch(ask_base):
        ask_base = ""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Dashboard</title><style>" + _SPEC_WRAPPER_STYLE + "</style></head>"
        f'<body data-ask-base="{html.escape(ask_base, quote=True)}" '
        f'data-frame="{html.escape(frame_path, quote=True)}">'
        '<a id="ic-ask" hidden target="_blank" rel="noopener noreferrer"></a>'
        '<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Dashboard"></iframe>'
        "<script>" + SPEC_WRAPPER_SCRIPT + "</script></body></html>\n")
# ── end Phase 22 wrapper block ───────────────────────────────────────────────────────

# WebRTC is not governed by CSP (webrtc 'block' is not enforced); TURN over TCP leaked
# from the sandbox in Chrome 156. The constructors are redefined as non-configurable
# undefined before any agent script runs.
PRELUDE = (
    "(function(){['RTCPeerConnection','webkitRTCPeerConnection','RTCDataChannel',"
    "'RTCIceTransport','RTCSctpTransport'].forEach(function(n){try{"
    "Object.defineProperty(window,n,{value:undefined,writable:false,configurable:false});"
    "}catch(e){}});})();")
PRELUDE_HASH = _sha256_token(PRELUDE)

# Host values that may be echoed into connect-src (letters, digits, dots, hyphens,
# optional port); anything else falls back to 'self' alone (header injection guard).
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")

_DOCTYPE_RE = re.compile(r"\A\s*<!doctype html[^>]*>", re.I)
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>", re.I)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_CROSSORIGIN_RE = re.compile(r"\bcrossorigin\b", re.I)


def _is_lib_tag(tag: str) -> bool:
    m = _SRC_RE.search(tag)
    if not m:
        return False
    src = next(g for g in m.groups() if g is not None)
    return src == dashboardlint.LIB_SRC and not _CROSSORIGIN_RE.search(tag)


def _add_crossorigin(m: re.Match) -> str:
    tag = m.group(0)
    if not _is_lib_tag(tag):
        return tag
    return tag[:7] + ' crossorigin="anonymous"' + tag[7:]


def content_html(code: str) -> str:
    """The content-frame body for stored (immutable) code.

    AGENT-07: the frame serves the canonical serialisation of the tree a spec-conformant
    HTML5 parser builds from the code (dashboardhtml.serve_form), which is exactly what the
    linter checked, so markup one parser hides cannot reach the browser unchecked. Then the
    serve-time edits: crossorigin on the lib tag (SRI needs CORS mode in an opaque origin)
    and the WebRTC prelude right after the doctype. Raises ValueError when the code has no
    stable canonical form (callers check servable() first; such code is never served)."""
    canonical = dashboardhtml.serve_form(code)
    if canonical is None:
        raise ValueError("dashboard code has no stable canonical form")
    code = _SCRIPT_TAG_RE.sub(_add_crossorigin, canonical)
    prelude = "<script>" + PRELUDE + "</script>"
    m = _DOCTYPE_RE.match(code)
    if m:
        return code[:m.end()] + prelude + code[m.end():]
    return prelude + code


def servable(code) -> bool:
    """AGENT-07: only code with a stable canonical form is ever rendered."""
    return isinstance(code, str) and dashboardhtml.serve_form(code) is not None
# ── end Phase 12 block ───────────────────────────────────────────────────────────

CURRENT_AUTH_SQL = (
    "SELECT d.view_token, v.id AS version_id, v.status FROM dashboards d "
    "JOIN dashboard_versions v ON v.id = d.current_version_id WHERE d.slug = $1")
PINNED_AUTH_SQL = (
    "SELECT d.view_token, v.id AS version_id, v.status FROM dashboards d "
    "JOIN dashboard_versions v ON v.dashboard_id = d.id WHERE d.slug = $1 AND v.version = $2")
CODE_SQL = "SELECT code, script_hashes, queries FROM dashboard_versions WHERE id = $1"


@functools.lru_cache(maxsize=64)
def _lint_rules(code: str, queries_text: str) -> tuple[str, ...]:
    """Quick 029 F4: sorted error rule names the current lint finds in a stored version.
    Versions are immutable, so the verdict is cached per content. Fails closed."""
    try:
        from . import dashboardlint
        errors = dashboardlint.lint(code, json.loads(queries_text or "[]") or [])["errors"]
        return tuple(sorted({e["rule"] for e in errors}))
    except Exception:
        return ("lint_error",)


_lint_warned: set = set()

DATA_SQL = (
    # Phase 17 (CONS-03): data_as_of is how old the data is; the renderer prints it
    "SELECT json_build_object('query_id', s.query_id, 'columns', s.columns, "
    "'rows', s.data, 'refreshed_at', s.refreshed_at, 'data_as_of', s.data_as_of, "
    "'timezone', s.day_timezone, 'error', s.error)::text "
    "FROM dashboard_snapshots s WHERE s.version_id = $1 AND s.query_id = $2")


# Phase 12: data routes are read from an opaque-origin frame (CORS, Origin: null), so
# their failures must stay readable too. No cookies or credentials exist on this origin;
# no credentials header is ever sent.
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
# Link-hint ladder step 1: no speculative DNS lookups for hosts named in agent HTML.
HTML_EXTRA_HEADERS = {"X-DNS-Prefetch-Control": "off"}


def not_found(cors: bool = False) -> Response:
    return Response("Not found\n", status_code=404, media_type="text/plain",
                    headers={**BASE_HEADERS, **(CORS_HEADERS if cors else {})})


def unavailable(cors: bool = False) -> Response:
    return Response("Service unavailable\n", status_code=503, media_type="text/plain",
                    headers={**BASE_HEADERS, **(CORS_HEADERS if cors else {})})


def one_param(request: Request, name: str) -> str | None:
    vals = request.query_params.getlist(name)
    if len(vals) != 1 or not vals[0] or len(vals[0]) > TOKEN_MAX:
        return None
    return vals[0]


def token_matches(given, stored) -> bool:
    if not isinstance(given, str) or not isinstance(stored, str):
        return False
    return hmac.compare_digest(given.encode("utf-8"), stored.encode("utf-8"))


def build_csp(code: str, stored, host: str | None = None) -> str:
    """Contract section 6 header for the content frame (Phase 12: served at /frame).
    script-src keeps only stored hashes that are also real inline hashes of this version
    or the vendored library hash (W1): an agent-chosen integrity token would otherwise
    admit a runtime-injected cross-origin script (browser-confirmed in Chrome 156). The
    serve-time prelude hash is always present, so script-src is never 'none'.
    AGENT-07: the inline hashes are read from the canonical bytes content_html serves.
    Phase 12: sandbox allow-scripts only (opaque origin, never the same-origin flag);
    connect-src also names the request's own dashboards origin when the Host header is
    plain, so data fetches do not depend on how a browser matches 'self' for an opaque
    origin."""
    served = dashboardhtml.serve_form(code) if isinstance(code, str) else None
    allowed = (dashboards.inline_script_hashes(served) if served is not None
               else frozenset()) | LEGACY_LIB_CSP_HASHES
    kept = [h for h in dict.fromkeys(stored or [])
            if isinstance(h, str) and _HASH_RE.fullmatch(h) and h in allowed]
    kept.append(PRELUDE_HASH)
    toks = " ".join(f"'{h}'" for h in dict.fromkeys(kept))
    connect = "'self'"
    if isinstance(host, str) and _HOST_RE.fullmatch(host):
        connect += f" http://{host} https://{host}"
    return ("default-src 'none'; script-src " + toks +
            f"; style-src 'unsafe-inline'; img-src data:; connect-src {connect}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
            "sandbox allow-scripts")


_LOG_CUT_RE = re.compile(r"[?%]")


class MaskDashboardQuery(logging.Filter):
    """uvicorn.access args = (client, method, full_path, http_version, status).

    uvicorn logs quote(decoded path) plus the raw query, and nginx forwards the raw,
    un-normalized URI, so an encoded "?" arrives as %3F (or %253F) and leading "//" or
    dot segments survive. A canonical /d/ path never contains "%", so on any /d/-shaped
    path we cut at the first "?" or "%" (WR-01, the 04-VERIFICATION gap).
    """

    def filter(self, record):
        a = record.args
        if isinstance(a, tuple) and len(a) == 5 and isinstance(a[2], str):
            head = a[2].split("?", 1)[0].lower()
            if "d" in head.split("/") or "%3f" in head:
                m = _LOG_CUT_RE.search(a[2])
                if m:
                    record.args = (a[0], a[1], a[2][:m.start()] + "?REDACTED", a[3], a[4])
        return True


def install_access_log_mask() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, MaskDashboardQuery) for f in lg.filters):
        lg.addFilter(MaskDashboardQuery())


install_access_log_mask()

router = APIRouter(include_in_schema=False)

_ALL = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError, OSError)


def _proxied(request: Request) -> bool:
    return request.headers.get(PROXY_HEADER) == "1"


def _redirect(request: Request, slug: str, n: str | None) -> Response:
    if not _proxied(request) or not dashboards.SLUG_RE.fullmatch(slug):
        return not_found()
    if n is not None and not VERSION_RE.fullmatch(n):
        return not_found()
    raw = request.scope.get("query_string", b"").decode("latin-1")
    if any(not (0x21 <= ord(c) <= 0x7E) for c in raw):
        return not_found()
    loc = f"/d/{slug}/" if n is None else f"/d/{slug}/v/{n}/"
    if raw:
        loc += "?" + raw
    return Response(status_code=301, headers={**BASE_HEADERS, "Location": loc})


async def _authorize(conn, slug: str, n: str | None, request: Request) -> tuple[int, str] | None:
    if n is None:
        row = await conn.fetchrow(CURRENT_AUTH_SQL, slug)
    else:
        row = await conn.fetchrow(PINNED_AUTH_SQL, slug, int(n))
    if row is None:
        return None
    status = row["status"]
    if status == "published" or (status == "retired" and n is not None):
        if token_matches(one_param(request, "t"), row["view_token"]):
            return row["version_id"], status
        return None
    if status == "draft" and n is not None:
        p = one_param(request, "p")
        if p is not None and await dashboards.preview_token_ok(
                conn, version_id=row["version_id"], token=p):
            return row["version_id"], status
    return None


_V2_QIDS: dict = {}
_V2_QIDS_MAX = 512


async def _v2_data_qids(conn, vid) -> frozenset | None:
    """Phase 22 (D-03): the query ids a v2 version may serve, i.e. those a visible tile draws
    from. Hidden tiles keep their snapshots but are not readable. None for other versions.
    Versions are immutable, so the answer is cached per version id."""
    if vid in _V2_QIDS:
        return _V2_QIDS[vid]
    fmt = await conn.fetchrow(FORMAT_SQL, vid)
    if fmt is None or fmt["renderer"] != "ic-render-2.0.0":
        return None
    row = await conn.fetchrow(SPEC_SQL, vid)
    if row is None or row["spec"] is None:
        return None
    spec = dashboards._decode_json(row["spec"])
    queries = dashboards._decode_json(row["queries"]) or []
    rendered = dashboardspec.render_spec(spec, queries)
    allowed = frozenset(rendered.get("refresh") or {})
    if len(_V2_QIDS) >= _V2_QIDS_MAX:
        _V2_QIDS.clear()
    _V2_QIDS[vid] = allowed
    return allowed


async def _serve(request: Request, slug: str, n: str | None, qid: str | None,
                 frame: bool = False) -> Response:
    cors = qid is not None  # Phase 12: data failures stay readable from the opaque frame
    if not _proxied(request) or not dashboards.SLUG_RE.fullmatch(slug):
        return not_found(cors)
    if n is not None and not VERSION_RE.fullmatch(n):
        return not_found(cors)
    if qid is not None and not dashboards.QUERY_ID_RE.fullmatch(qid):
        return not_found(cors)
    # Phase 12: agent HTML is never a top-level document.
    if frame and request.headers.get("sec-fetch-dest", "").lower() == "document":
        return not_found()
    try:
        pool = await db.get_pool()
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT) as conn:
            auth = await _authorize(conn, slug, n, request)
            if auth is None:
                return not_found(cors)
            vid, status = auth
            if qid is None and not frame:
                # Phase 12: constant wrapper, always no-store, no read of the code.
                # Phase 22 (C-12): only a v2 spec version gets the channel wrapper.
                fmt = await conn.fetchrow(FORMAT_SQL, vid)
                if fmt is not None and fmt["renderer"] == "ic-render-2.0.0":
                    frame_path = "./frame"
                    if n is None:
                        vrow = await conn.fetchrow(VERSION_SQL, vid)
                        if vrow is None:
                            return not_found()
                        frame_path = f"./v/{vrow['version']}/frame"
                    return Response(
                        spec_wrapper_html(dashsettings.ask_base(), frame_path),
                        media_type="text/html; charset=utf-8", headers={
                            **BASE_HEADERS, **HTML_EXTRA_HEADERS,
                            "Content-Security-Policy": SPEC_WRAPPER_CSP})
                return Response(WRAPPER_HTML, media_type="text/html; charset=utf-8", headers={
                    **BASE_HEADERS, **HTML_EXTRA_HEADERS, "Content-Security-Policy": WRAPPER_CSP})
            if qid is None:
                # Phase 18
                p18 = await _p18_frame(conn, vid, n, status, request)
                if p18 is not None:
                    return p18
                row = await conn.fetchrow(CODE_SQL, vid)
                if row is None:
                    return not_found()
                code = row["code"]
                if not servable(code):
                    # AGENT-07: never render bytes the linter could not have checked.
                    log.warning("dashboard version %s has no stable canonical form; not served", vid)
                    return not_found()
                # Quick 029 F4: published and retired versions are re-linted against the
                # current rules. Drafts are NOT: a draft frame needs a preview token, and every
                # issuer of one lints first; the nav check also relies on this (pinned by a test).
                if status in ("published", "retired"):
                    qraw = row["queries"]
                    qtext = qraw if isinstance(qraw, str) else json.dumps(qraw or [])
                    rules = _lint_rules(code, qtext)
                    if rules:
                        if vid not in _lint_warned:
                            _lint_warned.add(vid)
                            log.warning("dashboard version %s fails the current lint (%s); not served",
                                        vid, ", ".join(rules))
                        return not_found()
                headers = {**BASE_HEADERS, **HTML_EXTRA_HEADERS,
                           "Content-Security-Policy": build_csp(
                               code, row["script_hashes"] or [],
                               host=request.headers.get("host"))}
                # Phase 18: legacy HTML frames are never cached (turning dashboard_legacy_html
                # off must take effect on the next load)
                return Response(content_html(code), media_type="text/html; charset=utf-8",
                                headers=headers)
            allowed = await _v2_data_qids(conn, vid)
            if allowed is not None and qid not in allowed:
                return not_found(cors)
            text = await conn.fetchval(DATA_SQL, vid, qid)
            if text is None:
                return not_found(cors)
            return Response(text, media_type="application/json", headers={
                **BASE_HEADERS, **CORS_HEADERS, "Content-Security-Policy": DATA_CSP})
    except _DB_ERRORS as e:
        log.warning("dashboard serve DB error: %s", type(e).__name__)
        return unavailable(cors)
    except Exception:
        log.exception("dashboard serve error")
        return unavailable(cors)


@router.api_route("/d", methods=_ALL)
async def d_bare():
    return not_found()


@router.get("/d/{slug}")
async def d_slug_redirect(request: Request, slug: str):
    return _redirect(request, slug, None)


@router.get("/d/{slug}/")
async def d_current(request: Request, slug: str):
    return await _serve(request, slug, None, None)


@router.get("/d/{slug}/data/{qid}")
async def d_current_data(request: Request, slug: str, qid: str):
    return await _serve(request, slug, None, qid)


@router.get("/d/{slug}/v/{n}")
async def d_pinned_redirect(request: Request, slug: str, n: str):
    return _redirect(request, slug, n)


@router.get("/d/{slug}/v/{n}/")
async def d_pinned(request: Request, slug: str, n: str):
    return await _serve(request, slug, n, None)


@router.get("/d/{slug}/v/{n}/data/{qid}")
async def d_pinned_data(request: Request, slug: str, n: str, qid: str):
    return await _serve(request, slug, n, qid)


# Phase 12 (AGENT-07): the content frame URLs.
@router.get("/d/{slug}/frame")
async def d_current_frame(request: Request, slug: str):
    return await _serve(request, slug, None, None, frame=True)


@router.get("/d/{slug}/v/{n}/frame")
async def d_pinned_frame(request: Request, slug: str, n: str):
    return await _serve(request, slug, n, None, frame=True)


# ── Phase 18: chart-spec frames (SPEC-02, SPEC-06) ──────────────────────────────
import json  # noqa: E402

from . import dashboardrender, dashboardspec  # noqa: E402

FORMAT_SQL = "SELECT (spec IS NOT NULL) AS is_spec, renderer FROM dashboard_versions WHERE id = $1"
# Phase 22 (C-12): the published version number, read only for an unpinned v2 wrapper.
VERSION_SQL = "SELECT version FROM dashboard_versions WHERE id = $1"
SPEC_SQL = "SELECT spec, queries, renderer FROM dashboard_versions WHERE id = $1"


def spec_frame_html(name: str) -> str | None:
    """The constant app-owned frame for a renderer: no agent string appears in it."""
    pin = dashboardrender.pin(name)
    if pin is None:
        return None
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Dashboard</title>"
        "<script>" + PRELUDE + "</script>"
        "<style>" + pin["css"] + "</style>"
        f'<script src="{pin["lib_src"]}" integrity="{pin["lib_integrity"]}" '
        'crossorigin="anonymous"></script>'
        '</head><body><main id="ic-root"></main>'
        f'<script src="{pin["src"]}" integrity="{pin["csp_hash"]}" '
        'crossorigin="anonymous"></script></body></html>\n')


def build_spec_csp(name: str, host: str | None = None) -> str | None:
    """Hash-only CSP from the immutable renderer pin; stored script_hashes are not used."""
    pin = dashboardrender.pin(name)
    if pin is None:
        return None
    connect = "'self'"
    if isinstance(host, str) and _HOST_RE.fullmatch(host):
        connect += f" http://{host} https://{host}"
    return (f"default-src 'none'; script-src '{PRELUDE_HASH}' '{pin['lib_csp_hash']}' "
            f"'{pin['csp_hash']}'; style-src '{pin['css_hash']}'; connect-src {connect}; "
            "base-uri 'none'; form-action 'none'; "
            "require-trusted-types-for 'script'; trusted-types 'none'; "
            "frame-ancestors 'self'; sandbox allow-scripts")


NEEDS_CONVERSION_CSS = (
    "html,body{margin:0;background:#080C14;color:#F0F4F8;"
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}"
    "main{max-width:560px;margin:0 auto;padding:48px 16px}"
    "h1{font-size:20px}p{color:#A0AEC0;line-height:1.5}")
NEEDS_CONVERSION_CSS_HASH = _sha256_token(NEEDS_CONVERSION_CSS)
NEEDS_CONVERSION_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Dashboard</title><style>" + NEEDS_CONVERSION_CSS + "</style></head><body><main>"
    "<h1>This dashboard needs conversion</h1>"
    "<p>This dashboard uses the old format, which is switched off. "
    "The owner can ask the agent to convert it to the current chart format.</p>"
    "</main></body></html>\n")
NEEDS_CONVERSION_CSP = (
    f"default-src 'none'; style-src '{NEEDS_CONVERSION_CSS_HASH}'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'; sandbox")


async def _p18_frame(conn, vid, n, status, request) -> Response | None:
    """Phase 18 hook for the frame branch: spec frame, needs-conversion page, or None
    (legacy HTML on: fall through to the v1.0 path)."""
    row = await conn.fetchrow(FORMAT_SQL, vid)
    if row is None:
        return not_found()
    headers = {**BASE_HEADERS, **HTML_EXTRA_HEADERS}
    if row["is_spec"]:
        name = row["renderer"]
        body = spec_frame_html(name)
        csp = build_spec_csp(name, request.headers.get("host"))
        if body is None or csp is None:
            log.warning("dashboard version %s has unknown renderer; not served", vid)
            return not_found()
        headers["Content-Security-Policy"] = csp
        if n is not None and status in ("published", "retired"):
            headers["Cache-Control"] = PINNED_HTML_CACHE
        return Response(body, media_type="text/html; charset=utf-8", headers=headers)
    if not dashboards.legacy_html_on():
        headers["Content-Security-Policy"] = NEEDS_CONVERSION_CSP
        return Response(NEEDS_CONVERSION_HTML, media_type="text/html; charset=utf-8",
                        headers=headers)
    return None


async def _serve_spec(request: Request, slug: str, n: str | None) -> Response:
    if not _proxied(request) or not dashboards.SLUG_RE.fullmatch(slug):
        return not_found(True)
    if n is not None and not VERSION_RE.fullmatch(n):
        return not_found(True)
    try:
        pool = await db.get_pool()
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT) as conn:
            auth = await _authorize(conn, slug, n, request)
            if auth is None:
                return not_found(True)
            row = await conn.fetchrow(SPEC_SQL, auth[0])
            if row is None or row["spec"] is None:
                return not_found(True)
            spec = dashboards._decode_json(row["spec"])
            queries = dashboards._decode_json(row["queries"]) or []
            rendered = dashboardspec.render_spec(spec, queries)
            if rendered.get("render_version") == 2:  # Phase 22: the operator zone
                rendered["timezone"] = kpihistory._tz()
            text = json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
            return Response(text, media_type="application/json", headers={
                **BASE_HEADERS, **CORS_HEADERS, "Content-Security-Policy": DATA_CSP})
    except _DB_ERRORS as e:
        log.warning("dashboard serve DB error: %s", type(e).__name__)
        return unavailable(True)
    except Exception:
        log.exception("dashboard serve error")
        return unavailable(True)


@router.get("/d/{slug}/spec")
async def d_current_spec(request: Request, slug: str):
    return await _serve_spec(request, slug, None)


@router.get("/d/{slug}/v/{n}/spec")
async def d_pinned_spec(request: Request, slug: str, n: str):
    return await _serve_spec(request, slug, n)
# ── End Phase 18 ─────────────────────────────────────────────────────────────────


# Must stay the last route: uniform 404 for everything else under /d/.
@router.api_route("/d/{rest:path}", methods=_ALL)
async def d_catch_all(rest: str):
    return not_found()
