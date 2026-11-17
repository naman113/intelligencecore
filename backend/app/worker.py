"""Background worker: picks up queued tasks, runs the supervisor, replies."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import asyncpg

_SEND_VERBS = ("sent", "messaged", "forwarded", "notified", "reminded", "pinged")

from . import appchannel, contextassembler, heartbeat, memory, runstatus, sandbox, usage, whatsapp
from .agents import graph
from .budget import BudgetExceeded
from .config import get_settings
from .db import get_pool

log = logging.getLogger("worker")

POLL_INTERVAL = 1.0

# Serializes CLAIMING only (not processing). Claims are single fast queries, so
# contention is negligible; the lock closes the read-then-update race where two
# consumers could both claim tasks for the same thread before either commit is
# visible. NOTE: in-process lock — if you ever run multiple worker *processes*,
# replace with a pg advisory lock keyed on thread_id.
_claim_lock = asyncio.Lock()


# History bounds (A5): per-message character cap and a whole-window cap so one
# giant paste (or 20 of them) can't crowd the prompt out of the budget.
HISTORY_MSG_CHARS = 2000
HISTORY_TOTAL_CHARS = 12000


def _clip(content: str, cap: int = HISTORY_MSG_CHARS) -> str:
    content = content or ""
    return content if len(content) <= cap else content[:cap] + "…"


# Quick 002: the supervisor sees earlier replies as bare text, with no trace of
# the tool calls behind them, so their figures read as something it simply
# knows; in long threads it copied or extended them instead of querying (runs
# 6, 8 and 13 of a client test made no tool call). Each earlier reply that
# carries figures is marked as a past snapshot of tool results.
HISTORY_REPLY_NOTE = ("[Earlier reply. Its figures came from data tools called in that turn "
                      "and are a past snapshot; they are not a data source for a new answer.]\n")


def _note_reply(content: str) -> str:
    if not re.search(r"\d", content or "") or content.startswith(UNVERIFIED_PREFIX):
        return content
    return HISTORY_REPLY_NOTE + content


async def _load_history(conn, thread_id: str, limit: int = 8,
                        message_id: int | None = None) -> tuple[str, list[dict]]:
    """Rolling summary + short recent tail (B3). Returns (summary, history):
    summary is the thread's stored rolling summary ("" when none), history is
    the last `limit` messages oldest-first with the Phase A char caps applied.
    The caller prepends the summary as a synthetic leading user message, so old
    context survives even though the verbatim tail is now short.

    Quick 019: `message_id` is the message this run answers. It is left out
    (the caller passes it separately), and so is every user message newer than
    it: a second Ask message sent while a run works, a burst, or group chatter
    belongs to a later run. Replies sent since stay, so each run sees the
    conversation as it stood when its message arrived plus the replies sent
    since. Without an id the query is the old one."""
    summary = ""
    try:  # fail-soft: a missing column/row must never block the reply path
        summary = await conn.fetchval(
            "SELECT summary FROM threads WHERE thread_id = $1", thread_id) or ""
    except Exception:
        log.debug("thread summary load failed (non-fatal)", exc_info=True)
    rows = await conn.fetch(
        """SELECT role, content FROM messages
           WHERE thread_id = $1 AND role IN ('user', 'assistant')
             AND ($3::bigint IS NULL
                  OR (id <> $3 AND NOT (role = 'user' AND id > $3)))
           ORDER BY id DESC LIMIT $2""",
        thread_id,
        limit,
        message_id,
    )
    history = [{"role": r["role"], "content": _clip(r["content"])} for r in reversed(rows)]
    history = [dict(h, content=_note_reply(h["content"])) if h["role"] == "assistant" else h
               for h in history]  # Quick 002
    total = sum(len(h["content"]) for h in history)
    while history and total > HISTORY_TOTAL_CHARS:  # drop oldest whole messages
        total -= len(history[0]["content"])
        history.pop(0)
    return summary, history


_AFFIRM = {"yes", "y", "yeah", "yep", "keep", "continue", "sure", "ok", "okay", "haan", "ha"}
_DENY = {"no", "n", "nope", "stop", "cancel", "nah", "nahi"}


_IGNORE_WORDS = {"ignore", "no", "skip", "leave it", "not needed", "nope"}


async def _handle_chat_onboarding(thread_id: str, text: str) -> str | None:
    """Interpret a DM from the primary user as the answer to 'what is this group
    about?'. Returns the reply to send, or None if this isn't that.

    Deliberately NOT an LLM call: the question has exactly two outcomes
    (activate with a purpose, or ignore), and a misread here silently switches
    the assistant on in a chat nobody approved.
    """
    if appchannel.is_app_thread(thread_id):  # Phase 8: in-app thread
        return None
    if thread_id.endswith("@g.us"):
        return None                     # the question was asked in a DM
    try:
        from . import chats, memoryservice, people

        pending = await chats.awaiting_answer()
        if not pending:
            return None
        primary = await people.primary_person()
        if not primary or not primary.get("whatsapp"):
            return None
        from .people import normalize_phone
        if normalize_phone(thread_id) != normalize_phone(primary["whatsapp"]):
            return None                 # someone else — not their call to make

        answer = (text or "").strip()
        label = pending.get("subject") or pending["jid"]

        if answer.lower().strip(" .!") in _IGNORE_WORDS:
            await chats.set_status(pending["jid"], "ignored")
            return (f'Understood — I\'ll stay quiet in "{label}". '
                    "You can switch it on later from the dashboard Chats tab.")

        await chats.set_status(pending["jid"], "active", purpose=answer)
        # Seed the group's memory scope so this survives past any one case.
        try:
            await memoryservice.memory_graph.add_episode(
                f'The WhatsApp group "{label}" is for: {answer}',
                source="chat_onboarding",
                group_id=memoryservice.chat_group(pending["jid"]))
        except Exception:
            log.debug("group memory seed failed (non-fatal)", exc_info=True)

        return (f'Got it — now tracking "{label}". I\'ll read what\'s said there '
                "and follow up on commitments, but I'll only reply when someone "
                "@mentions me.")
    except Exception:
        log.exception("chat onboarding handling failed")
        return None


async def _handle_watch_confirmation(pool, thread_id: str, text: str):
    """If a watch for this thread is awaiting a keep-watching answer, resolve it.
    Returns a reply string if handled, else None."""
    watch = await pool.fetchrow(
        """SELECT id, title FROM watches
           WHERE thread_id = $1 AND status = 'awaiting_confirmation'
           ORDER BY last_fired_at DESC LIMIT 1""",
        thread_id)
    if not watch:
        return None

    first = text.strip().lower().split()[0] if text.strip() else ""
    if first in _AFFIRM:
        await pool.execute("UPDATE watches SET status='active' WHERE id=$1", watch["id"])
        return f"👍 Still watching '{watch['title']}'. I'll let you know."
    if first in _DENY:
        await pool.execute("UPDATE watches SET status='done' WHERE id=$1", watch["id"])
        return f"✅ Stopped watching '{watch['title']}'."
    if appchannel.is_app_thread(thread_id):
        # Phase 8: the Ask panel carries unrelated requests; only a bare
        # yes/no answers the confirmation, everything else goes to the agent.
        return None
    # Ambiguous — leave awaiting, nudge once.
    return (f"Just to confirm about '{watch['title']}' — reply YES to keep watching "
            "or NO to stop.")


async def _guard_false_send(run_id: int, reply: str, user_message: str) -> str:
    """Deterministic anti-hallucination guard. The supervisor sometimes narrates
    'Message sent to X' WITHOUT actually calling send_message_to_person — a false
    'done'. We compare the reply against the run's real tool calls: if it claims a
    send to a registered person but no send tool fired, replace it with an honest
    correction (which also stops the false claim from poisoning future history)."""
    if not reply:
        return reply
    low = reply.lower()
    if not any(v in low for v in _SEND_VERBS):
        return reply

    pool = await get_pool()
    people = await pool.fetch("SELECT name, aliases FROM people WHERE active")
    names: list[str] = []
    for p in people:
        names.append((p["name"] or "").lower())
        names += [(a or "").lower() for a in (p["aliases"] or [])]
    names = [n for n in names if n]

    claims_person_send = any(
        f"to {n}" in low or f"sent {n}" in low or f"messaged {n}" in low
        or f"message to {n}" in low or f"notified {n}" in low or f"reminded {n}" in low
        for n in names
    ) or any(x in low for x in ("message sent to", "sent a message to", "forwarded to"))
    if not claims_person_send:
        return reply

    called = {r["tool"] for r in await pool.fetch(
        "SELECT tool FROM tool_calls WHERE task_run_id = $1", run_id)}
    if {"send_message_to_person", "send_document"} & called:
        return reply  # a real send happened — the claim is truthful

    # The model narrated a send it never made. Rather than just apologize, actually
    # perform the send now via a forced single-tool call. If that succeeds, the
    # original "sent" reply becomes true; otherwise fall back to an honest correction.
    log.warning("run %s narrated a send with no send tool-call — forcing the send", run_id)
    if await _force_send(run_id, user_message, reply):
        log.info("run %s: forced send succeeded", run_id)
        return reply
    return ("Correction: I did not actually send that message — it was not dispatched. "
            'Reply "send it" and I\'ll send it right now.')


# ── Phase 12 (AGENT-08): never report a dashboard draft that this run did not create ──
# Single definition; plan 12-09's reflection skip reuses it.
CONTEXT_PACK_TOOLS = frozenset({
    "delegate_to_context_agent", "read_context_pack", "propose_context_pack_items",
    "propose_context_changes", "propose_hide_tables", "propose_kpi_candidates"})

_DRAFT_CLAIM_RES = [re.compile(p, re.I) for p in (
    r"\b(created|saved|built|made|prepared|drafted|generated|put together|set up)\b"
    r"[^.!?\n]{0,60}\bdrafts?\b",
    r"\bdrafts?\b[^.!?\n]{0,80}\b(awaiting|waiting for|ready for|pending)\b"
    r"[^.!?\n]{0,30}\b(approval|review)\b",
    r"\bdashboards?\b[^.!?\n]{0,60}\b(awaiting|waiting for|ready for|pending)\b"
    r"[^.!?\n]{0,30}\b(approval|review)\b",
)]
_NEGATION_RE = re.compile(
    r"\b(no|not|none|never|nothing|didn't|did not|isn't|aren't|wasn't|weren't|"
    r"haven't|hasn't|without)\b", re.I)
_CITE_RE = re.compile(r"#(\d{1,9})")
_FINAL_STATE_RE = re.compile(r"\b(approved|published|is live|now live|went live)\b", re.I)
_SENT_SPLIT_RE = re.compile(r"[.!?\n]+")


def _strip_md(text: str) -> str:
    # a curly apostrophe ("haven’t") must negate exactly like a straight one
    return re.sub(r"[*_~`]", "", text or "").lower().replace("’", "'")


# ── 22 release eval fix-forward (request 12): an honest decline is not a draft claim ──
# "Make the outward bars red": the specialist declined (no colour field) and saved nothing.
# The reply said no new draft was saved, then mentioned the untouched v1 draft still waiting
# for approval; that status line has no #N, so the guard replaced the honest reply with its
# canned text. A reply that says no draft was saved is a decline: its uncited status lines
# about an existing draft pass. A creation claim ("I created a draft") and a status line
# about a "new" draft stay claims, and an offer ("I can put together a draft") never was one.
_NO_DRAFT_NOUN = (r"(?:(?:a|an|any|another|new|second|fresh|separate|dashboard|updated|revised)\s+)"
                  r"{0,4}drafts?\b")
_NO_DRAFT_RES = [re.compile(p) for p in (
    r"\b(?:did not|didn't|have not|haven't|has not|hasn't)\s+(?:\w+\s+)?"
    r"(?:save|saved|create|created|make|made|build|built)\s+" + _NO_DRAFT_NOUN,
    r"\bno\s+" + _NO_DRAFT_NOUN + r"\s+(?:was|were|has been|have been|got)\s+"
    r"(?:saved|created|made|built)\b",
    r"\b(?:saved|created|made|built)\s+no\s+" + _NO_DRAFT_NOUN,
)]
_NEW_WORD_RE = re.compile(r"\bnew\b")


def _declines_draft(text: str) -> bool:
    """The reply says plainly that no draft was saved or created ("I did not save a new draft")."""
    low = _strip_md(text)
    return any(rx.search(low) for rx in _NO_DRAFT_RES)


def _claim_hits(text: str) -> list[tuple[str, bool]]:
    """(sentence, creation) for each sentence holding an un-negated draft claim; creation is
    True when a creation verb matched ("built ... draft"), not only a status ("awaiting approval").
    An offer or question before a creation verb ("I can put together a draft") is not a claim."""
    out = []
    for sent in _SENT_SPLIT_RE.split(_strip_md(text)):
        kinds = [any(not _NEGATION_RE.search(sent[max(0, m.start() - 30):m.end()])
                     and not (i == 0 and _offer_not_claim(sent, m.start()))
                     for m in rx.finditer(sent))
                 for i, rx in enumerate(_DRAFT_CLAIM_RES)]
        if any(kinds):
            out.append((sent, kinds[0]))
    return out
# ── End 22 release eval fix-forward (request 12) ──


def _claim_sentences(text: str) -> list[str]:
    """Sentences (markdown stripped, lowercased) holding an un-negated draft claim. In a reply
    that declines (no draft saved), uncited status lines about an existing draft are not claims."""
    hits = _claim_hits(text)
    if hits and _declines_draft(text):
        hits = [(s, creation) for s, creation in hits
                if creation or _CITE_RE.search(s) or _NEW_WORD_RE.search(s)]
    return [s for s, _ in hits]


def _claims_draft(text: str) -> bool:
    low = _strip_md(text)
    if "dashboard" not in low:
        return False
    return bool(_claim_sentences(text))


async def _guard_false_draft(run_id: int, reply: str, thread_id: str = "") -> str:
    """Replace a reply that claims a dashboard draft this run did not create."""
    if not reply or not _claims_draft(reply):
        return reply
    try:
        pool = await get_pool()
        called = {r["tool"] for r in await pool.fetch(
            "SELECT tool FROM tool_calls WHERE task_run_id = $1", run_id)}
        if called & CONTEXT_PACK_TOOLS:
            return reply
        if await pool.fetchval(
                "SELECT 1 FROM actions WHERE kind = 'publish_dashboard' AND task_run_id = $1",
                run_id):
            return reply
        sentences = _claim_sentences(reply)
        cited = sorted({int(n) for s in sentences for n in _CITE_RE.findall(s)})
        if cited:
            rows = await pool.fetch(
                "SELECT id, status FROM actions WHERE kind = 'publish_dashboard' "
                "AND id = ANY($1::bigint[])", cited)
            status = {r["id"]: r["status"] for r in rows}
            for s in sentences:
                for n in (int(x) for x in _CITE_RE.findall(s)):
                    st = status.get(n)
                    if st == "proposed" or (st == "executed" and _FINAL_STATE_RE.search(s)):
                        return reply
        from . import dashboardtools
        pending = await dashboardtools.pending_dashboard_proposals()
        log.warning("run %s claimed a dashboard draft it did not create", run_id)
        head = "I did not create a new dashboard draft in this turn."
        if pending and not dashboardtools.is_owner_thread(thread_id):
            mid = f" {len(pending)} dashboard proposal(s) are waiting for the owner's approval."
        elif pending:
            lines = "; ".join(f"#{p['action_id']} {p['slug']} v{p['version']}: {p['title']}"
                              for p in pending)
            mid = f" Pending dashboard proposals: {lines}."
        else:
            mid = " There are no dashboard drafts awaiting approval."
        return head + mid + " Ask me again and I will build it now."
    except Exception:
        log.exception("dashboard draft guard failed; reply left unchanged")
        return reply
# ── End Phase 12 (AGENT-08) ──


# ── Phase 12 gap closure (12-VERIFICATION gap 2, CTX-09d): never report a watch this run did not create ──
_WATCH_NOUN = r"(?:watch|watches|alert|alerts|monitor|notification)"
_WATCH_VERB = (r"(?:set up|setup|created|added|scheduled|registered|configured|activated|"
               r"started|put in place|armed)")
_WATCH_CLAIM_RES = [re.compile(p, re.I) for p in (
    rf"\b{_WATCH_VERB}\b[^.!?\n]{{0,60}}\b{_WATCH_NOUN}\b",
    rf"\b{_WATCH_NOUN}\b[^.!?\n]{{0,40}}\b(?:is|are|has been|was)\b\s+"
    r"(?:now\s+)?(?:set up|created|added|scheduled|active|live|in place|tracking|monitoring|watching)\b",
)]
_WATCH_PROMISE_RE = re.compile(
    r"\bi(?:'ll| will)\s+(?:let you know|notify you|alert you|ping you|tell you|message you)", re.I)
_ONE_SHOT_CUE_RE = re.compile(
    r"\b(?:let me know|tell me|notify me|alert me|ping me|message me|inform me|warn me|remind me)\b"
    r"[^.!?\n]{0,40}\b(?:when|if|once|as soon as)\b", re.I)
_STANDING_CUE_RE = re.compile(
    r"\b(?:track|monitor|keep an eye|kpi|every\s+(?:day|week|hour|month)|daily|weekly|hourly|monthly)\b",
    re.I)
_WATCH_CITE_RE = re.compile(r"watch\s*#\s*(\d{1,9})", re.I)
_WATCH_CREATED_RE = re.compile(r"watch #(\d+) created")
_ALREADY_RE = re.compile(r"\b(?:already|earlier|previously|existing|still)\b", re.I)


# ── Quick 021: an offer, a question or a list of capabilities is not a watch claim ──
# Runs 16/17 on the client copy: a reply to "hi" listed "- Set up alerts, or keep track
# of a KPI" under "I can:", _WATCH_CLAIM_RES[0] matched the list item and the guard
# replaced the whole greeting with the correction. A claim says a watch exists now.
# Promises ("I'll let you know ...") stay claims, exactly as before.
_CHUNK_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
_CAPABILITY_RE = re.compile(
    r"\b(?:can|could|may|might|able to|happy to|glad to|ready to|want me to|like me to|"
    r"would you like|shall i|should i|offer to|if you(?:'d)? (?:want|like))\b", re.I)
_LIST_LEAD_RE = re.compile(r"\b(?:for example|e\.g\.|such as|including|options|ideas)\b", re.I)
_DONE_MARKER_RE = re.compile(
    r"\b(?:i've|i have|we've|we have|has been|have been|had been|done|is now|are now)\b", re.I)
_WATCH_TOPIC_RE = re.compile(
    r"\b(?:watch|watches|watching|alert|alerts|monitor|monitoring|notify|notification|"
    r"notifications|remind|reminder|track|tracking|kpi|kpis|let me know|tell me when)\b", re.I)
WATCH_NOTE = "Note: I did not set up a watch in this turn, so nothing is being watched yet."


def _plain(text: str) -> str:
    return _strip_md(text).replace("’", "'").strip()


_SUBJECT_BEFORE_RE = re.compile(r"\b(?:i|we|i just|we just|just|already)\s+$", re.I)
_INVERTED_RE = re.compile(r"\b(?:shall|should|can|could|may|might|do)\s+$", re.I)


def _offer_not_claim(sent: str, start: int) -> bool:
    """A question, or a capability/offer phrase before the verb, with no 'done' marker
    and no "I"/"we" right before the verb ("You can relax, I set up an alert" claims,
    "Shall I set up a watch?" does not)."""
    before = sent[:start]
    subject = _SUBJECT_BEFORE_RE.search(before)
    if _DONE_MARKER_RE.search(sent) or (
            subject and not _INVERTED_RE.search(before[:subject.start()])):
        return False
    return sent.rstrip(" )\"'").endswith("?") or bool(_CAPABILITY_RE.search(before))


def _watch_claims(reply: str, user_message: str = "", promises: bool | None = None):
    """[(line, chunk, sentence)] for every reply sentence holding an un-negated watch
    claim; sentence is markdown-stripped and lowercased (the old return shape)."""
    if promises is None:
        promises = bool(_ONE_SHOT_CUE_RE.search(user_message or ""))
    rxs = list(_WATCH_CLAIM_RES) + ([_WATCH_PROMISE_RE] if promises else [])
    out, in_list = [], False
    for li, line in enumerate((reply or "").split("\n")):
        whole = _plain(line)
        if not whole:
            continue                        # a blank line keeps a list going
        bullet = bool(_BULLET_RE.match(line))
        in_list = in_list and bullet
        for ci, chunk in enumerate(_CHUNK_SPLIT_RE.split(line)):
            sent = _plain(chunk)
            for rx in rxs:
                m = next((m for m in rx.finditer(sent)
                          if not _NEGATION_RE.search(sent[max(0, m.start() - 30):m.end()])), None)
                if m:
                    listed = in_list and not _DONE_MARKER_RE.search(sent)
                    if not listed and not _offer_not_claim(sent, m.start()):
                        out.append((li, ci, sent))
                    break
        if (not bullet and whole.endswith(":") and not _DONE_MARKER_RE.search(whole)
                and (_CAPABILITY_RE.search(whole) or _LIST_LEAD_RE.search(whole))):
            in_list = True                  # "I can:" introduces a list of capabilities
    return out


def _watch_claim_sentences(reply: str, user_message: str = "") -> list[str]:
    """Sentences (markdown stripped, lowercased) holding an un-negated watch-creation claim."""
    return [s for _, _, s in _watch_claims(reply, user_message)]


def _without_watch_claims(reply: str) -> str:
    """The reply minus every sentence that claims a watch or promises a notification."""
    drop = {(li, ci) for li, ci, _ in _watch_claims(reply, promises=True)}
    lines = []
    for li, line in enumerate((reply or "").split("\n")):
        chunks = _CHUNK_SPLIT_RE.split(line)
        kept = [c for ci, c in enumerate(chunks) if (li, ci) not in drop]
        if len(kept) == len(chunks):
            lines.append(line)
            continue
        text = " ".join(kept).rstrip()
        if _plain(_BULLET_RE.sub("", text)):
            lines.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _substantive(text: str) -> bool:
    return len(re.findall(r"[^\W\d_]{2,}", text or "")) >= 3
# ── End Quick 021 (watch claims) ──


async def _watch_state(pool, run_id: int, extra_ids=()):
    """(tools called, created watch rows, existing rows for created+extra ids)."""
    rows = await pool.fetch(
        "SELECT tool, result_digest FROM tool_calls WHERE task_run_id = $1", run_id)
    tools = {r["tool"] for r in rows}
    parsed = []
    for r in rows:
        if r["tool"] == "create_watch":
            parsed += [int(n) for n in _WATCH_CREATED_RE.findall(r["result_digest"] or "")]
    ids = sorted(set(parsed) | set(extra_ids))
    existing = {}
    if ids:
        for w in await pool.fetch(
                "SELECT id, title, cadence_seconds, one_shot, thread_id, status FROM watches "
                "WHERE id = ANY($1::bigint[])", ids):
            existing[w["id"]] = w
    created = [existing[i] for i in sorted(set(parsed)) if i in existing]
    return tools, created, existing


def _watch_lines(created) -> str:
    return "\n".join(
        f"I've set up watch #{w['id']}: '{w['title']}'. It checks every "
        f"{(w['cadence_seconds'] or 0) // 60} minutes and I'll let you know once it happens."
        for w in created)


async def _guard_false_watch(run_id: int, reply: str, user_message: str = "",
                             thread_id: str = "") -> str:
    """Check watch claims against this run's real create_watch calls; repair or correct."""
    try:
        sentences = _watch_claim_sentences(reply, user_message)
        if not sentences:
            return reply
        pool = await get_pool()
        cited = sorted({int(n) for s in sentences for n in _WATCH_CITE_RE.findall(s)})
        tools, created, existing = await _watch_state(pool, run_id, cited)
        if tools & CONTEXT_PACK_TOOLS:
            return reply
        if "propose_scheduled_report" in tools:  # Quick 047: its own action #N is the claim
            return reply
        cids = {w["id"] for w in created}
        if created:
            if set(cited) <= cids:
                return reply
            log.warning("run %s cited a watch it did not create (watch claim)", run_id)
            if len(created) == 1:
                only = created[0]["id"]
                bad = set(cited) - cids
                return _WATCH_CITE_RE.sub(
                    lambda m: f"watch #{only}" if int(m.group(1)) in bad else m.group(0), reply)
            names = ", ".join(f"#{w['id']} ('{w['title']}')" for w in created)
            return reply + f"\n\nCorrection: the watches created in this turn are {names}."
        if (cited and all(i in existing  # Quick 003: an owner's chats share watches
                          and appchannel.same_owner(existing[i]["thread_id"], thread_id)
                          and existing[i]["status"] != "done" for i in cited)
                and any(_ALREADY_RE.search(s) for s in sentences)):
            return reply
        action_ids = [r["id"] for r in await pool.fetch(
            "SELECT id FROM actions WHERE kind = 'create_watch' AND task_run_id = $1 "
            "ORDER BY id", run_id)]
        if "propose_kpi" in tools or action_ids:
            log.warning("run %s claimed a watch but only proposed one (watch claim)", run_id)
            ref = f" as action #{min(action_ids)}" if action_ids else ""
            return ("I did not start a watch in this turn. Your request is waiting for approval"
                    f"{ref} on the Approvals page; nothing runs until you approve it.")
        if (_ONE_SHOT_CUE_RE.search(user_message or "")
                and not _STANDING_CUE_RE.search(user_message or "")):
            await _force_one_shot_watch(run_id, user_message, reply)
            _, created, _ = await _watch_state(pool, run_id)
            if created:
                log.warning("run %s watch claim repaired with %d one-shot watch(es)",
                            run_id, len(created))
                return _watch_lines(created)
        log.warning("run %s watch claim corrected: no watch created", run_id)
        # Quick 021: a message that is not about watches gets its answer back with the
        # claim taken out and a one-line note, instead of an unrelated refusal.
        if not _WATCH_TOPIC_RE.search(user_message or ""):
            kept = _without_watch_claims(reply)
            if _substantive(kept):
                return kept + "\n\n" + WATCH_NOTE
        return ("I did not set up a watch in this turn, so nothing is being watched yet. "
                "Ask me again (for example 'let me know when ...') and I will create it now.")
    except Exception:
        log.exception("watch claim guard failed; reply left unchanged")
        return reply


