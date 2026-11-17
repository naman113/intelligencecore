"""Phase 2 acceptance scenario: schema discovery -> proposal -> approval -> search.

Drives the RUNNING stack through its ops API (stdlib only). Usage from backend/:

    SCENARIO_DEMO_STACK=1 OPS_KEY=... BASE_URL=http://localhost:9021 \
        uv run python scripts/scenario_context.py

DEMO STACK ONLY. The script enables the schema-documentation skill, approves agent
proposals and approves a hard-coded rule that is true only for the synthetic
Northwind Supply data. There is no delete path for context rows, so on a real client
stack it would plant a false approved rule that every agent retrieves as fact. It
therefore refuses (exit 2) unless SCENARIO_DEMO_STACK=1, BASE_URL is loopback, and the
stack's context catalog only names the 9 Northwind tables.

Exit codes: 0 pass, 1 assertion failed, 2 refused / no OPS_KEY / stack unhealthy.
SCENARIO_NO_LLM=1 runs steps 0-2 only (no model calls).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("BASE_URL", "http://localhost:8000")
KEY = os.getenv("OPS_KEY") or os.getenv("WEBHOOK_SECRET", "")
SKILL = "schema-documentation"
DEMO_TABLES = {"regions", "sales_reps", "customers", "products", "sales_orders",
               "order_lines", "invoices", "payments", "inventory_snapshots"}
DISCOVER_MSG = ("Discover the schema of the ERP database and propose descriptions "
                "for its tables and columns.")
RULE_MSG = ("Record this business rule for my approval with slug south-distributor-loss: "
            "from 2026-08-01 the South region lost its main distributor, so South order "
            "volume is expected to run about a third lower than before.")
RULE_REF = "rule:south-distributor-loss"
SUMMARY: dict = {}


class Fail(Exception):
    pass


def call(method: str, path: str, body: dict | None = None, expect_error: bool = False):
    """Returns (status, decoded json)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-ops-key": KEY, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if not expect_error:
            raise Fail(f"{method} {path} -> HTTP {e.code}: {e.read()[:300]!r}")
        return e.code, {}


def get(path: str):
    return call("GET", path)[1]


def payload(a: dict) -> dict:
    p = a.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"   {'PASS' if cond else 'FAIL'} {label}" + (f" ({detail})" if detail else ""))
    if not cond:
        raise Fail(label)


def refuse(msg: str) -> None:
    print(f"REFUSED: {msg}")
    sys.exit(2)


def catalog_tables() -> set[str]:
    rows = get("/api/context/tables?status=all&limit=1000")["tables"]
    return {r["ref"].rsplit(".", 1)[-1] for r in rows}


def demo_guard(strict: bool) -> set[str]:
    host = urllib.parse.urlparse(BASE).hostname or ""
    if os.getenv("SCENARIO_DEMO_STACK") != "1":
        refuse("set SCENARIO_DEMO_STACK=1 only on the Northwind demo stack")
    if host not in ("localhost", "127.0.0.1", "::1"):
        refuse(f"BASE_URL host {host!r} is not loopback")
    names = {s["name"] for s in get("/api/context/sources")["sources"]}
    if not names <= {"business_db"}:
        refuse(f"unexpected context sources {sorted(names)}")
    tabs = catalog_tables()
    if not tabs <= DEMO_TABLES:
        refuse(f"catalog names non-demo tables {sorted(tabs - DEMO_TABLES)}")
    if strict and not ({"sales_orders", "regions"} <= tabs):
        refuse("after discovery the catalog lacks sales_orders/regions")
    return tabs


