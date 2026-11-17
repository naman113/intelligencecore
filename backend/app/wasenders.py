"""WhatsApp sender identity at the gateway (Quick 017).

Modern WhatsApp may address a sender by a private ID (LID) instead of their
phone number. The connector forwards both identities it has: `phone` (empty
while WhatsApp hides it) and `lid`. This module is the one place that:

* decides whether a sender passes the allowlist (`is_allowed`): phone or LID
  listed, after normalising to digits, or the sender is `owner_number`. An
  empty Allowed numbers means no allowlist, exactly as before;
* keeps the LID map (`wa_lid_map`): a LID with its phone once seen together, so
  a later LID-only message is allowlisted and threaded as that phone, and a
  known LID with no phone yet, so a reply goes to `<lid>@lid` rather than to
  the LID with a phone-number suffix (`recipient_for`);
* for a LID-only sender with no phone on file, asks the connector for the
  LIDs of the numbers the owner entered (Allowed numbers, owner_number):
  Baileys' documented PN -> LID lookup, throttled (`match_by_lookup`). WhatsApp
  offers no LID -> PN lookup, so this is the only way to recognise the owner
  before WhatsApp reveals their number;
* records messages the allowlist dropped (`wa_blocked_senders`): who, when and
  the first few characters only, never the message body; and the owner's
  "Allow this sender" (`allow_sender`), which goes through the ordinary config
  save path, `configservice.save_many`.

Thread keying (main.py): a direct chat threads on the phone when known (from
the payload or the map), else on `<lid>@lid`, which the connector passes
through as a LID address. Outbound replies and owner notifications still go to
phone numbers whenever one is known.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from .people import normalize_phone

log = logging.getLogger("wasenders")

LID_SUFFIX = "@lid"
SNIPPET_CHARS = 24          # what Setup shows of a blocked message; never more
BLOCKED_KEEP = 200          # newest blocked senders kept
BLOCKED_SHOWN = 5           # Setup > WhatsApp default
MAX_ID_DIGITS = 20          # phone numbers are <= 15 digits, LIDs about 15
LOOKUP_TIMEOUT_S = 3.0      # an outbound send never waits longer on the map


def digits(raw) -> str:
    """Digits only, the same normalisation as people.normalize_phone (drops a
    '+', spaces, a JID suffix and leading trunk zeros)."""
    return normalize_phone(raw)


def mask(raw) -> str:
    """'123456789012345' -> '123…345' for log lines."""
    d = digits(raw) or str(raw or "")
    return d if len(d) <= 6 else f"{d[:3]}…{d[-3:]}"


def is_lid_jid(value) -> bool:
    return str(value or "").strip().lower().endswith(LID_SUFFIX)


def lid_address(lid) -> str:
    """The thread id / recipient for a sender known only by LID."""
    return f"{digits(lid)}{LID_SUFFIX}"


def is_allowed(settings, *, phone: str = "", lid: str = "") -> bool:
    """The allowlist rule (quick 046: default-deny). A sender passes when its
    phone or its LID is listed in Allowed numbers (digits compared) or it is
    owner_number; the raw exact match is kept so a non-digit test id listed
    verbatim still works. An empty list means owner only; empty list and no
    owner means nobody. Anyone passes only when whatsapp_open_to_anyone is
    explicitly on."""
    if getattr(settings, "whatsapp_open_to_anyone", False) is True:
        return True
    raw = {str(n).strip() for n in (getattr(settings, "allowlist", None) or ()) if str(n).strip()}
    if phone and phone in raw:
        return True
    listed = {digits(n) for n in raw} - {""}
    owner = digits(getattr(settings, "owner_number", "") or "")
    if owner:
        listed.add(owner)
    return any(d and d in listed for d in (digits(phone), digits(lid)))


def snippet(text: str | None) -> str:
    """The first few characters of a blocked message, whitespace flattened."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if not flat:
        return "[attachment]"
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"


# ── LID map ─────────────────────────────────────────────────────────────

