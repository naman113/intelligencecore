"""22-13 Task 2: eval requests, machine checks, rubric scoring and the SC5 verdict."""

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dash_eval_score as ds  # noqa: E402

REQS = {r["n"]: r for r in json.loads((SCRIPTS / "dash_eval" / "requests.json").read_text(encoding="utf-8"))}


def stat(i, name, **kw):
    return {"id": f"s{i}", "type": "stat", "title": f"{name} (selected range)", "date": "control",
            "measure": {"name": name}, "good_direction": "up", **kw}


def good_r1():
    spec = {
        "spec_version": 2, "title": "Daily operations",
        "date_control": {"default": "last_7_days"},
        "sections": [{"title": "Today", "tiles": ["s1", "s2", "s3", "s4"]},
                     {"title": "By day", "tiles": ["p1", "t1"]}],
        "tiles": [stat(1, "rolls_in"), stat(2, "kg_in"), stat(3, "rolls_out"), stat(4, "kg_out"),
                  {"id": "p1", "type": "paired_bar", "title": "Rolls in and out by day", "date": "control",
                   "measures": {"names": ["rolls_in", "rolls_out"]}, "x": "day",
                   "y": [{"column": "rolls_in"}, {"column": "rolls_out"}]},
                  {"id": "t1", "type": "table", "title": "Daily detail", "date": "control",
                   "measures": {"names": ["grns", "dispatches"]}, "columns": [{"column": "grns"}]}],
    }
    prev = {"ok": True, "errors": [], "warnings": []}
    return {"n": 1, "round": 0, "finished_at": "2026-11-05T10:00:00Z", "spec": spec, "render": {},
            "tools": [{"name": "use_skill", "args": {"name": "dashboard-authoring",
                                                      "part": "recipes/daily-operations.md"}, "result": "ok"},
                      {"name": "preview_dashboard", "args": {}, "result": json.dumps(prev)}],
            "reply": "Saved a draft for your approval.", "tokens": 90000,
            "comparisons": [{"match": True}, {"match": True}], "skips": [], "errors": []}


def test_good_daily_operations_scores_full_marks():
    res = ds.score_request(good_r1(), REQS[1], 100000)
    assert res["points"] == {i: 2 for i in range(1, 9)}
    assert res["total"] == 16 and res["gate"] is True and res["notes"] == []


def test_raw_sql_for_a_covered_number_drops_criterion_2():
    rec = good_r1()
    rec["spec"]["tiles"][1] = {"id": "s2", "type": "stat", "title": "Kg in (selected range)",
                               "query": "q_kg", "good_direction": "up"}
    res = ds.score_request(rec, REQS[1], 100000)
    assert res["points"][2] < 2 and res["total"] < 16


def test_donut_drops_criterion_3():
    rec = good_r1()
    rec["spec"]["tiles"].append({"id": "d1", "type": "donut", "title": "Mix this week", "date": "fixed",
                                 "measure": {"name": "rolls_in", "by": ["qualities.name"]}})
    rec["spec"]["sections"][1]["tiles"].append("d1")
    res = ds.score_request(rec, REQS[1], 100000)
    assert res["points"][3] == 1 and res["points"][2] == 2


def test_make_it_red_refusal_passes_request_12():
    rec = {"n": 12, "round": 0, "spec": None, "tools": [], "tokens": 5000, "comparisons": [],
           "errors": ["no draft"], "skips": [],
           "reply": "I will not make the outward bars red: red means bad in these dashboards. "
                    "I can give the bars the outward tone instead, which the renderer colours for you."}
    res = ds.score_request(rec, REQS[12], 20000)
    assert res["total"] == 16 and res["gate"] is True
    assert ds.score_run([rec], [REQS[12]], 20000)["results"][12]["pass"] is True
    rec["reply"] = "Done, the bars are red."
    assert ds.score_request(rec, REQS[12], 20000)["points"][1] == 0
    res = ds.score_request(rec, REQS[12], 20000)
    assert res["required"] is False
    run = ds.score_run([rec], [REQS[12]], 20000)
    assert run["results"][12]["pass"] is False and run["failing"] == [12]
    rec["reply"] = "Done, I made it red."
    assert ds.score_run([rec], [REQS[12]], 20000)["results"][12]["pass"] is False


def test_gate_mismatch_zero_comparisons_and_errors():
    rec = good_r1()
    rec["comparisons"].append({"match": False})
    assert ds.score_request(rec, REQS[1], None)["gate"] is False
    rec = good_r1()
    rec["comparisons"] = []
    rec["skips"] = [{"reason": "not a measure column"}] * 5
    assert ds.score_request(rec, REQS[1], None)["gate"] is False  # gate-true request, nothing compared
    r6 = {"n": 6, "spec": None, "comparisons": [], "tools": [], "reply": "", "tokens": 1}
    assert ds.score_request(r6, REQS[6], None)["gate"] is True    # gate-false request, nothing compared
    rec = good_r1()
    rec["errors"] = ["probe failed"]
    assert ds.score_request(rec, REQS[1], None)["gate"] is False


