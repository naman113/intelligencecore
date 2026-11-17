"""Phase 13 (GOLIVE-08 gap): pure parts of the KPI compile probe. No network, no docker."""
import importlib.util
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "probe_kpi_compile", BACKEND / "scripts" / "probe_kpi_compile.py")
pk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pk)


def test_refuses_without_demo_flag(monkeypatch, capsys):
    monkeypatch.delenv("SCENARIO_DEMO_STACK", raising=False)
    monkeypatch.setenv("STACK", "p13")
    monkeypatch.setenv("OPS_KEY", "k")
    assert pk.main([]) == 2
    assert capsys.readouterr().out.startswith("REFUSED")


def test_refuses_non_loopback_and_no_key(monkeypatch):
    monkeypatch.setenv("SCENARIO_DEMO_STACK", "1")
    monkeypatch.setenv("STACK", "p13")
    monkeypatch.setenv("OPS_KEY", "k")
    monkeypatch.setenv("BASE_URL", "http://example.com:9131")
    assert pk.main([]) == 2
    monkeypatch.delenv("BASE_URL")
    monkeypatch.delenv("OPS_KEY")
    assert pk.main([]) == 2


def test_conditions():
    c = {x["id"]: x["condition"] for x in pk.PROBE_CONDITIONS}
    assert list(c) == ["compound_stale", "compound_high", "plain"]
    for t in c.values():
        assert "sales_orders" in t and "'open'" in t
    assert "Record that count as the daily value" in c["compound_stale"]
    assert "older than 7 days" in c["compound_stale"]
    assert "above 500" in c["compound_high"]


def _w(rule, err=None):
    return {"trigger_rule": rule, "condition": "c", "compile_error": err}


def test_verdict_pass_and_fail():
    rule = json.dumps({"column": "n", "value_column": "n"})
    h = {"points": [1], "latest": {"metric": "n", "value": 84.0}}
    v, ev = pk.probe_verdict(_w(rule), h, 84)
    assert v == "PASS" and "metric=n" in ev and "trigger=n" in ev and "value_column=n" in ev
    v, ev = pk.probe_verdict(_w({"column": "x"}, "boom"), {"points": [1], "latest": {"metric": "x", "value": 3}}, 84)
    assert v == "FAIL" and "value_column=-" in ev and "compile_error=boom" in ev
    v, ev = pk.probe_verdict(_w(None), {"points": []}, 84)
    assert v == "FAIL" and "no history" in ev


def test_render():
    rows = [{"repeat": 1, "id": "plain", "watch_id": 5, "verdict": "PASS", "seconds": 40, "evidence": "e"},
            {"repeat": 1, "id": "compound_stale", "watch_id": 6, "verdict": "FAIL", "seconds": 300, "evidence": "f"}]
    t = pk.render_probe(rows, "abc123")
    assert t.startswith("# Phase 13 KPI compile probe (GOLIVE-08 gap)")
    assert "App commit: abc123" in t
    assert "Verdict: FAIL probe_kpi_compile (1/2)" in t


def test_no_direct_sql_writes():
    src = (BACKEND / "scripts" / "probe_kpi_compile.py").read_text(encoding="utf-8")
    for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert bad not in src
