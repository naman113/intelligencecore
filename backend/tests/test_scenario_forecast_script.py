"""Offline tests for scripts/scenario_forecast.py helpers (Phase 13.1, plan 08). No network, no docker."""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "scenario_forecast.py"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("scenario_forecast_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load()


def _rows(n_hist=3, horizon=14):
    rows = [{"period": f"2026-09-{i + 1:02d}", "actual": 10.0 + i, "forecast": None,
             "low": None, "high": None, "method": "ets", "wape": 0.1, "as_of": "x"}
            for i in range(n_hist)]
    rows += [{"period": f"2026-10-{i + 1:02d}", "actual": None, "forecast": 20.0 + i,
              "low": 15.0 + i, "high": 25.0 + i, "method": "ets", "wape": 0.1, "as_of": "x"}
             for i in range(horizon)]
    return rows


def _detail(horizon=14):
    return {"method": "ets", "wape": 0.12,
            "forecast": [{"period": f"2026-10-{i + 1:02d}", "mean": 5.0, "low": 4.0, "high": 6.0}
                         for i in range(horizon)]}


def test_stack_config_p19():
    c = sf.stack_config("p19")
    assert c["base"] == "http://127.0.0.1:9191" and c["dash"] == "http://127.0.0.1:9192"
    assert c["ui"] == "http://127.0.0.1:9190"
    assert c["app"] == "ic-p19-app-1" and c["pg"] == "ic-p19-postgres-1"


def test_refusal():
    cfg = sf.stack_config("p19")
    cfg["ops_key"] = "k"
    assert sf.refusal(cfg, {}) is not None
    assert sf.refusal(cfg, {"SCENARIO_DEMO_STACK": "1"}) is None
    bad = dict(cfg, base="http://example.com:9191")
    assert sf.refusal(bad, {"SCENARIO_DEMO_STACK": "1"})
    assert sf.refusal(dict(cfg, ops_key=""), {"SCENARIO_DEMO_STACK": "1"})


def test_refuses_other_stacks_containers():
    cfg = sf.stack_config("p19")
    cfg["ops_key"] = "k"
    for bad in ("agentic-app-1", "ic-p13-app-1", "ic-p14-postgres-1"):
        assert sf.refusal(dict(cfg, app=bad), {"SCENARIO_DEMO_STACK": "1"})


def test_check_forecast_rows_ok():
    assert sf.check_forecast_rows(_rows(), 14) == []


def test_check_forecast_rows_problems():
    assert sf.check_forecast_rows([], 14)
    assert sf.check_forecast_rows(_rows(horizon=13), 14)
    r = _rows()
    r[-1]["low"] = 99.0
    assert sf.check_forecast_rows(r, 14)
    r = _rows()
    r[0]["actual"] = None
    assert sf.check_forecast_rows(r, 14)
    r = _rows()
    r[4]["actual"] = 3.0
    assert sf.check_forecast_rows(r, 14)
    r = _rows()
    r[-1]["period"] = r[-2]["period"]
    assert sf.check_forecast_rows(r, 14)


def test_check_detail():
    assert sf.check_detail(_detail(), 14) == []
    assert sf.check_detail(_detail(10), 14)
    d = _detail()
    d["forecast"][0]["mean"] = 9.0
    assert sf.check_detail(d, 14)
    d = _detail()
    d["method"] = None
    assert sf.check_detail(d, 14)
    d = _detail()
    d["wape"] = None
    assert sf.check_detail(d, 14)
    d = _detail()
    d["forecast"][0]["mean"] = float("nan")
    assert sf.check_detail(d, 14)


@pytest.mark.parametrize("text,expected", [
    ("You have hit your usage limits", True), ("credit balance is too low", True),
    ("insufficient_quota", True), ("rate_limit_error", True),
    ("no LLM configured", True), ("tool error: bad sql", False), ("", False)])
def test_is_provider_refusal(text, expected):
    assert sf.is_provider_refusal(text) is expected


def test_render_results():
    steps = [{"step": "1", "req": "FCST-01", "status": "PASS", "detail": "ok"},
             {"step": "9", "req": "FCST-05", "status": "SKIPPED", "detail": "no key"}]
    md = sf.render_results(steps, "p19")
    assert "FCST-01" in md and "SKIPPED" in md
    assert md.rstrip().splitlines()[-1] == "Verdict: PASS scenario_forecast"
    steps.append({"step": "4", "req": "FCST-02", "status": "FAIL", "detail": "boom"})
    assert "Verdict: FAIL scenario_forecast" in sf.render_results(steps, "p19")
    assert "Verdict: PASS" not in sf.render_results(steps, "p19")


def test_only_stdlib_or_sibling_imports():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module.split(".")[0])
    ok = set(sys.stdlib_module_names) | {"scenario_dashboard"}
    assert mods <= ok, mods - ok


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        sf.main(["--help"])
    assert e.value.code == 0
    assert "--live" in capsys.readouterr().out
