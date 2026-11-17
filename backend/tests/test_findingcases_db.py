"""Quick 006 real-Postgres coverage of cases from findings: a critical watch fire
opens one case, a repeat attaches to it, the escalation's findings become a note,
N normal checks close it through the one close path (summary, notice, distill, no
chain), stopped watches close their case, every warning or critical insight gets its own case (and the one-off split),
open_case opens or extends a chat case, and triage never lists any of them.

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the other real-Postgres
tests). Each test uses a throwaway database with schema.sql applied twice, so the
Quick 006 block is also checked for idempotency:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9153/agent_memory
"""
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
THRESH = {"type": "threshold", "column": "n", "op": ">", "value": 3, "value_column": "n"}


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def env(monkeypatch):
    import asyncpg

    from app import (actionsvc, casetools, db, findingcases, insights, kpihistory,
                     memory_pipeline, sentinel, triage)
    from app.agents import graph

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "q006_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    p = None
    state = {"rows": [], "distilled": [], "escalations": [], "chained": []}
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = await asyncpg.connect(url)
        await conn.execute(sql)
        await conn.execute(sql)
        await conn.close()
        p = await asyncpg.create_pool(url, min_size=1, max_size=6)

        async def gp():
            return p
        for mod in (db, findingcases, sentinel, triage, actionsvc, insights, casetools,
                    memory_pipeline, kpihistory):
            monkeypatch.setattr(mod, "get_pool", gp)

        async def run_sql(sql):
            return state["rows"]

        async def run(history, message, context_text="", **kw):
            state["escalations"].append(message)
            return "South region had no orders on Tuesday; check the feed."

        async def distill(case_id):
            state["distilled"].append(case_id)
            return True

        async def chain(pool, case_id):
            state["chained"].append(case_id)
        monkeypatch.setattr(sentinel, "run_readonly_sql", run_sql)
        monkeypatch.setattr(sentinel, "_escalation_times", [])
        monkeypatch.setattr(graph, "run", run)
        monkeypatch.setattr(memory_pipeline, "distill_case", distill)
        monkeypatch.setattr(triage, "_maybe_chain_case", chain)
        state["pool"] = p
        yield state
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _watch(pool, title="Open orders", trigger=THRESH, thread="ui:owner") -> int:
    return await pool.fetchval(
        """INSERT INTO watches (thread_id, kind, title, condition, cadence_seconds, compiled_sql,
                                trigger_rule, last_checked_at)
           VALUES ($1, 'kpi', $2, 'open orders above 3', 86400, 'SELECT 1 AS n', $3::jsonb, now())
           RETURNING id""", thread, title, json.dumps(trigger))


async def _check(env, wid, n):
    from app import sentinel
    env["rows"] = [{"n": n}]
    w = dict(await env["pool"].fetchrow("SELECT * FROM watches WHERE id = $1", wid))
    await sentinel._check_one(w)


async def _case(pool, cid):
    return dict(await pool.fetchrow("SELECT * FROM cases WHERE id = $1", cid))


async def _notes(pool, cid):
    return [dict(r) for r in await pool.fetch(
        """SELECT ce.note AS kind, e.content, e.provenance, e.triage_status FROM case_events ce
           JOIN events e ON e.id = ce.event_id WHERE ce.case_id = $1 ORDER BY e.id""", cid)]


