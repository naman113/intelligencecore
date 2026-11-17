"""Phase C — nightly consolidation + retention (consolidation.py), pure logic.

Fakes only (no DB/LLM/graph). Proves:
  * cluster merge SUPERSEDES losers (status flip, content preserved) — never
    deletes a learning;
  * run_nightly is per-step fail-soft: step 1 raising does not stop retention;
  * dry-run counts but deletes NOTHING (rows or artifact dirs);
  * retention predicates: case-linked events excluded, newest-50-per-thread
    messages kept, queued/running task_runs kept, tool_calls swept before
    task_runs;
  * the scheduler hook fires once per day, honors consolidation_enabled and
    consolidation_hour, and only marks the day done after a successful run;
  * memory.retrieve_learnings filters superseded learnings out.
"""

import json
import types
from datetime import datetime, timezone

import pytest

from app import consolidation, expectations, memory

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- fakes

class FakePool:
    """Routes queries by SQL substring; records everything. `execute_results`
    (optional) scripts successive execute() returns, e.g. a full first DELETE
    batch followed by a short one; once exhausted, `execute_result` applies.
    acquire()/transaction() mimic asyncpg so code can wrap executes in a
    transaction — conn.execute delegates to the pool's recorder."""

    def __init__(self):
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.seeds: list[dict] = []
        self.neighbours: dict[int, list[dict]] = {}
        self.rows_by_id: dict[int, dict] = {}
        self.fetchval_value = 0
        self.execute_result = "DELETE 0"
        self.execute_results: list[str] = []
        self.execute_raises_for: str | None = None

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "JOIN learnings l2" in sql:
            return list(self.neighbours.get(args[0], []))
        if "SELECT id FROM learnings" in sql:
            return list(self.seeds)
        return []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self.rows_by_id.get(args[0])

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self.fetchval_value

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        if self.execute_raises_for and self.execute_raises_for in sql:
            raise RuntimeError("boom")
        if self.execute_results:
            return self.execute_results.pop(0)
        return self.execute_result

    def acquire(self):
        return _FakeAcquire(self)


class _FakeAcquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _FakeConn(self.pool)

    async def __aexit__(self, *exc):
        return False


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _FakeTx()

    async def execute(self, sql, *args):
        return await self.pool.execute(sql, *args)

    async def fetchval(self, sql, *args):
        return await self.pool.fetchval(sql, *args)


