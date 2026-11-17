"""Phase 18 (SPEC-02): static scan of the vendored renderer, pins and frame stylesheet."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app import dashboardrender, dashboards_serve

APP_ROOT = Path(__file__).resolve().parents[2]
VENDOR_JSON = APP_ROOT / "dashboards" / "vendor.json"
RENDER = APP_ROOT / "dashboards" / "vendor" / "ic-render-1.0.0" / "render.js"
PIN_SCRIPT = APP_ROOT / "backend" / "scripts" / "pin_renderer.py"

ALLOWED_TAGS = {"main", "header", "section", "div", "h1", "h2", "p", "span",
                "canvas", "table", "thead", "tbody", "tr", "th", "td"}


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}\s])//[^'\"\n]*$", "", src)


def _code() -> str:
    return _strip_comments(RENDER.read_text(encoding="utf-8"))


def _libs():
    return json.loads(VENDOR_JSON.read_text(encoding="utf-8"))["libraries"]


def _renderer_entry():
    return next(x for x in _libs() if x["name"] == "ic-render" and x["version"] == "1.0.0")


def test_render_js_is_ascii_lf():
    b = RENDER.read_bytes()
    assert b.isascii()
    assert b"\r" not in b


FORBIDDEN = [
    r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"setAttribute",
    r"cssText", r"\beval\b", r"\bFunction\s*\(", r"\.src\s*=[^=]", r"\.href\s*=[^=]",
    r"window\.open", r"postMessage", r"XMLHttpRequest", r"WebSocket", r"EventSource",
    r"sendBeacon", r"\bimport\s*\(", r"localStorage", r"sessionStorage", r"indexedDB",
    r"document\.cookie", r"https?:", r"""['"]//""", r"\.style\b", r"\.location\b",
    r"\bnavigator\b", r"importScripts", r"\bWorker\b",
    r"""setTimeout\(\s*['"]""", r"""setInterval\(\s*['"]""",
]


def test_static_scan_refuses_sinks_and_foreign_io():
    code = _code()
    hits = [p for p in FORBIDDEN if re.search(p, code)]
    assert not hits, hits


def test_location_only_search():
    code = _code()
    for m in re.finditer(r"\blocation\b(\.\w+)?", code):
        assert m.group(1) == ".search", m.group(0)


def test_single_fetch_with_fixed_targets():
    code = _code()
    assert len(re.findall(r"\bfetch\s*\(", code)) == 1
    assert "fetch(rel + location.search," in code
    calls = re.findall(r"getJSON\(([^)]*)\)", code)
    assert sorted(set(calls)) == sorted({"rel", "'./spec'", "'./data/' + q"})


def test_create_element_only_allowed_tags():
    code = _code()
    ce = re.findall(r"createElement\(([^)]*)\)", code)
    assert ce == ["tag"]  # the one helper, which checks the allow-list
    assert "ALLOWED[tag] !== 1" in code
    for tag in re.findall(r"\bel\(\s*'([a-z0-9]+)'", code):
        assert tag in ALLOWED_TAGS, tag


def test_text_only_writes():
    code = _code()
    assert "textContent" in code
    assert not re.search(r"\.(innerText|innerHTML|outerHTML)\b", code)


def test_renderer_pin_matches_vendor_json_and_bytes():
    data = RENDER.read_bytes()
    entry = _renderer_entry()
    f = entry["files"][0]
    assert entry["source"] == "in-repo" and "tarball" not in entry and "integrity" not in entry
    assert entry["served_at"] == "/d/_lib/ic-render-1.0.0/render.js"
    assert f["bytes"] == len(data)
    assert f["sha256"] == hashlib.sha256(data).hexdigest()
    assert f["sri"] == "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    csp = "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()
    assert f["csp_hash"] == csp
    p = dashboardrender.pin(dashboardrender.CURRENT_RENDERER)
    assert dashboardrender.CURRENT_RENDERER == "ic-render-1.0.0"
    assert p["src"] == entry["served_at"]
    assert p["csp_hash"] == csp
    assert dashboardrender.pin("nope") is None


def test_libraries_order_and_chart_hash():
    libs = _libs()
    assert [x["name"] for x in libs][:2] == ["chart.js", "ic-render"]  # Phase 22 appends more
    chart_hash = libs[0]["files"][0]["csp_hash"]
    assert dashboardrender.LIB_CSP_HASH == chart_hash
    assert chart_hash in dashboards_serve.LIB_CSP_HASHES


def test_spec_css_rules():
    css = dashboardrender.SPEC_CSS
    assert css.isascii()
    for bad in ("url(", "@import", "expression(", "<"):
        assert bad not in css
    code = RENDER.read_text(encoding="utf-8")
    classes = set()
    for m in re.finditer(r"'([^']*\bic-[a-z-]+[^']*)'", code):
        classes.update(re.findall(r"\bic-[a-z-]+", m.group(1)))
    assert len(classes) >= 25
    for c in sorted(classes):
        assert ("." + c in css) or ("#" + c in css), c
    p = dashboardrender.pin(dashboardrender.CURRENT_RENDERER)
    assert p["css_hash"] == "sha256-" + base64.b64encode(
        hashlib.sha256(css.encode("utf-8")).digest()).decode()


def _run_check(root: Path):
    return subprocess.run([sys.executable, str(PIN_SCRIPT), "--check", "--root", str(root)],
                          capture_output=True, text=True, timeout=60)


def test_pin_script_check_on_real_tree_and_tampered_copy(tmp_path):
    assert _run_check(APP_ROOT).returncode == 0
    (tmp_path / "dashboards" / "vendor").mkdir(parents=True)
    (tmp_path / "backend" / "app").mkdir(parents=True)
    shutil.copy(VENDOR_JSON, tmp_path / "dashboards" / "vendor.json")
    shutil.copy(APP_ROOT / "backend" / "app" / "dashboardrender.py",
                tmp_path / "backend" / "app" / "dashboardrender.py")
    d = tmp_path / "dashboards" / "vendor" / "ic-render-1.0.0"
    d.mkdir()
    shutil.copy(RENDER, d / "render.js")
    assert _run_check(tmp_path).returncode == 0
    (d / "render.js").write_bytes(RENDER.read_bytes() + b" ")
    proc = _run_check(tmp_path)
    assert proc.returncode == 1
    assert "differs" in proc.stderr
