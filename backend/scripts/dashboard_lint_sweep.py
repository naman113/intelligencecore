"""Quick 029 F4: report which published and retired dashboard versions fail the current lint.

Read-only. Uses only the ops API (GET /api/dashboards, /api/dashboards/{slug},
/api/dashboards/{slug}/versions/{n}) and lints each version locally with the same
dashboardlint the gateway uses. Prints slug, version, status and the failing rule
names, never code, tokens or keys. Exit 1 when any version fails.

    cd backend && OPS_KEY=... uv run python scripts/dashboard_lint_sweep.py --base http://127.0.0.1:<api port>

Versions that fail stop rendering at /frame once the F4 gate is deployed, and can no
longer be published or rolled back to.
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

SWEEP_STATUSES = ("published", "retired")


def sweep(get, lint) -> list[dict]:
    """`get(path) -> parsed JSON`, `lint(code, queries) -> {"errors": [...]}`.
    Returns one row per failing published or retired version."""
    failing = []
    for d in get("/api/dashboards"):
        detail = get(f"/api/dashboards/{d['slug']}")
        for v in detail.get("versions", []):
            if v.get("status") not in SWEEP_STATUSES:
                continue
            full = get(f"/api/dashboards/{d['slug']}/versions/{v['version']}")
            queries = full.get("queries") or []
            if isinstance(queries, str):
                queries = json.loads(queries or "[]")
            rules = sorted({e["rule"] for e in lint(full.get("code") or "", queries)["errors"]})
            if rules:
                failing.append({"slug": d["slug"], "version": v["version"],
                                "status": v["status"], "rules": rules})
    return failing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("BASE_URL", "http://127.0.0.1:8080"))
    args = ap.parse_args(argv)
    key = os.getenv("OPS_KEY", "")
    if not key:
        print("set OPS_KEY in the environment")
        return 2

    def get(path):
        req = urllib.request.Request(args.base.rstrip("/") + path, headers={"x-ops-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    from app import dashboardlint
    failing = sweep(get, dashboardlint.lint)
    for f in failing:
        print(f"{f['slug']} v{f['version']} ({f['status']}): {', '.join(f['rules'])}")
    print(f"{len(failing)} version(s) fail the current lint")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
