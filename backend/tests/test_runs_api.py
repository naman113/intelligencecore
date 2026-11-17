"""Quick 008: the Runs control center API (runsapi.py). Pure: derivation of
agent-run kinds and titles, job titles, filters, the merged feed and paging,
and "running now" including work in flight that has no row yet."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import jobruns, runsapi

T0 = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


def _run(**kw):
    row = {"id": 11, "thread_id": "ui:owner", "status": "done", "result_summary": "ok",
           "error": None, "created_at": T0, "started_at": T0,
           "ended_at": T0 + timedelta(seconds=42), "playbook_id": None, "case_id": None,
           "playbook_name": None, "playbook_trigger": None, "thread_title": None,
           "chat_subject": None, "person_name": None, "message": "How did sales do?",
           "kind": "chat"}
    row.update(kw)
    return row


def _job(**kw):
    row = {"id": 5, "kind": "watch_check", "name": "Stale orders", "trigger": "every 15 min",
           "status": "done", "summary": "stale = 3", "error": None, "counts": '{"value": 3}',
           "ref_type": "watch", "ref_id": "7", "task_run_id": None, "trace_id": None,
           "routine": True, "repeats": 4, "duration_ms": 30, "started_at": T0,
           "ended_at": T0, "last_at": T0 + timedelta(hours=1), "dashboard_title": None,
           "dashboard_slug": None}
    row.update(kw)
    return row


# ── agent runs ────────────────────────────────────────────────────────────

def test_the_nightly_exploration_says_what_it_is():
    it = runsapi.agent_item(_run(kind="playbook", thread_id="playbook-global", playbook_id=3,
                                 playbook_name="explore_data", message="playbook run: ...",
                                 playbook_trigger=json.dumps({"type": "cron", "hour": 2})))
    assert it["title"] == "Data exploration (playbook)"
    assert it["trigger"] == "schedule (daily 02:00)"
    assert it["family"] == "playbooks" and it["kind_label"] == "Playbook"
    assert (it["ref_type"], it["ref_id"], it["question"]) == ("playbook", "3", None)


def test_unknown_playbooks_are_humanized_and_followups_say_so():
    it = runsapi.agent_item(_run(kind="playbook", thread_id="case-4", case_id=4,
                                 playbook_name="weekly_stock_check",
                                 playbook_trigger={"type": "expectation_violated"}))
    assert it["title"] == "Weekly stock check (playbook)"
    assert it["trigger"] == "overdue follow-up" and it["case_id"] == 4


def test_chat_runs_carry_the_chat_title_and_id():
    named = runsapi.agent_item(_run(thread_id="ui:owner:abc", thread_title="Fuel costs"))
    assert named["title"] == "Chat: Fuel costs" and named["chat"] == "ui:owner:abc"
    assert named["question"] == "How did sales do?" and named["trigger"] == "chat"
    first = runsapi.agent_item(_run(thread_id="ui:owner:abc"),
                               {"ui:owner:abc": "Which depot had the most late orders last week?"})
    assert first["title"] == "Chat: Which depot had the most late orders last week?"
    assert runsapi.agent_item(_run())["title"] == "Chat: Main chat"
    assert runsapi.agent_item(_run(duration_ms=None))["duration_ms"] == 42000


@pytest.mark.parametrize("row,title,trigger", [
    (dict(kind="upload", thread_id="dashboard", message="[document received: inv.pdf] Extract"),
     "Upload: inv.pdf", "upload"),
    (dict(kind="inbox", thread_id="inbox", message="[document received: po.pdf] Extract"),
     "Inbox: po.pdf", "inbox"),
    (dict(kind="whatsapp", thread_id="1203@g.us", chat_subject="Accounts team"),
     "WhatsApp: Accounts team", "WhatsApp"),
    (dict(kind="whatsapp", thread_id="919800000000", person_name="Ravi"),
     "WhatsApp: Ravi", "WhatsApp"),
    (dict(kind="whatsapp", thread_id="919800000000"), "WhatsApp: 919800000000", "WhatsApp"),
    (dict(kind="test", thread_id="test-local"), "Console test", "console"),
])
def test_other_agent_kinds(row, title, trigger):
    it = runsapi.agent_item(_run(**row))
    assert (it["title"], it["trigger"]) == (title, trigger)


def test_agent_kind_sql_covers_every_entry_point():
    sql = runsapi.AGENT_KIND_SQL
    for needle in ("'playbook'", "'inbox'", "'dashboard'", "'[document received: '", "'ui:'",
                   "'whatsapp'", "'test'"):
        assert needle in sql


# ── job runs ──────────────────────────────────────────────────────────────

def test_job_titles():
    assert runsapi.job_item(_job())["title"] == "Watch check: Stale orders"
    dash = runsapi.job_item(_job(kind="dashboard_refresh", name="", ref_type="dashboard",
                                 ref_id="4", dashboard_title="Sales", dashboard_slug="sales"))
    assert dash["title"] == "Dashboard refresh: Sales" and dash["dashboard_slug"] == "sales"
    gone = runsapi.job_item(_job(kind="dashboard_refresh", name="", ref_type="dashboard", ref_id="4"))
    assert gone["title"] == "Dashboard refresh: dashboard 4"
    assert runsapi.job_item(_job(kind="playbook_dispatch", name="explore_data"))["title"] == \
        "Scheduled: Data exploration"
    assert runsapi.job_item(_job(kind="loop", name="Watch sweep"))["title"] == "Loop error: Watch sweep"
    assert runsapi.job_item(_job(kind="forecast_v2", name="x"))["family"] == "other"


def test_forecast_rows_have_a_label_family_and_link():
    # Quick 013: the nightly forecast step on Runs
    it = runsapi.job_item(_job(kind="forecast", name="Weekly revenue", ref_type="forecast",
                               ref_id="3", trigger="nightly",
                               counts='{"definitions": 1, "ok": 1, "failed": 0}'))
    assert it["title"] == "Forecast: Weekly revenue" and it["kind_label"] == "Forecast"
    assert it["family"] == "forecasts" and it["counts"]["definitions"] == 1
    assert runsapi.parse_kinds("forecasts") == ["forecast"]
    assert runsapi.parse_ref("forecast:3") == ("forecast", "3")


async def test_forecast_ref_links_to_the_forecasts_page(monkeypatch):
    from app import forecasts
    rows = {}

    async def name_status(fid):
        assert fid == 3
        return rows.get("r")
    monkeypatch.setattr(forecasts, "name_status", name_status)
    item = {"ref_type": "forecast", "ref_id": "3"}
    rows["r"] = {"name": "Weekly revenue", "status": "active"}
    link = await runsapi._ref_link(None, item)
    assert link == {"type": "forecast", "id": "3", "label": "Weekly revenue",
                    "to": "/forecasts/3"}
    rows["r"] = {"name": "Old", "status": "retired"}
    assert (await runsapi._ref_link(None, item))["label"] == "Old (retired)"
    rows["r"] = None
    assert (await runsapi._ref_link(None, item))["to"] is None


def test_job_item_decodes_counts_and_keeps_repeats():
    it = runsapi.job_item(_job())
    assert it["counts"] == {"value": 3} and it["repeats"] == 4 and it["uid"] == "job-5"
    assert it["last_at"] == (T0 + timedelta(hours=1)).isoformat()


# ── filters ───────────────────────────────────────────────────────────────

def test_kinds_expand_families_and_reject_junk():
    assert runsapi.parse_kinds("watches") == ["watch_check", "watch_investigation"]
    assert runsapi.parse_kinds("chat,dashboards") == ["chat", "dashboard_refresh"]
    assert runsapi.parse_kinds("") is None
    with pytest.raises(HTTPException):
        runsapi.parse_kinds("drop table")


def test_refs():
    assert runsapi.parse_ref("playbook:explore_data") == ("playbook", "explore_data")
    assert runsapi.parse_ref("watch:12") == ("watch", "12")
    for bad in ("watch", "user:1", "watch:1;drop", "dashboard:"):
        with pytest.raises(HTTPException):
            runsapi.parse_ref(bad)


def test_which_source_a_filter_reaches():
    assert runsapi.agent_where(["watch_check"], None, None, None, None) is None
    assert runsapi.job_where(["chat"], None, None, None, None) is None
    assert runsapi.job_where(None, "queued", None, None, None) is None
    assert runsapi.agent_where(None, None, None, ("watch", "1"), None) is None
    where, args = runsapi.agent_where(None, None, None, ("playbook", "explore_data"), None)
    assert "p.name = $1" in where and args == ["explore_data"]
    where, args = runsapi.job_where(None, "failed", None, ("playbook", "explore_data"), None, [3])
    assert args == ["failed", "playbook", ["explore_data", "3"]]
    where, args = runsapi.agent_where(["chat", "watch_check"], "running", T0, None, T0)
    assert args == [["chat"], "running", T0, T0] and where.count("$") == 4


def test_before_and_status_validation():
    assert runsapi.parse_before("2026-10-26T02:00:00Z") == T0
    with pytest.raises(HTTPException):
        runsapi.parse_before("yesterday")


def test_merge_is_newest_first_with_a_cursor():
    a = [{"uid": f"run-{i}", "at": (T0 + timedelta(minutes=i)).isoformat()} for i in (5, 3)]
    j = [{"uid": f"job-{i}", "at": (T0 + timedelta(minutes=i)).isoformat()} for i in (4, 1)]
    items, nxt = runsapi.merge(a, j, 3)
    assert [i["uid"] for i in items] == ["run-5", "job-4", "run-3"]
    assert nxt == items[-1]["at"]
    items, nxt = runsapi.merge(a[:1], j[:1], 3)
    assert len(items) == 2 and nxt is None


# ── routes through a fake pool ────────────────────────────────────────────

class Pool:
    def __init__(self, runs=(), jobs=(), counts=None):
        self.runs, self.jobs = list(runs), list(jobs)
        self.counts = counts or {"queued": 1, "agent_failed": 2, "agent_done": 3,
                                 "job_failed": 4, "job_done": 5}
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        if "FROM task_runs r" in sql:
            return list(self.runs)
        if "FROM job_runs j" in sql:
            return list(self.jobs)
        if "FROM messages" in sql:
            return []
        if "FROM tool_calls" in sql:
            return [{"task_run_id": 11, "name": "forecasting", "first_id": 1},
                    {"task_run_id": 11, "name": "driver-analysis", "first_id": 4}]
        if "FROM playbooks" in sql:
            return [{"id": 3}]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        if "count(*)" in sql:
            return self.counts
        if "FROM task_runs r" in sql:
            return self.runs[0] if self.runs else None
        if "FROM job_runs j" in sql:
            return self.jobs[0] if self.jobs else None
        if "FROM watches" in sql:
            return {"id": 7, "kind": "kpi", "title": "Stale orders"}
        raise AssertionError(sql)


@pytest.fixture
def wire(monkeypatch):
    def install(pool):
        async def gp():
            return pool
        monkeypatch.setattr(runsapi, "get_pool", gp)
        return pool
    return install


async def test_feed_merges_both_sources(wire):
    pool = wire(Pool(runs=[_run(created_at=T0 + timedelta(minutes=2))], jobs=[_job()]))
    out = await runsapi.activity(limit=50)
    assert [i["uid"] for i in out["items"]] == ["run-11", "job-5"]
    assert out["next_before"] is None
    assert out["items"][0]["skills_used"] == ["forecasting", "driver-analysis"]
    assert out["items"][1]["skills_used"] == []
    skills_sql = [s for s, a in pool.sql if "FROM tool_calls" in s][0]
    assert "tool = 'use_skill'" in skills_sql and "args->>'name'" in skills_sql


async def test_feed_ref_filter_resolves_a_playbook_name(wire):
    pool = wire(Pool())
    await runsapi.activity(ref="playbook:explore_data")
    job_sql = [a for s, a in pool.sql if "FROM job_runs j" in s][0]
    assert ["explore_data", "3"] in job_sql


async def test_feed_rejects_bad_filters(wire):
    wire(Pool())
    for kw in ({"status": "sleeping"}, {"since": "1y"}, {"kind": "Bad!"}):
        with pytest.raises(HTTPException):
            await runsapi.activity(**kw)


async def test_now_lists_running_work_including_in_flight_jobs(wire, monkeypatch):
    running = _run(status="running", ended_at=None)
    queued = _run(id=12, status="queued", started_at=None, ended_at=None)
    live_row = _job(id=9, status="running", kind="triage", name="3 new message(s)", ref_type="",
                    ref_id="")
    wire(Pool(runs=[running, queued], jobs=[live_row]))
    monkeypatch.setattr(jobruns, "active", lambda: [
        {"token": 1, "id": 9, "kind": "triage", "name": "3 new message(s)", "trigger": "",
         "ref_type": None, "ref_id": None, "live": True, "started_at": T0.isoformat()},
        {"token": 2, "id": None, "kind": "dashboard_refresh", "name": "", "trigger": "x",
         "ref_type": None, "ref_id": None, "live": False, "started_at": T0.isoformat()},
    ])
    out = await runsapi.activity_now()
    uids = sorted(i["uid"] for i in out["running"])
    assert uids == ["job-9", "live-2", "run-11"]
    assert [q["uid"] for q in out["queued"]] == ["run-12"]
    assert out["counts"] == {"running": 3, "queued": 1, "failed_24h": 6, "done_24h": 8}


def test_a_running_playbook_does_not_say_it_is_reading_a_question():
    from app import runstatus
    assert runsapi._step(999_999, "running", "playbook") == runsapi.PLAYBOOK_FIRST_STEP
    assert runsapi._step(999_999, "running", "chat") == runstatus.FIRST_STEP
    assert runsapi._step(999_999, "done", "chat") is None


async def test_job_detail_links_the_watch(wire):
    wire(Pool(jobs=[_job()]))
    out = await runsapi.activity_job(5)
    assert out["ref"] == {"type": "watch", "id": "7", "label": "Stale orders", "to": "/kpis/7"}


async def test_missing_rows_are_404(wire):
    wire(Pool())
    with pytest.raises(HTTPException) as e:
        await runsapi.activity_job(99)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):
        await runsapi.activity_run(99)


# Phase 17 (CONS-05): mode switches on System > Runs
def test_mode_switch_kind_is_labelled_in_intake_family():
    assert runsapi.JOB_KINDS["mode_switch"] == "Table mode switch"
    assert runsapi.FAMILY_OF["mode_switch"] == "intake"
    assert "mode_switch" in runsapi.FAMILIES["intake"] and "sync" in runsapi.FAMILIES["intake"]
