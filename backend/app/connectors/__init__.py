"""Connector layer — normalizes events from every source into the `events` table.

A Connector produces RawEvents (by polling or from a webhook); `ingest()` is the
single shared path that normalizes them, resolves the actor via the people
registry, dedupes, tags self-provenance, and inserts one `events` row.

Sources: whatsapp (push, via the webhook), email (mock file poll now), erp
(read-only DB poll), plus system/timer for internally-generated events.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .. import jobruns, people  # Quick 008: jobruns
from ..db import get_pool

log = logging.getLogger("connectors")


@dataclass
class RawEvent:
    source: str                              # whatsapp|email|erp|system|timer
    content: str
    occurred_at: datetime
    external_id: str | None = None           # natural dedupe key within a source
    actor_phone: str | None = None
    actor_email: str | None = None
    actor_alias: str | None = None
    actor_raw: str | None = None             # raw identifier to show for unmatched
    thread_ref: str | None = None
    meta: dict = field(default_factory=dict)
    provenance: str = "external"             # external|self
    # Born 'skipped' when the source chat isn't activated yet: the message is
    # still recorded (so the timeline is complete and can be re-triaged later)
    # but the ambient layer must not act on a chat a human hasn't approved.
    triage_status: str = "pending"           # pending|done|skipped|failed


class Connector(Protocol):
    """A source of events. Implement poll() for pull sources; push sources
    (WhatsApp) call ingest() directly from their webhook handler."""

    name: str

    async def poll(self) -> list[RawEvent]:
        ...


def _aware(dt: datetime | None) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _is_self_echo(conn, raw: RawEvent) -> bool:
    """Baileys echoes our own outbound back as inbound. If this inbound matches a
    recently-recorded self message on the same thread, it's an echo — mark it self
    so triage skips it (feedback-loop prevention, plan §1.10)."""
    if raw.provenance == "self":
        return True
    # Written to be sargable for idx_events_echo (thread_ref, provenance,
    # occurred_at DESC): plain equality / IS NULL on thread_ref instead of
    # IS NOT DISTINCT FROM (which the planner cannot use an index for), bare
    # column comparisons on provenance and occurred_at. The content match is a
    # residual filter over the handful of rows the index narrows to.
    row = await conn.fetchrow(
        """SELECT 1 FROM events
           WHERE (thread_ref = $3 OR (thread_ref IS NULL AND $3 IS NULL))
             AND provenance = 'self'
             AND occurred_at > now() - interval '10 minutes'
             AND source = $1
             AND btrim(content) = btrim($2)
           LIMIT 1""",
        raw.source, raw.content, raw.thread_ref)
    return row is not None


async def ingest(raw: RawEvent) -> int | None:
    """Normalize → resolve actor → dedupe → insert. Returns the event id, or None
    if it was a duplicate. Never raises on the dedupe race (ON CONFLICT)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            actor_id = None
            if raw.provenance == "external":
                actor_id = await people.resolve(
                    conn, phone=raw.actor_phone, email=raw.actor_email,
                    alias=raw.actor_alias)

            provenance = raw.provenance
            if provenance == "external" and await _is_self_echo(conn, raw):
                provenance = "self"

            actor_raw = raw.actor_raw or raw.actor_phone or raw.actor_email or raw.actor_alias
            event_id = await conn.fetchval(
                """INSERT INTO events
                     (source, external_id, actor_id, actor_raw, thread_ref, content,
                      meta, provenance, occurred_at, triage_status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10)
                   ON CONFLICT (source, external_id) DO NOTHING
                   RETURNING id""",
                raw.source, raw.external_id, actor_id, actor_raw, raw.thread_ref,
                raw.content, json.dumps(raw.meta, default=str), provenance,
                _aware(raw.occurred_at), raw.triage_status)
    if event_id is None:
        return None
    log.debug("ingested %s event %s (actor=%s, provenance=%s)",
              raw.source, event_id, actor_id, provenance)
    return event_id


# ── perception loop (polls the pull-based connectors) ─────────────────────

def _note_poll(job, new: dict, errors: list) -> None:
    """Quick 008: a poll is recorded only when it brought in new rows or a
    source failed. New rows are routine and collapse with their counts summed."""
    total = sum(new.values())
    counts = {"rows": total, **{k[:60]: v for k, v in list(new.items())[:20]}}
    if errors:
        job.fail("; ".join(errors)[:500], f"{total} new row(s)", counts)
    elif total:
        job.ok(f"{total} new row(s): " + ", ".join(f"{n} from {k}" for k, n in new.items()),
               counts, routine=True, collapse="sum")


async def run_forever() -> None:
    """Background perception loop for pull connectors (email mock + ERP poller).
    WhatsApp is push (webhook) and does not need polling. Follows worker.py
    conventions: crash-tolerant, connection-blip backoff, never dies."""
    import asyncio

    import asyncpg

    from .db_poller import DbPoller, get_poll_seconds
    from .email_mock import EmailMockConnector

    from .. import usage
    usage.set_attribution(feature="triage")  # Quick 005: poller classification is intake triage
    email = EmailMockConnector()
    db = DbPoller()
    log.info("perception loop started (email mock + db poller)")

    last_db = 0.0
    while True:
        try:
            loop_now = asyncio.get_event_loop().time()
            for ev in await email.poll():
                await ingest(ev)
            # Poll interval is read live each cycle so the dashboard can change it
            # without a restart.
            poll_s = await get_poll_seconds()
            if loop_now - last_db >= poll_s:
                last_db = loop_now
                async with jobruns.track("db_poll", "Database poller",  # Quick 008
                                         trigger=jobruns.every(poll_s)) as job:
                    new: dict[str, int] = {}
                    for ev in await db.poll():
                        if await ingest(ev) is not None:
                            label = str((ev.meta or {}).get("label") or ev.thread_ref or "rows")
                            new[label] = new.get(label, 0) + 1
                    _note_poll(job, new, getattr(db, "errors", []))
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("perception DB connection blip, retrying: %s", type(e).__name__)
            poll_s = 15
        except Exception as e:
            log.exception("perception loop error")
            await jobruns.loop_error("connectors", e)  # Quick 008 (never raises)
            poll_s = 15
        # Wake often enough to honor a short interval promptly, but never busy-loop.
        await asyncio.sleep(max(2, min(poll_s, 15)))