def _settings(**overrides):
    base = dict(
        consolidation_enabled=True, consolidation_hour=3,
        retention_days=90, retention_dry_run=True,
        stale_case_days=21, consolidation_prune_episodes=False,
        anthropic_api_key="test-key", openai_api_key="",
        timezone="UTC", expectation_poll_seconds=60,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _wire_pool(monkeypatch, pool):
    async def fake_pool():
        return pool
    monkeypatch.setattr(consolidation, "get_pool", fake_pool)


# ---------------------------------------------------------------- step 1

async def test_cluster_merge_supersedes_never_deletes(monkeypatch):
    pool = FakePool()
    # Three actives: 1 (oldest) and 2 are near-duplicates; 3 is unrelated.
    pool.seeds = [{"id": 1}, {"id": 2}, {"id": 3}]
    pool.neighbours = {
        1: [{"id": 2, "content": "newer duplicate", "created_at": 2}],
        3: [],
    }
    pool.rows_by_id = {1: {"id": 1, "content": "old version", "created_at": 1}}
    _wire_pool(monkeypatch, pool)

    merge_calls = []

    async def fake_merge(contents):
        merge_calls.append(list(contents))
        return "MERGED CONTENT"

    async def fake_embed(text):
        return "[0.1,0.2]"

    monkeypatch.setattr(memory, "merge_learning_contents", fake_merge)
    monkeypatch.setattr(memory, "_embed", fake_embed)

    n = await consolidation.consolidate_learnings()
    assert n == 1
    assert merge_calls == [["old version", "newer duplicate"]]  # oldest first

    # Nothing is EVER deleted.
    assert not any("DELETE" in sql.upper() for sql, _ in pool.executes)

    # The OLDEST row (id 1) received the merged content + re-embed.
    keeper_sql, keeper_args = pool.executes[0]
    assert "UPDATE learnings SET content" in keeper_sql
    assert keeper_args[0] == 1
    assert keeper_args[1] == "MERGED CONTENT"

    # The other member is superseded by status flip only — content untouched.
    super_sql, super_args = pool.executes[1]
    assert "status = 'superseded'" in super_sql
    assert "content" not in super_sql          # content column never assigned
    assert super_args[0] == [2]

    # Seed 2 was consumed by cluster 1: only seeds 1 and 3 ran neighbour SQL.
    neighbour_seeds = [a[0] for sql, a in pool.fetch_calls if "JOIN learnings l2" in sql]
    assert neighbour_seeds == [1, 3]


async def test_cluster_work_is_capped_per_night(monkeypatch):
    pool = FakePool()
    pool.seeds = [{"id": i} for i in range(1, 60)]
    pool.neighbours = {i: [{"id": 1000 + i, "content": f"dup {i}", "created_at": 2}]
                       for i in range(1, 60)}
    pool.rows_by_id = {i: {"id": i, "content": f"seed {i}", "created_at": 1}
                       for i in range(1, 60)}
    _wire_pool(monkeypatch, pool)

    async def fake_merge(contents):
        return "M " + contents[0]

    async def fake_embed(text):
        return "[0.1]"

    monkeypatch.setattr(memory, "merge_learning_contents", fake_merge)
    monkeypatch.setattr(memory, "_embed", fake_embed)

    n = await consolidation.consolidate_learnings()
    assert n == consolidation.MAX_CLUSTERS_PER_NIGHT


# ---------------------------------------------------------------- fail-soft

async def test_run_nightly_is_per_step_fail_soft(monkeypatch):
    ran = []

    async def broken_learnings():
        raise RuntimeError("step 1 exploded")

    async def fake_communities():
        ran.append("communities")
        return True

    async def fake_prune():
        ran.append("prune")
        return 0

    async def fake_retention():
        ran.append("retention")
        return {"dry_run": True, "events": 5}

    monkeypatch.setattr(consolidation, "consolidate_learnings", broken_learnings)
    monkeypatch.setattr(consolidation, "rebuild_communities", fake_communities)
    monkeypatch.setattr(consolidation, "prune_episodes", fake_prune)
    monkeypatch.setattr(consolidation, "retention", fake_retention)

    async def fake_history():
        ran.append("history_retention")
        return {"dry_run": True}

    monkeypatch.setattr(consolidation, "history_retention", fake_history)

    async def fake_sync_retention():
        ran.append("sync_retention")
        return {"dry_run": True, "sync_runs": 3}

    monkeypatch.setattr(consolidation, "sync_retention", fake_sync_retention)

    async def fake_recipes():
        ran.append("recipes")
        return 0

    async def fake_backfill():
        ran.append("graph_backfill")
        return {"learnings": {"sent": 0}}

    monkeypatch.setattr(consolidation, "supersede_bad_recipes", fake_recipes)
    monkeypatch.setattr(consolidation, "graph_backfill", fake_backfill)

    out = await consolidation.run_nightly()   # must not raise
    assert out["learnings_merged"] == "error"
    assert ran == ["communities", "prune", "sync_retention", "retention", "history_retention",
                   "recipes", "graph_backfill"]
    assert out["retention"] == {"dry_run": True, "events": 5}


async def test_prune_episodes_off_by_default(monkeypatch):
    monkeypatch.setattr(consolidation, "get_settings", lambda: _settings())

    async def would_fail(*a, **k):
        raise AssertionError("graph must not be touched when pruning is off")

    import app.memoryservice as memoryservice
    monkeypatch.setattr(memoryservice.memory_graph, "remove_episodes_before",
                        would_fail)
    assert await consolidation.prune_episodes() == 0


# ---------------------------------------------------------------- retention

async def test_dry_run_counts_but_deletes_nothing(monkeypatch, tmp_path):
    pool = FakePool()
    pool.fetchval_value = 7
    _wire_pool(monkeypatch, pool)
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(retention_dry_run=True))

    counts = await consolidation.retention()
    assert counts["dry_run"] is True
    assert pool.executes == []                       # not one DELETE issued
    # The dry-run path must go through fetchval with the COUNT query — a
    # regression that hands delete_sql to fetchval would delete on a real DB
    # while this suite stays green.
    assert pool.fetchval_calls, "dry run must issue count queries"
    for sql, _ in pool.fetchval_calls:
        assert "count(*)" in sql
        assert "DELETE" not in sql.upper()
    for name in ("events", "tool_calls", "task_runs", "watch_events", "messages"):
        assert counts[name] == 7


