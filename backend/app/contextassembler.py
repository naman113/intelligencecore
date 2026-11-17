"""Context assembly for reactive runs — one module that gathers every memory
source, stamps provenance, dedupes, and fits the result into a fixed token
budget before it becomes the delimited memory message.

Replaces the ad-hoc assembly in worker._build_context. Sources, in DROP
priority (lowest-priority dropped first when the budget overflows):

    group purpose  >  group members  >  open cases  >  lessons (Quick 009)
                   >  group graph facts  >  pgvector learnings  >  global graph facts

The members roster and open cases outrank graph facts deliberately: both are
short lines of verifiable ground truth (the roster synced from the connector;
cases the system's OWN structured state — the cross-channel convergence
point, so a DM run sees what the group fed the case). Graph facts are
re-retrievable.

Within a source, retrieval order is preserved. Every source is fail-soft: a
raising source contributes nothing and the others still render. Returns ""
when nothing survives, so the caller emits no memory message at all (keeps the
prompt cache-stable).

The budget is settings.context_token_budget tokens, approximated as 4 chars
per token — a deliberate overcount-resistant heuristic, not a tokenizer.
"""

import logging
import re

from .config import get_settings

log = logging.getLogger("contextassembler")

# One rendered context line (learning or graph fact) never exceeds this,
# including its "- " prefix and provenance stamp.
LINE_CHARS = 300
# The group purpose is operator-authored prose, not a retrieved line; it gets
# a larger — but still hard — cap.
PURPOSE_CHARS = 500
CHARS_PER_TOKEN = 4

GLOBAL_FACT_LIMIT = 6
GROUP_FACT_LIMIT = 4

# The members line: hard char cap and how many names are listed before the
# rest collapse into "and N more".
MEMBERS_CHARS = 400
MEMBER_NAME_LIMIT = 12

LEARNINGS_HEADER = "Learned from previous conversations (use when relevant):"
# Quick 009: approach lessons from hard runs, recalled for the task and the
# tables it touches, and framed as instructions.
LESSONS_HEADER = "Lessons from earlier runs (follow these):"
LESSON_CHARS = 700
GLOBAL_HEADER = "Relevant business knowledge:"
GROUP_HEADER = "What I know about this group:"
CASES_HEADER = "Open cases (system-tracked; treat as ground truth over memory):"

# Open-cases block: how many cases may render and the per-piece caps.
CASE_LIMIT = 2
CASE_SUMMARY_CHARS = 200
CASE_NOTE_CHARS = 120
CASE_RECENT_NOTES = 2
CASE_EXPECTATION_LIMIT = 3


# ── per-source gathering (each fail-soft) ────────────────────────────────


async def _learnings(msg: str, thread_id: str = "") -> list[dict]:
    """pgvector learnings; the 0.25 score floor lives in retrieve_learnings.
    Quick 020: a preference only for the person this run is for (the run's
    memscope identity); facts, corrections and recipes for everyone."""
    from . import memory, memscope
    try:
        scopes = await memscope.recall_scopes(thread_id)
        return await memory.retrieve_learnings(msg, scopes=scopes)
    except Exception:
        log.debug("learning retrieval failed (non-fatal)", exc_info=True)
        return []


async def _lessons(msg: str) -> list[dict]:
    """Quick 009: up to memory.LESSON_LIMIT approach lessons for this task."""
    try:
        from . import memory
        return await memory.retrieve_lessons(msg)
    except Exception:
        log.debug("lesson retrieval failed (non-fatal)", exc_info=True)
        return []


