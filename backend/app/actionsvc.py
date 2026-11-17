"""Action service — the human-approval execution layer.

Tier-'draft' playbooks (and anything with an external side effect other than a
WhatsApp reminder) don't act; they insert a proposed `actions` row. A human
approves in the dashboard, and approval runs the kind's executor.

v1 executors (plan §7.5):
  * whatsapp_message — actually sends via the connector.
  * email_draft / po_draft — no provider yet, so approval just marks executed and
    the payload (the drafted content) is what the human copies out. This keeps the
    approval workflow real while the email/ERP-write integrations stay out of scope.
  * publish_dashboard (Phase 6) — publishes the approved draft via the row-locked pointer flip.
"""

import json
import logging

from . import appchannel, whatsapp
from .config import get_settings
from .db import get_pool

log = logging.getLogger("actionsvc")


async def notify_case_closed(case_id: int, kind: str = "completed") -> bool:
    """Send a close notice to the case's requester. `kind='completed'` announces
    the work is done; `kind='stale'` says the case is being archived for
    inactivity and must never claim completion. Falls back to the configured
    owner number if the requester has no WhatsApp. Best-effort — never raises,
    never blocks the close. Records the notice as an action for audit.

    Quick 024: `kind='answered'` (a request the assistant answered in the chat it
    was asked in) sends nothing: the requester already has the answer there, and a
    "✅ Done" on top of it is noise. triage._close_case does not call this for that
    kind either; the guard keeps any other caller from sending one."""
    if kind == "answered":
        return False
    try:
        pool = await get_pool()
        case = await pool.fetchrow("SELECT title, summary FROM cases WHERE id = $1", case_id)
        if not case:
            return False
        try:  # Quick 006: finding cases tell the owner where the finding went
            if await _notify_finding_case(pool, case_id, kind):
                return True
        except Exception:
            log.exception("finding-case close notice failed for %s", case_id)
        phone = await pool.fetchval(
            """SELECT p.whatsapp FROM case_participants cp JOIN people p ON p.id = cp.person_id
               WHERE cp.case_id = $1 AND cp.role = 'requester' AND p.whatsapp IS NOT NULL
               ORDER BY cp.person_id LIMIT 1""", case_id)
        phone = phone or get_settings().owner_number
        if not phone:
            log.info("case %s closed but no requester/owner number to notify", case_id)
            return False
        if kind == "stale":
            msg = (f"Closing “{case['title']}” — no recent activity, so it has been "
                   "archived. Reply if this still needs attention.")
        else:
            msg = f"✅ Done — “{case['title']}” is complete."
        if case["summary"]:
            msg += f"\n\n{case['summary'][:400]}"
        ok = await whatsapp.send_message(phone, msg)
        await pool.execute(
            """INSERT INTO actions (case_id, kind, payload, tier, status, executed_at)
               VALUES ($1, 'whatsapp_message', $2::jsonb, 'notify',
                       CASE WHEN $3 THEN 'executed' ELSE 'failed' END,
                       CASE WHEN $3 THEN now() END)""",
            case_id, json.dumps({"recipient": "requester", "phone": phone,
                                 "body": msg, "close_kind": kind}), ok)
        return ok
    except Exception:
        log.exception("case-closed notification failed for %s", case_id)
        return False


async def _notify_finding_case(pool, case_id: int, kind: str) -> bool:
    """Quick 006: a case opened from a data finding tells the owner in the thread the
    finding went to (the in-app chat on an install without WhatsApp), with wording for
    how it ended; nothing is sent when the owner closed it themselves. Returns False
    for an ordinary (triage) case, which keeps the requester path above unchanged."""
    from . import findingcases
    origin = await pool.fetchval("SELECT origin FROM cases WHERE id = $1", case_id)
    if origin not in findingcases.FINDING_ORIGINS:
        return False
    row = await pool.fetchrow(
        "SELECT id, title, summary, origin, origin_chat_jid FROM cases WHERE id = $1", case_id)
    if not row:
        return False
    case = dict(row)
    msg = findingcases.notice_text(case, kind)
    if msg is None:
        return True
    thread = findingcases.notice_thread(case)
    try:
        ok = await findingcases.deliver_notice(thread, msg)
    except Exception as e:
        log.warning("finding case %s close notice not delivered (%s)", case_id, type(e).__name__)
        ok = False
    await pool.execute(
        """INSERT INTO actions (case_id, kind, payload, tier, status, executed_at)
           VALUES ($1, $2, $3::jsonb, 'notify',
                   CASE WHEN $4 THEN 'executed' ELSE 'failed' END,
                   CASE WHEN $4 THEN now() END)""",
        case_id, "app_message" if appchannel.is_app_thread(thread) else "whatsapp_message",
        json.dumps({"recipient": "owner", "thread_id": thread, "body": msg,
                    "close_kind": kind}), ok)
    return True


