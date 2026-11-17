"""Append knowledge to a case — the one sanctioned write path.

A case is the cross-channel convergence point: events from WhatsApp, email,
the DB poller and docintel all link to it via triage. But linked events are
raw material; when a conversation yields a distilled FACT about the case
("≈₹18 lakh reported pending on TC-006"), that fact needs a durable, visible
home. `cases.summary` is wholesale rewritten by the rolling summarizer, and
`case_events.note` is never read back — so notes are stored as synthetic
`events` rows linked to the case, which the timeline endpoint and the rolling
summary both already consume.

The synthetic row is `provenance='self'` + `triage_status='skipped'`: self
provenance keeps it out of triage's batch query (the anti-feedback-loop
invariant — a note about a case must never re-triage into a new case), and
'skipped' documents that this was never a pending inbound event.
"""

import json
import logging
import re

log = logging.getLogger("cases")

NOTE_CONTENT_CAP = 2000
FACT_FIELD_CAP = 64
FACT_VALUE_CAP = 300
ALIAS_CAP = 80


async def add_case_note(conn, case_id: int, text: str, kind: str = "note",
                        cap: int = NOTE_CONTENT_CAP, meta: dict | None = None) -> int | None:
    """Insert a note event and link it to the case. Returns the event id.

    `conn` is an acquired connection (caller owns the transaction — triage
    calls this inside its per-event tx so a rollback removes the note with
    everything else). `kind` lands in case_events.note ('triage',
    'investigation', ...) as the provenance label of the link. `cap` bounds
    the stored text (investigation findings keep their historical 8000). `meta` holds structured
    fields (exploration insights store title, severity, evidence SQL and evidence
    sample there) and defaults to an empty object.
    """
    text = (text or "").strip()
    if not text or not case_id:
        return None
    eid = await conn.fetchval(
        """INSERT INTO events (source, content, provenance, occurred_at, triage_status, meta)
           VALUES ('system', $1, 'self', now(), 'skipped', $2::jsonb) RETURNING id""",
        text[:cap], json.dumps(meta or {}, default=str))
    await conn.execute(
        """INSERT INTO case_events (case_id, event_id, note)
           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""", case_id, eid, kind)
    # A new note is activity (staleness clock) AND new material for the next
    # rolling-summary refresh — without the bump, a note-only case never
    # resummarizes and the fact stays invisible to the triage case index.
    await conn.execute(
        """UPDATE cases SET last_activity_at = now(),
              events_since_summary = events_since_summary + 1
            WHERE id = $1""", case_id)
    return eid


def _normalize_field(field) -> str:
    """snake_case a model-written field name so "Amount Pending", "amount-pending"
    and "amount_pending" all land on the SAME row lineage — supersession keys on
    (case_id, field), so a spelling drift would silently fork the history."""
    s = str(field or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:FACT_FIELD_CAP]


async def record_case_fact(conn, case_id: int, field, value,
                           source_event_id: int | None = None) -> int | None:
    """Record the current value of a named case fact, superseding prior ones.

    Notes are append-only prose; facts are latest-wins-with-history: "₹18 lakh
    pending" later corrected to "10 lakh" must yield ONE current value, not two
    contradicting notes. The insert-then-supersede pair keeps every observation
    (audit trail, like learnings' 'superseded') while `superseded_by IS NULL`
    always selects exactly the latest per (case, field). Returns the new fact
    id, or None when the field/value is unusable.

    No cases-row bump here: the event (or note) that carried the fact already
    bumped events_since_summary, and a fact never arrives alone.
    """
    field = _normalize_field(field)
    value = str(value or "").strip()[:FACT_VALUE_CAP]
    if not case_id or not field or not value:
        return None
    # Serialize concurrent writers of the same (case, field): under READ
    # COMMITTED, two overlapping insert-then-supersede pairs can't see each
    # other's uncommitted insert, and both rows would commit un-superseded.
    # Callers run inside a transaction, so the lock holds until commit.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($2, $1))",
        case_id, field)
    fid = await conn.fetchval(
        """INSERT INTO case_facts (case_id, field, value, source_event_id)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        case_id, field, value, source_event_id)
    await conn.execute(
        """UPDATE case_facts SET superseded_by = $3
            WHERE case_id = $1 AND field = $2
              AND superseded_by IS NULL AND id != $3""",
        case_id, field, fid)
    return fid


async def add_alias(conn, case_id: int, alias) -> bool:
    """Attach an identifier ("INV-2041", "the Northwind PO") to a case so triage
    can match future events on it — titles alone once misattached events.

    One guarded UPDATE: case-insensitive dedupe and the size cap live in the
    WHERE clause, so concurrent callers can't blow past the cap or double-add.
    Returns whether a row actually changed."""
    alias = str(alias or "").strip()[:ALIAS_CAP]
    if not alias or not case_id:
        return False
    result = await conn.execute(
        """UPDATE cases SET aliases = array_append(aliases, $2)
            WHERE id = $1 AND cardinality(aliases) < 12
              AND NOT EXISTS (SELECT 1 FROM unnest(aliases) a
                              WHERE lower(a) = lower($2))""",
        case_id, alias)
    return result.endswith("1")


def _compose_src(r) -> str:
    """Build the human-readable provenance label from a raw joined row.

    Python-side on purpose — the query returns the joined columns
    (source_event_id, source, actor_name, actor_raw) and this composes them,
    which is the seam the tests pin (test_case_facts._src_row). Shapes:
    'unattributed' (no source event, or the event row vanished), bare
    '{source}' for actor-less system/db rows (never 'system:unknown' noise),
    else '{source}:{actor}' with the registered name preferred over the raw
    handle, 'unknown' as the last resort."""
    if not r.get("source_event_id") or not r.get("source"):
        return "unattributed"
    source = str(r["source"])
    actor = r.get("actor_name") or r.get("actor_raw")
    actor = str(actor).strip() if actor is not None else ""
    if not actor and source in ("system", "db"):
        return source
    return f"{source}:{actor or 'unknown'}"


async def current_facts(conn, case_id: int, limit: int = 8) -> list[dict]:
    """Latest value per field for a case, each with `src` — WHO said it.

    A fact rendered without provenance reads as system ground truth even when
    it came from one chat message; stamping the source lets every consumer
    (reactive context, both case-detail UIs) show "per whatsapp:Vinayak, 11
    Aug" instead of an authoritative bare value. The query fetches the raw
    provenance columns and `src` is composed in Python (_compose_src). Works
    with a connection OR a pool (both expose .fetch) — the context assembler
    calls it on the pool."""
    rows = await conn.fetch(
        # field/value/observed_at exist only on case_facts, so they stay
        # unqualified — same shape as before src was added.
        """SELECT field, value, observed_at,
                  f.source_event_id, e.source, p.name AS actor_name,
                  e.actor_raw
             FROM case_facts f
             LEFT JOIN events e ON e.id = f.source_event_id
             LEFT JOIN people p ON p.id = e.actor_id
            WHERE f.case_id = $1 AND superseded_by IS NULL
            ORDER BY field LIMIT $2""", case_id, limit)
    return [{"field": r["field"], "value": r["value"],
             "observed_at": r["observed_at"], "src": _compose_src(r)}
            for r in rows]
