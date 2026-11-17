"""Triage — the cheap gate that turns raw events into cases + expectations.

A batch loop pulls pending, external-provenance events and asks a cheap model
(triage_model) to decide, per event: is it relevant? attach to an existing case,
open a new one, or ignore? what expectations does it create or satisfy?

Cost discipline (plan §6.6): the prompt stays small — open-case index capped at
30 most-recent, event content truncated. The verdict is applied transactionally:
open case, link case_events, resolve participants via the people registry, insert
expectations, flip satisfied ones.

Autonomy: triage reads + records structure and never touches the business DB. It
has exactly ONE narrow outbound ability: when a missing critical detail (due date,
amount, owner) blocks tracking, it may post a single short question to the case's
origin GROUP chat — every guard (mode switch, active group only, per-case cooldown,
length cap) is enforced in code, not the prompt. Those sends record
provenance='self', so they can never re-enter triage and loop. All other
follow-ups happen later in the expectation engine.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import asyncpg

from .config import get_settings
from .db import get_pool
from . import llmclient
from . import bizclock  # Quick 049: business-timezone dates
from . import jobruns  # Quick 008: triage batches show on Runs
from . import modelgate  # Quick 039: backoff, attempt cap and spend-limit pause

log = logging.getLogger("triage")

BATCH_SIZE = 20
EVENT_CONTENT_CAP = 1000     # chars per event in the prompt
OPEN_CASE_CAP = 30           # most-recent open cases shown to the model
SUMMARY_REFRESH_EVENTS = 5   # refresh rolling summary after this many new events
MAX_CHAIN_DEPTH = 3          # follow-on cases may nest this deep, no further
GROUP_QUESTION_COOLDOWN_HOURS = 12   # at most one group question per case per window
FACT_CONFIRM_COOLDOWN_HOURS = 1      # owner fact-confirmations may repeat sooner
GROUP_QUESTION_MAX_CHARS = 300       # hard cap on an outbound group question
THREAD_CONTEXT_EVENTS = 6            # recent same-thread events shown per event
THREAD_CONTEXT_LINE_CAP = 200        # chars per context line


TRIAGE_SYSTEM = """You are the triage gate of an ambient business assistant. You turn raw \
inbound events (WhatsApp / email / ERP) into structured work: CASES (threads of related \
activity) and EXPECTATIONS (things someone is now expected to do).

You are given the open cases and a batch of new events. For EACH event return a verdict.

An event is RELEVANT if it creates or advances an obligation, request, commitment, \
question awaiting an answer, or a business condition worth tracking. Chit-chat, \
acknowledgements, and pure FYI are NOT relevant.

