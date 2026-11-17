"""Whose memory a run writes and reads (Quick 020, audit Fix 5a and 5b).

The one place that decides, from a run's thread:

* who the run is for (`identify`): the owner (web chat, or WhatsApp from
  owner_number, from another number of the person it resolves to, or from a
  private ID that Quick 017's LID map ties to one of those), another
  registered person, or nobody we can name (a group, whose messages carry no
  sender; an unknown number; a private ID with no number on file; the
  document inbox; an automatic run nobody asked for);
* where a preference written in that run applies (`RunIdentity.scope`) and
  which preferences it recalls (`RunIdentity.recall`). A preference applies
  only to the person who stated it; facts, corrections, recipes and lessons
  apply to everyone (scope NULL). Scopes hold numbers, never person ids (the
  roster rule): `wa:<digits>` is matched against the person's current
  numbers when recalled, so an edited person never silently keeps a stale id;
* the approval of a learning written by nobody we can name: it is stored
  `pending` and an `activate_learning` action waits on Approvals
  (`propose_activation`); approving runs `activate_from_proposal`, rejecting
  `retire_rejected`. Pending and rejected learnings are never recalled,
  merged or graphed (every read filters status = 'active');
* the scope of preferences stored before this existed
  (`migrate_preference_scopes`, idempotent, run at startup).

Never raises into a run: an identity that cannot be looked up falls back to
what the thread alone says (the owner by owner_number, else unattributed).
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass

from .config import get_settings
from .db import get_pool

log = logging.getLogger("memscope")

OWNER = "owner"
ACTION_KIND = "activate_learning"
GLOBAL_KINDS = ("fact", "correction", "recipe", "lesson")

# Set by the worker for each user run and cleared for playbook runs. Unset
# means no person asked for this run (a sentinel escalation): anything it
# remembers is unattributed and waits for the owner.
ctx_identity: ContextVar["RunIdentity | None"] = ContextVar("memscope_identity", default=None)


@dataclass(frozen=True)
class RunIdentity:
    kind: str                     # 'owner' | 'person' | 'unattributed'
    source: str                   # provenance stamped on learnings written in this run
    scope: str | None             # where a preference written in this run applies
    recall: tuple[str, ...]       # preference scopes this run recalls
    origin: str                   # 'web' | 'whatsapp' | 'group' | 'sender' | 'other' | 'automatic'
    label: str                    # who or where, in words (approval card, logs)
    thread_id: str = ""
    sender: str | None = None     # digits of a direct-chat sender (phone, else LID)

    @property
    def attributed(self) -> bool:
        return self.kind != "unattributed"

    def scope_for(self, kind: str) -> str | None:
        """Where a learning of `kind` written in this run applies."""
        return self.scope if kind == "preference" else None


# ── helpers ──────────────────────────────────────────────────────────────

def _digits(raw) -> str:
    from .people import normalize_phone
    return normalize_phone(raw)


def mask(raw) -> str:
    """'919812345678' -> '919…678' for log lines (never the full number)."""
    d = _digits(raw) or str(raw or "")
    return d if len(d) <= 6 else f"{d[:3]}…{d[-3:]}"


def is_group(thread_id) -> bool:
    return str(thread_id or "").strip().endswith("@g.us")


def _is_lid(thread_id) -> bool:
    return str(thread_id or "").strip().lower().endswith("@lid")


def _is_web(thread_id) -> bool:
    from . import appchannel
    t = str(thread_id or "").strip()
    return t == "dashboard" or appchannel.is_app_thread(t)


async def _person_numbers(conn, person_id: int) -> tuple[list[str], str | None]:
    """(primary + alt numbers, name) of a person; primary first."""
    row = await conn.fetchrow(
        "SELECT name, whatsapp, whatsapp_alt FROM people WHERE id = $1", person_id)
    if row is None:
        return [], None
    nums: list[str] = []
    for n in [row["whatsapp"], *(row["whatsapp_alt"] or [])]:
        d = _digits(n)
        if d and d not in nums:
            nums.append(d)
    return nums, (row["name"] or None)


async def _lids_for(conn, numbers) -> list[str]:
    """Private IDs Quick 017's LID map ties to these numbers. [] on error."""
    if not numbers:
        return []
    try:
        rows = await conn.fetch(
            "SELECT lid FROM wa_lid_map WHERE phone = ANY($1::text[]) ORDER BY lid",
            list(numbers))
        return [r["lid"] for r in rows if r["lid"]]
    except Exception:
        log.debug("LID map unreadable (non-fatal)", exc_info=True)
        return []


