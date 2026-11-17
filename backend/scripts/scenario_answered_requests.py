"""Quick 024 acceptance: a request to the assistant does not leave a case open once
the assistant has answered it, on a demo stack.

Runs INSIDE the stack's gateway container, against its own working-memory DB and
the gateway's own HTTP API:

    docker exec ic-p18-app-1 python scripts/scenario_answered_requests.py [--steps 1,2,4,5]

Everything is real (webhook, reactive agent, triage, satisfaction sweep; LLM calls:
the supervisor's answers and the triage model) except WhatsApp: this script serves a
fake connector on 127.0.0.1 inside the container, points `wa_api_base` at it for the
run and clears it at the end, so nothing reaches WhatsApp and every send is captured.
The owner is a fictional number (555-01xx) registered as a person, so a close notice
WOULD go out if one were sent.

  1. a lookup the assistant answers: the case closes "answered by the assistant in
     the chat" (or none was opened), and no "Done" message is sent;
  2. a request it cannot fulfil: the case stays open with the assistant's commitment;
  4. "did you send me ...?" about the answered request: no new case;
  5. a colleague asks the assistant in an active group: the case closes as answered
     and nothing is sent but the reply in the group.
(3, a person-owed commitment, is scripts/scenario_expense.py against the same stack;
the clean-up of stuck cases needs a restart and is driven from the host.)

Refuses to run unless IC_STACK names a test stack. Exit 0 all passed, 1 a check failed.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OWNER = "15550100001"
OWNER_NAME = "Asha Scenario"
API = "http://127.0.0.1:8080"
KEY = os.environ.get("OPS_KEY") or os.environ.get("WEBHOOK_SECRET", "")
ANSWERED = "answered by the assistant in the chat"
PASSED, FAILED, SENT = [], [], []


def check(ok: bool, name: str, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)


def info(msg: str) -> None:
    print("  " + msg, flush=True)


class FakeConnector(BaseHTTPRequestHandler):
    """Accepts every send the gateway makes and remembers it."""

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        SENT.append({"path": self.path, "phone": str(body.get("phone") or ""),
                     "text": str(body.get("message") or body.get("caption") or ""),
                     "at": time.time()})
        self._ok()

    def do_GET(self):
        self._ok()

    def _ok(self):
        data = b'{"success": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def call(method: str, path: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"x-ops-key": KEY, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


async def until(fn, timeout: float, every: float = 3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(every)
    return None


async def main(steps: set[str]) -> int:
    stack = os.environ.get("IC_STACK", "")
    if not stack or stack == "int" or not stack.startswith("p"):
        sys.exit("refusing: IC_STACK must name a test stack (p1..p99), never int or a client")
    if not KEY:
        sys.exit("no OPS_KEY in the container environment")

    from app.db import get_pool
    pool = await get_pool()

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeConnector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    call("PUT", "/api/config", {"values": {"wa_api_base": f"http://127.0.0.1:{port}",
                                           "owner_number": OWNER}})
    try:
        call("POST", "/api/people", {"name": OWNER_NAME, "whatsapp": OWNER})
    except Exception:
        pass                                       # already registered on a rerun
    info(f"fake connector on 127.0.0.1:{port}; owner {OWNER} registered as {OWNER_NAME!r}")
    out: dict = {"stack": stack}

    async def send(text: str) -> int:
        since = await pool.fetchval("SELECT COALESCE(max(id), 0) FROM events")
        call("POST", "/api/test-message", {"phone": OWNER, "message": text})

        async def ev():
            return await pool.fetchval(
                """SELECT id FROM events WHERE id > $1 AND provenance = 'external'
                     AND thread_ref = $2 AND content = $3 ORDER BY id LIMIT 1""",
                since, OWNER, text)
        eid = await until(ev, 30, 1)
        assert eid, f"no event for {text!r}"
        meta = json.loads(await pool.fetchval("SELECT meta FROM events WHERE id = $1", eid) or "{}")
        info(f"event {eid} recorded (to_assistant={meta.get('to_assistant')})")
        return eid

    async def triaged(eid):
        return await pool.fetchval("SELECT triage_status <> 'pending' FROM events WHERE id = $1", eid)

    async def case_of(eid):
        r = await pool.fetchrow(
            """SELECT c.* FROM case_events ce JOIN cases c ON c.id = ce.case_id
               WHERE ce.event_id = $1 ORDER BY c.id LIMIT 1""", eid)
        return dict(r) if r else None

    async def replies_after(eid):
        return [dict(r) for r in await pool.fetch(
            """SELECT id, left(content, 160) AS content FROM events
               WHERE thread_ref = $1 AND provenance = 'self' AND source = 'whatsapp' AND id > $2
               ORDER BY id""", OWNER, eid)]

    async def run_done(eid):
        """The reactive run for this message finished (its final reply is out)."""
        return await pool.fetchval(
            """SELECT r.status IN ('done', 'failed') FROM task_runs r JOIN messages m ON m.id = r.message_id
               WHERE m.thread_id = $1 AND m.content = (SELECT content FROM events WHERE id = $2)
               ORDER BY r.id DESC LIMIT 1""", OWNER, eid)

    async def exps(cid):
        return [dict(r) for r in await pool.fetch(
            "SELECT id, description, status, owed_by, satisfied_by, last_checked_at "
            "FROM expectations WHERE case_id = $1 ORDER BY id", cid)]

    def done_pings(t0):
        return [s for s in SENT if s["at"] >= t0 and "Done" in s["text"] and "complete" in s["text"]]

    answered_case = None
    try:
        if "1" in steps:
            print("1. a lookup the assistant answers", flush=True)
            t0 = time.time()
            e1 = await send("Can you look up the engine specs of the Liebherr A 934 C crawler "
                            "excavator on the web? Engine model and power, please.")
            await until(lambda: run_done(e1), 420, 5)
            reps = await replies_after(e1)
            info(f"{len(reps)} reply(ies): " + " | ".join(r["content"][:70] for r in reps))

            async def settled():
                if not await triaged(e1):
                    return None
                c = await case_of(e1)
                return c if (c is None or c["status"] != "open") else None
            c1 = await until(settled, 240, 5)
            c1 = c1 or await case_of(e1)
            if c1 is None:
                check(True, "1 the answered lookup left no case (triage opened none)")
            else:
                x = await exps(c1["id"])
                info(f"case #{c1['id']} {c1['title']!r}: {c1['status']} ({c1['close_reason']}); "
                     f"expectations {[(e['owed_by'], e['status']) for e in x]}")
                check(c1["status"] == "closed" and c1["close_reason"] == ANSWERED, "1 the case closed as answered",
                      f"{c1['status']} / {c1['close_reason']}")
                met = [e for e in x if e["owed_by"] == "assistant" and e["status"] == "satisfied"]
                check(bool(met), "1 the assistant's commitment was met by its reply")
                if met:
                    by = await pool.fetchrow("SELECT provenance, thread_ref FROM events WHERE id = $1",
                                             met[0]["satisfied_by"])
                    check(by and by["provenance"] == "self" and by["thread_ref"] == OWNER,
                          "1 met by a reply in the same chat")
                answered_case = c1
                out["case_1"] = c1["id"]
            check(bool(reps), "1 the reply went out through the (fake) connector",
                  f"{len([s for s in SENT if s['at'] >= t0])} send(s)")
            await asyncio.sleep(15)            # a close notice would have gone out by now
            check(not done_pings(t0), "1 no 'Done' message for the answered request",
                  "; ".join(s["text"][:80] for s in done_pings(t0)))

        if "2" in steps:
            print("2. a request the assistant cannot fulfil", flush=True)
            t0 = time.time()
            e2 = await send("Please get me the signed PDF of our contract with Acme Logistics "
                            "from their supplier portal.")
            await until(lambda: run_done(e2), 420, 5)
            reps = await replies_after(e2)
            info(f"{len(reps)} reply(ies): " + " | ".join(r["content"][:90] for r in reps))

            async def judged():
                c = await case_of(e2)
                if not c:
                    return None
                x = await exps(c["id"])
                return c if any(e["owed_by"] == "assistant" and e["last_checked_at"] for e in x) else None
            await until(judged, 240, 5)
            c2 = await case_of(e2)
            if c2 is None:
                check(False, "2 the request opened a case", "triage judged it not relevant")
            else:
                x = await exps(c2["id"])
                info(f"case #{c2['id']} {c2['title']!r}: {c2['status']}; "
                     f"expectations {[(e['owed_by'], e['status'], e['description'][:60]) for e in x]}")
                check(c2["status"] == "open", "2 the case stays open", c2["status"])
                check(any(e["owed_by"] == "assistant" and e["status"] == "open" for e in x),
                      "2 with the assistant's open commitment")
                out["case_2"] = c2["id"]
            check(not done_pings(t0), "2 no 'Done' message")

        if "4" in steps:
            print("4. a follow-up about the answered request", flush=True)
            before = await pool.fetchval("SELECT COALESCE(max(id), 0) FROM cases")
            e4 = await send("did you send me the engine specs I asked for?")
            await until(lambda: run_done(e4), 300, 5)
            await until(lambda: triaged(e4), 120, 3)
            await asyncio.sleep(20)
            c4 = await case_of(e4)
            new = await pool.fetch("SELECT id, title, status, close_reason FROM cases WHERE id > $1", before)
            verdict = await pool.fetchval("SELECT triage_status FROM events WHERE id = $1", e4)
            info(f"event {e4} triage_status={verdict}; linked case: {c4['id'] if c4 else None}; "
                 f"new cases: {[dict(r) for r in new]}")
            check(not new, "4 the follow-up opened no new case",
                  "; ".join(f"#{r['id']} {r['status']} {r['close_reason']}" for r in new))
            if answered_case and c4:
                check(c4["id"] != answered_case["id"] or c4["status"] == "closed",
                      "4 the answered case was not reopened")

        if "5" in steps:
            print("5. a colleague asks the assistant in a group", flush=True)
            group, colleague = "120363000000000024@g.us", "15550100008"
            await pool.execute(
                """INSERT INTO chats (jid, subject, is_group, status, purpose)
                   VALUES ($1, 'Scenario Site Ops', TRUE, 'active', 'site operations (scenario)')
                   ON CONFLICT (jid) DO UPDATE SET status = 'active'""", group)
            t0 = time.time()
            since = await pool.fetchval("SELECT COALESCE(max(id), 0) FROM events")
            text = "@assistant how many customers do we have in the database?"
            call("POST", "/api/test-message", {"phone": colleague, "message": text, "chatId": group,
                                               "isGroup": True, "mentioned": True, "name": "Ravi"})

            async def gev():
                return await pool.fetchval(
                    """SELECT id FROM events WHERE id > $1 AND provenance = 'external'
                         AND thread_ref = $2 ORDER BY id LIMIT 1""", since, group)
            e5 = await until(gev, 30, 1)
            meta = json.loads(await pool.fetchval("SELECT meta FROM events WHERE id = $1", e5) or "{}")
            info(f"event {e5} recorded in the group (to_assistant={meta.get('to_assistant')})")

            async def group_reply():
                return await pool.fetchval(
                    """SELECT count(*) FROM events WHERE thread_ref = $1 AND provenance = 'self'
                         AND id > $2""", group, e5)
            await until(group_reply, 300, 5)

            async def settled5():
                if not await triaged(e5):
                    return None
                c = await case_of(e5)
                return c if (c is None or c["status"] != "open") else None
            c5 = await until(settled5, 240, 5) or await case_of(e5)
            if c5 is None:
                check(True, "5 the answered group question left no case (triage opened none)")
            else:
                info(f"case #{c5['id']} {c5['title']!r}: {c5['status']} ({c5['close_reason']})")
                check(c5["status"] == "closed" and c5["close_reason"] == ANSWERED, "5 the group case closed as answered",
                      f"{c5['status']} / {c5['close_reason']}")
                out["case_5"] = c5["id"]
            check(any(s["phone"] == group for s in SENT if s["at"] >= t0), "5 the reply went to the group")
            await asyncio.sleep(15)
            check(not done_pings(t0), "5 no 'Done' message to anyone")

        sends = [s for s in SENT if s["phone"]]
        out["sends"] = len(sends)
        out["done_pings"] = len(done_pings(0))
    finally:
        call("PUT", "/api/config", {"values": {"wa_api_base": "", "owner_number": ""}})
        server.shutdown()
        info("wa_api_base and owner_number cleared; fake connector stopped")
    print(json.dumps(out, default=str))
    print(f"scenario_answered_requests: {len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--steps", default="1,2,4,5", help="comma-separated steps to run (1,2,4,5)")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(set(a.steps.split(",")))))