Rules:
- Attach to an existing case ONLY when the event shares that case's SUBJECT/deliverable.
Same chat thread or same people is NOT enough — a different topic is a NEW case. E.g. a
licence-renewal request does NOT belong in a monthly-billing case; open a new one.
- An EXPECTATION tracks something someone OWES. For a HUMAN, create one only when a named
PERSON must deliver something (e.g. "Vinu sends the certificate expiry data"), and set that
person as the owner. Never invent work for the ASSISTANT/system that nobody asked it for
(e.g. "produce the report", "run the analysis").
- REQUESTS TO THE ASSISTANT. An event marked "to_assistant": true was addressed to the
assistant, which is replying to it in that chat. When such an event asks the assistant
itself for something (a question, a lookup, figures, a file, a dashboard) and is relevant,
create exactly ONE expectation for it with "owner": "assistant", "due_at": null and a
satisfy_hint naming only what the answer must contain (never "or say it could not be
found": a reply that cannot do it does not meet it). The assistant's own reply meets it; it
stays open, so the case stays visible, when the reply only says it is working on it, could
not find it, or will come back later. If the request also needs a PERSON to deliver
something, create that person's expectation too. Never use owner "assistant" for an event
without "to_assistant": true, and never add a second one when open_expectations already
holds an equivalent "assistant" expectation.
- ALREADY ANSWERED. A message that only checks whether the assistant already did or sent
something ("did you send me the specs?"), when recent_thread_context shows the assistant's
answer or "answered_here" lists that request (requests the assistant already answered in
this chat), is NOT relevant: no case, no expectation. If it says the item never arrived or
is wrong, or asks for something new, it is a new request to the assistant.
- Give each expectation an owner (the person's name or alias as it appears), an optional \
due date (ISO 8601, infer from phrases like "by Friday" or "within 10 minutes" relative to \
the event's occurred_at), and a satisfy_hint describing what would satisfy it.
- DO NOT DUPLICATE. Before creating an expectation, scan open_expectations: if an \
equivalent one already exists (same owner + same deliverable, even if worded differently), \
do NOT create another. Re-phrasings of the same obligation are the SAME expectation.
- SATISFY ON DELIVERY. An expectation is satisfied the moment the awaited item is PROVIDED \
— whether the owner sent it, someone else did, OR the assistant answered it directly from \
data. When that happens, put its id in expectations_satisfied and create NO new expectation \
for the same thing. (Example: an open 'Vinu sends certificate expiry data' + an event where \
the certificate data is delivered → satisfy it, don't reopen it.) Never list an \
"assistant" expectation there: the system checks the assistant's own replies for those.
- One obligation = one expectation. If several events discuss the same deliverable, they \
advance the SAME expectation; never fan one obligation into multiple rows.
- CONTINUATIONS. Events arrive from live conversations, and many are terse \
("Yes", "Almost 18", "done"). Judge each event WITH its recent_thread_context: if the \
context shows the conversation is about an open case's subject, a terse reply that \
advances it (an amount, a confirmation, a status) is RELEVANT to THAT case even though \
the event text alone looks like chit-chat. The same subject discussed on different \
routes (group, DM, email, document readings) is ONE case — attach, don't fork.
- CASE NOTES. When an event (read with its thread context) supplies a concrete FACT \
about a case that is not an obligation — an amount, a payment status, a decision, a \
correction — put a one-line note in "case_note" (e.g. "≈₹18 lakh reported pending \
(group message)"). Quote the figure as stated; NEVER sharpen vague statements into \
precise ones. null when the event adds no durable fact.
- CASE FACTS. When an event states a key TRACKABLE VALUE for a case — an amount \
pending, a due date, a payment status, a decision — also put it in "case_facts" as \
{"field": ..., "value": ...} pairs (snake_case field names like "amount_pending", \
"due_date", "payment_status"), at most 3 per event. A new value for a field \
SUPERSEDES the old one, so corrections ("actually it's 10 lakh") go here — the \
latest value wins while history is kept. Quote the value as stated; NEVER sharpen \
vague statements into precise ones. Reuse the SAME snake_case field name for the \
same concept so corrections land on it.
- ALIASES. When an event refers to a case's subject by an identifier not yet in \
that case's aliases — an invoice number, a document reference, a nickname — put it \
in "aliases" (at most 3 short strings). open_cases shows each case's aliases; USE \
them when matching events to cases, so "INV-2041" attaches to the case even when \
the title says something else.
- OWNER'S PERSPECTIVE. The payload's "owner_company" is the business this assistant \
works for, and "owner_name" (when present) is the business owner — the primary human \
the assistant reports to. Title cases and expectations from the owner's point of view: \
if the owner's company ISSUED an invoice, the obligation is to COLLECT payment from the \
buyer — never "Pay invoice from <owner company>"; if the owner's company RECEIVED one, \
the obligation is to PAY it. When owner_company is unknown, orient from context; don't \
guess.
- OWNER VALIDATION. The owner's standing rule: a business-value item quoted in chat — \
an amount, a price, a deadline, a commercial term — becomes a case FACT only when the \
OWNER ("owner_name") states or confirms it. Stated by anyone ELSE, it is a CLAIM: \
record a case_note ("claimed by <actor>: ..., awaiting confirmation"), do NOT put it \
in case_facts, and set "group_question" asking the owner to confirm, with \
"question_kind": "fact_confirm" — keep it short (e.g. "Confirming: ≈₹18 lakh pending \
on TC-006 — correct?"). Emit case_facts for such items ONLY when the owner states or \
confirms them; the owner's confirmation reply arrives with your question visible in \
recent_thread_context. Document readings (docintel) and db rows are the system's own \
observations, not chat claims.
- CREDENCE. Questions, jokes, sarcasm, hypotheticals, and second-hand rumors are NOT \
facts — at most a case_note, often nothing at all. Money and date fields move only on \
affirmative FIRST-HAND statements; when credence is uncertain, write a note, never a \
fact.
- EMAIL events are external correspondence — the same rules as WhatsApp events apply, \
including attaching to the same case as related chat discussions of the same subject.
- DATABASE CHANGES (an event whose source is "db") are signals, not new threads. Use \
them ONLY to SATISFY an existing expectation they fulfill (e.g. a new machine row \
satisfies "create machine X") or to add a case_note to an EXISTING case. Never open a \
new case for a db row and never create an expectation from one; if it matches nothing, \
mark it not relevant.
- DOCUMENT READINGS (an event whose source is "docintel") are the SYSTEM'S OWN reading \
of a file someone shared in a chat — e.g. an invoice PDF understood in the background. \
They MAY open a case or create expectations when the document implies a HUMAN obligation \
(an invoice implies someone must pay it by its due date). As always, never create an \
expectation for the assistant itself from one.
- UPDATE, DON'T DUPLICATE, DEADLINES AND OWNERS. If an event supplies or corrects the \
deadline or the responsible person of an EXISTING open expectation (e.g. someone answers \
"due date is Sep 15" or "Vinu has to do it"), put {"id": <expectation_id>, "due_at": ..., \
"owner": ...} in expectations_updated (either field may be null) — do NOT create a second \
expectation for the same obligation. open_expectations shows each one's current due_at.
- DATES ARE IN THE FUTURE. The payload's "now" field is the current date-time. Every \
due date you infer ("by Friday", "before this weekend") is the NEXT such date after "now" \
— same year as "now" or later. A due date in the past is always an error and will be \
discarded.
- BUSINESS DATES (Quick 049). "now" and every event's "occurred_business" are on the business calendar (the operator timezone, named in "now"); "occurred_at" is UTC and can fall on a different calendar day. "today", "yesterday", "this week" and any date you write into an expectation description or satisfy_hint are BUSINESS-timezone dates, taken from "occurred_business" / "business date", never from the UTC date of "occurred_at".
- ASK WHEN BLOCKED, SPARINGLY. If ONE missing critical detail (due date, amount, owner) \
prevents tracking an obligation you are recording, you may set "group_question" to a \
single short question to post in the chat (e.g. "What's the due date for INV-2041?"). \
Only for events that came from a group chat, only when tracking is genuinely blocked, \
never more than one question. Usually it is null.
- Be conservative: no invented obligations.

Return ONLY JSON: {"verdicts": [ {
  "event_id": <int>,
  "relevant": <bool>,
  "case": "none" | "new" | <existing_case_id:int>,
  "new_case_title": <string, if case=="new">,
  "participants": [<person name/alias>, ...],
  "expectations_created": [ {"description": <str>, "owner": <person name/alias | "assistant" | null>,
                             "due_at": <ISO8601|null>, "satisfy_hint": <str>} ],
  "expectations_satisfied": [<expectation_id:int>, ...],
  "expectations_updated": [ {"id": <expectation_id:int>, "due_at": <ISO8601|null>,
                             "owner": <person name/alias|null>} ],
  "case_note": <string|null>,
  "case_facts": [ {"field": <snake_case str>, "value": <str>} ],
  "aliases": [<str>, ...],
  "group_question": <string|null>,
  "question_kind": "question" | "fact_confirm",
  "urgency": "low" | "normal" | "high",
  "reason": <short string>
} ] }"""


def _model():
    """Cheap structured-output model for triage. Anthropic triage_model if a key
    is set (it's a claude-haiku by default), else OpenAI cheap model."""
    settings = get_settings()
    if settings.anthropic_api_key:
        return llmclient.chat_anthropic(settings.triage_model, max_tokens=2000,
                                        temperature=0.0, api_key=settings.anthropic_api_key,
                                        role="triage", settings=settings)  # Phase 13 (GOLIVE-01)
    return llmclient.chat_openai("gpt-4o-mini", max_tokens=2000, temperature=0.0,
                                 api_key=settings.openai_api_key, role="triage")  # Quick 005


def _extract_json(text: str) -> dict | None:
    if isinstance(text, list):  # anthropic content blocks
        text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ── test-thread sandboxing ────────────────────────────────────────────────
# Events from a configured test thread run the FULL pipeline, but every case
# they touch is quarantined: born sandbox=TRUE, matched only against other
# sandbox cases, and invisible to the open-case index, the reactive context
# and the expectation sweep. Enforced in SQL, never left to the model.
# contextassembler imports _is_test_thread lazily (triage never imports
# contextassembler, so there is no cycle).


def _test_threads() -> set[str]:
    raw = getattr(get_settings(), "test_threads", "") or ""
    return {t.strip() for t in str(raw).split(",") if t.strip()}


def _is_test_thread(ref) -> bool:
    return bool(ref) and str(ref) in _test_threads()


# ── prompt context ────────────────────────────────────────────────────────

async def _open_cases_index(conn) -> list[dict]:
    rows = await conn.fetch(
        """SELECT c.id, c.title, c.summary, c.aliases,
                  COALESCE(array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL), '{}') AS participants
           FROM cases c
           LEFT JOIN case_participants cp ON cp.case_id = c.id
           LEFT JOIN people p ON p.id = cp.person_id
           -- sandbox cases never surface in the prompt index: the model must
           -- not be able to attach a real event to a test case (SQL guards
           -- the reverse too, but no index entry means no temptation).
           -- Quick 006: cases opened from data findings (watch alerts, insights,
           -- chat) have their own lifecycle and stay out too, so the triage input
           -- is unchanged whatever findings exist; so does the shared exploration
           -- case, now an internal log of info insights hidden from the owner.
           WHERE c.status = 'open' AND NOT c.sandbox
             AND (c.origin IS NULL OR c.origin NOT IN ('watch', 'insight', 'chat', 'exploration'))
           GROUP BY c.id
           ORDER BY c.last_activity_at DESC LIMIT $1""", OPEN_CASE_CAP)
    return [dict(r) for r in rows]


async def _open_expectations_index(conn) -> list[dict]:
    rows = await conn.fetch(
        # due_at is shown so the model can see which expectations still LACK a
        # deadline and route an answer into expectations_updated, not a duplicate.
        # 'violated' rows stay visible: they are still live obligations, and a
        # late answer ("actually it's due next week") must be able to reach them.
        # Sandbox cases' expectations are excluded like the case index: showing
        # them would contaminate verdicts on real events (and a burst of E2E
        # expectations could evict real ones from the LIMIT 60 window), while
        # showing REAL expectations to test events is what step 6's SQL guard
        # exists to backstop — no prompt entry means no temptation either way.
        # Quick 024: an expectation the assistant owes shows owner "assistant" (so
        # the model does not record it twice), and only while its case is open.
        """SELECT e.id, e.description, e.case_id, e.due_at, e.status,
                  COALESCE(p.name, CASE WHEN e.owed_by = 'assistant'
                                        THEN 'assistant' END) AS owner
           FROM expectations e
           JOIN cases c ON c.id = e.case_id
           LEFT JOIN people p ON p.id = e.owner_id
           WHERE e.status IN ('open', 'violated') AND NOT c.sandbox
             AND (e.owed_by IS DISTINCT FROM 'assistant' OR c.status = 'open')
           ORDER BY e.id DESC LIMIT 60""")
    return [dict(r) for r in rows]


async def _thread_context(conn, events: list[dict]) -> dict[str, list[str]]:
    """Recent same-thread lines per distinct thread_ref in the batch.

    Terse replies ("Yes", "Almost 18") are unjudgeable in isolation — the
    triage model needs the conversation they continue. ANY provenance is
    included on purpose: the assistant's own sends and skipped rows ARE the
    conversation ("How much is pending?" may be the assistant's own group
    question). The batch's own events are excluded (they're already in the
    payload). One query per distinct thread; a batch is ≤20 events and
    typically spans 1-3 threads."""
    batch_ids = {e["id"] for e in events}
    out: dict[str, list[str]] = {}
    for ref in {e.get("thread_ref") for e in events if e.get("thread_ref")}:
        try:
            rows = await conn.fetch(
                """SELECT e.id, e.content, e.provenance,
                          COALESCE(p.name, e.actor_raw) AS actor
                   FROM events e LEFT JOIN people p ON p.id = e.actor_id
                   WHERE e.thread_ref = $1 AND e.id != ALL($2::bigint[])
                   ORDER BY e.id DESC LIMIT $3""",
                ref, list(batch_ids), THREAD_CONTEXT_EVENTS)
        except Exception:
            log.exception("thread context fetch failed for %s", ref)
            continue
        lines = []
        for r in reversed(rows):        # oldest first, reading order
            actor = "assistant" if r["provenance"] == "self" else (r["actor"] or "unknown")
            lines.append(f"{actor}: {(r['content'] or '')[:THREAD_CONTEXT_LINE_CAP]}")
        if lines:
            out[ref] = lines
    return out


def _thread_label(e: dict) -> str | None:
    """Human-orienting label for where an event came from, without leaking
    raw JIDs as the only signal: group:<subject-or-jid> / dm:<actor> /
    email:<ref> / db."""
    ref = e.get("thread_ref")
    if not ref:
        return None
    if e.get("source") == "db":
        return "db"
    if e.get("source") == "email":
        return f"email:{ref}"
    if str(ref).endswith("@g.us"):
        return f"group:{e.get('chat_subject') or ref}"
    return f"dm:{e.get('actor_name') or e.get('actor_raw') or ref}"


# ── Quick 024: requests to the assistant ──────────────────────────────────
# A request someone makes TO the assistant becomes an expectation owed by the
# assistant (expectations.owed_by = 'assistant'), so the satisfaction sweep can meet
# it from the assistant's own reply and the case closes quietly as answered. Only an
# event the assistant is actually replying to may carry one (code, not prompt).

ASSISTANT_OWNER_NAMES = ("assistant", "the assistant")
ANSWERED_REASON = "answered by the assistant in the chat"
ANSWER_DISMISSED_REASON = "the assistant's request was dismissed"


def _event_meta(e: dict) -> dict:
    """The event's meta as a dict (asyncpg returns jsonb as a string)."""
    m = e.get("meta")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    return m if isinstance(m, dict) else {}


def _to_assistant(e: dict) -> bool:
    """Whether the assistant is replying to this event: the webhook records
    meta.to_assistant (a reply run was queued, the connector's `mentioned` gate).
    An event recorded before Quick 024 counts when it is a WhatsApp direct chat,
    which is always answered: a thread on a number, or "<lid>@lid" (Quick 017)."""
    meta = _event_meta(e)
    if "to_assistant" in meta:
        return bool(meta["to_assistant"])
    ref = str(e.get("thread_ref") or "")
    return e.get("source") == "whatsapp" and (
        ref.isdigit() or (ref.endswith("@lid") and ref[:-4].isdigit()))


def _owed_by_assistant(owner) -> bool:
    return isinstance(owner, str) and owner.strip().lower() in ASSISTANT_OWNER_NAMES


ANSWERED_HERE_DAYS = 3       # how far back "answered_here" looks
ANSWERED_HERE_CAP = 5        # titles per chat


async def _answered_here(conn, events: list[dict]) -> dict[str, list[str]]:
    """Requests the assistant already answered in each chat of the batch (cases closed
    as answered in the last few days, by title only), so a "did you send it?" about
    one of them is recognised even after the answer has scrolled out of
    recent_thread_context. Scoped to the thread's own sandbox flag. Fail-soft."""
    out: dict[str, list[str]] = {}
    for ref in {e.get("thread_ref") for e in events if e.get("thread_ref")}:
        try:
            rows = await conn.fetch(
                """SELECT title, closed_at FROM cases
                   WHERE origin_chat_jid = $1 AND status = 'closed' AND close_reason = $2
                     AND closed_at > now() - interval '1 day' * $3 AND sandbox = $4
                   ORDER BY closed_at DESC LIMIT $5""",
                ref, ANSWERED_REASON, ANSWERED_HERE_DAYS, _is_test_thread(ref),
                ANSWERED_HERE_CAP)
        except Exception:
            log.exception("answered-here lookup failed for a thread")
            continue
        titles = [f"{r['title']} (answered {r['closed_at']:%d %b %H:%M})"
                  for r in rows if r.get("title") and r.get("closed_at")]
        if titles:
            out[ref] = titles
    return out


def _fmt_event(e: dict, thread_ctx: dict[str, list[str]] | None = None) -> dict:
    # Deliberately NO group roster here: a batch spans many chats and rosters
    # would bloat every tick of the cheap triage model. Owner resolution works
    # from names spoken in the text; the reactive path gets the roster via
    # contextassembler. If ever needed, gate it to events whose thread_ref is
    # an active group with few known members.
    out = {
        "event_id": e["id"],
        "source": e["source"],
        "actor": e.get("actor_name") or e.get("actor_raw") or "unknown",
        "occurred_at": e["occurred_at"].isoformat() if e.get("occurred_at") else None,
        "content": (e["content"] or "")[:EVENT_CONTENT_CAP],
    }
    if e.get("occurred_at"):  # Quick 049: the same instant on the business calendar
        out["occurred_business"] = bizclock.local_now(e["occurred_at"]).strftime("%Y-%m-%d %H:%M %Z")
    label = _thread_label(e)
    if label:
        out["thread"] = label
    if _to_assistant(e):
        out["to_assistant"] = True  # Quick 024: the assistant is replying to it
    if e.get("answered_here"):
        out["answered_here"] = e["answered_here"]   # Quick 024 (_answered_here)
    ctx = (thread_ctx or {}).get(e.get("thread_ref") or "")
    if ctx:
        out["recent_thread_context"] = ctx
    return out


# ── verdict application ───────────────────────────────────────────────────

def _parse_due(raw) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


# How far in the past a model-inferred due date may be before it's discarded.
# Generous on purpose: "by 5pm" said at 5:30 is a real deadline worth keeping;
# "2025-08-14" emitted in 2026 is not.
DUE_PAST_GRACE = timedelta(hours=12)


def _sane_due(raw) -> datetime | None:
    """A due date freshly inferred from conversation must be in the future.
    The triage model has emitted last YEAR's date for "before this weekend"
    (claude-haiku-4-5, year slip) — a born-violated expectation then fires an
    'overdue' escalation minutes after the case opens. Guarded in code, not
    just the prompt."""
    due = _parse_due(raw)
    if due is None:
        return None
    if due < datetime.now(timezone.utc) - DUE_PAST_GRACE:
        log.warning("discarding past due date %s from triage verdict", raw)
        return None
    return due


async def _apply_verdict(conn, event: dict, v: dict,
                         owner_pid: int | None = None) -> int | None:
    """Apply one event's verdict inside the batch transaction. Returns the case
    id the event resolved to (None if irrelevant / no case) so the batch can
    route any group_question AFTER this transaction commits. `owner_pid` is
    the resolved business owner (people id), used to gate case_facts — only
    the owner's word (or a system source) may set one."""
    from . import people

    event_id = event["id"]
    if not v.get("relevant"):
        return None

    # A DATABASE CHANGE (source='db') is a signal, not a human thread. It may ATTACH
    # to an existing case and SATISFY expectations, but must NEVER open a new case or
    # create obligations — otherwise every new row spawns a junk "New Machines: {...}"
    # case. Deterministic guard, independent of the model's verdict.
    is_db = event.get("source") == "db"

    # Test-thread quarantine: an event from a test thread may only resolve to
    # sandbox cases, and vice versa — SQL-enforced on both the resolution
    # lookups and the INSERTs, so the model cannot cross the boundary.
    is_test = _is_test_thread(event.get("thread_ref"))

    # 1. Resolve the case (db events: existing only; others: open if needed).
    case_sel = v.get("case", "none")
    case_id: int | None = None
    if isinstance(case_sel, int):
        case_id = await conn.fetchval(
            "SELECT id FROM cases WHERE id = $1 AND status = 'open' AND sandbox = $2",
            case_sel, is_test)
    elif isinstance(case_sel, str) and case_sel.isdigit():
        case_id = await conn.fetchval(
            "SELECT id FROM cases WHERE id = $1 AND status = 'open' AND sandbox = $2",
            int(case_sel), is_test)
    elif case_sel == "new" and not is_db:
        title = (v.get("new_case_title") or event["content"][:80] or "Untitled case").strip()
        case_id = await conn.fetchval(
            # origin_chat_jid: where this case came from, so a follow-up can be
            # posted back to the group the commitment was made in rather than
            # only DM'd to the person who owes it. For a DM the thread_ref is a
            # normalized phone, which _origin_chat() ignores.
            """INSERT INTO cases (title, opened_reason, origin_chat_jid, sandbox)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            title[:200], (v.get("reason") or "")[:500], event.get("thread_ref"),
            is_test)
    if case_id is None and not is_db:
        # relevant non-db event but no usable case → open one so nothing is lost
        title = (v.get("new_case_title") or event["content"][:80] or "Untitled case").strip()
        case_id = await conn.fetchval(
            """INSERT INTO cases (title, opened_reason, origin_chat_jid, sandbox)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            title[:200], "auto-opened (triage relevant, no case given)",
            event.get("thread_ref"), is_test)

    if case_id is not None:
        # 2. Link the event to the case (idempotent).
        await conn.execute(
            """INSERT INTO case_events (case_id, event_id, note) VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""", case_id, event_id, (v.get("reason") or "")[:500])
        await conn.execute(
            """UPDATE cases SET events_since_summary = events_since_summary + 1,
                  last_activity_at = now() WHERE id = $1""", case_id)

        # 3. Attach the actor + named participants (only those already registered).
        if event.get("actor_id"):
            await conn.execute(
                """INSERT INTO case_participants (case_id, person_id, role) VALUES ($1, $2, 'requester')
                   ON CONFLICT DO NOTHING""", case_id, event["actor_id"])
        for pname in (v.get("participants") or []):
            pid = await people.resolve(conn, alias=pname)
            if pid:
                await conn.execute(
                    """INSERT INTO case_participants (case_id, person_id, role) VALUES ($1, $2, 'watcher')
                       ON CONFLICT DO NOTHING""", case_id, pid)

        # 4. Create expectations — never from a db row (a row is not an obligation).
        if not is_db:
            assistant_recorded = False
            for exp in (v.get("expectations_created") or []):
                if not isinstance(exp, dict):
                    continue
                desc = (exp.get("description") or "").strip()
                if not desc:
                    continue
                if _owed_by_assistant(exp.get("owner")):
                    # Quick 024: the assistant owes this request. Only for an event
                    # it is replying to, at most one per event, never a due date
                    # (nothing chases the assistant) and never a person owner. The
                    # satisfaction sweep meets it from the assistant's reply in the
                    # same chat, after this event (source_event_id).
                    if assistant_recorded or not _to_assistant(event):
                        log.info("triage: assistant commitment for event %s dropped "
                                 "(%s)", event_id, "one per event" if assistant_recorded
                                 else "the assistant is not replying to it")
                        continue
                    assistant_recorded = True
                    await conn.execute(
                        """INSERT INTO expectations (case_id, description, owner_id, due_at,
                                                     satisfy_hint, owed_by, source_event_id)
                           VALUES ($1, $2, NULL, NULL, $3, 'assistant', $4)""",
                        case_id, desc[:500], (exp.get("satisfy_hint") or "")[:500], event_id)
                    continue
                owner_id = await people.resolve(conn, alias=exp.get("owner")) if exp.get("owner") else None
                await conn.execute(
                    """INSERT INTO expectations (case_id, description, owner_id, due_at, satisfy_hint)
                       VALUES ($1, $2, $3, $4, $5)""",
                    case_id, desc[:500], owner_id, _sane_due(exp.get("due_at")),
                    (exp.get("satisfy_hint") or "")[:500])

        # 5. Set/adjust deadline and/or owner on EXISTING open expectations
        # (e.g. a group answered "Vinu, before this weekend"). Code-enforced
        # guard, independent of the model: the id must be an OPEN expectation
        # on THIS event's case — the model cannot reach into other cases'
        # rows. Skipped entirely when the event resolved to no case. Owners
        # resolve through the people registry only (exact name/alias) — an
        # unrecognized name changes nothing rather than guessing. Quick 024: an
        # expectation the assistant owes takes no due date and no person owner.
        for upd in (v.get("expectations_updated") or []):
            if not isinstance(upd, dict):
                continue
            due = _sane_due(upd.get("due_at"))
            owner_id = (await people.resolve(conn, alias=upd.get("owner"))
                        if upd.get("owner") else None)
            if not isinstance(upd.get("id"), int) or (due is None and owner_id is None):
                continue
            # A fresh FUTURE deadline un-violates: the reminder machinery
            # re-arms against the corrected date instead of nagging on.
            await conn.execute(
                """UPDATE expectations
                      SET due_at = COALESCE($2, due_at),
                          owner_id = COALESCE($3, owner_id),
                          status = CASE WHEN $2 IS NOT NULL THEN 'open'
                                        ELSE status END
                    WHERE id = $1 AND status IN ('open', 'violated')
                      AND case_id = $4
                      AND owed_by IS DISTINCT FROM 'assistant'""",
                upd["id"], due, owner_id, case_id)

        # 5b. Durable case fact ("≈₹18 lakh reported pending"): stored as a
        # note event via the shared helper so the timeline, rolling summary
        # and reactive context all see it. Allowed for db events too — a row
        # change may be a fact about an existing case even though it can
        # never open one.
        note = v.get("case_note")
        from . import cases_util
        if isinstance(note, str) and note.strip():
            await cases_util.add_case_note(conn, case_id, note, kind="triage")

        # 5c. Structured facts + aliases. Like the note, NOT gated on is_db —
        # a db row may correct a fact ("payment_status" flipping to paid) even
        # though it can never open a case. record_case_fact supersedes the
        # field's previous value; add_alias dedupes/caps in its WHERE clause.
        #
        # OWNER VALIDATION — the owner's standing rule, enforced in code, not
        # the prompt: "whenever anyone quotes a commercial number or any item
        # with business value in the group — validate it by me in the group
        # before adding it as a fact." Chat-sourced facts are accepted only
        # from the resolved owner; everyone else's figures stay claims (the
        # case_note above + a fact_confirm group question). When owner_number
        # is unset (owner_pid None), chat facts are ALWAYS skipped — fail
        # toward safety. System observations (db / docintel / system) are not
        # chat claims and pass.
        facts_ok = ((owner_pid is not None and event.get("actor_id") == owner_pid)
                    or event.get("source") in ("db", "docintel", "system"))
        if facts_ok:
            for f in (v.get("case_facts") or [])[:3]:
                if isinstance(f, dict):
                    await cases_util.record_case_fact(
                        conn, case_id, f.get("field"), f.get("value"),
                        source_event_id=event_id)
        # Aliases steer future event→case matching, so an UNREGISTERED sender
        # must not be able to graft "INV-2041" onto a case; any registered
        # actor (or system source) may.
        aliases_ok = (bool(event.get("actor_id"))
                      or event.get("source") in ("db", "docintel", "system"))
        if aliases_ok:
            for a in (v.get("aliases") or [])[:3]:
                if isinstance(a, str):
                    await cases_util.add_alias(conn, case_id, a)

    # 6. Mark satisfied expectations (the main value of a db event; always applies).
    # Sandbox-scoped in SQL like the case resolution above: a test-thread event
    # may only satisfy expectations hanging on SANDBOX cases, and a real event
    # only real ones — otherwise a fictional "payment received" could silence a
    # REAL case's reminder, and the auto-complete sweep would then close the
    # real case and notify the owner. Scoped via the cases subquery rather than
    # the event's own resolved case_id because a db event may legitimately
    # satisfy an expectation on a DIFFERENT (real) case.
    # Quick 024: never an expectation the assistant owes — only the assistant's own
    # reply meets one (the satisfaction sweep). Otherwise "did you send it?" from the
    # requester could mark it met and close the case with a false "Done".
    for exp_id in (v.get("expectations_satisfied") or []):
        if not isinstance(exp_id, int):
            continue
        await conn.execute(
            """UPDATE expectations SET status = 'satisfied', satisfied_by = $2, resolved_at = now()
               WHERE id = $1 AND status = 'open'
                 AND owed_by IS DISTINCT FROM 'assistant'
                 AND case_id IN (SELECT id FROM cases WHERE sandbox = $3)""",
            exp_id, event_id, is_test)

    return case_id


# ── batch ──────────────────────────────────────────────────────────────────

def _compose_system(policy_block: str) -> str:
    """TRIAGE_SYSTEM plus (optionally) the owner's triage policies.

    Strictly ADDITIVE: the full built-in rulebook is always present and
    always first — a policy extends judgment ("owner demanding a discussion
    opens a case"), it can never replace or precede the core rules. An empty
    block returns TRIAGE_SYSTEM byte-identical."""
    if not policy_block:
        return TRIAGE_SYSTEM
    return TRIAGE_SYSTEM + "\n\n" + policy_block


def _system_message(system: str, model):
    """Quick 035: the batch's system prompt with one explicit cache breakpoint.

    `system` is _compose_system's output: TRIAGE_SYSTEM (about 2.3k tokens) plus the
    owner's policies (ORDER BY name), stable between policy edits. Everything that
    changes per tick (now, open cases, expectations, events) is the user message
    after it, so a batch within 5 minutes of the last reads the prefix at 0.1x.
    No conversation cache: each batch is one call whose payload is never resent.
    Off (prompt_cache_loops) or on OpenAI: the plain ("system", text) as before."""
    if type(model).__name__ != "ChatAnthropic" or not llmclient.loop_cache_on(get_settings()):
        return ("system", system)
    from .agents.graph import _cached_prompt
    return _cached_prompt(system, model)


async def _triage_batch() -> int:
    """Process one batch of pending external events. Returns count handled.
    Quick 008: a batch that found events is recorded on Runs (live while it runs)."""
    async with jobruns.track("triage", "Triage", trigger="new messages") as job:
        return await _triage_events(job)


async def _triage_events(job) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _requeue_given_up(conn)  # Quick 039: once the model answers again
        events = await conn.fetch(
            # thread_ref is carried so a case can record which chat it came out
            # of — without it, cases.origin_chat_jid silently stays NULL and
            # follow-ups can only ever be DMs. chat subject labels group events
            # in the prompt ("group:Northwind Invoicing" beats a bare JID).
            """SELECT e.id, e.source, e.content, e.actor_id, e.actor_raw, e.occurred_at,
                      e.thread_ref, e.meta, p.name AS actor_name, c.subject AS chat_subject
               FROM events e LEFT JOIN people p ON p.id = e.actor_id
               LEFT JOIN chats c ON c.jid = e.thread_ref
               WHERE e.triage_status = 'pending' AND e.provenance = 'external'
               ORDER BY e.id LIMIT $1""", BATCH_SIZE)
        if not events:
            return 0
        if _gate.wait() > 0:  # Quick 039: backing off after a failed call, or paused
            return 0
        events = [dict(e) for e in events]
        await job.go_live(f"{len(events)} new message(s)")  # Quick 008
        open_cases = await _open_cases_index(conn)
        open_exps = await _open_expectations_index(conn)
        thread_ctx = await _thread_context(conn, events)
        # Quick 024: what the assistant already answered in each chat, by title
        answered = await _answered_here(conn, events)
        for e in events:
            if answered.get(e.get("thread_ref")):
                e["answered_here"] = answered[e["thread_ref"]]
        # Resolve the OWNER once per batch: their word is what turns a
        # business-value claim into a case fact (owner-validation rule in
        # _apply_verdict). Fail-soft: unresolved owner just means chat facts
        # are gated off for this batch.
        owner_pid, owner_name = None, None
        owner_number = str(getattr(get_settings(), "owner_number", "") or "").strip()
        if owner_number:
            try:
                from . import people
                owner_pid = await people.resolve(conn, phone=owner_number)
                if owner_pid:
                    owner_name = await conn.fetchval(
                        "SELECT name FROM people WHERE id = $1", owner_pid)
            except Exception:
                log.exception("owner resolution failed; treating owner as unknown")
                owner_pid, owner_name = None, None

    payload = {
        # Stated explicitly even though every event carries occurred_at: the
        # cheap triage model has resolved "before this weekend" to LAST year's
        # date without an unmissable anchor for what year it is.
        # Quick 049: the BUSINESS clock (operator timezone) first, UTC after it.
        "now": bizclock.business_now_text(),
        "open_cases": open_cases,
        "open_expectations": open_exps,
        "events": [_fmt_event(e, thread_ctx) for e in events],
    }
    owner_company = (get_settings().company_name or "").strip()
    if owner_company:
        payload["owner_company"] = owner_company
    if owner_name:
        payload["owner_name"] = owner_name

    # Owner-authored triage policies (skills with scope='triage'). The block
    # is "" in the common case; skillservice enforces custom-only + caps, and
    # fails soft — triage must never stall on the skills table.
    from . import skillservice
    system = _compose_system(await skillservice.triage_policy_block())

    verdicts_by_id: dict[int, dict] = {}
    try:
        from .agents.graph import _callbacks
        model = _model()
        out = await model.ainvoke(
            [_system_message(system, model), ("user", json.dumps(payload, default=str))],
            config=_callbacks())
        # Phase 12 (CTX-09e): a refusal fails the batch's events instead of dropping them as done
        if llmclient.is_refusal(out):
            log.warning("triage model declined (refusal); leaving %d event(s) pending", len(events))
            # review WR-04 + Quick 039: pending, retried on the backoff, given up at the cap
            return await _model_failed(job, events, TriageRefusal("the triage model declined (refusal)"))
        parsed = _extract_json(out.content)
        for v in (parsed or {}).get("verdicts", []):
            if isinstance(v.get("event_id"), int):
                verdicts_by_id[v["event_id"]] = v
    except Exception as e:
        # Quick 039: the events stay 'pending' and the gate backs off (a spend limit
        # pauses every triage call); at triage_max_attempts the batch is given up
        # ('failed' plus a marker, requeued once after the model answers again).
        return await _model_failed(job, events, e)
    _gate.success(("batch", events[0]["id"]))  # Quick 039

    # Apply each verdict in its OWN transaction — a failure in one event must roll
    # back only that event, not poison the whole batch (asyncpg aborts a tx on the
    # first error). The status UPDATE runs outside the per-event tx so it always
    # records, even for a failed verdict.
    ev_by_id = {e["id"]: e for e in events}
    pending_questions: list[tuple[int, str, str]] = []
    n_failed = 0  # Quick 008
    pool = await get_pool()
    async with pool.acquire() as conn:
        for eid, event in ev_by_id.items():
            v = verdicts_by_id.get(eid)
            try:
                case_id = None
                async with conn.transaction():
                    if v:
                        case_id = await _apply_verdict(conn, event, v,
                                                       owner_pid=owner_pid)
                status = "done"
                # Group questions are only COLLECTED here — never sent inside
                # the transaction, or a rollback would leave a message in the
                # group with no case state behind it.
                q = (v or {}).get("group_question")
                if case_id is not None and isinstance(q, str) and q.strip():
                    # question_kind picks the cooldown: "fact_confirm" (owner
                    # validating a claimed figure) may repeat sooner than an
                    # ordinary blocked-tracking question. Unknown → "question".
                    qk = (v or {}).get("question_kind")
                    qk = qk if qk == "fact_confirm" else "question"
                    pending_questions.append((case_id, q, qk))
            except Exception:
                log.exception("failed to apply verdict for event %s", eid)
                status = "failed"
                n_failed += 1  # Quick 008
            await conn.execute(
                "UPDATE events SET triage_status = $2 WHERE id = $1", eid, status)

    # Now that every verdict transaction is committed, post the questions.
    # _ask_group_question is fail-soft and enforces all guards in code.
    for cid, q, qk in pending_questions:
        await _ask_group_question(cid, q, kind=qk)

    await _refresh_summaries()
    await _complete_finished_cases()
    relevant = sum(1 for v in verdicts_by_id.values() if v.get("relevant"))
    log.info("triage handled %d event(s), %d relevant", len(events), relevant)
    job.ok(f"{len(events)} message(s) triaged, {relevant} relevant"  # Quick 008
           + (f", {n_failed} could not be applied" if n_failed else ""),
           {"events": len(events), "relevant": relevant, "failed": n_failed,
            "questions": len(pending_questions)}, routine=True)
    return len(events)


# ── group questions ────────────────────────────────────────────────────────


async def _ask_group_question(case_id: int, question: str,
                              kind: str = "question") -> None:
    """Post ONE short clarifying question to the case's origin group chat.

    The model only proposes the text; every guard lives here in code: the mode
    switch (off / proposed / autonomous — unknown values fail toward the
    approval gate), group-JID-origin only, chat must still be active, a per-case
    cooldown so a chatty model can't spam, and a hard length cap. Fail-soft:
    a failed question never touches case state. The send records
    provenance='self' (whatsapp.py does this for every outbound), so the
    question can never re-enter triage as an event.

    `kind` is "question" (a missing detail blocks tracking — 12h cooldown) or
    "fact_confirm" (asking the OWNER to validate a claimed business-value
    figure — 1h cooldown, because a claim awaiting confirmation goes stale in
    hours, not days). Unknown values coerce to "question". The kind is
    recorded in the action's payload so the two cooldowns stay auditable."""
    try:
        if kind != "fact_confirm":
            kind = "question"        # unknown value → ordinary question
        mode = str(getattr(get_settings(), "group_question_mode", "autonomous") or "").strip().lower()
        if mode == "off":
            return
        if mode != "autonomous":
            mode = "proposed"        # unknown value → safest interpretation
        q = (question or "").strip()[:GROUP_QUESTION_MAX_CHARS]
        if not q:
            return
        pool = await get_pool()
        jid = await pool.fetchval(
            "SELECT origin_chat_jid FROM cases WHERE id = $1", case_id)
        if not jid or not str(jid).endswith("@g.us"):
            return                   # DM-origin cases never get group questions
        from . import chats
        if not await chats.is_active(jid):
            return                   # group was deactivated → stay silent
        cooldown_hours = (FACT_CONFIRM_COOLDOWN_HOURS if kind == "fact_confirm"
                          else GROUP_QUESTION_COOLDOWN_HOURS)
        recent = await pool.fetchval(
            """SELECT 1 FROM actions
               WHERE case_id = $1 AND kind = 'group_question'
                 AND status IN ('proposed','approved','executed')
                 AND created_at > now() - interval '1 hour' * $2""",
            case_id, cooldown_hours)
        if recent:
            return                   # one question per case per cooldown window
        if mode == "autonomous":
            from . import whatsapp
            # A raising send (connector down, timeout) must still leave the
            # audit row — everything with a side effect writes an actions row,
            # and a timeout may have sent anyway.
            try:
                ok = await whatsapp.send_message(jid, q)
            except Exception:
                log.warning("group question send to %s raised", jid, exc_info=True)
                ok = False
            await pool.execute(
                """INSERT INTO actions (case_id, kind, payload, tier, status, executed_at)
                   VALUES ($1, 'group_question', $2::jsonb, 'autonomous_message',
                           CASE WHEN $3 THEN 'executed' ELSE 'failed' END,
                           CASE WHEN $3 THEN now() END)""",
                case_id, json.dumps({"chat": jid, "body": q, "kind": kind}), ok)
        else:
            # proposed mode: park it for a human — actionsvc._execute sends on
            # approval (kind='group_question').
            await pool.execute(
                """INSERT INTO actions (case_id, kind, payload, tier, status)
                   VALUES ($1, 'group_question', $2::jsonb, 'draft', 'proposed')""",
                case_id, json.dumps({"chat": jid, "body": q, "kind": kind}))
    except Exception:
        log.exception("group question failed for case %s", case_id)


# ── rolling summaries + auto-close ─────────────────────────────────────────

SUMMARY_PROMPT = """Summarize this case for an operator in 2-3 sentences: what was \
requested, who's involved, and what's still outstanding. Plain text, no preamble."""


async def _refresh_summaries() -> None:
    """Refresh the rolling summary for any case with enough new events."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id FROM cases WHERE status='open' AND events_since_summary >= $1",
        SUMMARY_REFRESH_EVENTS)
    for r in rows:
        try:
            await _summarize_case(r["id"])
        except Exception:
            log.exception("summary refresh failed for case %s", r["id"])


async def _summarize_case(case_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        evs = await conn.fetch(
            """SELECT e.content, e.source, COALESCE(p.name, e.actor_raw) AS actor
               FROM case_events ce JOIN events e ON e.id = ce.event_id
               LEFT JOIN people p ON p.id = e.actor_id
               WHERE ce.case_id = $1 ORDER BY e.occurred_at LIMIT 40""", case_id)
    transcript = "\n".join(f"[{e['source']}] {e['actor']}: {(e['content'] or '')[:300]}" for e in evs)
    out = await _model().ainvoke([("system", SUMMARY_PROMPT), ("user", transcript[:8000])])
    summary = out.content
    if isinstance(summary, list):
        summary = " ".join(b.get("text", "") for b in summary if isinstance(b, dict))
    await pool.execute(
        "UPDATE cases SET summary = $2, events_since_summary = 0 WHERE id = $1",
        case_id, str(summary).strip()[:2000])


async def _close_case(pool, case_id: int, close_reason: str | None = None, *,
                      kind: str = "completed", statuses: tuple = ("open",)) -> bool:
    """THE close path: flip the row (single-winner), notify the requester, distill
    into memory, and — for a completion — consider chaining a follow-on case.
    Every close, autonomous or manual, goes through here so nothing gets closed
    without those side effects.

    `kind` controls the notification wording: 'completed' says the work is done;
    'stale' says the case is being archived for inactivity. A stale close must
    NEVER claim completion — the case is closing precisely because nothing
    happened. Quick 024: 'answered' is a request the assistant answered in the
    chat it was asked in: no notice (the requester already has the answer there),
    no distill (the distiller never sees the answer, only "X asked for Y"; the
    case is stamped as holding nothing durable) and no chain.
    `statuses` is which current statuses may transition to closed
    (manual endpoints pass e.g. ('pending_ack',)); COALESCE keeps an
    already-recorded close_reason over the argument."""
    closed = await pool.fetchval(
        """UPDATE cases SET status = 'closed', closed_at = now(),
              close_reason = COALESCE(close_reason, $2)
           WHERE id = $1 AND status = ANY($3::text[]) RETURNING id""",
        case_id, close_reason, list(statuses))
    if not closed:
        return False
    # Sandbox (test-thread) cases close like any other, but their side effects
    # stay inside the quarantine: no real WhatsApp "✅ Done" to the owner about
    # a fictional case, and no distillation of fictional content into the
    # shared knowledge graph that real conversations retrieve. Fail-soft: a
    # failed flag lookup treats the case as sandbox — better a missed notify
    # than fiction reported authoritatively. Chaining still runs below; the
    # follow-on case inherits the sandbox flag.
    try:
        sandbox = bool(await pool.fetchval(
            "SELECT sandbox FROM cases WHERE id = $1", case_id))
    except Exception:
        log.exception("sandbox flag lookup failed for case %s; suppressing "
                      "close side effects", case_id)
        sandbox = True
    # Quick 006: a case opened from a data finding (watch alert, insight, chat) never
    # chains: closing an alert implies no follow-on human work. Fail-soft: a failed
    # lookup treats it as an ordinary case.
    try:
        from .findingcases import FINDING_ORIGINS
        finding = (await pool.fetchval(
            "SELECT origin FROM cases WHERE id = $1", case_id)) in FINDING_ORIGINS
    except Exception:
        finding = False
    if sandbox:
        log.info("sandbox case %s closed; notify/distill suppressed", case_id)
    elif kind == "answered":
        log.info("case %s closed: %s; no close notice, nothing to distill",
                 case_id, close_reason or ANSWERED_REASON)
        try:
            # distilled_at with an empty distillation = "nothing durable", so the
            # graph backfill (memory_pipeline._PENDING_CASES) never distills it later.
            await pool.execute(
                """UPDATE cases SET distilled_at = now(), distillation = ''
                    WHERE id = $1 AND distilled_at IS NULL""", case_id)
        except Exception:
            log.exception("distill stamp failed for answered case %s", case_id)
    else:
        try:
            from .actionsvc import notify_case_closed
            await notify_case_closed(case_id, kind=kind)  # completion vs stale wording
        except Exception:
            log.exception("close notify failed for case %s", case_id)
        try:
            from .memory_pipeline import distill_case
            await distill_case(case_id)
        except Exception:
            log.exception("distill failed for case %s", case_id)
    try:
        if kind == "completed" and not finding:
            # Only a COMPLETION can imply follow-on work; a stale archive can't.
            await _maybe_chain_case(pool, case_id)
    except Exception:
        log.exception("chain check failed for case %s", case_id)
    return True


# ── case chaining ──────────────────────────────────────────────────────────
# A completed case sometimes IMPLIES the next one: "gather invoice details" done
# means "pay the invoice by its due date" begins. Chaining opens that follow-on
# case (linked via cases.parent_case_id) so the ordinary expectation engine
# drives it. Loop safety, in code: completed closes only, depth capped, the
# chain step emits no events and sends no messages, and a kill switch.

CHAIN_SYSTEM = """A case in an ambient business assistant just COMPLETED. Decide whether \
its completion creates exactly ONE obvious follow-on obligation — e.g. invoice details \
gathered → the invoice must now be PAID by its due date. Most completions imply NO \
follow-on: routine Q&A, FYI threads, and anything without a clear next step for a HUMAN \
→ return null. Be conservative — no invented work, and never a follow-on for something \
the assistant itself would do.

Rules:
- due_at ONLY if a concrete date/deadline appears in the case content below. NEVER \
invent or infer one from nothing.
- owner is a person name/alias appearing in the case, or null if nobody is clearly \
on the hook.

Return ONLY JSON: {"follow_on": null | {"title": <short string>,
  "expectation": {"description": <str>, "owner": <str|null>,
                  "due_at": <ISO8601|null>, "satisfy_hint": <str>}}}"""


async def _chain_depth(pool, case_id: int) -> int:
    """Number of ancestors above this case via parent_case_id. Iterative walk,
    bounded at MAX_CHAIN_DEPTH steps so a (theoretically impossible) cycle can
    never spin — the caller only needs to know 'at/over the cap or not'."""
    depth, current = 0, case_id
    while depth < MAX_CHAIN_DEPTH:
        parent = await pool.fetchval(
            "SELECT parent_case_id FROM cases WHERE id = $1", current)
        if parent is None:
            break
        depth += 1
        current = parent
    return depth


async def _maybe_chain_case(pool, case_id: int) -> None:
    """After a COMPLETED close, ask the model whether the finished case implies
    one follow-on obligation, and open that case if so. Whole body fail-soft:
    the parent is already closed and NOTHING here may undo or block that — any
    failure just means no chained case this time. No outbound sends from this
    step; the follow-on case gets its own ordinary lifecycle."""
    try:
        if not getattr(get_settings(), "case_chaining_enabled", True):
            return
        if await _chain_depth(pool, case_id) >= MAX_CHAIN_DEPTH:
            return

        async with pool.acquire() as conn:
            case = await conn.fetchrow(
                """SELECT id, title, summary, close_reason, origin_chat_jid, sandbox
                   FROM cases WHERE id = $1""", case_id)
            if not case:
                return
            exps = await conn.fetch(
                """SELECT e.description, e.status, e.due_at, p.name AS owner
                   FROM expectations e LEFT JOIN people p ON p.id = e.owner_id
                   WHERE e.case_id = $1 ORDER BY e.id""", case_id)
            evs = await conn.fetch(
                """SELECT e.content, e.source, COALESCE(p.name, e.actor_raw) AS actor
                   FROM case_events ce JOIN events e ON e.id = ce.event_id
                   LEFT JOIN people p ON p.id = e.actor_id
                   WHERE ce.case_id = $1 ORDER BY e.occurred_at DESC LIMIT 10""",
                case_id)

        payload = {
            "case": {"title": case["title"], "summary": case["summary"],
                     "close_reason": case["close_reason"]},
            "expectations": [dict(e) for e in exps],
            "events": [f"[{e['source']}] {e['actor']}: {(e['content'] or '')[:300]}"
                       for e in reversed(list(evs))],
        }
        out = await _model().ainvoke(
            [("system", CHAIN_SYSTEM), ("user", json.dumps(payload, default=str))])
        follow = (_extract_json(out.content) or {}).get("follow_on")
        if not isinstance(follow, dict):
            return
        title = str(follow.get("title") or "").strip()
        if not title:
            return

        from . import people
        async with pool.acquire() as conn:
            async with conn.transaction():
                new_id = await conn.fetchval(
                    # sandbox is inherited: a completed TEST case must chain
                    # into another sandbox case, never leak a real one.
                    # .get(): tolerant of scripted rows in tests that predate
                    # the column (asyncpg Record and dict both support it).
                    """INSERT INTO cases (title, opened_reason, origin_chat_jid, parent_case_id, sandbox)
                       VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                    title[:200], f"chained from case {case_id}",
                    case["origin_chat_jid"], case_id, bool(case.get("sandbox")))
                # The follow-on inherits who asked, so its own close-notify has
                # a recipient and a group question has an audience.
                await conn.execute(
                    """INSERT INTO case_participants (case_id, person_id, role)
                       SELECT $1, person_id, 'requester' FROM case_participants
                       WHERE case_id = $2 AND role = 'requester'
                       ON CONFLICT DO NOTHING""", new_id, case_id)
                exp = follow.get("expectation")
                if isinstance(exp, dict) and str(exp.get("description") or "").strip():
                    owner_id = (await people.resolve(conn, alias=str(exp.get("owner")))
                                if exp.get("owner") else None)
                    await conn.execute(
                        """INSERT INTO expectations (case_id, description, owner_id, due_at, satisfy_hint)
                           VALUES ($1, $2, $3, $4, $5)""",
                        new_id, str(exp["description"]).strip()[:500], owner_id,
                        _parse_due(exp.get("due_at")),
                        str(exp.get("satisfy_hint") or "")[:500])
        log.info("chained case %s from completed case %s", new_id, case_id)
    except Exception:
        log.exception("case chaining failed for case %s (parent stays closed)", case_id)


async def _complete_finished_cases() -> None:
    """AUTONOMOUS completion: the instant a case's every expectation is resolved
    (and it had at least one), close it, notify the requester, and distill it — no
    manual acknowledgment. This is what makes "task done → owner gets told" happen on
    its own. (Deliberately overrides the original user-ack-to-close decision.)

    Quick 024: a case whose every expectation is owed by the assistant is a request
    the assistant handled in the chat it was asked in. It closes quietly (kind
    'answered': no "Done" notice, the requester already has the reply) with the
    reason "answered by the assistant in the chat", or "the assistant's request was
    dismissed" when none was met. A case with any person's expectation is a real
    completion and keeps its notice."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT c.id,
                  NOT EXISTS (SELECT 1 FROM expectations e
                              WHERE e.case_id = c.id
                                AND e.owed_by IS DISTINCT FROM 'assistant') AS all_assistant,
                  EXISTS (SELECT 1 FROM expectations e
                          WHERE e.case_id = c.id AND e.status = 'satisfied') AS any_met
           FROM cases c WHERE status = 'open'
             AND EXISTS (SELECT 1 FROM expectations e WHERE e.case_id = c.id)
             AND NOT EXISTS (SELECT 1 FROM expectations e
                             WHERE e.case_id = c.id AND e.status IN ('open','violated'))""")
    for r in rows:
        if r.get("all_assistant"):
            await _close_case(pool, r["id"],
                              ANSWERED_REASON if r.get("any_met") else ANSWER_DISMISSED_REASON,
                              kind="answered")
        else:
            await _close_case(pool, r["id"], "auto: all expectations satisfied")
    if rows:
        log.info("auto-completed %d case(s)", len(rows))


async def _close_stale_cases() -> None:
    """Stale sweep: close open cases with no linked activity for stale_case_days.

    A case with a still-OPEN expectation is live (someone still owes something and
    the expectation engine is chasing it) — left alone regardless of age. Violated/
    satisfied expectations don't keep a case alive. Fail-soft per case: one bad
    close never stops the sweep. Goes through _close_case so the requester is
    notified and the case is distilled, same as any other autonomous close."""
    try:
        days = int(getattr(get_settings(), "stale_case_days", 21))
    except (TypeError, ValueError):
        days = 21
    if days <= 0:          # 0 / negative disables the sweep
        return
    pool = await get_pool()
    rows = await pool.fetch(
        # Quick 006: a watch-alert case ends when its watch recovers, stops or the
        # owner closes it, never for inactivity (a standing alert adds no events);
        # the shared exploration case is an internal log, not work to archive.
        # Quick 024: an open expectation the ASSISTANT owes does not keep a case
        # alive: nothing chases it, so an unanswered request is archived like
        # before, with the same honest "no recent activity" wording.
        """SELECT id FROM cases c WHERE status = 'open'
             AND last_activity_at < now() - interval '1 day' * $1
             AND (c.origin IS NULL OR c.origin NOT IN ('watch', 'exploration'))
             AND NOT EXISTS (SELECT 1 FROM expectations e
                             WHERE e.case_id = c.id AND e.status = 'open'
                               AND e.owed_by IS DISTINCT FROM 'assistant')""", days)
    closed = 0
    for r in rows:
        cid = r["id"]
        try:
            if await _close_case(
                    pool, cid,
                    f"no activity for {days}+ days (auto-closed: stale)",
                    kind="stale"):
                closed += 1
        except Exception:
            log.exception("stale-case close failed for case %s", cid)
    if closed:
        log.info("auto-closed %d stale case(s)", closed)


# ── Quick 024: requests stuck open before this change ──────────────────────
# Before Quick 024 triage opened a case for a request to the assistant with no
# expectation ("the assistant owes the lookup, so no person expectation is
# created"), so only the stale sweep could ever close it, three weeks later and as
# "archived". At startup each such case gets the commitments it should have had;
# the satisfaction sweep then judges the assistant's replies with the model, like
# any new request, and closes the case quietly when they answered it, or leaves it
# open with the commitment shown. This never closes a case itself.
#
# Conservative by construction: only an open, non-sandbox triage case (origin NULL)
# with no expectations at all, whose linked messages are all WhatsApp, where every
# linked verdict says no human/person owes anything and none names a person owing,
# at least one names the assistant as owing it, and the latest such request has a
# reply from the assistant after it in the same chat. Anything else is left exactly
# as it is.

_NOTE_ASSISTANT = re.compile(r"\bassistant\b", re.I)
_NOTE_OWES = re.compile(
    r"\b(owes?|owed|owns?|answers?|answered|lookup|look up|requests?|requested|"
    r"questions?|asks?|asked|awaiting|waiting|reply|replies|replied)\b", re.I)
_NOTE_NO_HUMAN = re.compile(
    r"\b(?:no|not a|without (?:creating )?(?:any |a )?)\s?(?:human|person)\b"
    r"|\bno(?:body| one) (?:else )?owes\b", re.I)
# A named person owing something ("Ravi owes the certificate", "Vinu must send"):
# such a case is real tracked work, never a request the assistant answered.
_NOTE_PERSON_OWES = re.compile(
    r"\b(?!(?:The|No|Nobody|None|Assistant)\b)[A-Z][a-z]+ "
    r"(?:owes|must|needs to|has to|promised|will send|is to)\b")
_REPLY_ANCHOR = re.compile(r'^\[replying to [^\]\n]*\]\s*')
ADOPTED_HINT = "The assistant's reply in this chat gives what was asked."


def _says_assistant_owes(text) -> bool:
    t = str(text or "")
    return bool(_NOTE_ASSISTANT.search(t) and _NOTE_OWES.search(t))


def _request_description(content) -> str:
    text = " ".join(_REPLY_ANCHOR.sub("", str(content or "")).split())
    if len(text) > 160:
        text = text[:160].rstrip() + "…"
    return f"Answer: “{text}”" if text else "Answer the request"


def _requests_to_adopt(rows: list[dict]) -> list[dict]:
    """The requests of one stuck case to record as the assistant's commitments, or
    [] when the case does not qualify. `rows` are the case's linked EXTERNAL
    events, oldest first, each with the case's opened_reason, the event's source,
    thread_ref, content, the verdict note that linked it, and `replied` (a self
    WhatsApp delivery exists after it in the same thread)."""
    if not rows:
        return []
    requests = []
    for i, r in enumerate(rows):
        if r.get("source") != "whatsapp" or not r.get("thread_ref"):
            return []                       # a document, email or db row: not ours to judge
        # the verdict note; the case's opened_reason only stands in for a missing
        # note on the message that opened the case (they are the same text there)
        note = (r.get("note") or "").strip()
        if not note and i == 0:
            note = (r.get("opened_reason") or "").strip()
        # every verdict must have ruled out a person's part ("so no person
        # expectation is created", the old prompt's wording), and none may name a
        # person owing anything; otherwise this is real work, left alone
        if not _NOTE_NO_HUMAN.search(note) or _NOTE_PERSON_OWES.search(note):
            return []
        if _says_assistant_owes(note):
            requests.append(r)
    if not requests or not requests[-1].get("replied"):
        return []                           # no reply yet to the latest request: leave it
    return [{"event_id": r["event_id"], "description": _request_description(r.get("content")),
             "satisfy_hint": ADOPTED_HINT} for r in requests]


async def migrate_assistant_requests() -> int:
    """Startup clean-up (Quick 024): give each stuck request-to-the-assistant case
    its commitments, for the satisfaction sweep to judge. Idempotent (only cases
    with no expectations at all), never raises. Returns the number of cases."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """SELECT c.id AS case_id, c.opened_reason, e.id AS event_id, e.source,
                      e.thread_ref, e.content, ce.note,
                      EXISTS (SELECT 1 FROM events d
                              WHERE d.thread_ref = e.thread_ref AND d.provenance = 'self'
                                AND d.source = 'whatsapp' AND d.id > e.id) AS replied
               FROM cases c
               JOIN case_events ce ON ce.case_id = c.id
               JOIN events e ON e.id = ce.event_id AND e.provenance = 'external'
               WHERE c.status = 'open' AND NOT c.sandbox AND c.origin IS NULL
                 AND NOT EXISTS (SELECT 1 FROM expectations x WHERE x.case_id = c.id)
               ORDER BY c.id, e.id""")
        by_case: dict[int, list[dict]] = {}
        for r in rows:
            by_case.setdefault(r["case_id"], []).append(dict(r))
        adopted = 0
        for cid, case_rows in by_case.items():
            reqs = _requests_to_adopt(case_rows)
            if not reqs:
                continue
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if await conn.fetchval(
                            "SELECT 1 FROM expectations WHERE case_id = $1 LIMIT 1", cid):
                        continue
                    for q in reqs:
                        await conn.execute(
                            """INSERT INTO expectations (case_id, description, owner_id, due_at,
                                                         satisfy_hint, owed_by, source_event_id)
                               VALUES ($1, $2, NULL, NULL, $3, 'assistant', $4)""",
                            cid, q["description"][:500], q["satisfy_hint"], q["event_id"])
            adopted += 1
        if adopted:
            log.info("quick 024: %d open case(s) of requests to the assistant now carry its "
                     "commitments; the satisfaction sweep checks the replies next", adopted)
        return adopted
    except Exception:
        log.exception("quick 024 request clean-up failed (cases left as they were)")
        return 0


# ── satisfaction sweep ─────────────────────────────────────────────────────
# The assistant's own replies are provenance='self' and excluded from the triage
# batch (feedback-loop guard). But a reply often DELIVERS what an open expectation
# was waiting for. This sweep closes those: it shows the model the assistant's
# recent deliveries + open expectations and satisfies the ones clearly fulfilled —
# regardless of who delivered (the requester got what they needed).
#
# Quick 024: an expectation the ASSISTANT owes (a request made to it in a chat) is
# met only by the assistant's reply in that same chat, after the request. It is
# offered to the model only with such a delivery in the payload, and code checks
# the chat and the order of every match. A commitment never checked before also
# gets the first replies in its chat after the request, whatever their
# triage_status, because the reply may have been swept before triage recorded the
# commitment (a failed triage call, a backlog of more than one batch).

SATISFY_SYSTEM = """You decide which open EXPECTATIONS have now been fulfilled by the \
assistant's recent DELIVERIES (messages the assistant already sent, containing data or \
answers). An expectation is fulfilled when the awaited information/deliverable was \
provided — even if the assistant provided it directly rather than the named owner. Mark \
ONLY ones clearly fulfilled by a delivery below; when unsure, leave it open.

Some expectations are owed by the ASSISTANT itself ("owed_by": "assistant"): something a \
person asked the assistant for in a chat; "asked" is the person's own words. One of those is \
fulfilled only by a delivery in the SAME "chat", with a delivery_event_id greater than its \
"asked_in_event", that actually gives the main thing asked (the answer, the figures, the \
specs, or says the requested file was sent); caveats are fine. A reply that clearly says \
the assistant CANNOT do what was asked (it has no tool for it, is not allowed to, or it is \
not possible), with or without a reason, a workaround or advice on how to do it yourself, \
DOES fulfil it: "no, and here is why" is a final answer, so the request is closed (owner \
decision, quick 051). It is NOT fulfilled by a progress note ("on it", "checking now"), a \
reply saying it could not find, get or do it only because of a temporary failure (an \
error, a time-out, "try again later", "I will retry"), a promise to come back \
later, a question back to the person, a reply that leaves out the main thing asked, or a \
reply about something else. A reply that truthfully reports there is nothing for the period asked (a zero count, "none received", with the last activity date when it gives one) DOES give the answer: when no records exist, empty details are the correct answer, so it fulfils the request. A reply that only says it could not look, or gives a period other than the one asked with no figures for the asked one, does not. A request for "today" is dated on the business calendar ("now" in the input); a reply that says the business day has only just started and gives the last full day plus today so far fulfils it. Always give the delivery_event_id for these.

Return ONLY JSON: {"satisfied": [{"expectation_id": <int>, "delivery_event_id": <int>}]}."""

CATCHUP_REPLIES = 5     # replies per never-checked assistant commitment


def _merge_deliveries(*groups) -> list[dict]:
    """Deliveries from several queries, each once, oldest first."""
    seen: dict[int, dict] = {}
    for rows in groups:
        for r in rows or []:
            d = dict(r)
            if isinstance(d.get("id"), int) and d["id"] not in seen:
                seen[d["id"]] = d
    return [seen[i] for i in sorted(seen)]


def _answers_commitment(exp: dict, delivery: dict | None) -> bool:
    """Code's check on a match for an assistant commitment: a reply in the chat the
    request came from, sent after it."""
    if not delivery or not exp.get("asked_in"):
        return False
    src = exp.get("source_event_id")
    return (delivery.get("thread_ref") == exp["asked_in"]
            and isinstance(src, int) and delivery["id"] > src)


async def _satisfaction_sweep() -> None:
    """Close open expectations that the assistant's recent deliveries fulfilled."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """SELECT id, content, thread_ref FROM events
               WHERE provenance = 'self' AND triage_status = 'pending'
                 AND source IN ('whatsapp','system')
                 AND occurred_at > now() - interval '30 minutes'
               ORDER BY id LIMIT 20""")
        catchup = await conn.fetch(
            # Quick 024: replies in the chat of a never-checked assistant commitment,
            # after its request, already swept or not. Same scope as the index below:
            # NOT c.sandbox, and an open case only.
            """SELECT d.id, d.content, d.thread_ref
               FROM expectations x
               JOIN cases c ON c.id = x.case_id
               JOIN events src ON src.id = x.source_event_id
               CROSS JOIN LATERAL (
                   SELECT r.id, r.content, r.thread_ref FROM events r
                    WHERE r.thread_ref = src.thread_ref AND r.provenance = 'self'
                      AND r.source = 'whatsapp' AND r.id > src.id
                    ORDER BY r.id LIMIT $1) d
               WHERE x.status = 'open' AND x.owed_by = 'assistant'
                 AND x.last_checked_at IS NULL
                 AND NOT c.sandbox AND c.status = 'open'
               ORDER BY d.id LIMIT 40""", CATCHUP_REPLIES)
        deliveries = _merge_deliveries(pending, catchup)
        if not deliveries:
            return
        open_exps = await conn.fetch(
            # NOT c.sandbox: the assistant's real deliveries must never satisfy
            # a test fixture's expectations (which would rush a sandbox case to
            # auto-complete), and fictional expectations must not pollute the
            # satisfaction prompt. Quick 024: owed_by and the request's chat ride
            # along; an assistant commitment counts only while its case is open.
            """SELECT e.id, e.description, e.satisfy_hint, c.title AS case_title,
                      e.owed_by, e.source_event_id, src.thread_ref AS asked_in,
                      src.content AS asked_text
               FROM expectations e JOIN cases c ON c.id = e.case_id
               LEFT JOIN events src ON src.id = e.source_event_id
               WHERE e.status = 'open' AND NOT c.sandbox
                 AND (e.owed_by IS DISTINCT FROM 'assistant' OR c.status = 'open')
               ORDER BY e.id DESC LIMIT 60""")

    # Quick 024: an assistant commitment is offered only when a delivery here could
    # meet it (a later reply in its chat); without one there is nothing to judge,
    # and no model call is made when nothing is left.
    offered, assistant = [], {}
    for row in open_exps:
        e = dict(row)
        if e.get("owed_by") == "assistant":
            if not any(_answers_commitment(e, d) for d in deliveries):
                continue
            assistant[e["id"]] = e
        offered.append(e)

    delivery_ids = [d["id"] for d in deliveries]
    if offered and _gate.wait() > 0:  # Quick 039: triage model calls back off or pause
        return
    if offered:
        chats: dict[str, str] = {}   # thread_ref -> opaque label (no numbers to the model)

        def chat(ref) -> str | None:
            return chats.setdefault(str(ref), f"chat-{len(chats) + 1}") if ref else None

        def exp_payload(e: dict) -> dict:
            if e["id"] in assistant:
                # judged against the person's own words, not triage's hint: a hint
                # once read "... or state clearly that it could not be accessed",
                # which turned "I can't get that file" into a met commitment
                return {"expectation_id": e["id"], "case": e["case_title"],
                        "description": e["description"],
                        "asked": (e.get("asked_text") or "")[:500],
                        "owed_by": "assistant", "chat": chat(e["asked_in"]),
                        "asked_in_event": e["source_event_id"]}
            return {"expectation_id": e["id"], "case": e["case_title"],
                    "description": e["description"], "satisfies_when": e["satisfy_hint"]}

        payload = {
            "now": bizclock.business_now_text(),  # Quick 049: what "today" means
            "assistant_deliveries": [
                {"delivery_event_id": d["id"], "chat": chat(d.get("thread_ref")),
                 "content": (d.get("content") or "")[:800]}
                for d in deliveries],
            "open_expectations": [exp_payload(e) for e in offered],
        }
        try:
            from .agents.graph import _callbacks
            out = await _model().ainvoke(
                [("system", SATISFY_SYSTEM), ("user", json.dumps(payload, default=str))],
                config=_callbacks())
            parsed = _extract_json(out.content) or {}
            _gate.success(("sweep", tuple(sorted(delivery_ids))))  # Quick 039
            sats = [s for s in parsed.get("satisfied", [])
                    if isinstance(s, dict) and isinstance(s.get("expectation_id"), int)]
            by_id = {d["id"]: d for d in deliveries}
            person, answered = [], []
            for s in sats:
                xid, did = s["expectation_id"], s.get("delivery_event_id")
                did = did if isinstance(did, int) else None
                if xid in assistant:
                    if _answers_commitment(assistant[xid], by_id.get(did)):
                        answered.append((xid, did))
                    else:
                        log.info("satisfaction sweep: match for assistant commitment %s "
                                 "ignored (delivery %s is not a later reply in its chat)",
                                 xid, did)
                else:
                    person.append((xid, did))
            pool = await get_pool()
            async with pool.acquire() as conn:
                for xid, did in person:
                    await conn.execute(
                        # NOT sandbox again, in the write itself: the index
                        # above only shows real expectations, but a
                        # hallucinated id must not reach a sandbox row (nor an
                        # assistant commitment, which needs the checks above).
                        """UPDATE expectations SET status = 'satisfied',
                           satisfied_by = $2, resolved_at = now()
                           WHERE id = $1 AND status = 'open'
                             AND owed_by IS DISTINCT FROM 'assistant'
                             AND case_id IN (SELECT id FROM cases WHERE NOT sandbox)""",
                        xid, did)
                for xid, did in answered:
                    await conn.execute(
                        """UPDATE expectations SET status = 'satisfied',
                           satisfied_by = $2, resolved_at = now()
                           WHERE id = $1 AND status = 'open' AND owed_by = 'assistant'
                             AND case_id IN (SELECT id FROM cases WHERE NOT sandbox)""",
                        xid, did)
                if assistant:
                    # judged once with the replies so far; later replies arrive
                    # 'pending' and are offered again on their own
                    await conn.execute(
                        """UPDATE expectations SET last_checked_at = now()
                           WHERE id = ANY($1::bigint[]) AND owed_by = 'assistant'""",
                        list(assistant))
            if sats:
                log.info("satisfaction sweep: %d expectation(s) met by assistant "
                         "deliveries (%d answered request(s))",
                         len(person) + len(answered), len(answered))
        except Exception as e:
            # Quick 039: backoff and spend pause shared with the batch; at the attempt
            # cap the deliveries are given up as if nothing matched.
            if not await _sweep_failed(e, delivery_ids, list(assistant)):
                return  # don't mark skipped — retried after the backoff

    pool = await get_pool()
    await pool.execute(
        "UPDATE events SET triage_status = 'skipped' WHERE id = ANY($1::bigint[])", delivery_ids)


# ── Quick 039: triage model failures (backoff, attempt cap, spend-limit pause) ──
# One gate for every triage model call (the batch and the satisfaction sweep): a
# failure delays the next call (triage_batch_seconds doubling to
# triage_backoff_max_seconds), a spend limit pauses them all for
# triage_spend_pause_minutes, and an item that fails triage_max_attempts times is
# given up. A given-up batch is 'failed' with meta.triage_gave_up, the one existing
# non-destructive status, and is requeued GAVE_UP_REQUEUES time(s) by the next
# batch after a triage model call succeeds (an operator can requeue it the same way:
# UPDATE events SET triage_status = 'pending' WHERE meta ? 'triage_gave_up').

GAVE_UP_REQUEUES = 1
_gate = modelgate.ModelGate()
# Requeue once _gate.successes passes this mark; None = nothing to requeue. 0 at boot:
# the first batch after the first answered call looks once (give-ups before a restart).
_requeue_mark: int | None = 0

_GIVE_UP_SQL = """UPDATE events SET triage_status = 'failed',
       meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object('triage_gave_up',
              jsonb_build_object('at', now(), 'attempts', $2::int, 'error', $3::text))
 WHERE id = ANY($1::bigint[]) AND triage_status = 'pending'"""

_REQUEUE_SQL = """UPDATE events SET triage_status = 'pending',
       meta = (meta - 'triage_gave_up') || jsonb_build_object('triage_requeued',
              COALESCE((meta->>'triage_requeued')::int, 0) + 1)
 WHERE triage_status = 'failed' AND provenance = 'external' AND meta ? 'triage_gave_up'
   AND COALESCE((meta->>'triage_requeued')::int, 0) < $1"""


class TriageRefusal(Exception):
    """The triage model declined; counted as a failed attempt."""


class TriageGaveUp(RuntimeError):
    """Recorded through jobruns.loop_error when triage gives an item up."""


def _gate_limits() -> dict:
    s = get_settings()

    def num(name, default, lo):
        try:
            return max(lo, int(getattr(s, name, default) or default))
        except (TypeError, ValueError):
            return default
    return {"base": num("triage_batch_seconds", 10, 2),
            "cap": num("triage_backoff_max_seconds", 600, 10),
            "max_attempts": num("triage_max_attempts", 5, 1),
            "pause": num("triage_spend_pause_minutes", 60, 1) * 60}


def _clock_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%H:%M UTC")


async def _model_failed(job, events: list[dict], exc: BaseException) -> int:
    """A failed triage model call on this batch: back off, pause or give up. The
    events stay 'pending' unless given up. Returns 0 (nothing handled)."""
    global _requeue_mark
    lim = _gate_limits()
    out = _gate.failure(("batch", events[0]["id"]), exc, **lim)
    what = modelgate.describe(exc)
    if out.kind == "paused":
        until = _clock_after(out.wait)
        log.warning("triage: spend limit reached; model calls paused until %s (%s)", until, what)
        job.fail(f"The AI provider's spend limit was reached ({what}); triage pauses its "
                 "model calls and the messages stay pending",
                 summary=f"Paused until {until}; {len(events)} message(s) waiting")
        return 0
    if out.kind == "retry":
        log.warning("triage model call failed (attempt %d of %d, next try in %d s): %s",
                    out.attempts, lim["max_attempts"], round(out.wait), what,
                    exc_info=out.attempts == 1 and not isinstance(exc, TriageRefusal))
        job.fail(f"The triage model call failed ({what}); the messages stay pending for retry",
                 summary=f"Attempt {out.attempts} of {lim['max_attempts']}; "
                         f"next try in {round(out.wait)} s")
        return 0
    ids = [e["id"] for e in events]
    msg = (f"triage gave up on {len(ids)} message(s) after {out.attempts} failed model "
           f"calls ({what}); they are marked failed and retried once after the model "
           "answers again")
    log.error(msg)
    pool = await get_pool()
    await pool.execute(_GIVE_UP_SQL, ids, out.attempts, what)
    _requeue_mark = _gate.successes
    job.fail(msg[0].upper() + msg[1:], counts={"events": len(ids), "attempts": out.attempts})
    await jobruns.loop_error("triage", TriageGaveUp(msg))
    return 0


async def _requeue_given_up(conn) -> None:
    """Once a triage model call has succeeded since the last give-up: put batches
    given up on a model error back in the queue, each at most GAVE_UP_REQUEUES
    times. Runs before the batch fetch, so they are triaged next. Fail-soft."""
    global _requeue_mark
    mark = _requeue_mark
    if mark is None or _gate.successes <= mark:
        return
    _requeue_mark = None
    try:
        status = await conn.execute(_REQUEUE_SQL, GAVE_UP_REQUEUES)
        n = str(status or "").split()[-1:] or ["0"]
        if n[0].isdigit() and int(n[0]):
            log.info("triage: requeued %s message(s) given up during a model outage", n[0])
    except Exception:
        if _requeue_mark is None:
            _requeue_mark = mark
        log.exception("triage: requeue of given-up messages failed; tried again next batch")


async def _sweep_failed(exc: BaseException, delivery_ids: list, assistant_ids: list) -> bool:
    """A failed satisfaction-sweep call. True = give the deliveries up: assistant
    commitments are stamped checked and the caller marks the deliveries skipped,
    exactly as when nothing matched. False = leave them for the next try."""
    lim = _gate_limits()
    out = _gate.failure(("sweep", tuple(sorted(delivery_ids))), exc, **lim)
    what = modelgate.describe(exc)
    if out.kind != "give_up":
        log.warning("satisfaction sweep failed (%s, next try in %d s): %s",
                    "spend limit, paused" if out.kind == "paused"
                    else f"attempt {out.attempts} of {lim['max_attempts']}",
                    round(out.wait), what)
        return False
    msg = (f"triage satisfaction sweep gave up on {len(delivery_ids)} delivery(ies) after "
           f"{out.attempts} failed model calls ({what}); treated as matching nothing")
    log.error(msg)
    if assistant_ids:
        pool = await get_pool()
        await pool.execute(
            """UPDATE expectations SET last_checked_at = now()
               WHERE id = ANY($1::bigint[]) AND owed_by = 'assistant'""", assistant_ids)
    await jobruns.loop_error("triage", TriageGaveUp(msg))
    return True


# ── loop ────────────────────────────────────────────────────────────────────

async def _tick() -> int:
    """One iteration of the triage loop body, extracted so tests can pin the
    wiring: deleting any sweep from here (or this from run_forever) must fail a
    test, not silently kill the feature in production."""
    handled = await _triage_batch()
    await _satisfaction_sweep()
    await _complete_finished_cases()   # close+notify cases finished via the sweep
    await _close_stale_cases()         # close cases nobody has touched in weeks
    return handled


async def run_forever() -> None:
    log.info("triage loop started (batch every %ss)", get_settings().triage_batch_seconds)
    from . import usage
    usage.set_attribution(feature="triage")  # Quick 005: this loop task's model calls
    while True:
        try:
            handled = await _tick()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("triage DB connection blip, retrying: %s", type(e).__name__)
            handled = 0
        except Exception as e:
            log.exception("triage loop error")
            await jobruns.loop_error("triage", e)  # Quick 008 (never raises)
            handled = 0
        if not handled:
            await asyncio.sleep(max(2, get_settings().triage_batch_seconds))
