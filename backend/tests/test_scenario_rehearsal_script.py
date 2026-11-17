"""Phase 13 (GOLIVE-08): pure parts of the dress-rehearsal driver. No network, no docker."""
import importlib.util
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "scenario_rehearsal", BACKEND / "scripts" / "scenario_rehearsal.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def test_refuses_without_demo_flag(monkeypatch, capsys):
    monkeypatch.delenv("SCENARIO_DEMO_STACK", raising=False)
    monkeypatch.setenv("STACK", "p13")
    monkeypatch.setenv("OPS_KEY", "k")
    assert sr.main([]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_refuses_non_loopback(monkeypatch, capsys):
    monkeypatch.setenv("SCENARIO_DEMO_STACK", "1")
    monkeypatch.setenv("STACK", "p13")
    monkeypatch.setenv("OPS_KEY", "k")
    monkeypatch.setenv("BASE_URL", "http://example.com:9131")
    assert sr.main([]) == 2


def test_refuses_without_key(monkeypatch, capsys):
    monkeypatch.setenv("SCENARIO_DEMO_STACK", "1")
    monkeypatch.setenv("STACK", "p13")
    monkeypatch.delenv("OPS_KEY", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    assert sr.main([]) == 2


def test_instruction_matches_ask_ts():
    text = (BACKEND.parent / "frontend" / "src" / "lib" / "ask.ts").read_text(encoding="utf-8")
    m = re.search(r"importContextPack:\s*\(\)\s*=>\s*\n?\s*'([^']*)'", text)
    assert sr.INSTRUCTION and sr.INSTRUCTION == m.group(1)


def test_operator_names_have_no_digits():
    assert sr.OPERATOR_RE.match(sr.RUN_OP) and sr.OPERATOR_RE.match(sr.UPLOAD_OP)


def test_fresh_stack_problems():
    assert sr.fresh_stack_problems({"approved_context": 0, "create_watch_actions": "0", "dashboards": 0}) == []
    p = sr.fresh_stack_problems({"approved_context": 3, "create_watch_actions": 0, "dashboards": 1})
    assert len(p) == 2 and "approved_context" in p[0]


def test_pick_reply():
    msgs = [{"id": 1, "role": "assistant", "content": "old"}, {"id": 2, "role": "user", "content": "q"},
            {"id": 3, "role": "assistant", "content": "new"}]
    assert sr.pick_reply(msgs, 1) == "new"
    assert sr.pick_reply(msgs, 3) is None


def test_import_action_ids_decodes_string_payload():
    acts = [{"id": 1, "kind": "context_change", "task_run_id": 7, "payload": "{\"a\": 1}"},
            {"id": 2, "kind": "create_watch", "task_run_id": 7, "payload": {}},
            {"id": 3, "kind": "context_change", "task_run_id": 8, "payload": {}},
            {"id": 4, "kind": "send_message", "task_run_id": 7, "payload": {}}]
    assert sr.import_action_ids(acts, 7) == [1, 2]


def test_dashboard_verdict():
    ok = "x\nPASS scenario_dashboard (agent mode)\n"
    assert sr.dashboard_verdict(0, ok)[0] == "PASS"
    assert sr.dashboard_verdict(0, ok + "BUDGET tripped")[0] == "FAIL"
    v = sr.dashboard_verdict(3, "")
    assert v == ("FAIL", "no browser")
    assert sr.dashboard_verdict(1, ok)[0] == "FAIL"


def _state(reasons=()):
    return {"stack": "p13", "commit": "abc", "roles": [{"role": "supervisor", "model": "m", "effort": "medium", "source": "default"}],
            "steps": [{"step": "1", "name": "n", "result": "PASS", "seconds": 1, "evidence": "k"}],
            "questions": [{"id": f"q{i}", "ask": "a", "expected": 1, "result": "PASS", "seconds": 1,
                           "tools": ["run_sql"], "excerpt": "x"} for i in range(1, 11)],
            "reasons": list(reasons)}


def test_render_results():
    t = sr.render_results(_state())
    assert len(re.findall(r"^\| q(?:[1-9]|10) ", t, re.M)) == 10
    assert t.rstrip().endswith("Verdict: PASS scenario_rehearsal")
    f = sr.render_results(_state(["step 7"]))
    assert "Verdict: FAIL scenario_rehearsal (step 7)" in f


def test_track_evidence_full():
    w = {"condition": "Count open\norders",
         "trigger_rule": "{\"type\": \"change\", \"column\": \"open_orders\", \"value_column\": \"open_orders\"}"}
    ev = sr.track_evidence(w, {"latest": {"metric": "open_orders", "value": 84.0}})
    for part in ("metric=open_orders", "trigger=open_orders", "value_column=open_orders",
                 "condition=Count open orders"):
        assert part in ev


def test_track_evidence_empty_and_cut():
    ev = sr.track_evidence({}, {})
    assert "metric=-" in ev and "trigger=-" in ev and "value_column=-" in ev
    assert len(sr.track_evidence({"condition": "x" * 500}, {}).split("condition=")[1]) == 160
