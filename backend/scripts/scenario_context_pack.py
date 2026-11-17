"""Phase 12 live acceptance for the context-pack import (CTX-08, CTX-09e on Opus 5.5).

HARNESS / DEV MACHINE ONLY: uploads two fixture packs on ui:owner, checks the proposals the
context specialist makes, proves nothing was applied before approval, then rejects what it made.
Refuses (exit 2) unless SCENARIO_DEMO_STACK=1 and BASE_URL is loopback.
Exit: 0 pass (or SKIP when the stack has no LLM key), 1 fail, 2 refused/unhealthy.
Run from backend/:  SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/scenario_context_pack.py [--secrets PATH] [--keep] [--no-repeat] [--no-full]
Env: BASE_URL, PG_CONTAINER, SCENARIO_TIMEOUT (default 2400). OPS_KEY from env or --secrets; never printed.
"""

import json
import mimetypes  # noqa: F401  (kept for parity with other scripts)
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.getenv("BASE_URL", "http://127.0.0.1:9121").rstrip("/")
PG = os.getenv("PG_CONTAINER", "ic-p12-postgres-1")
TIMEOUT = int(os.getenv("SCENARIO_TIMEOUT", "2400"))
HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "..", "tests", "fixtures")
SMALL = "context_pack_northwind.json"
FULL = "context_pack_northwind_full.json"
SKILL = "context-pack-import"
OPERATOR = "owner"
INSTRUCTION = ("This file is a context pack. Import it with the context specialist using the exact "
               "filename shown above: propose only, apply nothing, and tell me what is waiting "
               "for my approval.")
HIDE = "business_db.public.inventory_snapshots"
PAY = "business_db.public.payments"
TABLES = ["regions", "sales_reps", "customers", "products", "sales_orders", "order_lines",
          "invoices", "payments", "inventory_snapshots"]
KEY = ""
CREATED: list[int] = []


class Fail(Exception):
    pass


def _secret(name, path):
    v = os.getenv(name, "")
    if not v and path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.startswith(name + "="):
                v = line.split("=", 1)[1]
    return v.strip("\r\n \"'")