async def test_live_run_deletes_rows_but_never_touches_artifacts(monkeypatch, tmp_path):
    """Disk retention is retention.py's job (carry_over margin, size cap,
    proposed-action evidence). consolidation.retention must not delete dirs."""
    pool = FakePool()
    pool.execute_result = "DELETE 3"                 # < batch → one batch each
    _wire_pool(monkeypatch, pool)
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(retention_dry_run=False))

    import app.sandbox as sandbox
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    old_dir = tmp_path / "run_9"
    old_dir.mkdir()
    (old_dir / "out.csv").write_text("x")
    import os
    os.utime(old_dir, (0, 0))                        # ancient — still not ours

    counts = await consolidation.retention()
    assert counts["dry_run"] is False
    deleted_tables = [sql for sql, _ in pool.executes]
    assert len(deleted_tables) == 5
    for name in ("events", "tool_calls", "task_runs", "watch_events", "messages"):
        assert counts[name] == 3
    assert "artifact_dirs" not in counts
    assert old_dir.is_dir()                          # retention.py owns disk


async def test_live_run_batches_until_short_batch(monkeypatch, tmp_path):
    """The while-loop in _sweep_rows must keep deleting until a batch comes
    back short — a single-batch-per-night mutation must fail here."""
    pool = FakePool()
    # First target: one full batch then a short one; remaining targets: empty.
    pool.execute_results = [f"DELETE {consolidation.RETENTION_BATCH}", "DELETE 3"]
    pool.execute_result = "DELETE 0"
    _wire_pool(monkeypatch, pool)
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(retention_dry_run=False))
    import app.sandbox as sandbox
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)

    counts = await consolidation.retention()
    assert counts["events"] == consolidation.RETENTION_BATCH + 3
    events_deletes = [sql for sql, _ in pool.executes if "DELETE FROM events" in sql]
    assert len(events_deletes) == 2                  # full batch → second sweep


async def test_one_failing_target_does_not_stop_the_rest(monkeypatch):
    pool = FakePool()
    pool.execute_result = "DELETE 0"
    pool.execute_raises_for = "DELETE FROM events"   # only events sweep breaks
    _wire_pool(monkeypatch, pool)
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(retention_dry_run=False))

    counts = await consolidation.retention()
    assert counts["events"] == -1                    # recorded as failed
    for name in ("tool_calls", "task_runs", "watch_events", "messages"):
        assert counts[name] == 0                     # still ran


async def test_retention_predicates_are_safe():
    targets = {name: (count_sql, delete_sql)
               for name, count_sql, delete_sql in consolidation.RETENTION_TARGETS}

    # Case-linked events, expectation satisfiers, and fact-source events are
    # excluded up front (case_facts.source_event_id is a NO ACTION FK: one
    # unexcluded referent would abort the whole batched DELETE).
    for sql in targets["events"]:
        assert "NOT IN (SELECT event_id FROM case_events)" in sql
        assert "satisfied_by" in sql
        assert "case_facts cf WHERE cf.source_event_id = e.id" in sql
        assert "occurred_at < $1" in sql

    # queued/running task_runs always survive; EVERY FK referrer excluded up
    # front — including learnings.source_run, whose FK would otherwise abort
    # the whole batch on the first run any learning points at.
    for sql in targets["task_runs"]:
        assert "status NOT IN ('queued','running')" in sql
        assert "tool_calls" in sql and "artifacts" in sql and "actions" in sql
        assert "learnings l WHERE l.source_run = r.id" in sql

    # Newest 50 per thread always survive; task_run-referenced messages kept.
    for sql in targets["messages"]:
        assert "PARTITION BY thread_id" in sql
        assert f"rn > {consolidation.MESSAGES_KEEP_PER_THREAD}" in sql
        assert "task_runs tr WHERE tr.message_id" in sql

    # FK direction: tool_calls swept BEFORE task_runs.
    order = [name for name, _, _ in consolidation.RETENTION_TARGETS]
    assert order.index("tool_calls") < order.index("task_runs")