# Phase 8: WR-01 - optional kind / exclude_kind filters (bound parameters only)
async def list_actions(status: str = "proposed", limit: int = 100,
                       kind: str | None = None,
                       exclude_kind: str | None = None) -> list[dict]:
    pool = await get_pool()
    if status:
        rows = await pool.fetch(
            """SELECT a.*, c.title AS case_title, pb.name AS playbook_name
               FROM actions a LEFT JOIN cases c ON c.id = a.case_id
               LEFT JOIN playbooks pb ON pb.id = a.playbook_id
               WHERE a.status = $2 AND ($3::text IS NULL OR a.kind = $3)
               AND ($4::text IS NULL OR a.kind <> $4)
               ORDER BY a.id DESC LIMIT $1""", limit, status, kind, exclude_kind)
    else:
        rows = await pool.fetch(
            """SELECT a.*, c.title AS case_title, pb.name AS playbook_name
               FROM actions a LEFT JOIN cases c ON c.id = a.case_id
               LEFT JOIN playbooks pb ON pb.id = a.playbook_id
               WHERE ($2::text IS NULL OR a.kind = $2)
               AND ($3::text IS NULL OR a.kind <> $3)
               ORDER BY a.id DESC LIMIT $1""", limit, kind, exclude_kind)
    return [dict(r) for r in rows]


async def approve(action_id: int, decided_by: int | None = None) -> dict:
    """Approve and execute a proposed action. Returns the resulting state."""
    pool = await get_pool()
    # Claim it so a double-click can't execute twice.
    action = await pool.fetchrow(
        """UPDATE actions SET status = 'approved', decided_by = $2
           WHERE id = $1 AND status = 'proposed' RETURNING *""", action_id, decided_by)
    if not action:
        return {"error": "action is not in a proposed state"}

    kind = action["kind"]
    payload = action["payload"]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)

    try:
        executed = await _execute(kind, payload)
    except Exception as e:
        log.exception("action %s executor crashed", action_id)
        await pool.execute("UPDATE actions SET status = 'failed' WHERE id = $1", action_id)
        if getattr(e, "status", None) in (404, 409, 422):  # Quick 052: a plain refusal
            return {"error": f"executor failed: {e}"}
        return {"error": f"executor failed: {type(e).__name__}: {e}"}

    status = "executed" if executed else "failed"
    await pool.execute(
        "UPDATE actions SET status = $2, executed_at = CASE WHEN $2='executed' THEN now() END WHERE id = $1",
        action_id, status)
    return {"action": action_id, "status": status}


BULK_MAX = 1000          # ids per bulk-approve call
BULK_CONCURRENCY = 8     # approvals in flight; each spends most of its time in the embedding call


