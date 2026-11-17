from _supprompt import composed_supervisor_prompt
"""Phase 13 (GOLIVE-08 gap): a plain 'track this' proposes one measure."""

from app.agents import graph


def test_propose_kpi_description_one_measure():
    d = " ".join(graph.propose_kpi.description.split())
    assert "window and threshold explicitly" not in d
    assert "ONE measure" in d and "only when the owner asked" in d


def test_supervisor_prompt_track_this_rule(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch)
    assert p.count("TRACK THIS (Phase 13)") == 1
    assert p.count("MUST use propose_kpi") == 1
    assert p.count("WATCH REQUESTS (Phase 12)") == 1
    assert p.count("DASHBOARD STATUS (Phase 12)") == 1


def test_propose_kpi_still_registered():
    assert "propose_kpi" in graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert graph._tool_registry()["propose_kpi"] is graph.propose_kpi
