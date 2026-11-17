"""Chat registry — which conversations the assistant is allowed to act in.

Being ADDED to a WhatsApp group is not consent to act in it. Anyone can add a
number to any group, and the assistant lands in family chats and vendor threads
as easily as the one it was meant for. So a chat is recorded on first sight as
`pending` and stays silent until a human activates it.

The allowlist and the onboarding queue are deliberately the same object: the
thing that makes a group safe to act in (a human looked at it) is the same thing
that gives the assistant its context (that human said what the group is for).
That context cannot come from anywhere else — the connector only ever sees
messages from now onwards, so there is no history to infer a purpose from.

Direct chats are NOT gated. A person messaging the assistant directly is
self-evidently addressing it; the phone allowlist (ALLOWED_NUMBERS) covers that
case and is enforced at the webhook.
"""

import asyncio
import logging
import time

from .db import get_pool

log = logging.getLogger("chats")

# Roster staleness/throttle knobs for the webhook-piggybacked refresh. The
# throttle state is in-process — a restart forgets it, worst case one extra
# sync per group after boot.
MEMBER_SYNC_STALE_SECONDS = 24 * 3600     # roster older than this is refreshed
MEMBER_SYNC_THROTTLE_SECONDS = 15 * 60    # min gap between ATTEMPTS per jid
_last_sync_attempt: dict[str, float] = {}
_sync_tasks: set = set()                  # strong refs: bare create_task results are GC-bait

ONBOARDING_QUESTION = (
    "I've just been added to the WhatsApp group \"{subject}\".\n\n"
    "What is it for, and should I track it? A sentence here is enough to switch "
    "it on — or open the dashboard Chats tab and I'll interview you properly "
    "about it (who matters, what to follow up on, when to speak up).\n\n"
    "Reply with a short description to activate it, or say \"ignore\" to stay quiet."
)


async def ensure_chat(jid: str, subject: str | None = None,
                      is_group: bool = True) -> dict:
    """Record a chat on first sight; refresh subject/last_seen thereafter.

    Returns the row. New group chats arrive `pending` — the CHECK constraint's
    default — so a chat is never active by accident.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO chats (jid, subject, is_group, status)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (jid) DO UPDATE
                 SET subject      = COALESCE(EXCLUDED.subject, chats.subject),
                     last_seen_at = now()
               RETURNING *, (xmax = 0) AS is_new""",
            jid, subject, is_group,
            # A direct chat needs no approval; only groups are gated.
            "pending" if is_group else "active")
    return dict(row)


async def is_active(jid: str) -> bool:
    pool = await get_pool()
    return bool(await pool.fetchval(
        "SELECT 1 FROM chats WHERE jid = $1 AND status = 'active'", jid))


async def list_chats() -> list[dict]:
    # member_count/known_count ride along so the 15s-polled Chats page needs
    # no second round-trip; the full roster is a separate on-demand endpoint.
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT c.jid, c.subject, c.is_group, c.status, c.purpose,
                  c.awaiting_purpose, c.last_seen_at, c.created_at,
                  c.members_synced_at,
                  (SELECT count(*) FROM chat_members m
                    WHERE m.chat_jid = c.jid) AS member_count,
                  (SELECT count(*) FROM chat_members m
                     JOIN people p ON p.active
                          AND (p.whatsapp = m.number OR m.number = ANY(p.whatsapp_alt))
                    WHERE m.chat_jid = c.jid) AS known_count
           FROM chats c
           ORDER BY (c.status = 'pending') DESC, c.last_seen_at DESC""")
    return [dict(r) for r in rows]


async def set_status(jid: str, status: str | None = None,
                     purpose: str | None = None) -> None:
    """Update a chat's status and/or purpose.

    `status=None` KEEPS the current status. Defaulting it to 'pending' instead
    would mean that editing only the purpose of an active group silently
    silences it — a partial update should never revoke approval.
    """
    if status is not None and status not in ("pending", "active", "ignored"):
        raise ValueError(f"bad status {status!r}")
    pool = await get_pool()
    await pool.execute(
        """UPDATE chats
              SET status = COALESCE($2, status),
                  purpose = COALESCE($3, purpose),
                  awaiting_purpose = false,
                  updated_at = now()
            WHERE jid = $1""",
        jid, status, purpose)
    log.info("chat %s -> %s", jid, status or "(status unchanged)")
    if status == "active":
        # Activation is the moment the roster starts to matter (context blocks,
        # membership answers). Fire-and-forget: a failure just leaves
        # members_synced_at NULL and the next group message retries via
        # maybe_sync_members. Covers BOTH the dashboard PATCH and the
        # WhatsApp-DM activation path in worker._handle_chat_onboarding.
        _schedule_sync(jid)


async def mark_asked(jid: str) -> None:
    """Record that we've put the onboarding question to the primary user."""
    pool = await get_pool()
    await pool.execute(
        """UPDATE chats SET awaiting_purpose = true, asked_at = now(),
                            updated_at = now()
            WHERE jid = $1""", jid)


