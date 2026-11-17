"""Phase 13.1 plan 05: propose_forecast / get_forecast, registry, supersets, prompts, labels."""

import json

import pytest
from _supprompt import composed_supervisor_prompt

from app import forecasts, forecasttools, runstatus
from app.agents import graph


@pytest.fixture
def logged(monkeypatch):
    calls = []

    async def fake(name, args, result, start, refused=False):
        calls.append((name, args, result, refused))
    monkeypatch.setattr(forecasttools, "_log_tool", fake)
    return calls


def test_registry_and_supersets():
    reg = graph._tool_registry()
    assert "propose_forecast" in reg and "get_forecast" in reg
    sup = graph.AGENT_TOOL_SUPERSET
    assert {"propose_forecast", "get_forecast"} <= set(sup["supervisor"])
    assert sup["supervisor"][-2:] == ["discover_schema", "propose_context_change"]
    assert sup["dashboard"] == ["search_business_context", "run_sql", "get_dashboard",
                                "preview_dashboard", "save_dashboard_version",
                                "get_kpi_history", "get_forecast", "get_model", "query_measure",
                                "use_skill", "skill_asset"]
    for k in ("data", "analysis", "document", "drawing", "context", "reflection"):
        if k in sup:
            assert "get_forecast" not in sup[k] and "propose_forecast" not in sup[k]
    cfg = graph.DEFAULT_CONFIGS
    assert {"propose_forecast", "get_forecast"} <= set(cfg["supervisor"]["tools"])
    assert cfg["dashboard"]["tools"] == sup["dashboard"]


def test_prompts(monkeypatch):
    sp = composed_supervisor_prompt(monkeypatch)
    assert "FORECASTS (Phase 13.1)" in sp and "never propose_kpi" in sp
    assert "get_forecast" in sp and "propose_forecast" in sp
    dp = graph.DEFAULT_PROMPTS["dashboard"]
    assert '"source": "forecast"' in dp and "get_forecast" in dp
    assert '"source": "forecast"' in graph.DEFAULT_CONFIGS["dashboard"]["prompt"]


def test_labels():
    assert runstatus.LABELS["propose_forecast"] == "Proposing the forecast for your approval"
    assert runstatus.LABELS["get_forecast"] == "Reading the forecast"
    assert runstatus.AFTER["get_forecast"] == "Reading the forecast"


def test_propose_description():
    d = forecasttools.propose_forecast.description
    low = d.lower()
    for need in ("period", "value", "one row per period", "no date window", "now()",
                 "90", "26", "12", "flow", "level", "awaiting owner approval"):
        assert need in low, need
    assert "7" in d and "52" in d
    assert "draft" not in low and "watch" not in low


@pytest.mark.asyncio
async def test_propose_forecast_calls_with_context(monkeypatch, logged):
    seen = {}

    async def prop(*a, **kw):
        seen.update(a=a, kw=kw)
        return "proposed forecast (action #5): x"
    monkeypatch.setattr(forecasts, "propose", prop)
    t1 = graph.ctx_phone.set("ui:owner")
    t2 = graph.ctx_run_id.set(7)
    try:
        out = await forecasttools.propose_forecast.ainvoke(
            {"name": "N", "sql": "select 1", "grain": "week", "horizon": 4,
             "season": 52, "kind": "flow", "rationale": "r"})
    finally:
        graph.ctx_phone.reset(t1)
        graph.ctx_run_id.reset(t2)
    assert out.startswith("proposed forecast")
    assert seen["a"] == ("N", "select 1", "week", 4, 52, "flow", "r")
    assert seen["kw"] == {"origin": "chat", "thread_id": "ui:owner", "run_id": 7}
    assert logged[0][0] == "propose_forecast" and logged[0][3] is False


@pytest.mark.asyncio
async def test_propose_refused_and_error(monkeypatch, logged):
    async def refuse(*a, **kw):
        return "REFUSED: nope"
    monkeypatch.setattr(forecasts, "propose", refuse)
    args = {"name": "N", "sql": "s", "grain": "week", "horizon": 4}
    assert (await forecasttools.propose_forecast.ainvoke(args)).startswith("REFUSED")
    assert logged[-1][3] is True

    async def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(forecasts, "propose", boom)
    out = await forecasttools.propose_forecast.ainvoke(args)
    assert out == "TOOL ERROR: RuntimeError: db down" and logged[-1][3] is False


@pytest.mark.asyncio
async def test_get_forecast_identical_to_route_json(monkeypatch, logged):
    body = {"forecast_id": 3, "name": "n", "forecast": [{"period": "2026-10-19", "mean": 1.0}]}

    async def d(i):
        return body
    monkeypatch.setattr(forecasts, "detail", d)
    out = await forecasttools.get_forecast.ainvoke({"forecast_id": 3})
    assert out == json.dumps(body) and logged[0][0] == "get_forecast"


@pytest.mark.asyncio
async def test_get_forecast_not_found_lists(monkeypatch, logged):
    async def d(i):
        return None

    async def lb(limit=50, include_retired=False):
        return [{"id": 1, "name": "A", "status": "active", "grain": "day", "horizon": 7,
                 "method": "ets"}]
    monkeypatch.setattr(forecasts, "detail", d)
    monkeypatch.setattr(forecasts, "list_brief", lb)
    out = await forecasttools.get_forecast.ainvoke({"forecast_id": 3})
    assert out.startswith("NOT FOUND: no forecast #3. Forecasts: ")
    assert json.loads(out.split("Forecasts: ", 1)[1]) == [
        {"id": 1, "name": "A", "status": "active", "grain": "day", "horizon": 7}]