async def test_watch_case_lifecycle(env):
    from app import findingcases, triage
    pool = env["pool"]
    wid = await _watch(pool)

    # 1. fire -> case opened, fire note, facts, event link, escalation note, alert names it
    await _check(env, wid, 5)
    cases = await pool.fetch("SELECT id FROM cases WHERE origin = 'watch'")
    assert len(cases) == 1
    cid = cases[0]["id"]
    c = await _case(pool, cid)
    assert (c["status"], c["origin"], c["origin_ref"], c["watch_id"]) == ("open", "watch", f"watch:{wid}", wid)
    assert c["origin_chat_jid"] == "ui:owner" and c["seen_at"] is None
    assert json.loads(c["links"])["watch_id"] == wid
    ev = await pool.fetchrow("SELECT case_id, escalated, severity FROM watch_events WHERE watch_id = $1", wid)
    assert (ev["case_id"], ev["escalated"], ev["severity"]) == (cid, True, "critical")
    notes = await _notes(pool, cid)
    assert [n["kind"] for n in notes] == ["watch", "investigation"]
    assert all(n["provenance"] == "self" and n["triage_status"] == "skipped" for n in notes)
    assert "South region had no orders" in notes[1]["content"]
    facts = {f["field"]: f["value"] for f in await pool.fetch(
        "SELECT field, value FROM case_facts WHERE case_id = $1 AND superseded_by IS NULL", cid)}
    assert facts == {"n": "5", "alert_rule": "n > 3"}
    msgs = [r["content"] for r in await pool.fetch(
        "SELECT content FROM messages WHERE thread_id = 'ui:owner' ORDER BY id")]
    assert any(f"Opened case #{cid}" in m for m in msgs)
    assert any(m.startswith("🔎 Investigation") for m in msgs)
    assert "Fired once" in c["summary"] and "Investigation:" in c["summary"]

    # The owner says "Got it": no longer owed.
    assert await findingcases.mark_seen(pool, cid) is True

    # 2. a normal check, then a second fire -> the SAME case, gold again
    await _check(env, wid, 2)
    assert await pool.fetchval("SELECT normal_streak FROM watches WHERE id = $1", wid) == 1
    await _check(env, wid, 6)
    assert await pool.fetchval("SELECT count(*) FROM cases WHERE origin = 'watch'") == 1
    c = await _case(pool, cid)
    assert c["seen_at"] is None and c["status"] == "open"
    assert await pool.fetchval("SELECT normal_streak FROM watches WHERE id = $1", wid) == 0
    assert [n["kind"] for n in await _notes(pool, cid)].count("watch") == 2
    assert "Fired 2 times" in c["summary"]

    # Triage never lists it; the stale sweep never closes it.
    index = await triage._open_cases_index(pool)
    assert cid not in [r["id"] for r in index]
    await pool.execute("UPDATE cases SET last_activity_at = now() - interval '90 days' WHERE id = $1", cid)
    await triage._close_stale_cases()
    assert (await _case(pool, cid))["status"] == "open"

    # 3. N normal checks in a row -> closed through the one close path
    for n in (1, 1):
        await _check(env, wid, n)
        assert (await _case(pool, cid))["status"] == "open"
    await _check(env, wid, 0)
    c = await _case(pool, cid)
    assert c["status"] == "closed" and c["close_reason"] == "resolved: back to normal"
    assert "Back to normal" in c["summary"] and "Fired 2 times" in c["summary"]
    assert (await _notes(pool, cid))[-1]["kind"] == "closing"
    assert env["distilled"] == [cid] and env["chained"] == []
    notice = await pool.fetchrow(
        "SELECT kind, status, payload FROM actions WHERE case_id = $1 ORDER BY id DESC LIMIT 1", cid)
    assert (notice["kind"], notice["status"]) == ("app_message", "executed")
    assert json.loads(notice["payload"])["close_kind"] == "resolved"
    last = await pool.fetchval("SELECT content FROM messages WHERE thread_id = 'ui:owner' ORDER BY id DESC LIMIT 1")
    assert last.startswith("✅ Back to normal")
    assert await pool.fetchval(
        "SELECT value FROM case_facts WHERE case_id = $1 AND field = 'n' AND superseded_by IS NULL", cid) == "0"

    # A later episode opens a NEW case (the old one is closed).
    await _check(env, wid, 9)
    assert await pool.fetchval("SELECT count(*) FROM cases WHERE origin = 'watch'") == 2