async def _global_facts(msg: str, sender_name: str | None) -> list[dict]:
    """Global-scope knowledge-graph facts, centered on the sender when we can
    resolve them to a graph node (center-node reranking, Phase B contract #1).

    The center_node_uuid kwarg lands in memoryservice concurrently with this
    module; the kwargs-dict + TypeError fallback keeps this working whether or
    not that change has merged yet."""
    from . import memoryservice
    try:
        kwargs: dict = {"limit": GLOBAL_FACT_LIMIT}
        if sender_name:
            try:
                resolver = getattr(memoryservice, "resolve_person_node", None)
                center = await resolver(sender_name) if resolver else None
            except Exception:
                center = None
            if center:
                kwargs["center_node_uuid"] = center
        try:
            return await memoryservice.memory_graph.search(msg, **kwargs)
        except TypeError:
            if "center_node_uuid" not in kwargs:
                raise
            kwargs.pop("center_node_uuid")
            return await memoryservice.memory_graph.search(msg, **kwargs)
    except Exception:
        log.debug("graph retrieval failed (non-fatal)", exc_info=True)
        return []


async def _group_facts(msg: str, thread_id: str) -> list[dict]:
    """Chat-scoped knowledge-graph facts (what onboarding/observation stored
    about this specific conversation)."""
    from . import memoryservice
    try:
        return await memoryservice.memory_graph.search(
            msg, scope=[memoryservice.chat_group(thread_id)],
            limit=GROUP_FACT_LIMIT)
    except Exception:
        log.debug("group fact retrieval failed (non-fatal)", exc_info=True)
        return []


async def _group_purpose(thread_id: str) -> str:
    """What the group is FOR — the one thing the agent cannot infer, because
    the connector only sees messages from now onwards. Stated once during
    onboarding, recalled here. Empty string when unknown."""
    from . import chats
    try:
        row = next((c for c in await chats.list_chats()
                    if c.get("jid") == thread_id), None)
        if row and row.get("purpose"):
            subject = row.get("subject") or thread_id
            return (f'This is the WhatsApp group "{subject}", which is for: '
                    f'{str(row["purpose"])[:PURPOSE_CHARS]}')
    except Exception:
        log.debug("group purpose retrieval failed (non-fatal)", exc_info=True)
    return ""


async def _group_members(thread_id: str) -> str:
    """Who is actually IN this group — ground truth from the synced roster, so
    the agent answers membership questions from data instead of confabulating.
    Empty string when the roster has never synced: absence of the block IS the
    signal that membership is unknown."""
    from . import chats
    try:
        rows = await chats.list_members(thread_id)
    except Exception:
        log.debug("group members retrieval failed (non-fatal)", exc_info=True)
        return ""
    if not rows:
        return ""
    parts = []
    for r in rows[:MEMBER_NAME_LIMIT]:
        label = r["name"] if r.get("name") else f"{r['number']} (not registered)"
        if r.get("is_admin"):
            label += " (admin)"
        parts.append(label)
    line = f"Group members ({len(rows)}): " + ", ".join(parts)
    extra = len(rows) - MEMBER_NAME_LIMIT
    if extra > 0:
        line += f", and {extra} more"
    return line[:MEMBERS_CHARS]


