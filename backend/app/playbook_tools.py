"""Playbook tools — where autonomy tiers are ENFORCED.

Tier is checked in the tool, never in the prompt (plan §7.3, default deny):
  - notify              → may only notify the operator/requester
  - autonomous_message  → may send WhatsApp reminders + escalate + propose drafts
  - draft               → may only propose drafts for human approval

A draft-tier playbook that calls send_reminder gets a hard refusal string, so a
misbehaving LLM cannot exceed the autonomy it was granted.

Every side effect is recorded in `actions` for audit, and outbound WhatsApp goes
through whatsapp.send_message (which records self-provenance, excluding the echo
from triage — no feedback loop).
"""

import json
import logging
import time
from contextvars import ContextVar

from langchain_core.tools import tool

from . import whatsapp
from .agents.graph import _log_tool, ctx_phone, ctx_run_id
from .config import get_settings
from .db import get_pool

log = logging.getLogger("playbook.tools")

# Set by the playbook runner before invoking the agent.
ctx_tier: ContextVar[str | None] = ContextVar("ctx_tier", default=None)
ctx_case_id: ContextVar[int | None] = ContextVar("ctx_case_id", default=None)
ctx_playbook_id: ContextVar[int | None] = ContextVar("ctx_playbook_id", default=None)
ctx_expectation_id: ContextVar[int | None] = ContextVar("ctx_expectation_id", default=None)

SNOOZE_INTERVAL = "1 day"     # after a reminder, re-check the expectation later

# The autonomy matrix — the single source of truth for what each tier may do.
# Enforced in the tools below (default deny), never in a prompt.
# `post_to_group` is deliberately narrower than `send_reminder`: a DM nudges one
# person, a group post is visible to everyone in the chat, so it is granted only
# to the tier that already has permission to message people unprompted.
TIER_CAPABILITIES: dict[str, set[str]] = {
    "notify":             {"notify_owner", "escalate", "case_overview"},
    "autonomous_message": {"notify_owner", "escalate", "case_overview",
                           "send_reminder", "propose_action", "post_to_group"},
    "draft":              {"notify_owner", "case_overview", "propose_action"},
}


def tier_allows(tier: str | None, capability: str) -> bool:
    """Does `tier` permit `capability`? Default deny (unknown tier → no)."""
    return capability in TIER_CAPABILITIES.get(tier or "", set())


_REFUSAL = ("TIER REFUSED: this playbook's autonomy tier '{tier}' does not permit "
            "{what}. Allowed here: {allowed}. Use only your granted tools.")


def _refuse(what: str, allowed: str) -> str:
    return _REFUSAL.format(tier=ctx_tier.get() or "unknown", what=what, allowed=allowed)


async def _record_action(kind: str, payload: dict, status: str) -> int | None:
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO actions (case_id, playbook_id, task_run_id, kind, payload, tier, status, executed_at)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7, CASE WHEN $7='executed' THEN now() END)
           RETURNING id""",
        ctx_case_id.get(), ctx_playbook_id.get(), ctx_run_id.get(), kind,
        json.dumps(payload, default=str), ctx_tier.get(), status)


async def _person_phone(name_or_alias: str) -> tuple[int | None, str | None]:
    """Resolve a person name/alias to (person_id, whatsapp). Exact match only."""
    from . import people
    pool = await get_pool()
    async with pool.acquire() as conn:
        pid = await people.resolve(conn, alias=name_or_alias)
        if not pid:
            return None, None
        phone = await conn.fetchval("SELECT whatsapp FROM people WHERE id = $1", pid)
    return pid, phone


async def _origin_chat() -> str | None:
    """The group this case came out of, if it came out of one.

    Stamped by triage from the triggering event's thread_ref, so a commitment
    made in a group can be followed up where it was made — not only in a DM
    the rest of the group never sees.
    """
    case_id = ctx_case_id.get()
    if not case_id:
        return None
    pool = await get_pool()
    jid = await pool.fetchval(
        "SELECT origin_chat_jid FROM cases WHERE id = $1", case_id)
    # Only groups. A DM's "origin chat" is the person's own thread, and posting
    # there would just duplicate the reminder we already sent them.
    return jid if (jid or "").endswith("@g.us") else None


async def _requester_phone() -> str | None:
    """Phone of the case's requester, else the configured owner number."""
    case_id = ctx_case_id.get()
    if case_id:
        pool = await get_pool()
        phone = await pool.fetchval(
            """SELECT p.whatsapp FROM case_participants cp JOIN people p ON p.id = cp.person_id
               WHERE cp.case_id = $1 AND cp.role = 'requester' AND p.whatsapp IS NOT NULL
               ORDER BY cp.person_id LIMIT 1""", case_id)
        if phone:
            return phone
    return get_settings().owner_number or None


