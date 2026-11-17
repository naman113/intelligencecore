"""The single contract gate for agent-authored dashboard HTML and query declarations.

Defence in depth behind the per-version CSP and human review before publish.
Pure and deterministic: no database, no network, no LLM, no logging of code.
Used by preview_dashboard, save_dashboard_version and the preview-mint route.
AGENT-07: rules read the tree a spec-conformant HTML5 parser builds (dashboardhtml), and
/d/ serves the canonical serialisation of that tree, so the browser parses what was checked.

lint(code, queries) never raises. Errors block; warnings advise. Every finding is
{"rule", "line", "message"} and the message says how to fix it.

Contract section 6 is enforced as an allowlist on globals, computed keys and runtime
string construction (CSP does not govern top-level navigation, so this is the control).
"""

import base64
import hashlib
import re

import sqlglot

from . import dashboardhtml, dashboards

# Hard-coded on purpose: the backend image has no dashboards/ directory. Tests tie
# them to dashboards/vendor.json, dashboards_serve.LIB_CSP_HASHES and the contract.
LIB_SRC = "/d/_lib/chart.js-4.5.1/chart.umd.min.js"
LIB_INTEGRITY = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
LIB_LINE = f'<script src="{LIB_SRC}" integrity="{LIB_INTEGRITY}"></script>'

ERROR_RULES = ("size", "doctype", "queries", "library", "tags", "attrs", "external_url",
               "css", "js_dangerous", "location", "fetch", "query_ids",
               "global_alias", "computed_key", "split_literal")
WARNING_RULES = ("unused_query", "dynamic_ids", "refresh_rate", "dup_columns", "soft_size")
SOFT_SIZE_CHARS = 60_000

ALLOWED_TAGS = frozenset("""
html head body meta title style script header footer main section article aside nav div span p
h1 h2 h3 h4 h5 h6 strong em b i u small sub sup br hr ul ol li dl dt dd table caption thead tbody
tfoot tr th td colgroup col canvas img figure figcaption svg g path rect circle ellipse line
polyline polygon text tspan defs lineargradient radialgradient stop clippath use button label
select option optgroup input details summary a abbr time code pre blockquote mark progress meter
""".split())
# AGENT-07: the only elements allowed in the SVG namespace (chart shapes). Names compare lowercased.
SVG_TAGS = frozenset("""svg g path rect circle ellipse line polyline polygon text tspan defs
lineargradient radialgradient stop clippath use""".split())
INPUT_TYPES = frozenset({"checkbox", "radio", "range", "search", "text", "number", "date", "month", "week"})
META_NAMES = frozenset({"viewport", "description"})
FORBIDDEN_ATTRS = frozenset({"srcdoc", "srcset", "formaction", "action", "ping", "poster", "background"})

_CTRL = re.compile(r"[\x00-\x20\x7f]+")
_XMLNS = re.compile(r"""(?i)\bxmlns(?::[\w.-]+)?\s*=\s*(["'])\s*https?://www\.w3\.org/(?:2000/svg|1999/xlink|1999/xhtml)\1""")
_SCHEME = re.compile(r"(?i)\b(?:https?|wss?|ftp):")
# AGENT-07: browsers read a backslash as '/' in http(s) URLs and drop tabs and newlines, so
# '/\host', '\/host' and '\\host' are protocol-relative too (_SLASHES = two of / or \).
_SLASHES = r"[/\\][\t\n\r]*[/\\]"
_CSS_PROTO_REL = re.compile(r"""(?i)url\(\s*["']?\s*""" + _SLASHES)
# Attribute values below are already stripped of whitespace and control characters.
_PROTO_REL_START = re.compile(r"[/\\]{2}")
_PROTO_REL_INNER = re.compile(r"""(?i)url[(=]["']?[/\\]{2}""")
_W3C_NS = re.compile(r"https?://www\.w3\.org/(?:2000/svg|1999/xlink|1999/xhtml)")
_JS_PROTO_REL = re.compile(r"""["'`]//[A-Za-z0-9]""")
_CSS_URL = re.compile(r"""(?i)url\(\s*["']?\s*(data:)?""")
_COMMENT_OR_STRING = re.compile(
    r""""(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/""", re.S)


def _clean(v) -> str:
    return _CTRL.sub("", v or "")


def _finding(rule, line, message):
    return {"rule": rule, "line": line, "message": message}


