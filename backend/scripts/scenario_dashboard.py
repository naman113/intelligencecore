"""DEPLOY-03 acceptance scenario: ask -> approve -> fetch /d/ on the dashboards origin.

DEMO STACKS ONLY. It creates a chart-spec dashboard draft (Phase 18), approves the publish action
through the ops API and fetches the published page from the dashboards origin, asserting 200 plus
the spec Content-Security-Policy (three script hashes, Trusted Types). A last step checks the
conversion path: an HTML dashboard needs conversion while dashboard_legacy_html is off, serves its
v1.0 bytes while on, and is converted to a spec draft of the same slug through the normal approval.

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p10 uv run --frozen python scripts/scenario_dashboard.py [--secrets <path>] [--keep]

Exit codes: 0 pass, 1 fail, 2 refused or unhealthy.
Env: STACK, BASE_URL, DASH_URL, UI_URL, APP_CONTAINER, PG_CONTAINER, DASH_CONTAINER,
OPS_KEY, SCENARIO_NO_LLM, SCENARIO_KEEP, SCENARIO_REPEAT, SCENARIO_TIMEOUT.
Tokens and the ops key are printed as len=N only.

Hand-off notes:
- Phase 18: tools mode needs the semantic model seeded (scripts/scenario_model.py --tools-only);
  the demo spec uses the net_sales and order_count measures. The legacy setting is restored at the end.
- When Phase 12's AGENT-07 sandboxed frame lands, only `csp_ok` and the fetched URL change.
- Phase 12's AGENT-08 turns on SCENARIO_REPEAT=1 (repeat the ask on the same thread).
- Phase 13 (GOLIVE-08) reruns the script with STACK=<its stack> --keep.
"""

import json
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTRACT_KEYS = ["_summary", "slug", "dashboard_id", "version_id", "version", "title",
                 "change_note", "path", "draft_url", "preview_expires_at", "from_version",
                 "is_new_dashboard", "queries_added", "queries_removed", "queries_changed",
                 "origin", "thread_id"]
SPEC_KEYS = ["format", "tiles_added", "tiles_removed", "tiles_changed", "tiles", "measures",
             "queries"]
