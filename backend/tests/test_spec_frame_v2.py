"""Phase 22 plan 05: v1 frame, CSP and wrapper stay byte-identical (golden fixture from 5ae19a6)."""
import json
from pathlib import Path

from app import dashboards_serve as ds

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "v1_frame_golden.json").read_text("utf-8"))


def test_v1_frame_csp_and_wrapper_match_golden():
    assert ds.spec_frame_html("ic-render-1.0.0") == GOLDEN["frame"]
    assert ds.build_spec_csp("ic-render-1.0.0", "dash.example.com:8091") == GOLDEN["csp"]
    assert ds.build_spec_csp("ic-render-1.0.0", None) == GOLDEN["csp_nohost"]
    assert ds.WRAPPER_HTML == GOLDEN["wrapper"]
    assert ds.WRAPPER_CSP == GOLDEN["wrapper_csp"]


import json as _json
import re

from app import dashboardrender as dr

_VENDOR = Path(__file__).resolve().parents[2] / "dashboards" / "vendor.json"


def _lib(name):
    return next(x for x in _json.loads(_VENDOR.read_text("utf-8"))["libraries"] if x["name"] == name)


def test_renderer_for():
    assert dr.renderer_for(1) == "ic-render-1.0.0"
    assert dr.renderer_for(2) == "ic-render-2.0.0"
    assert dr.renderer_for(3) is None
    assert dr.CURRENT_RENDERER == "ic-render-1.0.0"


def test_pin_libraries_equal_vendor_json():
    chart = _lib("chart.js")["files"][0]["csp_hash"]
    bundle = _lib("ic-chartlib")
    assert dr.LIB_CSP_HASH == chart
    for name, lib_src, lib_hash in (
            ("ic-render-1.0.0", "/d/_lib/chart.js-4.5.1/chart.umd.min.js", chart),
            ("ic-render-2.0.0", bundle["served_at"], bundle["files"][0]["csp_hash"])):
        p = dr.pin(name)
        assert p["lib_src"] == lib_src
        assert p["lib_integrity"] == p["lib_csp_hash"] == lib_hash
    assert dr.CHARTLIB_SRC == bundle["served_at"]
    assert dr.CHARTLIB_CSP_HASH == dr.CHARTLIB_INTEGRITY == bundle["files"][0]["csp_hash"]
    assert ds.LIB_CSP_HASHES == {chart, dr.CHARTLIB_CSP_HASH}


def test_v2_frame_has_only_prelude_bundle_and_renderer_scripts():
    p = dr.pin("ic-render-2.0.0")
    html = ds.spec_frame_html("ic-render-2.0.0")
    tags = re.findall(r"<script\b[^>]*>", html)
    assert tags == [
        "<script>",
        f'<script src="{p["lib_src"]}" integrity="{p["lib_integrity"]}" crossorigin="anonymous">',
        f'<script src="{p["src"]}" integrity="{p["csp_hash"]}" crossorigin="anonymous">']
    assert "<style>" + p["css"] + "</style>" in html


def test_v2_csp_shape():
    p = dr.pin("ic-render-2.0.0")
    csp = ds.build_spec_csp("ic-render-2.0.0", "dash.example.com:8091")
    assert (f"script-src '{ds.PRELUDE_HASH}' '{p['lib_csp_hash']}' '{p['csp_hash']}'; "
            f"style-src '{p['css_hash']}'; ") in csp
    for part in ("require-trusted-types-for 'script'", "trusted-types 'none'", "sandbox allow-scripts",
                 "default-src 'none'", "http://dash.example.com:8091"):
        assert part in csp
    # same shape as v1: only the three hash tokens differ
    v1 = ds.build_spec_csp("ic-render-1.0.0", "dash.example.com:8091")
    p1 = dr.pin("ic-render-1.0.0")
    swapped = (csp.replace(p["lib_csp_hash"], p1["lib_csp_hash"]).replace(p["csp_hash"], p1["csp_hash"])
               .replace(p["css_hash"], p1["css_hash"]))
    assert swapped == v1


def test_legacy_csp_never_admits_the_bundle():
    bundle_hash = dr.CHARTLIB_CSP_HASH
    csp = ds.build_csp("<!doctype html><html><body></body></html>", [bundle_hash, dr.LIB_CSP_HASH])
    assert bundle_hash not in csp
    assert dr.LIB_CSP_HASH in csp
    assert ds.LEGACY_LIB_CSP_HASHES == frozenset({dr.LIB_CSP_HASH})
