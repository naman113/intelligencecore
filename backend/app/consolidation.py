"""Nightly consolidation + retention (Phase C — the sleep-time job).

Dispatched once per night from the expectation scheduler's cron pass
(expectations._dispatch_cron) at settings.consolidation_hour, local time.

Five independent steps, each individually fail-soft — one failing step must
not stop the others, and run_nightly() itself never raises:

  1. consolidate_learnings — cluster near-duplicate 'active' learnings via
     pgvector cosine distance, LLM-merge each cluster into its OLDEST row,
     and mark the rest status='superseded'. Superseded rows keep their
     content forever; nothing here ever DELETEs a learning. Work is capped
     per night so the job stays bounded.
  2. rebuild_communities — graphiti community rebuild through the fail-soft
     memoryservice wrapper, only when graph_community_rebuild says so
     (Quick 038: off by default, since nothing reads communities).
  3. prune_episodes — remove raw graph episodes older than ~6 months.
     DEFAULT OFF (settings.consolidation_prune_episodes): graphiti's
     remove_episode has a known orphan-cleanup bug (issue #1083).
  4. retention — bounded, batched deletes of old operational DB rows.
     settings.retention_dry_run defaults TRUE: the sweep logs would-delete
     counts and deletes NOTHING until an operator explicitly turns dry-run
     off. Artifact directories on disk are retention.py's job, not ours.
  5. history_retention (Phase 9, KPI-04) — prunes metric_history older than
     400 days and the snapshots of versions retired more than 30 days ago,
     never the current version's. Honors retention_dry_run like step 4.
  6. recipes_superseded (Quick 009) — active recipe learnings whose SQL fails
     memory.recipe_problem (unparseable, not read-only, or reading tables that
     do not exist / the owner hid) become status='superseded' with a
     status_reason. Only on a night the live tenant catalog was read in full;
     never deletes.
  7. graph_backfill (Quick 009) — a small memory_pipeline.backfill_graph batch,
     so learnings and case distillations written while the graph was down
     reach it without anyone running the backfill by hand.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .db import get_pool
from .kpihistory import MAX_DAYS as METRIC_HISTORY_KEEP_DAYS

log = logging.getLogger("consolidation")

# Bounded nightly work.
MAX_CLUSTERS_PER_NIGHT = 20
CLUSTER_MAX_DISTANCE = 0.15          # pgvector cosine distance ≈ 0.85 similarity
RETENTION_BATCH = 5000               # rows per DELETE batch
MESSAGES_KEEP_PER_THREAD = 50        # newest N messages per thread always survive
EPISODE_PRUNE_DAYS = 183             # ~6 months
EPISODE_PRUNE_BATCH = 100


# ── step 1: learning consolidation ─────────────────────────────────────────

async def consolidate_learnings() -> int:
    """Merge clusters of near-duplicate active learnings. Returns clusters merged.

    One SQL per seed row finds its same-kind neighbours within
    CLUSTER_MAX_DISTANCE; already-clustered ids are skipped. The merged text
    (memory.py's merge prompt/model) lands in the cluster's OLDEST row, which
    is re-embedded; every other member becomes status='superseded'. NEVER
    deletes — superseded rows keep their content as the audit trail.
    """
    from . import memory

    pool = await get_pool()
    seeds = await pool.fetch(
        """SELECT id FROM learnings
           WHERE status = 'active' AND embedding IS NOT NULL
           ORDER BY created_at, id""")

    clustered: set[int] = set()
    merged_clusters = 0
    for seed in seeds:
        if merged_clusters >= MAX_CLUSTERS_PER_NIGHT:
            break
        seed_id = seed["id"]
        if seed_id in clustered:
            continue
        neighbours = await pool.fetch(
            """SELECT l2.id, l2.content, l2.created_at
               FROM learnings l1
               JOIN learnings l2 ON l2.kind = l1.kind AND l2.id <> l1.id
                    AND l2.scope IS NOT DISTINCT FROM l1.scope  -- Quick 020: never across people
               WHERE l1.id = $1
                 AND l2.status = 'active' AND l2.embedding IS NOT NULL
                 AND (l1.embedding <=> l2.embedding) <= $2
               ORDER BY l2.created_at, l2.id""",
            seed_id, CLUSTER_MAX_DISTANCE)
        members = [dict(n) for n in neighbours if n["id"] not in clustered]
        if not members:
            clustered.add(seed_id)
            continue

        seed_row = await pool.fetchrow(
            "SELECT id, content, created_at FROM learnings WHERE id = $1", seed_id)
        cluster = sorted([dict(seed_row)] + members,
                         key=lambda r: (r["created_at"], r["id"]))
        keeper, losers = cluster[0], cluster[1:]

        merged = await memory.merge_learning_contents([r["content"] for r in cluster])
        if not (merged or "").strip():
            clustered.add(seed_id)
            continue
        emb = await memory._embed(merged)
        # One transaction: the keeper's merged content and the losers'
        # supersede flip must land together, or a crash in between leaves the
        # merged text active alongside still-active losers (duplicates
        # resurface and get re-merged the next night).
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE learnings SET content = $2, embedding = $3::vector,
                           updated_at = now(), revisions = revisions + 1
                       WHERE id = $1""",
                    keeper["id"], merged, emb)
                # Supersede, never delete: content stays for audit, status hides
                # it from retrieval (memory.retrieve_learnings filters 'active').
                await conn.execute(
                    """UPDATE learnings SET status = 'superseded', updated_at = now()
                       WHERE id = ANY($1::bigint[])""",
                    [r["id"] for r in losers])
        clustered.update(r["id"] for r in cluster)
        merged_clusters += 1
        await _graph_after_merge(pool, keeper["id"], merged, losers)

    if merged_clusters:
        log.info("consolidation: merged %d learning cluster(s)", merged_clusters)
    return merged_clusters