async def _force_one_shot_watch(run_id: int, user_message: str, intended_reply: str) -> None:
    """Repair: a single-tool agent creates one one-shot watch (no forced tool selection: Claude 5
    rejects it, CTX-09e). Never raises."""
    try:
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent

        from .agents.graph import _callbacks, _make_model, create_watch, effective_configs

        @tool
        async def create_one_shot_watch(title: str, condition: str, cadence_minutes: int = 15) -> str:
            """Create a one-shot watch that alerts once when the condition is met."""
            return await create_watch.ainvoke({"title": title, "condition": condition,
                                               "cadence_minutes": cadence_minutes,
                                               "one_shot": True})

        cfg = (await effective_configs())["supervisor"]
        agent = create_react_agent(
            _make_model(cfg), [create_one_shot_watch],
            prompt=("The user asked to be told once when something happens. Call "
                    "create_one_shot_watch EXACTLY ONCE with a short title and a precise "
                    "condition that names the table, column, filter and threshold from the "
                    "request. Do not reply with prose."))
        task = f"User request: {user_message}\n\nCreate the one-shot watch now."
        await agent.ainvoke({"messages": [("user", task)]},
                            config={"recursion_limit": 4, **_callbacks()})
    except Exception:
        log.exception("forced one-shot watch failed for run %s", run_id)
