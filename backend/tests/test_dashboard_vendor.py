"""Vendored dashboard chart library: integrity, manifest, no-CDN, fetch script.

The chart library is committed, integrity-pinned content that must be present.
Unlike skills_library, these checks FAIL rather than skip when it is missing.
The fetch script checks run without network."""

import ast
import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
DASH = APP_ROOT / "dashboards"
MANIFEST = DASH / "vendor.json"
SCRIPT = APP_ROOT / "backend" / "scripts" / "fetch_chart_lib.py"

PINNED_CHART_SHA256 = "48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a"
PINNED_CHART_BYTES = 208522
PINNED_LICENSE_SHA256 = "41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce"
PINNED_INTEGRITY = ("sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5"
                    "+E8/Pw==")
PINNED_TARBALL = "https://registry.npmjs.org/chart.js/-/chart.js-4.5.1.tgz"

CDN_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com",
             "cdn.skypack.dev", "esm.sh", "ajax.googleapis.com",
             "code.jquery.com", "fonts.googleapis.com", "fonts.gstatic.com",
             "stackpath.bootstrapcdn.com", "cdn.plot.ly")
SCAN_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx",
                 ".json", ".conf", ".md"}


def _manifest():
    assert MANIFEST.is_file(), "dashboards/vendor.json missing"
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _libs():
    libs = _manifest()["libraries"]
    assert libs, "no libraries in manifest"
    return libs


def _chart():
    libs = [x for x in _libs() if x["name"] == "chart.js" and x["version"] == "4.5.1"]
    assert len(libs) == 1
    return libs[0]


def _file(lib, path):
    return next(f for f in lib["files"] if f["path"] == path)


def test_manifest_lists_chart_js_4_5_1():
    lib = _chart()
    assert lib["license"] == "MIT"
    assert lib["global"] == "Chart"
    assert lib["tarball"] == PINNED_TARBALL
    assert lib["integrity"] == PINNED_INTEGRITY


def test_chart_js_stays_first_and_renderer_is_appended():
    names = [x["name"] for x in _libs()]
    assert names[0] == "chart.js"
    assert "ic-render" in names
    assert "ic-chartlib" in names
    assert names.index("ic-render") < names.index("ic-chartlib")


def test_every_library_has_a_license_file():
    for lib in _libs():
        paths = {f["path"] for f in lib["files"]}
        if lib["name"] == "chart.js":
            assert "LICENSE.md" in paths
        elif lib["name"] == "ic-chartlib":
            assert {p for p in paths if p.startswith("LICENSE-")} == {
                "LICENSE-chart.js.md", "LICENSE-chartjs-plugin-annotation.md",
                "LICENSE-chartjs-chart-matrix.txt"}


def test_third_party_tarballs_are_integrity_pinned():
    for lib in _libs():
        if lib["name"] == "chart.js":
            assert lib["license"] == "MIT" and lib["integrity"].startswith("sha512-")
        if lib["name"] == "ic-chartlib":
            assert lib["license"] == "MIT"
            for c in lib["components"]:
                assert c["integrity"].startswith("sha512-")
                assert c["tarball"].startswith("https://registry.npmjs.org/")


def test_every_vendor_dir_is_pinned_in_manifest():
    listed = {lib["dir"] for lib in _libs()}
    on_disk = {f"vendor/{p.name}" for p in (DASH / "vendor").iterdir() if p.is_dir()}
    assert on_disk == listed


def test_dir_is_name_dash_version():
    for lib in _libs():
        assert lib["dir"] == f"vendor/{lib['name']}-{lib['version']}"
        assert (DASH / lib["dir"]).is_dir()


def test_served_at_is_versioned():
    for lib in _libs():
        assert lib["served_at"] == f"/d/_lib/{lib['name']}-{lib['version']}/{lib['entry']}"
        assert lib["entry"] in {f["path"] for f in lib["files"]}


def test_vendored_bytes_match_manifest():
    for lib in _libs():
        for f in lib["files"]:
            p = DASH / lib["dir"] / f["path"]
            assert p.is_file(), f"{p} missing"
            data = p.read_bytes()
            assert len(data) == f["bytes"], p
            assert hashlib.sha256(data).hexdigest() == f["sha256"], p


def test_vendored_bytes_match_pinned_constants():
    lib = _chart()
    d = DASH / lib["dir"]
    chart = (d / "chart.umd.min.js").read_bytes()
    assert len(chart) == PINNED_CHART_BYTES
    assert hashlib.sha256(chart).hexdigest() == PINNED_CHART_SHA256
    lic = (d / "LICENSE.md").read_bytes()
    assert hashlib.sha256(lic).hexdigest() == PINNED_LICENSE_SHA256


def test_sri_and_csp_hash_are_derived_from_bytes():
    lib = _chart()
    entry = _file(lib, lib["entry"])
    data = (DASH / lib["dir"] / lib["entry"]).read_bytes()
    assert entry["sri"] == "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    assert entry["csp_hash"] == "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_license_is_mit():
    lib = _chart()
    text = (DASH / lib["dir"] / "LICENSE.md").read_text(encoding="utf-8")
    assert "MIT License" in text


def test_no_sourcemap_or_extra_files_vendored():
    for lib in _libs():
        on_disk = {p.name for p in (DASH / lib["dir"]).iterdir()}
        assert on_disk == {f["path"] for f in lib["files"]}


def _scan_roots():
    yield DASH, {"vendor"}
    yield APP_ROOT / "frontend", {"node_modules", "dist"}
    yield APP_ROOT / "backend" / "app", None  # *.html only, non-recursive


def test_no_cdn_hosts_in_product_code():
    hits = []
    for root, skip in _scan_roots():
        if not root.is_dir():
            continue
        if skip is None:
            files = list(root.glob("*.html"))
        else:
            files = [p for p in root.rglob("*")
                     if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES
                     and not (set(p.relative_to(root).parts) & skip)]
        for p in files:
            text = p.read_text(encoding="utf-8", errors="ignore")
            for host in CDN_HOSTS:
                if host in text:
                    hits.append(f"{p}: {host}")
    assert not hits, "CDN reference(s) found: " + "; ".join(hits)


def test_fetch_script_parses():
    assert SCRIPT.is_file(), "backend/scripts/fetch_chart_lib.py missing"
    ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))


def test_fetch_script_is_stdlib_only():
    assert SCRIPT.is_file()
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [(node.module or "").split(".")[0]]
        for m in mods:
            assert m in stdlib, f"non-stdlib import {m!r}"
            assert m != "app"


def test_fetch_script_help_runs_without_network():
    assert SCRIPT.is_file()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--tarball" in proc.stdout and "--dry-run" in proc.stdout


def test_fetch_script_pins_match_manifest():
    assert SCRIPT.is_file()
    spec = importlib.util.spec_from_file_location("fetch_chart_lib", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    lib = _chart()
    assert mod.VERSION == lib["version"]
    assert mod.INTEGRITY == lib["integrity"]