async def _graph_after_merge(pool, keeper_id: int, merged: str, losers: list[dict]) -> None:
    """Quick 019 (audit Fix 5(c)): the knowledge graph follows a merge. The
    keeper's merged text is graphed first, so the facts it still states keep
    a supporting episode; then each superseded loser's own episodes, and what
    only they support, leave the graph. Fail-soft per step: a graph that is
    down leaves the keeper to the backfill (graphed_at < updated_at) and the
    losers to a rebuild (recorded in job_runs)."""
    from . import memory
    try:
        row = await pool.fetchrow(
            "SELECT kind, revisions FROM learnings WHERE id = $1", keeper_id)
        if row is not None and row["kind"] in memory.GRAPHED_KINDS:
            await memory.to_graph(keeper_id, merged, row["kind"], row["revisions"] or 0,
                                  pool=pool)
        for loser in losers:
            await memory.retire_learning_graph(
                loser["id"], kind=row["kind"] if row is not None else None,
                content=loser.get("content"), pool=pool)
    except Exception:
        log.warning("consolidation: graph follow-up for learning %s failed (non-fatal)",
                    keeper_id, exc_info=True)


# ── step 1b: retire hallucinated recipes (Quick 009) ───────────────────────

MAX_RECIPES_CHECKED = 500


