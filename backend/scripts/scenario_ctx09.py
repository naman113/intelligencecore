"""Phase 12 live check for CTX-09 parts (a) and (d) on a demo stack.

DEMO STACK ONLY: asks the agent seven questions on ui:owner, then cleans up what it made.
Step 4 runs on the used ui:owner thread on purpose (12-VERIFICATION gap 2).
Refuses (exit 2) unless SCENARIO_DEMO_STACK=1 and BASE_URL is loopback.
Exit: 0 pass (or SKIP when no LLM key), 1 fail, 2 refused/unhealthy.
Run from backend/:  SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/scenario_ctx09.py [--secrets PATH]
The ops key comes from OPS_KEY or the secrets file and is never printed.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:9121").rstrip("/")
TIMEOUT = int(os.getenv("SCENARIO_TIMEOUT", "300"))
OPERATOR = "owner"
CLAIM_RES = [re.compile(r"(?i)\b(set up|created|added|scheduled)\b[^.!?\n]{0,60}\b(watch|alert)"),
             re.compile(r"(?i)\bi('ll| will) let you know")]


def reply_truthful(reply, created, history=()):
    """Quick 015: (truthful, cited ids) for a one-off watch reply. A cited watch id is one this
    turn created or one an earlier step of this run really created on the same thread (the
    model may refer back to it: history, not a claim). A creation claim needs a watch created
    in this turn, and when the reply claims one and quotes ids, one of them is that watch."""
    reply = reply or ""
    cited = [int(x) for x in re.findall(r"(?i)watch\s*#\s*(\d+)", reply)]
    claims = any(rx.search(reply) for rx in CLAIM_RES)
    known = set(created) | set(history)
    ok = (all(c in known for c in cited) and (not claims or bool(created))
          and (not (claims and cited) or bool(set(cited) & set(created))))
    return ok, cited


def run_digest(tools, run) -> str:
    """Quick 015: what a run did, for a failure line (a skipped tool, a refusal or an answer
    from history read differently)."""
    calls = "; ".join(f"{t.get('tool')}: {(t.get('result_digest') or '')[:120]}"
                      for t in tools or []) or "no tool calls"
    reply = ((run or {}).get("result_summary") or "")[:200].replace("\n", " ")
    return f"status={(run or {}).get('status')} tools=[{calls}] reply={reply!r}"


def _secret(name: str, path: str | None) -> str:
    v = os.getenv(name, "")
    if not v and path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.startswith(name + "="):
                v = line.split("=", 1)[1]
    return v.strip("\r\n \"'")


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-ops-key": KEY, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def fail(step, msg):
    print(f"FAIL {step} {msg}")
    sys.exit(1)


def ok(step, name):
    print(f"ok {step} {name}")


def ask(text):
    rid = api("POST", "/api/ask", {"message": text, "operator": OPERATOR})["run_id"]
    end = time.time() + TIMEOUT
    while time.time() < end:
        run = next((r for r in api("GET", "/api/runs?limit=20") if r["id"] == rid), None)
        if run and run["status"] in ("done", "completed", "failed", "error"):
            return rid, run
        time.sleep(3)
    fail("ask", f"run {rid} timed out")


def _fresh_thread():
    import subprocess
    pg = os.getenv("PG_CONTAINER", "ic-p12-postgres-1")
    sql = ("INSERT INTO threads (thread_id, phone) SELECT 'ui:owner-old', phone FROM threads "
           "WHERE thread_id = 'ui:owner' ON CONFLICT DO NOTHING; "
           "UPDATE messages SET thread_id = 'ui:owner-old' WHERE thread_id = 'ui:owner' "
           "AND EXISTS (SELECT 1 FROM threads WHERE thread_id = 'ui:owner-old'); "
           "UPDATE threads SET summary = NULL, messages_since_summary = 0 WHERE thread_id = 'ui:owner'; "
           # rejected proposals are remembered on purpose (propose_kpi refuses them); clear the
           # earlier runs' rejected invoice KPI proposals so the ask can propose again. Quick 015:
           # match the normalized title anywhere (on p18 a rejected "Overdue invoices (weekly
           # count)" survived the old 'open invoice%' prefix and the next run's propose_kpi was
           # refused as declined; any step 2 title not starting "open invoice" fails the same
           # way), and drop this scenario's own proposals an aborted run left pending.
           "DELETE FROM actions WHERE kind = 'create_watch' AND (status = 'rejected' OR "
           "(status = 'proposed' AND payload->>'origin' = 'chat' "
           "AND payload->>'thread_id' = 'ui:owner')) "
           "AND btrim(regexp_replace(lower(payload->>'title'), '[^[:alnum:]]+', ' ', 'g')) "
           "LIKE '%invoice%';")
    r = subprocess.run(["docker", "exec", "-i", pg, "psql", "-U", "agent", "-d", "agent_memory",
                        "-v", "ON_ERROR_STOP=1", "-At"], input=sql, capture_output=True, text=True,
                       encoding="utf-8", env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    if r.returncode != 0:
        fail("0", "could not clear ui:owner thread: " + r.stderr[:200])


def main():
    global KEY
    path = sys.argv[sys.argv.index("--secrets") + 1] if "--secrets" in sys.argv else None
    if os.getenv("SCENARIO_DEMO_STACK") != "1" or not any(
            h in BASE for h in ("127.0.0.1", "localhost")):
        print("refused: set SCENARIO_DEMO_STACK=1 and a loopback BASE_URL")
        return 2
    KEY = _secret("OPS_KEY", path)
    if not KEY:
        print("refused: no OPS_KEY")
        return 2
    if not (_secret("ANTHROPIC_API_KEY", path) or os.getenv("SCENARIO_HAS_LLM")):
        print("SKIP no LLM key configured")
        return 0
    try:
        api("GET", "/api/watches")
    except Exception as e:
        print(f"unhealthy: {type(e).__name__}")
        return 2

    # Prior runs leave "Track ..." exchanges on ui:owner; the supervisor then answers from chat
    # history without calling a tool. Move the thread aside (demo stack only, guarded above).
    _fresh_thread()
    before = {w["id"] for w in api("GET", "/api/watches")}
    # 1: snake_case SQL stays unquoted
    sqls = []
    for q1 in ("How many customers are there in each region? Answer in the chat.",
               "Run a SQL query on the live database: customers per region, answer in the chat."):
        rid, _ = ask(q1)
        sqls = [t for t in api("GET", f"/api/runs/{rid}/tools") if t["tool"] == "run_sql"]
        if sqls:
            break
    if not sqls:
        fail(1, "no run_sql call")
    for t in sqls:
        a = t["args"]
        d = json.loads(a) if isinstance(a, str) else a
        q = str(d.get("sql") or d.get("query") or "")
        if '"' in q:
            fail(1, f"quoted identifier in SQL: {q[:120]}")
    ok(1, "snake_case SQL unquoted")

    # 2: standing request -> proposed create_watch, no watch
    rid, run = ask("Track the number of open invoices every day.")
    acts = [a for a in api("GET", "/api/actions?status=proposed&kind=create_watch")
            if a.get("task_run_id") == rid]
    if not acts:
        fail(2, "no proposed create_watch for the run; "
             + run_digest(api("GET", f"/api/runs/{rid}/tools"), run))
    payload = acts[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if payload.get("origin") != "chat" or payload.get("thread_id") != f"ui:{OPERATOR}":
        fail(2, "payload origin/thread_id wrong")
    new = [w for w in api("GET", "/api/watches") if w["id"] not in before]
    if new:
        fail(2, "a watch was created without approval")
    ok(2, "track -> proposed create_watch on ui:owner")
    for a in acts:
        api("POST", f"/api/actions/{a['id']}/reject", {})

    # 3: one-shot -> watch directly. Fresh thread: after a "Done! I've proposed ..." reply the
    # model sometimes mimics it for the next ask without calling any tool.
    _fresh_thread()
    # vary the (unreachable) threshold per run: memory of an earlier identical request makes the
    # supervisor answer "done" from recall without calling the tool
    rid, _ = ask(f"Let me know when there are more than {5000000 + int(time.time()) % 99991} open invoices.")
    new = [w for w in api("GET", "/api/watches") if w["id"] not in before]
    if not new or not any(w.get("one_shot") for w in new):
        fail(3, "no one-shot watch created")
    if [a for a in api("GET", "/api/actions?status=proposed&kind=create_watch")
            if a.get("task_run_id") == rid]:
        fail(3, "one-shot request produced an action")
    ok(3, "let me know -> one-shot watch")
    history = [w["id"] for w in new]  # Quick 015: step 4 runs on this thread; these are its history
    for w in new:
        api("DELETE", f"/api/watches/{w['id']}")

    # 4 (12-VERIFICATION gap 2): one-off asks on the used ui:owner thread; the thread is NOT moved aside
    run_ids = []
    results = []
    try:
        rid4, _ = ask("Track the number of overdue invoices every week.")
        run_ids.append(rid4)
        wording = ["Let me know when there are more than {n} open invoices.",
                   "Tell me when the number of open invoices goes above {n}.",
                   "Alert me when open invoices exceed {n}."]
        for i, tmpl in enumerate(wording, 1):
            n = 5000000 + (int(time.time()) + i * 7919) % 99991
            rid, run = ask(tmpl.format(n=n))
            run_ids.append(rid)
            reply = run.get("result_summary") or ""
            live = {w["id"]: w for w in api("GET", "/api/watches")}
            created = []
            for t in api("GET", f"/api/runs/{rid}/tools"):
                if t["tool"] == "create_watch":
                    created += [int(x) for x in re.findall(r"watch #(\d+) created", t.get("result_digest") or "")]
            created = [c for c in created if c in live]
            one_shot_new = [c for c in created if live[c].get("one_shot")]
            truthful, cited = reply_truthful(reply, created, history)
            if not truthful:
                fail(f"4.{i}", f"untruthful reply: cited {cited}, created {created}, "
                     f"earlier on this thread {history}: {reply[:160]}")
            ok(f"4.{i}", f"created={created} cited={cited}")
            history += created
            results.append(bool(one_shot_new))
        if sum(results) < 2:
            fail("4", f"only {sum(results)}/3 used-thread one-off asks created a one-shot watch")
        ok(4, f"used-thread one-off asks: {sum(results)}/3 created a one-shot watch, all replies truthful")
    finally:
        for w in api("GET", "/api/watches"):
            if w["id"] not in before:
                api("DELETE", f"/api/watches/{w['id']}")
        for a in api("GET", "/api/actions?status=proposed&kind=create_watch"):
            if a.get("task_run_id") in run_ids:
                api("POST", f"/api/actions/{a['id']}/reject", {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
