"""Phase 17 (17-14): pure helpers of scripts/cons_gate.py (no database)."""
import json

import pytest

from scripts import cons_gate as g


def test_parser_accepts_every_step():
    assert len(g.STEPS) >= 20
    for step in g.STEPS:
        assert g.build_parser().parse_args([step]).step == step


def test_handlers_cover_steps():
    assert set(g.HANDLERS) == set(g.STEPS)


def test_parser_rejects_unknown_step():
    with pytest.raises(SystemExit):
        g.build_parser().parse_args(["nope"])


def test_parser_options():
    a = g.build_parser().parse_args(["force-check", "--watch", "7", "--timeout", "30", "--budget", "4"])
    assert a.watch == 7 and a.timeout == 30.0 and a.budget == 4


def test_parse_json_arg():
    assert g.parse_json_arg("") == {}
    assert g.parse_json_arg('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        g.parse_json_arg("[1]")
    with pytest.raises(ValueError):
        g.parse_json_arg("{bad")
    assert g.parse_list_arg('["a"]') == ["a"]
    with pytest.raises(ValueError):
        g.parse_list_arg("{}")


def test_close_enough():
    assert g.close_enough(1.0, 1.0 + 1e-12)
    assert not g.close_enough(1.0, 1.001)
    assert g.close_enough("100", 100.0)
    assert not g.close_enough(None, 1)
    assert not g.close_enough(float("nan"), float("nan"))
    assert g.close_enough(0, 0)


def test_norm_signature():
    a = g.norm_signature([{"name": "ID", "type": "integer"}, ("Amt", "numeric(12,2)")])
    assert a == ["id:integer", "amt:numeric(12,2)"]
    assert g.norm_signature([("x", "character  varying")]) == ["x:character varying"]
    assert g.norm_signature(None) == []


def test_classify_and_ids():
    assert g.classify("REFUSED: no")["refused"]
    assert g.classify("TOOL ERROR: x")["tool_error"] and not g.classify("TOOL ERROR: x")["ok"]
    assert g.classify("LIVE QUERY BUDGET SPENT: x")["budget_spent"]
    assert g.first_id("proposed KPI watch (action #12): t", "action") == 12
    assert g.first_id("recorded insight (note event #5) on case", "note event") == 5
    assert g.first_id("nothing", "action") is None


def test_first_number():
    assert g.first_number([{"a": "x", "b": 3}]) == 3.0
    assert g.first_number([]) is None
    assert g.first_number([{"a": True}]) is None


def test_shape_one_line_and_exit():
    line = g.shape({"ok": True, "v": "é"})
    assert "\n" not in line and json.loads(line)["ok"] is True
    assert g.exit_code({"ok": False}) == 1 and g.exit_code({"ok": True}) == 0