async def note_lid(conn, lid: str, phone: str = "", source: str = "message") -> str:
    """Remember a LID, with its phone when the message carried both. A known
    phone is never cleared by a later LID-only message; a different phone from
    WhatsApp replaces it. Returns the phone now on file ('' if none)."""
    lid, phone = digits(lid), digits(phone)
    if not lid:
        return phone
    stored = await conn.fetchval(
        """INSERT INTO wa_lid_map (lid, phone, source) VALUES ($1, NULLIF($2, ''), $3)
           ON CONFLICT (lid) DO UPDATE SET
               phone = COALESCE(EXCLUDED.phone, wa_lid_map.phone),
               source = CASE WHEN EXCLUDED.phone IS NULL THEN wa_lid_map.source
                             ELSE EXCLUDED.source END,
               last_seen = now()
           RETURNING phone""", lid, phone, source)
    return stored or ""


async def phone_for_lid(conn, lid: str) -> str:
    return await conn.fetchval(
        "SELECT phone FROM wa_lid_map WHERE lid = $1", digits(lid)) or ""


LOOKUP_EVERY_S = 600       # each allowlisted number asked of WhatsApp at most every 10 min
_looked_up: dict[str, float] = {}


def expected_phones(settings) -> list[str]:
    """Digits of every Allowed numbers entry plus owner_number: the people the
    owner told us about, whose private IDs are worth asking WhatsApp for."""
    raw = [str(n).strip() for n in (getattr(settings, "allowlist", None) or ()) if str(n).strip()]
    out = {digits(n) for n in raw}
    out.add(digits(getattr(settings, "owner_number", "") or ""))
    return sorted(d for d in out if 6 <= len(d) <= 15)


async def match_by_lookup(conn, settings, lid: str) -> str:
    """A sender arrived only as a LID with no phone on file. Ask the connector
    for the LIDs of the expected numbers (Allowed numbers and owner_number)
    that have none on file yet, each at most once per LOOKUP_EVERY_S: Baileys'
    documented one-way PN -> LID lookup (lidMapping.getLIDsForPNs). Store the
    pairs and return the phone now mapped to `lid`, or ''. Only maps numbers
    the owner already entered; it never adds anything to the allowlist."""
    from . import whatsapp
    phones = expected_phones(settings)
    if not phones or not digits(lid):
        return ""
    # Skip numbers whose LID is already on file, and entries that are LIDs
    # themselves (an owner may have listed a private ID): neither needs asking.
    known = set()
    for r in await conn.fetch(
            """SELECT lid, phone FROM wa_lid_map
               WHERE phone = ANY($1::text[]) OR lid = ANY($1::text[])""", phones):
        known.add(r["phone"])
        known.add(r["lid"])
    now = time.monotonic()
    todo = [p for p in phones
            if p not in known and now - _looked_up.get(p, -LOOKUP_EVERY_S) >= LOOKUP_EVERY_S]
    if not todo:
        return ""
    for p in todo:
        _looked_up[p] = now
    pairs = await whatsapp.lookup_lids(todo)
    if pairs is None:
        log.info("allowlist: private-ID lookup unavailable (connector not reachable)")
        return ""
    for pr in pairs:
        if digits(pr.get("phone")) in todo:
            await note_lid(conn, pr.get("lid"), pr.get("phone"), source="lookup")
    log.info("allowlist: looked up private IDs for %d number(s), %d found", len(todo), len(pairs))
    return await phone_for_lid(conn, lid)


async def _lookup(target: str) -> tuple[bool, str]:
    """(is a known LID, its phone or '')."""
    from . import db
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT phone FROM wa_lid_map WHERE lid = $1", digits(target))
    if row is None:
        return False, ""
    return True, row["phone"] or ""