# ── End Phase 12 gap closure (CTX-09d) ──


# ── Quick 002: a reply that states figures must rest on a tool call in its turn ──
UNVERIFIED_PREFIX = ("_Not checked against data: I wrote this without querying the data in "
                     "this turn, so treat any figures below as unverified. Ask again and I "
                     "will run the query._\n\n")
# ── Quick 021: the re-ask names the message it answers ──
# Run 19 on the client copy: "how many cases are open" was answered from the open-case
# lines of the memory message (no tool lists cases), the re-ask said only "get the
# figures now with run_sql", and the model carried out its own earlier offer (live
# diesel readings) instead. The note now quotes the message and keeps the answer on it.
REASK_QUOTE_CHARS = 300


def _reask_note(user_message: str) -> str:
    q = re.sub(r"\s+", " ", user_message or "").strip()
    if len(q) > REASK_QUOTE_CHARS:
        q = q[:REASK_QUOTE_CHARS] + "…"
    return ("\n\n[Grounding check: your first answer to this message stated figures without "
            "calling a tool in this turn. Answer this message again, and only this message: "
            f"\"{q}\". Do not answer an earlier message, and do not carry out an offer from an "
            "earlier reply unless this message accepts it. Get the figures with the tool that "
            "holds them, in this turn: run_sql (or a delegate) for business data, list_cases for "
            "cases. If no tool holds them, leave them out and say what you could not check. "
            "Figures in earlier messages, the summary or memory are not a source.]")
