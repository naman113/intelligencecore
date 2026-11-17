"""Dashboard eval runner (22-19, DCAP-17): setup, run and report on DISPOSABLE TEST STACKS.

    SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/dash_eval.py setup  --stack p33 --client mill --secrets <env file>
    SCENARIO_DEMO_STACK=1 uv run --frozen python scripts/dash_eval.py run    --stack p33 --client mill --out DIR [--requests 1,12] [--round 0]
    uv run --frozen python scripts/dash_eval.py report --out DIR [--out DIR ...] --write PREFIX [--budget N]

Guards (exit 2): SCENARIO_DEMO_STACK=1, --stack matches p33..p99, loopback URLs only on this stack's
own ports (API 9000+10N+1, dashboards 9000+10N+2; an invalid port is refused too), only containers
named ic-<stack>-*, never agentic-* or client-* and never a URL naming railway.
Nothing here prints the ops key, tokens or database passwords.
"""

import argparse
import json
import os
import re
import secrets as pysecrets
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import dash_eval_refs  # noqa: E402
import dash_eval_score  # noqa: E402

STACK_RE = re.compile(r"^p(3[3-9]|[4-9][0-9])$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
FORBIDDEN_CONTAINERS = ("agentic-", "client-")
CLIENTS = ("mill", "fleet", "distributor")
ZONE = "Asia/Kolkata"
OPERATOR = "eval"
RUN_TIMEOUT = 900
SKILL = "dashboard-authoring"


class Refused(Exception):
    pass


class Fail(Exception):
    pass


class RunTimeout(Fail):
    """The run did not finish in time (any other Fail is a real error and keeps its message)."""


# ------------------------------------------------------------------ redaction

_REDACT = [
    (re.compile(r"([?&](?:t|p)=)[^&\s\"']+"), r"\1REDACTED"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "REDACTED"),
    (re.compile(r"(?i)\b(OPS_KEY|api[_-]?key|token|secret|password)(\s*[=:]\s*)\S+"), r"\1\2REDACTED"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1REDACTED@"),
    (re.compile(r"(?i)(\bPASSWORD\s+)'[^']*'"), r"\1'REDACTED'"),  # psql echoes a failing ALTER ROLE
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "REDACTED"),
]


def redact(text, extra=()):
    t = str(text)
    for v in extra:
        if v:
            t = t.replace(v, "REDACTED")
    for rx, rep in _REDACT:
        t = rx.sub(rep, t)
    return t


def say(msg, extra=()):
    print(redact(msg, extra), flush=True)


# ------------------------------------------------------------------ guards

def stack_urls(stack):
    base = 9000 + 10 * int(stack[1:])
    return f"http://127.0.0.1:{base + 1}", f"http://127.0.0.1:{base + 2}"


def container(stack, name):
    c = f"ic-{stack}-{name}"
    check_container(stack, c)
    return c


def check_container(stack, name):
    if name.startswith(FORBIDDEN_CONTAINERS):
        raise Refused(f"container {name} is not a test-stack container")
    if not name.startswith(f"ic-{stack}-"):
        raise Refused(f"container {name} is not named ic-{stack}-*")


def check_guards(stack, env, base, dash):
    if env.get("SCENARIO_DEMO_STACK") != "1":
        raise Refused("set SCENARIO_DEMO_STACK=1 only on a disposable test stack")
    if not STACK_RE.match(stack or ""):
        raise Refused(f"--stack must match p33..p99, got {stack!r}")
    want = dict(zip(("API", "dashboards"), stack_urls(stack)))
    for label, u in (("API", base), ("dashboards", dash)):
        if "railway" in u.lower():
            raise Refused("a URL names railway")
        pu = urllib.parse.urlparse(u)
        if (pu.hostname or "") not in LOOPBACK:
            raise Refused(f"{pu.hostname} is not loopback")
        try:
            port = pu.port
        except ValueError:
            raise Refused(f"{label} URL has an invalid port")
        # Real client copies also sit on loopback (8080, 8090, 8190): only this stack's own ports pass.
        if port != urllib.parse.urlparse(want[label]).port:
            raise Refused(f"{label} URL port {port} is not stack {stack}'s port "
                          f"{urllib.parse.urlparse(want[label]).port}")


def ops_key(env, secrets_path):
    """The key from --secrets or the environment; a different key in both is refused (never guess which
    stack), so the file never overrides. A --secrets path that cannot be read is refused."""
    junk = "\r\n \t\"'"
    envk = (env.get("OPS_KEY") or "").strip(junk)
    filek = ""
    if secrets_path:
        try:
            for line in open(secrets_path, encoding="utf-8"):
                if line.startswith("OPS_KEY="):
                    filek = line.split("=", 1)[1].strip(junk)
        except OSError:
            raise Refused("the --secrets file cannot be read")
    if filek and envk and filek != envk:
        raise Refused("OPS_KEY in the environment differs from the --secrets file; unset one")
    return filek or envk


# ------------------------------------------------------------------ transport

def http_json(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if body is not None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            st = r.status
    except urllib.error.HTTPError as e:
        raw, st = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        raise Fail(f"{method} {url.split('?')[0]} unreachable: {type(e).__name__}")
    try:
        return st, (json.loads(raw) if raw else {})
    except ValueError:
        return st, {}


def sh(args, stdin=None, env=None):
    e = dict(os.environ, MSYS_NO_PATHCONV="1")
    e.update(env or {})
    return subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e)


class Ctx:
    def __init__(self, stack, client, key, base, dash):
        self.stack, self.client, self.key, self.base, self.dash = stack, client, key, base, dash
        self.pg = container(stack, "postgres-1")
        self.app = container(stack, "app-1")
        self.db = f"eval_{client}"

    def call(self, method, path, body=None, ok=(200,), timeout=60):
        st, b = http_json(method, self.base + path, body, {"x-ops-key": self.key}, timeout)
        if st not in ok:
            raise Fail(f"{method} {path.split('?')[0]} -> HTTP {st}: {redact(json.dumps(b)[:200], [self.key])}")
        return b

    def psql(self, sql, db="agent_memory", user="agent", stdin_sql=None):
        check_container(self.stack, self.pg)
        r = sh(["docker", "exec", "-i", self.pg, "psql", "-q", "-U", user, "-d", db,
                "-v", "ON_ERROR_STOP=1", "-At"], stdin_sql if stdin_sql is not None else sql)
        if r.returncode != 0:
            raise Fail("psql: " + redact(r.stderr[:300]))
        return r.stdout.strip()


# ------------------------------------------------------------------ time

def today_ist():
    return datetime.now(ZoneInfo(ZONE)).date()


# ------------------------------------------------------------------ setup

def cmd_setup(ctx, args):
    today = args.today or today_ist().isoformat()
    pw = pysecrets.token_urlsafe(24)
    db = ctx.db
    say(f"setup: {ctx.stack} client {ctx.client} fixture day {today}", [pw, ctx.key])
    ctx.psql("", db="postgres", stdin_sql=f"DROP DATABASE IF EXISTS {db} WITH (FORCE);\nCREATE DATABASE {db};")
    ctx.psql("", db="postgres", stdin_sql=(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eval_ro') "
        "THEN CREATE ROLE eval_ro LOGIN; END IF; END $$;\n"
        f"ALTER ROLE eval_ro WITH LOGIN PASSWORD '{pw}';\n"
        "ALTER ROLE eval_ro SET default_transaction_read_only = on;\n"
        f"GRANT CONNECT ON DATABASE {db} TO eval_ro;"))
    schema = (HERE / "dash_eval" / "fixtures" / f"{ctx.client}.schema.sql").read_text(encoding="utf-8")
    ctx.psql("", db=db, stdin_sql=schema)
    gen = subprocess.run([sys.executable, str(HERE / "dash_eval" / "gen_fixture.py"), "--client",
                          ctx.client, "--today", today, "--seed", "7"], capture_output=True,
                         text=True, encoding="utf-8")
    if gen.returncode != 0:
        raise Fail("gen_fixture failed")
    ctx.psql("", db=db, stdin_sql=gen.stdout)
    ctx.psql("", db=db, stdin_sql=("GRANT USAGE ON SCHEMA public TO eval_ro;\n"
                                   "GRANT SELECT ON ALL TABLES IN SCHEMA public TO eval_ro;"))
    say("setup: fixture database and eval_ro role ready", [pw])
    url = f"postgresql://eval_ro:{pw}@postgres:5432/{db}"
    ctx.call("PUT", "/api/config", {"values": {"tenant_database_url": url, "timezone": ZONE}})
    for _ in range(20):
        res = ctx.call("POST", "/api/config/test/business_db", ok=(200,))
        if res.get("ok"):
            break
        time.sleep(3)
    else:
        raise Fail("business DB probe did not pass")
    say("setup: tenant URL and timezone set, business DB probe passed", [pw])
    st, _ = http_json("POST", ctx.base + "/api/skills/install", {"id": f"intelligence-corner/{SKILL}"},
                      {"x-ops-key": ctx.key})
    ctx.call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
    say("setup: dashboard-authoring enabled")
    model = json.loads((HERE / "dash_eval" / "models" / f"{ctx.client}.json").read_text(encoding="utf-8"))
    total = 0
    for stage in model_stages(model):
        stage = skip_active(stage, active_names(ctx))  # a re-run finds earlier items already active
        for i in range(0, len(stage), 12):
            propose_in_app(ctx, stage[i:i + 12])
        total += approve_model_changes(ctx)
    verify_model_active(ctx, model)
    say(f"setup: fixture model proposed and {total} model_change actions approved; every item active")
    return 0


def item_name(item):
    d = item.get("definition") if isinstance(item.get("definition"), dict) else item
    return d.get("name") or item.get("name")


def model_stages(model):
    """Dependency order: a ratio or derived measure is refused until its inputs are active."""
    def pick(f):
        return [m for m in model if f(m)]
    def is_m(m):
        return m.get("object") == "measure"
    def mtype(m):
        d = m.get("definition") if isinstance(m.get("definition"), dict) else m
        return d.get("type")
    stages = [pick(lambda m: m.get("object") == "table"),
              pick(lambda m: m.get("object") == "relationship"),
              pick(lambda m: is_m(m) and mtype(m) in (None, "simple")),
              pick(lambda m: is_m(m) and mtype(m) not in (None, "simple"))]
    placed = {id(m) for st in stages for m in st}
    stages.insert(2, pick(lambda m: id(m) not in placed))  # unknown object kinds, before measures
    return [st for st in stages if st]


MODEL_TABLES = (("table", "model_tables"), ("relationship", "model_relationships"),
                ("measure", "model_measures"))


def active_names(ctx):
    return {obj: set(ctx.psql(f"SELECT name FROM {table} WHERE status = 'active'").split())
            for obj, table in MODEL_TABLES}


def skip_active(items, active):
    """Drop items whose name is already active, so setup can run again on the same stack."""
    return [m for m in items if item_name(m) not in active.get(m.get("object"), set())]


def verify_model_active(ctx, model):
    for obj, table in (("table", "model_tables"), ("relationship", "model_relationships"),
                       ("measure", "model_measures")):
        want = {item_name(m) for m in model if m.get("object") == obj}
        have = set(ctx.psql(f"SELECT name FROM {table} WHERE status = 'active'").split())
        missing = sorted(w for w in want if w not in have)
        if missing:
            raise Fail(f"model {obj} not active after setup: {', '.join(missing)}")


def check_tool_result(text):
    bad = [ln[:200] for ln in str(text).splitlines()
           if re.search(r"(?i)\brefused:|TOOL ERROR", ln)]
    if bad:
        raise Fail("propose_model_change: " + redact(" | ".join(bad[:5])))


def propose_in_app(ctx, items):
    check_container(ctx.stack, ctx.app)
    prog = (
        "import asyncio, json\n"
        f"P = json.loads({json.dumps(json.dumps(items))})\n"
        "async def main():\n"
        "    from app.agents.graph import ctx_phone\n"
        "    from app import modeltools as mt, db, configservice\n"
        "    ctx_phone.set('ui:dasheval')\n"
        "    try:\n"
        "        await configservice.load_overrides()\n"
        "        out = await mt.propose_model_change.ainvoke({'items': P})\n"
        "        print('RESULT ' + json.dumps(str(out)))\n"
        "    finally:\n"
        "        await db.close_pool()\n"
        "asyncio.run(main())\n")
    r = sh(["docker", "exec", "-i", ctx.app, "python", "-"], prog)
    if "RESULT " not in r.stdout:
        raise Fail("propose_model_change failed: " + redact((r.stderr or r.stdout)[-300:]))
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")][-1]
    check_tool_result(json.loads(line[7:]))


def approve_model_changes(ctx):
    done = 0
    pending = ctx.call("GET", "/api/actions?status=proposed&kind=model_change&limit=200")
    pending = sorted(pending, key=lambda a: a["id"])
    failed = []
    # One pass: an action whose executor fails is marked 'failed' and cannot be approved again.
    for a in pending:
        try:
            res = ctx.call("POST", f"/api/actions/{a['id']}/approve", ok=(200,))
        except Fail as e:
            failed.append(f"#{a['id']}: {str(e)[:160]}")
            continue
        if res.get("status") == "executed":
            done += 1
        else:
            failed.append(f"#{a['id']}: status {res.get('status')} {str(res.get('error') or '')[:120]}")
    if failed:
        raise Fail(f"{len(failed)} model_change actions could not be approved: " + " | ".join(failed[:5]))
    return done


# ------------------------------------------------------------------ run helpers

def parse_preview_digest(text):
    """The tool log keeps 1000 characters of the preview result: JSON when short, a cut JSON otherwise."""
    if isinstance(text, dict):
        return text
    s = str(text or "")
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    ok = bool(re.search(r'"ok":\s*true', s))
    errors = []
    if not ok or not re.search(r'"errors":\s*\[\s*\]', s):
        errors = [{"rule": r} for r in re.findall(r'"rule":\s*"([^"]+)"', s)[:5]] or ["preview failed"]
        if ok and not re.search(r'"errors":\s*\[', s):
            errors = []
    warnings = []
    wi = s.find('"warnings"')
    if wi >= 0:
        warnings = [{"rule": r} for r in re.findall(r'"rule":\s*"([^"]+)"', s[wi:])]
    return {"ok": ok and not errors, "errors": errors, "warnings": warnings}


def convert_tools(rows):
    out = []
    for t in rows or []:
        name = t.get("tool") or t.get("name") or ""
        res = t.get("result_digest", t.get("result"))
        if name == "preview_dashboard":
            res = parse_preview_digest(res)
        out.append({"name": name, "args": t.get("args"), "result": res})
    return out


def payload_of(a):
    p = a.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def wait_run(ctx, run_id, timeout=RUN_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(4)
        for r in ctx.call("GET", "/api/runs?limit=200"):
            if r["id"] == run_id and r["status"] in ("done", "failed"):
                return r
    raise RunTimeout(f"run {run_id} did not finish in time")


def read_tokens(ctx, run_id):
    q = ("SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens), 0) "
         f"FROM llm_usage WHERE run_id = {int(run_id)}")
    last, stable = None, 0
    for _ in range(11):
        cur = int(ctx.psql(q) or 0)
        if cur == last:
            stable += 1
            if stable >= 1:
                return cur
        else:
            stable = 0
        last = cur
        time.sleep(3)
    return last or 0


def read_refreshed(ctx, version_id):
    out = ctx.psql(
        "SELECT query_id || '|' || ((refreshed_at AT TIME ZONE 'Asia/Kolkata')::date)::text "
        f"FROM dashboard_snapshots WHERE version_id = {int(version_id)} AND refreshed_at IS NOT NULL "
        "ORDER BY query_id")
    res = {}
    for line in out.splitlines():
        if "|" in line:
            q, d = line.rsplit("|", 1)
            res[q] = datetime.strptime(d, "%Y-%m-%d").date()
    return res


def run_probe(page_url):
    r = sh(["node", str(HERE / "dash_eval_gate.mjs")], env={"DRAFT_URL": page_url})
    if r.returncode == 3:
        raise Fail("no Chrome or Edge found for the numbers gate")
    if r.returncode != 0:
        raise Fail("gate probe failed: " + redact(r.stderr[-200:]))
    line = (r.stdout.strip().splitlines() or ["[]"])[-1]
    try:
        vals = json.loads(line)
    except ValueError:
        raise Fail("gate probe printed no JSON")
    return vals if isinstance(vals, list) else []


def fill_cols(values, render):
    """Stat tiles carry no icCol: a single-measure tile has one possible column."""
    tiles = {t["id"]: t for t in render.get("tiles", [])}
    out = []
    for v in values:
        v = dict(v)
        if not v.get("col"):
            ms = (tiles.get(v.get("tile")) or {}).get("measures") or []
            if len(ms) == 1:
                v["col"] = ms[0].get("column")
        v["row"] = v.get("row") or ""
        out.append(v)
    return out


def run_ref_sql(ctx, sql):
    stmt = ("SET ROLE eval_ro;\nSET default_transaction_read_only = on;\n"
            f"SET TIME ZONE '{ZONE}';\n{sql.strip().rstrip(';')};\n")
    out = ctx.psql("", db=ctx.db, stdin_sql=stmt)
    line = (out.splitlines() or [""])[-1].strip()
    if line == "":
        return None
    return float(line)


def gate(ctx, values, render, authoring, refs, today, refreshed):
    plan = dash_eval_refs.plan_checks(fill_cols(values, render), render, authoring, refs, today,
                                      ctx.client, refreshed)
    comps, skips, errors = [], [], []
    for p in plan:
        if "error" in p:
            errors.append(f"{p['tile']}/{p['col']}: {p['error']}")
        elif "skip" in p:
            skips.append({"tile": p["tile"], "col": p["col"], "row": p["row"], "reason": p["skip"]})
        else:
            try:
                exp = run_ref_sql(ctx, p["sql"])
            except Fail as e:
                errors.append(f"{p['tile']}/{p['col']}: reference query failed ({str(e)[:100]})")
                continue
            comps.append({"tile": p["tile"], "col": p["col"], "row": p["row"], "member": p["member"],
                          "span": p["span"], "shown": p["shown"], "expected": exp,
                          "match": dash_eval_refs.compare(p["shown"], exp)})
    return comps, skips, errors


def collect_draft(ctx, record, run_id):
    props = [a for a in ctx.call("GET", "/api/actions?status=proposed&limit=200")
             if a.get("kind") == "publish_dashboard" and a.get("task_run_id") == run_id]
    if not props:
        record["errors"].append("no draft")
        return None
    p = payload_of(sorted(props, key=lambda a: a["id"])[-1])
    slug, ver = p.get("slug"), p.get("version")
    version = ctx.call("GET", f"/api/dashboards/{slug}/versions/{ver}")
    record["spec"] = version.get("spec")
    record["slug"], record["version"] = slug, ver
    end = time.time() + 180
    while time.time() < end:
        snaps = ctx.call("GET", f"/api/dashboards/{slug}/versions/{ver}").get("snapshots") or []
        if snaps and all(s.get("refreshed_at") and not s.get("error") for s in snaps):
            break
        time.sleep(3)
    pv = ctx.call("POST", f"/api/dashboards/{slug}/versions/{ver}/preview", {"ttl_minutes": 30})
    tok = pv.get("preview_token")
    if not tok:
        record["errors"].append("no preview token")
        return None
    base = f"{ctx.dash}/d/{slug}/v/{ver}/"
    st, render = http_json("GET", f"{base}spec?p={tok}")
    if st != 200:
        record["errors"].append(f"render form HTTP {st}")
        return None
    record["render"] = render
    vid = int(ctx.psql("SELECT id FROM dashboard_versions WHERE dashboard_id = "
                       f"(SELECT id FROM dashboards WHERE slug = '{re.sub(r'[^a-z0-9-]', '', slug)}') "
                       f"AND version = {int(ver)}"))
    return {"page": f"{base}?p={tok}", "tok": tok, "vid": vid}


def run_request(ctx, req, chat, rnd, refs):
    record = {"n": req["n"], "round": rnd, "client": ctx.client, "chat": chat, "spec": None,
              "render": None, "tools": [], "reply": "", "tokens": 0, "model_calls": 0,
              "comparisons": [], "skips": [], "errors": []}
    try:
        # A new dashboard is asked the way the owner's UI asks it (frontend asks.dashboard); an edit
        # (a request with "after") continues its predecessor's chat and goes bare.
        message = req["text"] if req.get("after") is not None else "Build a dashboard: " + req["text"]
        run_id = ctx.call("POST", "/api/ask", {"message": message, "operator": OPERATOR,
                                               "chat": chat}, timeout=60)["run_id"]
        try:
            run = wait_run(ctx, run_id)
        except RunTimeout:
            record["errors"].append("run timed out")
            run = {"status": "timeout"}
        if run["status"] == "failed":
            record["errors"].append("run failed: " + redact(str(run.get("error"))[:200], [ctx.key]))
        record["tools"] = convert_tools(ctx.call("GET", f"/api/runs/{run_id}/tools"))
        # The reply of THIS run: the first assistant message after the run's own user message. A run
        # that wrote none leaves the reply empty (an earlier request's reply must not be scored).
        record["reply"] = ctx.psql(
            f"SELECT content FROM messages WHERE thread_id = '{re.sub(r'[^A-Za-z0-9:_-]', '', chat)}' "
            "AND role = 'assistant' AND id > "
            f"(SELECT COALESCE(message_id, 0) FROM task_runs WHERE id = {int(run_id)}) "
            "ORDER BY id ASC LIMIT 1")
        record["tokens"] = read_tokens(ctx, run_id)
        record["model_calls"] = int(ctx.psql(f"SELECT count(*) FROM llm_usage WHERE run_id = {int(run_id)}") or 0)
        d = collect_draft(ctx, record, run_id) if run["status"] == "done" else None
        if d:
            before = read_refreshed(ctx, d["vid"])
            today = today_ist()
            values = run_probe(d["page"])
            if today_ist() != today:
                today = today_ist()
                values = run_probe(d["page"])
            after = read_refreshed(ctx, d["vid"])
            if before != after:
                values = run_probe(d["page"])
                after = read_refreshed(ctx, d["vid"])
            comps, skips, errors = gate(ctx, values, record["render"], record["spec"], refs, today, after)
            record["comparisons"], record["skips"] = comps, skips
            record["errors"] += errors
    except Exception as e:  # one bad request must not drop the record or stop the baseline
        record["errors"].append(redact(f"{type(e).__name__}: {e}", [ctx.key]))
    record["finished_at"] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def load_requests():
    return json.loads((HERE / "dash_eval" / "requests.json").read_text(encoding="utf-8"))


def cmd_run(ctx, args):
    if args.runs != 1:
        # Records carry no run index and the report keeps only the latest record per request, so extra
        # runs would be paid for and discarded; run the command again with a new --out folder instead.
        raise Refused("--runs > 1 is not supported yet (the report merges one record per request)")
    allreq = load_requests()
    if args.requests:
        want = [int(x) for x in args.requests.split(",") if x.strip()]
        reqs = [r for r in allreq if r["n"] in want]
    else:
        want = None
        reqs = [r for r in allreq if r["client"] == ctx.client]
    bad = [r["n"] for r in reqs if r["client"] != ctx.client]
    if bad:
        raise Refused(f"requests {bad} belong to another client")
    selected = {r["n"] for r in reqs}
    refs = json.loads((HERE / "dash_eval" / "references.json").read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    chats = {}
    for req in reqs:
        pred = req.get("after")
        if pred is not None and (pred not in selected or pred not in chats):
            rec = {"n": req["n"], "round": args.round, "client": ctx.client, "chat": None,
                   "spec": None, "render": None, "tools": [], "reply": "", "tokens": 0,
                   "comparisons": [], "skips": [], "errors": ["predecessor not run"],
                   "finished_at": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            if pred is not None:
                chat = chats[pred]
            else:
                chat = ctx.call("POST", "/api/ask/chats", {
                    "operator": OPERATOR,
                    "title": f"eval {ctx.client} r{args.round} #{req['n']}"})["id"]
            chats.setdefault(req["n"], chat)
            rec = run_request(ctx, req, chat, args.round, refs)
        f = out / f"req{req['n']:02d}-round{args.round}-run1.json"
        f.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        say(f"run: request {req['n']} round {args.round} -> {len(rec['comparisons'])} compared, "
            f"{len(rec['skips'])} skipped, {len(rec['errors'])} errors", [ctx.key])
    return 0


# ------------------------------------------------------------------ report

def load_records(folders):
    recs = []
    for fo in folders:
        for f in sorted(Path(fo).glob("*.json")):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(r, dict) and "n" in r:
                recs.append(r)
    return recs


def build_report(records, budget):
    requests = load_requests()
    result = dash_eval_score.score_run(records, requests, budget)
    latest = {}
    for r in records:
        n = r["n"]
        if n not in latest or dash_eval_score._later(r, latest[n]):
            latest[n] = r
    rows, tokens = [], []
    for req in requests:
        n = req["n"]
        res = result["results"][n]
        rec = latest.get(n) or {}
        previews = sum(1 for t in rec.get("tools") or [] if t.get("name") == "preview_dashboard")
        tokens_n = int(rec.get("tokens") or 0)
        if rec:
            tokens.append(tokens_n)
        rows.append({"n": n, "points": res["points"], "total": res["total"], "gate": res["gate"],
                     "pass": res["pass"], "compared": len(rec.get("comparisons") or []),
                     "skipped": len(rec.get("skips") or []), "errors": len(rec.get("errors") or []),
                     "tokens": tokens_n, "model_calls": int(rec.get("model_calls") or 0),
                     "preview_rounds": previews, "round": res.get("round"), "notes": res.get("notes", [])})
    suggestion = int(statistics.median(tokens) * 1.25) if tokens else None
    return {"rows": rows, "result": result, "gate_line": dash_eval_score.gate_line(result),
            "total_model_calls": sum(r["model_calls"] for r in rows),
            "total_tokens": sum(r["tokens"] for r in rows), "budget_suggestion": suggestion}


def render_markdown(rep):
    L = ["# Dashboard eval report", "", rep["gate_line"], "",
         "| # | pts 1-8 | total | gate | pass | compared | skipped | errors | tokens | calls | previews | round |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        pts = " ".join(str(r["points"].get(i, r["points"].get(str(i), 0))) for i in range(1, 9))
        L.append(f"| {r['n']} | {pts} | {r['total']} | {r['gate']} | {r['pass']} | {r['compared']} | "
                 f"{r['skipped']} | {r['errors']} | {r['tokens']} | {r['model_calls']} | "
                 f"{r['preview_rounds']} | {r['round']} |")
    L += ["", f"Total model calls: {rep['total_model_calls']}; total tokens: {rep['total_tokens']}",
          f"Criterion 8 budget suggestion (median tokens per request x 1.25): {rep['budget_suggestion']}", ""]
    for r in rep["rows"]:
        if r["notes"]:
            L.append(f"- request {r['n']}: " + "; ".join(str(x) for x in r["notes"]))
    return redact("\n".join(L))


def cmd_report(args):
    records = load_records(args.out)
    rep = build_report(records, args.budget)
    prefix = Path(args.write)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    safe = json.loads(redact(json.dumps(rep, default=str)))
    Path(str(prefix) + ".json").write_text(json.dumps(safe, indent=1), encoding="utf-8")
    Path(str(prefix) + ".md").write_text(render_markdown(rep), encoding="utf-8")
    print(rep["gate_line"])
    return 0


# ------------------------------------------------------------------ cli

def build_parser():
    ap = argparse.ArgumentParser(description="Dashboard eval runner (test stacks only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "run"):
        p = sub.add_parser(name)
        p.add_argument("--stack", required=True)
        p.add_argument("--client", required=True, choices=CLIENTS)
        p.add_argument("--secrets", default="")
        if name == "setup":
            p.add_argument("--today", default="")
        else:
            p.add_argument("--requests", default="")
            p.add_argument("--runs", type=int, default=1)
            p.add_argument("--round", type=int, default=0)
            p.add_argument("--budget", type=int, default=None)
            p.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--out", action="append", required=True)
    r.add_argument("--write", required=True)
    r.add_argument("--budget", type=int, default=None)
    return ap


def main(argv=None, env=None):
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    if args.cmd == "report":
        return cmd_report(args)
    try:
        d_base, d_dash = (stack_urls(args.stack) if STACK_RE.match(args.stack) else ("", ""))
        base = (env.get("BASE_URL") or d_base).rstrip("/")
        dash = (env.get("DASH_URL") or d_dash).rstrip("/")
        check_guards(args.stack, env, base, dash)
        key = ops_key(env, args.secrets)
        if not key:
            raise Refused("no ops key (set OPS_KEY or pass --secrets <file>)")
        ctx = Ctx(args.stack, args.client, key, base, dash)
        return cmd_setup(ctx, args) if args.cmd == "setup" else cmd_run(ctx, args)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except Fail as e:
        print(f"FAILED: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
