"""Phase 2 acceptance scenario — the accounts-team expense case.

Drives the RUNNING server (uvicorn) via its ops API. Requires the app up and the
ops key in OPS_KEY (same as WEBHOOK_SECRET). Usage from backend/:

    OPS_KEY=$WEBHOOK_SECRET uv run python scripts/scenario_expense.py

Steps (plan §6):
  1. register the accounts requester + 5 owners
  2. inject one group message asking all five for March expense sheets by Friday
  3. wait for triage → assert 1 case + 5 expectations with the right owners
  4. inject Priya's reply with an attachment → assert her expectation flips satisfied
"""

import json
import os
import sys
import time
import urllib.request

BASE = os.getenv("BASE_URL", "http://localhost:8000")
KEY = os.getenv("OPS_KEY") or os.getenv("WEBHOOK_SECRET", "")
OWNERS = ["Rakesh", "Priya", "Amit", "Neha", "Vikram"]


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-ops-key": KEY, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "{}")


def main() -> int:
    if not KEY:
        print("set OPS_KEY (or WEBHOOK_SECRET)"); return 2

    print("1. registering people…")
    call("POST", "/api/people", {"name": "Anita", "role": "accounts", "aliases": ["accounts"]})
    for i, name in enumerate(OWNERS):
        call("POST", "/api/people", {"name": name, "whatsapp": f"91999900{i:04d}",
                                     "aliases": [name.lower()]})

    print("2. injecting the group request…")
    call("POST", "/api/test-event", {
        "source": "whatsapp", "actor_alias": "Anita", "thread_ref": "grp-accounts",
        "content": ("Team, please send your March expense sheets by Friday. "
                    "Waiting on: Rakesh, Priya, Amit, Neha, Vikram."),
    })

    print("3. waiting for triage…")
    case = None
    for _ in range(24):  # up to ~2 min
        time.sleep(5)
        cases = call("GET", "/api/cases?status=open")
        if cases:
            case = cases[0]
            if case["open_exp"] >= 5:
                break
    if not case:
        print("   ✗ no case opened"); return 1
    detail = call("GET", f"/api/cases/{case['id']}")
    exps = detail["expectations"]
    owners_found = {e["owner"] for e in exps if e["owner"]}
    print(f"   case #{case['id']} — {len(exps)} expectation(s); owners: {sorted(owners_found)}")
    ok = len(exps) >= 5 and set(OWNERS).issubset(owners_found)
    print("   ", "✓ 1 case + 5 owner expectations" if ok else "✗ expectations/owners mismatch")

    print("4. injecting Priya's reply with attachment…")
    call("POST", "/api/test-event", {
        "source": "email", "actor_alias": "Priya", "thread_ref": "grp-accounts",
        "content": "Subject: March expenses\n\nAttached my March expense sheet.",
        "external_id": "priya-reply-1",
    })
    satisfied = False
    for _ in range(24):
        time.sleep(5)
        detail = call("GET", f"/api/cases/{case['id']}")
        for e in detail["expectations"]:
            if e["owner"] == "Priya" and e["status"] == "satisfied":
                satisfied = True
                break
        if satisfied:
            break
    print("   ", "✓ Priya's expectation satisfied" if satisfied else "✗ not satisfied")
    return 0 if (ok and satisfied) else 1


if __name__ == "__main__":
    sys.exit(main())