# ── End Quick 021 (re-ask note) ──
# Tools whose result carries no business data; refused calls never count either.
NO_EVIDENCE_TOOLS = frozenset({"send_progress", "remember_fact", "use_skill", "skill_asset"})
REASK_TIMEOUT_SECONDS = 300
# A number not glued to a letter or '#': "case #19" / "action #431" are system ids.
_FIGURE_RE = re.compile(r"(?<![#\w])\d[\d,]*(?:\.\d+)?")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")


FIGURES_MIN = 2  # one stray number in prose ("top 100") is not a data answer


def _figures(reply: str, user_message: str = "") -> list[str]:
    """Numbers the reply states that the user did not supply (list markers aside)."""
    asked = {m.strip(",") for m in _FIGURE_RE.findall(user_message or "")}
    text = _LIST_MARKER_RE.sub(" ", reply or "")
    return [m.strip(",") for m in _FIGURE_RE.findall(text) if m.strip(",") not in asked]


def _states_figures(reply: str, user_message: str = "") -> bool:
    return len(set(_figures(reply, user_message))) >= FIGURES_MIN


async def _evidence_tools(pool, run_id: int) -> set[str]:
    rows = await pool.fetch("SELECT tool FROM tool_calls WHERE task_run_id = $1", run_id)
    return {r["tool"] for r in rows
            if not r["tool"].endswith(":refused") and r["tool"] not in NO_EVIDENCE_TOOLS}