async def _open_cases(msg: str, thread_id: str) -> list[str]:
    """Open cases relevant to this message/thread — the system's own
    structured state, rendered one line-group per case.

    This is what makes cross-channel convergence visible to the reactive
    path: a fact fed into a case from the group (or email, or a db event) is
    readable from a DM about the same subject. Matching is deliberately
    boring: origin-chat match OR full-text match of the message against the
    case's title/summary/linked-event content, recency-ordered, capped at
    CASE_LIMIT. Fail-soft like every other gatherer."""
    from .db import get_pool
    # OR-semantics on purpose: plainto_tsquery ANDs every word, so one word
    # of the question missing from the case text ("due") hides an otherwise
    # perfect match ("TC-006", "invoice", "pending" all present). Any strong
    # term should surface the case; ts_rank puts the best match first.
    import re
    terms = re.findall(r"[A-Za-z0-9][\w-]*", msg)[:24]
    or_query = " OR ".join(terms)
    try:
        # Sandbox quarantine: a test thread sees ONLY sandbox cases and every
        # real thread sees ONLY real ones — same SQL boundary triage enforces.
        # Lazy import; no cycle: triage never imports contextassembler.
        from .triage import _is_test_thread
        is_test = _is_test_thread(thread_id)
        pool = await get_pool()
        rows = await pool.fetch(
            """SELECT id, title, summary, origin_match FROM (
                   SELECT c.id, c.title, c.summary, c.last_activity_at,
                          (c.origin_chat_jid = $2) AS origin_match,
                          to_tsvector('english',
                              -- aliases in the doc: "INV-2041" in a DM must
                              -- surface the case even when the title says
                              -- something else entirely.
                              c.title || ' ' ||
                              array_to_string(c.aliases, ' ') || ' ' ||
                              COALESCE(c.summary, '') || ' ' ||
                              COALESCE((SELECT string_agg(LEFT(e.content, 300), ' ')
                                        FROM (SELECT e2.content
                                              FROM case_events ce
                                              JOIN events e2 ON e2.id = ce.event_id
                                              WHERE ce.case_id = c.id
                                              ORDER BY e2.id DESC LIMIT 8) e), '')
                          ) AS doc
                   FROM cases c
                   WHERE c.status = 'open' AND c.sandbox = $4) s
               WHERE origin_match
                  OR doc @@ websearch_to_tsquery('english', $1)
               ORDER BY origin_match DESC NULLS LAST,
                        ts_rank(doc, websearch_to_tsquery('english', $1)) DESC,
                        last_activity_at DESC
               LIMIT $3""",
            or_query, thread_id, CASE_LIMIT, is_test)
    except Exception:
        log.debug("open-case retrieval failed (non-fatal)", exc_info=True)
        return []
    out: list[str] = []
    for r in rows:
        try:
            pool = await get_pool()
            notes = await pool.fetch(
                """SELECT LEFT(e.content, $2) AS content
                   FROM case_events ce JOIN events e ON e.id = ce.event_id
                   WHERE ce.case_id = $1
                   ORDER BY e.id DESC LIMIT $3""",
                r["id"], CASE_NOTE_CHARS, CASE_RECENT_NOTES)
            exps = await pool.fetch(
                """SELECT e.description, e.due_at, e.status, p.name AS owner, e.owed_by
                   FROM expectations e LEFT JOIN people p ON p.id = e.owner_id
                   WHERE e.case_id = $1 AND e.status IN ('open', 'violated')
                   ORDER BY e.id LIMIT $2""", r["id"], CASE_EXPECTATION_LIMIT)
        except Exception:
            log.debug("case detail retrieval failed (non-fatal)", exc_info=True)
            notes, exps = [], []
        line = f"- Case #{r['id']}: {r['title']}"
        if r["summary"]:
            line += f" — {str(r['summary'])[:CASE_SUMMARY_CHARS]}"
        # Current structured facts (latest value per field) — ahead of the
        # raw recent notes because a superseded amount may still appear in
        # them; the Facts segment is the authoritative current state.
        try:
            from . import cases_util
            facts = await cases_util.current_facts(pool, r["id"], limit=6)
        except Exception:
            log.debug("case facts retrieval failed (non-fatal)", exc_info=True)
            facts = []
        if facts:
            # Provenance is rendered with each fact ("per whatsapp:Vinayak")
            # so the agent reports WHO said a figure instead of presenting a
            # chat claim as system ground truth.
            line += " Facts: " + "; ".join(
                f"{f['field']}={f['value']} (per {f.get('src') or 'unattributed'}, "
                f"{f['observed_at']:%d %b})"
                for f in facts)
        if notes:
            line += " Recent: " + " | ".join(
                str(n["content"]).replace("\n", " ") for n in notes)
        if exps:
            line += " Expectations: " + "; ".join(
                # Quick 024: a request the assistant owes is labelled as such
                f"{e['description'][:80]} ({_exp_owner(e)}"
                + (f", due {e['due_at']:%d %b}" if e["due_at"] else "")
                + (", OVERDUE" if e["status"] == "violated" else "") + ")"
                for e in exps)
        out.append(line)
    return out


