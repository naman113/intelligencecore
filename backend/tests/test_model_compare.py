"""Phase 16 plan 08: the pure parts of scripts/model_compare.py (no stack needed)."""
import importlib.util
import sys
from pathlib import Path

import pytest

from app.guardrails import validate_readonly_sql

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_compare.py"
spec = importlib.util.spec_from_file_location("model_compare", SCRIPT)
mcmp = importlib.util.module_from_spec(spec)
sys.modules["model_compare"] = mcmp
spec.loader.exec_module(mcmp)

SUP_LINE = ("- TWO LANES (Phase 16): every number comes from query_measure when get_model lists a measure.")
SUP_PROMPT = "You are the supervisor of the system.\n- Rule one stays.\n" + SUP_LINE + "\n- Rule two stays."
DATA_PROMPT = ("You are the data agent. Use run_sql for questions.\n"
               "Never write data. TWO LANES (Phase 16): prefer query_measure ... unverified.")


def entry(prompt, customized=False, tools=None):
    return {"config_key": "data", "customized": customized,
            "config": {"prompt": prompt, "model": "auto", "temperature": 0.2, "max_tokens": 4096,
                       "recursion_limit": 25,
                       "tools": tools or ["run_sql", "get_model", "query_measure", "propose_model_change",
                                          "propose_relationships_from_keys", "discover_schema"]}}


def test_twenty_unique_questions_fifteen_modeled():
    ids = [q["id"] for q in mcmp.QUESTIONS]
    assert ids == [f"q{i:02d}" for i in range(1, 21)]
    assert sum(q["expected"] == "modeled" for q in mcmp.QUESTIONS) == 15
    assert sum(q["expected"] == "not_modeled" for q in mcmp.QUESTIONS) == 5
    assert all(q["expected"] == "modeled" for q in mcmp.QUESTIONS[:15])


def test_at_least_three_measures_used():
    used = {r["measure"] for q in mcmp.QUESTIONS if q["expected"] == "modeled" for r in q["measure_requests"]}
    assert len(used) >= 3
    assert used == set(mcmp.SEVEN)


def test_not_modeled_questions_name_unmodeled_measures():
    names = {r["measure"] for q in mcmp.QUESTIONS[15:] for r in q["measure_requests"]}
    assert names.isdisjoint(mcmp.SEVEN)
    assert {"days_to_pay", "unpaid_invoice_amount", "active_customers", "return_rate",
            "units_sold"} == names


def test_ranges_are_literal_and_dimensions_qualified():
    for q in mcmp.QUESTIONS:
        for r in q["measure_requests"]:
            rg = r.get("range")
            assert rg is None or (set(rg) == {"start", "end"} and len(rg["start"]) == 10)
            for d in r.get("by", []):
                assert d.count(".") == 2


@pytest.mark.parametrize("q", mcmp.QUESTIONS, ids=lambda q: q["id"])
def test_reference_sql_passes_guardrails(q):
    validate_readonly_sql(q["reference_sql"])
    assert q["compare"] in ("scalar", "rows")


def test_extract_numbers():
    assert mcmp.extract_numbers("Net sales were 1,234,567.89 in September (up 3.2%)") == [1234567.89, 3.2]
    assert mcmp.extract_numbers("See action #431 and case #19 for details") == []
    assert mcmp.extract_numbers("") == []


def test_agrees_scalar_and_rows():
    assert mcmp.agrees(1000, 1004.9)
    assert not mcmp.agrees(1000, 1006)
    assert mcmp.agrees(0, 0.005)
    assert not mcmp.agrees(0, 0.5)
    assert mcmp.agrees({"a": 10, "b": 20}, {"a": 10.01, "b": 20})
    assert not mcmp.agrees({"a": 10, "b": 20}, {"a": 10})
    assert not mcmp.agrees({"a": 10}, {"a": 11})


def test_score_raw():
    assert mcmp.score_raw("Net sales were 1,000.00", 1000.0) == "agree"
    assert mcmp.score_raw("Net sales were 5,000", 1000.0) == "wrong"
    assert mcmp.score_raw("I could not find it", 1000.0) == "no_answer"
    ref = {"a": 100.0, "b": 200.0, "c": 300.0, "d": 400.0, "e": 500.0}
    assert mcmp.score_raw("a 100 b 200 c 300 d 400 e 999", ref) == "agree"
    assert mcmp.score_raw("a 100 b 200", ref) == "wrong"
    assert mcmp.score_raw("Growth was -28.9%", -0.289, percent=True) == "agree"


def test_strip_prompt_supervisor_line_only():
    out = mcmp.strip_phase16_prompt(SUP_PROMPT)
    assert "TWO LANES" not in out
    assert out == "You are the supervisor of the system.\n- Rule one stays.\n- Rule two stays."


def test_strip_prompt_data_tail_idempotent_and_unchanged():
    out = mcmp.strip_phase16_prompt(DATA_PROMPT)
    assert out.endswith("Never write data.") and "TWO LANES" not in out
    assert mcmp.strip_phase16_prompt(out) == out
    plain = "A prompt without the marker that is long enough."
    assert mcmp.strip_phase16_prompt(plain) == plain
    assert len(out) >= 20


