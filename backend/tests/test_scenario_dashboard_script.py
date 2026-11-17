"""Static and pure-function tests for scripts/scenario_dashboard.py (DEPLOY-03)."""

import ast
import importlib.util
import re
import sys
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scenario_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("scenario_dashboard_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load()
LIB = sd.LIB_HASH


def P(s: str) -> str:
    return (f"default-src 'none'; script-src {s}; style-src 'unsafe-inline'; img-src data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            "sandbox allow-scripts allow-same-origin")


def H(s: str) -> dict:
    return {"Content-Security-Policy": P(s)}


def test_imports_are_stdlib_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)


def test_no_developer_path():
    src = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", src)
    assert "Intelligence corner" not in src
    assert "C:/Users" not in src


def test_stack_config():
    c = sd.stack_config("p10")
    assert c["ui"] == "http://127.0.0.1:9100"
    assert c["base"] == "http://127.0.0.1:9101"
    assert c["dash"] == "http://127.0.0.1:9102"
    assert c["app"] == "ic-p10-app-1"
    assert c["pg"] == "ic-p10-postgres-1"
    assert c["dashc"] == "ic-p10-dashboards-1"
    i = sd.stack_config("int")
    assert (i["ui"], i["base"], i["dash"]) == (
        "http://127.0.0.1:9000", "http://127.0.0.1:9001", "http://127.0.0.1:9002")
    assert i["app"] == "ic-int-app-1"
    p3 = sd.stack_config("p3")
    assert p3["ui"].endswith(":9030") and p3["dash"].endswith(":9032")
    with pytest.raises(ValueError):
        sd.stack_config("x")


def test_load_config(tmp_path):
    env = {"STACK": "p10", "OPS_KEY": "k1", "SCENARIO_NO_LLM": "1", "SCENARIO_TIMEOUT": "30",
           "BASE_URL": "http://localhost:1", "APP_CONTAINER": "myapp"}
    cfg = sd.load_config(env, [])
    assert cfg["base"] == "http://localhost:1"
    assert cfg["dash"] == "http://127.0.0.1:9102"
    assert cfg["app"] == "myapp"
    assert cfg["ops_key"] == "k1"
    assert cfg["no_llm"] is True and cfg["keep"] is False and cfg["repeat"] is False
    assert cfg["timeout"] == 30
    sec = tmp_path / "s.env"
    sec.write_text('A=1\r\nOPS_KEY= "abc123" \r\n', encoding="utf-8")
    cfg = sd.load_config({"STACK": "p3"}, ["--secrets", str(sec), "--keep"])
    assert cfg["ops_key"] == "abc123" and cfg["keep"] is True and cfg["timeout"] == 900
    assert sd.load_config({"STACK": "p3", "SCENARIO_KEEP": "1", "SCENARIO_REPEAT": "1"}, [])[
        "repeat"] is True


def test_refusal():
    cfg = sd.load_config({"STACK": "p10", "OPS_KEY": "k"}, [])
    assert sd.refusal(cfg, {"SCENARIO_DEMO_STACK": "1"}) is None
    assert sd.refusal(cfg, {})
    bad = dict(cfg, dash="http://example.com:9102")
    assert sd.refusal(bad, {"SCENARIO_DEMO_STACK": "1"})
    assert sd.refusal(dict(cfg, ops_key=""), {"SCENARIO_DEMO_STACK": "1"})


def test_main_refuses_without_network(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("network call")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv("SCENARIO_DEMO_STACK", raising=False)
    monkeypatch.setenv("STACK", "p10")
    assert sd.main([]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_csp_truth_table():
    s = f"'{LIB}' 'sha256-x'"
    assert sd.csp_ok(H(s), "page", script_hashes=[LIB, "sha256-x"], uses_lib=True)
    assert not sd.csp_ok(H(s), "page", script_hashes=[LIB], uses_lib=True)
    assert sd.csp_ok(H("'sha256-x'"), "page", script_hashes=["sha256-x"], uses_lib=False)
    assert not sd.csp_ok(H("'sha256-x'"), "page", script_hashes=["sha256-x"], uses_lib=True)
    assert sd.csp_ok(H("'none'"), "page", script_hashes=[], uses_lib=False)
    assert sd.csp_ok(H(s), "page", script_hashes=None, uses_lib=True)
    assert not sd.csp_ok({}, "page")
    assert not sd.csp_ok({"Content-Security-Policy": P("'none'").replace("default-src 'none'; ", "")}, "page")
    assert not sd.csp_ok({"Content-Security-Policy": P("'none'").replace("frame-ancestors 'none'; ", "")}, "page")
    assert not sd.csp_ok(H("'none' 'unsafe-eval'"), "page")
    assert not sd.csp_ok(H("'unsafe-inline'"), "page")
    assert sd.csp_ok({"content-security-policy": P("'none'")}, "page", script_hashes=[])


def test_csp_data():
    good = "default-src 'none'; frame-ancestors 'none'; sandbox"
    assert sd.csp_ok({"content-security-policy": good}, "data")
    assert not sd.csp_ok({"content-security-policy": "default-src 'none'; frame-ancestors 'none'"}, "data")
    assert not sd.csp_ok({"content-security-policy": "default-src 'none'; sandbox"}, "data")


def test_uses_library():
    assert sd.uses_library(f'<script src="{sd.LIB_SRC}"></script>')
    assert not sd.uses_library("<p>hi</p>")


def test_redact():
    out = sd.redact("x?t=abc&p=def y /d/s/?p=Q")
    for v in ("abc", "def", "Q"):
        assert v not in out.replace("<redacted>", "")
    assert out.count("<redacted>") == 3


def test_new_operator():
    a, b = sd.new_operator(), sd.new_operator()
    assert a.startswith("scn-") and a != b
    assert re.match(r"^[a-z][a-z_-]{0,31}$", a)


def test_contract_example():
    code, queries = sd.contract_example()
    assert "/d/_lib/" in code and LIB in code
    assert queries[0]["id"] == "invoiced_by_customer"


def test_csp_wrapper_and_frame_phase12():
    wrapper = {"Content-Security-Policy": "default-src 'none'; script-src 'sha256-x'; style-src 'unsafe-inline'; "
                                           "frame-src 'self'; child-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}
    assert sd.csp_ok(wrapper, "wrapper")
    assert not sd.csp_ok({"Content-Security-Policy": "default-src 'none'"}, "wrapper")
    frame = P(f"'{LIB}'").replace("frame-ancestors 'none'", "frame-ancestors 'self'").replace(
        " allow-same-origin", "")
    assert sd.csp_ok({"Content-Security-Policy": frame}, "frame")
    assert not sd.csp_ok({"Content-Security-Policy": frame + " allow-same-origin"}, "frame")
    assert sd.Scenario.frame_of("/d/s/?p=abc") == "/d/s/frame?p=abc"


# ---- Phase 18: spec dashboards and the conversion step

def test_contract_spec_example_parses():
    from app import dashboardspec
    spec = sd.contract_spec_example()
    assert dashboardspec.parse(spec).title == spec["title"]
    code, queries = sd.contract_example()  # legacy blocks still readable
    assert "/d/_lib/" in code and queries


def test_spec_keys_and_walk_keys():
    assert len(sd.CONTRACT_KEYS) == 17
    assert {"format", "tiles_added", "tiles_removed", "tiles_changed", "tiles", "measures",
            "queries"} == set(sd.SPEC_KEYS)
    assert "sql" in set(sd.walk_keys({"a": [{"sql": 1}]}))
    assert "sql" not in set(sd.walk_keys({"a": [{"x": 1}]}))


def _spec_headers(**kw):
    from app import dashboardrender, dashboards_serve
    csp = dashboards_serve.build_spec_csp(dashboardrender.CURRENT_RENDERER, "127.0.0.1:9282")
    for old, new in kw.get("replace", []):
        assert old in csp
        csp = csp.replace(old, new)
    return {"Content-Security-Policy": csp}, dashboardrender.pin(dashboardrender.CURRENT_RENDERER)


def test_csp_spec_accepts_the_real_header_and_refuses_deviations():
    from app import dashboards_serve
    h, pin = _spec_headers()
    assert sd.csp_ok(h, "spec", renderer_hash=pin["csp_hash"], lib_hash=LIB)
    prelude = dashboards_serve.PRELUDE_HASH
    bad = [
        [("'sha256-", "'unsafe-inline' 'sha256-")],
        [(f"script-src '{prelude}'", f"script-src 'self' '{prelude}'")],
        [(f"script-src '{prelude}'", f"script-src 'sha256-AAAA' '{prelude}'")],
        [("require-trusted-types-for 'script'; ", "")],
        [("trusted-types 'none'", "trusted-types x")],
        [("sandbox allow-scripts", "sandbox allow-scripts allow-same-origin")],
        [("default-src 'none'", "default-src 'self'")],
    ]
    for rep in bad:
        hh, _ = _spec_headers(replace=rep)
        assert not sd.csp_ok(hh, "spec", renderer_hash=pin["csp_hash"], lib_hash=LIB), rep
    assert not sd.csp_ok(h, "spec", renderer_hash="sha256-nope", lib_hash=LIB)
    assert not sd.csp_ok({}, "spec")
    # page kind is unchanged
    assert sd.csp_ok(H(f"'{LIB}'"), "page", script_hashes=[LIB], uses_lib=True)


def test_scenario_has_conversion_step():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "dashboard_legacy_html" in src and "needs_conversion" in src
    assert "convert the dashboard" in src and "finally" in src