async def _phone_for_lid(conn, lid: str) -> str:
    try:
        from . import wasenders
        return await wasenders.phone_for_lid(conn, lid)
    except Exception:
        log.debug("LID map unreadable (non-fatal)", exc_info=True)
        return ""


async def owner_numbers(conn) -> tuple[set[str], int | None]:
    """The owner's WhatsApp numbers: owner_number plus every number of the
    person it resolves to (same rule as triage's owner validation), and that
    person's id. The primary-person flag is not used: it marks who digests go
    to, not who the owner is."""
    own = _digits(getattr(get_settings(), "owner_number", "") or "")
    nums = {own} if own else set()
    pid = None
    if own:
        from . import people
        pid = await people.resolve(conn, phone=own)
        if pid is not None:
            nums |= set((await _person_numbers(conn, pid))[0])
    nums.discard("")
    return nums, pid


def _owner_identity(thread_id: str, *, nums, lids, source: str, origin: str,
                    label: str, sender: str | None = None) -> RunIdentity:
    recall = (OWNER, *(f"wa:{n}" for n in sorted(nums)), *(f"lid:{x}" for x in sorted(lids)))
    return RunIdentity("owner", source, OWNER, tuple(dict.fromkeys(recall)), origin,
                       label, thread_id, sender)


def _nobody(thread_id: str, label: str | None = None) -> RunIdentity:
    """A run for nobody we can name and no sender id (the document inbox, a
    run with no thread). A preference it writes, once the owner approves it,
    applies to the owner, who approved it; it recalls no preferences."""
    if not label:
        label = ("the document inbox" if thread_id == "inbox"
                 else f"thread {thread_id}" if thread_id else "a run with no conversation")
    return RunIdentity("unattributed", "unattributed:sender", OWNER, (), "other",
                       label, thread_id, None)


def _sender_identity(thread_id: str, phone: str, lid: str) -> RunIdentity:
    scope = f"wa:{phone}" if phone else f"lid:{lid}"
    recall = tuple(dict.fromkeys(
        [scope] + ([f"lid:{lid}"] if lid and phone else [])))
    who = (f"+{phone}" if phone else f"private ID {lid}")
    return RunIdentity("unattributed", "unattributed:sender", scope, recall, "sender",
                       f"an unregistered sender ({who})", thread_id, phone or lid)


def _group_identity(thread_id: str, subject: str | None) -> RunIdentity:
    scope = f"chat:{thread_id}"
    label = f'the group "{subject}"' if subject else "a WhatsApp group"
    return RunIdentity("unattributed", "unattributed:group", scope, (scope,), "group",
                       label, thread_id, None)


def _fallback(thread_id: str) -> RunIdentity:
    """No database: decide from the thread and settings alone, never wider."""
    if _is_web(thread_id):
        own = _digits(getattr(get_settings(), "owner_number", "") or "")
        return _owner_identity(thread_id, nums={own} - {""}, lids=[], source="dashboard",
                               origin="web", label="the owner (web chat)")
    if is_group(thread_id):
        return _group_identity(thread_id, None)
    if _is_lid(thread_id):
        return _sender_identity(thread_id, "", _digits(thread_id))
    d = _digits(thread_id)
    own = _digits(getattr(get_settings(), "owner_number", "") or "")
    if d and own and d == own:
        return _owner_identity(thread_id, nums={own}, lids=[], source="owner_number",
                               origin="whatsapp", label="the owner (WhatsApp)", sender=d)
    if d:
        return _sender_identity(thread_id, d, "")
    return _nobody(thread_id)


