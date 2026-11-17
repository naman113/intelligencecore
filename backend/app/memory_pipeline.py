"""Memory pipeline (Phase 4) — turn finished work into durable knowledge.

Entry points:
  * distill_case(case_id): on user-acknowledged close, distill what happened into
    the knowledge graph (what was asked, who delivered, durable facts such as
    "Priya handles Mumbai expenses"). Retrieval later surfaces these into context.
  * backfill_graph(limit) / start_backfill(limit) (Quick 009): send what never
    reached the graph (active fact/preference learnings, closed cases never
    distilled) once it is connected. Idempotent and bounded; the nightly job
    runs a small batch so knowledge written while the graph was down arrives
    on its own.
  * propose_playbooks(): a weekly review of closed cases for repeating patterns;
    when found, insert a playbooks row as status='draft', proposed_by='system' and
    notify the operator. Never auto-activates (plan §8.5).

Both are best-effort and fail-soft: the graph being down or the LLM erroring must
never break case closing or the scheduler.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from .config import get_settings
from .db import get_pool
from .memoryservice import GLOBAL_GROUP, memory_graph

log = logging.getLogger("memory_pipeline")


DISTILL_PROMPT = """You distill a closed operational case into durable business \
knowledge for a company's memory. Extract only facts that will still be true and \
useful weeks from now — who owns what, who reliably delivers, standing arrangements, \
vendor/person relationships. Ignore one-off logistics.

Return a compact prose paragraph (3-5 sentences) stating those durable facts \
explicitly by name (e.g. "Priya handles Mumbai expense reporting and submitted on \
time."). Name the machines, sites, measures, rules, tables and people involved \
exactly as the case does, so this links to what is already known about them. \
No preamble, no bullet points.