# ── tools ──────────────────────────────────────────────────────────────────

@tool
async def send_reminder(person: str, text: str) -> str:
    """Send a WhatsApp reminder to a named person. Only usable by playbooks with
    the 'autonomous_message' tier. Increments the expectation's reminder count and
    reschedules its next check."""
    start = time.monotonic()
    if not tier_allows(ctx_tier.get(), "send_reminder"):
        result = _refuse("sending a WhatsApp reminder", "propose_action / escalate / notify_owner")
        await _log_tool("send_reminder", {"person": person}, result, start)
        return result

    # Reminder cap — read the expectation BEFORE sending. Once max_reminders is
    # reached the right move is escalation, not an endless drip of nudges. The
    # refusal is still audited as an actions row so the dashboard shows why
    # nothing went out. (Tier gate above must stay the FIRST check.)
    exp_id = ctx_expectation_id.get()
    if exp_id:
        pool = await get_pool()
        exp = await pool.fetchrow(
            "SELECT reminder_count, max_reminders FROM expectations WHERE id = $1",
            exp_id)
        if exp:
            cap = exp["max_reminders"] if exp["max_reminders"] is not None else 5
            count = exp["reminder_count"] or 0
            if count >= cap:
                result = (f"reminder cap reached ({count}/{cap}) — do not send more "
                          "reminders for this expectation; use the escalate tool.")
                await _record_action(
                    "reminder_cap",
                    {"recipient": person, "expectation_id": exp_id,
                     "reminder_count": count, "max_reminders": cap,
                     "suppressed_text": text[:500]},
                    "executed")
                await _log_tool("send_reminder", {"person": person}, result, start)
                return result

    pid, phone = await _person_phone(person)
    if not phone:
        result = f"ERROR: '{person}' is not a registered person with a WhatsApp number."
        await _log_tool("send_reminder", {"person": person}, result, start)
        return result

    sent = await whatsapp.send_message(phone, text)
    await _record_action("whatsapp_message", {"recipient": person, "phone": phone, "body": text},
                         "executed" if sent else "failed")
    exp_id = ctx_expectation_id.get()
    if sent and exp_id:
        pool = await get_pool()
        await pool.execute(
            f"""UPDATE expectations SET reminder_count = reminder_count + 1, status = 'open',
                due_at = now() + interval '{SNOOZE_INTERVAL}', last_checked_at = now()
                WHERE id = $1""", exp_id)
    # Dual channel: the owner gets the nudge privately, and the group where the
    # commitment was made sees that it is being chased — so the requester does
    # not have to ask twice, and nobody re-does work already in hand.
    posted = ""
    jid = await _origin_chat() if sent else None
    if jid and tier_allows(ctx_tier.get(), "post_to_group"):
        if await whatsapp.send_message(jid, text):
            await _record_action("whatsapp_group_message",
                                 {"chat": jid, "body": text, "about": person}, "executed")
            posted = " (and posted to the group)"

    result = (f"reminder sent to {person}{posted}" if sent
              else "ERROR: WhatsApp send failed")
    await _log_tool("send_reminder", {"person": person, "text": text[:200]}, result, start)
    return result


@tool
async def post_to_group(text: str) -> str:
    """Post a short message to the WhatsApp group this case came from. Only usable
    by playbooks with the 'autonomous_message' tier, and only when the case
    actually originated in a group. Everyone in the chat sees it — use it to keep
    the group informed, not to chase an individual (use send_reminder for that)."""
    start = time.monotonic()
    if not tier_allows(ctx_tier.get(), "post_to_group"):
        result = _refuse("posting to a WhatsApp group",
                         "notify_owner / escalate / propose_action")
        await _log_tool("post_to_group", {}, result, start)
        return result

    jid = await _origin_chat()
    if not jid:
        result = "ERROR: this case did not come from a WhatsApp group — nothing to post to."
        await _log_tool("post_to_group", {}, result, start)
        return result

    sent = await whatsapp.send_message(jid, text)
    await _record_action("whatsapp_group_message", {"chat": jid, "body": text},
                         "executed" if sent else "failed")
    result = f"posted to {jid}" if sent else "ERROR: WhatsApp send failed"
    await _log_tool("post_to_group", {"chat": jid, "text": text[:200]}, result, start)
    return result


