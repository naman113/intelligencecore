"""GOLIVE-08 gap probe (Phase 13). DEMO STACKS ONLY.

Runs the failing "record X, alert on Y" KPI condition (plus two control conditions) through the
real sentinel compiler, using only the ops API (POST/DELETE /api/watches). Checks that the first
recorded value equals the demo DB's open-order count. Watches are always deleted afterwards.
Nothing is written to any database directly; the demo DB is only read for ground truth.

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p13 uv run --frozen python scripts/probe_kpi_compile.py \
      --secrets <path> [--repeat N] [--out <file>]

Exit codes: 0 PASS, 1 FAIL, 2 refused. The ops key is never printed or written.
"""

import json
import os
import random
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

APP = HERE.parent.parent
DEMO_DB = "ic-demo-db"
THREAD = "ui:kpi-probe"
POLL_SECONDS = 10
WAIT_SECONDS = 300

PROBE_CONDITIONS = [
    {"id": "compound_stale",
     "condition": ("Count the sales orders whose status is 'open' (sales_orders.status = 'open'). "
                   "Record that count as the daily value. Also alert when any open order is older "
                   "than 7 days (order_date more than 7 days before today).")},
    {"id": "compound_high",
     "condition": ("Number of open sales orders (sales_orders where status = 'open'), recorded "
                   "once a day; alert if it goes above 500.")},
    {"id": "plain",
     "condition": ("Track the number of open sales orders (sales_orders where status = 'open') "
                   "each day.")},
]


def probe_verdict(watch, hist, truth):
    watch = watch or {}
    hist = hist or {}
    rule = watch.get("trigger_rule")
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except ValueError:
            rule = {}
    if not isinstance(rule, dict):
        rule = {}
    latest = hist.get("latest") or {}
    ev = (f"metric={latest.get('metric') or '-'} trigger={rule.get('column') or '-'} "
          f"value_column={rule.get('value_column') or '-'}")
    if watch.get("compile_error"):
        ev += f" compile_error={watch['compile_error']}"
    if not hist.get("points"):
        return "FAIL", "no history; " + ev
    try:
        ok = float(latest.get("value")) == float(truth)
    except (TypeError, ValueError):
        ok = False
    ev += f" value={latest.get('value')} truth={truth}"
    return ("PASS" if ok else "FAIL"), ev


def _cell(v):
    return str(v).replace("|", "/").replace("\n", " ")


def render_probe(results, commit):
    n = len(results)
    k = sum(1 for r in results if r["verdict"] == "PASS")
    lines = ["# Phase 13 KPI compile probe (GOLIVE-08 gap)", "",
             f"Date: {date.today().isoformat()} App commit: {commit}", "",
             "| repeat | id | watch | verdict | seconds | evidence |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {_cell(r['repeat'])} | {_cell(r['id'])} | {_cell(r['watch_id'])} | "
                     f"{_cell(r['verdict'])} | {_cell(round(float(r['seconds'])))} | "
                     f"{_cell(redact(str(r['evidence'])))} |")
    lines += ["", f"Verdict: {'PASS' if k == n else 'FAIL'} probe_kpi_compile ({k}/{n})", ""]
    return "\n".join(lines)


class Api:
    def __init__(self, cfg):
        self.c = cfg

    def call(self, method, path, body=None, timeout=60):
        h = {"x-ops-key": self.c["ops_key"]}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["content-type"] = "application/json"
        req = urllib.request.Request(self.c["base"] + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                               f"{redact(e.read()[:200].decode('utf-8', 'replace'))}") from None
        return json.loads(raw) if raw else {}


def ground_truth():
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    r = subprocess.run(["docker", "exec", DEMO_DB, "psql", "-U", "demo_ro", "-d", "demo_business",
                        "-X", "-A", "-t", "-c",
                        "SELECT count(*) FROM sales_orders WHERE status = 'open'"],
                       capture_output=True, text=True, env=env, timeout=60)
    if r.returncode:
        raise RuntimeError("demo psql: " + r.stderr[:200])
    return int(r.stdout.strip())


def run_probe(cfg, repeat, truth):
    api = Api(cfg)
    results = []
    for rep in range(1, repeat + 1):
        made = []  # (id, watch_id, started)
        try:
            for c in PROBE_CONDITIONS:
                suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(8))
                res = api.call("POST", "/api/watches", {
                    "thread_id": THREAD, "title": f"KPI probe {c['id']} {suffix}",
                    "condition": c["condition"], "kind": "kpi", "cadence_seconds": 86400,
                    "one_shot": False})
                made.append((c["id"], res["created"], time.time()))
            for cid, wid, t0 in made:
                hist = {}
                while time.time() < t0 + WAIT_SECONDS:
                    hist = api.call("GET", f"/api/watches/{wid}/history")
                    if hist.get("points"):
                        break
                    time.sleep(POLL_SECONDS)
                w = next((x for x in api.call("GET", "/api/watches") if x["id"] == wid), {})
                verdict, ev = probe_verdict(w, hist, truth)
                rec = {"repeat": rep, "id": cid, "watch_id": wid, "verdict": verdict,
                       "seconds": time.time() - t0, "evidence": ev}
                results.append(rec)
                print(redact(f"{verdict} {rep}/{cid} watch {wid} {ev}"), flush=True)
        finally:
            for _cid, wid, _t in made:
                try:
                    api.call("DELETE", f"/api/watches/{wid}")
                except Exception as e:  # noqa: BLE001
                    print(redact(f"cleanup of watch {wid} failed: {e}"), flush=True)
    return results


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
    repeat = int(argv[argv.index("--repeat") + 1]) if "--repeat" in argv else 1
    out = argv[argv.index("--out") + 1] if "--out" in argv else ""
    try:
        truth = ground_truth()
        results = run_probe(cfg, repeat, truth)
    except Exception as e:  # noqa: BLE001
        print(redact(f"FAIL probe_kpi_compile ({type(e).__name__}: {e})"))
        return 1
    n = len(results)
    k = sum(1 for r in results if r["verdict"] == "PASS")
    if out:
        c = subprocess.run(["git", "-C", str(APP), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(render_probe(results, c.stdout.strip() or "unknown"), encoding="utf-8")
    ok = n > 0 and k == n
    print(f"{'PASS' if ok else 'FAIL'} probe_kpi_compile ({k}/{n})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
