"""GOLIVE-08 dress rehearsal driver (Phase 13). DEMO STACKS ONLY.

Does what an owner would do on the demo database, through the same API the UI uses:
upload the fixture context pack (CTX-08 path), approve every proposal through the actions
gate, ask 10 known-answer questions, turn "Track this daily." into a watch with history,
and run the dashboard scenario. Nothing is written to the database directly; the stack's
Postgres is only read (SELECT counts) and the demo business DB is only read for ground truth.

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p13 uv run --frozen python scripts/scenario_rehearsal.py \
      --secrets <path> --out <file> [--allow-used]

Exit codes: 0 PASS, 1 FAIL, 2 refused or stack not fresh.
Keys and tokens are never printed or written.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rehearsal_match  # noqa: E402
import scenario_dashboard as sd  # noqa: E402
from scenario_dashboard import OPERATOR_RE, load_config, redact, refusal  # noqa: E402,F401

BACKEND = HERE.parent
FIX = BACKEND / "tests" / "fixtures"
ASK_TS = BACKEND.parent / "frontend" / "src" / "lib" / "ask.ts"
PACK = "context_pack_rehearsal.json"
QUESTIONS = "rehearsal_questions.json"
DEMO_DB = "ic-demo-db"
SKILLS = ("context-pack-import", "dashboard-authoring")
IMPORT_KINDS = ("context_change", "create_watch", "hide_table")
TABLES = ["regions", "sales_reps", "customers", "products", "sales_orders", "order_lines",
          "invoices", "payments", "inventory_snapshots"]
ROLES = ("supervisor", "onboarding", "context", "document", "research", "triage")
DASH_PASS = "PASS scenario_dashboard (agent mode)"
RUN_OP = "rehearsal"
UPLOAD_OP = "owner"
DASH_OK_LINES = 40


def _load_instruction() -> str:
    text = ASK_TS.read_text(encoding="utf-8")
    m = re.search(r"importContextPack:\s*\(\)\s*=>\s*\n?\s*'((?:[^'\\]|\\.)*)'", text)
    return m.group(1) if m else ""


INSTRUCTION = _load_instruction()


# ---------------------------------------------------------------- pure helpers

def fresh_stack_problems(counts: dict) -> list:
    names = {"approved_context": "approved context rows exist",
             "create_watch_actions": "create_watch actions exist",
             "dashboards": "dashboards exist"}
    out = []
    for key, label in names.items():
        try:
            n = int(counts.get(key) or 0)
        except (TypeError, ValueError):
            n = 1
        if n:
            out.append(f"{label} ({key}={n})")
    return out


def pick_reply(messages, after_id):
    best = None
    for m in messages or []:
        if m.get("role") == "assistant" and (m.get("id") or 0) > (after_id or 0):
            if best is None or m["id"] > best["id"]:
                best = m
    return best["content"] if best else None


def _payload(a):
    p = a.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def import_action_ids(actions, run_id):
    return [a["id"] for a in actions or []
            if a.get("status", "proposed") == "proposed" and a.get("task_run_id") == run_id
            and a.get("kind") in IMPORT_KINDS]


def dashboard_verdict(exit_code, output):
    if exit_code == 3:
        return "FAIL", "no browser"
    if exit_code == 0 and DASH_PASS in (output or "") and "BUDGET" not in (output or ""):
        return "PASS", ""
    if "BUDGET" in (output or ""):
        return "FAIL", "BUDGET tripped"
    return "FAIL", f"exit {exit_code}"


def track_evidence(watch: dict, hist: dict) -> str:
    """Which measure the watch recorded: metric, trigger column, value_column, condition."""
    watch = watch or {}
    rule = watch.get("trigger_rule")
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except ValueError:
            rule = {}
    if not isinstance(rule, dict):
        rule = {}
    latest = ((hist or {}).get("latest") or {})
    cond = " ".join(str(watch.get("condition") or "").split())[:160]
    return (f"metric={latest.get('metric') or '-'} trigger={rule.get('column') or '-'} "
            f"value_column={rule.get('value_column') or '-'} condition={cond}")


def _cell(v):
    return str(v if v is not None else "").replace("|", "/").replace("\n", " ")


def render_results(state: dict) -> str:
    L = ["# Phase 13 dress rehearsal results (GOLIVE-08)", "",
         f"Date: {state.get('date', date.today().isoformat())}  ",
         f"Stack: {state.get('stack', '')}  App commit: {state.get('commit', '')}", "",
         "## Per-role models", "",
         "| Role | Model | Effort | Source |", "|---|---|---|---|"]
    for r in state.get("roles", []):
        L.append(f"| {_cell(r.get('role'))} | {_cell(r.get('model'))} | {_cell(r.get('effort'))} "
                 f"| {_cell(r.get('source'))} |")
    L += ["", "## Steps", "", "| Step | Name | Result | Seconds | Evidence |", "|---|---|---|---|---|"]
    for s in state.get("steps", []):
        L.append(f"| {_cell(s['step'])} | {_cell(s['name'])} | {_cell(s['result'])} "
                 f"| {round(float(s.get('seconds') or 0), 1)} | {_cell(s.get('evidence'))[:400]} |")
    L += ["", "## Questions", "",
          "| Id | Question | Expected | Result | Retried | Seconds | Tools | Reply excerpt |",
          "|---|---|---|---|---|---|---|---|"]
    qs = state.get("questions", [])
    for q in qs:
        L.append(f"| {_cell(q['id'])} | {_cell(q.get('ask'))} | {_cell(q.get('expected'))} "
                 f"| {_cell(q['result'])} | {'yes' if q.get('retried') else 'no'} "
                 f"| {round(float(q.get('seconds') or 0), 1)} | {_cell(', '.join(q.get('tools') or []))} "
                 f"| {_cell(q.get('excerpt'))} |")
    passed = sum(1 for q in qs if q["result"] == "PASS")
    L += ["", f"Questions passed: {passed}/{len(qs)}", ""]
    reasons = list(state.get("reasons") or [])
    if reasons:
        L.append(f"Verdict: FAIL scenario_rehearsal ({'; '.join(reasons)})")
    else:
        L.append("Verdict: PASS scenario_rehearsal")
    text = "\n".join(L) + "\n"
    return redact(text)


# ---------------------------------------------------------------- driver

class Fail(Exception):
    pass


class Rehearsal:
    def __init__(self, cfg, out, allow_used):
        self.c = cfg
        self.out = out
        self.allow_used = allow_used
        self.state = {"date": date.today().isoformat(), "stack": os.environ.get("STACK", ""),
                      "commit": "", "roles": [], "steps": [], "questions": [], "reasons": []}
        self.truth = {}

    # -- http
    def raw(self, method, path, body=None, headers=None, data=None, timeout=120):
        h = {"x-ops-key": self.c["ops_key"]}
        h.update(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            h["content-type"] = "application/json"
        req = urllib.request.Request(self.c["base"] + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def call(self, method, path, body=None, ok=(200,), **kw):
        st, b = self.raw(method, path, body, **kw)
        if st not in ok:
            raise Fail(f"{method} {path} -> HTTP {st}: {redact(b[:300].decode('utf-8', 'replace'))}")
        return json.loads(b) if b else {}

    def get(self, path):
        return self.call("GET", path)

    # -- shell
    def sh(self, args, stdin=None, timeout=120):
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=timeout)

    def agent_db(self, sql):
        r = self.sh(["docker", "exec", "-i", self.c["pg"], "psql", "-U", "agent", "-d",
                     "agent_memory", "-v", "ON_ERROR_STOP=1", "-At"], stdin=sql)
        if r.returncode:
            raise Fail("agent psql: " + r.stderr[:200])
        return r.stdout.strip()

    def demo_db(self, sql):
        r = self.sh(["docker", "exec", DEMO_DB, "psql", "-U", "demo_ro", "-d", "demo_business",
                     "-X", "-A", "-t", "-c", sql])
        if r.returncode:
            raise Fail("demo psql: " + r.stderr[:200])
        return r.stdout.strip()

    # -- bookkeeping
    def step(self, step, name, result, seconds, evidence=""):
        self.state["steps"].append({"step": step, "name": name, "result": result,
                                    "seconds": seconds, "evidence": evidence})
        print(f"{result} {step} {name} ({seconds:.0f}s) {redact(str(evidence))[:200]}", flush=True)
        if result == "FAIL":
            self.state["reasons"].append(f"step {step} {name}")

    def timed(self, step, name, fn):
        t = time.time()
        try:
            ev = fn()
            self.step(step, name, "PASS", time.time() - t, ev or "")
            return True
        except Fail as e:
            self.step(step, name, "FAIL", time.time() - t, str(e))
        except Exception as e:  # noqa: BLE001
            self.step(step, name, "FAIL", time.time() - t, f"{type(e).__name__}: {e}")
        return False

    def tools(self, run_id):
        return [t.get("tool") or t.get("name") for t in self.get(f"/api/runs/{run_id}/tools")]

    def wait_run(self, run_id, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for r in self.get("/api/runs?limit=50"):
                if r["id"] == run_id and r["status"] in ("done", "failed"):
                    if r["status"] == "failed":
                        raise Fail(f"run {run_id} failed: {redact(str(r.get('error'))[:300])}")
                    return r
            time.sleep(4)
        raise Fail(f"run {run_id} did not finish in {timeout}s")

    def ask(self, text, operator=RUN_OP, timeout=600):
        b = self.call("POST", "/api/ask", {"message": text, "operator": operator})
        run_id, mid = b["run_id"], b.get("message_id") or 0
        self.wait_run(run_id, timeout)
        time.sleep(1)
        msgs = self.get(f"/api/ask?operator={operator}&limit=50").get("messages", [])
        return run_id, pick_reply(msgs, mid) or ""

    # -- steps
    def s1_preflight(self):
        h = self.get("/health")
        flat = {}
        for items in self.get("/api/config").get("config", {}).values():
            for it in items:
                flat[it["key"]] = it
        for role in ROLES:
            m, e = flat.get(f"{role}_model", {}), flat.get(f"{role}_effort", {})
            self.state["roles"].append({"role": role, "model": m.get("effective") or m.get("value"),
                                        "effort": e.get("effective") or e.get("value"),
                                        "source": m.get("source")})
        c = {"approved_context": self.agent_db("SELECT count(*) FROM business_context WHERE status='approved'"),
             "create_watch_actions": self.agent_db("SELECT count(*) FROM actions WHERE kind='create_watch'"),
             "dashboards": self.agent_db("SELECT count(*) FROM dashboards")}
        problems = fresh_stack_problems(c)
        if problems and not self.allow_used:
            raise Fail("stack is not fresh: " + ", ".join(problems))
        try:
            self.state["commit"] = self.sh(["git", "-C", str(BACKEND.parent), "rev-parse", "--short", "HEAD"]).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        return f"health={str(h)[:60]} counts={c}"

    def s2_skills(self):
        done = []
        for name in SKILLS:
            sk = self.get(f"/api/skills/{name}")
            if not sk.get("enabled"):
                self.call("PATCH", f"/api/skills/{name}", {"enabled": True})
                done.append(name)
        return "enabled: " + (", ".join(done) or "already on")

    def upload(self):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("operator", UPLOAD_OP), ("instruction", INSTRUCTION)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{PACK}"\r\n'
                      "Content-Type: application/json\r\n\r\n").encode()
                     + (FIX / PACK).read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        st, b = self.raw("POST", "/api/documents/upload", headers={
            "content-type": f"multipart/form-data; boundary={boundary}"}, data=b"".join(parts))
        if st != 200:
            raise Fail(f"upload -> HTTP {st}: {redact(b[:200].decode('utf-8', 'replace'))}")
        return json.loads(b)

    def s3_upload(self):
        if not INSTRUCTION:
            raise Fail("could not read importContextPack from ask.ts")
        resp = self.upload()
        rid = resp.get("queued")
        if not rid:
            raise Fail(f"upload not queued: {resp}")
        self.import_run = rid
        t = time.time()
        self.wait_run(rid, 1800)
        tools = self.tools(rid)
        if "delegate_to_context_agent" not in tools:
            raise Fail(f"no context delegation, tools={tools}")
        if "extract_document" in tools:
            raise Fail("extract_document was used on the pack")
        self.import_seconds = time.time() - t
        return f"run {rid} done in {self.import_seconds:.0f}s tools={tools}"

    def s4_approve(self):
        acts = self.get("/api/actions?status=proposed&limit=500")
        ids = import_action_ids(acts, self.import_run)
        if not ids:
            raise Fail("no import proposals found for the upload run")
        kinds = {}
        by_id = {a["id"]: a for a in acts}
        for i in ids:
            self.call("POST", f"/api/actions/{i}/approve", {}, ok=(200,))
            k = by_id[i]["kind"]
            kinds[k] = kinds.get(k, 0) + 1
        tables = self.get("/api/context/tables?limit=200")["tables"]
        approved = {t["ref"].rsplit(".", 1)[-1] for t in tables
                    if t.get("status") == "approved" and t.get("description")}
        missing = [t for t in TABLES if t not in approved]
        if missing:
            raise Fail(f"tables not approved with descriptions: {missing}")
        pack = json.loads((FIX / PACK).read_text(encoding="utf-8"))
        rules = {r["ref"] for r in self.get("/api/context/rules?limit=200")["rules"]
                 if r.get("status") == "approved"}
        want = {x["propose"]["ref"] for x in pack.get("rules", []) + pack.get("kpis", [])}
        if want - rules:
            raise Fail(f"rules/KPIs missing: {sorted(want - rules)}")
        watches = {w["title"] for w in self.get("/api/watches")}
        wm = [w["title"] for w in pack.get("watch_candidates", []) if w["title"] not in watches]
        if wm:
            raise Fail(f"watches missing: {wm}")
        return f"approved {len(ids)} proposals {kinds}; 9 tables, {len(want)} rules/KPIs, {len(pack.get('watch_candidates', []))} watches"

    def s6_track(self):
        q6 = next(q for q in self.state["questions"] if q["id"] == "q6")
        truth = self.truth["q6"]
        rid, reply = self.ask("Track this daily.")
        tools = self.tools(rid)
        if "propose_kpi" not in tools:
            raise Fail(f"propose_kpi not used, tools={tools}")
        acts = self.get("/api/actions?status=proposed&limit=500")
        mine = [a for a in acts if a.get("task_run_id") == rid and a.get("kind") == "create_watch"
                and _payload(a).get("origin") == "chat"]
        if not mine:
            raise Fail("no create_watch proposal with origin chat for this run")
        a = mine[0]
        title = _payload(a).get("title")
        res = self.call("POST", f"/api/actions/{a['id']}/approve", {})
        wid = None
        if isinstance(res, dict):
            inner = res.get("result") if isinstance(res.get("result"), dict) else {}
            wid = res.get("watch_id") or inner.get("watch_id")
        if not wid:
            for w in self.get("/api/watches"):
                if w["title"] == title:
                    wid = w["id"]
                    break
        if not wid:
            raise Fail(f"watch not found after approval (title={title})")
        t0 = time.time()
        end = t0 + 300
        hist = {}
        while time.time() < end:
            hist = self.get(f"/api/watches/{wid}/history")
            if hist.get("points"):
                break
            time.sleep(10)
        w = next((x for x in self.get("/api/watches") if x["id"] == wid), {})
        if not hist.get("points"):
            raise Fail(f"watch {wid} has no history after 300s compile_error={w.get('compile_error')}; "
                       + track_evidence(w, hist))
        latest = (hist.get("latest") or {}).get("value")
        ev = track_evidence(w, hist)
        if latest is None or float(latest) != float(truth):
            raise Fail(f"watch {wid} latest={latest} truth={truth}; {ev}")
        self.track = {"watch_id": wid, "first_value": latest, "seconds": time.time() - t0,
                      "evidence": ev}
        return (f"propose_kpi used; create_watch #{a['id']} approved; watch {wid} first history "
                f"value {latest} equals truth {truth} after {time.time() - t0:.0f}s; {ev}")

    def s7_dashboard(self):
        env = dict(os.environ, SCENARIO_DEMO_STACK="1", STACK=os.environ.get("STACK", ""))
        args = [sys.executable, str(HERE / "scenario_dashboard.py"), "--keep"]
        if self.secrets:
            args[2:2] = ["--secrets", self.secrets]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=1500)
        out = (r.stdout or "") + (r.stderr or "")
        verdict, note = dashboard_verdict(r.returncode, out)
        tail = " / ".join(redact(out).strip().splitlines()[-DASH_OK_LINES:])
        if verdict != "PASS":
            raise Fail(f"{note}: {tail[-400:]}")
        return f"{DASH_PASS}; {tail[-300:]}"

    def run(self, secrets):
        self.secrets = secrets
        self.import_run = None
        if not self.timed("1", "preflight", self.s1_preflight):
            return self.finish()
        self.timed("2", "skills enabled", self.s2_skills)
        if self.timed("3", "upload context pack", self.s3_upload):
            self.timed("4", "approve proposals", self.s4_approve)
        qs = json.loads((FIX / QUESTIONS).read_text(encoding="utf-8"))
        t = time.time()
        order = list(qs)
        # run q1..q6, then track, then the rest; the questions share the "rehearsal" thread
        first = order[:6]
        rest = order[6:]
        self._ask_questions(first)
        self.timed("6", "track this", self.s6_track)
        self._ask_questions(rest)
        fails = [q["id"] for q in self.state["questions"] if q["result"] != "PASS"]
        qres = "PASS" if not fails and len(self.state["questions"]) == len(order) else "FAIL"
        self.step("5", "ten known-answer questions", qres, time.time() - t,
                  f"{sum(1 for q in self.state['questions'] if q['result'] == 'PASS')}/{len(order)} passed"
                  + (f"; failed {fails}" if fails else ""))
        self.timed("7", "dashboard scenario", self.s7_dashboard)
        return self.finish()

    def _ask_questions(self, qs):
        for q in qs:
            t = time.time()
            rec = {"id": q["id"], "ask": q["ask"], "retried": False, "expected": None,
                   "tools": [], "excerpt": ""}
            try:
                exp = self.demo_db(q["sql"])
                if q["match"] == "int":
                    exp = int(exp)
                elif q["match"] == "money":
                    exp = float(exp)
                cands = ([x for x in self.demo_db(q["candidates_sql"]).splitlines() if x]
                         if q.get("candidates_sql") else None)
                self.truth[q["id"]] = exp
                rec["expected"] = exp
                rid, reply = self.ask(q["ask"])
                ok = rehearsal_match.check(q, reply, exp, cands)
                if not ok:
                    rec["retried"] = True
                    rid, reply = self.ask(q["ask"])
                    ok = rehearsal_match.check(q, reply, exp, cands)
                rec.update(result="PASS" if ok else "FAIL", tools=self.tools(rid),
                           excerpt=redact(reply)[:160])
            except Fail as e:
                rec.update(result="FAIL", excerpt=redact(str(e))[:160])
            rec["seconds"] = time.time() - t
            self.state["questions"].append(rec)
            print(f"{rec['result']} {q['id']} expected={rec['expected']} retried={rec['retried']} "
                  f"{rec['excerpt'][:80]}", flush=True)

    def finish(self):
        passed = sum(1 for q in self.state["questions"] if q["result"] == "PASS")
        if self.state["questions"] and passed < 10:
            self.state["reasons"].append(f"questions {passed}/10")
        if not self.state["questions"] and not any(s["step"] == "1" and s["result"] == "FAIL" for s in self.state["steps"]):
            self.state["reasons"].append("no questions run")
        text = render_results(self.state)
        if self.out:
            Path(self.out).parent.mkdir(parents=True, exist_ok=True)
            Path(self.out).write_text(text, encoding="utf-8")
        reasons = list(dict.fromkeys(self.state["reasons"]))
        if reasons:
            print(f"FAIL scenario_rehearsal ({'; '.join(reasons)})")
            return 1
        print("PASS scenario_rehearsal")
        return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = load_config(os.environ, argv)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    why = refusal(cfg, os.environ)
    if why:
        print(f"REFUSED: {why}")
        return 2
    secrets = argv[argv.index("--secrets") + 1] if "--secrets" in argv else ""
    out = argv[argv.index("--out") + 1] if "--out" in argv else ""
    r = Rehearsal(cfg, out, "--allow-used" in argv)
    try:
        return r.run(secrets)
    except Fail as e:
        print(f"FAIL scenario_rehearsal ({redact(str(e))})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