LIB_HASH = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
LIB_SRC = "/d/_lib/chart.js-4.5.1/chart.umd.min.js"
INVOICE_ASK = "build a dashboard of open invoices by customer"
SKILL = "dashboard-authoring"
OPERATOR_RE = re.compile(r"^[a-z][a-z_-]{0,31}$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
CONTRACT_DOC = Path(__file__).resolve().parents[2] / "docs" / "DASHBOARD_CONTRACT.md"
HERE = Path(__file__).resolve().parent


class Fail(Exception):
    pass


# ---------------------------------------------------------------- pure helpers

def stack_config(stack: str) -> dict:
    if stack == "int":
        base = 9000
    else:
        m = re.fullmatch(r"p(\d+)", stack or "")
        if not m:
            raise ValueError(f"STACK must be 'int' or 'pN', got {stack!r}")
        base = 9000 + 10 * int(m.group(1))
    return {"ui": f"http://127.0.0.1:{base}", "base": f"http://127.0.0.1:{base + 1}",
            "dash": f"http://127.0.0.1:{base + 2}", "app": f"ic-{stack}-app-1",
            "pg": f"ic-{stack}-postgres-1", "dashc": f"ic-{stack}-dashboards-1"}


def _secret_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("OPS_KEY="):
                    return line.split("=", 1)[1].strip("\r\n \t\"'")
    except OSError:
        pass
    return ""


def load_config(env, argv) -> dict:
    cfg = stack_config(env.get("STACK", "int"))
    for key, name in (("base", "BASE_URL"), ("dash", "DASH_URL"), ("ui", "UI_URL"),
                      ("app", "APP_CONTAINER"), ("pg", "PG_CONTAINER"),
                      ("dashc", "DASH_CONTAINER")):
        if env.get(name):
            cfg[key] = env[name].rstrip("/")
    argv = list(argv)
    ops = (env.get("OPS_KEY") or "").strip("\r\n \t\"'")
    if not ops and "--secrets" in argv:
        i = argv.index("--secrets")
        if i + 1 < len(argv):
            ops = _secret_key(argv[i + 1])
    cfg["ops_key"] = ops
    cfg["no_llm"] = env.get("SCENARIO_NO_LLM") == "1"
    cfg["keep"] = env.get("SCENARIO_KEEP") == "1" or "--keep" in argv
    cfg["repeat"] = env.get("SCENARIO_REPEAT") == "1"
    cfg["timeout"] = int(env.get("SCENARIO_TIMEOUT") or 900)
    return cfg


def refusal(cfg, env):
    if env.get("SCENARIO_DEMO_STACK") != "1":
        return "set SCENARIO_DEMO_STACK=1 only on a demo stack"
    for k in ("base", "dash", "ui"):
        if (urllib.parse.urlparse(cfg[k]).hostname or "") not in LOOPBACK:
            return f"{k} URL is not loopback"
    if not cfg.get("ops_key"):
        return "no ops key (set OPS_KEY or pass --secrets <path>)"
    return None


def csp_ok(headers, kind="page", script_hashes=None, uses_lib=False, renderer_hash=None,
           lib_hash=None) -> bool:
    csp = {k.lower(): v for k, v in (headers or {}).items()}.get("content-security-policy", "")
    if not csp:
        return False
    if kind == "spec":  # Phase 18: the constant spec frame
        d = {}
        for part in csp.split(";"):
            part = part.strip()
            if part:
                name, _, val = part.partition(" ")
                d[name] = val.strip()
        script = d.get("script-src", "")
        hashes = re.findall(r"'(sha256-[^']+)'", script)
        if len(hashes) != 3 or len(set(hashes)) != 3:
            return False
        if re.sub(r"'sha256-[^']+'", "", script).strip():
            return False  # 'self', 'unsafe-inline' or anything but the three hashes
        if renderer_hash and renderer_hash not in hashes:
            return False
        if lib_hash and lib_hash not in hashes:
            return False
        style = d.get("style-src", "")
        if len(re.findall(r"'sha256-[^']+'", style)) != 1                 or re.sub(r"'sha256-[^']+'", "", style).strip():
            return False
        if d.get("require-trusted-types-for") != "'script'" or d.get("trusted-types") != "'none'":
            return False
        sandbox = d.get("sandbox", "")
        return (d.get("default-src") == "'none'" and d.get("frame-ancestors") == "'self'"
                and "allow-scripts" in sandbox and "allow-same-origin" not in sandbox
                and "'unsafe-eval'" not in csp)
    if kind == "wrapper":  # Phase 13: Phase 12 serves a constant wrapper at the page URL
        return ("default-src 'none'" in csp and "frame-src 'self'" in csp
                and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0])
    if kind == "frame":  # Phase 13: agent HTML lives at .../frame, opaque-origin sandbox
        if "sandbox allow-scripts" in csp and "allow-same-origin" in csp:
            return False
        if "sandbox allow-scripts" not in csp or "frame-ancestors 'self'" not in csp:
            return False
        kind = "page"
        csp = csp.replace("frame-ancestors 'self'", "frame-ancestors 'none'")
        headers = {"content-security-policy": csp}
        if script_hashes is not None:
            # the serve-time prelude hash is always added to script-src (one extra hash)
            m0 = re.search(r"script-src([^;]*)", csp)
            served0 = set(re.findall(r"'(sha256-[^']+)'", m0.group(1))) if m0 else set()
            extra = served0 - set(script_hashes) - {LIB_HASH}
            if len(extra) > 1 or uses_lib != (LIB_HASH in served0):
                return False
            script_hashes = None
    if kind == "data":
        return (csp.startswith("default-src 'none'") and "frame-ancestors 'none'" in csp
                and "sandbox" in csp)
    if "default-src 'none'" not in csp or "frame-ancestors 'none'" not in csp:
        return False
    if "'unsafe-eval'" in csp:
        return False
    m = re.search(r"script-src([^;]*)", csp)
    if not m:
        return False
    src = m.group(1)
    if "'unsafe-inline'" in src:
        return False
    served = set(re.findall(r"'(sha256-[^']+)'", src))
    if script_hashes is not None:
        if not served <= set(script_hashes):
            return False
        if uses_lib != (LIB_HASH in served):
            return False
    return True


def uses_library(code: str) -> bool:
    return LIB_SRC in (code or "")


def redact(text: str) -> str:
    return re.sub(r"([?&][tp]=)[^&\s]+", r"\1<redacted>", text or "")


def new_operator() -> str:
    return "scn-" + "".join(random.choices(string.ascii_lowercase, k=8))


def contract_spec_example(path=CONTRACT_DOC):
    """The json block under '## Spec example' (Phase 18)."""
    text = Path(path).read_text(encoding="utf-8")
    sect = text.split("## Spec example", 1)[1]
    return json.loads(re.search(r"```json\n(.*?)```", sect, re.S).group(1))