async def _guard_ungrounded(run_id: int, reply: str, history: list[dict], user_message: str,
                            context_text: str = "") -> str:
    """Re-ask once when a reply states figures but nothing in the turn fetched
    data; if the second attempt is still ungrounded, mark it unverified rather
    than let it reach the owner as fact. Never raises."""
    if not reply or reply.startswith(UNVERIFIED_PREFIX) or not _states_figures(reply, user_message):
        return reply
    try:
        pool = await get_pool()
        if await _evidence_tools(pool, run_id):
            return reply
        log.warning("run %s stated figures with no data tool call; re-asking once", run_id)
        runstatus.note(run_id, runstatus.REASK_STEP)  # Quick 003
        retry = await asyncio.wait_for(
            graph.run(history, user_message + _reask_note(user_message), context_text),
            timeout=REASK_TIMEOUT_SECONDS)
        if await _evidence_tools(pool, run_id) or not _states_figures(retry, user_message):
            return retry
        log.warning("run %s still ungrounded after the re-ask; marking it unverified", run_id)
        return UNVERIFIED_PREFIX + retry
    except Exception:  # BudgetExceeded and timeouts included: the first reply stands, marked
        log.warning("run %s grounding re-ask failed; marking the reply unverified", run_id,
                    exc_info=True)
        return UNVERIFIED_PREFIX + reply
# ── End Quick 002 (grounding guard) ──


# ── Phase 12 (CTX-08): context-pack runs ──
def _skip_reflection_for_pack(tool_log) -> bool:
    """True when the run used a context-pack tool: pack text must not become
    learnings without the owner's approval."""
    return any((r.get("tool") if isinstance(r, dict) else None) in CONTEXT_PACK_TOOLS
               for r in (tool_log or []))


def task_timeout() -> int:
    """Quick 003: the task_timeout_seconds setting, clamped to
    TASK_TIMEOUT_MIN..TASK_TIMEOUT_MAX (an env value skips Setup's range check)."""
    try:
        n = int(get_settings().task_timeout_seconds)
    except (TypeError, ValueError):
        n = TASK_TIMEOUT_SECONDS
    return max(TASK_TIMEOUT_MIN, min(TASK_TIMEOUT_MAX, n))


def _task_timeout(content) -> int:
    """Context-pack upload tasks get context_pack_timeout_seconds (never below
    the task timeout); every other task gets task_timeout()."""
    from .contextpack import CONTEXT_PACK_EXTS
    base = task_timeout()
    text = content or ""
    prefix = "[document received: "
    if isinstance(text, str) and text.startswith(prefix):
        name = text[len(prefix):].split("]", 1)[0].strip().lower()
        if name.endswith(CONTEXT_PACK_EXTS):
            return max(base, int(get_settings().context_pack_timeout_seconds))
    return base
# ── End Phase 12 (CTX-08) ──

# ── Phase 14 (WH-07): kind='sync' task_runs never take the reactive or playbook path ──
SYNC_RUNNER_MISSING = "sync runner not installed"


def _sync_timeout() -> int:
    # M2 sync: the floor is the live task_timeout_seconds setting (Quick 003), not the default
    return max(task_timeout(), int(get_settings().sync_task_timeout_seconds))


async def _run_sync_task(task: dict) -> None:
    """Phase 15 runs the sync in sourcesvc.run_sync_task; it never reads messages or runs an agent."""
    from . import sourcesvc
    await sourcesvc.run_sync_task(task)
# ── End Phase 14 (WH-07) ──


async def _force_send(run_id: int, user_message: str, intended_reply: str) -> bool:
    """Deterministic fallback: build a single-tool agent that MUST call
    send_message_to_person, run it, and report whether a send actually executed
    (verified via the actions row it writes). Never raises."""
    try:
        from langgraph.prebuilt import create_react_agent

        from .agents.graph import (_callbacks, _make_model, effective_configs,
                                    send_message_to_person)
        cfg = (await effective_configs())["supervisor"]
        agent = create_react_agent(
            _make_model(cfg), [send_message_to_person],
            prompt=("The user asked you to send/forward/relay a message to another "
                    "person. Call send_message_to_person EXACTLY ONCE with the correct "
                    "registered person and a clear, appropriate message. Do not reply "
                    "with prose — just make the tool call."))
        task = (f"User request: {user_message}\n\n"
                f"What you intended to convey: {intended_reply}\n\nSend it now.")
        await agent.ainvoke({"messages": [("user", task)]},
                            config={"recursion_limit": 4, **_callbacks()})
    except Exception:
        log.exception("forced send failed for run %s", run_id)
        return False
    # Verify: send_message_to_person records an executed action for this run.
    pool = await get_pool()
    ok = await pool.fetchval(
        """SELECT 1 FROM actions WHERE task_run_id = $1 AND kind = 'whatsapp_message'
             AND status = 'executed' AND created_at > now() - interval '2 minutes' LIMIT 1""",
        run_id)
    return bool(ok)


# Context assembly lives in contextassembler.assemble (Phase B) — one module
# that gathers learnings + graph facts + group purpose, dedupes, stamps
# provenance, and fits a token budget. The worker only resolves WHO is asking.


async def _resolve_sender(pool, thread_id: str) -> tuple[int | None, str | None]:
    """Exact-match identity for a DM sender (A7 rule: no fuzzy matching).
    Returns (person_id, name); (None, None) for groups (sender not derivable
    per-message), the dashboard thread (no phone to resolve), or unknown
    numbers. Fail-soft — identity is an enhancement, never a gate on replying."""
    if thread_id == "dashboard" or thread_id.endswith("@g.us") or appchannel.is_app_thread(thread_id):  # Phase 8: in-app thread
        return None, None
    try:
        from . import people as _people
        async with pool.acquire() as conn:
            person_id = await _people.resolve(conn, phone=thread_id)
            if person_id is None:
                return None, None
            name = await conn.fetchval(
                "SELECT name FROM people WHERE id = $1", person_id)
            return person_id, (name or None)
    except Exception:
        log.debug("sender resolution failed (non-fatal)", exc_info=True)
        return None, None


async def _store_memory_used(pool, run_id: int, used: dict | None) -> None:
    """Quick 019: record on the run what its memory message held
    (contextassembler.assemble_with_provenance), before the run starts, so a
    run that fails still shows what it had. Fail-soft: provenance is a record,
    never a gate on replying."""
    if not isinstance(used, dict) or not used:
        return
    try:
        await pool.execute("UPDATE task_runs SET memory_used = $2::jsonb WHERE id = $1",
                           run_id, json.dumps(used, default=str))
    except Exception:
        log.warning("storing memory provenance for run %s failed (non-fatal)",
                    run_id, exc_info=True)


# ── rolling thread summary (B3) ──────────────────────────────────────────

SUMMARY_REFRESH_MESSAGES = 10   # assistant replies between summary refreshes
SUMMARY_MAX_CHARS = 1500
SUMMARY_SRC_MESSAGES = 40       # messages fed to the summarizer
SUMMARY_MSG_CHARS = 300         # per-message clip in the transcript
SUMMARY_TRANSCRIPT_CHARS = 8000  # whole prompt-body clip (mirrors triage._summarize_case)
SUMMARY_HISTORY_PREFIX = ("CONVERSATION SUMMARY SO FAR (context only; figures in it are past "
                          "snapshots, not a data source):\n")

# Quick 002: the summary used to keep the assistant's figures as "facts", which
# turned one invented number into standing context for every later turn.
THREAD_SUMMARY_PROMPT = """You maintain a rolling summary of a WhatsApp conversation \
between a user and an assistant. Update the previous summary with the new events in \
the transcript: keep standing facts, decisions, open requests and their outcomes; \
drop pleasantries and resolved back-and-forth detail. Do not copy business figures \
(amounts, litres, counts, readings) from the assistant's answers: note what was asked \
and which data answered it, because figures are re-queried, never recalled. Plain \
text, no preamble, at most 1500 characters."""


