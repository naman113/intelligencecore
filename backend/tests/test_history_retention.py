"""Phase 9 KPI-04: unit tests for consolidation.history_retention (no DB)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import consolidation, kpihistory

pytestmark = pytest.mark.asyncio


class Pool:
    def __init__(self):
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.fetchval_value = 0
        self.execute_result = "DELETE 0"
        self.execute_results: list[str] = []
        self.execute_raises_for: str | None = None

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


def _wire(monkeypatch, pool, **settings):
    async def get_pool():
        return pool
    monkeypatch.setattr(consolidation, "get_pool", get_pool)
    settings.setdefault("llm_usage_retention_days", 0)  # Quick 005 target: own tests
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: SimpleNamespace(**settings))


def _near(dt: datetime, days: int) -> bool:
    assert dt.tzinfo is not None
    expected = datetime.now(timezone.utc) - timedelta(days=days)
    return abs((dt - expected).total_seconds()) < 60


def test_constants_and_targets():
    assert consolidation.METRIC_HISTORY_KEEP_DAYS == kpihistory.MAX_DAYS == 400
    assert consolidation.RETIRED_SNAPSHOT_KEEP_DAYS == 30
    t = consolidation.HISTORY_RETENTION_TARGETS
    assert [x[0] for x in t] == ["metric_history", "dashboard_snapshots"]
    assert [x[1] for x in t] == [400, 30]


def test_predicate_text():
    (_, _, mc, md), (_, _, sc, sd) = consolidation.HISTORY_RETENTION_TARGETS
    assert "m.observed_at < $1" in mc and "m.observed_at < $1" in md
    for sql in (sc, sd):
        assert "v.status = 'retired'" in sql
        assert "d.current_version_id IS DISTINCT FROM v.id" in sql
        assert "COALESCE(v.retired_at, s.refreshed_at, s.created_at) < $1" in sql
    assert "FOR SHARE OF d" in sd and "FOR SHARE OF v" not in sd
    assert md.startswith("DELETE FROM metric_history")
    assert sd.startswith("DELETE FROM dashboard_snapshots")
    for sql in (mc, md, sc, sd):
        assert "DELETE FROM dashboard_versions" not in sql
        assert "DELETE FROM dashboards" not in sql


async def test_dry_run_counts_only(monkeypatch):
    pool = Pool()
    pool.fetchval_value = 7
    _wire(monkeypatch, pool, retention_dry_run=True)
    out = await consolidation.history_retention()
    assert pool.executes == []
    assert len(pool.fetchval_calls) == 2
    for sql, _ in pool.fetchval_calls:
        assert "count(*)" in sql
        assert "DELETE" not in sql and "FOR SHARE" not in sql
    assert out["dry_run"] is True
    assert out["metric_history"] == 7 and out["dashboard_snapshots"] == 7
    assert _near(pool.fetchval_calls[0][1][0], 400)
    assert _near(pool.fetchval_calls[1][1][0], 30)
    assert "metric_history_cutoff" in out and "dashboard_snapshots_cutoff" in out


async def test_missing_setting_is_dry_run(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool)
    out = await consolidation.history_retention()
    assert out["dry_run"] is True and pool.executes == []


async def test_live_run(monkeypatch):
    pool = Pool()
    pool.execute_result = "DELETE 3"
    _wire(monkeypatch, pool, retention_dry_run=False)
    out = await consolidation.history_retention()
    assert len(pool.executes) == 2
    assert pool.executes[0][0].startswith("DELETE FROM metric_history")
    assert pool.executes[1][0].startswith("DELETE FROM dashboard_snapshots")
    for _, args in pool.executes:
        assert args[1] == consolidation.RETENTION_BATCH
    assert out["metric_history"] == 3 and out["dashboard_snapshots"] == 3
    assert out["dry_run"] is False


async def test_batching(monkeypatch):
    pool = Pool()
    B = consolidation.RETENTION_BATCH
    pool.execute_results = [f"DELETE {B}", "DELETE 1"]
    _wire(monkeypatch, pool, retention_dry_run=False)
    out = await consolidation.history_retention()
    assert out["metric_history"] == B + 1
    assert sum(1 for s, _ in pool.executes
               if s.startswith("DELETE FROM metric_history")) == 2


async def test_per_target_fail_soft(monkeypatch):
    pool = Pool()
    pool.execute_raises_for = "DELETE FROM metric_history"
    _wire(monkeypatch, pool, retention_dry_run=False)
    out = await consolidation.history_retention()
    assert out["metric_history"] == -1
    assert out["dashboard_snapshots"] == 0


def _patch_steps(monkeypatch, ran, raising=()):
    def mk(name):
        async def fn():
            ran.append(name)
            if name in raising:
                raise RuntimeError(name)
            return name
        return fn
    for attr, name in [("consolidate_learnings", "learnings"),
                       ("rebuild_communities", "communities"),
                       ("prune_episodes", "prune"),
                       ("retention", "retention"),
                       ("history_retention", "history_retention")]:
        monkeypatch.setattr(consolidation, attr, mk(name))


async def test_run_nightly_order(monkeypatch):
    ran: list[str] = []
    _patch_steps(monkeypatch, ran)
    await consolidation.run_nightly()
    assert ran == ["learnings", "communities", "prune", "retention",
                   "history_retention"]


async def test_run_nightly_history_error_isolated(monkeypatch):
    ran: list[str] = []
    _patch_steps(monkeypatch, ran, raising=("history_retention",))
    out = await consolidation.run_nightly()
    assert out["history_retention"] == "error"
    assert out["retention"] == "retention"


async def test_retention_error_does_not_stop_history(monkeypatch):
    ran: list[str] = []
    _patch_steps(monkeypatch, ran, raising=("retention",))
    out = await consolidation.run_nightly()
    assert out["retention"] == "error"
    assert ran[-1] == "history_retention"
