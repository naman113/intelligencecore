"""Phase 22 plan 05: the pinned chart library bundle (Chart.js + annotation + matrix)."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
DASH = APP_ROOT / "dashboards"
SCRIPT = APP_ROOT / "backend" / "scripts" / "build_chartlib.py"
BDIR = DASH / "vendor" / "ic-chartlib-1.0.0"


def _entry():
    libs = json.loads((DASH / "vendor.json").read_text("utf-8"))["libraries"]
    return next(x for x in libs if x["name"] == "ic-chartlib")


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_check_passes_on_committed_tree():
    p = _run("--check")
    assert p.returncode == 0, p.stderr


def test_check_fails_after_one_byte_change(tmp_path):
    root = tmp_path / "app"
    (root / "dashboards").mkdir(parents=True)
    shutil.copy(DASH / "vendor.json", root / "dashboards" / "vendor.json")
    shutil.copytree(BDIR, root / "dashboards" / "vendor" / "ic-chartlib-1.0.0")
    assert _run("--check", "--root", str(root)).returncode == 0
    f = root / "dashboards" / "vendor" / "ic-chartlib-1.0.0" / "chartlib.min.js"
    b = bytearray(f.read_bytes())
    b[100] ^= 1
    f.write_bytes(bytes(b))
    assert _run("--check", "--root", str(root)).returncode == 1


def test_bundle_is_verbatim_concatenation():
    data = (BDIR / "chartlib.min.js").read_bytes()
    chart = (DASH / "vendor" / "chart.js-4.5.1" / "chart.umd.min.js").read_bytes()
    assert data.startswith(chart + b"\n")
    e = _entry()
    assert [c["name"] for c in e["components"]] == [
        "chart.js", "chartjs-plugin-annotation", "chartjs-chart-matrix"]
    assert [c["version"] for c in e["components"]] == ["4.5.1", "3.1.0", "3.1.0"]
    assert e["components"][0]["sha256"] == hashlib.sha256(chart).hexdigest()
    assert e["global"] == "Chart" and e["source"] == "bundle"


def test_bundle_has_no_dom_sinks():
    text = (BDIR / "chartlib.min.js").read_text("utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "DOMParser",
                "document.write", "eval(", "new Function"):
        assert bad not in text, bad



def test_dashboardrender_constants_match_manifest():
    from app import dashboardrender as r
    f = _entry()["files"][0]
    assert r.CHARTLIB_CSP_HASH == f["csp_hash"]
    assert r.CHARTLIB_INTEGRITY == f["csp_hash"]
    assert r.CHARTLIB_SRC == _entry()["served_at"]