@tool
async def escalate(text: str) -> str:
    """Escalate a case to its requester (or the operator) instead of nudging the
    owner again. Marks the triggering expectation as violated so it stops
    re-firing. Usable by 'autonomous_message' and 'notify' tiers."""
    start = time.monotonic()
    if not tier_allows(ctx_tier.get(), "escalate"):
        result = _refuse("escalating via message", "propose_action")
        await _log_tool("escalate", {}, result, start)
        return result
    phone = await _requester_phone()
    if not phone:
        result = "ERROR: no requester or owner number configured to escalate to."
        await _log_tool("escalate", {}, result, start)
        return result
    sent = await whatsapp.send_message(phone, text)
    await _record_action("whatsapp_message", {"recipient": "requester", "phone": phone, "body": text},
                         "executed" if sent else "failed")
    exp_id = ctx_expectation_id.get()
    if exp_id:
        pool = await get_pool()
        await pool.execute("UPDATE expectations SET status = 'violated', last_checked_at = now() WHERE id = $1", exp_id)
    result = "escalated to requester" if sent else "ERROR: WhatsApp send failed"
    await _log_tool("escalate", {"text": text[:200]}, result, start)
    return result


# Kinds with a dedicated, validating proposal tool; the generic tool must not mint them.
RESERVED_ACTION_KINDS = {"context_change", "record_extracted_data", "agent_task"}