# ── who a run is for ─────────────────────────────────────────────────────

async def identify(thread_id: str | None, *, pool=None) -> RunIdentity:
    """The identity of a run on `thread_id`. `pool` is the caller's (the
    worker passes its own); default the working-memory pool. Never raises."""
    t = str(thread_id or "").strip()
    try:
        return await _identify(t, pool)
    except Exception:
        log.warning("memory identity lookup failed for a %s thread; deciding from "
                    "the thread alone", "group" if is_group(t) else "direct",
                    exc_info=True)
        return _fallback(t)


async def _identify(t: str, pool=None) -> RunIdentity:
    if not t:
        return _nobody(t)
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        if _is_web(t):
            nums, _pid = await owner_numbers(conn)
            return _owner_identity(t, nums=nums, lids=await _lids_for(conn, nums),
                                   source="dashboard", origin="web",
                                   label="the owner (web chat)")
        if is_group(t):
            subject = None
            try:
                subject = await conn.fetchval("SELECT subject FROM chats WHERE jid = $1", t)
            except Exception:
                log.debug("group subject lookup failed (non-fatal)", exc_info=True)
            return _group_identity(t, subject)

        # A direct chat: the phone when known, else the private ID (Quick 017
        # threads a LID-only sender as "<lid>@lid").
        lid, phone = "", ""
        if _is_lid(t):
            lid = _digits(t)
            phone = await _phone_for_lid(conn, lid) if lid else ""
        else:
            phone = _digits(t)
        if not phone and not lid:
            return _nobody(t)

        nums, owner_pid = await owner_numbers(conn)
        from . import people
        pid = await people.resolve(conn, phone=phone) if phone else None
        if phone and phone not in nums and pid is None and not lid:
            # A thread created before Quick 017 may be a bare LID: the map
            # knows it as a LID (same check as wasenders.recipient_for).
            mapped = await _phone_for_lid(conn, phone)
            if mapped:
                lid, phone = phone, mapped
                pid = await people.resolve(conn, phone=phone)
        if pid is None and lid and not phone:
            # A People row (or owner_number, which the allowlist also accepts as
            # a private ID) may hold the private ID itself as a number.
            pid = await people.resolve(conn, phone=lid)

        if ({phone, lid} - {""}) & nums or (owner_pid is not None and pid == owner_pid):
            return _owner_identity(
                t, nums=nums, lids=await _lids_for(conn, nums),
                source=f"person:{owner_pid}" if owner_pid is not None else "owner_number",
                origin="whatsapp", label="the owner (WhatsApp)", sender=phone or lid)

        if pid is not None:
            numbers, name = await _person_numbers(conn, pid)
            if phone and phone not in numbers:
                numbers.append(phone)
            lids = await _lids_for(conn, numbers)
            if lid and lid not in lids:
                lids.append(lid)
            primary = numbers[0] if numbers else phone
            recall = tuple(dict.fromkeys(
                [f"wa:{n}" for n in numbers] + [f"lid:{x}" for x in lids]))
            return RunIdentity("person", f"person:{pid}", f"wa:{primary}", recall,
                               "whatsapp", name or f"+{primary}", t, phone or lid)

        return _sender_identity(t, phone, lid)


async def for_thread(thread_id: str | None) -> RunIdentity:
    """This run's identity when the worker set it for this thread, else a
    fresh lookup."""
    ident = ctx_identity.get()
    if ident is not None and ident.thread_id == str(thread_id or "").strip():
        return ident
    return await identify(thread_id)


async def automatic(thread_id: str | None) -> RunIdentity:
    """A run no person asked for (a sentinel escalation): unattributed, so a
    memory write waits for the owner. A preference it writes would apply where
    its thread's own preferences do."""
    base = await identify(thread_id) if thread_id else None
    where = f" in {base.label}" if base is not None and base.label else ""
    return RunIdentity("unattributed", "unattributed:automatic",
                       base.scope if base is not None else OWNER,
                       base.recall if base is not None else (), "automatic",
                       f"an automatic run nobody asked for{where}",
                       str(thread_id or ""), base.sender if base is not None else None)


