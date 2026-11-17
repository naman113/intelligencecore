"""GOLIVE-01 per-role live smoke: one real call per role on its default model.

DEMO STACKS ONLY. Never imported by the app. Drives the ops API of a loopback demo stack
and runs two small programs inside the app container so saved Setup values apply.

Usage (from backend/):
  SCENARIO_DEMO_STACK=1 STACK=p13 uv run --frozen python scripts/smoke_models.py \
      --secrets <path> [--out <file>] [--only 0,1,2]

Exit codes: 0 pass, 1 fail, 2 refused. Keys are printed as len=N only.
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
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenario_dashboard import load_config, redact, refusal  # noqa: E402

LOCKED_DEFAULTS = {
    "supervisor_model": "claude-opus-5-5", "onboarding_model": "claude-opus-5-5",
    "context_model": "claude-opus-5-5", "document_model": "claude-haiku-5-5",  # Quick 022
    "research_model": "claude-sonnet-5-5", "triage_model": "claude-haiku-5-5",
    "supervisor_effort": "medium", "onboarding_effort": "high", "context_effort": "medium",
    "document_effort": "high", "research_effort": "medium", "triage_effort": "low",
    "data_model": "claude-sonnet-5-5", "data_effort": "medium",  # Quick 012
    # Quick 022: document extraction and the memory model on Haiku 5.5
    "reflection_model": "claude-haiku-5-5", "reflection_effort": "medium",
    "graph_model": "claude-sonnet-5-5", "graph_effort": "medium",  # opt-in Anthropic path
}
ROLES = ("supervisor", "onboarding", "context", "document", "research", "triage", "data",
         "reflection", "graph")  # Quick 022
HEADER = "| Step | Role | Setting | Model | Effort | Path | Result | Seconds | Note |"


class Fail(Exception):
    pass


def row(step, role, setting, model, effort, path, result, seconds=0.0, note=""):
    return {"step": step, "role": role, "setting": setting, "model": model, "effort": effort,
            "path": path, "result": result, "seconds": round(seconds, 1), "note": note}


def _flat(config_json):
    out = {}
    for items in (config_json or {}).get("config", {}).values():
        for it in items:
            out[it["key"]] = it
    return out


def check_defaults(config_json):
    flat = _flat(config_json)
    rows = []
    for key, want in LOCKED_DEFAULTS.items():
        it = flat.get(key) or {}
        got, src = it.get("value"), it.get("source")
        ok = got == want and src == "default"
        role = key.rsplit("_", 1)[0]
        kind = "model" if key.endswith("_model") else "effort"
        rows.append(row("0", role, key, got if kind == "model" else "", got if kind == "effort" else "",
                        "GET /api/config", "PASS" if ok else "FAIL", 0,
                        "" if ok else f"{key}: want {want} source default, got {got!r} source {src!r}"))
    return rows


def render_results(rows, secret=""):
    lines = [HEADER, "|" + "---|" * 9]
    for r in rows:
        cells = [r["step"], r["role"], r["setting"], r["model"], r["effort"], r["path"],
                 r["result"], str(r["seconds"]), str(r["note"]).replace("|", "/").replace("\n", " ")]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    text = "\n".join(lines) + "\n"
    if secret:
        text = text.replace(secret, "<redacted>")
    return redact(text)


def summary_line(rows):
    n = sum(1 for r in rows if r["result"] == "FAIL")
    return "PASS smoke_models" if n == 0 else f"FAIL smoke_models ({n} failed)"


# ---------------------------------------------------------------- live part

class Smoke:
    def __init__(self, cfg):
        self.c = cfg
        self.key = cfg["ops_key"]
        self.rows = []
        self.cfgjson = {}

    def call(self, method, path, body=None, timeout=120):
        data = json.dumps(body).encode() if body is not None else None
        h = {"x-ops-key": self.key}
        if body is not None:
            h["content-type"] = "application/json"
        req = urllib.request.Request(self.c["base"] + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read()[:300].decode("utf-8", "replace")}

    def sh(self, args, stdin=None, timeout=900):
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        return subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=timeout)

    def psql(self, sql):
        r = self.sh(["docker", "exec", "-i", self.c["pg"], "psql", "-U", "agent", "-d",
                     "agent_memory", "-v", "ON_ERROR_STOP=1", "-At"], sql)
        if r.returncode != 0:
            raise Fail("psql: " + r.stderr[:200])
        return r.stdout.strip()

    def eff(self, role):
        flat = _flat(self.cfgjson)
        return (flat.get(f"{role}_model") or {}).get("value", ""), (flat.get(f"{role}_effort") or {}).get("value", "")

    def add(self, step, role, path, result, secs, note=""):
        m, e = self.eff(role) if role in ROLES else ("", "")
        self.rows.append(row(step, role, f"{role}_model" if role in ROLES else "", m, e, path, result, secs, note))
        print(f"{result} {step} {role} {note[:120]}", flush=True)

    def wait_run(self, run_id, timeout=600):
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(3)
            st, runs = self.call("GET", "/api/runs?limit=50")
            for r in runs if isinstance(runs, list) else []:
                if r["id"] == run_id and r["status"] in ("done", "failed"):
                    return r
        raise Fail("run did not finish in time")

    def container(self, program):
        r = self.sh(["docker", "exec", "-i", self.c["app"], "python", "-"], program, timeout=300)
        return r.returncode, r.stdout, r.stderr

    # steps
    def s0(self):
        st, j = self.call("GET", "/api/config")
        self.cfgjson = j
        self.rows += check_defaults(j)

    def s1(self):
        t = time.time()
        st, j = self.call("POST", "/api/config/test/llm", {}, timeout=300)
        detail = str(j.get("detail", j))
        ok = st == 200 and j.get("ok") is True and all(r in detail.lower() for r in ROLES)
        self.add("1", "probe", "POST /api/config/test/llm", "PASS" if ok else "FAIL", time.time() - t, detail[:400])

    def s2(self):
        t = time.time()
        r = self.sh(["docker", "exec", "ic-demo-db", "psql", "-U", "demo_ro", "-d", "demo_business",
                     "-X", "-A", "-t", "-c", "SELECT count(*) FROM customers"])
        truth = r.stdout.strip()
        op = "smoke-" + "".join(random.choices(string.ascii_lowercase, k=8))
        st, b = self.call("POST", "/api/ask", {"message": "How many customers are in the database? Reply with just the number.", "operator": op})
        run = self.wait_run(b["run_id"])
        st, a = self.call("GET", f"/api/ask?operator={op}&limit=10")
        replies = [m["content"] for m in a.get("messages", []) if m.get("role") == "assistant"]
        st, tools = self.call("GET", f"/api/runs/{b['run_id']}/tools")
        names = [x.get("tool") or x.get("name") for x in tools] if isinstance(tools, list) else []
        ok = (run["status"] == "done" and bool(truth) and any(truth in (x or "") for x in replies)
              and any(n in ("run_sql", "delegate_to_data_agent") for n in names))
        self.add("2", "supervisor", "POST /api/ask", "PASS" if ok else "FAIL", time.time() - t,
                 f"truth={truth} status={run['status']} tools={names[:4]} reply={(replies[-1] if replies else '')[:60]!r} err={run.get('error')}")

    def s3(self):
        t = time.time()
        try:
            self.call("POST", "/api/onboarding/reset", {"kind": "business"})
            st, j = self.call("POST", "/api/onboarding", {"kind": "business"}, timeout=300)
            ok = st == 200 and bool(j.get("reply")) and not j.get("error")
            self.add("3", "onboarding", "POST /api/onboarding", "PASS" if ok else "FAIL", time.time() - t,
                     f"reply_len={len(j.get('reply') or '')} error={j.get('error')}")
        finally:
            self.call("POST", "/api/onboarding/reset", {"kind": "business"})

    def s4(self):
        t = time.time()
        prog = '''
import asyncio, json
from pathlib import Path
from app import configservice, docintel
async def main():
    await configservice.load_overrides()
    p = Path("/tmp/smoke_invoice.txt")
    p.write_text("INVOICE\\nSupplier: Example Supplies (example)\\nInvoice number: INV-SMOKE-0042\\nDate: 2026-10-01\\nItem: Widgets x 10\\nTotal due: 1234.50 USD\\n")
    out = await docintel.extract_pdf(p)
    print(json.dumps(out, default=str))
asyncio.run(main())
'''
        rc, out, err = self.container(prog)
        ok = rc == 0 and "INV-SMOKE-0042" in out and ("1234.5" in out)
        self.add("4", "document", "docintel.extract_pdf", "PASS" if ok else "FAIL", time.time() - t,
                 "extracted" if ok else (err or out)[-200:])

    def s5(self):
        t = time.time()
        prog = '''
import asyncio
from app import configservice
from app.agents import graph
async def main():
    await configservice.load_overrides()
    print(await graph.web_research.ainvoke({"question": "What is the capital of France? Answer in one word."}))
asyncio.run(main())
'''
        rc, out, err = self.container(prog)
        if rc == 0 and "Paris" in out:
            return self.add("5", "research", "graph.web_research", "PASS", time.time() - t, "Paris")
        low = (out + err).lower()
        if "not enabled" in low or "organi" in low or "web search" in low:
            self.add("5", "research", "graph.web_research", "SKIP", time.time() - t, "web search not enabled for the organisation: " + (out + err)[-120:])
            fb = '''
import asyncio
from anthropic import AsyncAnthropic
from app import configservice, llmclient
from app.config import get_settings
async def main():
    await configservice.load_overrides()
    s = get_settings()
    c = AsyncAnthropic(api_key=s.anthropic_api_key)
    m = await c.messages.create(**llmclient.messages_kwargs(s.research_model, role="research", max_tokens=1024, settings=s,
        messages=[{"role": "user", "content": "What is the capital of France? Answer in one word."}]))
    print("".join(b.text for b in m.content if getattr(b, "type", "") == "text"))
asyncio.run(main())
'''
            t = time.time()
            rc, out, err = self.container(fb)
            return self.add("5b", "research", "client messages.create", "PASS" if rc == 0 and "Paris" in out else "FAIL",
                            time.time() - t, out.strip()[:80] if rc == 0 else err[-200:])
        self.add("5", "research", "graph.web_research", "FAIL", time.time() - t, (out + err)[-250:])

    def s6(self):
        t = time.time()
        self.call("POST", "/api/people", {"name": "SmokeTester", "aliases": ["smoketester"], "whatsapp": "910000000098"})
        before = self.call("GET", "/api/cases?limit=100")[1]
        before = len(before) if isinstance(before, list) else 0
        st, j = self.call("POST", "/api/test-event", {
            "source": "whatsapp", "actor_alias": "smoketester", "thread_ref": "smoke",
            "external_id": f"smoke-commit-{int(time.time())}",
            "content": "smoketester will send the Q3 stock count to accounts by Friday."})
        eid = j.get("event_id")
        status = ""
        for _ in range(24):
            time.sleep(5)
            status = self.psql(f"SELECT triage_status FROM events WHERE id = {int(eid)}")
            if status in ("done", "skipped", "failed"):
                break
        after = self.call("GET", "/api/cases?limit=100")[1]
        after = len(after) if isinstance(after, list) else 0
        self.add("6", "triage", "POST /api/test-event", "PASS" if status == "done" else "FAIL", time.time() - t,
                 f"triage_status={status} cases {before}->{after}")

    def s7(self):
        t = time.time()
        env = dict(os.environ, BASE_URL=self.c["base"], PG_CONTAINER=self.c["pg"], SCENARIO_DEMO_STACK="1")
        r = subprocess.run([sys.executable, str(HERE / "scenario_context_pack.py"), "--no-full", "--no-repeat",
                            "--secrets", self.c["secrets"]], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=1500)
        tail = redact((r.stdout + r.stderr).strip().splitlines()[-1:] and (r.stdout + r.stderr).strip().splitlines()[-1] or "")
        self.add("7", "context", "scenario_context_pack.py", "PASS" if r.returncode == 0 else "FAIL", time.time() - t, tail)

    def s8(self):
        t = time.time()
        try:
            self.call("PUT", "/api/config", {"values": {"research_model": "claude-fable-5-1"}})
            flat = _flat(self.call("GET", "/api/config")[1]).get("research_model", {})
            taken = flat.get("value") == "claude-fable-5-1" and flat.get("source") == "db"
            st, j = self.call("POST", "/api/config/test/llm", {}, timeout=300)
            detail = str(j.get("detail", j))
            retention = "30" in detail and "retention" in detail.lower()
            line = [x for x in detail.split(";") if "research" in x.lower()]
            ok = taken and (j.get("ok") is True or retention)
            self.add("8", "research", "PUT claude-fable-5-1 + probe", "PASS" if ok else "FAIL", time.time() - t,
                     f"source={flat.get('source')} ok={j.get('ok')} retention_msg={retention} {(line[0] if line else detail)[:200]}")
            self.rows[-1]["model"] = "claude-fable-5-1"
        finally:
            self.call("PUT", "/api/config", {"values": {"research_model": ""}})
            back = _flat(self.call("GET", "/api/config")[1]).get("research_model", {})
            if back.get("source") != "default":
                self.rows.append(row("8b", "research", "research_model", "", "", "clear override", "FAIL", 0, f"source={back.get('source')}"))


def main(argv=None, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    cfg = load_config(env, argv)
    why = refusal(cfg, env)
    if why:
        print(f"REFUSED: {why}")
        return 2
    cfg["secrets"] = argv[argv.index("--secrets") + 1] if "--secrets" in argv else ""
    out = argv[argv.index("--out") + 1] if "--out" in argv else str(Path(os.environ.get("TEMP", ".")) / "smoke_models_results.md")
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    sm = Smoke(cfg)
    sm.s0()
    anth = ""
    if cfg["secrets"]:
        for line in open(cfg["secrets"], encoding="utf-8"):
            if line.startswith("ANTHROPIC_API_KEY="):
                anth = line.split("=", 1)[1].strip()
    print(f"ops_key len={len(cfg['ops_key'])} anthropic_key len={len(anth)}")
    steps = [("1", sm.s1), ("2", sm.s2), ("3", sm.s3), ("4", sm.s4), ("5", sm.s5), ("6", sm.s6), ("7", sm.s7), ("8", sm.s8)]
    for n, fn in steps:
        if only and n not in only:
            continue
        if not anth:
            sm.rows.append(row(n, "", "", "", "", "", "SKIP", 0, "no Anthropic key"))
            continue
        try:
            fn()
        except Exception as e:  # a crashed step is a FAIL row, never a hidden one
            sm.rows.append(row(n, "", "", "", "", "", "FAIL", 0, redact(f"{type(e).__name__}: {e}")[:200]))
            print(f"FAIL {n} {type(e).__name__}: {e}")
    text = render_results(sm.rows, cfg["ops_key"])
    header = f"# Model smoke results\n\nDate: {date.today()}\n\n"
    Path(out).write_text(header + text, encoding="utf-8")
    print("results written to", out)
    line = summary_line(sm.rows)
    print(line)
    return 0 if line.startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