async def supersede_bad_recipes() -> int | str:
    """Mark active recipes whose SQL fails the store-time recipe check as
    superseded, with the reason. Conservative: skipped (returns a string)
    unless the live tenant catalog was read in full this time, so a tenant
    outage cannot retire recipes for tables that merely went unlisted.
    Returns the number superseded. Never deletes."""
    from . import memory

    pool = await get_pool()               # raises when the memory DB is down
    rows = await pool.fetch(
        """SELECT id, content FROM learnings
           WHERE status = 'active' AND kind = 'recipe'
           ORDER BY id LIMIT $1""", MAX_RECIPES_CHECKED)
    if not rows:
        return 0
    known = await memory.known_tables(refresh=True)
    if not known.live_complete:
        log.info("consolidation: recipe check skipped (tenant catalog not readable)")
        return "skipped: tenant catalog not readable"
    superseded = 0
    for r in rows:
        problem = memory.recipe_problem(r["content"], known)
        if not problem or problem.startswith("a recipe must be a SQL query"):
            # Prose-only recipes are left alone here: nothing in them can be
            # executed against a table that does not exist.
            continue
        reason = f"recipe check: {problem}"[:500]
        n = await pool.execute(
            """UPDATE learnings SET status = 'superseded', status_reason = $2,
                   updated_at = now()
               WHERE id = $1 AND status = 'active'""", r["id"], reason)
        if _deleted_rows(n):
            superseded += 1
            log.warning("consolidation: superseded recipe %s (%s)", r["id"], problem)
    return superseded


# ── step 2: graph community rebuild ────────────────────────────────────────
# Quick 038: no retrieval path, view, API or prompt reads graph communities
# (memoryservice's search configs carry no community search), and graphiti's
# rebuild writes one model summary per entity in multi-entity clusters plus a
# name per community, on the graph extraction model. So it runs only when
# graph_community_rebuild asks: off (default), weekly (the nightly run on
# COMMUNITY_WEEKDAY, local time) or nightly. Quick 025's bounded clustering is
# untouched. A skipped night returns why, as text.
COMMUNITY_WEEKDAY = 6                    # date.weekday(): Sunday


async def rebuild_communities(now: datetime | None = None) -> bool | str:
    mode = str(getattr(get_settings(), "graph_community_rebuild", "off") or "off").lower()
    if mode not in ("weekly", "nightly"):
        return "off"
    if mode == "weekly":
        if now is None:
            from .expectations import _local_now
            now = _local_now()
        if now.weekday() != COMMUNITY_WEEKDAY:
            return "weekly, not tonight"
    from .memoryservice import memory_graph
    return await memory_graph.build_communities()


# ── step 3: graph episode pruning (default OFF) ────────────────────────────

async def prune_episodes() -> int:
    """Remove raw graph episodes older than ~6 months. Gated behind
    consolidation_prune_episodes (default False — graphiti issue #1083)."""
    if not getattr(get_settings(), "consolidation_prune_episodes", False):
        return 0
    from .memoryservice import memory_graph
    cutoff = datetime.now(timezone.utc) - timedelta(days=EPISODE_PRUNE_DAYS)
    return await memory_graph.remove_episodes_before(cutoff, limit=EPISODE_PRUNE_BATCH)


# ── step 4: retention sweeps ───────────────────────────────────────────────
#
# Each target is a (count_sql, delete_sql) pair sharing one predicate, so
# dry-run reports exactly what a live run would remove. Safety predicates:
#   events    — never touch case-linked events (case_events FK), events an
#               expectation records as its satisfier (satisfied_by FK), or
#               events a case fact cites as its source (case_facts FK):
#               triage happens to co-link fact sources via case_events, but
#               any caller passing source_event_id without that link would
#               otherwise abort the whole batch on the NO ACTION FK.
#   tool_calls— swept BEFORE task_runs (tool_calls.task_run_id FK → task_runs).
#   task_runs — queued/running survive; rows still referenced by tool_calls,
#               artifacts, actions, or learnings.source_run are excluded up
#               front so the FK can't abort a batch.
#   messages  — the newest MESSAGES_KEEP_PER_THREAD rows per thread always
#               survive (history + summary tail); rows referenced by
#               task_runs.message_id are excluded (FK).

_EVENTS_PRED = """
    FROM events e
    WHERE e.occurred_at < $1
      AND e.id NOT IN (SELECT event_id FROM case_events)
      AND NOT EXISTS (SELECT 1 FROM expectations x WHERE x.satisfied_by = e.id)
      AND NOT EXISTS (SELECT 1 FROM case_facts cf WHERE cf.source_event_id = e.id)"""