async def recall_scopes(thread_id: str | None) -> list[str]:
    """Preference scopes a run on this thread recalls. [] = global only."""
    return list((await for_thread(thread_id)).recall)


# ── approval of a learning nobody we can name wrote ─────────────────────

def applies_to(kind: str, scope: str | None) -> str:
    """Where a learning would apply, in words."""
    if kind != "preference":
        return "everyone (facts, corrections, recipes and lessons are shared)"
    if scope == OWNER or scope is None:
        return "the owner only"
    if scope.startswith("chat:"):
        return "this group only"
    return "this sender only"


def proposal_payload(*, learning_id: int, kind: str, content: str,
                     identity: RunIdentity, run_id: int | None) -> dict:
    scope = identity.scope_for(kind)
    return {
        "learning_id": int(learning_id),
        "kind": kind,
        "content": content,
        "origin": identity.origin,
        "origin_label": identity.label,
        "thread_id": identity.thread_id or None,
        "sender": identity.sender,
        "scope": scope,
        "applies_to": applies_to(kind, scope),
        "run_id": run_id,
        "_summary": f"Remember this {kind} from {identity.label}: {content[:200]}",
    }


async def propose_activation(pool, *, learning_id: int, kind: str, content: str,
                             identity: RunIdentity, run_id: int | None) -> int | None:
    """The approval card for a pending learning (tier draft, proposed). Returns
    the action id, or the open one already waiting for this learning. Safe
    inside a transaction (ON CONFLICT, no exception to recover from)."""
    payload = proposal_payload(learning_id=learning_id, kind=kind, content=content,
                               identity=identity, run_id=run_id)
    aid = await pool.fetchval(
        "INSERT INTO actions (kind, payload, tier, status, task_run_id) "
        "VALUES ('activate_learning', $1::jsonb, 'draft', 'proposed', $2) "
        "ON CONFLICT ((payload->>'learning_id')) "
        "WHERE kind = 'activate_learning' AND status = 'proposed' DO NOTHING "
        "RETURNING id", json.dumps(payload), run_id)
    if aid is None:
        aid = await pool.fetchval(
            "SELECT id FROM actions WHERE kind = 'activate_learning' AND status = 'proposed' "
            "AND payload->>'learning_id' = $1", str(int(learning_id)))
    return aid


def _learning_id(payload) -> int | None:
    """The learning id of a card: a JSON integer only, as propose_activation
    writes it ("0457" or " 457" would dodge the one-open-card index)."""
    v = (payload or {}).get("learning_id") if isinstance(payload, dict) else None
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v > 0 else None


