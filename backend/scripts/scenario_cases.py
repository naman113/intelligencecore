"""Quick 006 acceptance: the cases-from-findings lifecycle on a demo stack, mostly
without the LLM.

Runs INSIDE the stack's gateway container, against its own working-memory DB and the
synthetic demo business DB (read-only, through guardrails):

    docker exec ic-p15-app-1 python scripts/scenario_cases.py [--live-escalation]

It drives the real sentinel check, the real case code and the real close path:
  1. a critical watch fire opens a case;
  2. a second fire attaches to the same case;
  3. the escalation's findings become an investigation note (stubbed unless
     --live-escalation, which runs the real supervisor: one LLM call);
  4. N normal checks in a row close it ("resolved: back to normal") with a summary,
     an in-app notice and a memory distill (the real distill_case; it reports what
     it returned);
  5. every warning or critical insight gets its own case (title = the insight title,
     the finding, evidence SQL and sample as its first note); the same subject, named
     by the agent (same_as_case), joins that case and raises its severity;
  6. an info insight opens no case (Insights page only); the one-off split gives each
     warning or critical note on an old shared case its own case and archives the
     shared case (run through scripts/split_insight_cases.py, dry run first, then for
     real, then again to show it is idempotent);
  7. open_case (the chat tool, called directly) opens a chat case;
  8. pausing a watch through the HTTP route closes its case;
  9. one more alert is left open and unseen, for the gold card on Overview.
Firing and recovery are forced by editing the watch's threshold (and, for a
re-fire on the static demo data, its last reading) in the working-memory DB; the
business DB is never written. The scenario watches have a
one-day cadence, so the gateway's own sentinel loop leaves them alone.

Refuses to run unless IC_STACK names a test stack (never int, never a client).
Exit code: 0 all passed, 1 a check failed.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

PASSED, FAILED = [], []


def check(ok: bool, name: str, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))


def info(msg: str) -> None:
    print("INFO " + msg)


async def main(live: bool) -> int:
    stack = os.environ.get("IC_STACK", "")
    if not stack or stack == "int" or not stack.startswith("p"):
        sys.exit("refusing: IC_STACK must name a test stack (p1..p99), never int or a client")

    from app import findingcases, insights, memory_pipeline, sentinel
    from app.agents import graph
    from app.db import get_pool

    pool = await get_pool()
    tag = time.strftime("%H%M%S")
    distills: list = []
    real_distill = memory_pipeline.distill_case

    async def distill(case_id):
        ok = await real_distill(case_id)
        distills.append((case_id, ok))
        return ok
    memory_pipeline.distill_case = distill

    real_run = graph.run
    runs = {"live": 0}

    async def stub_run(history, message, context_text="", **kw):
        # --live-escalation: only the first escalation is real (one LLM call).
        if live and runs["live"] == 0:
            runs["live"] += 1
            return await real_run(history, message, context_text, **kw)
        return ("Customers grew past the alert line because the demo data holds every "
                "customer ever created (stubbed escalation; no LLM call).")
    graph.run = stub_run
    sentinel._escalation_times = []

    async def new_watch(title, value):
        trig = {"type": "threshold", "column": "customers", "op": ">", "value": value,
                "value_column": "customers"}
        return await pool.fetchval(
            """INSERT INTO watches (thread_id, kind, title, condition, cadence_seconds,
                                    compiled_sql, trigger_rule, last_checked_at, origin)
               VALUES ('ui:owner', 'kpi', $1, 'customer count above a threshold', 86400,
                       'SELECT count(*)::int AS customers FROM customers', $2::jsonb, now(),
                       'scenario')
               RETURNING id""", title, json.dumps(trig))

    async def set_threshold(wid, value):
        await pool.execute(
            "UPDATE watches SET trigger_rule = jsonb_set(trigger_rule, '{value}', to_jsonb($2::numeric)) WHERE id = $1",
            wid, value)

    async def run_check(wid):
        w = dict(await pool.fetchrow("SELECT * FROM watches WHERE id = $1", wid))
        await sentinel._check_one(w)

    async def case_of(wid):
        return await pool.fetchrow(
            "SELECT * FROM cases WHERE origin_ref = $1 ORDER BY id DESC LIMIT 1", f"watch:{wid}")

    async def kinds(cid):
        return [r["note"] for r in await pool.fetch(
            "SELECT ce.note FROM case_events ce JOIN events e ON e.id = ce.event_id "
            "WHERE ce.case_id = $1 ORDER BY e.id", cid)]

    # 1. fire -> case
    w1 = await new_watch(f"Scenario {tag}: customer count above alert line", 0)
    await run_check(w1)
    c = await case_of(w1)
    check(c is not None and c["status"] == "open" and c["origin"] == "watch",
          "1 critical fire opens a case", str(dict(c) if c else None))
    cid = c["id"] if c else None
    info(f"watch #{w1} case #{cid}")
    msg = await pool.fetchval(
        "SELECT content FROM messages WHERE thread_id = 'ui:owner' AND content LIKE $1 ORDER BY id DESC LIMIT 1",
        f"%Opened case #{cid}%")
    check(bool(msg), "1 the alert in the owner's chat names the case")
    check(await pool.fetchval("SELECT case_id FROM watch_events WHERE watch_id = $1", w1) == cid,
          "1 watch event links to the case")

    # 3. investigation note (escalation ran inside the fire)
    ks = await kinds(cid)
    check("investigation" in ks, "3 escalation findings become an investigation note",
          f"notes={ks}")

    # 2. normal once, then fire again -> same case. The demo DB is static, so a fire
    # needs a below-the-line previous reading: sentinel fires on the transition.
    await set_threshold(w1, 1e9)
    await run_check(w1)
    check(await pool.fetchval("SELECT normal_streak FROM watches WHERE id = $1", w1) == 1,
          "2 a normal check counts towards recovery")
    await set_threshold(w1, 0)
    await pool.execute("UPDATE watches SET last_state = '{\"customers\": 0}'::jsonb WHERE id = $1", w1)
    await run_check(w1)
    c2 = await case_of(w1)
    n_cases = await pool.fetchval("SELECT count(*) FROM cases WHERE origin_ref = $1", f"watch:{w1}")
    check(c2["id"] == cid and n_cases == 1 and (await kinds(cid)).count("watch") == 2,
          "2 second fire attaches to the same case", f"cases={n_cases} notes={await kinds(cid)}")

    # 4. three normal checks -> closed
    await set_threshold(w1, 1e9)
    need = findingcases.recovery_checks()
    for i in range(need):
        await run_check(w1)
        st = (await case_of(w1))["status"]
        if i < need - 1:
            check(st == "open", f"4 still open after {i + 1} normal check(s)")
    c4 = await case_of(w1)
    check(c4["status"] == "closed" and c4["close_reason"] == findingcases.RESOLVED_REASON,
          f"4 {need} normal checks close it with the reason", f"{c4['status']} {c4['close_reason']}")
    info(f"summary: {c4['summary']}")
    notice = await pool.fetchval(
        "SELECT content FROM messages WHERE thread_id = 'ui:owner' AND content LIKE '%Back to normal%' "
        "ORDER BY id DESC LIMIT 1")
    check(bool(notice) and f"case #{cid}" in notice, "4 back-to-normal notice in the owner's chat")
    d = [x for x in distills if x[0] == cid]
    check(bool(d), "4 memory distill ran on close", str(distills))
    info(f"distill_case returned {d[0][1] if d else None} (False = no OpenAI key or graph down; fail-soft)")

    # 5/6. insights (owner change: one case per warning or critical insight)
    ev = {"row_count": 3, "columns": ["region", "customers"],
          "sample": [{"region": "South", "customers": 0}, {"region": "North", "customers": 4}],
          "truncated": False, "relative_dates": False}
    extra = {"summary": "No new customers in the South for 30 days.",
             "impact": "The South was a third of new business.",
             "next_step": "Ask the South rep what changed.",
             "key_figures": [{"label": "New South customers, 30 days", "value": "0"},
                             {"label": "Monthly average before", "value": "4"}]}
    t5 = f"Scenario {tag}: new customers stopped in the South"
    e5, c5, why5 = await insights.record_note(
        t5, "No new South customers for 30 days, against about 4 a month before. "
        "Caveat: two sign-ups may still be in review.", "SELECT count(*) FROM customers",
        "critical", ev, extra=extra)
    r5 = await pool.fetchrow("SELECT origin, title, links FROM cases WHERE id = $1", c5)
    first5 = await pool.fetchval(
        """SELECT e.content FROM case_events ce JOIN events e ON e.id = ce.event_id
            WHERE ce.case_id = $1 ORDER BY e.id LIMIT 1""", c5)
    links5 = json.loads(r5["links"]) if r5 and isinstance(r5["links"], str) else dict(r5["links"] or {})
    check(r5["origin"] == "insight" and r5["title"] == t5
          and links5.get("insight_key") == f"note:{e5}" and links5.get("severity") == "critical"
          and all(x in (first5 or "") for x in ("No new South customers", "Evidence SQL:",
                                                 "SELECT count(*) FROM customers", "region=South")),
          "5 critical insight opens its own case (title, finding, SQL, sample, link back)",
          f"case {c5} {dict(r5) if r5 else None} {why5}")
    t5b = f"Scenario {tag}: late payers rose to 14"
    e5b, c5b, why5b = await insights.record_note(
        t5b, "14 customers paid late in October, up from 9.", "SELECT count(*) FROM customers",
        "warning", ev)
    o5b = await pool.fetchrow("SELECT origin, links FROM cases WHERE id = $1", c5b)
    check(o5b["origin"] == "insight" and c5b != c5, "5b warning insight opens its own case",
          f"case {c5b} {dict(o5b)} {why5b}")
    e5c, c5c, why5c = await insights.record_note(
        f"Scenario {tag}: late payers now 19", "19 customers paid late, up from 14.",
        "SELECT count(*) FROM customers", "critical", ev, same_as_case=c5b)
    sev5c = json.loads((await pool.fetchval("SELECT links FROM cases WHERE id = $1", c5b)) or "{}")
    check(c5c == c5b and sev5c.get("severity") == "critical",
          "5c same subject (same_as_case) joins the open case and raises it to critical",
          f"case {c5c} vs {c5b} {sev5c} {why5c}")
    e6, c6, why6 = await insights.record_note(
        f"Scenario {tag}: average order holds at 13 units", "Steady for six months.",
        "SELECT count(*) FROM customers", "info", ev)
    o6 = await pool.fetchval("SELECT origin FROM cases WHERE id = $1", c6)
    listed = {i["id"]: i for i in (await insights.list_insights(origin="exploration"))["items"]}
    item6 = listed.get(f"note:{e6}") or {}
    from app import main as gateway
    visible = {r["id"] for r in await gateway.api_list_cases()}
    check(o6 == "exploration" and item6.get("case_id") is None and c6 not in visible
          and c5 in visible and listed.get(f"note:{e5}", {}).get("summary") == extra["summary"],
          "6 info insight opens no case (Insights page only; the log case is hidden)",
          f"log case {c6} origin {o6} item {item6.get('case_id')} {why6}")

    # 6b. the one-off split, on a shared case shaped like the ones before Quick 006
    import subprocess
    legacy = await pool.fetchval(
        """INSERT INTO cases (title, opened_reason)
           VALUES ('Insights from data exploration', 'exploration') RETURNING id""")
    seeded = {}
    from app import cases_util
    async with pool.acquire() as conn:
        async with conn.transaction():
            for title, sev in ((f"Scenario {tag}: fuel drops on barge 3", "critical"),
                               (f"Scenario {tag}: diesel price up 6%", "warning"),
                               (f"Scenario {tag}: two suppliers paid twice", "warning"),
                               (f"Scenario {tag}: basket size steady", "info")):
                meta = {"origin": "exploration", "title": title, "severity": sev,
                        "finding": f"{title}.", "evidence_sql": "SELECT 1",
                        "evidence": {"row_count": 1, "columns": ["n"], "sample": [{"n": 1}]}}
                seeded[title] = await cases_util.add_case_note(
                    conn, legacy, f"{title}{chr(10)}{chr(10)}{title}.", kind="exploration", meta=meta)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def split(*args):
        out = subprocess.run([sys.executable, "scripts/split_insight_cases.py", *args],
                             cwd=here, capture_output=True, text=True, timeout=120)
        info(f"split_insight_cases.py {' '.join(args)}:{chr(10)}{out.stdout.strip()}")
        return json.loads(out.stdout.strip().splitlines()[-1])

    dry = split("--dry-run")
    dry_mine = [c for c in dry["carriers"] if c["case"] == legacy]
    still = await pool.fetchval("SELECT status FROM cases WHERE id = $1", legacy)
    check(bool(dry_mine) and len(dry_mine[0]["split"]) == 3 and still == "open",
          "6b dry run names 3 findings to split and changes nothing", str(dry_mine))
    real = split()
    mine = [c for c in real["carriers"] if c["case"] == legacy]
    new_cases = {x["title"]: x["case"] for x in (mine[0]["split"] if mine else [])}
    shapes = [await pool.fetchrow("SELECT origin, status, title FROM cases WHERE id = $1", cid)
              for cid in new_cases.values()]
    old = await pool.fetchrow("SELECT status, close_reason FROM cases WHERE id = $1", legacy)
    check(len(new_cases) == 3 and all(r["origin"] == "insight" and r["status"] == "open" for r in shapes)
          and old["status"] == "closed" and old["close_reason"] == findingcases.SPLIT_REASON,
          "6b split: 3 new insight cases, the shared case archived",
          f"{new_cases} {dict(old)}")
    again = split()
    check(not [c for c in again["carriers"] if c["case"] == legacy],
          "6b split is idempotent (a second run finds nothing to do)", str(again["carriers"]))
    info_item = {i["title"]: i for i in (await insights.list_insights(origin="exploration"))["items"]}
    check(info_item.get(f"Scenario {tag}: basket size steady", {}).get("case_id") is None
          and info_item.get(f"Scenario {tag}: fuel drops on barge 3", {}).get("case_id")
          == new_cases.get(f"Scenario {tag}: fuel drops on barge 3"),
          "6b after the split the Insights page links each finding to its own case")

    # 7. open_case from chat (tool called directly; the live Ask is separate)
    from app.agents.graph import ctx_phone
    from app import casetools
    tok = ctx_phone.set("ui:owner")
    try:
        out = await casetools.open_case.ainvoke({
            "title": f"Scenario {tag}: 16 bulk order lines far above normal",
            "finding": "16 order lines of 400 to 978 units against a typical 13 (IQR fence at 3x).",
            "evidence_sql": "SELECT count(*) FROM order_lines WHERE qty > 400",
            "watch_id": w1})
    finally:
        ctx_phone.reset(tok)
    check(out.startswith("case #") and "opened" in out, "7 open_case opens a chat case", out)

    # 8. pausing a watch through the route closes its case
    w2 = await new_watch(f"Scenario {tag}: second alert, then paused", 0)
    await run_check(w2)
    c8 = await case_of(w2)
    req = urllib.request.Request(
        f"http://127.0.0.1:8080/api/watches/{w2}", method="PATCH",
        data=json.dumps({"status": "paused"}).encode(),
        headers={"x-ops-key": os.environ.get("OPS_KEY", ""), "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    c8b = await pool.fetchrow("SELECT status, close_reason FROM cases WHERE id = $1", c8["id"])
    check(c8b["status"] == "closed" and "paused" in (c8b["close_reason"] or ""),
          "8 pausing the watch closes its case", f"{dict(c8b)} {body}")

    # 9. one alert left open and unseen (the gold card)
    w3 = await new_watch(f"Scenario {tag}: customer count still above the line", 0)
    await run_check(w3)
    c9 = await case_of(w3)
    check(c9 is not None and c9["status"] == "open" and c9["seen_at"] is None,
          "9 an open unseen alert case is left for the gold card")

    print(json.dumps({"stack": stack, "watch_closed": w1, "case_closed": cid, "insight_case": c5,
                      "warning_case": c5b, "info_log_case": c6, "split_cases": new_cases, "chat": out, "paused_case": c8["id"],
                      "open_alert_case": c9["id"] if c9 else None, "distills": distills,
                      "live_escalation": live}, default=str))
    print(f"scenario_cases: {len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live-escalation", action="store_true",
                    help="run the real supervisor escalation (one LLM call)")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.live_escalation)))
