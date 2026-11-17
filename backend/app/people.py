"""People registry — human-managed identity resolution.

Decision (plan §1.3): the system NEVER auto-creates people. It resolves an
incoming actor (phone / email / alias) to a registered person by EXACT match
only — no fuzzy matching in v1. Unresolved actors are surfaced on the People
dashboard for a human to register.

Normalization is the load-bearing part: WhatsApp numbers arrive in many shapes
(+91 87..., 91-87..., 0087...) so we reduce to digits and compare on that.
"""

import logging

from .db import get_pool

log = logging.getLogger("people")


def normalize_phone(raw: str | None) -> str:
    """Digits only. Drops '+', spaces, dashes, and a Baileys '@s.whatsapp.net'
    suffix. Leaves country code intact (we don't guess a default region)."""
    if not raw:
        return ""
    raw = str(raw).split("@", 1)[0]          # strip jid suffix if present
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.lstrip("0") if digits else ""   # drop leading trunk zeros


def normalize_email(raw: str | None) -> str:
    return (raw or "").strip().lower()


async def resolve(conn, *, phone: str | None = None, email: str | None = None,
                  alias: str | None = None) -> int | None:
    """Return a person id by EXACT match, or None. No fuzzy matching (v1).

    Precedence: phone → email → alias (case-insensitive). `conn` is an acquired
    asyncpg connection so this composes inside the caller's transaction."""
    ph = normalize_phone(phone)
    if ph:
        # A person may message from any of their numbers (personal + work SIM);
        # whatsapp is the primary/outbound one, whatsapp_alt the rest.
        pid = await conn.fetchval(
            """SELECT id FROM people
               WHERE active AND (whatsapp = $1 OR $1 = ANY(whatsapp_alt))
               LIMIT 1""", ph)
        if pid:
            return pid

    em = normalize_email(email)
    if em:
        pid = await conn.fetchval(
            "SELECT id FROM people WHERE lower(email) = $1 AND active LIMIT 1", em)
        if pid:
            return pid

    al = (alias or "").strip().lower()
    if al:
        # exact, case-insensitive match against name OR any alias
        pid = await conn.fetchval(
            """SELECT id FROM people
               WHERE active AND (lower(name) = $1
                     OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = $1))
               LIMIT 1""", al)
        if pid:
            return pid
    return None


# ── registry CRUD (backing the dashboard People tab) ──────────────────────

async def primary_person() -> dict | None:
    """The one person the assistant reports to — onboarding questions, digests,
    escalations. Falls back to the owner_number setting when nobody is flagged,
    so a fresh install still has somewhere to send things."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, whatsapp FROM people WHERE is_primary AND active LIMIT 1")
    if row and row["whatsapp"]:
        return dict(row)

    from .config import get_settings
    fallback = normalize_phone(get_settings().owner_number)
    if fallback:
        return {"id": None, "name": "owner", "whatsapp": fallback}
    return None


async def list_people() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, name, aliases, email, whatsapp, whatsapp_alt, role, active,
                  is_primary, created_at, updated_at
           FROM people ORDER BY is_primary DESC, active DESC, lower(name)""")
    return [dict(r) for r in rows]


def _normalize_alt(numbers, primary: str | None) -> list[str]:
    """Normalize a list of extra numbers: digits-only, deduped, primary
    excluded (a primary repeated as an alt would shadow uniqueness checks)."""
    out: list[str] = []
    for n in numbers or []:
        ph = normalize_phone(n)
        if ph and ph != (primary or "") and ph not in out:
            out.append(ph)
    return out


async def set_primary(person_id: int) -> None:
    """Make this person the primary user. Clearing the old flag first is
    required, not tidiness: idx_people_primary is UNIQUE on (is_primary) WHERE
    is_primary, so setting a second one would violate the constraint."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE people SET is_primary = false WHERE is_primary")
            await conn.execute(
                "UPDATE people SET is_primary = true, updated_at = now() WHERE id = $1",
                person_id)


async def create_person(name: str, aliases: list[str] | None = None,
                        email: str | None = None, whatsapp: str | None = None,
                        role: str | None = None,
                        whatsapp_alt: list[str] | None = None) -> int:
    pool = await get_pool()
    primary = normalize_phone(whatsapp) or None
    pid = await pool.fetchval(
        """INSERT INTO people (name, aliases, email, whatsapp, role, whatsapp_alt)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        name.strip(), [a.strip() for a in (aliases or []) if a.strip()],
        normalize_email(email) or None, primary,
        (role or "").strip() or None, _normalize_alt(whatsapp_alt, primary))
    return pid


async def update_person(person_id: int, fields: dict) -> None:
    """Patch a subset of columns; normalizes email/whatsapp when present."""
    sets, vals = [], []
    for col in ("name", "role", "email", "whatsapp", "active", "aliases",
                "whatsapp_alt"):
        if col not in fields:
            continue
        v = fields[col]
        if col == "email":
            v = normalize_email(v) or None
        elif col == "whatsapp":
            v = normalize_phone(v) or None
        elif col == "aliases":
            v = [a.strip() for a in (v or []) if str(a).strip()]
        elif col == "whatsapp_alt":
            primary = fields.get("whatsapp")
            if primary is None:
                pool = await get_pool()
                primary = await pool.fetchval(
                    "SELECT whatsapp FROM people WHERE id = $1", person_id)
            v = _normalize_alt(v, normalize_phone(primary))
        vals.append(v)
        sets.append(f"{col} = ${len(vals)}")
    if not sets:
        return
    vals.append(person_id)
    pool = await get_pool()
    await pool.execute(
        f"UPDATE people SET {', '.join(sets)}, updated_at = now() WHERE id = ${len(vals)}",
        *vals)


async def unmatched_senders(limit: int = 50) -> list[dict]:
    """Distinct raw actors we've seen inbound but can't resolve — the human's
    work queue on the People page."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT actor_raw,
                  array_agg(DISTINCT source) AS sources,
                  count(*) AS event_count,
                  max(occurred_at) AS last_seen,
                  (array_agg(content ORDER BY occurred_at DESC))[1] AS last_content
           FROM events
           WHERE actor_id IS NULL AND provenance = 'external' AND actor_raw IS NOT NULL
           GROUP BY actor_raw
           ORDER BY last_seen DESC
           LIMIT $1""", limit)
    return [dict(r) for r in rows]


async def reresolve_events() -> int:
    """After a person is created/edited, attach previously-unmatched events whose
    actor_raw now resolves. Returns the number of events re-linked.

    Bounded to the last 30 days: a person edit is a dashboard click and must not
    trigger a scan over the whole table's history — anything older stays
    unmatched (it was already surfaced on the People tab while it was fresh)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT actor_raw, source FROM events
               WHERE actor_id IS NULL AND provenance = 'external' AND actor_raw IS NOT NULL
                 AND occurred_at > now() - interval '30 days'""")
        linked = 0
        for r in rows:
            raw, source = r["actor_raw"], r["source"]
            pid = await resolve(
                conn,
                phone=raw if source in ("whatsapp",) else None,
                email=raw if source == "email" else None,
                alias=raw)
            if pid:
                n = await conn.fetchval(
                    """WITH u AS (UPDATE events SET actor_id = $1
                        WHERE actor_raw = $2 AND actor_id IS NULL AND provenance='external'
                          AND occurred_at > now() - interval '30 days'
                        RETURNING 1) SELECT count(*) FROM u""", pid, raw)
                linked += n or 0
        if linked:
            log.info("re-resolved %d previously-unmatched event(s)", linked)
        return linked