def wait_run(snapshot: int, timeout: int = 900) -> dict:
    """Newest test-local run above the snapshot, once done or failed."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(5)
        runs = [r for r in get("/api/runs?limit=50")
                if r.get("thread_id") == "test-local" and r["id"] > snapshot]
        if runs:
            run = max(runs, key=lambda r: r["id"])
            if run["status"] in ("done", "failed"):
                return run
    raise Fail("run did not finish in time")


def max_run() -> int:
    return max([r["id"] for r in get("/api/runs?limit=50")] or [0])


def proposals() -> list[dict]:
    rows = get("/api/actions?status=proposed&limit=1000")
    return [a for a in rows if a.get("kind") == "context_change"]


def approved_refs() -> set[str]:
    out = set()
    for path in ("/api/context/tables?status=approved&limit=1000",
                 "/api/context/columns?status=approved&limit=1000"):
        data = get(path)
        out |= {r["ref"] for r in data.get("tables", data.get("columns", []))}
    return out


def run_discovery() -> tuple[dict, list]:
    snap = max_run()
    call("POST", "/api/test-message", {"message": DISCOVER_MSG})
    run = wait_run(snap)
    if run["status"] == "failed":
        raise Fail(f"discovery run failed: {str(run.get('error'))[:300]} "
                   "(see max_tokens / budget fallbacks in the plan)")
    tools = get(f"/api/runs/{run['id']}/tools")
    names = [t.get("tool") or t.get("name") for t in tools]
    SUMMARY.setdefault("runs", []).append(
        {"id": run["id"], "tool_calls": len(tools),
         "propose_calls": names.count("propose_context_change")})
    return run, names


def step(n: str, title: str) -> None:
    print(f"{n}. {title}")


def main() -> int:
    if not KEY:
        print("set OPS_KEY"); return 2
    step("0", "demo-stack guard")
    try:
        demo_guard(strict=False)
    except urllib.error.URLError as e:
        print(f"stack unreachable: {e}"); return 2
    print("   PASS demo stack")
    try:
        step("1", "health and skill")
        h = urllib.request.urlopen(BASE + "/health", timeout=20).read().decode()
        check("ok" in h, "health ok")
        call("GET", f"/api/skills/{SKILL}")
        call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
        check(get(f"/api/skills/{SKILL}").get("enabled") is True, "skill enabled")

        step("2", "snapshot")
        snap_pending = {(payload(a).get("kind"), payload(a).get("ref")) for a in proposals()}
        snap_approved = approved_refs()
        print(f"   pending={len(snap_pending)} approved={len(snap_approved)}")
        if os.getenv("SCENARIO_NO_LLM") == "1":
            print("LLM steps skipped"); return 0

        step("3", "discovery run")
        snap_act = max([a["id"] for a in proposals()] or [0])
        run, names = run_discovery()
        check("discover_schema" in names, "discover_schema called", f"{len(names)} calls")
        check("propose_context_change" in names, "propose_context_change called")

        step("4", "proposals")
        new = [a for a in proposals() if a["id"] > snap_act]
        check(len(new) > 0, "new proposals", str(len(new)))
        pairs = [(payload(a).get("kind"), payload(a).get("ref")) for a in proposals()]
        check(len(pairs) == len(set(pairs)), "no duplicate pending (kind, ref)")
        for a in new:
            ref = payload(a).get("ref") or ""
            parts = ref.split(".")
            if not (len(parts) >= 3 and parts[0] == "business_db" and parts[2] in DEMO_TABLES):
                raise Fail(f"non-demo ref proposed: {ref}")
            if ref in snap_approved:
                raise Fail(f"proposed an already approved ref: {ref}")
        print("   PASS all new refs target demo tables, none already approved")
        covered = {payload(a).get("ref").split(".")[2] for a in proposals()
                   if payload(a).get("ref")} | {r.split(".")[2] for r in approved_refs()}
        print(f"   coverage {len(covered & DEMO_TABLES)}/9 tables: {sorted(covered)}")
        if len(covered & DEMO_TABLES) < 9:
            print("   WARN fewer than 9 tables covered (per-run cap; re-run continues)")
        SUMMARY["proposals_created"] = len(new)

        step("4b", "re-check catalog before approving")
        demo_guard(strict=True)
        print("   PASS")

        step("5", "approve / reject")
        cols = [a for a in new if payload(a).get("kind") == "column"]
        tabs = [a for a in new if payload(a).get("kind") == "table"]
        check(bool(cols) and bool(tabs), "have a column and a table proposal",
              f"{len(cols)} cols {len(tabs)} tables")
        col, tab = cols[0], tabs[0]
        others = [a for a in new if a["id"] not in (col["id"], tab["id"])]
        check(bool(others), "have a proposal to reject")
        rej = others[0]
        for a in (col, tab):
            st, r = call("POST", f"/api/actions/{a['id']}/approve")
            check(r.get("status") == "executed", f"approve {a['id']}", str(r))
        st, r = call("POST", f"/api/actions/{rej['id']}/reject")
        check(r.get("rejected") == rej["id"], f"reject {rej['id']}")
        cref, tref, rref = (payload(a)["ref"] for a in (col, tab, rej))
        ctext = payload(col).get("text") or ""
        cols_ok = {c["ref"]: c for c in get("/api/context/columns?status=approved&limit=1000")["columns"]}
        check(cref in cols_ok and (cols_ok[cref].get("description") or "").strip() == ctext.strip(),
              "approved column present with proposal text")
        check(tref in {t["ref"] for t in get("/api/context/tables?status=approved&limit=1000")["tables"]},
              "approved table present")
        q = urllib.parse.quote(" ".join(ctext.split()[:8]))
        hits = get(f"/api/context/search?q={q}&limit=10")
        check(any(h.get("ref") == cref for h in hits), "search finds approved column")
        check(rref not in approved_refs(), "rejected ref not approved")
        step_max = max(a["id"] for a in (col, tab, rej))
        pend_before = {(payload(a).get("kind"), payload(a).get("ref")) for a in proposals()}
        SUMMARY["approved"] = [col["id"], tab["id"]]; SUMMARY["rejected"] = [rej["id"]]

        step("6", "re-run discovery")
        step_max = max([a["id"] for a in proposals()] + [step_max])
        _, names2 = run_discovery()
        if "discover_schema" not in names2:
            print("   WARN re-run did not call discover_schema; step 6 is weak evidence")
        approved_now = {cref, tref}
        newer = [a for a in proposals() if a["id"] > step_max]
        for a in newer:
            p = payload(a)
            if p.get("ref") in approved_now:
                raise Fail(f"re-proposed approved ref {p.get('ref')}")
            if (p.get("kind"), p.get("ref")) in pend_before:
                raise Fail(f"duplicated pending ref {p.get('ref')}")
        pairs = [(payload(a).get("kind"), payload(a).get("ref")) for a in proposals()]
        check(len(pairs) == len(set(pairs)), "pending refs unique", f"{len(newer)} new")

        step("7", "South rule")
        snap = max_run()
        snap_act = max([a["id"] for a in proposals()] or [0])
        call("POST", "/api/test-message", {"message": RULE_MSG})
        run = wait_run(snap)
        check(run["status"] == "done", "rule run done", str(run.get("error"))[:200])
        rules = [a for a in proposals() if payload(a).get("kind") == "rule" and a["id"] > snap_act]
        pick = [a for a in rules if payload(a).get("ref") == RULE_REF] or rules
        check(bool(pick), "rule proposal exists")
        if payload(pick[0]).get("ref") != RULE_REF:
            print(f"   NOTE ref was {payload(pick[0]).get('ref')}")
        ref = payload(pick[0])["ref"]
        st, r = call("POST", f"/api/actions/{pick[0]['id']}/approve")
        check(r.get("status") == "executed", "approve rule", str(r))
        ap = get("/api/context/rules?status=approved&limit=1000")["rules"]
        check(any(x["ref"] == ref for x in ap), "rule listed approved")
        hits = get("/api/context/search?q=" + urllib.parse.quote("why did South orders drop in August") + "&limit=10")
        check(any(h.get("type") == "rule" and h.get("ref") == ref for h in hits),
              "search returns the South rule")
        SUMMARY["rule_ref"] = ref
        SUMMARY["search_hits"] = [{"type": h.get("type"), "ref": h.get("ref"), "score": h.get("score")}
                                  for h in hits[:5]]

        step("8", "summary")
        SUMMARY["embeddings_used"] = any(h.get("score") is not None for h in hits)
        print(json.dumps(SUMMARY, indent=2, default=str))
        return 0
    except Fail as e:
        print(f"FAILED: {e}")
        print(json.dumps(SUMMARY, indent=2, default=str))
        return 1


if __name__ == "__main__":
    sys.exit(main())