If the case holds nothing durable, answer exactly NONE and nothing else. Never \
write a sentence saying there is nothing to extract."""

# Quick 014: a distillation that says there is nothing to keep is not
# knowledge. The client's graph held "There are no durable facts to extract
# from the provided case..." as an episode. The prompt now answers NONE; this
# also catches the prose forms an older prompt (or a model) still writes.
_KNOWLEDGE = r"(?:business\s+)?(?:facts?|knowledge|information|learnings?|insights?)"
_EMPTY_FIRST_SENTENCE = re.compile(
    r"\b(?:no|not\s+any)\s+(?:durable|lasting|long[- ]term|new|useful|relevant)\s+" + _KNOWLEDGE
    + r"|\b(?:no|not\s+any)\s+" + _KNOWLEDGE + r"\s+(?:to|worth|that\s+(?:can|could|would))\b"
    + r"|\bnothing\s+(?:durable|lasting|of\s+lasting|worth\s+\w+ing|to\s+(?:extract|distil+|store|record|keep))\b",
    re.I)
EMPTY_DISTILLATION_MAX_CHARS = 400


def is_empty_distillation(text: str | None) -> bool:
    """True for a distillation that holds no knowledge: empty, NONE, or a
    short text whose first sentence says there is nothing durable to keep.
    Pure."""
    t = (text or "").strip()
    if len(t) < 20:
        return True
    if re.fullmatch(r"[\W_]*none[\W_]*", t, re.I):
        return True
    first = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    return len(t) <= EMPTY_DISTILLATION_MAX_CHARS and bool(_EMPTY_FIRST_SENTENCE.search(first))


def case_episode_name(case_id: int) -> str:
    return f"case_{case_id}_close"


def case_episode_text(case_id: int, title: str | None, distillation: str,
                      closed_at=None) -> str:
    """The episode a closed case becomes. The "Case #<id>" header gives the
    graph a Case entity that links the case to the machines, rules and
    people it was about (Quick 014)."""
    when = ""
    if closed_at is not None:
        when = f", closed {str(closed_at)[:10]}"
    head = f"Case #{case_id}"
    if (title or "").strip():
        head += f' ("{title.strip()}")'
    return f"{head}{when}: {distillation.strip()}"


async def distill_case(case_id: int) -> bool:
    """Distill a case into the knowledge graph. Returns True if an episode
    landed (or the same text was already there)."""
    return await distill_case_outcome(case_id) == "stored"


async def distill_text(case_id: int) -> str | None:
    """The LLM distillation of one case: the text, "" when the case holds
    nothing durable, None when the case is missing or the call failed."""
    try:
        pool = await get_pool()
        case = await pool.fetchrow("SELECT title, summary FROM cases WHERE id = $1", case_id)
        if not case:
            return None
        exps = await pool.fetch(
            """SELECT e.description, e.status, p.name AS owner
               FROM expectations e LEFT JOIN people p ON p.id = e.owner_id
               WHERE e.case_id = $1""", case_id)
        parts = await pool.fetch(
            """SELECT p.name, cp.role FROM case_participants cp JOIN people p ON p.id = cp.person_id
               WHERE cp.case_id = $1""", case_id)

        payload = {
            "title": case["title"], "summary": case["summary"],
            "participants": [{"name": r["name"], "role": r["role"]} for r in parts],
            "expectations": [{"what": r["description"], "status": r["status"], "owner": r["owner"]}
                             for r in exps],
        }
        payload.update(await _finding_context(pool, case_id))  # Quick 006
        context = json.dumps(payload, default=str)[:6000]

        # Quick 022: the memory model (reflection_model, default claude-haiku-5-5);
        # a gpt-* value keeps the pre-022 OpenAI path.
        from . import memory
        distillation = (await memory.memory_text(
            DISTILL_PROMPT, context, ledger_role="memory", max_tokens=400)).strip()
        return "" if is_empty_distillation(distillation) else distillation
    except Exception:
        log.exception("distilling case %s failed", case_id)
        return None


async def _stamp_case(pool, case_id: int, distillation: str) -> None:
    """distilled_at (Quick 009: the backfill skips it) and the distilled text
    (Quick 014: a rebuild re-ingests it without another LLM call; '' means
    nothing durable)."""
    try:
        await pool.execute(
            "UPDATE cases SET distilled_at = now(), distillation = $2 WHERE id = $1",
            case_id, distillation)
    except Exception:
        log.debug("distilled_at stamp failed for case %s", case_id, exc_info=True)


async def store_case_episode(pool, case_id: int, distillation: str) -> bool:
    """Write a case's distillation to the graph once, in the global scope
    (Quick 014: no second copy in a case_<id> graph; global search finds it,
    and its Case entity links it to everything it mentions)."""
    row = await pool.fetchrow("SELECT title, closed_at FROM cases WHERE id = $1", case_id)
    text = case_episode_text(case_id, row["title"] if row else None, distillation,
                             row["closed_at"] if row else None)
    return await memory_graph.add_episode(
        text, source="case_close", group_id=GLOBAL_GROUP,
        occurred_at=row["closed_at"] if row and row["closed_at"] else None,
        name=case_episode_name(case_id))


async def distill_case_outcome(case_id: int) -> str:
    """Distill one closed case into the graph: "stored", "empty" (nothing
    durable: stamped, nothing written, never retried) or "failed"."""
    try:
        pool = await get_pool()
        distillation = await distill_text(case_id)
        if distillation is None:
            return "failed"
        if not distillation:
            log.info("case %s holds nothing durable; nothing written to the graph", case_id)
            await _stamp_case(pool, case_id, "")
            return "empty"
        if not await store_case_episode(pool, case_id, distillation):
            return "failed"
        log.info("distilled case %s into knowledge graph", case_id)
        await _stamp_case(pool, case_id, distillation)
        return "stored"
    except Exception:
        log.exception("distill_case failed for %s", case_id)
        return "failed"


# ── Quick 009: graph backfill ────────────────────────────────────────────────

BACKFILL_DEFAULT = 50
BACKFILL_MAX = 200
NIGHTLY_BACKFILL = 20

# Quick 020: facts only (memory.GRAPHED_KINDS). Preferences are personal and
# stay out of the shared graph; a pending learning is never active, so never sent.
_PENDING_LEARNINGS = """
    FROM learnings
    WHERE status = 'active' AND kind = 'fact'
      AND (graphed_at IS NULL OR graphed_at < updated_at)"""
_PENDING_CASES = """
    FROM cases
    WHERE status = 'closed' AND NOT sandbox AND distilled_at IS NULL"""


def pending_learnings_sql() -> str:
    """The learnings a backfill sends. Quick 038: while graph_skip_low_value_turns
    is on, a fact kept out of the graph (learnings.graph_skip) is not pending;
    switching the setting off lets the backfill send those too."""
    if getattr(get_settings(), "graph_skip_low_value_turns", True):
        return _PENDING_LEARNINGS + " AND graph_skip IS NULL"
    return _PENDING_LEARNINGS


_backfill_lock = asyncio.Lock()
_backfill_task: asyncio.Task | None = None
_backfill_progress: dict = {"running": False}


async def pending_counts() -> dict:
    """What a backfill would send right now. Never raises ({} on error)."""
    try:
        pool = await get_pool()
        return {
            "learnings": int(await pool.fetchval(
                f"SELECT count(*) {pending_learnings_sql()}") or 0),
            "cases": int(await pool.fetchval(f"SELECT count(*) {_PENDING_CASES}") or 0),
        }
    except Exception:
        log.warning("backfill pending count failed", exc_info=True)
        return {}


async def backfill_graph(limit: int = BACKFILL_DEFAULT) -> dict:
    """Send up to `limit` pending items to the knowledge graph: active
    fact/preference learnings first (oldest first), then closed non-sandbox
    cases never distilled. Idempotent: an item is stamped (graphed_at /
    distilled_at) once it lands, and an exact-content or episode-name match
    already in the graph counts as landed without writing again. Stops early
    if the graph goes away. Single-flight: returns {"skipped": "running"}
    while another backfill runs. Never raises."""
    from . import memory

    limit = max(1, min(int(limit or BACKFILL_DEFAULT), BACKFILL_MAX))
    result = {"limit": limit,
              "learnings": {"sent": 0, "already": 0, "failed": 0},
              "cases": {"sent": 0, "already": 0, "failed": 0, "skipped": 0},
              "stopped": None}
    if _backfill_lock.locked():
        return {**result, "skipped": "running"}
    async with _backfill_lock:
        _backfill_progress.clear()
        _backfill_progress.update(running=True, started_at=_now(), finished_at=None,
                                  **result)
        try:
            if not await memory_graph._ensure():
                result["stopped"] = "graph not connected"
                return result
            pool = await get_pool()
            budget = limit
            rows = await pool.fetch(
                f"SELECT id, kind, content, revisions {pending_learnings_sql()} "
                "ORDER BY created_at, id LIMIT $1", budget)
            for r in rows:
                name = memory.learning_episode_name(r["id"], r["revisions"])
                have = await memory_graph.has_episode(GLOBAL_GROUP, name=name,
                                                      content=r["content"])
                if have:
                    await pool.execute(
                        "UPDATE learnings SET graphed_at = now() WHERE id = $1", r["id"])
                    result["learnings"]["already"] += 1
                elif await memory.to_graph(r["id"], r["content"], r["kind"],
                                           r["revisions"], pool=pool):
                    result["learnings"]["sent"] += 1
                else:
                    result["learnings"]["failed"] += 1
                    if not memory_graph._available:
                        result["stopped"] = "graph went away"
                        return result
                budget -= 1
                _backfill_progress.update(learnings=dict(result["learnings"]))
            if budget > 0:
                cases = await pool.fetch(
                    f"SELECT id {_PENDING_CASES} ORDER BY closed_at NULLS LAST, id LIMIT $1",
                    budget)
                for c in cases:
                    cid = c["id"]
                    have = await memory_graph.has_episode(GLOBAL_GROUP,
                                                          name=case_episode_name(cid))
                    outcome = "already" if have else await distill_case_outcome(cid)
                    if outcome == "already":
                        await pool.execute(
                            "UPDATE cases SET distilled_at = now() WHERE id = $1", cid)
                        result["cases"]["already"] += 1
                    elif outcome == "stored":
                        result["cases"]["sent"] += 1
                    elif outcome == "empty":
                        result["cases"]["skipped"] += 1
                    else:
                        result["cases"]["failed"] += 1
                        if not memory_graph._available:
                            result["stopped"] = "graph went away"
                            return result
                    _backfill_progress.update(cases=dict(result["cases"]))
            return result
        except Exception as e:
            log.exception("graph backfill failed")
            result["stopped"] = f"error: {type(e).__name__}"
            return result
        finally:
            _backfill_progress.update(running=False, finished_at=_now(), **result)
            log.info("graph backfill: %s", result)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_backfill(limit: int = BACKFILL_DEFAULT) -> bool:
    """Start backfill_graph in the background (the HTTP request returns at
    once; progress shows in graph_status). False when one is already running."""
    global _backfill_task
    if _backfill_lock.locked() or (_backfill_task is not None and not _backfill_task.done()):
        return False
    _backfill_task = asyncio.create_task(backfill_graph(limit))
    return True


async def graph_status(*, probe: bool = True) -> dict:
    """memory_graph.status() plus what a backfill would send and the last
    backfill's progress — the one payload Memory and Health render."""
    st = await memory_graph.status(probe=probe)
    st["pending"] = await pending_counts()
    st["backfill"] = dict(_backfill_progress)
    try:  # Quick 014: the last rebuild's progress and result
        from . import memory_rebuild
        st["rebuild"] = memory_rebuild.progress()
    except Exception:
        st["rebuild"] = {}
    return st