async def _bump_and_maybe_summarize(thread_id: str) -> None:
    """Count one assistant reply against the thread; refresh the rolling summary
    once the counter reaches SUMMARY_REFRESH_MESSAGES. Fail-soft by contract:
    any error is logged and swallowed, and the counter is only reset on a
    successful refresh — so a failed refresh retries on the next reply. An
    empty (but successful) model output instead backs the counter off to half
    the threshold, so that degenerate case retries periodically, not per-reply."""
    try:
        pool = await get_pool()
        n = await pool.fetchval(
            """UPDATE threads SET messages_since_summary =
                   COALESCE(messages_since_summary, 0) + 1
               WHERE thread_id = $1 RETURNING messages_since_summary""",
            thread_id)
        if n is None or n < SUMMARY_REFRESH_MESSAGES:
            return
        with usage.attribute(agent="thread_summary"):  # Quick 005
            await _refresh_thread_summary(pool, thread_id)
    except Exception:
        log.exception("thread summary refresh failed for %s (non-fatal)", thread_id)


async def _refresh_thread_summary(pool, thread_id: str) -> None:
    """Distill the last SUMMARY_SRC_MESSAGES messages plus the previous summary
    into a <=SUMMARY_MAX_CHARS rolling summary, store it, reset the counter.
    Transcript bounds mirror triage._summarize_case (40 msgs, 300 chars each,
    8000-char prompt body); the model is triage's cheap summarization model."""
    from .triage import _model  # triage-model path: anthropic if keyed, else openai

    rows = await pool.fetch(
        """SELECT role, content FROM messages
           WHERE thread_id = $1 AND role IN ('user', 'assistant')
           ORDER BY id DESC LIMIT $2""",
        thread_id, SUMMARY_SRC_MESSAGES)
    if not rows:
        return
    transcript = "\n".join(
        f"{r['role']}: {(r['content'] or '')[:SUMMARY_MSG_CHARS]}"
        for r in reversed(rows))
    prev = await pool.fetchval(
        "SELECT summary FROM threads WHERE thread_id = $1", thread_id) or ""
    body = (f"PREVIOUS SUMMARY:\n{prev or '(none)'}\n\n"
            f"NEW TRANSCRIPT (oldest first):\n{transcript}")[:SUMMARY_TRANSCRIPT_CHARS]
    out = await _model().ainvoke([("system", THREAD_SUMMARY_PROMPT), ("user", body)])
    summary = out.content
    if isinstance(summary, list):  # anthropic content blocks
        summary = " ".join(b.get("text", "") for b in summary if isinstance(b, dict))
    summary = str(summary).strip()[:SUMMARY_MAX_CHARS]
    if not summary:
        # The model yielded nothing (misconfigured cheap model, content-filter
        # refusal). Back the counter off to half the threshold so the retry is
        # periodic — leaving it at the threshold would re-pay one LLM call on
        # every subsequent reply, indefinitely.
        await pool.execute(
            "UPDATE threads SET messages_since_summary = $2 WHERE thread_id = $1",
            thread_id, SUMMARY_REFRESH_MESSAGES // 2)
        return
    await pool.execute(
        "UPDATE threads SET summary = $2, messages_since_summary = 0 WHERE thread_id = $1",
        thread_id, summary)


async def _process_one() -> bool:
    """Claim and process a single queued task. Returns False if nothing claimable.

    Claim rule: oldest queued task whose thread has NO task currently running —
    parallel across users, strictly ordered within a conversation.
    """
    pool = await get_pool()
    async with _claim_lock:
        async with pool.acquire() as conn:
            task = await conn.fetchrow(
                """UPDATE task_runs SET status = 'running', started_at = now()
                   WHERE id = (
                       SELECT t.id FROM task_runs t
                       WHERE t.status = 'queued'
                         AND t.kind <> 'sync' -- Phase 15: sync tasks run only on the sync lane
                         AND NOT EXISTS (
                             SELECT 1 FROM task_runs r
                             WHERE r.thread_id = t.thread_id AND r.status = 'running'
                         )
                       ORDER BY t.id LIMIT 1 FOR UPDATE SKIP LOCKED
                   )
                   RETURNING id, thread_id, message_id, kind, playbook_id, case_id, expectation_id"""
            )
            if task is None:
                return False

    runstatus.start(task["id"])  # Quick 003: live step; released by _consume
    # Phase 15: unreachable through the claim; kept as a fallback
    # Phase 14 (WH-07): sync tasks have their own handler, timeout and heartbeat window
    if task["kind"] == "sync":
        heartbeat.busy("worker", _sync_timeout() + 300)
        try:
            await asyncio.wait_for(_run_sync_task(dict(task)), timeout=_sync_timeout())
        except Exception as e:
            log.exception("sync task %s crashed", task["id"])
            await pool.execute(
                "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1 AND status='running'",
                task["id"], f"{type(e).__name__}: {e}")
        return True

    heartbeat.busy("worker", task_timeout() + 300)  # Phase 11: GOLIVE-05 heartbeat (Quick 003: setting)
    # Ambient playbook task: runs the supervisor with tier-gated tools; it manages
    # its own reply/side-effects and its own task_runs status. No user reply.
    if task["kind"] == "playbook":
        from . import playbooks
        from . import memscope  # Quick 020: no person asked for a playbook run
        memscope.ctx_identity.set(None)
        # Quick 005: the daily AI budget pauses background AI work (chat keeps going).
        paused = await usage.background_pause_reason()
        if paused:  # Quick 047: a scheduled report spends no AI, so it is never paused
            from . import reports
            if await reports.is_report_playbook(task["playbook_id"], pool):
                paused = None
        if paused:
            log.warning("playbook task %s not started: %s", task["id"], paused)
            await pool.execute(
                "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 "
                "WHERE id=$1 AND status='running'", task["id"], f"paused: {paused}")
            return True
        usage.set_attribution(feature="exploration", agent="playbook",
                              thread_id=task["thread_id"], run_id=task["id"],
                              task_id=f"playbook:{task['playbook_id']}")
        try:
            await asyncio.wait_for(playbooks.run_playbook_task(dict(task)),
                                   timeout=task_timeout())  # Quick 003: setting
        except Exception as e:
            log.exception("playbook task %s crashed", task["id"])
            await pool.execute(
                "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1 AND status='running'",
                task["id"], f"{type(e).__name__}: {e}")
        return True

    async with pool.acquire() as conn:
        msg = await conn.fetchrow("SELECT content FROM messages WHERE id = $1", task["message_id"])
        # History excludes the triggering message (it's passed separately) and
        # user messages that arrived after it (Quick 019: double texting and
        # group chatter belong to later runs).
        summary, history = await _load_history(conn, task["thread_id"],
                                               message_id=task["message_id"])
        if (task["message_id"] is None and history and msg
                and history[-1]["content"] == _clip(msg["content"])):
            history = history[:-1]  # fallback when the task names no message
        # B3: the rolling summary rides as a synthetic leading user message, so
        # the model sees old context even though the verbatim tail is short.
        # graph.py is untouched — this is just the first history entry.
        if summary:
            history = [{"role": "user",
                        "content": SUMMARY_HISTORY_PREFIX + summary}] + history

    thread_id, run_id = task["thread_id"], task["id"]
    # Quick 021: every send to this thread during the run quotes the message it answers
    # (whatsapp._reply_to); reset in the finally below, so nothing after the run quotes.
    reply_token = whatsapp.set_reply_context(
        await whatsapp.reply_target(pool, task["message_id"], thread_id))
    try:
        graph.ctx_run_id.set(run_id)
        graph.ctx_phone.set(thread_id)
        # Quick 020: who this run is for (memscope.identify, never raises). It
        # decides whether remember_fact is held for approval, where a stated
        # preference applies, which preferences are recalled, and reflection.
        from . import memscope
        identity = await memscope.identify(thread_id, pool=pool)
        memscope.ctx_identity.set(identity)
        usage.set_attribution(feature="chat", thread_id=thread_id, run_id=run_id)  # Quick 005
        # Stamp of THIS run's claim: gates send_document's carried-file
        # provenance note (the note itself keys off sandbox.carry_over's
        # carried record, not this clock — this stamp only says "we are inside
        # a real claimed run", and the note stays silent when it is unset).
        graph.ctx_run_started.set(datetime.now(timezone.utc))
        # Carry the last few runs' files into this run's /work so a follow-up
        # message can still see the drawing/CSV the previous message produced.
        try:
            prev = [r["id"] for r in await pool.fetch(
                """SELECT id FROM task_runs WHERE thread_id = $1 AND id < $2
                   ORDER BY id DESC LIMIT 3""", thread_id, run_id)]
            if prev:
                await asyncio.to_thread(sandbox.carry_over, run_id, prev)
        except Exception:
            log.exception("workspace carry-over failed (continuing with an empty /work)")
        # Clear any ambient playbook context left over from a prior playbook run on
        # THIS consumer task (contextvars persist across loop iterations). Prevents a
        # reactive investigate_case from attaching findings to a stale case.
        from . import playbook_tools as _pt
        _pt.ctx_tier.set(None)
        _pt.ctx_case_id.set(None)
        _pt.ctx_playbook_id.set(None)
        _pt.ctx_expectation_id.set(None)

        # Onboarding short-circuit: the primary user answering "what is this
        # group about?". Handled here rather than by the agent because it is a
        # single stateful question whose answer is configuration, not a task.
        handled = await _handle_chat_onboarding(thread_id, msg["content"])
        if handled is None:
            # Confirmation-loop short-circuit: if a one-shot watch just fired and is
            # awaiting the user's "keep watching?" answer, interpret this reply here
            # instead of routing it to the full agent.
            handled = await _handle_watch_confirmation(pool, thread_id, msg["content"])
        if handled is not None:
            await whatsapp.send_message(thread_id, handled)
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO messages (thread_id, role, content, reply_to) "
                    "VALUES ($1, 'assistant', $2, $3)",  # Quick 021: reply_to
                    thread_id, handled, task["message_id"])
                await conn.execute(
                    "UPDATE task_runs SET status='done', ended_at=now(), result_summary=$2 WHERE id=$1",
                    run_id, handled[:500])
            return True

        # Deterministic progress ping: if the task is still running after 20s and
        # the supervisor hasn't sent its own progress message, send one. Removes
        # the dependence on the model remembering the send_progress prompt rule.
        async def _auto_progress():
            await asyncio.sleep(20)
            try:
                # Guard on BOTH: supervisor already sent progress, or the task is
                # no longer running (failed/done before we woke — a stray "on it"
                # after an error message reads as broken).
                sent = await pool.fetchval(
                    "SELECT 1 FROM tool_calls WHERE task_run_id = $1 AND tool = 'send_progress' LIMIT 1",
                    run_id)
                status = await pool.fetchval(
                    "SELECT status FROM task_runs WHERE id = $1", run_id)
                if not sent and status == "running":
                    await whatsapp.send_message(
                        thread_id, "⏳ On it — this one needs some processing, ~a minute.")
            except Exception:
                log.debug("auto-progress failed (non-fatal)", exc_info=True)

        progress_task = asyncio.create_task(_auto_progress())

        # Resolve the sender once: the name centers graph retrieval on them
        # (contextassembler → resolve_person_node), the id stamps reflection
        # provenance after the reply (A7b).
        sender_person_id, sender_name = await _resolve_sender(pool, thread_id)
        context_text, memory_used = await contextassembler.assemble_with_provenance(
            msg["content"], thread_id, sender_name)
        await _store_memory_used(pool, run_id, memory_used)  # Quick 019
        timeout = _task_timeout(msg["content"])  # Phase 12 (CTX-08); Quick 003: setting
        heartbeat.busy("worker", timeout + 300)  # the busy window follows this task's timeout
        try:
            reply = await asyncio.wait_for(
                graph.run(history, msg["content"], context_text),
                timeout=timeout,
            )
        except BudgetExceeded as e:
            # A4b: the run burned through its token budget mid-flight. Fail it
            # (NO retry — a rerun would burn the same budget again) and tell the
            # user honestly instead of going silent.
            log.warning("task %s aborted: %s", run_id, e)
            await pool.execute(
                "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1",
                run_id, "token budget exceeded")
            apology = (
                "Sorry — that request needed more processing than I allow in one "
                "go. Try narrowing it down or splitting it into smaller questions.")
            await whatsapp.send_message(thread_id, apology)
            # Record the apology in history like every other reply path, so the
            # next turn's context shows the user was already answered.
            await pool.execute(
                "INSERT INTO messages (thread_id, role, content, reply_to) "
                "VALUES ($1, 'assistant', $2, $3)",  # Quick 021: reply_to
                thread_id, apology, task["message_id"])
            return True
        finally:
            progress_task.cancel()
        # Quick 002: figures need a data tool call in this turn (re-ask once, else mark).
        # First, so the claim guards below judge the text that actually goes out.
        reply = await _guard_ungrounded(run_id, reply, history, msg["content"], context_text)
        # Anti-hallucination: don't let a fabricated "message sent to X" go out —
        # verify against real tool calls, and force the send if the model skipped it.
        reply = await _guard_false_send(run_id, reply, msg["content"])
        reply = await _guard_false_draft(run_id, reply, thread_id)  # Phase 12 (AGENT-08)
        reply = await _guard_false_watch(run_id, reply, msg["content"], thread_id)  # Phase 12 gap closure (CTX-09d)
        sent = await whatsapp.send_message(thread_id, reply)

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (thread_id, role, content, reply_to) "
                "VALUES ($1, 'assistant', $2, $3)",  # Quick 021: the answer names its question
                thread_id,
                reply,
                task["message_id"],
            )
            await conn.execute(
                """UPDATE task_runs SET status = 'done', ended_at = now(),
                   result_summary = $2 WHERE id = $1""",
                run_id,
                reply[:500] + ("" if sent or appchannel.is_app_thread(thread_id) else " [WA SEND FAILED]"),  # Phase 8: in-app thread
            )
        log.info("task %s done (sent=%s)", run_id, sent)

        # B3: count this reply toward the thread's rolling summary; refresh at
        # the threshold. Fail-soft inside — never breaks the reply path.
        await _bump_and_maybe_summarize(thread_id)

        # Learning loop: distill and store learnings from this task (post-reply,
        # so it never delays the user). A7b: only reflect when the sender resolved
        # to a registered person (exact match, people.resolve, done once before
        # context assembly) — memory written on an unknown sender's word is
        # unattributable — and stamp the provenance.
        try:
            reflect_source = None
            if thread_id == "dashboard" or appchannel.is_app_thread(thread_id):  # Phase 8: in-app thread
                # Operator surface (/api/test-message, scenario scripts): trusted
                # by construction — there is no phone number to resolve.
                reflect_source = "dashboard"
            elif identity.attributed:
                # Quick 020: one identity (memscope.identify) decides attribution
                # for reflection, remember_fact and recall alike: a registered
                # person ('person:<id>'), or the owner's WhatsApp even without a
                # People row ('owner_number'). Groups, unknown senders and a
                # lookup that failed stay unattributed, so nothing is reflected.
                reflect_source = identity.source
            if reflect_source is None:
                log.debug("skipping reflection for task %s: sender on thread %s "
                          "is not a registered person", run_id, thread_id)
            else:
                tool_log = [dict(r) for r in await pool.fetch(
                    "SELECT tool, args, result_digest FROM tool_calls WHERE task_run_id = $1 ORDER BY id",
                    run_id)]
                if _skip_reflection_for_pack(tool_log):  # Phase 12 (CTX-08)
                    log.info("skipping reflection for task %s: context-pack import "
                             "(proposals only)", run_id)
                elif reply.startswith(UNVERIFIED_PREFIX):  # Quick 002
                    log.info("skipping reflection for task %s: reply not checked "
                             "against data", run_id)
                else:
                    await memory.reflect_and_store(run_id, thread_id, msg["content"],
                                                   reply, tool_log,
                                                   source=reflect_source,
                                                   scope=identity.scope)  # Quick 020
        except Exception:
            log.exception("reflection failed for task %s", run_id)
    except Exception as e:
        log.exception("task %s failed", run_id)
        error = f"{type(e).__name__}: {e}"
        if isinstance(e, TimeoutError):  # Quick 003: a plain cause the Ask panel can show
            error = f"timed out after {_task_timeout(msg['content'] if msg else None)} s"
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_runs SET status = 'failed', ended_at = now(), error = $2 WHERE id = $1",
                run_id,
                error,
            )
        await whatsapp.send_message(thread_id, "Something went wrong with that one — try again or rephrase.")
    finally:
        whatsapp.reset_reply_context(reply_token)  # Quick 021
    return True