def test_config_raw_lane_body_removes_measure_tools_keeps_order():
    body = mcmp.raw_lane_body(entry(DATA_PROMPT))
    assert body["tools"] == ["run_sql", "discover_schema"]
    assert "TWO LANES" not in body["prompt"]
    assert (body["model"], body["temperature"], body["max_tokens"], body["recursion_limit"]) == \
        ("auto", 0.2, 4096, 25)


def test_config_raw_lane_body_rejects_short_prompt():
    with pytest.raises(ValueError):
        mcmp.raw_lane_body(entry("TWO LANES (Phase 16): x"))


def test_config_restore_plan():
    assert mcmp.restore_plan(entry(DATA_PROMPT, customized=False)) == ("delete", None)
    kind, body = mcmp.restore_plan(entry(DATA_PROMPT, customized=True))
    assert kind == "put" and body["prompt"] == DATA_PROMPT
    assert "get_model" in body["tools"] and body["recursion_limit"] == 25


def test_verdict_contaminated_first():
    assert mcmp.raw_verdict(["run_sql", "query_measure"], "1000", 1000.0, "scalar") == "contaminated"
    assert mcmp.raw_verdict(["get_model:refused"], "1000", 1000.0, "scalar") == "contaminated"


def test_verdict_no_sql_and_scoring():
    assert mcmp.raw_verdict(["search_business_context"], "1000", 1000.0, "scalar") == "no_sql"
    assert mcmp.raw_verdict(["run_sql"], "It is 1,000", 1000.0, "scalar") == "agree"
    assert mcmp.raw_verdict(["run_sql"], "It is 7", 1000.0, "scalar") == "wrong"
    assert mcmp.raw_verdict(["run_sql"], "no idea", 1000.0, "scalar") == "no_answer"


def test_summarise_lane_totals():
    res = [{"measure_status": "measure_agree", "raw_verdict": "agree"},
           {"measure_status": "measure_mismatch", "raw_verdict": "wrong"},
           {"measure_status": "not_modeled_correct", "raw_verdict": "no_sql"},
           {"measure_status": "not_modeled_wrong", "raw_verdict": "contaminated"},
           {"measure_status": "measure_agree", "raw_verdict": "no_answer"},
           {"measure_status": "measure_agree"}]
    t = mcmp.summarise(res)
    assert t["measure_agree"] == 3 and t["raw_skipped"] == 1 and t["raw_contaminated"] == 1
    assert sum(t[k] for k in ("measure_agree", "measure_mismatch", "not_modeled_correct",
                              "not_modeled_wrong")) == 6
    assert sum(v for k, v in t.items() if k.startswith("raw_")) == 6


def test_value_helpers():
    doc = {"measure": "net_sales", "columns": ["name", "net_sales"],
           "rows": [{"name": "A", "net_sales": "10"}, {"name": "B", "net_sales": "30"},
                    {"name": "C", "net_sales": "20"}]}
    assert mcmp.measure_value(doc, "rows", top=2) == {"B": 30.0, "C": 20.0}
    assert mcmp.measure_value({"measure": "m", "columns": ["m"], "rows": [{"m": "5"}]}, "scalar") == 5.0
    assert mcmp.reference_value([{"k": "x", "value": 3}], "rows") == {"x": 3.0}


def test_refuses_without_demo_flag():
    cfg = mcmp.sm.load_config({"STACK": "p26", "OPS_KEY": "k"}, [])
    assert mcmp.sm.refusal(cfg, {}) is not None
    assert mcmp.sm.refusal(cfg, {"SCENARIO_DEMO_STACK": "1"}) is None


# ---- 16-11: label gate -------------------------------------------------------

def test_label_questions():
    assert mcmp.LABEL_QUESTIONS == ("q17", "q19")


@pytest.mark.parametrize("names,reply,want", [
    (["get_model", "run_sql"], "There are 183 unpaid invoices (unverified).", True),
    (["run_sql"], "There are 183 unpaid invoices.", False),
    (["run_sql", "propose_model_change"], "183 unpaid invoices; I proposed a measure.", False),
    (["propose_model_change"], "I proposed unpaid_invoice_amount for approval.", True),
    (["run_sql:refused", "run_sql"], "UNVERIFIED: 183", True),
    ([], "I cannot answer that.", False),
])
def test_label_ok(names, reply, want):
    assert mcmp.label_ok(names, reply) is want


def test_skill_ok_cases():
    good = {"ran": True, "q01_used_query_measure": True, "q17": {"label_ok": True}, "q19": {"label_ok": True}}
    assert mcmp.skill_ok(good) is True
    assert mcmp.skill_ok({**good, "q01_used_query_measure": False}) is False
    assert mcmp.skill_ok({**good, "q17": {"label_ok": False}}) is False
    assert mcmp.skill_ok({**good, "q19": {}}) is False
    assert mcmp.skill_ok({**good, "ran": False}) is False


def test_strip_real_prompts():
    from app.agents import graph
    for k in ("supervisor", "data"):
        out = mcmp.strip_phase16_prompt(graph.DEFAULT_PROMPTS[k])
        assert "TWO LANES" not in out and "whether or not" not in out
        assert len(out) >= 20
