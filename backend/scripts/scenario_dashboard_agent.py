# Phase 18: superseded for Phase 18+ stacks. The operator route is spec-only, so these HTML payloads can no longer be saved; spec_hostile_check.mjs and scenario_dashboard.py are the current proofs.
"""Phase 6 acceptance scenario for the dashboard specialist on a harness stack.

DEMO STACK ONLY: it reinstalls and enables the dashboard-authoring skill, seeds a paused
KPI watch with synthetic history in agent_memory, and approves a dashboard. It refuses
(exit 2) unless SCENARIO_DEMO_STACK=1 and BASE_URL and DASH_URL are loopback.

Run from backend/:  SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/scenario_dashboard_agent.py
Exit codes: 0 pass, 1 fail, 2 refused or unhealthy. SCENARIO_NO_LLM=1 runs only the
non-LLM steps. Tokens and the ops key are printed as len=N only.
Env: BASE_URL, DASH_URL, OPS_KEY, PG_CONTAINER, APP_CONTAINER, SCENARIO_TIMEOUT.
Phase 12: step 8b repeats the invoices ask on the used thread (AGENT-08); step 6 reads the sandboxed /frame (AGENT-07).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:9061")
DASH = os.getenv("DASH_URL", "http://127.0.0.1:9062")
PG = os.getenv("PG_CONTAINER", "ic-p6-postgres-1")
APP = os.getenv("APP_CONTAINER", "ic-p6-app-1")
TIMEOUT = int(os.getenv("SCENARIO_TIMEOUT", "900"))
SECRETS = "D:/Code repo/Intelligence corner/intelligence-corner/deploy/secrets.env"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = "dashboard-authoring"
CONTRACT_KEYS = ["_summary", "slug", "dashboard_id", "version_id", "version", "title",
                 "change_note", "path", "draft_url", "preview_expires_at", "from_version",
                 "is_new_dashboard", "queries_added", "queries_removed", "queries_changed",
                 "origin", "thread_id"]
LIB_HASH = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
INVOICE_ASK = "build a dashboard of open invoices by customer"
LEFT: dict = {}


class Fail(Exception):
    pass


def _key() -> str:
    k = os.getenv("OPS_KEY", "")
    if not k and os.path.exists(SECRETS):
        for line in open(SECRETS, encoding="utf-8"):
            if line.startswith("OPS_KEY="):
                k = line.split("=", 1)[1].strip()
    return k.strip("\r\n \"'")


KEY = _key()


def raw(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if body is not None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def call(method, path, body=None, ok=(200,)):
    st, b, _ = raw(method, BASE + path, body, {"x-ops-key": KEY})
    if st not in ok:
        raise Fail(f"{method} {path} -> HTTP {st}: {b[:200]!r}")
    try:
        return st, json.loads(b) if b else {}
    except ValueError:
        return st, {}


def get(path):
    return call("GET", path)[1]


def payload(a):
    p = a.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def ok(step, name):
    print(f"ok {step} {name}", flush=True)


def need(cond, step, name, detail=""):
    if not cond:
        raise Fail(f"{step} {name} {detail}")
    ok(step, name)


def dash_get(path):
    return raw("GET", DASH + path)


def psql(sql):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    r = subprocess.run(["docker", "exec", "-i", PG, "psql", "-U", "agent", "-d", "agent_memory",
                        "-v", "ON_ERROR_STOP=1", "-At"], input=sql, capture_output=True,
                       text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise Fail("psql: " + r.stderr[:300])
    return r.stdout


def max_run():
    return max([r["id"] for r in get("/api/runs?limit=50")] or [0])


def wait_run(snapshot):
    end = time.time() + TIMEOUT
    while time.time() < end:
        time.sleep(5)
        runs = [r for r in get("/api/runs?limit=50")
                if r.get("thread_id") == "test-local" and r["id"] > snapshot]
        if runs:
            run = max(runs, key=lambda r: r["id"])
            if run["status"] in ("done", "failed"):
                return run
    raise Fail("run did not finish in time")


def tool_names(run_id):
    return [t.get("tool") or t.get("name") for t in get(f"/api/runs/{run_id}/tools")]


def ask(message):
    """One chat ask; on a token-budget failure raise the budget (harness only) and retry once."""
    for attempt in (1, 2):
        snap = max_run()
        call("POST", "/api/test-message", {"message": message})
        run = wait_run(snap)
        if run["status"] == "done":
            return run
        err = str(run.get("error"))
        if attempt == 1 and "token budget" in err:
            print(f"BUDGET run {run['id']} failed: {err[:200]}; raising reactive_token_budget to 400000")
            call("PUT", "/api/config", {"values": {"reactive_token_budget": 400000}})
            LEFT["budget_raised"] = True
            continue
        raise Fail(f"run {run['id']} failed: {err[:300]}")
    raise Fail("unreachable")


def proposed(run_id):
    rows = get("/api/actions?status=proposed&limit=200")
    return sorted([a for a in rows if a.get("kind") == "publish_dashboard"
                   and a.get("task_run_id") == run_id], key=lambda a: a["id"])


def render(url):
    r = subprocess.run(["node", os.path.join(HERE, "dashboard_render_check.mjs"), url],
                       capture_output=True, text=True, encoding="utf-8")
    line = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        out = json.loads(line)
    except ValueError:
        out = {}
    return r.returncode, out


def poll_data(draft_url, qids, timeout=120):
    path, _, q = draft_url.partition("?")
    results = {}
    for qid in qids:
        end = time.time() + timeout
        while True:
            st, b, _ = dash_get(f"{path}data/{qid}?{q}")
            d = json.loads(b) if st == 200 and b else {}
            if d.get("refreshed_at"):
                results[qid] = d
                break
            if time.time() > end:
                raise Fail(f"data {qid} not refreshed (HTTP {st})")
            time.sleep(3)
    return results


def main():
    if not KEY:
        print("set OPS_KEY")
        return 2
    no_llm = os.getenv("SCENARIO_NO_LLM") == "1"
    # ---- 0 preflight
    for u in (BASE, DASH):
        if (urllib.parse.urlparse(u).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
            print(f"REFUSED: {u} is not loopback")
            return 2
    if os.getenv("SCENARIO_DEMO_STACK") != "1":
        print("REFUSED: set SCENARIO_DEMO_STACK=1 only on a harness stack")
        return 2
    try:
        st, _, _ = raw("GET", BASE + "/health", timeout=20)
    except urllib.error.URLError as e:
        print(f"stack unreachable: {e}")
        return 2
    if st != 200:
        print("stack unhealthy")
        return 2
    print(f"ops key len={len(KEY)}")
    llm = get("/api/config/status").get("llm", {}).get("state")
    if llm == "not_configured" and not no_llm:
        print("SKIP 0 llm not_configured: LLM steps skipped")
        no_llm = True
    ok("0", f"preflight llm={llm}")
    try:
        # ---- 1 skill
        raw("DELETE", BASE + f"/api/skills/{SKILL}", None, {"x-ops-key": KEY})
        call("POST", "/api/skills/install", {"id": f"intelligence-corner/{SKILL}"}, ok=(200, 201))
        call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
        need("kpi_history" in json.dumps(get(f"/api/skills/{SKILL}")), "1",
             "skill reinstalled with kpi_history guidance")
        if no_llm:
            return no_llm_steps()
        # ---- 1b clean slate: earlier runs leave published dashboards on the slug the agent
        # picks, which would make step 4 (still a draft) see a prior current_version.
        # Demo stack only (guarded by SCENARIO_DEMO_STACK above); cascades to versions/tokens.
        psql("DELETE FROM dashboards;")
        # W-6: the supervisor answers from chat history, so a prior invoices ask on the
        # test-local thread means no delegation. Start the scenario on a clean thread.
        psql("INSERT INTO threads (thread_id, phone) SELECT 'test-local-old', phone "
             "FROM threads WHERE thread_id = 'test-local' ON CONFLICT DO NOTHING; "
             "UPDATE messages SET thread_id = 'test-local-old' WHERE thread_id = 'test-local' "
             "AND EXISTS (SELECT 1 FROM threads WHERE thread_id = 'test-local-old'); "
             "UPDATE threads SET summary = NULL, messages_since_summary = 0 "
             "WHERE thread_id = 'test-local';")
        ok("1b", "leftover dashboards and test-local thread cleared")
        # ---- 2 invoices ask
        run = ask(INVOICE_ASK)
        names = tool_names(run["id"])
        print("   tools:", names, "preview rounds:", names.count("preview_dashboard"))
        need(all(n in names for n in ("delegate_to_dashboard_agent", "preview_dashboard",
                                      "save_dashboard_version")), "2",
             "specialist delegated, previewed and saved")
        # ---- 3 proposal
        props = proposed(run["id"])
        need(len(props) >= 1, "3", "publish_dashboard proposal", f"count={len(props)}")
        act = props[-1]
        p = payload(act)
        need(all(k in p for k in CONTRACT_KEYS), "3", "payload has every contract key",
             str([k for k in CONTRACT_KEYS if k not in p]))
        slug, ver = p["slug"], p["version"]
        m = re.fullmatch(rf"/d/{re.escape(slug)}/v/{ver}/\?p=([A-Za-z0-9_-]{{20,}})", p["draft_url"])
        need(m is not None and p["origin"] == "agent" and bool(p["queries_added"]), "3",
             "draft_url shape, origin, queries_added")
        ptoken = m.group(1)
        print(f"   slug={slug} v{ver} action={act['id']} preview token len={len(ptoken)}")
        # ---- 4 still a draft
        d = get(f"/api/dashboards/{slug}")
        v = get(f"/api/dashboards/{slug}/versions/{ver}")
        need(d["dashboard"].get("current_version") is None and v["status"] == "draft", "4",
             "still a draft")
        vtoken = d["dashboard"]["view_token"]
        # ---- 5 token hygiene
        hay = json.dumps(get(f"/api/runs/{run['id']}/tools")) + json.dumps(
            get("/api/threads/test-local/messages?limit=20"))
        logs = subprocess.run(["docker", "logs", APP, "--since", "30m"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        hay += logs.stdout + logs.stderr
        need(ptoken not in hay and vtoken not in hay, "5",
             "no preview or view token in tools, messages or logs")
        # ---- 6 draft opens
        st, _, hdr = dash_get(p["draft_url"])
        csp = {k.lower(): val for k, val in hdr.items()}.get("content-security-policy", "")
        need(st == 200 and "frame-src 'self'" in csp and "frame-ancestors 'none'" in csp, "6",
             "draft wrapper opens with frame-src 'self' CSP", f"status={st}")
        frame_url = p["draft_url"].replace("?", "frame?", 1)
        fst, _, fhdr = dash_get(frame_url)
        fcsp = {k.lower(): val for k, val in fhdr.items()}.get("content-security-policy", "")
        need(fst == 200 and "frame-ancestors 'self'" in fcsp and "sandbox allow-scripts" in fcsp
             and LIB_HASH in fcsp and "allow-same-origin" not in fcsp, "6",
             "sandboxed /frame CSP (opaque origin, per-version hash)", f"status={fst}")
        qids = [q["id"] for q in v["queries"]]
        data = poll_data(p["draft_url"], qids)
        need(all(x.get("error") is None and x.get("rows") for x in data.values()), "6",
             "draft data non-empty")
        # ---- 7 render
        rc, out = render(DASH + p["draft_url"])
        if rc == 3:
            print("SKIP 7 render: no browser")
        else:
            need(rc == 0 and bool(out.get("ok")), "7", "render has chart or table, no console errors",
                 json.dumps({k: out.get(k) for k in ("charts", "tableRows", "errors")}))
            print(f"   charts={out['charts']} tableRows={out['tableRows']} errors={out['errors']}")
        # ---- 8 approve race
        res = []

        def go():
            res.append(raw("POST", BASE + f"/api/actions/{act['id']}/approve", {},
                           {"x-ops-key": KEY}))
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        codes = sorted(r[0] for r in res)
        need(codes == [200, 409], "8", "approve race gives one 200 and one 409", str(codes))
        d = get(f"/api/dashboards/{slug}")
        need(d["dashboard"]["current_version"] == ver, "8", "current version is the approved draft")
        st, _, _ = dash_get(f"/d/{slug}/?t={vtoken}")
        need(st == 200, "8", "published page opens")
        LEFT["invoices"] = slug
        # ---- 8b repeat ask on the used thread (AGENT-08); thread deliberately NOT archived
        rrun = ask(INVOICE_ASK)
        rnames = tool_names(rrun["id"])
        print("   8b tools:", rnames)
        rprops = proposed(rrun["id"])
        rreply = ""
        msgs = get("/api/threads/test-local/messages?limit=5")
        for m_ in msgs:
            if m_.get("role") == "assistant":
                rreply = str(m_.get("content") or "")
        cited = [int(x) for x in re.findall(r"#(\d+)", rreply)]
        pending_ids = {a["id"] for a in get("/api/actions?status=proposed&limit=200")
                       if a.get("kind") == "publish_dashboard"}
        need(bool(rprops) or any(c in pending_ids for c in cited), "8b",
             "repeat ask yields a new proposal or names a pending action id",
             f"reply={rreply[:300]!r} tools={rnames}")
        for a in rprops:
            raw("POST", BASE + f"/api/actions/{a['id']}/reject", {}, {"x-ops-key": KEY})
        # ---- 9 KPI trend ask
        title = f"Open invoice total (scenario {run['id']})"
        wid = int(psql(
            "INSERT INTO watches (thread_id, kind, title, condition, status, trigger_rule) VALUES "
            f"('test-local', 'kpi', '{title}', 'Total value of unpaid invoices', 'paused', "
            "'{\"column\": \"open_total\"}') RETURNING id;").strip().splitlines()[0])
        LEFT["watch"] = wid
        psql("INSERT INTO metric_history (watch_id, observed_at, metric, value, observation) "
             f"SELECT {wid}, now() - make_interval(days => k) - interval '1 hour', 'open_total', "
             "100000 + k * 731 + (k % 7) * 1900, '{}'::jsonb FROM generate_series(0, 29) k;")
        krun = ask(f"Build a dashboard that charts the 30-day trend of KPI watch #{wid} ({title}).")
        knames = tool_names(krun["id"])
        print("   tools:", knames)
        need("delegate_to_dashboard_agent" in knames and "save_dashboard_version" in knames,
             "9", "KPI run delegated and saved")
        kprops = proposed(krun["id"])
        need(len(kprops) >= 1, "9", "KPI proposal exists")
        kact = kprops[-1]
        kp = payload(kact)
        kv = get(f"/api/dashboards/{kp['slug']}/versions/{kp['version']}")
        kq = [q for q in kv["queries"] if q.get("source") == "kpi_history"]
        need(bool(kq) and int(kq[0].get("watch_id")) == wid, "9",
             "kpi_history query references the seeded watch")
        # ---- 10 KPI draft data and render
        kd = poll_data(kp["draft_url"], [kq[0]["id"]])
        rows = kd[kq[0]["id"]]["rows"]
        need(len(rows) >= 28, "10", "kpi snapshot has at least 28 rows", f"rows={len(rows)}")
        rc, out = render(DASH + kp["draft_url"])
        if rc == 3:
            print("SKIP 10 render: no browser")
        else:
            need(rc == 0 and bool(out.get("ok")) and out.get("maxPoints", 0) >= 28, "10",
                 "KPI chart renders at least 28 points", json.dumps(out)[:300])
        LEFT["kpi"] = kp["slug"]
        # ---- 11 reject
        call("POST", f"/api/actions/{kact['id']}/reject", {})
        kv2 = get(f"/api/dashboards/{kp['slug']}/versions/{kp['version']}")
        kd2 = get(f"/api/dashboards/{kp['slug']}")
        st, _, _ = dash_get(kp["draft_url"])
        need(kv2["status"] == "draft" and st == 200
             and kd2["dashboard"].get("current_version") is None,
             "11", "rejected draft stays served, current version unchanged")
        # ---- 12 mint route
        _, b = call("POST", f"/api/dashboards/{kp['slug']}/versions/{kp['version']}/preview", {})
        need(b["url"] == b["path"] + "?p=" + b["preview_token"] and b["refreshing"] is True,
             "12", "mint route url and refreshing")
        need(dash_get(b["url"])[0] == 200, "12", "minted link opens")
        st, _, _ = raw("POST", BASE + f"/api/dashboards/{slug}/versions/{ver}/preview", {},
                       {"x-ops-key": KEY})
        need(st == 409, "12", "mint for a published version is 409", str(st))
        # ---- 13 budget log check
        logs = subprocess.run(["docker", "logs", APP, "--since", "60m"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        n = (logs.stdout + logs.stderr).lower().count("token budget exceeded")
        print(f"   'token budget exceeded' log lines in last 60m: {n} "
              f"(budget raised: {LEFT.get('budget_raised', False)})")
        ok("13", "budget log check")
        print("PASS dashboard agent scenario")
        return 0
    except Fail as e:
        print(f"FAIL {e}")
        return 1
    finally:
        # ---- 14 cleanup
        if LEFT.get("watch"):
            try:
                psql(f"DELETE FROM metric_history WHERE watch_id = {LEFT['watch']}; "
                     f"DELETE FROM watches WHERE id = {LEFT['watch']};")
            except Fail as e:
                print("cleanup:", e)
        print("left on stack, dashboards:",
              {k: v for k, v in LEFT.items() if k in ("invoices", "kpi", "operator")})


def no_llm_steps():
    for s in ("2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "13"):
        print(f"SKIP {s} llm steps skipped")
    text = open(os.path.join(HERE, "..", "..", "docs", "DASHBOARD_CONTRACT.md"),
                encoding="utf-8").read()
    code = re.search(r"```html\n(.*?)```", text, re.S).group(1)
    queries = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
    slug = "scenario-op-draft"
    _, r = call("POST", f"/api/dashboards/{slug}/versions",
                {"title": "Scenario operator draft", "code": code, "queries": queries,
                 "change_note": "scenario non-LLM draft"})
    LEFT["operator"] = slug
    # non-LLM variant of step 12 mint route
    _, b = call("POST", f"/api/dashboards/{slug}/versions/{r['version']}/preview", {})
    need(b["url"] == b["path"] + "?p=" + b["preview_token"], "12", "mint url shape")
    need(dash_get(b["url"])[0] == 200, "12", "minted link opens")
    print("   preview token len=%d" % len(b["preview_token"]))
    print("PASS dashboard agent scenario (LLM steps skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