TASK_TIMEOUT_SECONDS = 600  # default cap per task; the live value is the task_timeout_seconds setting
TASK_TIMEOUT_MIN, TASK_TIMEOUT_MAX = 120, 7200  # Quick 003: bounds for the setting


# ── Phase 15 (SRC-05): single sync lane ──
SYNC_LANE_MARGIN = 120


def _sync_lane_timeout() -> int:
    """The container timer (_sync_timeout) fires first; the lane timer is the backstop."""
    return _sync_timeout() + SYNC_LANE_MARGIN


async def _process_sync_one() -> bool:
    """Claim and run one queued kind='sync' task. Whatever ends it, its sync_runs row is finished."""
    from . import sourcesvc
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await conn.fetchrow(
            """UPDATE task_runs SET status = 'running', started_at = now()
               WHERE id = (
                   SELECT t.id FROM task_runs t
                   WHERE t.status = 'queued' AND t.kind = 'sync'
                     AND NOT EXISTS (
                         SELECT 1 FROM task_runs r
                         WHERE r.thread_id = t.thread_id AND r.status = 'running'
                     )
                   ORDER BY t.id LIMIT 1 FOR UPDATE SKIP LOCKED
               )
               RETURNING id, thread_id, message_id, kind, playbook_id, case_id, expectation_id""")
    if task is None:
        return False
    heartbeat.busy("sync", _sync_lane_timeout() + 300)
    tid = task["id"]
    err_class = None
    try:
        await asyncio.wait_for(_run_sync_task(dict(task)), timeout=_sync_lane_timeout())
        return True
    except asyncio.TimeoutError:
        log.error("sync task %s hit the lane timeout", tid)
        text, err_class = "sync lane timed out", "sync container timed out"
    except Exception as e:
        log.exception("sync task %s crashed", tid)
        from . import secretbox
        text = (f"{type(e).__name__}: {secretbox.redact(str(e))}")[:300]
        err_class = type(e).__name__
    try:
        await pool.execute(
            "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1 AND status='running'",
            tid, text)
    except Exception as e:
        log.warning("could not fail sync task %s: %s", tid, type(e).__name__)
    try:
        await sourcesvc.fail_open_run(tid, err_class)
    except Exception as e:
        log.warning("could not finalize sync run of task %s: %s", tid, type(e).__name__)
    return True