def _exp_owner(e) -> str:
    """Who owes an open-case expectation, for the memory message (Quick 024)."""
    if e["owner"]:
        return e["owner"]
    owed_by = e.get("owed_by") if hasattr(e, "get") else None
    return "the assistant" if owed_by == "assistant" else "unassigned"


# ── rendering ────────────────────────────────────────────────────────────


def _learning_line(l: dict) -> str:
    tag = l.get("kind") or "fact"
    if l.get("source"):
        tag += f", from {l['source']}"
    return f"- [{tag}] {l.get('content') or ''}"[:LINE_CHARS]


def _fact_key(f: dict) -> str:
    """Dedupe on the bare fact text, so differing date stamps can't defeat it."""
    return str(f.get("fact") or "").strip().lower()


def _render(kept: dict[str, list[str]]) -> str:
    """Display order mirrors the historical worker layout; DROP priority is a
    separate concern handled by the fill loop in assemble()."""
    parts: list[str] = []
    if kept.get("lessons"):
        parts.append(LESSONS_HEADER + "\n" + "\n".join(kept["lessons"]))
    if kept["learnings"]:
        parts.append(LEARNINGS_HEADER + "\n" + "\n".join(kept["learnings"]))
    if kept["global"]:
        parts.append(GLOBAL_HEADER + "\n" + "\n".join(kept["global"]))
    if kept["purpose"]:
        parts.append(kept["purpose"][0])
    if kept["members"]:
        parts.append(kept["members"][0])
    if kept["cases"]:
        parts.append(CASES_HEADER + "\n" + "\n".join(kept["cases"]))
    if kept["group"]:
        parts.append(GROUP_HEADER + "\n" + "\n".join(kept["group"]))
    return "\n\n".join(parts)


def lessons_block(context_text: str) -> str:
    """Quick 012: the "Lessons from earlier runs" block of an assembled memory
    context, header included ("" when there is none). graph.run hands it to
    the Data Agent, which never sees the supervisor's memory message. Blocks
    are joined with a blank line by _render, so the block ends at the first one."""
    text = context_text or ""
    i = text.find(LESSONS_HEADER)
    if i < 0:
        return ""
    block = text[i:].split("\n\n", 1)[0].strip()
    return block if block != LESSONS_HEADER else ""


# ── entry point ──────────────────────────────────────────────────────────

# Quick 019: one stored provenance line (task_runs.memory_used) never exceeds
# this; the rendered lines are already clipped, case lines can run longer.
USED_TEXT_CHARS = 300
_CASE_ID = re.compile(r"^- Case #(\d+):")


def _used_text(line: str) -> str:
    text = line[2:] if line.startswith("- ") else line
    return text[:USED_TEXT_CHARS]


def _provenance(kept: list[tuple[str, str, dict]], text: str, budget_chars: int,
                dropped: int) -> dict:
    """What reached the model, for task_runs.memory_used (Quick 019): the kept
    items with the ids that name them (learning and lesson ids, graph uuids
    with kind and scope, case ids), the size of the memory message and how many
    candidates the budget left out. Pure."""
    used: dict = {"lessons": [], "learnings": [], "graph": [], "cases": [],
                  "group": {"purpose": False, "members": False},
                  "chars": len(text), "budget_chars": budget_chars, "dropped": dropped}
    for block, line, meta in kept:
        if block in ("purpose", "members"):
            used["group"][block] = True
        elif block == "lessons":
            used["lessons"].append({"id": meta.get("id"), "text": _used_text(line)})
        elif block == "learnings":
            used["learnings"].append({"id": meta.get("id"), "kind": meta.get("kind"),
                                      "source": meta.get("source"),
                                      "text": _used_text(line)})
        elif block in ("group", "global"):
            used["graph"].append({"uuid": meta.get("uuid") or None,
                                  "kind": meta.get("kind") or "fact",
                                  "group_id": meta.get("group_id") or None,
                                  "block": block, "text": _used_text(line)})
        elif block == "cases":
            m = _CASE_ID.match(line)
            used["cases"].append({"id": int(m.group(1)) if m else None,
                                  "text": _used_text(line)})
    return used