# ---------------------------------------------------------------- scheduler hook

_DEFAULT_PB = [{"id": 1, "name": "pb",
                "trigger": json.dumps({"type": "cron", "hour": 23})}]


class HookPool:
    """Pool for expectations._dispatch_cron. Default: one cron playbook whose
    hour never matches, so only the maintenance hooks can fire. Records fetches
    so tests can assert the playbook query was (not) issued."""

    def __init__(self, playbooks=None):
        self.playbooks = _DEFAULT_PB if playbooks is None else playbooks
        self.fetch_calls: list[str] = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append(sql)
        return list(self.playbooks)


def _wire_hook(monkeypatch, settings, state, run_calls, *, fail=False,
               playbooks=None, nightly_result=None):
    saved = {}
    pool = HookPool(playbooks)

    async def fake_pool():
        return pool

    async def load_state():
        return dict(state)

    async def save_state(s):
        saved.clear()
        saved.update(s)

    async def fake_run_nightly():
        if fail:
            raise RuntimeError("nightly blew up")
        run_calls.append(1)
        return {} if nightly_result is None else dict(nightly_result)

    async def fake_enqueue(pid, **kw):
        return 1, True

    monkeypatch.setattr(expectations, "get_settings", lambda: settings)
    monkeypatch.setattr(expectations, "get_pool", fake_pool)
    monkeypatch.setattr(expectations, "_load_cron_state", load_state)
    monkeypatch.setattr(expectations, "_save_cron_state", save_state)
    monkeypatch.setattr(expectations, "_local_now",
                        lambda: datetime(2026, 8, 10, 3, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(consolidation, "run_nightly", fake_run_nightly)
    import app.playbooks
    monkeypatch.setattr(app.playbooks, "enqueue_playbook_run_once", fake_enqueue)
    return saved, pool


async def test_hook_fires_once_per_day(monkeypatch):
    calls = []
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls)
    fired = await expectations._dispatch_cron()
    assert calls == [1]
    assert fired == 1
    assert saved["_consolidation_day"] == "2026-08-10"

    # Same day again: already marked done — must not run twice.
    calls.clear()
    _wire_hook(monkeypatch, _settings(),
               {"_consolidation_day": "2026-08-10"}, calls)
    assert await expectations._dispatch_cron() == 0
    assert calls == []


async def test_hook_respects_disabled_and_hour(monkeypatch):
    calls = []
    _wire_hook(monkeypatch, _settings(consolidation_enabled=False), {}, calls)
    assert await expectations._dispatch_cron() == 0
    assert calls == []

    calls = []
    _wire_hook(monkeypatch, _settings(consolidation_hour=4), {}, calls)  # now is 03:05
    assert await expectations._dispatch_cron() == 0
    assert calls == []


async def test_hook_failure_retries_next_sweep(monkeypatch):
    calls = []
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls, fail=True)
    fired = await expectations._dispatch_cron()      # must not raise
    assert fired == 0
    assert calls == []
    assert "_consolidation_day" not in saved         # day NOT marked done


async def test_hook_fires_with_zero_cron_playbooks(monkeypatch):
    """A fresh box with no active cron playbooks must still get its nightly
    consolidation + retention — data hygiene does not ride on the digest."""
    calls = []
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls, playbooks=[])
    fired = await expectations._dispatch_cron()
    assert calls == [1]
    assert fired == 1
    assert saved["_consolidation_day"] == "2026-08-10"


async def test_hook_fires_without_llm_key(monkeypatch):
    """Retention is pure SQL: no LLM key configured must not disable the
    nightly hook. The playbook query itself is skipped (zero-config boxes must
    not accumulate failed task_runs)."""
    calls = []
    saved, pool = _wire_hook(
        monkeypatch, _settings(anthropic_api_key="", openai_api_key=""), {}, calls)
    fired = await expectations._dispatch_cron()
    assert calls == [1]
    assert fired == 1
    assert saved["_consolidation_day"] == "2026-08-10"
    assert pool.fetch_calls == []        # no playbook dispatch without a key