def raw(method, path, body=None, headers=None, data=None, timeout=120):
    h = {"x-ops-key": KEY}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["content-type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call(method, path, body=None, ok_codes=(200,)):
    st, b = raw(method, path, body)
    if st not in ok_codes:
        raise Fail(f"{method} {path} -> HTTP {st}: {b[:200]!r}")
    return json.loads(b) if b else {}


def get(path):
    return call("GET", path)


def ok(step, name):
    print(f"ok {step} {name}", flush=True)


def need(cond, step, name, detail=""):
    if not cond:
        raise Fail(f"{step} {name} {detail}")
    ok(step, name)


def jd(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def psql(sql):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    r = subprocess.run(["docker", "exec", "-i", PG, "psql", "-U", "agent", "-d", "agent_memory",
                        "-v", "ON_ERROR_STOP=1", "-At"], input=sql, capture_output=True,
                       text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise Fail("psql: " + r.stderr[:300])
    return r.stdout.strip()


def counts():
    return {
        "approved_context": psql("SELECT count(*) FROM business_context WHERE status = 'approved'"),
        "watches": psql("SELECT count(*) FROM watches"),
        "learnings": psql("SELECT count(*) FROM learnings"),
        "hidden_tables": psql("SELECT count(*) FROM context_tables WHERE status = 'hidden'"),
    }


def forget_rejected():
    """Rejected proposals are remembered on purpose (the tools refuse to re-propose them), so
    the previous run's cleanup would make this run propose nothing. Demo stack only."""
    psql("DELETE FROM actions WHERE status = 'rejected' AND ("
         "(kind = 'create_watch' AND payload->>'origin' = 'context_pack') "
         "OR (kind = 'hide_table' AND payload->>'ref' = 'business_db.public.inventory_snapshots') "
         "OR (kind = 'context_change' AND payload->>'origin' = 'context_pack'));")


def upload(name):
    """Multipart upload exactly as the UI does; returns (response, upload_time)."""
    path = os.path.join(FIX, name)
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("operator", OPERATOR), ("instruction", INSTRUCTION)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                  "Content-Type: application/json\r\n\r\n").encode() + open(path, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    t0 = time.time()
    st, b = raw("POST", "/api/documents/upload", headers={
        "content-type": f"multipart/form-data; boundary={boundary}"}, data=b"".join(parts))
    if st != 200:
        raise Fail(f"upload {name} -> HTTP {st}: {b[:200]!r}")
    return json.loads(b), t0


def wait_run(rid, t0):
    end = time.time() + TIMEOUT
    while time.time() < end:
        run = next((r for r in get("/api/runs?limit=50") if r["id"] == rid), None)
        if run and run["status"] in ("done", "failed"):
            if run["status"] == "failed":
                raise Fail(f"run {rid} failed: {str(run.get('error'))[:300]}")
            return time.time() - t0
        time.sleep(5)
    raise Fail(f"run {rid} did not finish in {TIMEOUT}s")


def run_actions(rid):
    rows = get("/api/actions?status=&limit=500")
    return [a for a in rows if a.get("task_run_id") == rid]


def delegate_row(rid):
    for t in get(f"/api/runs/{rid}/tools"):
        if (t.get("tool") or t.get("name")) == "delegate_to_context_agent":
            return jd(t.get("args"))
    return {}


def refs(acts, kind):
    return [jd(a.get("payload")).get("ref") for a in acts if a.get("kind") == kind]


def reject(ids):
    for i in ids:
        raw("POST", f"/api/actions/{i}/reject", {})


def full_run_checks(rid, acts, label, wall):
    tools = [(t.get("tool") or t.get("name")) for t in get(f"/api/runs/{rid}/tools")]
    need("delegate_to_context_agent" in tools, label, "delegated to the context specialist")
    need(any(t in tools for t in ("read_context_pack", "propose_context_pack_items")), label,
         "pack tool used", str(tools))
    need("extract_document" not in tools, label, "extract_document not used")
    need(all(a.get("status") == "proposed" for a in acts), label, "every action is proposed",
         str({a["id"]: a["status"] for a in acts}))
    d = delegate_row(rid)
    need(not d.get("stopped_early"), label, "delegate not stopped early")
    print(f"metric {label} tokens_used={d.get('tokens_used')} token_budget={d.get('token_budget')} "
          f"delegate_seconds={d.get('seconds')} run_seconds={wall:.0f} proposals={len(acts)}", flush=True)
    tu, tb = d.get("tokens_used"), d.get("token_budget")
    if isinstance(tu, (int, float)) and isinstance(tb, (int, float)) and tu > tb / 2:
        print("WARN headroom tokens_used above half the budget")


def scenario(args):
    forget_rejected()
    before = counts()
    print("before", before)
    resp, t0 = upload(SMALL)
    need("queued" in resp and resp.get("filename") == SMALL, "2", "upload queued with fixture filename")
    rid = resp["queued"]
    wall = wait_run(rid, t0)
    ok("2", f"run {rid} done")
    acts = run_actions(rid)
    CREATED.extend(a["id"] for a in acts)
    ctx = [r for r in refs(acts, "context_change") if r]
    print("   context_change refs:", sorted(ctx))
    full_run_checks(rid, acts, "small", wall)
    need(any(r.endswith(".customers") for r in ctx) and any(r.endswith(".invoices") for r in ctx),
         "3", "descriptions for customers and invoices")
    need(any(r.startswith("rule:") for r in ctx), "3", "at least one rule")
    need(any(r.startswith("kpi:") for r in ctx), "3", "at least one KPI definition")
    cw = [a for a in acts if a.get("kind") == "create_watch"
          and jd(a.get("payload")).get("origin") == "context_pack"]
    need(len(cw) >= 1, "3", "create_watch with origin context_pack", f"count={len(cw)}")
    hides = refs(acts, "hide_table")
    need(hides.count(HIDE) == 1, "3", "exactly one hide_table for inventory_snapshots", str(hides))
    need(HIDE not in ctx, "3", "low-confidence table not proposed for description")
    need(PAY not in ctx and PAY not in hides, "3", "payments not acted on")
    after = counts()
    need(after == before, "4", "nothing applied before approval", f"{before} -> {after}")
    msgs = get(f"/api/ask?operator={OPERATOR}&limit=20")
    msgs = msgs.get("messages", msgs) if isinstance(msgs, dict) else msgs
    last = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None) if msgs else None
    txt = str((last or {}).get("content", "")).lower()
    need(bool(last) and any(c.isdigit() for c in txt) and "approv" in txt, "4",
         "reply mentions a count and approval")
    # 5 repeat
    if not args["no_repeat"]:
        pending = {(a["kind"], jd(a.get("payload")).get("ref")) for a in acts
                   if a.get("kind") == "context_change"}
        resp2, t1 = upload(SMALL)
        rid2 = resp2["queued"]
        wait_run(rid2, t1)
        acts2 = run_actions(rid2)
        CREATED.extend(a["id"] for a in acts2)
        dup = [a for a in acts2 if a.get("kind") == "context_change"
               and ("context_change", jd(a.get("payload")).get("ref")) in pending]
        need(not dup, "5", "re-upload adds no duplicate context_change", str([a["id"] for a in dup]))
        newer = [jd(a.get("payload")).get("ref") for a in acts2 if a.get("kind") == "context_change"]
        if newer:
            print("   new proposals on re-upload:", newer)
    # 6 clean
    reject(list(CREATED))
    CREATED.clear()
    ok("6", "small-pack proposals rejected")
    # 7 full
    if not args["no_full"]:
        forget_rejected()
        before = counts()
        resp3, t2 = upload(FULL)
        need(resp3.get("filename") == FULL, "7", "full pack queued")
        rid3 = resp3["queued"]
        wall3 = wait_run(rid3, t2)
        ok("7", f"full run {rid3} done")
        acts3 = run_actions(rid3)
        CREATED.extend(a["id"] for a in acts3)
        ctx3 = [r for r in refs(acts3, "context_change") if r]
        missing = [t for t in TABLES if t != "inventory_snapshots"
                   and f"business_db.public.{t}" not in ctx3]
        need(not missing, "7", "descriptions for the 8 non-low tables", str(missing))
        need(sum(r.startswith("rule:") for r in ctx3) >= 10, "7", ">= 10 rules")
        need(sum(r.startswith("kpi:") for r in ctx3) >= 5, "7", ">= 5 KPIs")
        cw3 = [a for a in acts3 if a.get("kind") == "create_watch"
               and jd(a.get("payload")).get("origin") == "context_pack"]
        need(1 <= len(cw3) <= 8, "7", "1..8 context_pack create_watch", f"count={len(cw3)}")
        need(refs(acts3, "hide_table").count(HIDE) == 1, "7", "exactly one hide_table")
        full_run_checks(rid3, acts3, "full", wall3)
        need(counts() == before, "7", "nothing applied by the full run")


def main():
    global KEY
    args = {"keep": "--keep" in sys.argv, "no_repeat": "--no-repeat" in sys.argv,
            "no_full": "--no-full" in sys.argv}
    path = sys.argv[sys.argv.index("--secrets") + 1] if "--secrets" in sys.argv else None
    if os.getenv("SCENARIO_DEMO_STACK") != "1" or (urllib.parse.urlparse(BASE).hostname or "") not in (
            "127.0.0.1", "localhost", "::1"):
        print("REFUSED: set SCENARIO_DEMO_STACK=1 and a loopback BASE_URL")
        return 2
    KEY = _secret("OPS_KEY", path)
    if not KEY:
        print("REFUSED: no OPS_KEY")
        return 2
    try:
        st, _ = raw("GET", "/health", timeout=20)
    except urllib.error.URLError as e:
        print(f"stack unreachable: {e}")
        return 2
    if st != 200:
        print("stack unhealthy")
        return 2
    llm = get("/api/config/status").get("llm", {}).get("state")
    if llm == "not_configured":
        print("SKIP no LLM key configured on the stack")
        return 0
    skill = get(f"/api/skills/{SKILL}")
    was = bool(skill.get("enabled"))
    call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
    ok("1", f"health ok, llm={llm}, skill enabled (was {was})")
    try:
        scenario(args)
        return 0
    except Fail as e:
        print(f"FAIL {e}")
        return 1
    finally:
        if not args["keep"]:
            reject(list(CREATED))
        try:
            call("PATCH", f"/api/skills/{SKILL}", {"enabled": was})
        except Exception:
            pass
        if not args["keep"]:
            ok("8", "cleanup: proposals rejected, skill state restored")


if __name__ == "__main__":
    sys.exit(main())