async def _finding_context(pool, case_id: int) -> dict:
    """Quick 006: a case opened from a data finding (watch alert, insight, chat) has no
    people or commitments, so its knowledge is in its facts, notes and how it ended.
    Empty for ordinary cases, whose distillation input stays unchanged."""
    try:
        from . import cases_util, findingcases
        origin = await pool.fetchval("SELECT origin FROM cases WHERE id = $1", case_id)
        if origin not in findingcases.FINDING_ORIGINS:
            return {}
        notes = await pool.fetch(
            """SELECT ce.note AS kind, LEFT(e.content, 600) AS content, e.occurred_at
                 FROM case_events ce JOIN events e ON e.id = ce.event_id
                WHERE ce.case_id = $1 ORDER BY e.occurred_at DESC, e.id DESC LIMIT 6""",
            case_id)
        return {
            "origin": origin,
            "close_reason": await pool.fetchval(
                "SELECT close_reason FROM cases WHERE id = $1", case_id),
            "facts": [{"field": f["field"], "value": f["value"]}
                      for f in await cases_util.current_facts(pool, case_id)],
            "notes": [{"kind": n["kind"], "when": n["occurred_at"], "text": n["content"]}
                      for n in reversed(list(notes))],
        }
    except Exception:
        log.exception("finding context for case %s failed; distilling without it", case_id)
        return {}


