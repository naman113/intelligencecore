"""WhatsApp connector — event side.

WhatsApp is push: the existing /webhook/whatsapp handler stays the source of
truth for the reactive task pipeline, and ADDITIONALLY records an `events` row
via ingest_inbound() (the two paths are independent; neither blocks the other).

Every outbound message we send records a self-provenance event via record_self()
so Baileys echoes can be recognized and excluded from triage.
"""

import logging
from datetime import datetime, timezone

from . import RawEvent, ingest
from .. import secretbox
from ..people import normalize_phone

log = logging.getLogger("connectors.whatsapp")


def _thread_ref(phone: str, group_jid: str | None = None) -> str:
    """Canonical thread key. Group → the (raw) group jid; direct chat → the
    NORMALIZED phone. Both the inbound and the self-record path use this so echo
    detection compares like-for-like (plan §1.10 feedback-loop prevention)."""
    if group_jid:
        return group_jid
    # An outbound send to a group arrives here with the GROUP JID in `phone`,
    # because worker.py sends to thread_id and a group thread_id is the jid.
    # normalize_phone would strip the "@g.us" and reduce it to digits, yielding
    # a thread_ref that can never match the inbound one — silently breaking echo
    # detection and the satisfaction sweep for every group.
    # Only a GROUP jid passes through. A user jid ("…@s.whatsapp.net") must
    # still normalize to digits so the self-record and inbound sides of a DM
    # converge — test_thread_ref_normalizes_direct_chat_phone pins that.
    if phone and phone.endswith("@g.us"):
        return phone
    return normalize_phone(phone) or (phone or "")


async def ingest_inbound(phone: str, text: str, wa_message_id: str | None,
                         name: str | None = None, group_jid: str | None = None,
                         is_group: bool = False, occurred_at: datetime | None = None,
                         triage_status: str = "pending",
                         to_assistant: bool | None = None) -> int | None:
    """Record an inbound WhatsApp message as an event. Best-effort — the caller
    must not let an ingest failure break the reactive path.

    Quick 024: `to_assistant` records whether a reply run was queued for this
    message (the connector's `mentioned` verdict, always true in a DM). Triage
    may record a commitment owed by the assistant only for such a message."""
    # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
    text = secretbox.scrub_pasted_secrets(text)[0]
    meta = {"group": bool(is_group), "sender_name": name} if name or is_group else {}
    if to_assistant is not None:
        meta["to_assistant"] = bool(to_assistant)
    ev = RawEvent(
        source="whatsapp",
        external_id=wa_message_id,
        content=text,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        actor_phone=phone,
        actor_alias=name,                    # lets alias resolution catch a known name
        actor_raw=phone,
        thread_ref=_thread_ref(phone, group_jid),
        meta=meta,
        triage_status=triage_status,
    )
    return await ingest(ev)


async def record_self(phone: str, text: str, external_id: str | None = None) -> None:
    """Record an outbound message we sent as a self-provenance event. Excluded
    from triage; used to detect and drop Baileys echoes. Never raises. thread_ref
    is normalized identically to the inbound path so the echo matches."""
    # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
    text = secretbox.scrub_pasted_secrets(text)[0]
    try:
        await ingest(RawEvent(
            source="whatsapp",
            external_id=external_id,
            content=text,
            occurred_at=datetime.now(timezone.utc),
            thread_ref=_thread_ref(phone),
            provenance="self",
        ))
    except Exception:
        log.debug("record_self failed (non-fatal)", exc_info=True)