async def _consume_sync() -> None:
    """The one sync lane: never dies, beats under its own heartbeat name."""
    while True:
        try:
            busy = await _process_sync_one()
            heartbeat.beat("sync", 30)
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("sync lane DB connection blip, retrying: %s", type(e).__name__)
            busy = False
        except Exception:
            log.exception("sync lane loop error")
            busy = False
        if not busy:
            await asyncio.sleep(POLL_INTERVAL)
# ── End Phase 15 ──



async def _consume(consumer_id: int) -> None:
    """One consumer loop: claim → process → repeat. Several run concurrently;
    the claim query guarantees per-thread serial execution."""
    while True:
        try:
            try:
                busy = await _process_one()
            finally:
                runstatus.release_current()  # Quick 003: drop this consumer's live step
            heartbeat.beat("worker", 30)  # Phase 11: GOLIVE-05 heartbeat
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            # Transient DB/network blip (e.g. Railway recycling an idle connection).
            # Not worth a stack trace — just back off and retry.
            log.warning("consumer %s DB connection blip, retrying: %s",
                        consumer_id, type(e).__name__)
            busy = False
        except Exception as e:
            log.exception("consumer %s loop error", consumer_id)
            from . import jobruns
            await jobruns.loop_error("worker", e)  # Quick 008 (never raises)
            busy = False
        if not busy:
            await asyncio.sleep(POLL_INTERVAL)


async def run_forever() -> None:
    concurrency = max(1, get_settings().worker_concurrency)
    log.info("worker started (%s parallel consumer(s))", concurrency)

    # Crash recovery. User tasks are safe to requeue (a re-run just recomputes a
    # reply). Playbook tasks are NOT: they may have already sent an autonomous
    # WhatsApp reminder or inserted a proposed action before the crash, and we
    # have no per-side-effect idempotency — re-running would double-send. So we
    # fail orphaned playbook runs and let the scheduler re-trigger if still due.
    try:
        pool = await get_pool()
        n = await pool.fetchval(
            """WITH r AS (UPDATE task_runs SET status = 'queued', started_at = NULL
               WHERE status = 'running' AND kind = 'user' RETURNING 1) SELECT count(*) FROM r"""
        )
        p = await pool.fetchval(
            """WITH r AS (UPDATE task_runs SET status = 'failed', ended_at = now(),
                 error = 'orphaned playbook run (not auto-retried to avoid double-send)'
               WHERE status = 'running' AND kind = 'playbook' RETURNING 1) SELECT count(*) FROM r"""
        )
        if n or p:
            log.info("orphan recovery: requeued %s user task(s), failed %s playbook run(s)", n, p)
        # Phase 14 (WH-07): a crashed sync is never retried blindly and must not block the 'sync' thread
        sy = await pool.fetchval(
            """WITH r AS (UPDATE task_runs SET status = 'failed', ended_at = now(),
                 error = 'orphaned sync run (not auto-retried)'
               WHERE status = 'running' AND kind = 'sync' RETURNING 1) SELECT count(*) FROM r"""
        )
        if sy:
            log.info("orphan recovery: failed %s sync run(s)", sy)
    except Exception:
        log.exception("orphan recovery failed")

    # Phase 15: orphaned sync ledger rows and leftover containers (never retried)
    try:
        pool = await get_pool()
        await pool.execute(
            "UPDATE sync_runs SET status='failed', ended_at=now(), error='orphaned sync run' "
            "WHERE status='running' OR (status='queued' AND NOT EXISTS (SELECT 1 FROM task_runs t "
            "WHERE t.id = sync_runs.task_run_id AND t.status = 'queued'))")
    except Exception:
        log.exception("sync ledger recovery failed")
    try:
        from . import syncrunner
        await syncrunner.cleanup_orphans()
    except Exception:
        log.exception("sync container cleanup failed")

    # ContextVars (ctx_run_id/ctx_phone/ctx_run_started) are task-local in asyncio, so each
    # consumer task carries its own run context — no cross-talk between
    # concurrently running agent tasks.
    await asyncio.gather(*(_consume(i) for i in range(concurrency)), _consume_sync())
