"""Quick 029 F3: the approval-routing rules are a never-stored layer after the editable prompt."""
from _supprompt import composed_supervisor_prompt

from app.agents import graph

HEADERS = ("INSIGHT TO KPI", "DASHBOARD STATUS (Phase 12)", "CONTEXT PACKS (Phase 12)",
           "WATCH REQUESTS (Phase 12)", "TRACK THIS (Phase 13)", "FORECASTS (Phase 13.1)")
TEXTS = [t for _, t in graph.SUPERVISOR_ROUTING_RULES]


def test_each_rule_once_after_a_customised_prompt_and_before_sql_routing(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch, prompt="Be brief and friendly.")
    assert graph.SUPERVISOR_RULES_HEADER in p
    for t in TEXTS:
        assert p.count(t) == 1
    assert p.index("Be brief and friendly.") < p.index(graph.SUPERVISOR_RULES_HEADER)
    for t in TEXTS:
        assert p.index(graph.SUPERVISOR_RULES_HEADER) < p.index(t) < p.index("SQL ROUTING")
    assert p.endswith("\n\n" + graph.SQL_ROUTING_RULE)


def test_saved_copy_of_the_old_default_keeps_one_copy_of_each_rule(monkeypatch):
    old = graph.DEFAULT_PROMPTS["supervisor"] + "\n" + "\n".join(TEXTS)
    p = composed_supervisor_prompt(monkeypatch, prompt=old)
    for t in TEXTS:
        assert p.count(t) == 1
    assert graph.SUPERVISOR_RULES_HEADER not in p       # nothing left to add


def _tools_without(*names):
    return [t for t in graph.DEFAULT_CONFIGS["supervisor"]["tools"] if t not in names]


def test_disabling_a_tool_drops_only_the_rules_that_name_it(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch, prompt="x", tools=_tools_without("propose_forecast"))
    assert "FORECASTS (Phase 13.1)" not in p and "TRACK THIS (Phase 13)" in p
    p = composed_supervisor_prompt(monkeypatch, prompt="x", tools=_tools_without("propose_kpi"))
    assert "MUST use propose_kpi" not in p and "TRACK THIS (Phase 13)" not in p
    assert "WATCH REQUESTS (Phase 12)" in p and "FORECASTS (Phase 13.1)" in p


def test_default_editable_prompt_holds_none_of_the_moved_rules():
    d = graph.DEFAULT_PROMPTS["supervisor"]
    for h in HEADERS:
        assert h not in d, h
    assert "MUST use propose_kpi" not in d
    assert "use create_watch instead of answering once" not in d
    assert graph.DEFAULT_CONFIGS["supervisor"]["prompt"] == d
    for h in HEADERS:      # the supervisor's own default editor text stays clean too
        assert h not in graph.DEFAULT_CONFIGS["supervisor"]["prompt"]


def test_no_rule_leaks_into_delegate_prompts(monkeypatch):
    seen = []

    def fake_cra(model, tool_list, prompt=None):
        seen.append(prompt)
        return object()
    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: object())
    try:
        graph._build({k: dict(graph.DEFAULT_CONFIGS[k]) for k in graph.DEFAULT_CONFIGS})
    finally:
        graph.reset_agents()
    with_rules = [s for s in seen if graph.SUPERVISOR_RULES_HEADER in s]
    assert len(with_rules) == 1          # only the supervisor


def test_rules_are_not_in_the_stored_config_text():
    assert graph.SUPERVISOR_RULES_HEADER not in graph.DEFAULT_CONFIGS["supervisor"]["prompt"]