def walk_keys(node):
    """Every dict key anywhere in a JSON value."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_keys(v)


def contract_example(path=CONTRACT_DOC):
    text = Path(path).read_text(encoding="utf-8")
    code = re.search(r"```html\n(.*?)```", text, re.S).group(1)
    queries = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
    return code, queries


# ---------------------------------------------------------------- flow

class Scenario:
    """State and helpers for one scenario run; every step prints ok/SKIP/FAIL lines."""

    def __init__(self, cfg):
        self.c = cfg
        self.key = cfg["ops_key"]
        self.created = {}   # cleanup bookkeeping
        self.pre_ids = set()
        self.proposal_ids = []
        self.view = ""
        self.ptoken = ""
        self.created_by_run = False

    # -- transport
    def raw(self, method, url, body=None, headers=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if body is not None:
            h["content-type"] = "application/json"
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:
                    time.sleep(2)
                    continue
                return e.code, e.read(), dict(e.headers)
        raise Fail("unreachable")

    def call(self, method, path, body=None, ok=(200,)):
        st, b, _ = self.raw(method, self.c["base"] + path, body, {"x-ops-key": self.key})
        if st not in ok:
            raise Fail(f"{method} {path} -> HTTP {st}: {redact(b[:200].decode('utf-8', 'replace'))}")
        try:
            return st, (json.loads(b) if b else {})
        except ValueError:
            return st, {}

    def get(self, path):
        return self.call("GET", path)[1]

    def dget(self, path, base=None):
        return self.raw("GET", (base or self.c["dash"]) + path)

    def sh(self, args, stdin=None):
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)

    def psql(self, sql):
        r = self.sh(["docker", "exec", "-i", self.c["pg"], "psql", "-U", "agent", "-d",
                     "agent_memory", "-v", "ON_ERROR_STOP=1", "-At"], sql)
        if r.returncode != 0:
            raise Fail("psql: " + r.stderr[:300])
        return r.stdout

    def logs(self, container):
        r = self.sh(["docker", "logs", "--since", self.start_ts, container])
        return r.stdout + r.stderr

    # -- reporting
    def ok(self, step, name):
        print(f"ok {step} {name}", flush=True)

    def need(self, cond, step, name, detail=""):
        if not cond:
            raise Fail(f"{step} {name} {redact(str(detail))}")
        self.ok(step, name)

    # -- agent helpers
    def wait_run(self, run_id):
        end = time.time() + self.c["timeout"]
        while time.time() < end:
            time.sleep(3)
            for r in self.get("/api/runs?limit=50"):
                if r["id"] == run_id and r["status"] in ("done", "failed"):
                    return r
        raise Fail(f"run {run_id} did not finish in time")

    def ask(self, op, message=INVOICE_ASK):
        _, b = self.call("POST", "/api/ask", {"message": message, "operator": op})
        return b["run_id"]

    def tool_names(self, run_id):
        return [t.get("tool") or t.get("name") for t in self.get(f"/api/runs/{run_id}/tools")]

    def proposals(self):
        return self.get("/api/actions?status=proposed&kind=publish_dashboard&limit=200")

    @staticmethod
    def payload(a):
        p = a.get("payload")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        return p if isinstance(p, dict) else {}

    def poll_data(self, draft_url, qids, timeout=120):
        path, _, q = draft_url.partition("?")
        out = {}
        for qid in qids:
            end = time.time() + timeout
            while True:
                st, b, h = self.dget(f"{path}data/{qid}?{q}")
                d = json.loads(b) if st == 200 and b else {}
                if d.get("refreshed_at"):
                    out[qid] = (d, h)
                    break
                if time.time() > end:
                    raise Fail(f"data {qid} not refreshed (HTTP {st})")
                time.sleep(3)
        return out

    def config_value(self, key):
        stack = [self.get("/api/config")]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("key") == key:
                    return x.get("value")
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        return None

    def budget_value(self):
        return self.config_value("reactive_token_budget")

    def measure_names(self):
        names, stack = set(), [self.get("/api/model/measures")]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("name"), str):
                    names.add(x["name"])
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        return names

    def tool_save(self, slug, spec, note):
        """Call save_dashboard_version inside the app container; returns the action id."""
        data = {"thread": self.thread, "slug": slug, "spec": spec, "change_note": note}
        prog = (
            "import asyncio, json\n"
            f"P = json.loads({json.dumps(json.dumps(data))})\n"
            "async def main():\n"
            "    from app.agents.graph import ctx_phone\n"
            "    from app import dashboardtools as dt, db\n"
            "    ctx_phone.set(P['thread'])\n"
            "    try:\n"
            "        out = await dt.save_dashboard_version.ainvoke({k: P[k] for k in "
            "('slug', 'spec', 'change_note')})\n"
            "        print('RESULT ' + json.dumps(out))\n"
            "    finally:\n"
            "        await db.close_pool()\n"
            "asyncio.run(main())\n")
        r = self.sh(["docker", "exec", "-i", self.c["app"], "python", "-"], prog)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), "")
        if "action #" not in line:
            raise Fail("tool save refused: " + (r.stderr or line)[-300:])
        return int(re.search(r"action #(\d+)", line).group(1))

    # -- steps
    def preflight(self):
        c = self.c
        try:
            st, b, _ = self.raw("GET", c["base"] + "/health", timeout=20)
            lst, _, _ = self.raw("GET", c["dash"] + LIB_SRC, timeout=20)
        except urllib.error.URLError as e:
            print(f"UNHEALTHY: stack unreachable: {e}")
            return 2
        degraded = False
        try:
            degraded = bool(json.loads(b).get("degraded"))
        except ValueError:
            pass
        if st != 200 or degraded or lst != 200:
            print(f"UNHEALTHY: health={st} degraded={degraded} chart_lib={lst}")
            return 2
        print(f"ops key len={len(self.key)}")
        llm = self.get("/api/config/status").get("llm", {}).get("state")
        if c["no_llm"]:
            self.mode = "tools"
            print("MODE tools (SCENARIO_NO_LLM=1)")
        elif llm == "not_configured":
            self.mode = "tools"
            print("MODE tools (no LLM configured)")
        else:
            self.mode = "agent"
            print("MODE agent")
        if self.mode == "tools":
            have = self.measure_names()
            if not {"net_sales", "order_count"} <= have:
                print("seed the model first: scripts/scenario_model.py --tools-only")
                return 2
        self.op = new_operator()
        self.thread = "ui:" + self.op
        self.start_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.pre_ids = {int(x) for x in self.psql("SELECT id FROM dashboards").split()}
        self.ok("0", f"preflight mode={self.mode}")
        return 0

    def draft_agent(self):
        sk = self.get(f"/api/skills/{SKILL}")
        if not sk.get("enabled"):
            self.call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
            self.created["skill_was_disabled"] = True
        run_id = self.ask(self.op)
        run = self.wait_run(run_id)
        if run["status"] == "failed" and "token budget" in str(run.get("error")):
            self.created["budget"] = self.budget_value()
            print("BUDGET run failed on token budget; raising reactive_token_budget to 400000")
            self.call("PUT", "/api/config", {"values": {"reactive_token_budget": "400000"}})
            run_id = self.ask(self.op)
            run = self.wait_run(run_id)
        self.need(run["status"] == "done", "1", "agent run done", str(run.get("error"))[:200])
        names = self.tool_names(run_id)
        self.need("delegate_to_dashboard_agent" in names and "save_dashboard_version" in names,
                  "1", "agent delegated and saved", str(names))
        self.run_id = run_id

    def draft_tools(self):
        letters = self.op.replace("scn-", "")
        self.action_id = self.tool_save(f"scenario-{letters}", contract_spec_example(),
                                        "scenario tools-mode spec draft")
        self.ok("1", "tools-mode spec draft saved")

    def proposal(self):
        if self.mode == "agent":
            cands = [a for a in self.proposals() if a.get("task_run_id") == self.run_id]
            self.need(bool(cands), "2", "publish_dashboard proposal for the run")
            act = max(cands, key=lambda a: a["id"])
        else:
            act = next((a for a in self.proposals() if a["id"] == self.action_id), None)
            self.need(act is not None, "2", "publish_dashboard proposal #%d" % self.action_id)
        p = self.payload(act)
        missing = [k for k in CONTRACT_KEYS if k not in p]
        self.need(not missing, "2", "payload has every contract key", str(missing))
        missing = [k for k in SPEC_KEYS if k not in p]
        self.need(not missing and p.get("format") == "spec", "2",
                  "payload is a spec payload (format, tile diff, measures, queries)", str(missing))
        self.need(p.get("thread_id") == self.thread, "2",
                  "payload thread_id is the scenario thread", str(p.get("thread_id")))
        self.act = act
        self.slug, self.version = p["slug"], p["version"]
        self.dashboard_id = p["dashboard_id"]
        self.draft_url = p["draft_url"]
        m = re.search(r"[?&]p=([^&]+)", self.draft_url)
        self.ptoken = m.group(1) if m else ""
        self.proposal_ids.append(act["id"])
        self.created_by_run = self.dashboard_id not in self.pre_ids
        if self.mode == "tools":
            self.need(p["is_new_dashboard"] is True and self.created_by_run, "2",
                      "new dashboard created by this run")
        else:
            print(f"INFO is_new_dashboard={p['is_new_dashboard']} "
                  f"slug_preexisted={not self.created_by_run}")
        v = self.get(f"/api/dashboards/{self.slug}/versions/{self.version}")
        self.qids = [q["id"] for q in v["queries"]]
        self.need(v.get("format") == "spec", "2", "saved version is a chart spec",
                  str(v.get("format")))
        meas = [q for q in v["queries"] if q.get("source") == "measure"
                and str(q["id"]).startswith("m_")]
        if self.mode == "tools":
            self.need(bool(meas) and any("CURRENT_DATE" in str(q.get("sql", "")) for q in meas),
                      "2", "compiled m_ measure queries frozen with CURRENT_DATE")
        else:
            print(f"INFO measure queries={len(meas)}")
        self.spec = v.get("spec") or {}
        self.ntiles = len(self.spec.get("tiles", []))
        self.need(self.ntiles >= 1, "2", "spec has tiles")
        print(f"   slug={self.slug} v{self.version} action={act['id']} "
              f"preview token len={len(self.ptoken)}")

    def repeat(self):
        if not self.c["repeat"]:
            print("SKIP 2b repeat (SCENARIO_REPEAT unset)")
            return
        if self.mode != "agent":
            print("SKIP 2b repeat (tools mode)")
            return
        rid = self.ask(self.op)
        self.wait_run(rid)
        new = [a for a in self.proposals() if a.get("task_run_id") == rid]
        for a in new:
            self.proposal_ids.append(a["id"])
        good = bool(new)
        if not good:
            msgs = self.get(f"/api/ask?operator={self.op}&limit=50").get("messages", [])
            reply = next((m for m in reversed(msgs) if m.get("role") == "assistant"), {})
            ids = {int(x) for x in re.findall(r"#(\d+)", str(reply.get("content", "")))}
            live = {a["id"] for a in self.proposals()}
            good = bool(ids & live)
        self.need(good, "2b", "repeat ask yields a real pending proposal",
                  "supervisor reported a draft it did not create (AGENT-08)")

    @staticmethod
    def frame_of(url):
        """Phase 13: the content-frame URL of a page URL (query string kept)."""
        path, sep, q = url.partition("?")
        return path.rstrip("/") + "/frame" + sep + q

    @staticmethod
    def spec_frame_ok(headers):
        return csp_ok(headers, "spec", lib_hash=LIB_HASH)

    def draft_page(self):
        st, _, h = self.dget(self.draft_url)
        self.need(st == 200 and csp_ok(h, "wrapper"), "3",
                  "draft wrapper 200 with CSP", f"status={st}")
        st, b, h = self.dget(self.frame_of(self.draft_url))
        self.need(st == 200 and self.spec_frame_ok(h), "3",
                  "draft frame 200 with the spec CSP", f"status={st}")
        cc = {k.lower(): v for k, v in h.items()}.get("cache-control", "")
        self.need("no-store" in cc, "3", "draft frame is no-store")
        body = b.decode("utf-8", "replace")
        titles = [self.spec.get("title", "")] + [t.get("title") for t in self.spec.get("tiles", [])]
        self.need(not any(t and t in body for t in titles), "3",
                  "frame bytes carry no spec text")
        path, _, q = self.draft_url.partition("?")
        st, sb, _ = self.dget(f"{path}spec?{q}")
        sj = json.loads(sb) if st == 200 and sb else {}
        self.need(st == 200 and bool(sj.get("tiles")) and "sql" not in set(walk_keys(sj)), "3",
                  "./spec JSON has tiles and no sql key", f"status={st}")

    def draft_data(self):
        res = self.poll_data(self.draft_url, self.qids)
        for qid, (d, h) in res.items():
            self.need(bool(d.get("rows")) and d.get("error") is None and csp_ok(h, "data"),
                      "4", f"draft data {qid} has rows and data CSP")

    def render(self):
        r = self.sh(["node", str(HERE / "dashboard_render_check.mjs"),
                     self.c["dash"] + self.draft_url, "--expect-tiles", str(self.ntiles)])
        if r.returncode == 3:
            print("SKIP 5 render: no browser")
            return
        self.need(r.returncode == 0, "5", "render check", r.stdout[-300:])

    def approve(self):
        i = self.act["id"]
        st, b, _ = self.raw("POST", f"{self.c['base']}/api/actions/{i}/approve", {},
                            {"x-ops-key": self.key})
        self.need(st == 200, "6", "approve 200", f"{st} {b[:200]!r}")
        self.proposal_ids.remove(i)
        st2, _, _ = self.raw("POST", f"{self.c['base']}/api/actions/{i}/approve", {},
                             {"x-ops-key": self.key})
        self.need(st2 == 409, "6", "second approve gives 409", str(st2))
        d = self.get(f"/api/dashboards/{self.slug}")["dashboard"]
        self.need(d.get("current_version") == self.version, "6",
                  "current version is the approved draft")
        self.view = d["view_token"]
        print(f"   view token len={len(self.view)}")

    def fetch(self):
        v, t = self.version, self.view
        st, _, h = self.dget(f"/d/{self.slug}/?t={t}")
        self.need(st == 200 and csp_ok(h, "wrapper"), "7", "/d/ wrapper 200 + CSP", f"status={st}")
        st, _, h = self.dget(self.frame_of(f"/d/{self.slug}/?t={t}"))
        self.need(st == 200 and self.spec_frame_ok(h), "7",
                  "/d/ frame 200 + spec CSP", f"status={st}")
        st, _, h = self.dget(self.frame_of(f"/d/{self.slug}/v/{v}/?t={t}"))
        cc = {k.lower(): x for k, x in h.items()}.get("cache-control", "")
        self.need(st == 200 and self.spec_frame_ok(h)
                  and "immutable" in cc, "7", "pinned version frame is immutable with CSP",
                  f"status={st}")
        st, b, h = self.dget(f"/d/{self.slug}/data/{self.qids[0]}?t={t}")
        dj = json.loads(b) if st == 200 and b else {}
        cc = {k.lower(): x for k, x in h.items()}.get("cache-control", "")
        self.need(st == 200 and bool(dj.get("rows")) and csp_ok(h, "data") and "no-store" in cc,
                  "7", "data route rows with data CSP", f"status={st}")

    def isolation(self):
        w = "wrong-scenario-token"
        st, _, _ = self.dget(f"/d/{self.slug}/?t={w}")
        self.need(st == 404, "8", "wrong token gives 404", str(st))
        st, _, _ = self.dget(f"/d/{self.slug}/?t={w}", self.c["base"])
        self.need(st == 404, "8", "gateway origin refuses /d/", str(st))
        st, b, _ = self.dget(f"/d/{self.slug}/?t={w}", self.c["ui"])
        body = b.decode("utf-8", "replace")
        title = self.get(f"/api/dashboards/{self.slug}/versions/{self.version}").get("title", "")
        self.need(LIB_SRC not in body and (not title or f"<title>{title}</title>" not in body),
                  "8", "UI origin never serves the dashboard code")

    def selftest(self):
        r = self.sh(["docker", "exec", self.c["app"], "python", "-m", "app.selftest"])
        out = r.stdout + r.stderr
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("✅")]
        dl = next((ln for ln in lines if "Dashboards origin" in ln), "")
        cl = next((ln for ln in lines if "Context store" in ln), "")
        print("   " + redact(dl))
        print("   " + redact(cl))
        self.need(bool(dl), "9", "selftest Dashboards origin ok")
        self.need(bool(cl), "9", "selftest Context store ok")
        self.need(self.view not in out, "9", "selftest output has no view token")
        st, b, _ = self.raw("POST", self.c["base"] + "/api/config/test/dashboards", {},
                            {"x-ops-key": self.key})
        txt = b.decode("utf-8", "replace")
        try:
            okf = json.loads(txt).get("ok") is True
        except ValueError:
            okf = False
        self.need(st == 200 and okf and self.view not in txt, "9b",
                  "config test dashboards ok:true without the token", f"{st} {txt[:200]}")
        print("   " + redact(txt[:200]))
        print(f"INFO probe names this run's slug: {self.slug in txt}")

    def hygiene(self):
        app, dashc = self.logs(self.c["app"]), self.logs(self.c["dashc"])
        for name, tok in (("view", self.view), ("preview", self.ptoken)):
            self.need(bool(tok) and tok not in app and tok not in dashc, "10",
                      f"{name} token absent from app and dashboards logs")
        req = [ln for ln in app.splitlines() if "HTTP Request:" in ln]
        probe = [ln for ln in req if "/d/" in ln and "t=" in ln]
        self.need(bool(probe), "10", "probe request logged by the gateway")
        bad = [ln for ln in req if re.search(r"[?&][tp]=(?!REDACTED)", ln)]
        self.need(not bad, "10", "no HTTP Request line carries a token",
                  redact(bad[0]) if bad else "")
        print("ok 10 probe request logged with the token redacted")
        if self.mode == "agent":
            hay = json.dumps(self.get(f"/api/runs/{self.run_id}/tools"))
            replies = json.dumps(self.get(f"/api/ask?operator={self.op}&limit=50"))
            both = hay + replies
            self.need(self.view not in both and self.ptoken not in both
                      and "?p=" not in replies and "?t=" not in replies, "10",
                      "no token in tool results or assistant replies")

    def person_id(self):
        out = self.psql("SELECT id FROM people WHERE is_primary AND active LIMIT 1").strip()
        return int(out) if out else None

    def set_legacy(self, on):
        self.call("PUT", "/api/config", {"values": {"dashboard_legacy_html": "true" if on else "false"}})

    def frame_text(self, slug, token):
        st, b, h = self.dget(f"/d/{slug}/frame?t={token}")
        return st, b.decode("utf-8", "replace"), h

    def convert(self):
        """SPEC-06: HTML dashboard -> needs conversion (legacy off) / v1.0 bytes (on) -> spec draft."""
        pid = self.person_id()
        if pid is None:
            print("SKIP 11 conversion: no primary active person to publish the HTML dashboard")
            return
        self.legacy_orig = self.config_value("dashboard_legacy_html")
        letters = self.op.replace("scn-", "")
        slug = f"scenario-legacy-{letters}"
        self.legacy_slug = slug
        code, queries = contract_example()
        data = {"slug": slug, "code": code, "queries": queries}
        prog = (
            "import asyncio, json\n"
            f"P = json.loads({json.dumps(json.dumps(data))})\n"
            "async def main():\n"
            "    from app import dashboards, db\n"
            "    pool = await db.get_pool()\n"
            "    try:\n"
            "        async with pool.acquire() as conn:\n"
            "            r = await dashboards.save_version(conn, slug=P['slug'], title='Scenario legacy',"
            " code=P['code'], queries=P['queries'], change_note='scenario legacy html', author='operator')\n"
            "        print('RESULT ' + json.dumps({'id': r['dashboard_id'], 'version': r['version']}))\n"
            "    finally:\n"
            "        await db.close_pool()\n"
            "asyncio.run(main())\n")
        r = self.sh(["docker", "exec", "-i", self.c["app"], "python", "-"], prog)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), "")
        self.need(bool(line), "11", "HTML dashboard created", (r.stderr or r.stdout)[-300:])
        info = json.loads(line[7:])
        self.legacy_id = info["id"]
        self.set_legacy(True)
        self.call("POST", f"/api/dashboards/{slug}/publish", {"version": info["version"], "decided_by": pid})
        view = self.get(f"/api/dashboards/{slug}")["dashboard"]["view_token"]
        st, body, _ = self.frame_text(slug, view)
        self.need(st == 200 and "<canvas" in body, "11", "legacy on: v1.0 HTML bytes served")
        self.set_legacy(False)
        st, body, _ = self.frame_text(slug, view)
        self.need(st == 200 and "<script" not in body and "needs conversion" in body, "11",
                  "legacy off: needs-conversion page, no script")
        rows = self.get("/api/dashboards")
        rows = rows if isinstance(rows, list) else rows.get("dashboards", [])
        mine = next((d for d in rows if d.get("slug") == slug), {})
        self.need(mine.get("needs_conversion") is True, "11", "list marks it needs_conversion")
        spec = contract_spec_example()
        if self.mode == "tools":
            aid = self.tool_save(slug, spec, "scenario conversion to a chart spec")
            act = next((a for a in self.proposals() if a["id"] == aid), None)
        else:
            rid = self.ask(self.op, f"convert the dashboard {slug} into a chart spec")
            self.wait_run(rid)
            names = self.tool_names(rid)
            self.need("get_dashboard" in names and "save_dashboard_version" in names, "11",
                      "agent read the dashboard and saved a spec", str(names))
            cands = [a for a in self.proposals() if a.get("task_run_id") == rid
                     and self.payload(a).get("slug") == slug]
            act = max(cands, key=lambda a: a["id"]) if cands else None
        self.need(act is not None, "11", "conversion proposal for the same slug")
        self.proposal_ids.append(act["id"])
        p = self.payload(act)
        self.need(p.get("format") == "spec" and p.get("slug") == slug, "11", "proposal is a spec draft")
        if self.mode == "tools":
            ids = sorted(t["id"] for t in spec["tiles"])
            self.need(p.get("from_version") == 1 and p.get("is_new_dashboard") is False
                      and sorted(p.get("tiles_added") or []) == ids, "11",
                      "conversion diff: from v1, not new, every tile added",
                      f"{p.get('from_version')} {p.get('tiles_added')}")
        st, _, _ = self.raw("POST", f"{self.c['base']}/api/actions/{act['id']}/approve", {},
                            {"x-ops-key": self.key})
        self.need(st == 200, "11", "approve conversion", str(st))
        self.proposal_ids.remove(act["id"])
        rows = self.get("/api/dashboards")
        rows = rows if isinstance(rows, list) else rows.get("dashboards", [])
        mine = next((d for d in rows if d.get("slug") == slug), {})
        self.need(mine.get("needs_conversion") is False, "11", "needs_conversion cleared after approval")

    def cleanup(self):
        if getattr(self, "legacy_orig", "unset") != "unset":
            try:
                o = str(self.legacy_orig).strip().lower()
                self.set_legacy(o == "true")
            except Fail as e:
                print("cleanup legacy setting:", e)
        lslug = getattr(self, "legacy_slug", None)
        if lslug and not self.c["keep"]:
            try:
                safe = re.sub(r"[^a-z0-9_-]", "", lslug)
                self.psql(f"DELETE FROM dashboards WHERE slug = '{safe}'")
                print(f"cleanup dashboard {lslug} removed")
            except Fail as e:
                print("cleanup:", e)
        for i in list(self.proposal_ids):
            try:
                self.raw("POST", f"{self.c['base']}/api/actions/{i}/reject", {},
                         {"x-ops-key": self.key})
            except Exception as e:  # noqa: BLE001
                print("cleanup reject:", e)
        try:
            if self.created.get("skill_was_disabled"):
                self.call("PATCH", f"/api/skills/{SKILL}", {"enabled": False})
            if self.created.get("budget") is not None:
                self.call("PUT", "/api/config",
                          {"values": {"reactive_token_budget": (
                              "" if str(self.created["budget"]) in ("150000", "300000")
                              else str(self.created["budget"]))}})  # Phase 13: str values; defaults cleared
        except Fail as e:
            print("cleanup:", e)
        slug = getattr(self, "slug", None)
        if not slug:
            return
        if self.c["keep"]:
            print(f"cleanup dashboard {slug} kept (--keep)")
        elif not self.created_by_run:
            print(f"cleanup dashboard {slug} kept (existed before this run; "
                  f"this run added v{self.version})")
        else:
            try:
                safe = re.sub(r"[^a-z0-9_-]", "", slug)
                self.psql(f"DELETE FROM dashboards WHERE id = {int(self.dashboard_id)} "
                          f"AND slug = '{safe}'")
                print(f"cleanup dashboard {slug} removed")
            except Fail as e:
                print("cleanup:", e)


def run(cfg) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    s = Scenario(cfg)
    try:
        rc = s.preflight()
    except Fail as e:
        print(f"FAIL 0 {e}")
        return 1
    if rc:
        return rc
    step = "?"
    try:
        for step, fn in (("1 draft", s.draft_agent if s.mode == "agent" else s.draft_tools),
                         ("2 proposal", s.proposal), ("2b repeat", s.repeat),
                         ("3 draft page", s.draft_page), ("4 draft data", s.draft_data),
                         ("5 render", s.render), ("6 approve", s.approve), ("7 fetch", s.fetch),
                         ("8 isolation", s.isolation), ("9 selftest", s.selftest),
                         ("10 hygiene", s.hygiene), ("11 convert", s.convert)):
            fn()
        print(f"PASS scenario_dashboard ({s.mode} mode)")
        return 0
    except Fail as e:
        print(f"FAIL {e}")
        print(f"FAIL scenario_dashboard: {step}")
        return 1
    finally:
        s.cleanup()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = load_config(os.environ, argv)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    why = refusal(cfg, os.environ)
    if why:
        print(f"REFUSED: {why}")
        return 2
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
