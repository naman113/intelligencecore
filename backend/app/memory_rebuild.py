"""Quick 014: rebuild the knowledge graph from its Postgres sources.

The graph is derived data. Its entities and facts come from LLM extraction
over episodes, and every episode comes from something Postgres already keeps:
learnings (fact / preference), closed cases (their distillation), completed
onboarding interviews, and chat setup answers. A rebuild therefore:

  1. archives every episode the graph holds into graph_episode_archive
     (Postgres, additive; survives a crash half-way, so a re-run can finish),
  2. plans what to ingest, from the sources, with today's hygiene rules:
       * learnings: active fact / preference only, the latest revision only
         (a merged learning used to leave one episode per revision); a
         preference that is a one-off request, or a "fact" that only describes
         a dashboard's layout, is superseded with a reason (never deleted),
       * closed non-sandbox cases: cases.distillation when kept, else the
         archived case episode, else a fresh distillation (bounded by
         max_distill); "nothing durable" is skipped and remembered as '';
         stored ONCE, in global, with its "Case #<id>" header,
       * completed business and chat interviews,
       * archived episodes with no other source (e.g. a chat purpose answer),
         except junk ("no durable facts") and legacy case_<id> copies,
     deduped by exact text per scope,
  3. clears our graphs (global and chat_* emptied in place, keeping their
     indexes; legacy case_* graphs deleted),
  4. re-ingests the plan one episode at a time, oldest first, with the
     business entity and relation types, stamping graphed_at / distilled_at,
  5. embeds any episode still without ic_embedding and reports before/after
     counts.

Idempotent: a second run plans the same items and ends with the same graph
(modulo the extraction model's wording). Single-flight with the backfill
(they share its lock). Plumbing only: what to extract is graph_ontology's
prompt text, what to keep is memory.check_learning and the distiller prompt.

Entry points: POST /api/memory/rebuild (background, progress in
GET /api/memory/status under "rebuild") and scripts/graph_rebuild.py (in
process, prints the result; the command a client runs after updating).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from .config import get_settings
from .db import get_pool
from .memoryservice import GLOBAL_GROUP, chat_group, memory_graph

log = logging.getLogger("memory_rebuild")

MAX_DISTILL_DEFAULT = 50
_LEARNING_NAME = re.compile(r"^learning_\d+_r\d+$")
_CASE_NAME = re.compile(r"^case_(\d+)_close(?:_scoped)?$")
_CASE_HEADER = re.compile(r"^Case #\d+(?: \(\"[^\n]*?\"\))?(?:, closed [0-9-]+)?:\s*")

_progress: dict = {"running": False}
_task: asyncio.Task | None = None


def progress() -> dict:
    """The current or last rebuild (for GET /api/memory/status)."""
    return json.loads(json.dumps(_progress, default=str))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def strip_case_header(text: str) -> str:
    """The distillation inside a case episode written with its header."""
    return _CASE_HEADER.sub("", (text or "").strip(), count=1).strip()


def is_junk_episode(name: str, source: str, content: str) -> bool:
    """An archived episode that must not come back: an empty case
    distillation ("There are no durable facts to extract ..."). Pure."""
    from .memory_pipeline import is_empty_distillation
    if source == "case_close" or _CASE_NAME.match(name or ""):
        return is_empty_distillation(strip_case_header(content))
    return not (content or "").strip()


def _item(group: str, name: str, source: str, text: str, when, ref: tuple) -> dict:
    return {"group": group, "name": name, "source": source, "text": text.strip(),
            "when": _parse_ts(when), "ref": ref}


# ── 1. archive ──────────────────────────────────────────────────────────────

async def archive_episodes(pool) -> int:
    """Copy every episode in our graphs into graph_episode_archive (one row
    per scope and exact text). Returns rows newly archived."""
    added = 0
    for gid in await memory_graph.scope_graphs() or []:
        rows = await memory_graph.read_episodes(gid, limit=10_000)
        if rows is None:
            raise RuntimeError(f"could not read the episodes of graph {gid}")
        for r in rows:
            content = str(r.get("content") or "").strip()
            if not content:
                continue
            status = await pool.execute(
                """INSERT INTO graph_episode_archive
                       (group_id, name, source, content, content_md5, valid_at)
                   VALUES ($1, $2, $3, $4, md5($4), $5)
                   ON CONFLICT (group_id, content_md5) DO NOTHING""",
                gid, str(r.get("name") or ""), str(r.get("source") or ""), content,
                _parse_ts(r.get("valid_at")))
            if str(status).endswith(" 1"):
                added += 1
    return added


# ── 2. plan ─────────────────────────────────────────────────────────────────

async def _archived(pool) -> list:
    return await pool.fetch(
        """SELECT id, group_id, name, source, content, valid_at
             FROM graph_episode_archive ORDER BY valid_at NULLS LAST, id""")


async def build_plan(pool, *, max_distill: int = MAX_DISTILL_DEFAULT,
                     distill: bool = True) -> dict:
    """What a rebuild would ingest, and what it would retire. Reads only,
    except the distillation LLM calls for closed cases that have no kept
    text (at most max_distill; distill=False makes none)."""
    from . import memory, memory_pipeline, onboarding

    items: list[dict] = []
    retire: list[dict] = []          # learnings to supersede: {"id", "reason"}
    notes = {"cases_nothing_durable": 0, "cases_distilled": 0,
             "cases_left_for_backfill": 0, "junk_dropped": 0,
             "legacy_case_copies_dropped": 0, "learning_episodes_dropped": 0,
             "learnings_kept_out": 0}
    archived = await _archived(pool)
    # Quick 038: facts kept out of the graph stay out of a rebuild while the
    # setting is on, so a rebuilt graph matches the one live work builds.
    keep_out = bool(getattr(get_settings(), "graph_skip_low_value_turns", True))

    # learnings
    for r in await pool.fetch(
            """SELECT id, kind, content, revisions, updated_at, graph_skip FROM learnings
                WHERE status = 'active' AND kind IN ('fact', 'preference')
                ORDER BY updated_at, id"""):
        content = str(r["content"] or "").strip()
        problem = (memory.preference_problem(content) if r["kind"] == "preference"
                   else memory.layout_fact_problem(content))
        if problem:
            retire.append({"id": r["id"], "kind": r["kind"], "reason": problem,
                           "content": content[:200]})
            continue
        if r["kind"] not in memory.GRAPHED_KINDS:
            # Quick 020: a preference is personal (learnings.scope); it is still
            # checked above, but it no longer goes into the shared graph, so a
            # rebuild also removes the preference episodes earlier versions wrote.
            continue
        if keep_out and r.get("graph_skip"):              # Quick 038
            notes["learnings_kept_out"] += 1
            continue
        items.append(_item(GLOBAL_GROUP, memory.learning_episode_name(r["id"], r["revisions"]),
                           f"reflection:{r['kind']}", content, r["updated_at"],
                           ("learning", r["id"])))

    # closed cases, once each, in global
    archived_cases: dict[int, str] = {}
    for a in archived:
        m = _CASE_NAME.match(a["name"] or "")
        if m and not is_junk_episode(a["name"], a["source"], a["content"]):
            archived_cases.setdefault(int(m.group(1)), strip_case_header(a["content"]))
    distilled = 0
    for c in await pool.fetch(
            """SELECT id, title, closed_at, distillation FROM cases
                WHERE status = 'closed' AND NOT sandbox
                ORDER BY closed_at NULLS LAST, id"""):
        cid = int(c["id"])
        text = c["distillation"]
        if text is None:
            text = archived_cases.get(cid)
        if text is None:
            if not distill or distilled >= max_distill:
                notes["cases_left_for_backfill"] += 1
                continue
            distilled += 1
            text = await memory_pipeline.distill_text(cid)
            if text is None:
                notes["cases_left_for_backfill"] += 1
                continue
            notes["cases_distilled"] += 1
        if not text or memory_pipeline.is_empty_distillation(text):
            notes["cases_nothing_durable"] += 1
            items.append({"group": GLOBAL_GROUP, "name": "", "source": "case_close",
                          "text": "", "when": None, "ref": ("case_empty", cid)})
            continue
        items.append(_item(
            GLOBAL_GROUP, memory_pipeline.case_episode_name(cid), "case_close",
            memory_pipeline.case_episode_text(cid, c["title"], text, c["closed_at"]),
            c["closed_at"], ("case", cid, text)))

    # completed interviews
    business = 0
    for o in await pool.fetch(
            """SELECT id, kind, subject_ref, profile, COALESCE(applied_at, updated_at) AS at
                 FROM onboarding_sessions
                WHERE status = 'complete' AND profile IS NOT NULL ORDER BY at, id"""):
        profile = o["profile"]
        if isinstance(profile, str):
            try:
                profile = json.loads(profile)
            except ValueError:
                continue
        if not isinstance(profile, dict):
            continue
        if o["kind"] == "business":
            text, _ = onboarding.business_episode_text(profile)
            if text.strip():
                business += 1
                items.append(_item(GLOBAL_GROUP, f"onboarding_business_{o['id']}",
                                   "onboarding", text, o["at"], ("onboarding", o["id"])))
        elif o["subject_ref"]:
            text = onboarding.chat_episode_text(profile)
            if text.strip():
                items.append(_item(chat_group(o["subject_ref"]), f"onboarding_chat_{o['id']}",
                                   "chat_onboarding", text, o["at"], ("onboarding", o["id"])))

    # archived episodes with no other source
    for a in archived:
        name, source, gid = a["name"] or "", a["source"] or "", a["group_id"] or ""
        if gid.startswith("case_"):
            notes["legacy_case_copies_dropped"] += 1
            continue
        if is_junk_episode(name, source, a["content"]):
            notes["junk_dropped"] += 1
            continue
        if _LEARNING_NAME.match(name) or source.startswith("reflection:"):
            continue                                     # re-derived from learnings above
        if _CASE_NAME.match(name) or source == "case_close":
            continue                                     # re-derived from cases above
        if source == "onboarding" and business:
            continue                                     # re-derived from onboarding above
        items.append(_item(gid, name, source or "archive", a["content"], a["valid_at"],
                           ("archive", a["id"])))

    # dedupe by exact text per scope (first one wins), oldest first
    seen: set[tuple[str, str]] = set()
    plan: list[dict] = []
    for it in items:
        if not it["text"]:
            plan.append(it)
            continue
        key = (it["group"], _norm(it["text"]))
        if key in seen:
            continue
        seen.add(key)
        plan.append(it)
    plan.sort(key=lambda it: (it["when"] is None, it["when"] or _now()))
    learning_names = {it["name"] for it in plan if it["ref"][0] == "learning"}
    notes["learning_episodes_dropped"] = sum(
        1 for a in archived
        if _LEARNING_NAME.match(a["name"] or "") and a["name"] not in learning_names)
    return {"items": plan, "retire": retire, "notes": notes,
            "archived_rows": len(archived)}


def plan_summary(plan: dict) -> dict:
    by_source: dict[str, int] = {}
    for it in plan["items"]:
        if it["text"]:
            by_source[it["source"]] = by_source.get(it["source"], 0) + 1
    return {"episodes": sum(by_source.values()), "by_source": by_source,
            "retire": plan["retire"], "notes": plan["notes"],
            "archived_rows": plan["archived_rows"]}


# ── 3-5. clear, ingest, report ──────────────────────────────────────────────

async def _stamp(pool, item: dict) -> None:
    kind = item["ref"][0]
    try:
        if kind == "learning":
            await pool.execute(  # Quick 038: in the graph now, so no longer kept out
                "UPDATE learnings SET graphed_at = now(), graph_skip = NULL WHERE id = $1",
                item["ref"][1])
        elif kind == "case":
            await pool.execute(
                "UPDATE cases SET distilled_at = now(), distillation = $2 WHERE id = $1",
                item["ref"][1], item["ref"][2])
        elif kind == "case_empty":
            await pool.execute(
                "UPDATE cases SET distilled_at = COALESCE(distilled_at, now()), "
                "distillation = '' WHERE id = $1", item["ref"][1])
    except Exception:
        log.debug("stamp after rebuild failed for %s", item["ref"], exc_info=True)


async def rebuild_graph(*, dry_run: bool = False, max_distill: int = MAX_DISTILL_DEFAULT,
                        trigger: str = "operator") -> dict:
    """Rebuild the graph (see the module docstring). dry_run archives and
    plans (it may distill cases that have no kept text) but changes neither
    the graph nor the learnings. Never raises; the result says what
    happened ("stopped" names why it ended early)."""
    from . import jobruns
    from .memory_pipeline import _backfill_lock

    result: dict = {"dry_run": dry_run, "stopped": None, "before": None, "after": None,
                    "plan": None, "ingested": 0, "already": 0, "failed": 0,
                    "superseded_learnings": 0, "embedded": 0, "cleared": [], "dropped": []}
    if _backfill_lock.locked():
        return {**result, "stopped": "a backfill or rebuild is already running"}
    async with _backfill_lock:
        _progress.clear()
        _progress.update(running=True, phase="starting", started_at=_now().isoformat(),
                         finished_at=None, dry_run=dry_run, total=0, done=0, failed=0)
        try:
            async with jobruns.track("memory_graph", "Graph rebuild", trigger=trigger,
                                     ref=("graph", "rebuild"), live=True) as job:
                await _run(result, dry_run=dry_run, max_distill=max_distill)
                counts = {"ingested": result["ingested"], "failed": result["failed"]}
                if result["stopped"]:
                    job.fail(result["stopped"], counts=counts)
                else:
                    job.ok(f"{result['ingested']} episodes re-ingested"
                           + (" (dry run)" if dry_run else ""), counts)
        except Exception as e:
            log.exception("graph rebuild failed")
            result["stopped"] = result["stopped"] or f"error: {type(e).__name__}: {e}"[:300]
        finally:
            _progress.update(running=False, phase="done", finished_at=_now().isoformat(),
                             result=dict(result))
            log.info("graph rebuild: %s", {k: v for k, v in result.items() if k != "plan"})
    return result


async def _run(result: dict, *, dry_run: bool, max_distill: int) -> None:
    if not await memory_graph._ensure():
        result["stopped"] = "graph not connected"
        return
    pool = await get_pool()
    result["before"] = await memory_graph.graph_counts()
    _progress.update(phase="archiving")
    result["archived_new"] = await archive_episodes(pool)
    _progress.update(phase="planning")
    plan = await build_plan(pool, max_distill=max_distill)
    result["plan"] = plan_summary(plan)
    if dry_run:
        return

    for r in plan["retire"]:
        n = await pool.execute(
            """UPDATE learnings SET status = 'superseded', status_reason = $2,
                   updated_at = now()
               WHERE id = $1 AND status = 'active'""", r["id"], r["reason"])
        if str(n).endswith(" 1"):
            result["superseded_learnings"] += 1

    _progress.update(phase="clearing")
    for gid in await memory_graph.scope_graphs() or []:
        if gid.startswith("case_"):
            if await memory_graph.drop_scope(gid):
                result["dropped"].append(gid)
        elif await memory_graph.clear_scope(gid):
            result["cleared"].append(gid)
        else:
            result["stopped"] = f"could not clear graph {gid}"
            return

    todo = [it for it in plan["items"] if it["text"]]
    _progress.update(phase="ingesting", total=len(todo), done=0, failed=0)
    for it in plan["items"]:
        if not it["text"]:
            await _stamp(pool, it)                       # nothing durable: remembered
            continue
        ok = await memory_graph.add_episode(
            it["text"], source=it["source"], occurred_at=it["when"],
            group_id=it["group"], name=it["name"] or None)
        if ok:
            result["ingested"] += 1
            await _stamp(pool, it)
        else:
            result["failed"] += 1
            if not memory_graph._available:
                result["stopped"] = "graph went away during the rebuild; run it again"
                break
        _progress.update(done=result["ingested"], failed=result["failed"])

    _progress.update(phase="embedding")
    result["embedded"] = await memory_graph.embed_missing_episodes(limit=1000)
    result["after"] = await memory_graph.graph_counts()


def start_rebuild(*, dry_run: bool = False, max_distill: int = MAX_DISTILL_DEFAULT) -> bool:
    """Start rebuild_graph in the background; False when a backfill or a
    rebuild is already running."""
    global _task
    from .memory_pipeline import _backfill_lock
    if _backfill_lock.locked() or (_task is not None and not _task.done()):
        return False
    _task = asyncio.create_task(rebuild_graph(dry_run=dry_run, max_distill=max_distill))
    return True