async def approve_many(ids: list[int], decided_by: int | None = None) -> dict:
    """Quick 001: approve many proposals in one request. Every id goes through the same
    approve() as a single approval (claim a 'proposed' row, run the kind's executor,
    record executed or failed), so each keeps its own approval record. Bounded
    concurrency; one entry per requested id, in request order."""
    import asyncio
    clean: list[int] = []
    for i in ids or []:
        if isinstance(i, int) and not isinstance(i, bool) and i > 0 and i not in clean:
            clean.append(i)
    sem = asyncio.Semaphore(BULK_CONCURRENCY)

    async def one(aid: int) -> dict:
        async with sem:
            try:
                res = await approve(aid, decided_by)
            except Exception as e:  # one bad item never stops the batch
                log.exception("bulk approve: action %s crashed", aid)
                return {"id": aid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        if "error" in res:
            return {"id": aid, "ok": False, "error": res["error"]}
        return {"id": aid, "ok": res.get("status") == "executed", "status": res.get("status")}

    results = await asyncio.gather(*(one(a) for a in clean))
    ok = sum(1 for r in results if r["ok"])
    return {"results": list(results), "requested": len(clean), "approved": ok,
            "failed": len(results) - ok}


async def reject(action_id: int, decided_by: int | None = None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE actions SET status = 'rejected', decided_by = $2
           WHERE id = $1 AND status = 'proposed' RETURNING id, kind, payload""",
        action_id, decided_by)
    if not row:
        return {"error": "action is not in a proposed state"}

    # A rejected extraction must stop looking like one that was accepted:
    # otherwise it is indistinguishable from "awaiting review" in the Documents
    # list, and search_documents keeps quoting a number a human refused.
    if row["kind"] == "record_extracted_data":
        try:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            doc_id = (payload or {}).get("document_id")
            if doc_id:
                await pool.execute(
                    "UPDATE documents SET status = 'rejected' WHERE id = $1", doc_id)
        except Exception:
            log.exception("could not mark document rejected for action %s", action_id)

    # ── Quick 020: a rejected unattributed learning is retired, never recalled ──
    if row["kind"] == "activate_learning":
        from . import memscope
        await memscope.retire_rejected(row["payload"], action_id)  # never raises
    # end Quick 020

    return {"rejected": action_id}


async def _execute(kind: str, payload: dict) -> bool:
    """Dispatch to the kind's executor. Unknown/no-provider kinds are recorded as
    executed (the payload is the deliverable the human copies out)."""
    if kind == "whatsapp_message":
        phone = payload.get("phone") or payload.get("recipient")
        body = payload.get("body") or payload.get("message") or ""
        if not phone or not body:
            return False
        return await whatsapp.send_message(str(phone), str(body))

    if kind == "record_extracted_data":
        # Approved document extraction → extracted_records (working-memory DB;
        # the tenant business DB is read-only and never written).
        from . import docintel
        return await docintel.execute_record(payload)

    if kind == "agent_task":
        # Approved next-step → queue a normal agent task on the payload's thread.
        from . import docintel
        return await docintel.execute_agent_task(payload)

    if kind == "group_question":
        # Approved ask-in-group proposal → post the question to the case's origin
        # group. Without this executor an approved proposal would no-op silently.
        chat = payload.get("chat")
        body = payload.get("body")
        if not chat or not body:
            return False
        return await whatsapp.send_message(str(chat), str(body))

    # ── Phase 2: context store ──
    if kind == "context_change":
        from . import contextstore
        return await contextstore.apply(payload)

    # ── Phase 7: exploration ──
    if kind == "create_watch":
        # The executor re-validates the untrusted payload and hard-codes provenance;
        # origin_ref makes a retry insert nothing, and approve() already claims single-winner.
        from . import insights
        return await insights.create_watch_from_proposal(payload)

    # ── Phase 6: dashboards ──
    if kind == "publish_dashboard":
        # approve() already claimed single-winner; the executor re-validates the payload
        # and publish() row-locks the dashboard.
        from . import dashboards
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await dashboards.apply_publish_proposal(conn, payload)

    # ── Phase 12 (CTX-08): owner-confirmed hide from a context pack ──
    if kind == "hide_table":
        ref = payload.get("ref")
        if not isinstance(ref, str) or not ref.startswith("business_db.") or len(ref) > 300:
            return False
        from . import contextstore
        return bool(await contextstore.set_table_hidden(ref, True))

    # ── Phase 13.1: forecast definitions ──
    if kind == "create_forecast":
        from . import forecasts
        return await forecasts.create_from_proposal(payload)

    # ── Phase 16: semantic model ──
    if kind == "model_change":
        # approve() already claimed single-winner; apply re-validates the payload, recompiles
        # measures, runs model-schema DDL as wh_owner, then upserts the model row.
        from . import semanticmodel
        return await semanticmodel.apply(payload)
    # ── End Phase 16 ──
    # ── Phase 15: sources (SRC-04) ──
    if kind == "source_change":
        from . import sourcesvc
        return await sourcesvc.apply_source_change(payload)
    # ── Quick 020: the owner approves a learning nobody we could name wrote ──
    if kind == "activate_learning":
        # approve() already claimed single-winner; the executor re-validates the
        # id and flips only a 'pending' row (an already active one counts as done).
        from . import memscope
        return await memscope.activate_from_proposal(payload)
    # end Quick 020
    # ── Quick 047: an approved daily report becomes an active cron playbook ──
    if kind == "create_scheduled_report":
        # approve() already claimed single-winner; the executor re-validates the payload,
        # decides the recipient shape and inserts idempotently on the proposal key.
        from . import reports
        return await reports.create_from_proposal(payload)
    # end Quick 047

    # email_draft, po_draft, and any other kind: no live provider (out of scope).
    # Mark executed so the approval loop is real; the drafted content lives in the
    # payload for the operator to send from their own tool.
    log.info("action kind '%s' has no live executor; recorded as executed (draft only)", kind)
    return True
