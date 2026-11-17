"""Live system diagnostic + smoke test. Run from backend/ with the app running:

    OPS_KEY=$WEBHOOK_SECRET uv run python scripts/diagnose.py

It (1) dumps queue state + recent logs so we can see triage/worker health, then
(2) probes the pipeline: injects a real commitment and asserts triage opens a case
with an expectation, and injects casual chat and asserts it does NOT — proving
whether "no cases while chatting" is by-design or a triage failure.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("BASE_URL", "http://localhost:8000")
KEY = os.getenv("OPS_KEY") or os.getenv("WEBHOOK_SECRET", "")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"x-ops-key": KEY, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:300]}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def hdr(t): print(f"\n{'='*60}\n{t}\n{'='*60}")


def main():
    if not KEY:
        print("Set OPS_KEY (or WEBHOOK_SECRET)."); return

    hdr("1. HEALTH & QUEUE")
    print("health:", call("GET", "/health"))
    stats = call("GET", "/api/stats")
    print("queue:", stats.get("queue"))

    hdr("2. STATE COUNTS")
    people = call("GET", "/api/people")
    cases = call("GET", "/api/cases?limit=100")
    unmatched = call("GET", "/api/people/unmatched")
    print(f"people registered : {len(people) if isinstance(people, list) else people}")
    print(f"cases total       : {len(cases) if isinstance(cases, list) else cases}")
    if isinstance(cases, list):
        for c in cases[:8]:
            print(f"   #{c['id']} [{c['status']}] {c['title'][:50]} "
                  f"(open {c['open_exp']}, violated {c['violated_exp']})")
    print(f"unmatched senders : {len(unmatched) if isinstance(unmatched, list) else unmatched}")
    if isinstance(unmatched, list):
        for u in unmatched[:8]:
            print(f"   {u['actor_raw']} — {u['event_count']} event(s), last: {(u.get('last_content') or '')[:50]}")

    hdr("3. RECENT RUNS (reactive worker)")
    runs = call("GET", "/api/runs?limit=8")
    if isinstance(runs, list):
        for r in runs:
            print(f"   #{r['id']} [{r['status']}] {r['thread_id']}: {(r.get('result_summary') or r.get('error') or '')[:60]}")

    hdr("4. RECENT LOGS (look for triage/worker errors)")
    logs = call("GET", "/api/logs?limit=80")
    if isinstance(logs, list):
        for e in logs[-60:]:
            line = e if isinstance(e, str) else (e.get("message") or json.dumps(e))
            lvl = "" if isinstance(e, str) else e.get("level", "")
            print(f"   {lvl:<7} {line[:150]}")
    else:
        print("   ", logs)

    hdr("5. TRIAGE PROBE — inject a clear commitment, expect a case")
    call("POST", "/api/people", {"name": "DiagTester", "aliases": ["diagtester"],
                                 "whatsapp": "910000000099"})
    ext = f"diag-commit-{int(time.time())}"
    inj = call("POST", "/api/test-event", {
        "source": "whatsapp", "actor_alias": "DiagTester", "thread_ref": "diag",
        "external_id": ext,
        "content": "DiagTester will send the Q2 budget report to accounts by this Friday."})
    print("injected commitment event:", inj)
    opened = None
    for _ in range(18):
        time.sleep(5)
        cs = call("GET", "/api/cases?limit=100")
        if isinstance(cs, list) and len(cs) > (len(cases) if isinstance(cases, list) else 0):
            opened = cs[0]
            break
    if opened:
        d = call("GET", f"/api/cases/{opened['id']}")
        print(f"   PASS: case #{opened['id']} opened with {len(d['expectations'])} expectation(s)")
        for e in d["expectations"]:
            print(f"      - {e['description'][:60]} | owner {e['owner']} | due {e['due_at']}")
    else:
        print("   FAIL: no case opened within ~90s.")
        print("   → Triage isn't converting events to cases. Check section 4 logs for")
        print("     'triage model call failed' (bad/missing LLM key) or a stuck loop.")

    hdr("6. CASUAL-CHAT PROBE — inject small talk, expect NO new case")
    before = call("GET", "/api/cases?limit=100")
    before_n = len(before) if isinstance(before, list) else 0
    call("POST", "/api/test-event", {
        "source": "whatsapp", "actor_alias": "DiagTester", "thread_ref": "diag",
        "external_id": f"diag-chat-{int(time.time())}",
        "content": "hey, how's it going today?"})
    time.sleep(20)
    after = call("GET", "/api/cases?limit=100")
    after_n = len(after) if isinstance(after, list) else 0
    if after_n == before_n:
        print("   PASS: casual chat did NOT open a case (this is by design).")
    else:
        print("   NOTE: a case opened for small talk — triage may be over-eager.")

    hdr("VERDICT")
    print("If section 5 PASSED, triage works and 'no cases while chatting' just means")
    print("your messages were casual (section 6 confirms casual chat is ignored by design).")
    print("If section 5 FAILED, the section-4 logs show why (usually the triage LLM key).")


if __name__ == "__main__":
    main()