PROPOSE_PROMPT = """You review recently closed operational cases and decide whether a \
REPEATING pattern is worth codifying as a reusable playbook (an automated handler that \
triggers on a condition and pursues a goal). Only propose if you see the SAME kind of \
situation recurring across multiple cases.

Return ONLY JSON: {"propose": <bool>, "name": "<snake_case_name>", \
"goal": "<natural-language goal for the handler>", \
"trigger": {"type": "expectation_violated"|"cron"|"erp_event", ...}, \
"reason": "<why this recurs>"}. If nothing recurs, {"propose": false}."""


def _json_object(text: str | None) -> dict:
    """The first JSON object in a model reply ({} when there is none). Quick 022:
    Anthropic replies may wrap the object in prose or a code fence."""
    t = (text or "").strip()
    try:
        out = json.loads(t)
        return out if isinstance(out, dict) else {}
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        out = json.loads(t[start:end + 1])
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


async def propose_playbooks() -> int:
    """Weekly: review closed cases, propose a draft playbook if a pattern repeats."""
    try:
        pool = await get_pool()
        closed = await pool.fetch(
            # NOT sandbox: repeated E2E scenarios must not look like a
            # recurring business pattern and seed a draft playbook from fiction.
            """SELECT title, summary, close_reason FROM cases
               WHERE status = 'closed' AND NOT sandbox
                 AND closed_at > now() - interval '30 days'
               ORDER BY closed_at DESC LIMIT 40""")
        if len(closed) < 3:
            return 0
        context = json.dumps([{"title": c["title"], "summary": c["summary"]} for c in closed],
                             default=str)[:8000]
        from . import memory  # Quick 022: the memory model, like distillation
        text = await memory.memory_text(PROPOSE_PROMPT, context, ledger_role="memory",
                                        max_tokens=500, json_object=True)
        parsed = _json_object(text)
        if not parsed.get("propose") or not parsed.get("name"):
            return 0

        name = str(parsed["name"]).strip()[:60]
        exists = await pool.fetchval("SELECT 1 FROM playbooks WHERE name = $1", name)
        if exists:
            return 0
        await pool.execute(
            """INSERT INTO playbooks (name, trigger, goal, tool_grants, autonomy_tier,
                   status, proposed_by)
               VALUES ($1, $2::jsonb, $3, $4, 'draft', 'draft', 'system')""",
            name, json.dumps(parsed.get("trigger") or {"type": "expectation_violated"}),
            str(parsed.get("goal") or "")[:1000], ["notify_owner", "propose_action"])
        owner = get_settings().owner_number
        if owner:
            from . import whatsapp
            await whatsapp.send_message(
                owner, f"💡 I noticed a recurring pattern and drafted a playbook '{name}' "
                       f"for your review in the dashboard. Reason: {parsed.get('reason','')[:200]}")
        log.info("proposed system playbook draft '%s'", name)
        return 1
    except Exception:
        log.exception("propose_playbooks failed")
        return 0