async def test_recovery_setting_is_live(env, monkeypatch):
    from app import findingcases
    pool = env["pool"]
    monkeypatch.setattr(findingcases, "get_settings",
                        lambda: types.SimpleNamespace(watch_case_recovery_checks=1, timezone="UTC"))
    wid = await _watch(pool)
    await _check(env, wid, 5)
    await _check(env, wid, 1)
    assert await pool.fetchval("SELECT status FROM cases WHERE watch_id = $1", wid) == "closed"


async def test_stopped_watches_close_their_case(env):
    from app import findingcases
    pool = env["pool"]
    paused, deleted = await _watch(pool, "Paused one"), await _watch(pool, "Deleted one")
    await _check(env, paused, 5)
    await _check(env, deleted, 5)
    await pool.execute("UPDATE watches SET status = 'paused' WHERE id = $1", paused)
    await pool.execute("DELETE FROM watches WHERE id = $1", deleted)   # FK sets watch_id NULL
    assert await findingcases.sweep_watch_cases() == 2
    rows = {r["origin_ref"]: r for r in await pool.fetch("SELECT origin_ref, status, close_reason FROM cases")}
    assert rows[f"watch:{paused}"]["close_reason"] == "closed: the watch was paused"
    assert rows[f"watch:{deleted}"]["close_reason"] == "closed: the watch was deleted"
    assert all(r["status"] == "closed" for r in rows.values())
    assert await findingcases.sweep_watch_cases() == 0
    # The route path: close_for_watch before a delete.
    other = await _watch(pool, "Route one")
    await _check(env, other, 5)
    assert await findingcases.close_for_watch(other, "done") == 1


async def test_warning_fire_opens_no_case(env):
    pool = env["pool"]
    wid = await _watch(pool, "Order count", trigger={"type": "increase", "column": "n"})
    await _check(env, wid, 1)
    await _check(env, wid, 2)   # increase -> warning fire
    assert await pool.fetchval("SELECT count(*) FROM watch_events WHERE watch_id = $1", wid) == 1
    assert await pool.fetchval("SELECT count(*) FROM cases") == 0


