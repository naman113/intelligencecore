"""dashboard_digest tool, registry, superset and the seeded draft playbook (Phase 22, DCAP-16)."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import dashdigesttools as ddt
from app import guardrails
from app.agents import graph
from app import playbooks

SCHEMA = (Path(__file__).parent.parent / "app" / "schema.sql").read_text(encoding="utf-8")


def _form(title="Sales", stat=True):
    t = {"id": "t1", "type": "stat" if stat else "line", "title": "Revenue", "qid": "q1",
         "follow": "control", "horizon_days": 400, "presets_qid": None, "format": "number",
         "comparison": "previous_period", "column": "rev",
         "measures": [{"column": "rev", "kind": "sum"}]}
    return {"render_version": 2, "title": title, "number_locale": "en-US", "timezone": None,
            "date_control": {"default": "last_7_days", "presets": [], "horizon_days": 400,
                             "fy_start_month": 4},
            "sections": [{"title": "A", "tiles": ["t1"]}], "tiles": [t]}


def _rows():
    out, d = [], date(2025, 9, 30)
    end = datetime.now(timezone.utc).date()
    while d <= end:
        out.append({"metric_time__day": d.isoformat(), "rev": 10})
        d += timedelta(days=1)
    return out


class FakePool:
    def __init__(self, dashboards):
        self.dashboards = dashboards  # list of dict(slug,title,version_id,spec,queries)
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if sql == ddt._PUBLISHED_ONE:
            return [d for d in self.dashboards if d["slug"] == args[0]]
        if sql == ddt._PUBLISHED_ALL:
            return list(self.dashboards)
        if sql == ddt._SNAPSHOTS:
            return [{"query_id": "q1", "data": json.dumps(_rows()),
                     "data_as_of": datetime.now(timezone.utc), "day_timezone": "UTC",
                     "error": None, "refreshed_at": None}]
        raise AssertionError(sql)


@pytest.fixture
def env(monkeypatch):
    logged = []

    async def fake_log(name, args, result, start, refused=False):
        logged.append((name, refused))

    async def boom(*a, **k):
        raise AssertionError("business database touched")

    monkeypatch.setattr(ddt, "_log_tool", fake_log)
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom, raising=False)
    forms = {}
    monkeypatch.setattr(ddt.dashboardspec, "render_spec", lambda spec, q: forms[spec["k"]])

    def setup(rows):
        pool = FakePool([{"slug": s, "title": s, "version_id": i, "spec": json.dumps({"k": s}),
                          "queries": "[]"} for i, (s, f) in enumerate(rows)])
        for s, f in rows:
            forms[s] = f

        async def gp():
            return pool
        monkeypatch.setattr(ddt, "get_pool", gp)
        return pool, logged
    return setup


async def test_one_slug(env):
    pool, logged = env([("sales", _form())])
    out = await ddt.dashboard_digest.ainvoke({"slug": "sales"})
    assert out.startswith("Sales (Last 7 days")
    assert "- Revenue: 70 (no change vs previous 7 days)" in out
    assert logged == [("dashboard_digest", False)]


async def test_unknown_slug_and_no_stats(env):
    env([("sales", _form(stat=False))])
    assert (await ddt.dashboard_digest.ainvoke({"slug": "nope"})) == "NOT FOUND: no published dashboard nope"
    assert (await ddt.dashboard_digest.ainvoke({"slug": "sales"})).startswith("NOT FOUND")


async def test_no_slug_max_three(env):
    env([(f"d{i}", _form(title=f"D{i}")) for i in range(5)])
    out = await ddt.dashboard_digest.ainvoke({})
    assert [l for l in out.splitlines() if l.startswith("D")].__len__() == 3
    assert "D3" not in out


async def test_one_bad_dashboard_does_not_stop_the_others(env, monkeypatch):
    pool, _ = env([("bad", _form(title="Bad")), ("good", _form(title="Good"))])
    real = ddt.dashboardspec.render_spec

    def render(spec, q):
        if spec["k"] == "bad":
            raise KeyError("old stored spec")
        return real(spec, q)
    monkeypatch.setattr(ddt.dashboardspec, "render_spec", render)
    out = await ddt.dashboard_digest.ainvoke({})
    assert out.startswith("Good (") and not out.startswith("TOOL ERROR")


async def test_cap(env, monkeypatch):
    env([("sales", _form(title="X" * 5000))])
    out = await ddt.dashboard_digest.ainvoke({"slug": "sales"})
    assert len(out) < ddt.MAX_CHARS + 60 and "(cut:" in out


def test_registered_for_supervisor_and_playbooks():
    assert "dashboard_digest" in graph.AGENT_TOOL_SUPERSET["supervisor"]
    ts = graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert ts.index("dashboard_digest") < ts.index("discover_schema")
    assert "dashboard_digest" in playbooks._playbook_registry()
    assert "notify_owner" in playbooks._playbook_registry()


def test_tool_is_read_only_by_source():
    src = (Path(__file__).parent.parent / "app" / "dashdigesttools.py").read_text(encoding="utf-8")
    assert "run_readonly_sql" not in src and "_record_action" not in src and "INSERT" not in src
    assert len(ddt.dashboard_digest.description) < 400


def test_seed_block():
    m = re.search(r"-- ── Phase 22: dashboard digest playbook ──\n(.*?)-- ── End Phase 22: dashboard digest playbook ──",
                  SCHEMA, re.S)
    assert m
    b = m.group(1)
    assert "'dashboard_digest'" in b and "ARRAY['dashboard_digest','notify_owner']" in b
    assert "'notify', NULL, 'draft', 'system', NULL" in b and '"hour":8' in b
    assert "ON CONFLICT (name) DO NOTHING" in b
    assert "recursion_limit = 12, token_budget = 20000" in b
    assert "do not send anything" in b and "NOT FOUND or TOOL ERROR" in b and "single notify_owner call" in b
    # the 8 -> 12 bump rides on the goal re-key, so it never runs again on an owner-edited row
    assert "CASE WHEN recursion_limit = 8 THEN 12" in b
    assert not re.search(r"UPDATE playbooks SET recursion_limit = 12\s+WHERE", b)
    assert "goal = (" in b   # earlier shipped goal is re-keyed only while unedited
    assert SCHEMA.index("End Phase 22: dashboard-authoring for spec v2") < m.start()