def _lineno(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _blank(m) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def _strip_comments(js: str) -> str:
    """Replace // and /* */ comments with same-length blanks; strings are kept."""
    def sub(m):
        s = m.group(0)
        return _blank(m) if s.startswith("//") or s.startswith("/*") else s
    return _COMMENT_OR_STRING.sub(sub, js)


class _Page:
    """What the linter checks, read from the canonical tree (dashboardhtml.canonicalize).

    That tree is what a spec-conformant HTML5 parser builds from the bytes /d/ serves,
    so every element the browser will see is here, including markup inside svg/math
    integration points. Script and style text is verbatim; attribute values are decoded.
    """

    def __init__(self, canon):
        self.elements = canon.elements
        self.scripts: list[tuple[str, int]] = []  # inline script (text, line)
        self.styles: list[tuple[str, int]] = []
        self.script_srcs: list[tuple[list, int]] = []
        for el in canon.elements:
            name = el.name.lower()
            if el.ns != dashboardhtml.HTML_NS:
                continue
            if name == "script":
                if any(k == "src" for k, _ in el.attrs):
                    self.script_srcs.append((el.attrs, el.line))
                else:
                    self.scripts.append((el.text or "", el.line))
            elif name == "style":
                self.styles.append((el.text or "", el.line))


def _lint_queries(queries):
    """Returns (findings, declared_ids, valid_sql_by_id)."""
    out: list[dict] = []
    declared: set[str] = set()
    valid: dict[str, str] = {}
    if not isinstance(queries, list):
        return [_finding("queries", None, "queries must be a list of {id, sql, refresh_minutes}; "
                         "pass a JSON list")], declared, valid
    if len(queries) > dashboards.MAX_QUERIES:
        out.append(_finding("queries", None, f"at most {dashboards.MAX_QUERIES} queries per dashboard "
                            f"(got {len(queries)}); merge or drop some"))
    seen: set = set()
    for i, q in enumerate(queries):
        qid = q.get("id") if isinstance(q, dict) else None
        label = f"queries[{i}]" + (f" '{qid}'" if isinstance(qid, str) else "")
        if isinstance(qid, str) and dashboards.QUERY_ID_RE.fullmatch(qid):
            declared.add(qid)
        if isinstance(qid, str):
            if qid in seen:
                out.append(_finding("queries", None, f"{label}: duplicate query id; ids must be unique"))
                continue
            seen.add(qid)
        try:
            ok = dashboards.validate_queries([q])
        except dashboards.DashboardError as e:
            out.append(_finding("queries", None, f"{label}: {e}"))
            continue
        except Exception as e:  # defensive: lint never raises
            out.append(_finding("queries", None, f"{label}: invalid ({type(e).__name__})"))
            continue
        if ok and isinstance(ok[0].get("sql"), str):
            valid[ok[0]["id"]] = ok[0]["sql"]
    return out, declared, valid


def _lint_structure(text, p, add):
    stripped = text.lstrip("﻿ \t\r\n\f\v")
    if not stripped.lower().startswith("<!doctype html>"):
        add("doctype", 1, "file must start with <!doctype html>")

    libs = 0
    for attrs, line in p.script_srcs:
        names = [k for k, _ in attrs]
        d = dict(attrs)
        if len(names) == 2 and d.get("src") == LIB_SRC and d.get("integrity") == LIB_INTEGRITY:
            libs += 1
        else:
            add("library", line, "the only allowed script src is this exact line, copied verbatim "
                f"(no other attributes): {LIB_LINE}")
    if libs > 1:
        add("library", None, f"the chart library line may appear only once: {LIB_LINE}")

    try:
        dashboards.script_hashes(text)
    except dashboards.DashboardError as e:
        msg = str(e)
        if "base" in msg:
            add("tags", None, "<base> is not allowed; remove it")
        else:
            add("library", None, f"{msg}; use only the library line: {LIB_LINE}")

    foreign = 0
    for el in p.elements:
        tag, attrs, line = el.name.lower(), el.attrs, el.line
        names = {k for k, _ in attrs}
        d: dict = {}
        for k, v in attrs:
            d.setdefault(k, v)
        # AGENT-07: the namespace comes from the spec parser, not from counting tags. A refused
        # element still gets every attribute check below (an element can be refused for its
        # place and still carry a URL).
        allowed = False
        if el.ns != dashboardhtml.HTML_NS:
            if tag in RAW_TEXT_TAGS:
                if foreign < 5:
                    foreign += 1
                    add("foreign_raw_text", line,
                        f"<{tag}> inside <svg> or <math> is not allowed: browsers parse it as markup there "
                        "while the validator reads it as text. Put CSS in the one <style> in <head>, code in "
                        "the inline <script>, and use <text> or aria-label instead of an SVG <title>")
            elif el.ns != dashboardhtml.SVG_NS or tag not in SVG_TAGS:
                add("tags", line, f"<{tag}> is not allowed inside <svg> (and <math> is not allowed); "
                    f"use only {', '.join(sorted(SVG_TAGS))}")
        elif any(ns != dashboardhtml.HTML_NS for ns, _ in el.ancestors):
            add("tags", line, f"<{tag}> inside <svg> is not allowed; keep HTML outside the chart shapes")
        elif tag not in ALLOWED_TAGS:
            add("tags", line, f"<{tag}> is not allowed; use plain div/span/table/svg elements")
        else:
            allowed = True
        if tag == "meta" and "http-equiv" in names:
            add("tags", line, "<meta http-equiv> is forbidden (no refresh or policy overrides)")
        elif not allowed or el.ns != dashboardhtml.HTML_NS:
            pass
        elif tag == "meta":
            if names == {"charset"}:
                pass
            elif names == {"name", "content"} and _clean(d["name"]).lower() in META_NAMES:
                pass
            else:
                add("tags", line, "<meta> is allowed only as charset, or name=viewport/description with content")
        elif tag == "input":
            t = d.get("type")
            if t is not None and _clean(t).lower() not in INPUT_TYPES:
                add("tags", line, f"<input type={t!r}> is not allowed; use one of {sorted(INPUT_TYPES)}")

        for k, v in attrs:
            kl = k.lower()
            cv = _clean(v)
            low = cv.lower()
            if kl.startswith("on"):
                add("attrs", line, f"inline handler '{k}' is forbidden; use addEventListener in the script")
            elif kl in FORBIDDEN_ATTRS:
                add("attrs", line, f"attribute '{k}' is forbidden; remove it")
            elif kl in ("href", "xlink:href"):
                if not cv.startswith("#"):
                    add("attrs", line, f"{k} may only be a '#fragment'; remove links to other places")
            elif kl == "src" and tag != "script":
                if not low.startswith("data:"):
                    add("attrs", line, "src must be a data: URI; embed images as data: URIs")
            elif kl == "style":
                if "expression(" in low:
                    add("attrs", line, "style expression() is forbidden")
                for m in _CSS_URL.finditer(v):
                    if not m.group(1):
                        add("attrs", line, "style url() may only point at a data: URI")
                        break
            if _PROTO_REL_START.match(cv):
                add("external_url", line, f"attribute '{k}' starts with // or a backslash form "
                    "(protocol-relative URL); remove it")
            elif _PROTO_REL_INNER.search(cv) or (kl == "style" and _CSS_STR_REL.search(v)):
                add("external_url", line, f"attribute '{k}' contains url(//...), url=//... or a string "
                    "starting with // or a backslash form (protocol-relative URL); remove it")
            if not (kl in ("xmlns", "xmlns:xlink") and _W3C_NS.fullmatch(cv)):
                m = _SCHEME.search(cv)
                if m:
                    add("external_url", line, f"attribute '{k}' holds the external URL scheme "
                        f"'{m.group(0)}'; the page may reference no external host")


def _lint_external_and_css(p, add):
    """URL rules inside script and style text from the tree (raw-text rules: _lint_text)."""
    for js, start in p.scripts:
        sj = _strip_comments(js)
        for m in _JS_PROTO_REL.finditer(sj):
            add("external_url", start + sj.count("\n", 0, m.start()),
                "string starting with // looks like a protocol-relative URL; remove it")
        n = 0
        for m in _STR_LIT.finditer(sj):
            if _PROTO_REL_TEXT.match(_js_unescape(m.group(0)[1:-1])):
                add("external_url", start + sj.count("\n", 0, m.start()),
                    "a string starting with // or a backslash form (/\\, \\/, \\\\) is a "
                    "protocol-relative URL; remove it")
                n += 1
                if n >= 5:
                    break
    for css, start in p.styles:
        for m in re.finditer(r"(?i)@import\b|@font-face\b", css):
            add("css", start + css.count("\n", 0, m.start()),
                f"{m.group(0)} is forbidden; use the system font stack and inline CSS")
        for m in _CSS_URL.finditer(css):
            if not m.group(1):
                add("css", start + css.count("\n", 0, m.start()), "CSS url() may only point at a data: URI")
        for m in _CSS_STR_REL.finditer(css):
            add("external_url", start + css.count("\n", 0, m.start()),
                "a CSS string starting with // or a backslash form is a protocol-relative URL; remove it")


# --- Script rules (js_dangerous, location, fetch, query_ids, script warnings) ---
_DANGEROUS = [
    (r"(?<![\w$])eval\s*\(", "eval() is forbidden; parse data with JSON and plain code"),
    (r"\bnew\s+Function\b|(?<![\w$.])Function\s*\(", "Function constructor is forbidden"),
    (r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]", "setTimeout/setInterval with a string is forbidden; pass a function"),
    (r"\bdocument\s*\.\s*(?:write|writeln|cookie|domain)\b", "document.write/cookie/domain is forbidden"),
    (r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b",
     "innerHTML/outerHTML/insertAdjacentHTML are forbidden (DB text is untrusted); set textContent instead"),
    (r"\b(?:localStorage|sessionStorage|indexedDB|caches)\b", "browser storage APIs are forbidden; keep state in variables"),
    (r"\b(?:XMLHttpRequest|WebSocket|EventSource|SharedWorker|Worker|BroadcastChannel|RTCPeerConnection)\b",
     "network and worker APIs are forbidden; use only the ./data/<id> fetch"),
    (r"\b(?:sendBeacon|importScripts|postMessage)\b", "sendBeacon/importScripts/postMessage are forbidden"),
    (r"(?<![\w$.])import\s*\(|(?m:^\s*import\s*[\w{*\"'])", "dynamic and static import are forbidden; use one plain script"),
    (r"\b(?:window|self|globalThis|document)\s*\.\s*open\b|(?<![\w$.])open\s*\(", "window.open is forbidden"),
    (r"(?<![\w$.])(?:top|parent|opener|frames)\s*[.\[]|\b(?:window|self|globalThis|document)\s*\.\s*(?:top|parent|opener|frames)\b",
     "top/parent/opener/frames are forbidden (navigation and exfiltration)"),
    (r"\b(?:atob|btoa|unescape|fromCharCode|fromCodePoint|decodeURI|decodeURIComponent)\b|(?<![\w$.])escape\s*\(",
     "runtime string decoding/encoding helpers are forbidden; write literal strings"),
    (r"\bconstructor\b|__proto__|\bReflect\b", "constructor/__proto__/Reflect are forbidden"),
    (r"\b(?:window|self|globalThis|document|location|navigator)\s*\[", "computed access on globals is forbidden; use a dotted name"),
]
_DANGEROUS = [(re.compile(r), m) for r, m in _DANGEROUS]
_LOCATION = [re.compile(r"(?<![\w$.])location\b(?!\s*\.\s*search\b)"),
             re.compile(r"\b(?:window|document|self|globalThis)\s*\.\s*location\b(?!\s*\.\s*search\b)")]
# CR-01: navigation and exfiltration primitives that need no `location` token. Comments in
# inline scripts are refused outright so the comment stripper can never be desynchronised.
_JS_COMMENT = re.compile(r"//|/\*")
_NAV = [
    (re.compile(r"\bdefaultView\b"), "defaultView is forbidden (reaches window.location)"),
    (re.compile(r"(?<![\w$.])this\s*[.\[]"), "this.<member> is forbidden in dashboard scripts; use named variables"),
    (re.compile(r"\.\s*click\s*\("), ".click() is forbidden (programmatic navigation)"),
    (re.compile(r"\.\s*(?:href|src|action|formAction)\s*="), "assigning href/src/action is forbidden (navigation)"),
    (re.compile(r"\bsetAttribute(?:NS)?\s*\("), "setAttribute is forbidden; set textContent or style properties"),
    (re.compile(r"""\bcreateElement\s*\(\s*(?!['"`](?:div|span|p|tr|td|th|li|strong|em|small|table|tbody|thead)['"`]\s*\))"""),
     "createElement is allowed only with a literal plain tag (div, span, p, tr, td, th, li, table...)"),
    (re.compile(r"\bcreateElementNS\s*\("), "createElementNS is forbidden; write inline <svg> in the markup"),
    (re.compile(r"RTC\w*", re.I), "WebRTC is forbidden (it bypasses CSP)"),
    (re.compile(r"""\bnavigate\s*\(|\bassign\s*\(|\breplace\s*\(\s*['"`]"""), "navigation calls are forbidden"),
]
# Phase 6 gap closure (06-VERIFICATION gap 1): aliasing a global and reading it with a
# computed key bypassed the token blocklist. Payloads: w=window; w['loc'+'ation']['hr'+'ef'],
# D=document; D['loc'+'ation'], g=globalThis; g['loc'+'ation'], g['R'+'TCPeerConnection'].
_GLOBAL_ALIAS = re.compile(r"(?<![\w$.])(?:window|self|globalThis|document|frames)\b(?!\s*\.)")
_STR_LIT = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""", re.S)
_SPLIT_LIT = re.compile(r"""\s*\+\s*['"`]""")
_KEY_OK = re.compile(r"\s*(?:\d+|[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*")
_LOCATION_ANY = re.compile(r"\blocation\b")
_LOCATION_READ = re.compile(r"location\s*\.\s*search\b(?!\s*(?:=(?!=)|\+=))")
_FETCH = re.compile(r"\bfetch\s*\(")
_FETCH_OK = re.compile(r"""\s*(?:'\./data/|"\./data/|`\./data/)""")
_TIMER = re.compile(r"\bset(?:Interval|Timeout)\s*\(")
_DATA_LIT = re.compile(r"""\./data/([A-Za-z0-9_]*)(?=["'`?#/]|$)""")
_FN_DECL = re.compile(
    r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b[^(]*\(|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_LIT_ARG = re.compile(r"""\s*(['"`])([^'"`\\\n]*)\1""")


# ── Phase 12 (AGENT-07): bypass-class rules (06-VERIFICATION gap 1); the sandboxed frame is the real control ──
ERROR_RULES = ERROR_RULES + ("self_ref", "dom_backref", "dispatch", "computed_write", "string_build",
                             "location_parts", "webrtc")
_SELF_REF = re.compile(
    r"(?<![\w$])(?:window|self|globalThis|document)\s*\.\s*(?:window|self|globalThis|document)(?![\w$])"
    r"|\.\s*(?:frames|top|parent)(?![\w$])")
_DOM_BACKREF = re.compile(
    r"\b(?:ownerDocument|defaultView|getRootNode|parentNode|composedPath|contentWindow|contentDocument)\b"
    r"|\.\s*view(?![\w$])")
_DISPATCH = re.compile(r"\bdispatchEvent\b")
_ASSIGN_AFTER = re.compile(r"\s*(?:\*\*|<<|>>>?|&&|\|\||\?\?|[-+*/%&|^])?=(?![=>])")
_INT_KEY = re.compile(r"\s*\d+\s*")
_KW_BEFORE_BRACKET = frozenset(
    "const let var return of in typeof case new else do void delete yield await throw instanceof".split())
_TEMPLATE_EXPR = re.compile(r"\$\{")
_CONCAT = re.compile(r"\.\s*concat\s*\(")
_ARRAY_JOIN = re.compile(r"""\[[^\[\]]*['"`][^\[\]]*\]\s*\.\s*join\s*\(""")
_LOC_PART_SET = re.compile(
    r"\.\s*(?:host|hostname|protocol|port|pathname|origin|hash)\s*"
    r"(?:\*\*|<<|>>>?|&&|\|\||\?\?|[-+*/%&|^])?=(?![=>])")
_WEBRTC = re.compile(r"PeerConnection|DataChannel|IceCandidate|(?i:[\"'`]\s*(?:stuns?|turns?):)")


def _lint_phase12(s, ln, add):
    """Phase 12 rules over a comment-stripped script; each capped at 5 findings."""
    def scan(rx, rule, msg):
        for n, m in enumerate(rx.finditer(s)):
            if n >= 5:
                break
            add(rule, ln(m.start()), f"{m.group(0).strip()!r}: {msg}")

    scan(_SELF_REF, "self_ref",
         "a global may only be used as a dotted member access (document.getElementById(...)); "
         "no window.window, self.self, .top, .parent, .frames")
    scan(_DOM_BACKREF, "dom_backref",
         "do not walk from an element back to the document or window; keep references from "
         "document.getElementById")
    scan(_DISPATCH, "dispatch", "dispatchEvent is forbidden; call your own function")
    scan(_CONCAT, "string_build",
         "template literals with ${...}, .concat( and string-array .join( are forbidden; join a "
         "variable and a whole literal with +")
    scan(_ARRAY_JOIN, "string_build",
         "template literals with ${...}, .concat( and string-array .join( are forbidden; join a "
         "variable and a whole literal with +")
    n = 0
    for m in _STR_LIT.finditer(s):
        if m.group(0)[0] == "`" and _TEMPLATE_EXPR.search(m.group(0)):
            add("string_build", ln(m.start()),
                "template literals with ${...} are forbidden; join a variable and a whole literal with +")
            n += 1
            if n >= 5:
                break
    scan(_LOC_PART_SET, "location_parts", "assigning location parts navigates; never write to them")
    scan(_WEBRTC, "webrtc", "WebRTC is forbidden (it bypasses CSP)")
    n = 0
    for i, ch in enumerate(s):
        if ch != "[":
            continue
        j = i - 1
        while j >= 0 and s[j] in " 	":
            j -= 1
        if j < 0 or not (s[j].isalnum() or s[j] in "_$)]'\"`"):
            continue
        k = j
        while k >= 0 and (s[k].isalnum() or s[k] in "_$"):
            k -= 1
        if s[k + 1:j + 1] in _KW_BEFORE_BRACKET:
            continue
        close = _match_close(s, i, 400)
        if close >= len(s) or s[close] != "]":
            continue
        if _INT_KEY.fullmatch(s[i + 1:close]):
            continue
        if _ASSIGN_AFTER.match(s, close + 1):
            add("computed_write", ln(i),
                "computed writes are forbidden; use a Map (m.set(k, v)), push, or build a new array with map")
            n += 1
            if n >= 5:
                break


def _match_close(s: str, open_idx: int, cap: int | None = None) -> int:
    """Index of the paren/brace matching s[open_idx], or the cap/end when unbalanced."""
    o = s[open_idx]
    c = {"(": ")", "{": "}", "[": "]"}[o]
    depth = 0
    end = len(s) if cap is None else min(len(s), open_idx + cap)
    for i in range(open_idx, end):
        ch = s[i]
        if ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return i
    return end


def _fn_body(s: str, m) -> tuple[int, int]:
    """(start, end) of the function body for a _FN_DECL match."""
    i = m.end()
    if m.group(1) or "function" in m.group(0):
        paren = s.find("(", m.start() if m.group(1) else m.end() - 1)
        i = _match_close(s, paren) + 1 if paren >= 0 else m.end()
    brace = re.compile(r"\s*(?:=>\s*)?\{").match(s, i)
    if brace:
        b = brace.end() - 1
        return b, _match_close(s, b) + 1
    nl = re.compile(r"[;\n]").search(s, i)
    return i, nl.start() if nl else len(s)


def _lint_scripts(p, declared, valid_sql, add, add_warn) -> list[str]:
    ids_used: list[str] = []
    referenced: dict[str, int] = {}  # id -> first line
    dynamic = False
    dyn_lines: list[int] = []
    for js, start in p.scripts:
        s = _strip_comments(js)

        def ln(off, s=s, start=start):
            return start + s.count("\n", 0, off)

        for rx, msg in _DANGEROUS:
            for n, m in enumerate(rx.finditer(s)):
                if n >= 5:
                    break
                add("js_dangerous", ln(m.start()), f"{m.group(0).strip()!r}: {msg}")
        for m in _JS_COMMENT.finditer(js):
            add("js_dangerous", start + js.count("\n", 0, m.start()),
                "JS comments (// and /* */) are not allowed in inline scripts; remove them")
            break
        for m in _SCHEME.finditer(js):
            add("external_url", ln(m.start()), f"external URL scheme '{m.group(0)}' is forbidden in scripts")
        for rx, msg in _NAV:
            for m in rx.finditer(s):
                add("js_dangerous", ln(m.start()), f"{m.group(0).strip()!r}: {msg}")
        for n, m in enumerate(_GLOBAL_ALIAS.finditer(s)):
            if n >= 5:
                break
            add("global_alias", ln(m.start()),
                f"{m.group(0)!r}: window/self/globalThis/document/frames may only be used as a dotted "
                "member access (document.getElementById(...)); do not assign, pass or index the object itself")
        n = 0
        for m in _STR_LIT.finditer(s):
            if _SPLIT_LIT.match(s, m.end()):
                add("split_literal", ln(m.start()),
                    "two string literals joined with + are forbidden (URLs and API names must never be "
                    "built at runtime); write the whole string")
                n += 1
                if n >= 5:
                    break
        n = 0
        for i, ch in enumerate(s):
            if ch != "[":
                continue
            j = i - 1
            while j >= 0 and s[j] in " \t":
                j -= 1
            if j < 0 or not (s[j].isalnum() or s[j] in "_$)]'\"`"):
                continue
            close = _match_close(s, i, 200)
            if close >= len(s) or s[close] != "]" or not _KEY_OK.fullmatch(s[i + 1:close]):
                add("computed_key", ln(i),
                    "computed member access must use an integer literal or a plain dotted name "
                    "(rows[i], datasets[0].data); build no key from strings")
                n += 1
                if n >= 5:
                    break
        _lint_phase12(s, ln, add)
        for m in _LOCATION_ANY.finditer(s):
            prev = s[max(0, m.start() - 1):m.start()]
            if prev in (".", "$") or not _LOCATION_READ.match(s, m.start()):
                add("location", ln(m.start()),
                    "location may only be read as location.search; remove other uses of location")
        for m in _FETCH.finditer(s):
            close = _match_close(s, m.end() - 1, 300)
            args = s[m.end():close]
            if not _FETCH_OK.match(args):
                add("fetch", ln(m.start()), "fetch only './data/<id>' + location.search; no other fetch target")
            elif "location.search" not in args:
                add("fetch", ln(m.start()), "fetch('./data/<id>') must append + location.search to forward the view token")
        for m in _TIMER.finditer(s):
            close = _match_close(s, m.end() - 1, 2000)
            args = s[m.end():close]
            last, depth = 0, 0
            for i, ch in enumerate(args):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                elif ch == "," and depth == 0:
                    last = i + 1
            tail = args[last:].strip().replace("_", "")
            if last and re.fullmatch(r"\d+(?:\.\d+)?", tail) and float(tail) < 60000:
                add_warn("refresh_rate", ln(m.start()), "timer faster than 60000 ms; re-fetch no faster than every 60 s")

        # query ids: (a) literal ./data/<id>, (b) literal first args of data helpers
        bodies = []
        for m in _FN_DECL.finditer(s):
            name = m.group(1) or m.group(2)
            b0, b1 = _fn_body(s, m)
            if "./data/" in s[b0:b1]:
                bodies.append((name, b0, b1))
        spans = [(b0, b1) for _, b0, b1 in bodies]
        for m in _DATA_LIT.finditer(s):
            inside = any(b0 <= m.start() < b1 for b0, b1 in spans)
            if m.group(1):
                referenced.setdefault(m.group(1), ln(m.start()))
            elif not inside:
                dynamic = True
                dyn_lines.append(ln(m.start()))
        for name, b0, b1 in bodies:
            body_dyn = any(not m.group(1) for m in _DATA_LIT.finditer(s, b0, b1))
            lit = 0
            nonlit = 0
            for m in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", s):
                if re.search(r"function\s+$", s[max(0, m.start() - 20):m.start()]):
                    continue
                if b0 <= m.start() < b1:
                    continue
                a = _LIT_ARG.match(s, m.end())
                if a:
                    lit += 1
                    referenced.setdefault(a.group(2), ln(m.start()))
                else:
                    nonlit += 1
            if body_dyn and (lit == 0 or nonlit):
                dynamic = True
                dyn_lines.append(ln(b0))

    for qid, line in sorted(referenced.items()):
        ids_used.append(qid)
        if qid not in declared:
            add("query_ids", line, f"undeclared query id '{qid}'; declare it in queries or fix the id")
    if dynamic:
        add_warn("dynamic_ids", min(dyn_lines) if dyn_lines else None,
                 "a ./data/ id is built at runtime; call the data helper with literal ids so every id "
                 "can be checked against the declared queries")
    else:
        for qid in sorted(declared - set(referenced)):
            add_warn("unused_query", None, f"query '{qid}' is declared but never fetched; remove it or use it")
    return ids_used


def _dup_columns(valid_sql, add_warn):
    for qid, sql in valid_sql.items():
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
        except Exception:
            continue
        exprs = tree.expressions if tree is not None and tree.key == "select" else None
        if not exprs:
            continue
        names = [e.alias_or_name for e in exprs if e.alias_or_name and e.alias_or_name != "*"]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:
            add_warn("dup_columns", None, f"query '{qid}' returns duplicate column names {dups}; "
                     "alias every column uniquely or rows lose data")


# Phase 12 review CR-01: html.parser and browsers disagree on where a comment ends
# (e.g. "<!-->"), so markup can hide from the tag allowlist yet stay live. Refuse them.
ERROR_RULES = ERROR_RULES + ("html_comment",)
_HTML_COMMENT = re.compile(r"<!--|<!(?!doctype[\s>])|<\?", re.I)


def _lint_comments(text, add):
    m = _HTML_COMMENT.search(text)
    if m:
        add("html_comment", text[:m.start()].count(chr(10)) + 1,
            "HTML comments and markup declarations are not allowed in dashboard code "
            "(browsers and the validator disagree on where a comment ends); remove them")


# ── Phase 12 gap closure, AGENT-07 hidden-markup fix ──
# The linter no longer guesses nesting from raw text. The page is parsed by a spec-conformant
# HTML5 parser (dashboardhtml), every rule above reads that tree, and /d/ serves the canonical
# serialisation of the same tree, so the browser parses exactly what was checked. The text rules
# here do not depend on any parser; they run on the stored text and again on the served bytes.
ERROR_RULES = ERROR_RULES + ("foreign_raw_text", "resource_hint", "style_markup",
                             "http_equiv", "parser_differential")
RAW_TEXT_TAGS = frozenset({"script", "style", "title", "textarea", "xmp", "noscript", "noembed",
                           "noframes", "plaintext", "iframe"})
_LINK_TEXT = re.compile(r"<link", re.I)
_HTTP_EQUIV = re.compile(r"http-equiv", re.I)
_HINT_REL = re.compile(r"(?<![\w-])(dns-prefetch|preconnect|prefetch|prerender|modulepreload)(?![\w-])", re.I)
_STYLE_MARKUP = re.compile(r"<[A-Za-z/!?]")
# value = '//x', or a quoted value starting with any of '//', '/\', '\/', '\\', or an unquoted
# one starting with a backslash form (an unquoted '/\' is left to the tree check: in a script
# it is a regex literal such as /\d+/).
_ATTR_PROTO_REL = re.compile(r"""=\s*["']?\s*//[^\s/"'>]|=\s*["']\s*""" + _SLASHES
                             + r"""|=\s*\\[\t\n\r]*[/\\]""")
_CSS_STR_REL = re.compile(r"""["']\s*""" + _SLASHES)
_PROTO_REL_TEXT = re.compile(r"[\x00-\x20]*" + _SLASHES)
_JS_ESC = re.compile(r"\\(?:x([0-9A-Fa-f]{2})|u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|\r\n|(.))", re.S)
_JS_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _line_of(text, pos):
    return text.count(chr(10), 0, pos) + 1


def _js_unescape(body: str) -> str:
    """The value of a JS string literal body (escapes decoded, line continuations dropped)."""
    def sub(m):
        h = m.group(1) or m.group(2) or m.group(3)
        if h:
            try:
                return chr(int(h, 16))
            except (ValueError, OverflowError):
                return ""
        c = m.group(4)
        if c is None or c in "\n\r\u2028\u2029":
            return ""
        return _JS_SIMPLE.get(c, c)
    return _JS_ESC.sub(sub, body)


def _script_hash(js: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode()


def _lint_text(text, add):
    """Parser-independent rules over raw text (the stored file, then the served bytes)."""
    m = _HTTP_EQUIV.search(text)
    if m:
        add("http_equiv", _line_of(text, m.start()),
            "the text 'http-equiv' is not allowed anywhere in a dashboard; dashboards never need "
            "<meta http-equiv> (no refresh or policy overrides)")
    m = _LINK_TEXT.search(text)
    if m:
        add("resource_hint", _line_of(text, m.start()),
            "the text '<link' is not allowed anywhere in a dashboard; dashboards never need <link>")
    for i, m in enumerate(_HINT_REL.finditer(text)):
        if i >= 5:
            break
        add("resource_hint", _line_of(text, m.start()),
            f"resource hint '{m.group(1).lower()}' is not allowed anywhere in a dashboard: browsers "
            "open these connections without checking CSP or the sandbox")
    blanked = _XMLNS.sub(_blank, text)
    for m in _SCHEME.finditer(blanked):
        add("external_url", _lineno(text, m.start()),
            f"external URL scheme '{m.group(0)}' is forbidden; the page may reference no external host "
            "(only W3C xmlns values on svg are exempt)")
    for m in _CSS_PROTO_REL.finditer(text):
        add("external_url", _lineno(text, m.start()),
            "CSS url(//...) or a backslash form is a protocol-relative URL; use a data: URI")
    for i, m in enumerate(_ATTR_PROTO_REL.finditer(text)):
        if i >= 5:
            break
        add("external_url", _line_of(text, m.start()),
            "a value starting with // or a backslash form (/\\, \\/, \\\\) is a protocol-relative URL; "
            "remove it")


def _lint_canonical(text, canon, p, add):
    """The served bytes must be a stable canonical form whose scripts are the hashed ones."""
    if not canon.stable:
        add("parser_differential", None,
            "the page reads differently when an HTML5 parser re-reads it, so a browser could see "
            "markup the validator did not; close every tag you open, quote attribute values and keep "
            "<svg> to chart shapes")
    if not canon.html.startswith("<!DOCTYPE html>"):
        add("doctype", 1, "file must start with <!doctype html>")
    hashed = dashboards.inline_script_hashes(text)
    for js, line in p.scripts:
        if _script_hash(js) not in hashed:
            add("parser_differential", line,
                "browsers read this inline script differently from the save-time hasher, so it would "
                "be blocked; keep the script plain (no '<!--', '<script' or '</script' inside it)")
    for css, start in p.styles:
        for i, m in enumerate(_STYLE_MARKUP.finditer(css)):
            if i >= 5:
                break
            add("style_markup", start, "markup inside <style> is not allowed; CSS never contains tags")
# ── End AGENT-07 block ──


def lint(code, queries) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []

    def add(rule, line, msg):
        f = _finding(rule, line, msg)
        if f not in errors:
            errors.append(f)

    def add_new(rule, line, msg):
        # Second pass over the served bytes: report only what the stored text did not show.
        if not any(f["rule"] == rule and f["message"] == msg for f in errors):
            add(rule, line, msg)

    def add_warn(rule, line, msg):
        f = _finding(rule, line, msg)
        if f not in warnings:
            warnings.append(f)

    qfind, declared, valid_sql = _lint_queries(queries)
    for f in qfind:
        add(f["rule"], f["line"], f["message"])

    try:
        text = dashboards.normalize_code(code)
    except dashboards.DashboardError as e:
        raw = len(code.encode("utf-8", "replace")) if isinstance(code, str) else 0
        add("size", None, f"{e}; keep the page non-empty and under {dashboards.MAX_CODE_BYTES} "
            "bytes of UTF-8 with no NUL bytes")
        return {"errors": errors, "warnings": warnings, "ids_used": [], "size_bytes": raw}

    size = len(text.encode("utf-8"))
    try:
        canon = dashboardhtml.canonicalize(text)
    except Exception as e:  # defensive: html5lib should not raise
        add("tags", None, f"page could not be parsed ({type(e).__name__}); simplify the markup")
        return {"errors": errors, "warnings": warnings, "ids_used": [], "size_bytes": size}
    p = _Page(canon)

    _lint_comments(text, add)
    _lint_text(text, add)
    _lint_comments(canon.html, add_new)
    _lint_text(canon.html, add_new)
    _lint_canonical(text, canon, p, add)
    _lint_structure(text, p, add)
    _lint_external_and_css(p, add)
    ids_used = _lint_scripts(p, declared, valid_sql, add, add_warn)
    _dup_columns(valid_sql, add_warn)
    if len(text) > SOFT_SIZE_CHARS:
        add_warn("soft_size", None, f"page is over {SOFT_SIZE_CHARS} characters; "
                 "keep dashboards small so they can be revised")

    def key(f):
        return (f["line"] or 0, f["rule"])

    errors.sort(key=key)
    warnings.sort(key=key)
    return {"errors": errors, "warnings": warnings, "ids_used": sorted(set(ids_used)), "size_bytes": size}