async def awaiting_answer() -> dict | None:
    """The chat whose onboarding question is outstanding, if any.

    One at a time, oldest first: the primary user replies in a plain DM with no
    way to say WHICH group they mean, so two open questions would be ambiguous.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT jid, subject FROM chats
            WHERE awaiting_purpose ORDER BY asked_at NULLS LAST LIMIT 1""")
    return dict(row) if row else None


async def next_unasked() -> dict | None:
    """A pending chat we haven't asked about yet — but only when nothing else is
    outstanding, to preserve the one-question-at-a-time rule."""
    pool = await get_pool()
    if await pool.fetchval("SELECT 1 FROM chats WHERE awaiting_purpose LIMIT 1"):
        return None
    row = await pool.fetchrow(
        """SELECT jid, subject FROM chats
            WHERE status = 'pending' AND asked_at IS NULL AND is_group
            ORDER BY created_at LIMIT 1""")
    return dict(row) if row else None


# ── group membership roster ────────────────────────────────────────────────
#
# Ground truth for "who is in this group". Synced from the connector's
# groupMetadata; without it a member is invisible until they speak, and the
# reactive agent invents membership answers.

async def sync_members(jid: str) -> int | None:
    """Fetch the group roster from the connector and make chat_members match.

    Replace-style: upsert every present member, delete absent ones, refresh
    chats.subject/members_synced_at. Returns the member count, or None when
    the connector had nothing (unconfigured/down/not in the group) — in that
    case NOTHING is written, so a flaky connector can't wipe a good roster.
    """
    from . import people, whatsapp
    data = await whatsapp.fetch_group_members(jid)
    if data is None:
        return None

    # Normalize + dedupe gateway-side too: the connector sends bare numbers,
    # but normalize_phone also strips leading trunk zeros; a person present
    # under two raw forms collapses to one row, keeping any admin flag.
    members: dict[str, bool] = {}
    for m in data.get("members") or []:
        num = people.normalize_phone(m.get("number"))
        if num:
            is_admin = m.get("admin") in ("admin", "superadmin")
            members[num] = members.get(num, False) or is_admin

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for num, is_admin in members.items():
                await conn.execute(
                    """INSERT INTO chat_members (chat_jid, number, is_admin)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (chat_jid, number) DO UPDATE
                         SET is_admin = EXCLUDED.is_admin,
                             last_seen_at = now(), synced_at = now()""",
                    jid, num, is_admin)
            await conn.execute(
                """DELETE FROM chat_members
                    WHERE chat_jid = $1 AND NOT (number = ANY($2::text[]))""",
                jid, list(members))
            await conn.execute(
                """UPDATE chats SET subject = COALESCE($2, subject),
                                    members_synced_at = now(), updated_at = now()
                    WHERE jid = $1""",
                jid, data.get("subject"))
    log.info("synced %d member(s) for %s", len(members), jid)
    return len(members)