_TOOL_CALLS_PRED = """
    FROM tool_calls t
    WHERE t.created_at < $1"""

_TASK_RUNS_PRED = """
    FROM task_runs r
    WHERE r.created_at < $1
      AND r.status NOT IN ('queued','running')
      AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.task_run_id = r.id)
      AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.task_run_id = r.id)
      AND NOT EXISTS (SELECT 1 FROM actions ac WHERE ac.task_run_id = r.id)
      AND NOT EXISTS (SELECT 1 FROM learnings l WHERE l.source_run = r.id)
      AND NOT EXISTS (SELECT 1 FROM sync_runs sr WHERE sr.task_run_id = r.id)"""

_WATCH_EVENTS_PRED = """
    FROM watch_events w
    WHERE w.created_at < $1"""

_MESSAGES_PRED = f"""
    FROM (SELECT id, created_at,
                 row_number() OVER (PARTITION BY thread_id
                                    ORDER BY created_at DESC, id DESC) AS rn
          FROM messages) m
    WHERE m.rn > {MESSAGES_KEEP_PER_THREAD}
      AND m.created_at < $1
      AND NOT EXISTS (SELECT 1 FROM task_runs tr WHERE tr.message_id = m.id)"""

# Order matters: tool_calls before task_runs (FK direction).
RETENTION_TARGETS: list[tuple[str, str, str]] = [
    ("events",
     f"SELECT count(*) {_EVENTS_PRED}",
     f"DELETE FROM events WHERE id IN (SELECT e.id {_EVENTS_PRED} ORDER BY e.id LIMIT $2)"),
    ("tool_calls",
     f"SELECT count(*) {_TOOL_CALLS_PRED}",
     f"DELETE FROM tool_calls WHERE id IN (SELECT t.id {_TOOL_CALLS_PRED} ORDER BY t.id LIMIT $2)"),
    ("task_runs",
     f"SELECT count(*) {_TASK_RUNS_PRED}",
     f"DELETE FROM task_runs WHERE id IN (SELECT r.id {_TASK_RUNS_PRED} ORDER BY r.id LIMIT $2)"),
    ("watch_events",
     f"SELECT count(*) {_WATCH_EVENTS_PRED}",
     f"DELETE FROM watch_events WHERE id IN (SELECT w.id {_WATCH_EVENTS_PRED} ORDER BY w.id LIMIT $2)"),
    ("messages",
     f"SELECT count(*) {_MESSAGES_PRED}",
     f"DELETE FROM messages WHERE id IN (SELECT m.id {_MESSAGES_PRED} ORDER BY m.id LIMIT $2)"),
]


