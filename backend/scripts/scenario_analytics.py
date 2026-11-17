"""Quick 004 acceptance: the analytics skills through the real Ask path.

Asks a running stack five owner questions (forecast, significance, anomaly,
driver, trend) and, with --dashboard, a forecasting dashboard. For each run it reads
the tool log and checks that use_skill loaded the right skill, that run_python
used statsmodels, scikit-learn or scipy where the skill needs Python, and that
the reply gives a range or a percentage. Demo stacks only: the questions assume
the synthetic Northwind data (deploy/demo-business.sql).

    cd backend && OPS_KEY=... uv run python scripts/scenario_analytics.py --base http://127.0.0.1:9171
    cd backend && uv run python scripts/scenario_analytics.py --base http://127.0.0.1:9171 --secrets ../../intelligence-corner/deploy/secrets.env

The five skills must be enabled on the stack (Skills page). A run that fails
because the LLM provider refuses the account (usage or credit limit) is
reported SKIPPED, not FAILED. Exit code: 0 all passed or skipped, 1 a check
failed. Stdlib only.
"""

import argparse
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.request

QUESTIONS = [
    ("forecast", "Forecast weekly revenue for the next 8 weeks.",
     "forecasting", ("statsmodels",)),
    ("significance", "Have Hospitality customers really started paying their invoices later "
     "since June 2026, or could that just be chance?", "statistical-tests", ("scipy",)),
    ("anomaly", "Was there anything unusual in our daily orders or in individual order sizes "
     "over the last three months?", "anomaly-detection", ("statsmodels", "sklearn")),
    ("driver", "What drives how late our customers pay their invoices?", "driver-analysis",
     ("statsmodels", "sklearn")),
    ("trend", "When did South region orders start to fall, and is it just the season?",
     "trend-and-seasonality", ("statsmodels", "numpy")),
]
DASHBOARD = ("dashboard", "Create a forecasting dashboard: weekly revenue for the last 6 months "
             "and a forecast for the next 8 weeks.")
LIMIT_RE = re.compile(r"usage limits|credit balance|insufficient_quota|rate_limit", re.I)
RANGE_RE = re.compile(r"\d[\d.,]*\s*(?:%|percent)|between\s+\S+\s+and\s+\S+|\d[\d.,]*k?\s*(?:to|-)\s*\d", re.I)


def ops_key(args) -> str:
    if os.environ.get("OPS_KEY"):
        return os.environ["OPS_KEY"].strip()
    if args.secrets:
        with open(args.secrets, encoding="utf-8") as f:
            for line in f:
                if line.startswith("OPS_KEY="):
                    return line.split("=", 1)[1].strip()
    sys.exit("set OPS_KEY or pass --secrets")


class Stack:
    def __init__(self, base: str, key: str):
        self.base, self.h = base.rstrip("/"), {"x-ops-key": key, "content-type": "application/json"}

    def call(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.h,
                                     method=method or ("POST" if body is not None else "GET"))
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")

    def ask(self, question: str, timeout: int):
        op = "analytics-" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))
        rid = self.call("/api/ask", {"message": question, "operator": op})["run_id"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(5)
            th = self.call(f"/api/ask?operator={op}")
            if not th["pending"]:
                break
        th = self.call(f"/api/ask?operator={op}")
        run = th.get("last_run") or {}
        reply = th["messages"][-1]["content"] if th.get("messages") else ""
        tools = []
        for t in self.call(f"/api/runs/{rid}/tools"):
            a = t.get("args")
            tools.append((t["tool"], json.loads(a) if isinstance(a, str) else (a or {})))
        return rid, round(time.time() - t0), run, reply, tools


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default="http://127.0.0.1:9171", help="gateway URL of a demo stack")
    p.add_argument("--secrets", help="file with an OPS_KEY= line (else env OPS_KEY)")
    p.add_argument("--only", help="comma list: forecast,significance,anomaly,driver,trend,dashboard")
    p.add_argument("--dashboard", action="store_true", help="also ask for a forecasting dashboard")
    p.add_argument("--timeout", type=int, default=900, help="seconds per question")
    args = p.parse_args()
    stack = Stack(args.base, ops_key(args))
    sk = stack.call("/api/skills")
    sk = sk.get("skills", []) if isinstance(sk, dict) else (sk or [])
    enabled = {s["name"] for s in sk if s.get("enabled")}
    todo = [q for q in QUESTIONS] + ([DASHBOARD + ("dashboard-authoring", ())] if args.dashboard else [])
    if args.only:
        keep = set(args.only.split(","))
        todo = [q for q in todo if q[0] in keep]
    failed = False
    for key, question, slug, libs in todo:
        print(f"\n== {key}: {question}")
        if enabled and slug not in enabled:
            failed |= not check(f"{key} skill enabled", False, f"enable {slug} on the Skills page")
            continue
        rid, secs, run, reply, tools = stack.ask(question, args.timeout)
        err = str(run.get("error") or "")
        print(f"run {rid}: {run.get('status')} in {secs}s; tools: {[t for t, _ in tools]}")
        if run.get("status") != "done" and LIMIT_RE.search(err):
            print(f"SKIPPED {key}: the LLM provider refused the account ({err[:120]})")
            continue
        ok = check(f"{key} run done", run.get("status") == "done", err[:200])
        skills = [a.get("name") for t, a in tools if t == "use_skill"]
        ok &= check(f"{key} use_skill({slug})", slug in skills, f"loaded {skills}")
        if key == "dashboard":
            ok &= check("dashboard draft saved", any(t == "save_dashboard_version" for t, _ in tools))
        else:
            codes = [a.get("code", "") for t, a in tools if t == "run_python"]
            if libs:
                ok &= check(f"{key} run_python with {'/'.join(libs)}",
                            any(any(lib in c for lib in libs) for c in codes), f"{len(codes)} run_python call(s)")
            ok &= check(f"{key} reply gives a range or percentage", bool(RANGE_RE.search(reply)))
            ok &= check(f"{key} reply does not state a forecast as fact",
                        "will be" not in reply.lower() or key != "forecast")
        print("reply:", reply[:600].encode("ascii", "replace").decode())
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