async def assemble(msg: str, thread_id: str, sender_name: str | None = None) -> str:
    """Assemble the memory context for one reactive task. Never raises."""
    text, _ = await assemble_with_provenance(msg, thread_id, sender_name)
    return text


async def assemble_with_provenance(msg: str, thread_id: str,
                                   sender_name: str | None = None) -> tuple[str, dict]:
    """assemble() plus what went into it (Quick 019): returns (text, used),
    where used is _provenance()'s record of the items that survived the budget.
    The worker stores it on the run (task_runs.memory_used). Never raises."""
    try:
        budget_tokens = int(getattr(get_settings(), "context_token_budget", 1500))
    except Exception:
        budget_tokens = 1500
    budget_chars = max(0, budget_tokens * CHARS_PER_TOKEN)
    is_group = thread_id.endswith("@g.us")

    learnings = await _learnings(msg, thread_id)  # Quick 020: scoped to this run's person
    lessons = await _lessons(msg)
    global_facts = await _global_facts(msg, sender_name)
    # chat_group-scoped episodes are only ever written for group jids (group
    # onboarding/observation), so a scoped search on a DM thread is a wasted
    # embedding + graph roundtrip into an empty scope — gate it, like purpose.
    group_facts = await _group_facts(msg, thread_id) if is_group else []
    purpose = await _group_purpose(thread_id) if is_group else ""
    members = await _group_members(thread_id) if is_group else ""
    # Cases are NOT gated on is_group: the whole point is that a DM about a
    # subject sees what other channels fed the case.
    cases = await _open_cases(msg, thread_id)

    # Render candidates in DROP-priority order (highest priority first), with
    # cross-source dedupe: group facts are considered before global facts, so
    # a fact known in both scopes survives only in the group block. Learnings
    # dedupe against fact texts too (same content, different store).
    from .memoryservice import fact_line

    seen: set[str] = set()
    candidates: list[tuple[str, str, dict]] = []  # (block, rendered line, provenance)

    if purpose:
        candidates.append(("purpose", purpose, {}))

    if members:
        candidates.append(("members", members, {}))

    for c in cases:
        candidates.append(("cases", c, {}))

    # Lessons outrank retrieved facts: they are what stops a run repeating a
    # timeout or a wrong join (Quick 009).
    for l in lessons:
        key = str(l.get("content") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            candidates.append(("lessons", f"- {l.get('content') or ''}"[:LESSON_CHARS], l))

    for f in group_facts[:GROUP_FACT_LIMIT]:
        key = _fact_key(f)
        if key and key not in seen:
            seen.add(key)
            candidates.append(("group", f"- {fact_line(f)}"[:LINE_CHARS], f))

    for l in learnings:
        key = str(l.get("content") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            candidates.append(("learnings", _learning_line(l), l))

    for f in global_facts[:GLOBAL_FACT_LIMIT]:
        key = _fact_key(f)
        if key and key not in seen:
            seen.add(key)
            candidates.append(("global", f"- {fact_line(f)}"[:LINE_CHARS], f))

    # Greedy fill: add items highest-priority first; the first item that would
    # overflow the budget stops the fill, so everything dropped is of equal or
    # lower priority and within-source order stays stable.
    kept: dict[str, list[str]] = {"purpose": [], "members": [], "cases": [],
                                  "lessons": [], "group": [], "learnings": [],
                                  "global": []}
    kept_items: list[tuple[str, str, dict]] = []
    for block, line, meta in candidates:
        kept[block].append(line)
        if len(_render(kept)) > budget_chars:
            kept[block].pop()
            break
        kept_items.append((block, line, meta))

    text = _render(kept)
    try:
        used = _provenance(kept_items, text, budget_chars,
                           len(candidates) - len(kept_items))
    except Exception:
        log.debug("memory provenance failed (non-fatal)", exc_info=True)
        used = {}
    return text, used