async def test_each_warning_or_critical_insight_is_its_own_case(env):
    from app import insights, main, triage
    pool = env["pool"]
    ev = {"row_count": 3, "columns": ["region", "n"], "sample": [{"region": "South", "n": 1}]}
    extra = {"summary": "South orders fell.", "impact": "Revenue risk.", "next_step": "Call the rep.",
             "key_figures": [{"label": "Orders in Oct", "value": "210"}]}
    e1, c1, _ = await insights.record_note("South orders fell 30% in October", "Fell from 300 to 210.",
                                           "SELECT 1", "critical", ev, extra=extra)
    e2, c2, _ = await insights.record_note("Average basket grew 4%", "Up from 50 to 52.",
                                           "SELECT 1", "info", ev)
    e3, c3, _ = await insights.record_note("Returns rose in week 41", "Up 12%.", "SELECT 1", "warning", ev)
    e4, c4, _ = await insights.record_note("Fuel drops on barge 3", "32 drops.", "SELECT 1", "warning", ev)
    assert len({c1, c2, c3, c4}) == 4
    own, info_log, warn = await _case(pool, c1), await _case(pool, c2), await _case(pool, c3)
    assert (own["origin"], own["title"]) == ("insight", "South orders fell 30% in October")
    assert json.loads(own["links"]) == {"insight_title": "South orders fell 30% in October",
                                        "severity": "critical", "insight_key": f"note:{e1}"}
    assert (warn["origin"], json.loads(warn["links"])["severity"]) == ("insight", "warning")
    assert info_log["origin"] == "exploration"
    first = await pool.fetchval("SELECT content FROM events WHERE id = $1", e1)
    for part in ("South orders fell 30% in October", "South orders fell.", "Why it matters: Revenue risk.",
                 "Next step: Call the rep.", "Key figures: Orders in Oct: 210", "Fell from 300 to 210.",
                 "Evidence SQL:", "Sample (1 of 3 rows):", "region=South, n=1"):
        assert part in first, part
    # Same subject: the agent names the open case; a bad number falls back to a new case.
    e5, c5, _ = await insights.record_note("Returns still rising in week 42", "Up 15%.", "SELECT 1",
                                           "critical", ev, same_as_case=c3)
    assert c5 == c3
    assert json.loads((await _case(pool, c3))["links"])["severity"] == "critical"
    e6, c6, _ = await insights.record_note("Cash dip on 3 Oct", "Low.", "SELECT 1", "warning", ev,
                                           same_as_case=c2)   # the info log is not an insight case
    assert c6 not in (c2, c3)
    # Dedupe: the same title again within 30 days is refused.
    e7, _, why = await insights.record_note("South orders fell 30% in October!", "again", "SELECT 1",
                                            "critical", ev)
    assert e7 is None and "already recorded" in why
    listed = await insights.list_insights(origin="exploration")
    by_note = {i["id"]: i for i in listed["items"]}
    assert by_note[f"note:{e2}"]["case_id"] is None       # info: Insights page only
    assert by_note[f"note:{e1}"]["case_id"] == c1 and by_note[f"note:{e1}"]["case_status"] == "open"
    assert by_note[f"note:{e1}"]["summary"] == "South orders fell."
    assert by_note[f"note:{e1}"]["key_figures"] == [{"label": "Orders in Oct", "value": "210"}]
    opened = await insights.list_insights(origin="exploration", case="open")
    assert f"note:{e2}" not in {i["id"] for i in opened["items"]}
    summary = await insights.insights_summary()
    assert summary["by_severity"] == {"info": 1, "warning": 3, "critical": 2}
    assert summary["open_cases"] == 4 and summary["last_run"] is None
    # The info log is hidden from Cases and from triage; insight cases are not listed by triage.
    listed_cases = {r["id"] for r in await main.api_list_cases()}
    assert c2 not in listed_cases and {c1, c3, c4, c6} <= listed_cases
    assert c2 in {r["id"] for r in await main.api_list_cases(include_exploration=True)}
    index = [r["id"] for r in await triage._open_cases_index(pool)]
    assert not {c1, c2, c3, c4} & set(index)


async def test_split_of_the_shared_exploration_case(env):
    from app import findingcases, insights
    pool = env["pool"]
    # Before Quick 006: every insight was a note on one shared case (origin NULL then).
    carrier = await pool.fetchval(
        "INSERT INTO cases (title, opened_reason) VALUES ('Insights from data exploration', 'exploration') RETURNING id")
    notes = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for title, sev in (("Fuel drops on barge 3", "critical"), ("Late payers up", "warning"),
                               ("Diesel price up", "warning"), ("Basket steady", "info")):
                meta = {"origin": "exploration", "title": title, "severity": sev, "finding": f"{title}.",
                        "evidence_sql": "SELECT 1", "evidence": {"row_count": 1, "sample": [{"n": 1}]}}
                notes[title] = await __import__("app.cases_util", fromlist=["x"]).add_case_note(
                    conn, carrier, f"{title}\n\n{title}.", kind="exploration", meta=meta)
    dry = await findingcases.split_exploration_cases(pool, dry_run=True)
    assert [len(c["split"]) for c in dry["carriers"]] == [3]
    assert await pool.fetchval("SELECT count(*) FROM cases WHERE origin = 'insight'") == 0
    report = await findingcases.split_exploration_cases(pool)
    (car,) = report["carriers"]
    assert car["case"] == carrier and car["archived"] is True and len(car["split"]) == 3
    new = {s["title"]: s["case"] for s in car["split"]}
    for title, cid in new.items():
        c = await _case(pool, cid)
        assert (c["origin"], c["title"], c["status"]) == ("insight", title, "open")
        kinds = await _notes(pool, cid)
        assert [k["kind"] for k in kinds] == ["exploration"]          # the same note, linked
        assert json.loads(c["links"])["insight_key"] == f"note:{notes[title]}"
    old = await _case(pool, carrier)
    assert old["status"] == "closed" and old["close_reason"] == findingcases.SPLIT_REASON
    assert [k["kind"] for k in await _notes(pool, carrier)] == ["exploration"] * 4 + ["closing"]
    assert await pool.fetchval("SELECT count(*) FROM actions") == 0     # no notice sent
    assert env["distilled"] == []
    # Idempotent.
    again = await findingcases.split_exploration_cases(pool)
    assert again["carriers"] == []
    # Listed once each, under its own case; the info one has no case.
    items = {i["title"]: i for i in (await insights.list_insights(origin="exploration"))["items"]}
    assert len(items) == 4
    assert items["Fuel drops on barge 3"]["case_id"] == new["Fuel drops on barge 3"]
    assert items["Basket steady"]["case_id"] is None
    got = await insights.get_insight(f"note:{notes['Late payers up']}")
    assert got["case_id"] == new["Late payers up"]
    ctx = await insights.exploration_context(include_kpis=False)
    assert ctx.count("Late payers up") == 2   # once already recorded, once as an open case
    assert "OPEN INSIGHT CASES" in ctx