async def test_hook_failure_not_masked_by_playbook_fire(monkeypatch):
    """A cron playbook firing in the SAME sweep persists state — but the failed
    consolidation day must still be absent, so the night retries."""
    calls = []
    pb = [{"id": 1, "name": "pb",
           "trigger": json.dumps({"type": "cron", "hour": 3})}]  # matches 03:05
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls,
                          fail=True, playbooks=pb)
    fired = await expectations._dispatch_cron()      # must not raise
    assert fired == 1                                # the playbook fired
    assert calls == []                               # nightly did not run
    assert saved["1"] == "2026-08-10"                # playbook day persisted
    assert "_consolidation_day" not in saved         # failed night NOT marked


async def test_all_steps_failing_marks_night_for_retry(monkeypatch):
    """run_nightly never raises; the caller inspects the results dict. A night
    where EVERY step errored (e.g. DB down at 03:00) is not stamped done."""
    calls = []
    all_error = {"learnings_merged": "error", "communities_rebuilt": "error",
                 "episodes_pruned": "error", "retention": "error",
                 "history_retention": "error"}
    # A same-sweep playbook fire forces the state save, so a mark-day-anyway
    # mutation would actually persist and be caught here.
    pb = [{"id": 1, "name": "pb",
           "trigger": json.dumps({"type": "cron", "hour": 3})}]
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls,
                          nightly_result=all_error, playbooks=pb)
    fired = await expectations._dispatch_cron()
    assert calls == [1]                              # it did run
    assert fired == 1                                # only the playbook counted
    assert saved["1"] == "2026-08-10"                # state WAS persisted
    assert "_consolidation_day" not in saved         # failed night NOT marked

    # A partially successful night (retention ran) IS marked done.
    calls.clear()
    partial = dict(all_error, retention={"dry_run": True})
    saved, _ = _wire_hook(monkeypatch, _settings(), {}, calls,
                          nightly_result=partial)
    assert await expectations._dispatch_cron() == 1
    assert saved["_consolidation_day"] == "2026-08-10"


# ---------------------------------------------------------------- retrieval

async def test_retrieve_learnings_excludes_superseded(monkeypatch):
    recorded = []

    class Pool:
        async def fetch(self, sql, *args):
            recorded.append(sql)
            return []

    async def fake_embed(text):
        return "[0.1]"

    async def fake_pool():
        return Pool()

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr(memory, "get_pool", fake_pool)
    assert await memory.retrieve_learnings("anything") == []
    assert len(recorded) == 1
    assert "status = 'active'" in recorded[0]


async def test_dedup_store_never_matches_superseded(monkeypatch):
    """The dedup/merge closest-match query must consider ACTIVE rows only:
    matching a superseded row would either discard new knowledge as 'duplicate'
    or merge it into a row retrieval can never see — silent memory loss."""
    recorded = []

    class Pool:
        async def fetchrow(self, sql, *args):
            recorded.append(sql)
            return None                      # no match → INSERT path

        async def execute(self, sql, *args):
            return None

        async def fetchval(self, sql, *args):  # Quick 009: INSERT ... RETURNING id
            return 1

    async def fake_embed(text):
        return "[0.1]"

    monkeypatch.setattr(memory, "_embed", fake_embed)
    # kind='recipe' skips the graph mirror, keeping this a pure-SQL test.
    out = await memory._dedup_store(None, Pool(), "recipe",
                                    "SELECT 1 -- a recipe learning")
    assert out == "stored"
    assert len(recorded) == 1
    assert "status = 'active'" in recorded[0]


def test_summarize_names_sync_retention():
    text, counts, failed = consolidation.summarize_nightly(
        {"sync_retention": {"dry_run": True, "sync_runs": 4, "stage_schemas": 1}})
    assert "sync history" in text and "dry run" in text
    assert counts["sync_retention_rows"] == 5 and not failed
    _, _, failed = consolidation.summarize_nightly({"sync_retention": "error"})
    assert failed == ["sync history retention"]
