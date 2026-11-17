"""Offline checks of scripts/spec_hostile_check.mjs (Phase 18, SPEC-07).

The live run (browser, stack) is plan 18-09; here: syntax, oracle markers, loopback guard,
redaction on every print path, and that the hostile spec is valid and complete.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import dashboardspec

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spec_hostile_check.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

TEXT_KEYS = {"title", "subtitle", "note", "text", "unit", "label", "compare_label",
             "x_label", "y_label"}
SINK = "127.0.0.1:1"


def _spec(nonce="testnonce1"):
    r = subprocess.run([NODE, str(SCRIPT), "--print-spec", "--nonce", nonce],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in TEXT_KEYS and isinstance(v, str):
                yield path + "." + k, v
            if k != "queries":
                yield from _walk(v, path + "." + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_node_check_passes():
    r = subprocess.run([NODE, "--check", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_hostile_spec_validates_and_covers_every_tile_type():
    spec = _spec()
    parsed = dashboardspec.parse(spec)
    assert {t.type for t in parsed.tiles} >= set(dashboardspec.TILE_TYPES)
    assert all(t.get("query") for t in spec["tiles"] if t["type"] != "text")


def test_every_free_text_field_is_hostile():
    spec = _spec()
    fields = list(_walk(spec))
    assert len(fields) >= 40
    for path, value in fields:
        assert "<" in value, path
        assert "testnonce1.ic-hostile.invalid" in value or SINK in value, path
    paths = " ".join(p for p, _ in fields)
    for k in ("subtitle", "compare_label", "x_label", "y_label", "unit", "text", "note"):
        assert "." + k in paths, k


def test_data_values_are_hostile():
    sql = _spec()["queries"][0]["sql"]
    assert sql.count("testnonce1.ic-hostile.invalid") >= 3 and "<" in sql


def test_oracles_and_guards_present_in_source():
    src = SCRIPT.read_text(encoding="utf-8")
    for marker in ("Debugger.getScriptSource", "Audits.issueAdded", "Network.requestWillBeSent",
                   "--log-net-log", "Browser.close", "Target.getTargets", "ic-hostile.invalid",
                   "is not loopback", "IncludeSensitive"):
        assert marker in src, marker


def test_every_print_path_is_redacted():
    src = SCRIPT.read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if "console." in ln]
    assert len(lines) == 2
    assert all("redact(" in ln for ln in lines)
    assert "process.stdout.write" in src and src.count("process.stdout.write") == 1


def test_refuses_non_loopback():
    env = dict(os.environ, BASE_URL="http://10.0.0.1:9281", OPS_KEY="x")
    r = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2 and "not loopback" in r.stderr


def test_live_oracle_fixes_from_18_09():
    """Live run on p28: the renderer's own band-base dataset is 'low', and the run's slug embeds
    the nonce, so the NetLog oracle must discount the on-origin slug before counting."""
    src = SCRIPT.read_text(encoding="utf8")
    assert "'low'" in src
    assert "split(slugLc).join('')" in src


# ---- Phase 22 (DCAP-01): the v2 leg ---------------------------------------------------------------

V2_TYPES = {"stat", "line", "area", "bar", "stacked_bar", "sparkline", "text", "table", "combo",
            "paired_bar", "aging", "bullet", "waterfall", "calendar", "donut", "ranked_bar", "funnel"}


def _v2(nonce="testnonce1"):
    r = subprocess.run([NODE, str(SCRIPT), "--spec-version", "2", "--print-spec", "--nonce", nonce],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_v2_specs_validate_and_cover_all_17_types():
    out = _v2()
    assert set(out["covered_types"]) == V2_TYPES and len(V2_TYPES) == 17
    assert len(out["parts"]) == 2
    covered = set()
    for part in out["parts"]:
        parsed = dashboardspec.parse(part["spec"])
        assert parsed.spec_version == 2
        covered |= {t.type for t in parsed.tiles}
    assert covered == V2_TYPES


def test_v2_every_free_text_field_is_hostile_and_listed():
    out = _v2()
    paths = out["field_paths"]
    joined = " ".join(paths)
    for key in ("subtitle", "sections[].title", "sections[].description", "filters[].label",
                "tiles[stat].title", "tiles[stat].note", "tiles[stat].compare_label",
                "tiles[line].y[].label", "tiles[line].band.label", "tiles[line].target.label",
                "tiles[line].x_label", "tiles[line].y_label", "tiles[table].columns[].label",
                "tiles[table].columns[].unit", "tiles[text].text", "tiles[bullet].target.label"):
        assert key in joined, key
    for tp in V2_TYPES:
        assert f"tiles[{tp}].title" in paths or tp == "text" or f"tiles[{tp}]" in joined, tp
    for part in out["parts"]:
        spec = part["spec"]
        for path, value in _walk(spec):
            assert "<" in value, path
            assert "testnonce1.ic-hostile.invalid" in value or SINK in value, path
    assert len(paths) >= 60
    assert "tiles[].target.label" not in out["not_text_fields"]
    assert "tiles[].column" in out["not_text_fields"]


def test_v2_data_values_are_hostile():
    for part in _v2()["parts"]:
        sql = part["spec"]["queries"][0]["sql"]
        assert sql.count("testnonce1.ic-hostile.invalid") >= 3 and "<" in sql


def test_v2_chart_cap_respected():
    for part in _v2()["parts"]:
        charts = [t for t in part["spec"]["tiles"] if t["type"] in dashboardspec.CHART_TYPES_V2]
        assert len(charts) <= dashboardspec.MAX_CHARTS_V2


def test_v2_default_is_v1_unchanged():
    spec = _spec()
    assert spec["spec_version"] == 1 and "layout" in spec


def test_v2_oracles_in_source():
    src = SCRIPT.read_text(encoding="utf-8")
    for marker in ("8 wrapper", "ic-chartlib-1", "ic-render-2", "button.ic-range",
                   "button.ic-preset", "button.ic-info", "data-ask-base", "A-Za-z0-9_=,.:;%~-]{0,600}",
                   "setDeviceMetricsOverride", "--spec-version"):
        assert marker in src, marker
    assert src.count("process.stdout.write") == 1


def test_v2_refuses_bad_version():
    r = subprocess.run([NODE, str(SCRIPT), "--spec-version", "3", "--print-spec"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2


def test_v2_text_tile_title_is_not_expected_in_the_dom():
    """Live run on p32 (22-16): the v2 renderer draws a text tile's text and note, never its title, so the
    title must not be in the strings oracle 6 requires in the DOM; the nonce-attribute scan allows only the
    text-only aria-label and data-ic-row, and funnel labels may carry the renderer's share suffix."""
    parts = _v2()["parts"]
    for p in parts:
        shown = set()

        def collect(node, skip_title=False):
            if isinstance(node, str):
                shown.update(node.split(chr(10)))
            elif isinstance(node, dict):
                for k, v in node.items():
                    if k == "queries" or (skip_title and k == "title"):
                        continue
                    collect(v)
            elif isinstance(node, list):
                for v in node:
                    collect(v)

        for t in p["spec"]["tiles"]:
            collect(t, skip_title=t["type"] == "text")
        collect({k: v for k, v in p["spec"].items() if k != "tiles"})
        # every string the oracle requires in the DOM comes from a field the renderer draws
        for s in p["expect"]["dom"]:
            assert s in shown, s
    src = SCRIPT.read_text(encoding="utf-8")
    # the exemption is gated on the leg: v1 legs keep the strict nonce-free attribute scan
    assert "const v2 = ${ctx.v2 ? 'true' : 'false'};" in src
    assert "!(v2 && (at.name === 'aria-label' || at.name === 'data-ic-row'))" in src
    assert "at.name !== 'aria-label'" not in src
    assert "%( of first)?" in src
