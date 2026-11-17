"""Dashboard file contract doc lint (GRND-01).

docs/DASHBOARD_CONTRACT.md is a cross-phase spec: storage (Phase 3), serving
(Phase 4), the authoring skill (Phase 5) and the validators (Phase 6) all code
against it. This test pins its invariants, keeps it to one page, ties its
library line (path and integrity hash) to dashboards/vendor.json, and proves
its own example obeys it. The library path and hash are never hard-coded here.
"""

import json
import re
from pathlib import Path

from app.guardrails import validate_readonly_sql

APP_ROOT = Path(__file__).resolve().parents[2]
DOC = APP_ROOT / "docs" / "DASHBOARD_CONTRACT.md"
MANIFEST = APP_ROOT / "dashboards" / "vendor.json"

MAX_LINES = 300  # Phase 22 owns the cap (sections 12 and 13)
MAX_BYTES = 262144
QID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _doc() -> str:
    assert DOC.is_file(), "docs/DASHBOARD_CONTRACT.md missing"
    return DOC.read_text(encoding="utf-8")


def _lib() -> tuple[str, str]:
    assert MANIFEST.is_file(), "dashboards/vendor.json missing"
    lib = json.loads(MANIFEST.read_text(encoding="utf-8"))["libraries"][0]
    entry = next(f for f in lib["files"] if f["path"] == lib["entry"])
    return lib["served_at"], entry["csp_hash"]


def _canonical_line() -> str:
    served_at, csp_hash = _lib()
    return f'<script src="{served_at}" integrity="{csp_hash}"></script>'


def _fenced(lang: str) -> str:
    m = re.search(r"```" + lang + r"[ \t]*\r?\n(.*?)```", _doc(), re.S)
    assert m, f"no fenced {lang} block in the contract"
    return m.group(1)


def test_contract_exists_and_fits_one_page():
    assert len(_doc().splitlines()) <= MAX_LINES


def test_contract_states_locked_invariants():
    text = _doc()
    required = [
        "./data/", "location.search", "?t=", "8091", "dash.", "256 KB",
        "app:8080", "404", "frame-src 'self'",
        "Referrer-Policy: no-referrer", "query_id", "refreshed_at",
        "refresh_minutes", "sql_row_limit", "[a-z][a-z0-9_]{0,47}",
        "integrity=", "verbatim", "^[a-z0-9][a-z0-9-]{0,62}$", "t=REDACTED",
        "frame-ancestors 'self'; sandbox allow-scripts", 'crossorigin="anonymous"',
    ]
    for needle in required:
        assert needle in text, f"contract is missing {needle!r}"
    line = next(ln for ln in text.splitlines() if ln.startswith("- Content header"))
    assert "allow-same-origin" not in line
    assert "262,144" in text or "262144" in text
    assert "no external urls" in text.lower()


def test_contract_lib_line_matches_vendor_manifest():
    text = _doc()
    served_at, csp_hash = _lib()
    assert text.count(_canonical_line()) >= 2
    assert csp_hash in text.split("frame-ancestors")[0].split("## 6")[-1], \
        "csp_hash missing from the CSP header text"
    for path in re.findall(r"/d/_lib/[^\s\"'`<>]*\.js", text):
        assert path == served_at
    for value in re.findall(r'integrity="([^"]*)"', text):
        assert value == csp_hash


def test_example_html_obeys_contract():
    html = _fenced("html")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert len(html.encode("utf-8")) <= MAX_BYTES
    tags = re.findall(r"<script\b[^>]*\bsrc\s*=[^>]*>.*?</script>", html, re.S | re.I)
    assert len(re.findall(r"<script\b[^>]*\bsrc\s*=", html, re.I)) == 1
    assert tags == [_canonical_line()]
    assert not re.search(r"(?i)\b(?:https?|wss?|ftp):", html)
    assert not re.search(r"""(?i)\b(?:src|href)\s*=\s*["']?//""", html)
    assert not re.search(r"(?i)\son[a-z]+\s*=", html)
    for bad in ["<link", "@import", "<iframe", "<base", "localStorage",
                "sessionStorage", "document.cookie", "eval(", "new Function",
                "location.href", "location.assign", "location.replace", "window.open",
                ".opener", "document.write", "atob(", "fromCharCode"]:
        assert bad not in html, f"example contains {bad!r}"
    assert "./data/" in html and "location.search" in html


def _queries() -> list:
    return json.loads(_fenced("json"))


def test_example_queries_are_valid_declarations():
    qs = _queries()
    assert isinstance(qs, list) and 0 < len(qs) <= 20
    ids = []
    for q in qs:
        assert isinstance(q, dict) and set(q) == {"id", "sql", "refresh_minutes"}
        assert QID_RE.match(q["id"])
        ids.append(q["id"])
        n = q["refresh_minutes"]
        assert isinstance(n, int) and not isinstance(n, bool) and 5 <= n <= 1440
        validate_readonly_sql(q["sql"])
    assert len(ids) == len(set(ids))


def test_example_html_reads_only_declared_queries():
    html = _fenced("html")
    for q in _queries():
        assert re.search(r"""['"]""" + re.escape(q["id"]) + r"""['"]""", html)


def test_p12_contract_records_hint_and_dns_limit():
    sec = _doc().split("## 6.")[1].split("## 7.")[0]
    low = sec.lower()
    assert "DNS" in sec and "`<link`" in sec and "<svg>" in sec and "resource hint" in low


# ---------------------------------------------------------------- Phase 18: chart spec section

def _section10() -> str:
    return _doc().split("## 10. Chart spec (Phase 18)")[1].split("\n## ")[0]