def maybe_sync_members(row: dict) -> bool:
    """Webhook hook: decide from the ensure_chat row whether a background
    roster sync is warranted (new group, or roster stale), throttle repeated
    attempts, and fire-and-forget it. Never raises; returns True if scheduled.
    """
    try:
        jid = row.get("jid")
        if not jid or not row.get("is_group"):
            return False
        synced = row.get("members_synced_at")
        stale = synced is None or (
            time.time() - synced.timestamp() > MEMBER_SYNC_STALE_SECONDS)
        if not (row.get("is_new") or stale):
            return False
        now = time.monotonic()
        last = _last_sync_attempt.get(jid)
        if last is not None and now - last < MEMBER_SYNC_THROTTLE_SECONDS:
            return False
        _last_sync_attempt[jid] = now
        return _schedule_sync(jid)
    except Exception:
        log.exception("member sync scheduling failed (message intake unaffected)")
        return False


def _schedule_sync(jid: str) -> bool:
    try:
        t = asyncio.create_task(_sync_members_bg(jid))
    except RuntimeError:
        return False   # no running loop (tests) — the next trigger retries
    _sync_tasks.add(t)
    t.add_done_callback(_sync_tasks.discard)
    return True


async def _sync_members_bg(jid: str) -> None:
    try:
        await sync_members(jid)
    except Exception:
        log.exception("background member sync failed for %s", jid)


async def list_members(jid: str) -> list[dict]:
    """Roster with person identity resolved AT READ TIME (primary + alt
    numbers), so renaming/renumbering a person fixes every roster instantly."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT m.number, m.is_admin, m.first_seen_at, m.synced_at,
                  p.id AS person_id, p.name
             FROM chat_members m
             LEFT JOIN people p
               ON p.active AND (p.whatsapp = m.number OR m.number = ANY(p.whatsapp_alt))
            WHERE m.chat_jid = $1
            ORDER BY m.is_admin DESC, p.name NULLS LAST, m.number""", jid)
    return [dict(r) for r in rows]


# ── Quick 024: a chat's name as people say it ──

async def display_name(pool, ref) -> str | None:
    """Where a case started, for the operator UIs: "WhatsApp · <group name>" or
    "WhatsApp group", "WhatsApp · <person>" or "WhatsApp chat", "Email · <person>"
    or "Email thread". Never the raw number or JID. None when there is no chat.
    Never raises: anything unexpected names the channel only."""
    if not ref:
        return None
    ref = str(ref).strip()
    try:
        from . import appchannel, people
        if appchannel.is_app_thread(ref):
            return "Ask chat"
        if ref == "dashboard":
            return "Dashboard upload"
        if ref.endswith("@g.us"):
            subject = await pool.fetchval("SELECT subject FROM chats WHERE jid = $1", ref)
            return f"WhatsApp · {subject}" if subject else "WhatsApp group"
        source = await pool.fetchval(
            """SELECT source FROM events WHERE thread_ref = $1 AND provenance = 'external'
               ORDER BY id LIMIT 1""", ref)
        if source == "email":
            name = await pool.fetchval(
                "SELECT name FROM people WHERE active AND lower(email) = lower($1) LIMIT 1", ref)
            return f"Email · {name}" if name else "Email thread"
        # A direct chat threads on the sender's number, or on "<lid>@lid" for a
        # sender known only by a private ID (Quick 017; older threads may hold the
        # bare ID). The name comes from the people registry, at read time.
        digits = people.normalize_phone(ref)
        if not digits:
            return "WhatsApp chat"
        numbers = [] if ref.lower().endswith("@lid") else [digits]
        mapped = await pool.fetchval("SELECT phone FROM wa_lid_map WHERE lid = $1", digits)
        if mapped:
            numbers.append(str(mapped))
        for number in numbers:
            name = await pool.fetchval(
                """SELECT name FROM people
                   WHERE active AND (whatsapp = $1 OR $1 = ANY(whatsapp_alt)) LIMIT 1""",
                number)
            if name:
                return f"WhatsApp · {name}"
        return "WhatsApp chat"
    except Exception:
        log.warning("chat name lookup failed", exc_info=True)
        return "WhatsApp group" if ref.endswith("@g.us") else "WhatsApp chat"
