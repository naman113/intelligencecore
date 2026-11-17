"""Phase 13.1 acceptance: forecast definitions end to end on a demo stack (Northwind Supply).

DEMO STACKS ONLY. Tools mode (default, no LLM spend): propose a forecast through the
propose_forecast tool, approve it through the ops API, wait for the nightly step to run it
(zero LLM calls, counts of task_runs and tool_calls unchanged), chart it on a published
dashboard through the forecast query source, force a failure and a recovery, and check the
Forecasts page ships. Quick 013 adds: a daily definition with more than 3000 rows of history
proposes and runs with its trailing empty days zero-filled, a weekly proposal with daily
dates is refused, a run that crashes after the claim ends failed with the lease released,
and the forecast runs show on the Runs feed. With --live it also asks the agent at most two questions; a provider
refusal (usage limit, credit balance, no key) is reported SKIPPED, never FAILED.

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p19 uv run --frozen python scripts/scenario_forecast.py --secrets "<path to secrets.env>" [--live] [--keep] [--out <results.md>]

Exit codes: 0 pass, 1 fail, 2 refused or unhealthy. The ops key is printed as len=N only.
Refuses stacks p13 and p14 and any container outside ic-pN-*.
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scenario_dashboard as sd  # noqa: E402  (sibling module, stdlib only)

Fail = sd.Fail
LIMIT_RE = re.compile(r"usage limits|credit balance|insufficient_quota|rate_limit|"
                      r"no llm configured|not_configured", re.I)
FORBIDDEN_STACKS = {13, 14}
DAILY_SQL = ("SELECT o.order_date AS period, count(*)::numeric AS value FROM sales_orders o "
             "WHERE o.status <> 'cancelled' GROUP BY 1 ORDER BY 1")
STOCK_SQL = ("SELECT snapshot_date AS period, sum(on_hand)::numeric AS value "
             "FROM inventory_snapshots GROUP BY 1 ORDER BY 1")
LIVE_ASK_1 = "forecast daily orders 14 days ahead, every night"
FIRST_RUN_LIMIT = 180
# Quick 013: about 5400 daily rows (more than the old 3000-row cap), the last two days empty
LONG_DAILY_SQL = ("SELECT g::date AS period, (100 + extract(dow FROM g))::numeric AS value "
                  "FROM generate_series(DATE '2012-01-02', current_date - 3, INTERVAL '1 day') AS g")
CRASH_ERROR = "the forecast run hit an internal error: IsADirectoryError"


# ---------------------------------------------------------------- pure helpers

def stack_config(stack: str) -> dict:
    return sd.stack_config(stack)


def refusal(cfg, env):
    why = sd.refusal(cfg, env)
    if why:
        return why
    for key in ("app", "pg", "dashc"):
        m = re.fullmatch(r"ic-p(\d+)-[a-z-]+-1", cfg.get(key) or "")
        if not m:
            return f"{key} container {cfg.get(key)!r} is not an ic-pN-* test stack container"
        if int(m.group(1)) in FORBIDDEN_STACKS:
            return f"stack p{m.group(1)} belongs to another run"
    return None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_forecast_rows(rows, horizon) -> list:
    """Problems with the rows a forecast dashboard query serves; [] when valid."""
    if not rows:
        return ["no rows"]
    problems = []
    periods = [r.get("period") for r in rows]
    if any(not isinstance(p, str) for p in periods):
        return ["period missing"]
    if any(b <= a for a, b in zip(periods, periods[1:])):
        problems.append("periods not strictly increasing")
    hist = [r for r in rows if r.get("forecast") is None]
    fut = [r for r in rows if r.get("forecast") is not None]
    if not hist:
        problems.append("no history rows")
    for r in hist:
        if not _num(r.get("actual")):
            problems.append(f"history row {r.get('period')} has no actual")
        if r.get("low") is not None or r.get("high") is not None:
            problems.append(f"history row {r.get('period')} has a range")
    if len(fut) != horizon:
        problems.append(f"{len(fut)} future rows, expected {horizon}")
    for r in fut:
        f, lo, hi = r.get("forecast"), r.get("low"), r.get("high")
        if r.get("actual") is not None:
            problems.append(f"future row {r.get('period')} has an actual")
        if not (_num(f) and _num(lo) and _num(hi)) or not lo <= f <= hi:
            problems.append(f"future row {r.get('period')} range is not low <= forecast <= high")
    if hist and fut and max(r["period"] for r in hist) >= min(r["period"] for r in fut):
        problems.append("history overlaps the forecast")
    return problems[:8]


def check_detail(detail, horizon) -> list:
    """Problems with GET /api/forecasts/{id}; [] when valid."""
    problems = []
    fc = (detail or {}).get("forecast") or []
    if len(fc) != horizon:
        problems.append(f"{len(fc)} forecast points, expected {horizon}")
    for p in fc:
        m, lo, hi = p.get("mean"), p.get("low"), p.get("high")
        if not (_num(m) and _num(lo) and _num(hi)) or not lo <= m <= hi:
            problems.append(f"point {p.get('period')} is not low <= mean <= high")
            break
    if not (detail or {}).get("method"):
        problems.append("method missing")
    if not _num((detail or {}).get("wape")):
        problems.append("wape missing")
    return problems


def is_provider_refusal(text) -> bool:
    return bool(LIMIT_RE.search(str(text or "")))


def render_results(steps, stack, mode="tools") -> str:
    lines = [f"# scenario_forecast results ({stack}, {mode} mode)", "",
             "| Step | Requirement | Result | Detail |", "|---|---|---|---|"]
    for s in steps:
        detail = sd.redact(str(s.get("detail", ""))).replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {s['step']} | {s['req']} | {s['status']} | {detail} |")
    failed = [s for s in steps if s["status"] == "FAIL"]
    passed = [s for s in steps if s["status"] == "PASS"]
    ok = bool(passed) and not failed
    lines += ["", f"Verdict: {'PASS' if ok else 'FAIL'} scenario_forecast"]
    return "\n".join(lines) + "\n"


def forecast_page(fid: int) -> str:
    code, _ = sd.contract_example()
    start = code.index("data('invoiced_by_customer')")
    end = code.index("</script></body>")
    body = (
        "data('fc').then(d => {\n"
        "  const rows = d.rows || [];\n"
        "  document.getElementById('asof').textContent = 'As of ' + (d.refreshed_at || 'not refreshed yet')"
        " + (rows.length && rows[0].method ? ' - ' + rows[0].method + ', typical miss ' + "
        "Math.round(rows[0].wape || 0) + '%' : '');\n"
        "  new Chart(document.getElementById('c'), { type: 'line', data: {\n"
        "    labels: rows.map(r => r.period),\n"
        "    datasets: [\n"
        "      { label: 'Actual', data: rows.map(r => r.actual), borderColor: '#2B7FD4' },\n"
        "      { label: 'Forecast', data: rows.map(r => r.forecast), borderColor: '#5BA0E0', borderDash: [6, 4] },\n"
        "      { label: 'Low', data: rows.map(r => r.low), borderColor: 'transparent', pointRadius: 0 },\n"
        "      { label: 'High', data: rows.map(r => r.high), borderColor: 'transparent', pointRadius: 0,"
        " fill: '-1', backgroundColor: 'rgba(91,160,224,0.18)' }] } });\n"
        "}).catch(e => { const p = document.getElementById('asof'); p.className = 'owed'; p.textContent = e.message; });\n")
    page = code[:start] + body + code[end:]
    return page.replace("Invoiced by customer", f"Scenario forecast {fid}")


# ---------------------------------------------------------------- flow

def _prog(data: dict, body: str) -> str:
    return ("import asyncio, json\n"
            f"P = json.loads({json.dumps(json.dumps(data))})\n"
            "async def main():\n"
            "    from app.agents.graph import ctx_phone\n"
            "    from app import dashboardtools as dt, forecasttools as ft, db\n"
            "    ctx_phone.set(P['thread'])\n"
            "    try:\n" + body +
            "    finally:\n"
            "        await db.close_pool()\n"
            "asyncio.run(main())\n")


PROPOSE_BODY = (
    "        out = await ft.propose_forecast.ainvoke({k: P[k] for k in "
    "('name', 'sql', 'grain', 'horizon', 'season', 'kind')})\n"
    "        print('RESULT ' + json.dumps(out))\n"
    "        if P.get('again'):\n"
    "            out2 = await ft.propose_forecast.ainvoke({k: P[k] for k in "
    "('name', 'sql', 'grain', 'horizon', 'season', 'kind')})\n"
    "            print('RESULT2 ' + json.dumps(out2))\n")
DASH_BODY = (
    "        out = await dt.save_dashboard_version.ainvoke({k: P[k] for k in "
    "('slug', 'title', 'code', 'queries', 'change_note')})\n"
    "        print('RESULT ' + json.dumps(out))\n")


class Forecast(sd.Scenario):
    def __init__(self, cfg, live):
        super().__init__(cfg)
        self.live = live
        self.steps = []
        self.defs = {}           # name -> forecast id
        self.proposals = []      # action ids still pending
        self.skill_state = None
        self.op = sd.new_operator()
        self.thread = "ui:" + self.op
        self.letters = self.op.replace("scn-", "")

    def rec(self, step, req, status, detail=""):
        self.steps.append({"step": step, "req": req, "status": status, "detail": detail})
        print(f"{status} {step} {req} {sd.redact(str(detail))[:200]}", flush=True)

    def program(self, data, body):
        data = dict(data, thread=self.thread)
        r = self.sh(["docker", "exec", "-i", self.c["app"], "python", "-"], _prog(data, body))
        lines = {k: next((ln[len(k) + 1:] for ln in r.stdout.splitlines()
                          if ln.startswith(k + " ")), "") for k in ("RESULT", "RESULT2")}
        out = {k: (json.loads(v) if v else "") for k, v in lines.items()}
        out["err"] = r.stderr[-300:]
        return out

    def counts(self):
        return {t: int(self.psql(f"SELECT count(*) FROM {t}").strip() or 0)
                for t in ("task_runs", "tool_calls")}

    def forecasts(self):
        return self.get("/api/forecasts?status=all")

    def find(self, name):
        return next((f for f in self.forecasts() if f["name"] == name), None)

    def propose(self, name, sql, grain, horizon, season, kind, again=False):
        spec = {"name": name, "sql": sql, "grain": grain, "horizon": horizon,
                "season": season, "kind": kind, "again": again}
        out = self.program(spec, PROPOSE_BODY)
        m = re.search(r"action #(\d+)", str(out["RESULT"]))
        if not m:
            raise Fail(f"propose refused: {str(out['RESULT'] or out['err'])[:250]}")
        aid = int(m.group(1))
        self.proposals.append(aid)
        return aid, out

    def approve_action(self, aid):
        self.call("POST", f"/api/actions/{aid}/approve", {})
        self.proposals.remove(aid)

    def wait_until(self, name, cond, what, timeout=None):
        end = time.time() + (timeout or self.c["timeout"])
        f = None
        while time.time() < end:
            f = self.find(name)
            if f and cond(f):
                return f
            time.sleep(5)
        raise Fail(f"{what} not reached for {name!r}: {f}")

    def forecast_runs(self, fid):
        return self.get(f"/api/activity?kind=forecasts&ref=forecast:{fid}&limit=50")["items"]

    def wait_finished(self, name, after=None, timeout=None):
        """Poll until the definition shows a finished run (ok or error) newer than `after`."""
        end = time.time() + (timeout or self.c["timeout"])
        while time.time() < end:
            f = self.find(name)
            if f and f["last_run_at"] and (after is None or f["last_run_at"] > after) \
                    and (f["last_ok_at"] or f["last_error"]):
                return f
            time.sleep(5)
        raise Fail(f"no finished run for {name!r} in time")

    # -- steps
    def health(self):
        st, b, _ = self.raw("GET", self.c["base"] + "/health", timeout=20)
        degraded = bool(json.loads(b).get("degraded")) if st == 200 else True
        if st != 200 or degraded:
            return 2
        print(f"ops key len={len(self.key)}")
        self.base_counts = self.counts()
        self.rec("1", "FCST-01", "PASS", f"healthy; baseline {self.base_counts}")
        return 0

    def s_propose(self):
        self.name1 = f"Scenario daily orders {self.letters}"
        aid, out = self.propose(self.name1, DAILY_SQL, "day", 14, 7, "flow", again=True)
        self.a1 = aid
        act = next((a for a in self.get(
            "/api/actions?status=proposed&kind=create_forecast&limit=200") if a["id"] == aid), None)
        if act is None:
            raise Fail(f"action #{aid} not in proposed create_forecast list")
        p = self.payload(act)
        missing = [k for k in ("name", "sql", "grain", "horizon", "season", "kind", "name_key")
                   if k not in p]
        if missing:
            raise Fail(f"payload keys missing {missing}")
        again = str(out["RESULT2"])
        if not (again.startswith("REFUSED") or "already proposed" in again):
            raise Fail(f"duplicate proposal not refused: {again[:150]}")
        self.rec("2", "FCST-01", "PASS", f"one proposed create_forecast action #{aid}; "
                 "duplicate refused")

    def s_first_run(self):
        before = self.counts()
        self.t_approve = time.time()
        self.approve_action(self.a1)
        f = self.find(self.name1)
        if not f or f["status"] != "active":
            raise Fail(f"definition not active after approval: {f}")
        self.fid = f["id"]
        self.defs[self.name1] = self.fid
        self.rec("3", "FCST-01", "PASS", f"approved; definition #{self.fid} active")
        f = self.wait_finished(self.name1)
        secs = round(time.time() - self.t_approve)
        if f["last_error"] and not f["last_ok_at"]:
            raise Fail(f"first run failed: {f['last_error'][:200]}")
        d = self.get(f"/api/forecasts/{self.fid}")
        probs = check_detail(d, 14)
        if probs:
            raise Fail("; ".join(probs))
        after = self.counts()
        if after != before:
            kinds = self.psql("SELECT kind, count(*) FROM task_runs WHERE created_at > "
                              f"now() - interval '{secs + 60} seconds' GROUP BY 1")
            tools = self.psql("SELECT tool, count(*) FROM tool_calls WHERE created_at > "
                              f"now() - interval '{secs + 60} seconds' GROUP BY 1")
            raise Fail(f"counts moved {before} -> {after}; task_runs {kinds!r} tools {tools!r}")
        if secs > FIRST_RUN_LIMIT:
            raise Fail(f"first run took {secs}s (limit {FIRST_RUN_LIMIT}s)")
        listed = self.find(self.name1)
        self.rec("4", "FCST-02/03", "PASS",
                 f"first run in {secs}s: method={d['method']} wape={d['wape']:.3f} "
                 f"14 points low<=mean<=high; listed status={listed['status']} "
                 f"last_ok_at set; task_runs/tool_calls unchanged ({after})")
        self.original_sql = d["sql"]

    def s_level(self):
        self.name2 = f"Scenario stock {self.letters}"
        aid, _ = self.propose(self.name2, STOCK_SQL, "week", 8, 0, "level")
        self.approve_action(aid)
        f = self.find(self.name2)
        self.defs[self.name2] = f["id"]
        f = self.wait_finished(self.name2)
        if f["last_error"] and not f["last_ok_at"]:
            raise Fail(f"level run failed: {f['last_error'][:200]}")
        d = self.get(f"/api/forecasts/{f['id']}")
        probs = check_detail(d, 8)
        if probs:
            raise Fail("; ".join(probs))
        self.rec("5", "FCST-02", "PASS", f"level series run: method={d['method']} "
                 f"wape={d['wape']:.3f} 8 points")

    def s_long_daily(self):
        """Quick 013 WR-03 and WR-02."""
        self.name3 = f"Scenario long daily {self.letters}"
        aid, out = self.propose(self.name3, LONG_DAILY_SQL, "day", 14, 7, "flow")
        res = str(out["RESULT"])
        m = re.search(r"History: (\d+) complete periods", res)
        if not m or int(m.group(1)) < 1100:
            raise Fail(f"long history not proposed from its newest rows: {res[:200]}")
        self.approve_action(aid)
        f = self.find(self.name3)
        self.fid3 = f["id"]
        self.defs[self.name3] = self.fid3
        f = self.wait_finished(self.name3)
        if f["last_error"] and not f["last_ok_at"]:
            raise Fail(f"long daily run failed: {f['last_error'][:200]}")
        d = self.get(f"/api/forecasts/{self.fid3}")
        probs = check_detail(d, 14)
        if probs:
            raise Fail("; ".join(probs))
        acts, fc = d["actuals"], d["forecast"]
        last = acts[-1]
        nxt = (date.fromisoformat(last["period"]) + timedelta(days=1)).isoformat()
        if last["value"] != 0 or fc[0]["period"] != nxt:
            raise Fail(f"trailing empty days not zero-filled: last actual {last}, first point "
                       f"{fc[0]['period']}")
        if not any("counted as 0" in n for n in d["notes"]):
            raise Fail(f"no zero-fill note: {d['notes']}")
        zeros = sum(1 for a in acts if a["value"] == 0)
        self.rec("5b", "WR-02/03", "PASS",
                 f"~5400-row daily series proposed (history {m.group(1)} periods from the newest "
                 f"rows) and ran: method={d['method']}; {zeros} trailing empty days as 0, "
                 f"forecast starts {fc[0]['period']}")

    def s_misaligned(self):
        """Quick 013 WR-04: daily dates under a weekly grain are refused at propose time."""
        spec = {"name": f"Scenario misaligned {self.letters}", "sql": DAILY_SQL,
                "grain": "week", "horizon": 4, "season": 0, "kind": "flow"}
        out = self.program(spec, PROPOSE_BODY)
        res = str(out["RESULT"] or out["err"])
        m = re.search(r"action #(\d+)", res)
        if m:
            self.proposals.append(int(m.group(1)))
        if not res.startswith("REFUSED: periods must be the start of each week"):
            raise Fail(f"misaligned proposal not refused: {res[:200]}")
        self.rec("5c", "WR-04", "PASS", res[:170])

    def s_dashboard(self):
        slug = f"scenario-fc-{self.letters}"
        data = {"slug": slug, "title": f"Scenario forecast {self.fid}",
                "code": forecast_page(self.fid), "change_note": "scenario forecast panel",
                "queries": [{"id": "fc", "source": "forecast", "forecast_id": self.fid,
                             "refresh_minutes": 60}]}
        out = self.program(data, DASH_BODY)
        m = re.search(r"action #(\d+)", str(out["RESULT"]))
        if not m:
            raise Fail(f"dashboard draft refused: {str(out['RESULT'] or out['err'])[:300]}")
        aid = int(m.group(1))
        self.dash_slug = slug
        act = next((a for a in self.get(
            "/api/actions?status=proposed&kind=publish_dashboard&limit=200")
            if a["id"] == aid), None)
        if act is None:
            raise Fail("publish_dashboard proposal not found")
        self.call("POST", f"/api/actions/{aid}/approve", {})
        d = self.get(f"/api/dashboards/{slug}")["dashboard"]
        view = d["view_token"]
        end = time.time() + 120
        body = {}
        while time.time() < end:
            st, b, h = self.dget(f"/d/{slug}/data/fc?t={view}")
            body = json.loads(b) if st == 200 and b else {}
            if body.get("refreshed_at"):
                break
            time.sleep(3)
        if not body.get("refreshed_at"):
            raise Fail("forecast query never refreshed on the dashboard")
        if body.get("error"):
            raise Fail(f"forecast query error: {body['error']}")
        probs = check_forecast_rows(body.get("rows"), 14)
        if probs:
            raise Fail("; ".join(probs))
        self.rec("6", "FCST-04", "PASS",
                 f"/d/{slug}/data/fc served {len(body['rows'])} rows (history actual, "
                 "14 future forecast/low/high) refreshed by the loop without an Ask")

    def s_failure(self):
        fid, name = self.fid, self.name1
        prev = self.find(name)
        before_detail = self.get(f"/api/forecasts/{fid}")
        self.psql("UPDATE forecast_defs SET sql = 'SELECT 1 AS period, 2 AS value', "
                  f"next_run_at = now() WHERE id = {fid}")
        f = self.wait_finished(name, after=prev["last_run_at"])
        if not f["last_error"] or not f["failing_since"]:
            raise Fail(f"failure not recorded: {f['last_error']!r} {f['failing_since']!r}")
        if f["status"] != "active":
            raise Fail(f"status became {f['status']}")
        after_detail = self.get(f"/api/forecasts/{fid}")
        if after_detail["forecast"] != before_detail["forecast"] or not after_detail["forecast"]:
            raise Fail("previous run's points were not kept")
        first_since, first_run = f["failing_since"], f["last_run_at"]
        self.psql(f"UPDATE forecast_defs SET next_run_at = now() WHERE id = {fid}")
        f2 = self.wait_finished(name, after=first_run)
        if not f2["last_error"] or f2["failing_since"] != first_since:
            raise Fail(f"failing_since moved: {first_since} -> {f2['failing_since']}")
        orig = self.original_sql.replace("'", "''")
        self.psql(f"UPDATE forecast_defs SET sql = '{orig}', next_run_at = now() WHERE id = {fid}")
        end = time.time() + self.c["timeout"]
        f3 = None
        while time.time() < end:
            f3 = self.find(name)
            if f3 and not f3["last_error"] and not f3["failing_since"] \
                    and f3["last_run_at"] > f2["last_run_at"]:
                break
            time.sleep(5)
        else:
            raise Fail(f"did not recover: {f3}")
        self.rec("7", "FCST-02", "PASS",
                 "invalid SQL set last_error and failing_since, kept the previous points, "
                 "failing_since unchanged on a second failure, both cleared after the restore")

    def s_crash(self):
        """Quick 013 WR-01: an exception after the claim ends in a recorded failure with the
        lease released; the previous points are kept; the run shows failed on Runs."""
        fid, name = self.fid3, self.name3
        prev = self.find(name)
        before = self.get(f"/api/forecasts/{fid}")
        r = self.sh(["docker", "exec", self.c["app"], "python", "-c",
                     f"from app import sandbox; print(sandbox.scratch_dir('forecast_{fid}'))"])
        wd = (r.stdout.strip().splitlines() or [""])[-1]
        if not wd.startswith("/"):
            raise Fail(f"scratch dir not found: {r.stderr[-200:]}")
        blocker = f"{wd}/series.csv"
        r = self.sh(["docker", "exec", self.c["app"], "sh", "-c",
                     f"rm -f '{blocker}' && mkdir '{blocker}'"])
        if r.returncode:
            raise Fail(f"could not block series.csv: {r.stderr[-200:]}")
        try:
            self.psql(f"UPDATE forecast_defs SET next_run_at = now() WHERE id = {fid}")
            f = self.wait_until(name, lambda x: x["last_run_at"] > prev["last_run_at"]
                                and x["last_error"], "a recorded crash")
        finally:
            self.sh(["docker", "exec", self.c["app"], "rmdir", blocker])
        if f["last_error"] != CRASH_ERROR or not f["failing_since"]:
            raise Fail(f"crash not recorded as a failure: {f['last_error']!r} {f['failing_since']!r}")
        lease = self.psql(f"SELECT extract(epoch FROM next_run_at - now())::int FROM forecast_defs "
                          f"WHERE id = {fid}").strip()
        if int(lease or 0) < 2 * 3600:
            raise Fail(f"lease not released to the next nightly slot: next run in {lease}s")
        after = self.get(f"/api/forecasts/{fid}")
        if after["forecast"] != before["forecast"] or not after["forecast"]:
            raise Fail("previous points not kept after the crash")
        runs = self.forecast_runs(fid)
        bad = next((i for i in runs if i["status"] == "failed"), None)
        want = f"forecast '{name}' (#{fid}): {CRASH_ERROR}"
        if not bad or bad.get("error") != want or bad.get("title") != f"Forecast: {name}":
            raise Fail(f"no failed Runs row naming the definition: {runs[:2]}")
        job = self.get(f"/api/activity/jobs/{bad['id']}")
        if (job.get("ref") or {}).get("to") != f"/forecasts/{fid}":
            raise Fail(f"Runs row does not link to the forecast: {job.get('ref')}")
        crash_run = f["last_run_at"]
        self.psql(f"UPDATE forecast_defs SET next_run_at = now() WHERE id = {fid}")
        self.wait_until(name, lambda x: x["last_run_at"] > crash_run and not x["last_error"]
                        and not x["failing_since"], "recovery after the crash")
        self.rec("7b", "WR-01", "PASS",
                 f"crash after the claim recorded ({CRASH_ERROR}), next run in {int(lease) // 3600} h "
                 f"(not 30 min), previous points kept, Runs row failed naming the definition and "
                 f"linking /forecasts/{fid}; recovered on the next run")

    def s_runs(self):
        """Quick 013: forecast runs on the Runs feed with a readable label and counts."""
        runs = self.forecast_runs(self.fid)
        done = [i for i in runs if i["status"] == "done"]
        failed = [i for i in runs if i["status"] == "failed"]
        if not done or not failed:
            raise Fail(f"expected done and failed forecast runs for #{self.fid}: {runs[:3]}")
        ok = done[0]
        if ok["kind"] != "forecast" or ok["kind_label"] != "Forecast" \
                or ok["family"] != "forecasts" or ok["title"] != f"Forecast: {self.name1}":
            raise Fail(f"Runs row label wrong: {ok}")
        c = ok.get("counts") or {}
        if c.get("definitions") != 1 or c.get("ok") != 1 or c.get("failed") != 0 \
                or not str(ok.get("summary") or "").startswith("Forecast refreshed: 14 days ahead"):
            raise Fail(f"Runs row counts or summary wrong: {c} {ok.get('summary')!r}")
        every = self.get("/api/activity?kind=forecasts&limit=200")["items"]
        kinds = {i["kind"] for i in every}
        if kinds != {"forecast"}:
            raise Fail(f"forecasts filter returned other kinds: {kinds}")
        self.rec("7c", "Runs", "PASS",
                 f"{len(every)} forecast rows on /api/activity?kind=forecasts; '{ok['title']}' "
                 f"done counts={c} summary={ok['summary']!r}; failures name the definition "
                 f"({failed[0]['error'][:80]!r})")

    def s_ui(self):
        ui = self.c["ui"]
        st, b, _ = self.raw("GET", ui + "/forecasts", timeout=30)
        if st != 200:
            raise Fail(f"/forecasts -> HTTP {st}")
        html = b.decode("utf-8", "replace")
        todo = re.findall(r'(?:src|href)="(/assets/[^"]+\.js)"', html)
        seen, texts = set(), []
        while todo and len(seen) < 60:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            st, jb, _ = self.raw("GET", ui + path, timeout=30)
            if st == 200:
                t = jb.decode("utf-8", "replace")
                texts.append(t)
                todo += ["/" + m for m in re.findall(r'assets/[A-Za-z0-9_.-]+\.js', t)]
        blob = "\n".join(texts)
        missing = [s for s in ("Forecast failing", "Ask for a new forecast") if s not in blob]
        if missing:
            raise Fail(f"bundle lacks {missing}")
        self.rec("8", "FCST-05", "PASS", "/forecasts 200; bundle has 'Forecast failing' and "
                 "'Ask for a new forecast'")

    def ask(self, message, timeout):
        op = sd.new_operator()
        rid = self.call("POST", "/api/ask", {"message": message, "operator": op})[1]["run_id"]
        end = time.time() + timeout
        th = {}
        while time.time() < end:
            time.sleep(5)
            th = self.get(f"/api/ask?operator={op}")
            if not th.get("pending"):
                break
        run = th.get("last_run") or {}
        msgs = th.get("messages") or []
        reply = msgs[-1]["content"] if msgs else ""
        tools = [t.get("tool") or t.get("name") for t in self.get(f"/api/runs/{rid}/tools")]
        return op, run, reply, tools

    def s_live(self):
        if not self.live:
            self.rec("9", "FCST-01/05", "SKIPPED", "not requested (tools mode; pass --live)")
            return
        llm = self.get("/api/config/status").get("llm", {}).get("state")
        if llm == "not_configured":
            self.rec("9", "FCST-01/05", "SKIPPED", "no LLM key configured")
            return
        sk = self.get("/api/skills/forecasting")
        self.skill_state = bool(sk.get("enabled"))
        if not self.skill_state:
            self.call("PATCH", "/api/skills/forecasting", {"enabled": True})
        op, run, reply, tools = self.ask(LIVE_ASK_1, self.c["timeout"])
        err = str(run.get("error") or reply)
        if run.get("status") != "done" and is_provider_refusal(err):
            self.rec("9a", "FCST-01", "SKIPPED", f"provider refused: {err[:120]}")
            return
        if run.get("status") != "done":
            raise Fail(f"live run 1 ended {run.get('status')}: {err[:200]}")
        for a in self.get("/api/actions?status=proposed&kind=create_forecast&limit=200"):
            if self.payload(a).get("thread_id") == "ui:" + op:
                self.proposals.append(a["id"])
        if not ("propose_forecast" in tools and "propose_kpi" not in tools
                and "action #" in reply):
            raise Fail(f"live ask 1: tools={tools} reply={reply[:150]!r}")
        self.rec("9a", "FCST-01", "PASS", f"propose_forecast used, no propose_kpi; tools={tools}")
        op, run, reply, tools = self.ask(f"why did the {self.name1} forecast change?",
                                         self.c["timeout"])
        err = str(run.get("error") or reply)
        if run.get("status") != "done" and is_provider_refusal(err):
            self.rec("9b", "FCST-05", "SKIPPED", f"provider refused: {err[:120]}")
            return
        if "get_forecast" not in tools:
            raise Fail(f"live ask 2 did not call get_forecast: {tools}")
        self.rec("9b", "FCST-05", "PASS", f"get_forecast used; tools={tools}")

    def cleanup(self):
        for aid in list(self.proposals):
            try:
                self.raw("POST", f"{self.c['base']}/api/actions/{aid}/reject", {},
                         {"x-ops-key": self.key})
            except Exception as e:  # noqa: BLE001
                print("cleanup reject:", e)
        if self.skill_state is False:
            try:
                self.call("PATCH", "/api/skills/forecasting", {"enabled": False})
            except Fail as e:
                print("cleanup:", e)
        if self.c["keep"]:
            print("cleanup: scenario definitions kept (--keep)")
            return
        for fid in self.defs.values():
            try:
                self.call("PATCH", f"/api/forecasts/{fid}", {"status": "retired"})
            except Fail as e:
                print("cleanup:", e)
        print("cleanup: scenario definitions retired")


def run(cfg, live, out) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    s = Forecast(cfg, live)
    try:
        rc = s.health()
    except (urllib.error.URLError, Fail) as e:
        print(f"UNHEALTHY: {e}")
        return 2
    if rc:
        print("UNHEALTHY: /health not ok or degraded")
        return 2
    rc = 0
    name = "start"
    try:
        for fn in (s.s_propose, s.s_first_run, s.s_level, s.s_long_daily, s.s_misaligned,
                   s.s_dashboard, s.s_failure, s.s_crash, s.s_runs, s.s_ui, s.s_live):
            name = fn.__name__
            fn()
    except Fail as e:
        s.rec(name, "FCST", "FAIL", str(e))
        rc = 1
    finally:
        s.cleanup()
    text = render_results(s.steps, os.environ.get("STACK", "p19"),
                          "live" if live else "tools")
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text.splitlines()[-1] if text.strip() else "")
    return rc


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--secrets", help="file with an OPS_KEY= line (else env OPS_KEY)")
    ap.add_argument("--live", action="store_true", help="also ask the agent at most 2 questions")
    ap.add_argument("--keep", action="store_true", help="keep the scenario definitions")
    ap.add_argument("--out", help="write the results markdown here")
    args = ap.parse_args(argv)
    try:
        cfg = sd.load_config(os.environ, argv)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    cfg["timeout"] = int(os.environ.get("SCENARIO_TIMEOUT") or 240)
    why = refusal(cfg, os.environ)
    if why:
        print(f"REFUSED: {why}")
        return 2
    live = args.live and not cfg["no_llm"]
    return run(cfg, live, args.out)


if __name__ == "__main__":
    sys.exit(main())