def test_section_10_documents_the_spec():
    from app import dashboardspec as ds
    sec = _section10()
    assert "docs/dashboard-spec.v1.schema.json" in sec
    assert len(sec.splitlines()) <= 40
    for t in ds.TILE_TYPES:
        assert f"`{t}`" in sec, t
    for f in ds.FORMATS:
        assert f"`{f}`" in sec, f
    titles = re.findall(r"^## (\d+)\. ", _doc(), re.M)
    assert titles[:11] == [str(i) for i in range(1, 12)]


def test_spec_example_validates():
    from app import dashboardspec as ds
    m = re.search(r"## Spec example\s*```json[ \t]*\r?\n(.*?)```", _doc(), re.S)
    assert m, "no Spec example json block"
    spec = ds.parse(m.group(1))
    assert {t.type for t in spec.tiles} >= {"stat", "line", "bar", "table", "text"}


def test_first_json_block_is_still_the_legacy_queries_example():
    assert isinstance(_queries(), list)


def test_section_11_documents_renderer_and_serving():
    doc = _doc()
    sec = doc.split("## 11. Renderer and serving (Phase 18)")[1].split("\n## ")[0]
    for need in ("require-trusted-types-for", "trusted-types 'none'", "./spec",
                 "ic-render-1.0.0", "no-store"):
        assert need in sec, need
    assert len(sec.splitlines()) <= 25
    sec5 = doc.split("## 5. URLs and errors")[1].split("\n## ")[0]
    assert "./spec" in sec5



def test_p18_section6_closes_t_12_51_for_specs():
    sec = _doc().split("## 6.")[1].split("## 7.")[0]
    assert "closed" in sec and "dashboard_legacy_html" in sec and "spec_hostile_check" in sec


def test_p17_section3_documents_data_as_of():
    doc = _doc()
    sec = doc.split("## 3. Data")[1].split("\n## ")[0]
    assert '"data_as_of": ISO-8601 or null' in sec and "confirmation cadence" in sec
    sec11 = doc.split("## 11. Renderer and serving (Phase 18)")[1].split("\n## ")[0]
    assert "wired by Phase 17" in sec11
    assert len(doc.splitlines()) <= MAX_LINES


# ---------------------------------------------------------------- Phase 22: sections 12 and 13

def _sec(title: str) -> str:
    return _doc().split(title)[1].split("\n## ")[0]


def _csp_hash(name: str, version: str) -> str:
    libs = json.loads(MANIFEST.read_text(encoding="utf-8"))["libraries"]
    lib = next(x for x in libs if x["name"] == name and x["version"] == version)
    return next(f for f in lib["files"] if f["path"] == lib["entry"])["csp_hash"]


def test_section_12_names_tile_types_and_caps():
    from app import dashboardspec as ds
    sec = _sec("## 12. Chart spec v2 (Phase 22)")
    assert "docs/dashboard-spec.v2.schema.json" in sec
    for t in ds.TILE_TYPES_V2:
        assert f"`{t}`" in sec, t
    for p in ds.PRESETS:
        assert f"`{p}`" in sec, p
    for w in ds.WIDTHS:
        assert f"`{w}`" in sec, w
    for tone in ds.TONES:
        assert f"`{tone}`" in sec, tone
    assert f"{ds.MAX_TILES_V2} tiles" in sec and f"{ds.MAX_CHARTS_V2} charts" in sec
    assert f"{ds.MAX_QUERIES_V2} queries" in sec
    assert f"{ds.MAX_SPEC_BYTES_V2 // 1024} KB" in sec and str(ds.MAX_SPEC_BYTES_V2) in sec
    assert f"up to {ds.MAX_SECTIONS} sections" in sec and f"of up to {ds.MAX_SECTION_TILES} tile ids" in sec
    assert f"up to {ds.MAX_FILTERS} page filters" in sec
    for loc in ds.LOCALES:
        assert f"`{loc}`" in sec, loc


def test_section_12_measures_sentence_names_every_bindable_type():
    from app import dashboardspec as ds
    sec = _sec("## 12. Chart spec v2 (Phase 22)")
    line = next(ln for ln in sec.splitlines() if "`measures` (several" in ln)
    for t in ds._MEASURES_TYPES:
        assert f"`{t}`" in line, t
    assert "`fy_start_month`" in sec


def test_section_12_example_parses_as_v2():
    from app import dashboardspec as ds
    sec = _sec("## 12. Chart spec v2 (Phase 22)")
    fence = "`" * 3
    body = sec.split(fence + "json", 1)[1].split(fence, 1)[0]
    spec = ds.parse(body)
    assert isinstance(spec, ds.SpecV2) and spec.spec_version == 2
    assert {"stat", "paired_bar", "table"} <= {t.type for t in spec.tiles}


def test_section_13_names_pins_hashes_and_messages():
    sec = _sec("## 13. Renderer 2.0.0 and the wrapper channel (Phase 22)")
    for name, version in (("ic-render", "2.0.0"), ("ic-chartlib", "1.0.0")):
        assert f"{name}-{version}" in sec, name
        assert _csp_hash(name, version) in sec, name
    for need in ("{ic: 1, type: \"state\"", "{ic: 1, type: \"ask\"", "app_public_url",
                 "SET LOCAL TimeZone", "dashboard_snapshot_rows", "dashboard_snapshot_bytes",
                 "/api/dashboards/{slug}/layout", "dashboard_layout"):
        assert need in sec, need


def test_section_6_records_the_one_sanctioned_postmessage():
    sec = _doc().split("## 6.")[1].split("## 7.")[0]
    assert "one sanctioned `postMessage`" in sec and "section 13" in sec