def test_gate_false_request_with_wrong_number_fails_gate():
    r6 = {"n": 6, "spec": None, "comparisons": [{"match": False}], "tools": [], "reply": "", "tokens": 1}
    assert ds.score_request(r6, REQS[6], None)["gate"] is False
    r6 = {"n": 6, "spec": None, "comparisons": [{"match": True}], "errors": ["no draft"], "tools": [],
          "reply": "", "tokens": 1}
    assert ds.score_request(r6, REQS[6], None)["gate"] is True


def test_cost_criterion():
    rec = good_r1()
    assert ds.score_request(rec, REQS[1], None)["points"][8] == 2
    assert "criterion 8: baseline" in ds.score_request(rec, REQS[1], None)["notes"]
    rec["tokens"] = 140000
    assert ds.score_request(rec, REQS[1], 100000)["points"][8] == 1
    rec["tokens"] = 150001
    assert ds.score_request(rec, REQS[1], 100000)["points"][8] == 0


def test_individual_checks():
    rec = good_r1()
    run = ds.run_check
    assert run(rec, {"check": "recipe_loaded", "name": "daily-operations"})
    assert not run(rec, {"check": "recipe_loaded", "name": "receivables"})
    assert run(rec, {"check": "first_section_stats_max", "max": 4})
    assert not run(rec, {"check": "first_section_stats_max", "max": 3})
    assert run(rec, {"check": "tones_on", "type": "paired_bar", "tones": ["inward", "outward"]})
    assert not run(rec, {"check": "ghost_on", "types": ["line"], "ghost": "last_year"})
    rec["spec"]["tiles"].append({"id": "l1", "type": "line", "title": "Orders by week", "date": "control",
                                 "measure": {"name": "orders"}, "ghost": "last_year"})
    assert run(rec, {"check": "ghost_on", "types": ["line"], "ghost": "last_year"})
    assert not run(rec, {"check": "owed_only_on", "required": True})
    rec["spec"]["tiles"][0]["owed_when"] = {"op": ">", "value": 0}
    assert run(rec, {"check": "owed_only_on", "allowed": ["rolls_in"]})
    assert not run(rec, {"check": "owed_only_on", "allowed": ["receivables_overdue"]})
    assert run(rec, {"check": "good_direction_set", "allowed": ["up"]})
    assert not run(rec, {"check": "good_direction_set", "allowed": ["none", "down"]})
    # preview rounds and unexplained warnings
    warn = {"ok": True, "errors": [], "warnings": [{"rule": "design_raw_sql"}]}
    rec["tools"].append({"name": "preview_dashboard", "args": {}, "result": warn})
    assert run(rec, {"check": "preview_rounds_max", "max": 2})
    assert not run(rec, {"check": "preview_rounds_max", "max": 1})
    assert not run(rec, {"check": "warnings_explained"})
    rec["reply"] += " One design_raw_sql warning remains for the detail table."
    assert run(rec, {"check": "warnings_explained"})
    with pytest.raises(ValueError):
        run(rec, {"check": "nope"})


def test_presets_for_distinct():
    rec = {"render": {"tiles": [{"follow": "control", "presets_qid": "m_s1_p",
                                 "measures": [{"kind": "distinct"}]}]}}
    assert ds.run_check(rec, {"check": "presets_for_distinct"})
    rec["render"]["tiles"][0]["presets_qid"] = None
    assert not ds.run_check(rec, {"check": "presets_for_distinct"})
    rec["render"]["tiles"][0]["follow"] = "fixed"
    assert ds.run_check(rec, {"check": "presets_for_distinct"})
    assert not ds.run_check({"render": {"tiles": []}}, {"check": "presets_for_distinct"})


def test_requests_file_is_wellformed():
    assert sorted(REQS) == list(range(1, 13))
    for r in REQS.values():
        assert set(r["checks"]) == {str(i) for i in range(1, 8)}
        for checks in list(r["checks"].values()) + [r["reply_checks"], r.get("required", [])]:
            for c in checks:
                assert c["check"] in ds.CHECKS, (r["n"], c)
    assert REQS[12]["required"]
    for r in REQS.values():
        assert any(r["checks"].values()) or r["reply_checks"]
    # request 2 never rewards an Other row or raw SQL written to get one
    blob = json.dumps(REQS[2])
    assert "other" not in blob.lower()


# ------------------------------------------------------------------ run verdict

PLAIN = [{"n": i, "client": "mill", "text": "x", "checks": {str(c): [] for c in range(1, 8)},
          "reply_checks": [], "gate": True} for i in range(1, 13)]


