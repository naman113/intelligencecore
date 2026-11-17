"""Quick 036 (items 2-4): dashboard guard rails per agent run, enforced in tool code.

- Dashboard delegations per run are capped (dashboard_delegations_per_run, default 2).
- Preview rounds per run are capped (dashboard_preview_rounds, default 3).
- A clean preview returns a preview_id; save_dashboard_version(preview_id=...) stores
  exactly the previewed spec without the model sending it again; handles are per run.

No DB, LLM or network."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app import configservice, dashboards, dashguard, semanticmodel
from app import dashboardtools as dt
from app.agents import graph
from app.config import Settings

from tests.test_dashboard_spec_tools import (  # noqa: F401  (env is a fixture)
    env, measure_spec, preview, sql_spec)
from tests.test_quick036_cutoff import _build, _configs


@pytest.fixture
def guard():
    tok = dashguard.begin()
    yield dashguard.ctx_guard.get()
    dashguard.end(tok)


# ── 2. dashboard delegations per run ───────────────────────────────────────

async def test_third_dashboard_delegation_in_a_run_is_refused(monkeypatch, guard):
    b = _build(monkeypatch)
    for _ in range(2):
        assert await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"}) == "dashboard done"
    out = await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"})
    assert out == dashguard.DELEGATION_REFUSAL.format(n=2)
    assert out.startswith("REFUSED: the dashboard specialist already ran 2 times in this request")
    assert b.agents["dashboard"].calls == 2
    assert b.logged[-1]["refused"] is True and b.logged[-1]["result"] == out


async def test_delegation_cap_is_a_setting_and_per_run(monkeypatch):
    b = _build(monkeypatch)
    monkeypatch.setenv("DASHBOARD_DELEGATIONS_PER_RUN", "1")
    graph.get_settings.cache_clear()
    try:
        tok = dashguard.begin()
        assert await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"}) == "dashboard done"
        assert (await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"})).startswith(
            "REFUSED: the dashboard specialist already ran 1 times")
        dashguard.end(tok)
        tok = dashguard.begin()  # the next run starts fresh
        assert await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"}) == "dashboard done"
        dashguard.end(tok)
    finally:
        monkeypatch.delenv("DASHBOARD_DELEGATIONS_PER_RUN")
        graph.get_settings.cache_clear()


async def test_run_opens_a_fresh_guard_and_closes_it(monkeypatch):
    seen = []

    class _Sup:
        async def ainvoke(self, payload, config=None):
            seen.append(dashguard.ctx_guard.get())
            return {"messages": [AIMessage(content="ok")]}

    configs = _configs()

    async def eff():
        return configs

    monkeypatch.setattr(graph, "effective_configs", eff)
    monkeypatch.setattr(graph, "_supervisor", _Sup())
    monkeypatch.setattr(graph, "_built_with", configs)
    assert await graph.run([], "hi") == "ok"
    assert await graph.run([], "hi") == "ok"
    assert all(isinstance(g, dashguard.RunGuard) for g in seen) and seen[0] is not seen[1]
    assert dashguard.ctx_guard.get() is None


def test_settings_registered():
    s = Settings(_env_file=None)
    assert s.dashboard_delegations_per_run == 2 and s.dashboard_preview_rounds == 3
    for key, default in (("dashboard_delegations_per_run", 2), ("dashboard_preview_rounds", 3)):
        meta = configservice.REGISTRY[key]
        assert meta["group"] == "tuning" and not meta["secret"] and not meta["required"]
        assert meta["min"] == 1 and meta["min"] <= default <= meta["max"]
        assert "live" in meta["label"]


# ── 3. preview rounds per run ──────────────────────────────────────────────

async def test_fourth_preview_in_a_run_is_refused_without_sampling(env, guard):
    for left in (2, 1, 0):
        d = json.loads(await preview(sql_spec()))
        assert d["ok"] is True and d["preview_rounds_left"] == left
    sampled = len(env["samples"])
    out = await preview(sql_spec())
    pid = dashguard.latest_preview_id()
    assert out.startswith("PREVIEW LIMIT REACHED: this run has used its 3 preview rounds")
    assert f'preview_id "{pid}"' in out and "save_dashboard_version" in out and "stop" in out
    assert len(env["samples"]) == sampled, "a refused preview runs no query"
    assert env["logs"][-1]["refused"] is True and env["logs"][-1]["name"] == "preview_dashboard"


async def test_preview_cap_holds_inside_one_parallel_batch(env, guard):
    outs = await asyncio.gather(*(preview(sql_spec()) for _ in range(5)))
    assert sum(o.startswith("PREVIEW LIMIT REACHED") for o in outs) == 2


async def test_preview_outside_a_run_is_unchanged(env):
    for _ in range(5):
        d = json.loads(await preview(sql_spec()))
        assert d["ok"] is True and "preview_id" not in d and "preview_rounds_left" not in d


# ── 4. save from the preview handle, byte-identical ────────────────────────

async def save(**kw):
    a = {"slug": "sales", "change_note": "first cut"}
    a.update(kw)
    return await dt.save_dashboard_version.ainvoke(a)


async def test_save_by_preview_id_stores_exactly_the_preview(env, guard, monkeypatch):
    real = dashboards.prepare_spec
    prepared = []

    async def counting(spec):
        out = await real(spec)
        prepared.append(out)
        return out
    monkeypatch.setattr(dt.dashboards, "prepare_spec", counting)
    d = json.loads(await preview(sql_spec()))
    pid = d["preview_id"]
    assert pid.startswith("pv_") and pid in d["next"] and "save_dashboard_version" in d["next"]
    out = await save(preview_id=pid)
    assert "#41" in out and "NOT published" in out
    assert len(prepared) == 1, "save does not prepare the spec again"
    kw = env["save_kw"]
    assert json.dumps(kw["stored"], ensure_ascii=False) == json.dumps(
        prepared[0]["stored"], ensure_ascii=False)
    assert kw["queries"] == prepared[0]["queries"] and kw["title"] == "Open invoices"
    assert any("LIMIT 0" in s for s in env["samples"]), "the dry run still runs at save"
    log = env["logs"][-1]
    assert log["name"] == "save_dashboard_version" and log["args"]["preview_id"] == pid
    assert "spec_bytes" not in log["args"]


async def test_measure_sql_is_the_previewed_sql_even_if_the_model_changes(env, guard, monkeypatch):
    d = json.loads(await preview(measure_spec()))
    previewed_sql = [q["sql"] for q in dashguard.ctx_guard.get().preview["queries"]]

    async def boom():
        raise AssertionError("save must not recompile measures")
    monkeypatch.setattr(semanticmodel, "load_model", boom)
    await save(preview_id=d["preview_id"])
    assert [q["sql"] for q in env["save_kw"]["queries"]] == previewed_sql


async def test_only_the_latest_clean_preview_of_this_run_can_be_saved(env, guard):
    first = json.loads(await preview(sql_spec()))["preview_id"]
    second = json.loads(await preview(sql_spec("Other")))["preview_id"]
    assert first != second
    bad = sql_spec()
    bad["tiles"][0]["query"] = "missing"
    d = json.loads(await preview(bad))
    assert d["ok"] is False and "preview_id" not in d
    out = await save(preview_id=first)
    assert out.startswith("REFUSED:") and second in out
    assert env["conn"].events == []
    await save(preview_id=second)
    assert env["save_kw"]["title"] == "Other", "a failed preview never replaces the clean one"


async def test_preview_ids_never_cross_runs(env):
    tok = dashguard.begin()
    pid = json.loads(await preview(sql_spec()))["preview_id"]
    dashguard.end(tok)
    tok = dashguard.begin()
    out = await save(preview_id=pid)
    dashguard.end(tok)
    assert out.startswith("REFUSED:") and env["conn"].events == []
    out = await save(preview_id=pid)  # outside any run
    assert out.startswith("REFUSED:")


async def test_save_needs_exactly_one_of_spec_and_preview_id(env, guard):
    pid = json.loads(await preview(sql_spec()))["preview_id"]
    both = await save(preview_id=pid, spec=sql_spec())
    assert both.startswith("REFUSED:") and "not both" in both
    neither = await save()
    assert neither.startswith("REFUSED:") and "preview_id" in neither and "spec" in neither
    assert env["conn"].events == []
    await save(spec=sql_spec())  # the old call shape still saves
    assert env["save_kw"]["title"] == "Open invoices"


def test_save_signature_keeps_spec_and_puts_small_args_first():
    props = list(dt.save_dashboard_version.args)
    assert set(props) == {"slug", "change_note", "preview_id", "spec"}
    assert props.index("spec") == len(props) - 1, "the big argument is written last"
    req = dt.save_dashboard_version.tool_call_schema.model_json_schema().get("required", [])
    assert set(req) == {"slug", "change_note"}
    assert "preview_id" in dt.save_dashboard_version.description
    assert "preview_id" in dt.preview_dashboard.description


def test_prompt_step_six_saves_by_preview_id():
    p = graph.DEFAULT_PROMPTS["dashboard"]
    assert "save_dashboard_version(slug, change_note, preview_id)" in p
    assert "at most 3 preview rounds" in p