async def recipient_for(target: str) -> str:
    """Where an outbound message to `target` (a thread id or a number) goes.

    * a group or a full phone JID: unchanged;
    * a LID (`<lid>@lid`, or a bare number the map knows as a LID): its phone
      when known, else `<lid>@lid`;
    * anything else (a phone number): unchanged.

    Fail-soft: if the map cannot be read, the target goes out as given (an
    `@lid` address still reaches the person; the connector also prefers the
    phone when it knows the pair and never adds a phone suffix to a LID)."""
    t = str(target or "").strip()
    if not t or ("@" in t and not is_lid_jid(t)):
        return t
    d = digits(t)
    if not d:
        return t
    try:
        known, phone = await asyncio.wait_for(_lookup(d), LOOKUP_TIMEOUT_S)
    except Exception:
        log.warning("LID map unavailable; sending to %s as given", mask(t))
        return t
    if phone:
        return phone
    if known or is_lid_jid(t):
        return lid_address(d)
    return t


# ── Blocked senders ─────────────────────────────────────────────────────

async def record_blocked(conn, *, phone: str = "", lid: str = "", name: str | None = None,
                         text: str | None = None, chat_jid: str | None = None,
                         group_subject: str | None = None) -> None:
    """One row per sender (LID when known, else phone): newest snippet, count,
    last time. Only `snippet()` of the text is stored."""
    phone, lid = digits(phone), digits(lid)
    sender = lid or phone
    if not sender:
        return
    await conn.execute(
        """INSERT INTO wa_blocked_senders
               (sender, phone, lid, name, snippet, chat_jid, group_subject)
           VALUES ($1, NULLIF($2, ''), NULLIF($3, ''), $4, $5, $6, $7)
           ON CONFLICT (sender) DO UPDATE SET
               phone = COALESCE(EXCLUDED.phone, wa_blocked_senders.phone),
               lid = COALESCE(EXCLUDED.lid, wa_blocked_senders.lid),
               name = COALESCE(EXCLUDED.name, wa_blocked_senders.name),
               snippet = EXCLUDED.snippet,
               chat_jid = EXCLUDED.chat_jid,
               group_subject = EXCLUDED.group_subject,
               count = wa_blocked_senders.count + 1,
               last_at = now()""",
        sender, phone, lid, (str(name).strip()[:80] or None) if name else None,
        snippet(text), chat_jid or None, (str(group_subject)[:120] if group_subject else None))
    await conn.execute(
        """DELETE FROM wa_blocked_senders WHERE sender IN (
               SELECT sender FROM wa_blocked_senders
               ORDER BY last_at DESC OFFSET $1)""", BLOCKED_KEEP)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


async def list_blocked(limit: int = BLOCKED_SHOWN) -> list[dict]:
    from . import db
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT sender, phone, lid, name, snippet, chat_jid, group_subject,
                  count, first_at, last_at
           FROM wa_blocked_senders ORDER BY last_at DESC LIMIT $1""",
        max(1, min(int(limit), 50)))
    return [{**dict(r), "first_at": _iso(r["first_at"]), "last_at": _iso(r["last_at"])}
            for r in rows]


async def allow_sender(sender: str) -> dict:
    """The owner's "Allow this sender": add the phone or LID (digits) to
    allowed_numbers through configservice.save_many, then forget its blocked
    rows. Raises ValueError for anything that is not a plain id."""
    from . import configservice, db
    from .config import get_settings

    d = digits(sender)
    if not d or len(d) > MAX_ID_DIGITS or not re.fullmatch(r"[\s+\-()0-9]*(@lid)?", str(sender).strip()):
        raise ValueError("sender must be a phone number or a private ID (digits only)")
    current = [n.strip() for n in (get_settings().allowed_numbers or "").split(",") if n.strip()]
    changed = False
    detail = "Allowed."
    if not current:
        detail = "Allowed numbers is empty, so every sender is already allowed."
    elif d in {digits(n) for n in current}:
        detail = "Already in Allowed numbers."
    else:
        await configservice.save_many({"allowed_numbers": ",".join(current + [d])})
        changed = True
    pool = await db.get_pool()
    await pool.execute(
        """DELETE FROM wa_blocked_senders
           WHERE sender = $1 OR phone = $1 OR lid = $1
              OR lid IN (SELECT lid FROM wa_lid_map WHERE phone = $1)""", d)
    log.info("allowlist: owner allowed sender %s (changed=%s)", mask(d), changed)
    return {"allowed": d, "changed": changed, "detail": detail}