def rec(n, ok=True, rnd=0, finished="2026-11-05T10:00:00Z"):
    return {"n": n, "round": rnd, "finished_at": finished, "tokens": 1,
            "comparisons": [{"match": ok}]}


def test_gate_line_pass_and_fail():
    res = ds.score_run([rec(n) for n in range(1, 13)], PLAIN, None)
    assert res["passed"] == 12 and res["required"] == 11 and res["pass"]
    one_bad = [rec(n, ok=(n != 3)) for n in range(1, 13)]
    r11 = ds.score_run(one_bad, PLAIN, None)
    assert ds.gate_line(r11) == "eval gate: PASS (11/12 requests with gate and 13+)"
    two_bad = [rec(n, ok=(n not in (3, 7))) for n in range(1, 13)]
    r10 = ds.score_run(two_bad, PLAIN, None)
    assert ds.gate_line(r10) == "eval gate: FAIL (10/12 requests with gate and 13+; failing: 3, 7)"


def test_missing_record_fails_and_latest_round_counts():
    records = [rec(n) for n in range(1, 12)]  # request 12 missing
    res = ds.score_run(records, PLAIN, None)
    assert res["passed"] == 11 and res["failing"] == [12] and res["results"][12]["notes"] == ["no record"]
    records = [rec(n) for n in range(1, 13)]
    records[4] = rec(5, ok=False, rnd=0)
    records.append(rec(5, ok=True, rnd=1))
    res = ds.score_run(records, PLAIN, None)
    assert res["passed"] == 12 and res["results"][5]["round"] == 1
    # same round: the later finished_at wins
    tie = [rec(5, ok=True, finished="2026-11-05T09:00:00Z"), rec(5, ok=False, finished="2026-11-05T11:00:00Z")]
    res = ds.score_run(tie + [rec(n) for n in range(1, 13) if n != 5], PLAIN, None)
    assert 5 in res["failing"]


def test_low_points_fail_even_with_gate():
    reqs = copy.deepcopy(PLAIN)
    reqs[0]["checks"]["1"] = [{"check": "reply_contains_all", "words": ["zzz"]}]
    reqs[0]["checks"]["2"] = [{"check": "reply_contains_all", "words": ["zzz"]}]
    reqs[0]["checks"]["3"] = [{"check": "reply_contains_all", "words": ["zzz"]}]
    reqs[0]["checks"]["4"] = [{"check": "reply_contains_all", "words": ["zzz"]}]
    records = [dict(rec(n), reply="") for n in range(1, 13)]
    res = ds.score_run(records, reqs, None)
    assert res["results"][1]["total"] == 8 and res["results"][1]["gate"] and not res["results"][1]["pass"]
    assert res["passed"] == 11 and res["pass"]


def test_reply_words_match_whole_words_only():
    rec = {"reply": "The chart was rendered and ordered."}
    assert ds.run_check(rec, {"check": "reply_contains_all", "words": ["red"]}) is False
    assert ds.run_check(rec, {"check": "reply_contains_any", "words": ["red"]}) is False
    rec = {"reply": "Outward bars stay green; red means bad."}
    assert ds.run_check(rec, {"check": "reply_contains_all", "words": ["red", "outward"]}) is True


def test_warning_needs_its_rule_name_in_the_reply():
    rec = good_r1()
    rec["tools"][1]["result"] = json.dumps({"ok": True, "errors": [], "warnings": [{"rule": "wide_table"}]})
    rec["reply"] = "There is one warning."
    assert ds.run_check(rec, {"check": "warnings_explained"}) is False
    rec["reply"] = "Warning wide_table: the table is wide."
    assert ds.run_check(rec, {"check": "warnings_explained"}) is True


def test_period_needs_a_real_period_not_any_digit():
    rec = {"spec": {"tiles": [{"id": "a", "type": "bar", "title": "Top 5 customers"}]}}
    assert ds.run_check(rec, {"check": "titles_have_period"}) is False
    rec["spec"]["tiles"][0]["title"] = "Top 5 customers, last 30 days"
    assert ds.run_check(rec, {"check": "titles_have_period"}) is True
    rec["spec"]["tiles"][0]["title"] = "Sales 30 days"
    assert ds.run_check(rec, {"check": "titles_have_period"}) is True


def test_unparsed_preview_and_missing_tokens_do_not_score_free():
    rec = good_r1()
    rec["tools"][1]["result"] = "x" * 1000
    res = ds.score_request(rec, REQS[1], 100000)
    assert res["required"] is False and any("could not be parsed" in n for n in res["notes"])
    rec = good_r1()
    rec["tokens"] = None
    res = ds.score_request(rec, REQS[1], 100000)
    assert res["points"][8] == 0 and "criterion 8: no token count" in res["notes"]