async def activate_from_proposal(payload: dict) -> bool:
    """actionsvc executor for activate_learning: the owner approved, so the
    pending learning becomes active (recalled from now on) and a fact is
    mirrored to the knowledge graph. Idempotent: an already active learning
    counts as done. False when the learning is gone, rejected or invalid, or
    when the card's text is not the stored learning's: the owner must have
    seen exactly what becomes active (the card renders the payload)."""
    lid = _learning_id(payload)
    content, kind = payload.get("content"), payload.get("kind")
    if lid is None or not isinstance(content, str) or not isinstance(kind, str):
        return False
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE learnings SET status = 'active', status_reason = NULL, updated_at = now()
           WHERE id = $1 AND status = 'pending' AND content = $2 AND kind = $3
           RETURNING id, kind, content, revisions""", lid, content, kind)
    if row is None:
        cur = await pool.fetchrow("SELECT status, content, kind FROM learnings WHERE id = $1", lid)
        if cur is not None and (cur["content"] != content or cur["kind"] != kind):
            log.warning("memory: approval for learning %s refused: the card's text is not "
                        "the stored learning's", lid)
            return False
        return cur is not None and cur["status"] == "active"
    try:
        from . import memory
        await memory.to_graph(row["id"], row["content"], row["kind"],
                              row["revisions"] or 0, pool=pool)
    except Exception:
        log.debug("graph mirror of approved learning failed (backfill retries)", exc_info=True)
    log.info("memory: learning %s (%s) approved by the owner and now active",
             row["id"], row["kind"])
    return True


async def retire_rejected(payload, action_id: int) -> bool:
    """actionsvc reject hook: the owner rejected it, so the pending learning is
    retired (kept with the reason, never recalled). Never raises."""
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
        lid = _learning_id(payload)
        if lid is None:
            return False
        pool = await get_pool()
        n = await pool.execute(
            """UPDATE learnings SET status = 'rejected', status_reason = $2, updated_at = now()
               WHERE id = $1 AND status = 'pending'""",
            lid, f"rejected by the owner on Approvals (action #{action_id})")
        return str(n).endswith(" 1")
    except Exception:
        log.exception("could not retire the learning of rejected action %s", action_id)
        return False


# ── preferences mirrored into the shared graph before Quick 020 ───────────

async def retire_preference_graph(limit: int = 200) -> dict | None:
    """Take preferences out of the shared knowledge graph. Before Quick 020
    every preference learning was mirrored into the global graph, where every
    run recalls it: its episode, the facts extracted from it and the entity
    summaries that absorbed it. Each preference that reached the graph
    (graphed_at set) is retired through memory.retire_learning_graph (Quick
    019's scoped delete, which keeps facts other episodes support and rewrites
    the surviving entity summaries from their live facts), then its graphed_at
    is cleared, so a second run finds nothing to do. Returns counts, or None
    when nothing could be read. Never raises."""
    try:
        if not getattr(get_settings(), "memory_graph_enabled", True):
            return {"retired": 0, "skipped": "the knowledge graph is switched off"}
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, content FROM learnings WHERE kind = 'preference' "
            "AND graphed_at IS NOT NULL ORDER BY id LIMIT $1", limit)
        if not rows:
            return {"retired": 0}
        from . import memory
        done = failed = 0
        for r in rows:
            res = await memory.retire_learning_graph(r["id"], kind="preference",
                                                     content=r["content"], pool=pool)
            if res is None or res.get("skipped"):
                failed += 1
                continue
            await pool.execute("UPDATE learnings SET graphed_at = NULL WHERE id = $1", r["id"])
            done += 1
        log.info("memory scope: %d preference(s) taken out of the shared graph (%d failed)",
                 done, failed)
        return {"retired": done, "failed": failed}
    except Exception:
        log.exception("taking preferences out of the shared graph failed (retried)")
        return None


async def retire_preference_graph_soon(first_delay: float = 45.0, retry_every: float = 300.0,
                                       tries: int = 12) -> None:
    """Startup task: retire_preference_graph once the graph has had time to
    connect, retried while the graph cannot be reached. Bounded; never raises.
    Until it has run, agents' graph search hides those items anyway."""
    import asyncio
    try:
        await asyncio.sleep(first_delay)
        for _ in range(max(1, tries)):
            res = await retire_preference_graph()
            if res is not None and not res.get("failed"):
                return
            await asyncio.sleep(retry_every)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("preference graph clean-up task stopped")


# ── preferences stored before scopes existed ─────────────────────────────

_PERSON_SOURCE = re.compile(r"^person:(\d+)$")


async def _legacy_scope(conn, row, owner_pid: int | None) -> str:
    """The scope of one preference stored before Quick 020. First match wins;
    the result only ever narrows who it reaches (before, everyone)."""
    src = str(row["source"] or "").strip()
    thread = str(row["thread_id"] or "").strip()
    if src in ("dashboard", "owner_number") or _is_web(thread):
        return OWNER
    m = _PERSON_SOURCE.match(src)
    if m:
        pid = int(m.group(1))
        if owner_pid is not None and pid == owner_pid:
            return OWNER
        numbers, _name = await _person_numbers(conn, pid)
        if numbers:
            return f"wa:{numbers[0]}"
    if thread:
        ident = await identify(thread)
        if ident.kind == "owner":
            return OWNER
        if ident.scope:
            return ident.scope            # person 'wa:', group 'chat:', sender 'wa:'/'lid:'
    return OWNER                          # no thread, or a non-phone one (inbox)


