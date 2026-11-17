"""Pure-logic tests for scripts/smoke_models.py (Phase 13, GOLIVE-01). No network."""
import importlib.util
from pathlib import Path

from app import llmclient

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_models.py"
spec = importlib.util.spec_from_file_location("smoke_models_under_test", SCRIPT)
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)


def _cfg(overrides=None, source="default"):
    items = [{"key": k, "value": (overrides or {}).get(k, v), "source": source}
             for k, v in sm.LOCKED_DEFAULTS.items()]
    return {"config": {"models": items}}


def test_refused_without_demo_flag(monkeypatch, capsys):
    monkeypatch.delenv("SCENARIO_DEMO_STACK", raising=False)
    assert sm.main(["--secrets", "nope"], {"STACK": "p13", "OPS_KEY": "k"}) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_refused_non_loopback(capsys):
    env = {"STACK": "p13", "OPS_KEY": "k", "SCENARIO_DEMO_STACK": "1", "BASE_URL": "http://example.com:1"}
    assert sm.main([], env) == 2


def test_refused_without_key(capsys):
    assert sm.main([], {"STACK": "p13", "SCENARIO_DEMO_STACK": "1"}) == 2


def test_locked_defaults_match_efforts():
    efforts = {k.rsplit("_", 1)[0]: v for k, v in sm.LOCKED_DEFAULTS.items() if k.endswith("_effort")}
    assert efforts == llmclient.ROLE_EFFORT_FALLBACK
    assert sm.LOCKED_DEFAULTS["triage_model"] == "claude-haiku-5-5"


def test_check_defaults_pass_and_fail():
    assert all(r["result"] == "PASS" for r in sm.check_defaults(_cfg()))
    bad = sm.check_defaults(_cfg({"document_model": "claude-opus-5-5"}))
    fails = [r for r in bad if r["result"] == "FAIL"]
    assert len(fails) == 1 and "document_model" in fails[0]["note"]
    assert any(r["result"] == "FAIL" for r in sm.check_defaults(_cfg(source="db")))


def test_render_and_summary():
    rows = [sm.row("1", "probe", "", "m", "", "p", "PASS", 1, "has SECRETKEY inside"),
            sm.row("2", "x", "", "", "", "", "SKIP", 0, "no key")]
    text = sm.render_results(rows, "SECRETKEY")
    assert text.startswith(sm.HEADER) and "SECRETKEY" not in text
    assert sm.summary_line(rows) == "PASS smoke_models"
    rows.append(sm.row("3", "x", "", "", "", "", "FAIL"))
    assert sm.summary_line(rows) == "FAIL smoke_models (1 failed)"