async def test_open_case_tool_and_owner_close(env, monkeypatch):
    from app import casetools, main
    from app.agents.graph import ctx_phone

    async def log_tool(*a, **k):
        return None
    monkeypatch.setattr(casetools, "_log_tool", log_tool)
    pool = env["pool"]
    wid = await _watch(pool, "Daily revenue")
    tok = ctx_phone.set("ui:owner:abcdefghij")
    try:
        out = await casetools.open_case.ainvoke({
            "title": "Tuesday 9 Sep orders 30 percent low", "finding": "21 orders vs 30 expected.",
            "evidence_sql": "SELECT count(*) FROM sales_orders", "watch_id": wid,
            "dashboard_slug": "no-such-board"})
        again = await casetools.open_case.ainvoke({
            "title": "tuesday 9 sep orders: 30 percent LOW", "finding": "Confirmed by the feed."})
    finally:
        ctx_phone.reset(tok)
    assert out.startswith("case #") and "opened" in out and "dashboard 'no-such-board'" in out
    cid = int(out.split("#")[1].split()[0])
    assert again.startswith(f"case #{cid} with this title is already open")
    c = await _case(pool, cid)
    assert (c["origin"], c["origin_chat_jid"], c["watch_id"]) == ("chat", "ui:owner:abcdefghij", wid)
    assert json.loads(c["links"])["watch_id"] == wid
    assert [n["kind"] for n in await _notes(pool, cid)] == ["chat", "chat"]
    acts = await pool.fetch("SELECT kind, tier, status FROM actions WHERE case_id = $1", cid)
    assert [(a["kind"], a["tier"], a["status"]) for a in acts] == [("open_case", "user_command", "executed")] * 2

    # API shape for the UIs.
    listed = {r["id"]: r for r in await main.api_list_cases()}
    assert listed[cid]["origin"] == "chat" and isinstance(listed[cid]["links"], dict)
    detail = await main.api_case_detail(cid)
    assert detail["watch"]["id"] == wid and detail["watch"]["recovery_checks"] == 3
    assert [t["kind"] for t in detail["timeline"]] == ["chat", "chat"]

    # The owner closes it: same close path, no notice to themselves, distilled.
    assert await main.api_close_case(cid) == {"closed": cid}
    c = await _case(pool, cid)
    assert c["status"] == "closed" and c["close_reason"] == "closed by the owner"
    assert env["distilled"] == [cid] and env["chained"] == []
    assert await pool.fetchval(
        "SELECT count(*) FROM actions WHERE case_id = $1 AND kind IN ('app_message', 'whatsapp_message')", cid) == 0