async def migrate_preference_scopes() -> dict:
    """Give every preference stored before Quick 020 a scope (startup, after
    config overrides so owner_number is known). Touches only preferences whose
    scope is NULL, so a second run changes nothing. Never raises."""
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT id, source, thread_id FROM learnings "
            "WHERE kind = 'preference' AND scope IS NULL ORDER BY id")
        if not rows:
            return {"scoped": 0}
        counts: dict[str, int] = {}
        async with pool.acquire() as conn:
            _nums, owner_pid = await owner_numbers(conn)
            for r in rows:
                scope = await _legacy_scope(conn, r, owner_pid)
                await conn.execute(
                    "UPDATE learnings SET scope = $2 WHERE id = $1 AND scope IS NULL",
                    r["id"], scope)
                key = scope.split(":", 1)[0]
                counts[key] = counts.get(key, 0) + 1
        log.info("memory scope: %d preference(s) stored before scopes now scoped (%s)",
                 len(rows), ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return {"scoped": len(rows), **counts}
    except Exception as e:
        log.exception("memory scope migration failed (retried next start; until then "
                      "unscoped preferences count as the owner's)")
        return {"scoped": 0, "error": type(e).__name__}


# ── the Memory page: scope in words ──────────────────────────────────────

async def describe(rows: list[dict]) -> list[dict]:
    """Add scope_kind ('owner' | 'person' | 'group' | 'global') and scope_label
    to learning rows, names resolved now (people and chats are human-edited).
    Never raises: a failed lookup leaves the ids as labels."""
    wa, lids, chats = set(), set(), set()
    for r in rows:
        s = r.get("scope") or ""
        if s.startswith("wa:"):
            wa.add(s[3:])
        elif s.startswith("lid:"):
            lids.add(s[4:])
        elif s.startswith("chat:"):
            chats.add(s[5:])
    names: dict[str, str] = {}
    lid_phone: dict[str, str] = {}
    subjects: dict[str, str] = {}
    try:
        if wa or lids or chats:
            pool = await get_pool()
            if lids:
                for x in await pool.fetch(
                        "SELECT lid, phone FROM wa_lid_map WHERE lid = ANY($1::text[])",
                        list(lids)):
                    if x["phone"]:
                        lid_phone[x["lid"]] = x["phone"]
                        wa.add(x["phone"])
            if wa:
                for p in await pool.fetch(
                        """SELECT name, whatsapp, whatsapp_alt FROM people
                           WHERE whatsapp = ANY($1::text[]) OR whatsapp_alt && $1::text[]""",
                        list(wa)):
                    for n in [p["whatsapp"], *(p["whatsapp_alt"] or [])]:
                        if n and p["name"]:
                            names.setdefault(n, p["name"])
            if chats:
                for c in await pool.fetch(
                        "SELECT jid, subject FROM chats WHERE jid = ANY($1::text[])",
                        list(chats)):
                    if c["subject"]:
                        subjects[c["jid"]] = c["subject"]
    except Exception:
        log.debug("scope labels: lookup failed (ids shown)", exc_info=True)
    out = []
    for r in rows:
        s = r.get("scope")
        if not s:
            kind, label = (("owner", "Owner") if r.get("kind") == "preference"
                           else ("global", "Everyone"))
        elif s == OWNER:
            kind, label = "owner", "Owner"
        elif s.startswith("wa:"):
            kind, label = "person", names.get(s[3:]) or f"+{s[3:]}"
        elif s.startswith("lid:"):
            phone = lid_phone.get(s[4:])
            kind = "person"
            label = (names.get(phone) if phone else None) or (f"+{phone}" if phone
                                                              else f"private ID {s[4:]}")
        elif s.startswith("chat:"):
            kind, label = "group", subjects.get(s[5:]) or "a group"
        else:
            kind, label = "person", s
        out.append({**r, "scope_kind": kind, "scope_label": label})
    return out
