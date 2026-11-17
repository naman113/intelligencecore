"""Offline checks of scripts/dash_phone_budget.mjs (Phase 22, DCAP-01): syntax, the pure helpers
(spec builder, throttling table, result evaluation) and the guards. The live run is plan 22-16."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import dashboardspec

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dash_phone_budget.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(*args, env=None):
    return subprocess.run([NODE, str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env=env)


def _json(*args):
    r = _run(*args)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_node_check_passes():
    r = subprocess.run([NODE, "--check", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_spec_is_40_tiles_12_charts_and_valid_v2():
    spec = _json("--print-spec", "--measures", "m_a,m_b,m_c")
    assert len(spec["tiles"]) == 40
    charts = [t for t in spec["tiles"] if t["type"] in dashboardspec.CHART_TYPES_V2]
    assert len(charts) == 12
    parsed = dashboardspec.parse(spec)
    assert parsed.spec_version == 2 and len(parsed.tiles) == 40
    assert all(t["measure"]["name"] in {"m_a", "m_b", "m_c"} for t in spec["tiles"] if "measure" in t)
    placed = [i for s in spec["sections"] for i in s["tiles"]]
    assert sorted(placed) == sorted(t["id"] for t in spec["tiles"])
    assert all(t.get("date") == "control" for t in spec["tiles"] if t["type"] != "text")


def test_spec_uses_at_most_six_measures():
    spec = _json("--print-spec", "--measures", ",".join(f"m{i}" for i in range(9)))
    assert len({t["measure"]["name"] for t in spec["tiles"] if "measure" in t}) <= 6


def test_throttling_table():
    out = _json("--print-profiles")
    p = out["profiles"]
    assert out["budget_ms"] == 2000 and out["mark"] == "ic-first-screen"
    assert p["4g"]["latency"] == 150 and p["4g"]["down"] == 500000 and p["4g"]["up"] == 187500
    assert p["slow4g"]["latency"] == 150 and p["slow4g"]["down"] == 200000 and p["slow4g"]["up"] == 93750
    for prof in p.values():
        assert prof["cpu"] == 4 and (prof["width"], prof["height"]) == (412, 915)


def test_evaluate_passes_only_when_4g_cold_is_under_budget():
    ok = _json("--evaluate", json.dumps([{"profile": "4g", "cold_ms": 1500, "warm_ms": 600},
                                         {"profile": "slow4g", "cold_ms": 3900, "warm_ms": 900}]))
    assert ok["ok"] is True
    assert ok["lines"][0] == {"profile": "4g", "cold_ms": 1500, "warm_ms": 600, "budget_ms": 2000, "pass": True}
    assert ok["lines"][1]["pass"] is False  # slow4g is reported, not gated
    slow = _json("--evaluate", json.dumps([{"profile": "4g", "cold_ms": 2000, "warm_ms": 1}]))
    assert slow["ok"] is False
    none = _json("--evaluate", json.dumps([{"profile": "4g", "cold_ms": None, "warm_ms": None}]))
    assert none["ok"] is False and none["lines"][0]["pass"] is False
    assert _json("--evaluate", "[]")["ok"] is False


def test_source_contract():
    src = SCRIPT.read_text(encoding="utf-8")
    for marker in ("Network.emulateNetworkConditions", "Network.setCacheDisabled", "Emulation.setCPUThrottlingRate",
                   "Emulation.setDeviceMetricsOverride", "ic-first-screen", "performance.timeOrigin",
                   "/api/model/measures", "/publish", "is not loopback", "waitForDebuggerOnStart"):
        assert marker in src, marker
    assert "BINDABLE" in src and "m.kind" in src  # only measures the date control can follow
    lines = [ln for ln in src.splitlines() if "console." in ln]
    assert len(lines) == 2 and all("redact(" in ln for ln in lines)


def test_refuses_non_loopback_and_missing_key():
    env = dict(os.environ, BASE_URL="http://10.0.0.1:9281", OPS_KEY="x")
    r = _run(env=env)
    assert r.returncode == 2 and "not loopback" in r.stderr
    env2 = {k: v for k, v in os.environ.items() if k != "OPS_KEY"}
    r2 = _run(env=env2)
    assert r2.returncode == 2 and "OPS_KEY" in r2.stderr