def _deleted_rows(status: str) -> int:
    """asyncpg execute() returns e.g. 'DELETE 5000'."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def _sweep_rows(pool, name: str, count_sql: str, delete_sql: str,
                      cutoff: datetime, dry_run: bool) -> int:
    if dry_run:
        n = await pool.fetchval(count_sql, cutoff)
        return int(n or 0)
    total = 0
    while True:
        n = _deleted_rows(await pool.execute(delete_sql, cutoff, RETENTION_BATCH))
        total += n
        if n < RETENTION_BATCH:
            break
    return total


async def retention() -> dict:
    """Sweep old operational data. Honors retention_dry_run (default TRUE:
    counts only, deletes nothing). Each target is individually fail-soft."""
    s = get_settings()
    dry_run = bool(getattr(s, "retention_dry_run", True))
    days = int(getattr(s, "retention_days", 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    counts: dict = {"dry_run": dry_run, "cutoff": cutoff.isoformat()}
    pool = await get_pool()
    for name, count_sql, delete_sql in RETENTION_TARGETS:
        try:
            counts[name] = await _sweep_rows(pool, name, count_sql, delete_sql,
                                             cutoff, dry_run)
        except Exception:
            log.exception("retention: sweep of %s failed (continuing)", name)
            counts[name] = -1
    # Artifact directories are NOT swept here: retention.py owns disk retention
    # (its loop has protections this sweep lacks — carry_over margin, size cap,
    # proposed-action evidence). Two deleters of the same dirs is a hazard.

    log.info("retention%s: %s", " (dry-run)" if dry_run else "", counts)
    return counts


# ── Phase 9: KPI history + retired-snapshot retention (KPI-04) ──────────────
#
# Separate from retention(): these two targets have their own fixed cutoffs
# (not retention_days) and are reported under their own nightly step. They
# reuse _sweep_rows, so retention_dry_run (default TRUE) is honoured: a dry run
# issues count(*) only.
#   metric_history      — rows older than METRIC_HISTORY_KEEP_DAYS (400).
#   dashboard_snapshots — only snapshots of versions that are 'retired', are NOT
#       the dashboard's current_version_id, and were retired more than
#       RETIRED_SNAPSHOT_KEEP_DAYS ago. retired_at is reset to NULL on rollback
#       (publish), so re-retiring restarts the clock; when retired_at is NULL we
#       fall back to the snapshot's refreshed_at, then created_at.
#       FOR SHARE OF d (dashboard row only) serialises with
#       dashboards.publish()'s FOR UPDATE on the same row, so a rollback
#       committing while we wait is rechecked and its (now current) snapshots
#       are no longer selected. Do NOT also lock the version row: publish()
#       locks d then v, so locking v first deadlocks against it.
#   If a pruned version is published again, dashboard_refresh.SEED_SQL
#   re-creates its snapshot rows.

RETIRED_SNAPSHOT_KEEP_DAYS = 30

_METRIC_HISTORY_PRED = """
    FROM metric_history m
    WHERE m.observed_at < $1"""

_RETIRED_SNAPSHOT_PRED = """
    FROM dashboard_snapshots s
    JOIN dashboard_versions v ON v.id = s.version_id
    JOIN dashboards d ON d.id = v.dashboard_id
    WHERE v.status = 'retired'
      AND d.current_version_id IS DISTINCT FROM v.id
      AND COALESCE(v.retired_at, s.refreshed_at, s.created_at) < $1"""

# (name, keep_days, count_sql, delete_sql)
HISTORY_RETENTION_TARGETS: list[tuple[str, int, str, str]] = [
    ("metric_history", METRIC_HISTORY_KEEP_DAYS,
     f"SELECT count(*) {_METRIC_HISTORY_PRED}",
     f"DELETE FROM metric_history WHERE id IN "
     f"(SELECT m.id {_METRIC_HISTORY_PRED} ORDER BY m.id LIMIT $2)"),
    ("dashboard_snapshots", RETIRED_SNAPSHOT_KEEP_DAYS,
     f"SELECT count(*) {_RETIRED_SNAPSHOT_PRED}",
     f"DELETE FROM dashboard_snapshots WHERE (version_id, query_id) IN "
     f"(SELECT s.version_id, s.query_id {_RETIRED_SNAPSHOT_PRED} "
     f"ORDER BY s.version_id, s.query_id LIMIT $2 FOR SHARE OF d)"),
]


# Quick 005: usage-ledger rows older than llm_usage_retention_days (default 180;
# 0 keeps them forever). Same dry-run gate as every operational-row sweep.
_LLM_USAGE_PRED = """
    FROM llm_usage u
    WHERE u.ts < $1"""
LLM_USAGE_TARGET = ("llm_usage", f"SELECT count(*) {_LLM_USAGE_PRED}",
                    f"DELETE FROM llm_usage WHERE id IN "
                    f"(SELECT u.id {_LLM_USAGE_PRED} ORDER BY u.id LIMIT $2)")


def _history_targets() -> list[tuple[str, int, str, str]]:
    targets = list(HISTORY_RETENTION_TARGETS)
    days = int(getattr(get_settings(), "llm_usage_retention_days", 180) or 0)
    if days > 0:
        name, count_sql, delete_sql = LLM_USAGE_TARGET
        targets.append((name, days, count_sql, delete_sql))
    return targets


async def history_retention() -> dict:
    """KPI-04 nightly step. Dry run (default) counts only; each target is
    individually fail-soft (-1 on error)."""
    dry_run = bool(getattr(get_settings(), "retention_dry_run", True))
    now = datetime.now(timezone.utc)
    counts: dict = {"dry_run": dry_run}
    pool = await get_pool()
    for name, keep_days, count_sql, delete_sql in _history_targets():
        cutoff = now - timedelta(days=keep_days)
        counts[f"{name}_cutoff"] = cutoff.isoformat()
        try:
            counts[name] = await _sweep_rows(pool, name, count_sql, delete_sql,
                                             cutoff, dry_run)
        except Exception:
            log.exception("history retention: sweep of %s failed (continuing)", name)
            counts[name] = -1
    log.info("history retention%s: %s", " (dry-run)" if dry_run else "", counts)
    return counts


# ── Phase 17 (CONS-06): sync history and staging retention ──
SYNC_RUNS_KEEP_PER_SOURCE = 20
STAGE_KEEP_DAYS = 7

# Finished rows only; the newest N finished runs per source and the newest
# ok/partial run per source always survive; queued/running rows are never touched.
_SYNC_RUNS_PRED = f"""
    FROM sync_runs r
    WHERE r.created_at < $1 AND r.status IN ('ok','partial','failed')
      AND r.id NOT IN (SELECT k.id FROM (
            SELECT id, row_number() OVER (PARTITION BY source_id ORDER BY id DESC) AS rn
            FROM sync_runs WHERE status IN ('ok','partial','failed')) k
          WHERE k.rn <= {SYNC_RUNS_KEEP_PER_SOURCE})
      AND r.id NOT IN (SELECT max(id) FROM sync_runs
                       WHERE status IN ('ok','partial') GROUP BY source_id)"""

SYNC_RUNS_TARGET = (
    "sync_runs",
    f"SELECT count(*) {_SYNC_RUNS_PRED}",
    f"DELETE FROM sync_runs WHERE id IN (SELECT r.id {_SYNC_RUNS_PRED} ORDER BY r.id LIMIT $2)")


async def sync_retention() -> dict:
    """CONS-06 nightly step: prune the sync ledger and abandoned stage_* schemas.
    Honors retention_dry_run (default TRUE: counts only). Each part is fail-soft."""
    s = get_settings()
    dry_run = bool(getattr(s, "retention_dry_run", True))
    days = int(getattr(s, "retention_days", 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts: dict = {"dry_run": dry_run, "cutoff": cutoff.isoformat()}
    name, count_sql, delete_sql = SYNC_RUNS_TARGET
    try:
        pool = await get_pool()
        counts["sync_runs"] = await _sweep_rows(pool, name, count_sql, delete_sql,
                                                cutoff, dry_run)
    except Exception:
        log.exception("sync retention: sync_runs sweep failed (continuing)")
        counts["sync_runs"] = -1
    try:
        from . import warehouse, whsources
        if warehouse.warehouse_mode():
            res = await whsources.prune_stage_schemas(dry_run=dry_run,
                                                      keep_days=STAGE_KEEP_DAYS)
            counts["stage_schemas"] = int(res.get("stage_schemas", 0))
            counts["stage_tables"] = int(res.get("stage_tables", 0))
        else:
            counts["stage_schemas"] = 0
            counts["stage_tables"] = 0
    except Exception:
        log.exception("sync retention: stage schema pruning failed (continuing)")
        counts["stage_schemas"] = -1
        counts["stage_tables"] = -1
    log.info("sync retention%s: %s", " (dry-run)" if dry_run else "", counts)
    return counts


# ── step 7: graph catch-up (Quick 009) ─────────────────────────────────────

async def graph_backfill() -> dict:
    from . import memory_pipeline
    result = await memory_pipeline.backfill_graph(limit=memory_pipeline.NIGHTLY_BACKFILL)
    if str(result.get("stopped") or "").startswith("error"):
        raise RuntimeError(result["stopped"])   # recorded as this step's "error"
    return result


# ── entry point ────────────────────────────────────────────────────────────

async def run_nightly() -> dict:
    """The nightly job. Every step is individually fail-soft; a step that
    raises is recorded as 'error' and the remaining steps still run. Never
    raises — the scheduler inspects the returned dict and marks the day done
    unless EVERY step is 'error' (total failure retries on the next sweep)."""
    results: dict = {}
    steps = (
        ("learnings_merged", consolidate_learnings),
        ("communities_rebuilt", rebuild_communities),
        ("episodes_pruned", prune_episodes),
        ("sync_retention", sync_retention),  # Phase 17 (CONS-06)
        ("retention", retention),
        ("history_retention", history_retention),  # Phase 9 (KPI-04)
        ("recipes_superseded", supersede_bad_recipes),  # Quick 009
        ("graph_backfill", graph_backfill),             # Quick 009
    )
    for key, fn in steps:
        # Quick 025: a step that never returns is named by the last line logged.
        log.info("consolidation step %s starting", key)
        started = time.monotonic()
        try:
            results[key] = await fn()
        except Exception:
            log.exception("consolidation step %s failed (continuing)", key)
            results[key] = "error"
        log.info("consolidation step %s took %.1f s", key, time.monotonic() - started)
    log.info("nightly consolidation done: %s", results)
    return results


# ── Quick 008: the nightly result in words, for the Runs page ──────────────

_STEP_NAMES = {
    "learnings_merged": "learning merge", "communities_rebuilt": "community rebuild",
    "episodes_pruned": "episode pruning",
    "sync_retention": "sync history retention", "retention": "row retention",
    "history_retention": "history retention",
}


def _rows(counts) -> int:
    if not isinstance(counts, dict):
        return 0
    return sum(v for k, v in counts.items()
               if isinstance(v, int) and not isinstance(v, bool) and v > 0
               and k != "dry_run" and not k.endswith("_cutoff"))


def summarize_nightly(results: dict) -> tuple[str, dict, list[str]]:
    """(summary, counts, failed step names) for one run_nightly() result."""
    results = results if isinstance(results, dict) else {}
    parts, counts, failed = [], {}, []
    for key, label in _STEP_NAMES.items():
        if key not in results:
            continue
        v = results[key]
        if v == "error":
            failed.append(label)
            continue
        if key == "learnings_merged":
            counts[key] = v if isinstance(v, int) else 0
            parts.append(f"merged {counts[key]} learning cluster(s)")
        elif key == "communities_rebuilt":
            if isinstance(v, str):                       # Quick 038: skipped by the setting
                parts.append(f"graph community rebuild skipped ({v})")
            else:
                parts.append("graph communities rebuilt" if v else "graph communities not rebuilt")
        elif key == "episodes_pruned":
            counts[key] = v if isinstance(v, int) else 0
            parts.append(f"pruned {counts[key]} old graph episode(s)")
        else:
            n = _rows(v)
            dry = isinstance(v, dict) and v.get("dry_run")
            counts[f"{key}_rows"] = n
            what = {"history_retention": "history rows",
                    "sync_retention": "sync history rows"}.get(key, "old rows")
            parts.append(f"would delete {n} {what} (dry run)" if dry else f"deleted {n} {what}")
    if failed:
        parts.append("failed: " + ", ".join(failed))
    text = "; ".join(parts) or "nothing to do"
    return text[:1].upper() + text[1:], counts, failed