@tool
async def propose_action(kind: str, payload_json: str, summary: str) -> str:
    """Propose an action for HUMAN APPROVAL (a draft). Use for anything with an
    external side effect other than a WhatsApp reminder — emails, POs, etc. Inserts
    a proposed action into the approvals queue and notifies the requester/operator.
    payload_json is a JSON object string (e.g. recipient, body, attachments)."""
    start = time.monotonic()
    if not tier_allows(ctx_tier.get(), "propose_action"):
        result = _refuse("proposing an action", "notify_owner")
        await _log_tool("propose_action", {"kind": kind}, result, start)
        return result
    if (kind or "").strip().lower() in RESERVED_ACTION_KINDS:
        result = f"REFUSED: kind '{kind}' can only be proposed by its dedicated tool."
        await _log_tool("propose_action", {"kind": kind}, result, start)
        return result
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else dict(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {"raw": str(payload_json)}
    payload["_summary"] = summary
    aid = await _record_action(kind, payload, "proposed")
    phone = await _requester_phone()
    if phone:
        await whatsapp.send_message(
            phone, f"📝 A draft action is awaiting your approval in the dashboard: {summary[:200]}")
    result = f"proposed action #{aid} ({kind}) — awaiting human approval"
    await _log_tool("propose_action", {"kind": kind, "summary": summary[:200]}, result, start)
    return result


@tool
async def notify_owner(text: str) -> str:
    """Send a plain notification to the operator (owner number). Allowed for any
    tier — this is the baseline 'notify' capability."""
    start = time.monotonic()
    owner = get_settings().owner_number
    if not owner:
        result = "ERROR: no owner_number configured."
    else:
        sent = await whatsapp.send_message(owner, text)
        await _record_action("whatsapp_message", {"recipient": "owner", "phone": owner, "body": text},
                             "executed" if sent else "failed")
        result = "notified owner" if sent else "ERROR: WhatsApp send failed"
    await _log_tool("notify_owner", {"text": text[:200]}, result, start)
    return result


@tool
async def case_overview() -> str:
    """Read-only overview of open cases and their expectation counts, for digests."""
    start = time.monotonic()
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT c.id, c.title,
                  count(*) FILTER (WHERE e.status='open') AS open_exp,
                  count(*) FILTER (WHERE e.status='violated') AS violated_exp
           FROM cases c LEFT JOIN expectations e ON e.case_id = c.id
           WHERE c.status = 'open' AND NOT c.sandbox
           GROUP BY c.id ORDER BY violated_exp DESC, c.id DESC LIMIT 40""")
    overview = [{"case": r["id"], "title": r["title"], "open_expectations": r["open_exp"],
                 "violated_expectations": r["violated_exp"]} for r in rows]
    result = json.dumps(overview, default=str)
    await _log_tool("case_overview", {}, result, start)
    return result


PLAYBOOK_TOOL_REGISTRY = {
    "send_reminder": send_reminder,
    "post_to_group": post_to_group,
    "escalate": escalate,
    "propose_action": propose_action,
    "notify_owner": notify_owner,
    "case_overview": case_overview,
}



# ── Phase 7: exploration tools ──
import asyncpg  # noqa: E402
import uuid  # noqa: E402

from . import insights  # noqa: E402

# Both tools only write approval-gated rows (a case note, a proposed action), so
# they belong to draft and to autonomous_message, which is a superset of draft.
# Appended by union (not in the literal above) so parallel phases merge cleanly.
TIER_CAPABILITIES["draft"] |= {"record_insight", "propose_kpi"}
TIER_CAPABILITIES["autonomous_message"] |= {"record_insight", "propose_kpi"}

# Only propose_kpi mints create_watch, so the generic propose_action cannot queue
# an unvalidated watch.
RESERVED_ACTION_KINDS |= {"create_watch"}
# Quick 020: a learning nobody could attribute is approved only through the card
# remember_fact writes with it (memscope.propose_activation), never a playbook draft.
RESERVED_ACTION_KINDS |= {"activate_learning"}


@tool
async def record_insight(title: str, finding: str, evidence_sql: str = "", severity: str = "info",
                         same_as_case: int = 0, summary: str = "", impact: str = "",
                         next_step: str = "", key_figures: list[str] | None = None,
                         evidence_measure: dict | None = None) -> str:  # Phase 17 (CONS-04)
    """Record ONE notable, reproducible finding from studying the approved business
    data. title states the fact with a number; finding is 2-4 sentences with the
    period, magnitude, baseline and a caveat; evidence_sql is ONE read-only SELECT
    over the APPROVED TABLES with absolute date literals (for example
    DATE '2026-08-01', never now() or current_date) that returns the rows proving
    it; severity is info, warning or critical. A warning (costs money if it continues)
    or critical (the owner must act this week) finding opens its own case for the
    owner; info is kept on the Insights page only. same_as_case: when the finding is
    about the same subject as one of the OPEN INSIGHT CASES in your context, pass that
    case number and it is added there instead of opening a new case; otherwise leave
    it 0. For the owner, in plain words: summary (1-2 sentences, what you found),
    impact (why it matters), next_step (one concrete check or action) and
    key_figures (2-4 "Label: value" pairs from tool output, e.g. "South orders in Sep:
    210"); all optional, each under 300 characters. Instead of evidence_sql you may pass
    evidence_measure {measure, by, grain, filters, range:{start,end}} (an absolute range)
    for a number a governed measure covers: that finding is stored verified, while a
    raw evidence_sql finding is stored as unverified. The tool re-runs the SQL and refuses
    if it fails, returns no rows or touches an unapproved table. Record nothing
    when nothing is notable."""
    start = time.monotonic()
    args = {"title": (title or "")[:120], "severity": severity}
    if same_as_case:
        args["same_as_case"] = same_as_case
    if not tier_allows(ctx_tier.get(), "record_insight"):
        result = _refuse("recording an insight", "only the tools you were granted")
        await _log_tool("record_insight", args, result, start, refused=True)
        return result
    try:
        sev = (severity or "").strip().lower()
        t = title.strip() if isinstance(title, str) else ""
        f = finding.strip() if isinstance(finding, str) else ""
        reason = None
        if sev not in insights.INSIGHT_SEVERITIES:
            reason = f"severity must be one of {', '.join(insights.INSIGHT_SEVERITIES)}"
        elif not 1 <= len(t) <= 120:
            reason = "title must be 1-120 characters"
        elif not 1 <= len(f) <= 2000:
            reason = "finding must be 1-2000 characters"
        elif insights.has_hidden_chars(t) or insights.has_hidden_chars(f):
            reason = "title and finding must not contain control or hidden characters"
        sql = (evidence_sql or "").strip()
        if not reason and sql and evidence_measure:
            reason = "pass evidence_sql or evidence_measure, not both"
        elif not reason and not sql and not evidence_measure:
            reason = "evidence_sql or evidence_measure is required"
        if reason:
            result = f"REFUSED: {reason}"
        else:
            # Phase 17 (CONS-04): lane and verified come from how the evidence was produced.
            lane = {"lane": "raw_sql", "verified": False}
            if evidence_measure:
                evidence, sql, mname, why = await insights.check_measure_evidence(evidence_measure)
                lane = {"lane": "measure", "verified": True, "measure": mname}
            else:
                evidence, why = await insights.check_evidence(sql)
                f = f + " (unverified: raw SQL)"
            if evidence is None:
                result = f"REFUSED: {why}. Nothing was recorded."
            else:
                extra, dropped = insights.clean_owner_fields(summary, impact, next_step,
                                                             key_figures)
                eid, case_id, why = await insights.record_note(
                    t, f, sql, sev, evidence,
                    case_id=ctx_case_id.get(), run_id=ctx_run_id.get(),
                    same_as_case=same_as_case or None, extra=extra,
                    lane=lane)  # Phase 17 (CONS-04)
                if eid is None:
                    result = f"REFUSED: {why}"
                else:
                    result = (f"recorded insight (note event #{eid}) on case #{case_id}: "
                              f"'{t}' - evidence returned {evidence['row_count']} rows")
                    if evidence.get("relative_dates"):
                        result += (" Note: the evidence SQL uses a relative date; prefer "
                                   "an absolute date literal so it reproduces.")
                    if dropped:
                        result += " Not kept: " + "; ".join(dropped) + "."
    except Exception as e:  # never raise into the agent loop
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("record_insight", args, result, start,
                    refused=result.startswith("REFUSED"))
    return result


@tool
async def propose_kpi(title: str, condition: str, cadence: str = "daily", rationale: str = "",
                      measure: str = "", filters: list[dict] | None = None,
                      range: str | None = None, alert: dict | str | None = None) -> str:  # Phase 17 (CONS-01)
    """Propose ONE KPI watch nobody asked for, for OWNER APPROVAL; nothing runs until
    the owner approves. title is a short label; condition names the tables,
    columns, filter, window and threshold explicitly so the watchdog can compile it
    once into a one-row SELECT (for example "Count of South-region sales_orders in
    the trailing 7 days falls below 12"); cadence is hourly, daily or weekly (or a
    number of minutes, at least 15); rationale names the insight behind it. At most
    2 per run; never re-propose a KPI listed as already watched, proposed or
    declined. When the approved model has a measure for the number, pass
    measure=<name> (optional filters, a relative range, alert) instead of a SQL-style
    condition."""
    start = time.monotonic()
    args = {"title": (title or "")[:120], "cadence": cadence}
    if not tier_allows(ctx_tier.get(), "propose_kpi"):
        result = _refuse("proposing a KPI watch", "record_insight")
        await _log_tool("propose_kpi", args, result, start, refused=True)
        return result
    try:
        mfields = None
        mrefusal = None
        if measure:  # Phase 17 (CONS-01)
            mfields, mrefusal = await insights.measure_payload(measure, filters, range, alert, title, condition)
            if mfields:
                condition = mfields["condition"]
        reason = insights.validate_kpi(title, condition)
        seconds = insights.parse_cadence(cadence)
        if mrefusal:
            result = mrefusal
        elif reason:
            result = f"REFUSED: {reason}"
        elif seconds is None:
            result = ("REFUSED: cadence must be hourly, daily, weekly or a number of "
                      "minutes (at least 15)")
        else:
            title_key = insights.norm_title(title)
            if not title_key:
                result = "REFUSED: title needs letters or digits"
            else:
                async with insights.KPI_LOCK:
                    conflict = await insights.kpi_conflict(title_key, ctx_run_id.get())
                    if conflict:
                        result = f"REFUSED: {conflict}"
                    else:
                        label = insights.cadence_label(seconds)
                        payload = {
                            "_summary": f"Watch a new KPI: {title.strip()} ({label})",
                            "title": title.strip(), "title_key": title_key,
                            "condition": condition.strip(), "cadence": label,
                            "cadence_seconds": seconds,
                            "rationale": (rationale or "").strip()[:1000],
                            "origin": "exploration", "proposal_key": uuid.uuid4().hex,
                        }
                        if mfields:  # Phase 17 (CONS-01)
                            payload["measure_ref"] = mfields["measure_ref"]
                            payload["trigger_preset"] = mfields["trigger_preset"]
                            payload["_summary"] = (f"Watch a new KPI: {title.strip()} ({label}), "
                                                   f"measure {measure}")
                        try:
                            aid = await _record_action("create_watch", payload, "proposed")
                            result = (f"proposed KPI watch (action #{aid}): '{title.strip()}' every "
                                      f"{label} - awaiting owner approval. Nothing runs until approved.")
                        except asyncpg.UniqueViolationError:
                            result = "already proposed: a KPI with this title is awaiting approval"
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("propose_kpi", args, result, start,
                    refused=result.startswith("REFUSED"))
    return result


PLAYBOOK_TOOL_REGISTRY["record_insight"] = record_insight
PLAYBOOK_TOOL_REGISTRY["propose_kpi"] = propose_kpi
# ── end Phase 7 ──
