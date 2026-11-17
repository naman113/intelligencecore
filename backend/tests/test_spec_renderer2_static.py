"""Phase 22 plan 05 (DCAP-01, DCAP-02, DCAP-16): C-11 frame contract scan of render.js 2.0.0,
its pin, and the v2 frame stylesheet."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app import dashboardrender

APP_ROOT = Path(__file__).resolve().parents[2]
VENDOR_JSON = APP_ROOT / "dashboards" / "vendor.json"
RENDER = APP_ROOT / "dashboards" / "vendor" / "ic-render-2.0.0" / "render.js"
PIN_SCRIPT = APP_ROOT / "backend" / "scripts" / "pin_renderer.py"
NAME = "ic-render-2.0.0"

ALLOWED_TAGS = {"main", "header", "section", "div", "h1", "h2", "h3", "p", "span", "canvas", "table",
                "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "button", "select", "option",
                "input", "label"}
ALLOWED_DATASET = {"icTile", "icValue", "icCol", "icRow", "icSeries"}

# UI-SPEC section 14 (render.js uses only these classes).
VOCABULARY = """ic-head ic-title ic-sub ic-asof ic-bar ic-range ic-sheet ic-preset ic-on ic-date ic-apply
ic-close ic-chip ic-chip-x ic-filter ic-section ic-section-title ic-section-desc ic-section-toggle ic-grid
ic-w-quarter ic-w-third ic-w-half ic-w-two_thirds ic-w-full ic-tile ic-tile-head ic-tile-title ic-tile-asof
ic-now ic-fixed ic-info ic-def ic-note ic-stat ic-stat-value ic-stat-period ic-delta ic-good ic-bad ic-flat
ic-owed ic-owed-badge ic-spark ic-chart ic-chart-tall ic-bullet ic-table-wrap ic-table ic-num ic-sort
ic-total ic-pager ic-prev ic-next ic-cellbar ic-basis ic-text ic-empty ic-error ic-stale ic-na ic-fail
ic-sr ic-legend-note""".split()

TOKENS = ["page", "surface", "border", "grid", "ink", "ink-2", "muted", "good", "bad", "owed",
          "inward", "outward", "neutral", "accent"]


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}\s])//[^'\"\n]*$", "", src)


def _code() -> str:
    return _strip_comments(RENDER.read_text(encoding="utf-8"))


def _libs():
    return json.loads(VENDOR_JSON.read_text(encoding="utf-8"))["libraries"]


def _entry():
    return next(x for x in _libs() if x["name"] == "ic-render" and x["version"] == "2.0.0")


def test_render_js_is_ascii_lf():
    b = RENDER.read_bytes()
    assert b.isascii() and b"\r" not in b


FORBIDDEN = [
    r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"setAttribute",
    r"cssText", r"\beval\b", r"\bFunction\s*\(", r"\.src\s*=[^=]", r"\.href\s*=[^=]",
    r"window\.open", r"\bwindow\.top\b", r"\btop\.(location|document|frames|postMessage|opener|window|self|parent)\b",
    r"\bopener\b", r"window\.parent\.location", r"parent\.location",
    r"""addEventListener\(\s*['"]message['"]""", r"XMLHttpRequest", r"WebSocket", r"EventSource",
    r"sendBeacon", r"\bimport\s*\(", r"localStorage", r"sessionStorage", r"indexedDB",
    r"document\.cookie", r"https?:", r"""['"]//""", r"\bnavigator\b", r"importScripts", r"\bWorker\b",
    r"""setTimeout\(\s*['"]""", r"""setInterval\(\s*['"]""",
    # the window also reaches the code as root, self or globalThis
    r"\b(root|self|globalThis)\s*\.\s*(open|parent|top|opener|location|fetch)\b",
    r"\b(root|self|globalThis)\s*\[",
]


def test_static_scan_refuses_sinks_and_foreign_io():
    code = _code()
    hits = [p for p in FORBIDDEN if re.search(p, code)]
    assert not hits, hits


def test_location_only_search_or_hash():
    for m in re.finditer(r"\blocation\b(\.\w+)?", _code()):
        assert m.group(1) in (".search", ".hash"), m.group(0)


def test_at_most_one_fetch_with_fixed_targets():
    code = _code()
    assert len(re.findall(r"\bfetch\s*\(", code)) <= 1
    for m in re.finditer(r"getJSON\(([^)]*)\)", code):
        assert m.group(1) in ("rel", "'./spec'", "'./data/' + qid", "'./data/' + q"), m.group(1)


def test_exactly_one_post_message_call_site_with_star_target():
    code = _code()
    assert len(re.findall(r"\bparent\.postMessage\(", code)) == 1
    assert len(re.findall(r"postMessage", code)) == 1
    assert re.search(r"\bparent\.postMessage\(\s*msg\s*,\s*'\*'\s*\)", code)


def test_frame_never_listens_for_messages():
    code = _code()
    assert not re.search(r"""addEventListener\(\s*['"]message['"]""", code)
    assert not re.search(r"\bonmessage\b", code)


def test_created_tags_only_from_contract_list():
    code = _code()
    for tag in re.findall(r"createElement\(\s*'([a-z0-9]+)'\s*\)", code):
        assert tag in ALLOWED_TAGS, tag
    for arg in re.findall(r"createElement\(([^)]*)\)", code):
        assert arg == "tag" or re.fullmatch(r"'[a-z0-9]+'", arg.strip()), arg
    for tag in re.findall(r"\bel\(\s*'([a-z0-9]+)'", code):
        assert tag in ALLOWED_TAGS, tag


def test_style_writes_are_single_properties():
    code = _code()
    assert not re.search(r"\.style\s*=[^=]", code)
    assert not re.search(r"\.style\.cssText|\.style\s*\[", code)
    for m in re.finditer(r"\.style\b(?!\.[A-Za-z]+\s*=[^=])", code):
        raise AssertionError("style used other than .style.<prop> = : " + code[m.start():m.start() + 40])


def test_dataset_writes_only_contract_keys():
    code = _code()
    for m in re.finditer(r"\.dataset\b([^;\n]{0,40})", code):
        key = re.match(r"\.(\w+)\s*=[^=]", m.group(1))
        assert key and key.group(1) in ALLOWED_DATASET, m.group(0)
    assert not re.search(r"\.dataset\s*\[", code)


def test_text_only_writes():
    assert not re.search(r"\.(innerText|innerHTML|outerHTML)\b", _code())


def test_scanner_catches_a_violation():
    # The scan helpers refuse a synthetic bad source (guards against a vacuous scan).
    bad = _strip_comments("el.innerHTML = x; fetch('https://e.example'); parent.postMessage(1, '*');")
    assert [p for p in FORBIDDEN if re.search(p, bad)]


def test_renderer_pin_matches_vendor_json_and_bytes():
    data = RENDER.read_bytes()
    entry = _entry()
    f = entry["files"][0]
    assert entry["source"] == "in-repo" and "tarball" not in entry
    assert entry["served_at"] == "/d/_lib/ic-render-2.0.0/render.js"
    assert f["bytes"] == len(data)
    assert f["sha256"] == hashlib.sha256(data).hexdigest()
    assert f["sri"] == "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    csp = "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()
    assert f["csp_hash"] == csp == dashboardrender.CSP_HASH_2_0_0
    assert dashboardrender.pin(NAME)["src"] == entry["served_at"]
    assert dashboardrender.pin(NAME)["csp_hash"] == csp


def test_v1_pin_untouched():
    names = [(x["name"], x["version"]) for x in _libs()]
    assert names.index(("ic-render", "1.0.0")) < names.index(("ic-render", "2.0.0"))
    v1 = next(x for x in _libs() if x["name"] == "ic-render" and x["version"] == "1.0.0")
    assert v1["files"][0]["csp_hash"] == dashboardrender.CSP_HASH_1_0_0 == (
        "sha256-YbNWwj2W+vwtDj5/p3/uXyBtiFNdhV7e/4KAOlFf+dA=")


def test_spec_css_2_rules():
    css = dashboardrender.SPEC_CSS_2_0_0
    assert css.isascii()
    for bad in ("url(", "@import", "expression(", "<"):
        assert bad not in css
    assert dashboardrender.pin(NAME)["css"] == css
    assert dashboardrender.pin(NAME)["css_hash"] == "sha256-" + base64.b64encode(
        hashlib.sha256(css.encode("utf-8")).digest()).decode()
    assert dashboardrender.SPEC_CSS == dashboardrender.SPEC_CSS_1_0_0  # v1 stylesheet unchanged


def test_css_defines_every_token_in_dark_light_and_print():
    css = dashboardrender.SPEC_CSS_2_0_0
    dark_end = css.index("}", css.index(":root{"))
    light = css[css.index("@media (prefers-color-scheme: light)"):]
    light = light[:light.index("*{box-sizing")]
    printed = css[css.index("@media print"):]
    for part, name in ((css[:dark_end], "dark"), (light, "light"), (printed, "print")):
        for t in TOKENS:
            assert f"--ic-{t}:" in part, (name, t)


def test_css_grid_rules():
    css = dashboardrender.SPEC_CSS_2_0_0
    assert "@media (min-width:1024px){.ic-grid{grid-template-columns:repeat(12,minmax(0,1fr))}" in css
    assert "@media (min-width:640px){.ic-grid{grid-template-columns:repeat(6,minmax(0,1fr))}" in css
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in css
    wide = css[css.index("@media (min-width:1024px)"):]
    for w, n in (("quarter", 3), ("third", 4), ("half", 6), ("two_thirds", 8), ("full", 12)):
        assert f".ic-w-{w}{{grid-column:span {n}}}" in wide
    mid = css[css.index("@media (min-width:640px)"):css.index("@media (min-width:768px)")]
    for w, n in (("quarter", 3), ("third", 3), ("half", 3), ("two_thirds", 6), ("full", 6)):
        assert f".ic-w-{w}{{grid-column:span {n}}}" in mid


def test_css_covers_class_vocabulary_and_print():
    css = dashboardrender.SPEC_CSS_2_0_0
    for c in VOCABULARY:
        assert re.search(r"\." + re.escape(c) + r"(?![\w-])", css), c
    assert "break-inside:avoid" in css
    code = _code()
    used = set()
    for m in re.finditer(r"'([^']*\bic-[a-z_-]+[^']*)'", code):
        used.update(re.findall(r"\bic-[a-z_-]+", m.group(1)))
    # ic-collapsed and ic-section-head are style hooks of the stylesheet; ic-root and ic-first-screen
    # are the root element id and the performance mark name, not classes.
    extra = {"ic-collapsed", "ic-section-head", "ic-root", "ic-first-screen"}
    assert used <= set(VOCABULARY) | extra, used - set(VOCABULARY)


def _run_check(root: Path, version: str):
    return subprocess.run([sys.executable, str(PIN_SCRIPT), "--check", "--version", version,
                           "--root", str(root)], capture_output=True, text=True, timeout=60)


def test_pin_script_checks_2_0_0_and_leaves_1_0_0(tmp_path):
    assert _run_check(APP_ROOT, "2.0.0").returncode == 0
    assert _run_check(APP_ROOT, "1.0.0").returncode == 0
    (tmp_path / "dashboards" / "vendor").mkdir(parents=True)
    (tmp_path / "backend" / "app").mkdir(parents=True)
    shutil.copy(VENDOR_JSON, tmp_path / "dashboards" / "vendor.json")
    shutil.copy(APP_ROOT / "backend" / "app" / "dashboardrender.py",
                tmp_path / "backend" / "app" / "dashboardrender.py")
    d = tmp_path / "dashboards" / "vendor" / "ic-render-2.0.0"
    d.mkdir()
    shutil.copy(RENDER, d / "render.js")
    assert _run_check(tmp_path, "2.0.0").returncode == 0
    (d / "render.js").write_bytes(RENDER.read_bytes() + b" ")
    proc = _run_check(tmp_path, "2.0.0")
    assert proc.returncode == 1 and "differs" in proc.stderr
