"""Phase 15 (SRC-05, SRC-06): sync run bookkeeping in sourcesvc and the worker's single sync lane."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import config, heartbeat, secretbox, sourcesvc, syncrunner, whsources, worker


# ── fakes ───────────────────────────────────────────────────────────────────
class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        self.pool.queries.append(sql)
        if "FROM sync_runs" in sql:
            return self.pool.run
        return self.pool.claim.pop(0) if self.pool.claim else None

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))
        return "UPDATE 1"


class _Acq:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


class Pool:
    def __init__(self):
        self.run = {"id": 5, "source_id": 1, "kind": "sync", "task_run_id": 7, "status": "queued"}
        self.claim = []
        self.queries = []
        self.executed = []
        self.fail_open_result = "UPDATE 1"
        self.conn = _Conn(self)

    def acquire(self):
        return _Acq(self.conn)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "FROM sync_runs" in sql or "UPDATE sync_runs SET status='failed'" in sql:
            return self.fail_open_result
        return "UPDATE 1"

    async def fetchval(self, sql, *args):
        if "SELECT name FROM sources" in sql:
            return "shop"
        if "SELECT kind FROM sync_runs" in sql:
            return self.run["kind"]
        return None

    def by(self, needle):
        return [a for sql, a in self.executed if needle in sql]


def _source(**over):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = {"id": 1, "name": "shop", "kind": "mysql", "credential": "cred:shop", "status": "active",
         "config": {"caps": {"max_rows_per_run": 5000, "max_seconds_per_run": 600},
                    "remote_schema": "shop", "backfill_start": "2020-01-01"},
         "schedule": {"interval_minutes": 60, "window": None, "reconcile_hour": 2},
         "tables": [
             {"remote_table": "orders", "warehouse_table": "orders", "mode": "import", "status": "active",
              "cursor_column": "updated_at", "merge_key": ["id"], "reconcile": True,
              "last_success_at": now, "est_rows": 1000, "rows_total": 10},
             {"remote_table": "items", "warehouse_table": "items", "mode": "import", "status": "active",
              "cursor_column": None, "merge_key": ["id"], "reconcile": False,
              "last_success_at": None, "est_rows": 0, "rows_total": None},
             {"remote_table": "live1", "warehouse_table": "live1", "mode": "live", "status": "active",
              "last_success_at": None}]}
    s.update(over)
    return s


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    for k in ("SYNC_TASK_TIMEOUT_SECONDS", "SYNC_MAX_ROWS_PER_RUN", "SYNC_MAX_SECONDS_PER_RUN",
              "SYNC_CHUNK_SIZE"):
        monkeypatch.delenv(k, raising=False)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch):
    pool = Pool()
    state = {"source": _source(), "result": None, "catalog": [], "views": [], "raise": None}

    async def gp():
        return pool

    async def gs(name):
        return state["source"]

    async def load(name):
        assert not name.startswith("cred:"), "vault takes the bare credential name"
        return {"host": "h", "port": 3306, "database": "d", "user": "u", "password": "pw-secret-123"}

    async def run(cmd, job, secret_env, *, name, network, timeout, stdin_path=None):
        state["job"], state["cmd"], state["timeout"], state["secret_env"] = job, cmd, timeout, secret_env
        if state["raise"]:
            raise state["raise"]
        return state["result"]

    async def net(cmd):
        return "net"

    async def views(name, tables=None, roles=None):
        state["views"].append((name, list(tables or [])))
        return {"status": "ok"}

    async def wc(conn, name, probe=None):
        state["catalog"].append((conn, name))
        return 3

    class FakeWh:
        async def fetch(self, *a):
            return []

        async def close(self):
            pass

    async def conn_(role, **kw):
        return FakeWh()

    from app import sourcevault, warehouse
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    monkeypatch.setattr(sourcevault, "load", load)
    monkeypatch.setattr(syncrunner, "run", run)
    monkeypatch.setattr(syncrunner, "network_for", net)
    monkeypatch.setattr(syncrunner, "loader_env", lambda: {"IC_WH_USER": "loader"})
    monkeypatch.setattr(whsources, "refresh_import_views", views)
    monkeypatch.setattr(whsources, "write_catalog", wc)
    monkeypatch.setattr(warehouse, "connect", conn_)
    return pool, state


def _res(*tables, status="ok", **kw):
    return {"status": status, "tables": list(tables), "duration_ms": 1200, "peak_memory_mb": 300, **kw}


def _t(table, status="ok", rows=10, capped=False, **kw):
    return {"table": table, "status": status, "rows": rows, "cursor_max": None, "capped": capped, **kw}


TASK = {"id": 7}


# ── build_sync_job / caps ───────────────────────────────────────────────────
def test_build_job_orders_and_excludes_live():
    cmd, job, stdin = sourcesvc.build_sync_job(_source(), _source()["tables"], "sync")
    assert cmd == "sync" and job["dataset"] == "ic_imp_shop" and job["pipeline"] == "ic_shop"
    assert [t["warehouse_table"] for t in job["tables"]] == ["items", "orders"]  # NULLs first
    assert job["tables"][1]["cursor"] == "updated_at" and job["tables"][1]["merge_key"] == ["id"]
    assert job["tables"][1]["initial_value"] == "2020-01-01"
    assert not any(t["reconcile"] for t in job["tables"]) and job["reconcile"] is False


def test_build_job_reconcile_only_flagged_tables():
    _, job, _ = sourcesvc.build_sync_job(_source(), _source()["tables"], "reconcile")
    assert {t["warehouse_table"]: t["reconcile"] for t in job["tables"]} == {"items": False, "orders": True}


@pytest.mark.parametrize("src", [
    _source(name="business_db", tables=[{"mode": "live", "status": "active", "warehouse_table": "x",
                                         "remote_table": "x"}]),  # Phase 17: only all-live is nothing to sync
    _source(kind="csv"),
    _source(tables=[{"mode": "live", "status": "active", "warehouse_table": "x", "remote_table": "x"}])])
def test_build_job_nothing_to_sync(src):
    with pytest.raises(ValueError, match="nothing to sync"):
        sourcesvc.build_sync_job(src, src["tables"], "sync")


def _caps(rows, secs=600):
    s = _source(config={"caps": {"max_rows_per_run": rows, "max_seconds_per_run": secs}})
    return sourcesvc.build_sync_job(s, s["tables"], "sync")[1]["caps"]


def test_caps_chunk_follows_row_cap():
    assert _caps(5000)["chunk_size"] == 5000
    assert _caps(300)["chunk_size"] == 300
    assert _caps(2_000_000)["chunk_size"] == 50_000


def test_caps_seconds_clamped_below_container_timeout():
    assert _caps(5000, 7200)["max_seconds"] == 1800
    assert _caps(5000, 600)["max_seconds"] == 600


def test_caps_rows_clamped_to_settings(monkeypatch):
    monkeypatch.setenv("SYNC_MAX_ROWS_PER_RUN", "1000")
    config.get_settings.cache_clear()
    assert _caps(9_000_000)["max_rows"] == 1000


# ── table_progress ──────────────────────────────────────────────────────────
def test_table_progress_cases():
    assert sourcesvc.table_progress(_t("a", capped=True, rows=5)) == ("ok", True)
    assert sourcesvc.table_progress(_t("a")) == ("ok", False)
    assert sourcesvc.table_progress(_t("a", capped=True, rows=0)) == ("failed", False)
    assert sourcesvc.table_progress(_t("a", status="skipped", rows=0, capped=True)) == ("skipped", True)
    assert sourcesvc.table_progress(_t("a", status="failed", capped=True)) == ("failed", False)


# ── run_sync_task ───────────────────────────────────────────────────────────
async def test_run_ok_records_everything(env):
    pool, st = env
    st["result"] = _res(_t("orders", rows=40), _t("items", rows=2))
    await sourcesvc.run_sync_task(dict(TASK))
    assert st["timeout"] == worker._sync_timeout() < worker._sync_lane_timeout()
    assert st["secret_env"]["IC_SRC_PASSWORD"] == "pw-secret-123" and st["secret_env"]["IC_WH_USER"] == "loader"
    upd = pool.by("UPDATE sync_runs SET status=$2")[0]
    assert upd[1] == "ok" and upd[3] == 42 and upd[2] == 1200 and upd[6] == 300
    assert len(pool.by("UPDATE source_tables SET last_success_at")) == 2
    assert st["views"] == [("shop", ["orders", "items"])]
    assert len(st["catalog"]) == 1 and st["catalog"][0][1] == "shop"
    assert pool.by("UPDATE task_runs")[0][1:] == ("done", None)


async def test_progress_and_rows_total_use_estimate(env, monkeypatch):
    pool, st = env

    async def est(name, tables):
        return {"orders": 500}
    monkeypatch.setattr(sourcesvc, "_estimate_rows", est)
    st["result"] = _res(_t("orders", capped=True, rows=5))
    await sourcesvc.run_sync_task(dict(TASK))
    a = pool.by("UPDATE source_tables SET last_success_at")[0]
    assert a[2] == 500 and a[3] is False and a[4] == pytest.approx(0.5)


async def test_partial_run_keeps_catalog_call_shape(env):
    pool, st = env
    st["result"] = _res(_t("orders"), _t("items", status="failed", rows=0, error_class="OperationalError",
                                         error="lost connection"), status="partial")
    await sourcesvc.run_sync_task(dict(TASK))
    upd = pool.by("UPDATE sync_runs SET status=$2")[0]
    assert upd[1] == "partial"
    bad = pool.by("UPDATE source_tables SET status='failed'")[0]
    assert bad[1] == "items" and bad[2] == "OperationalError: lost connection"
    assert len(st["catalog"]) == 1  # exactly one call: (conn, name), no table list
    assert pool.by("UPDATE task_runs")[0][1] == "done"


async def test_run_summary_keeps_error_class_and_reconciled_deleted(env):
    import json
    pool, st = env
    st["result"] = _res(_t("orders", reconciled_deleted=1),
                        _t("items", status="failed", rows=0, error_class="DataValidationError",
                           error="type changed"), status="partial")
    await sourcesvc.run_sync_task(dict(TASK))
    summary = {t["table"]: t for t in json.loads(pool.by("UPDATE sync_runs SET status=$2")[0][4])}
    assert summary["orders"]["reconciled_deleted"] == 1
    assert summary["items"]["error_class"] == "DataValidationError"


async def test_skipped_table_untouched_and_counts_as_capped(env):
    pool, st = env
    st["result"] = _res(_t("orders"), _t("items", status="skipped", rows=0, capped=True))
    await sourcesvc.run_sync_task(dict(TASK))
    assert len(pool.by("UPDATE source_tables SET last_success_at")) == 1
    assert not pool.by("UPDATE source_tables SET status='failed'")
    nxt = pool.by("UPDATE sources SET next_sync_at")[0][1]
    assert nxt <= datetime.now(timezone.utc) + timedelta(seconds=5)  # immediate re-run


async def test_no_progress_ok_result_not_capped(env):
    pool, st = env
    st["result"] = _res(_t("orders", capped=True, rows=0))
    await sourcesvc.run_sync_task(dict(TASK))
    bad = pool.by("UPDATE source_tables SET status='failed'")[0]
    assert bad[2] == sourcesvc.NO_PROGRESS_ERROR
    nxt = pool.by("UPDATE sources SET next_sync_at")[0][1]
    assert nxt > datetime.now(timezone.utc) + timedelta(minutes=30)


async def test_cursor_stalled_not_rescheduled(env):
    pool, st = env
    st["result"] = _res(_t("orders", status="failed", rows=0, capped=False, error_class="CursorStalled",
                           error="stalled"), status="failed")
    await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE sync_runs SET status=$2")[0][1] == "failed"
    nxt = pool.by("UPDATE sources SET next_sync_at")[0][1]
    assert nxt > datetime.now(timezone.utc) + timedelta(minutes=30)
    assert pool.by("UPDATE task_runs")[0][1:] == ("failed", "sync failed: CursorStalled")


async def test_failed_result_fails_task(env):
    pool, st = env
    st["result"] = {"status": "failed", "error": "no result from sync container", "tables": []}
    await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE sync_runs SET status=$2")[0][1] == "failed"
    assert pool.by("UPDATE task_runs")[0][1] == "failed"


async def test_reconcile_success_sets_date(env):
    pool, st = env
    pool.run["kind"] = "reconcile"
    st["result"] = _res(_t("orders"))
    await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("last_reconcile_on")


async def test_image_missing(env):
    pool, st = env
    st["raise"] = syncrunner.SyncImageMissing()
    await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE task_runs")[0][1:] == ("failed", "sync runner not installed")
    assert pool.by("UPDATE sync_runs SET status='failed'")[0][1] == "sync runner not installed"


async def test_inactive_source_fails_run(env):
    pool, st = env
    st["source"] = _source(status="paused")
    await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE sync_runs SET status='failed'")[0][1] == "source is not active"
    assert "not active" in pool.by("UPDATE task_runs")[0][2]


async def test_cancel_finalizes_ledger_and_reraises(env):
    pool, st = env
    st["raise"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE sync_runs SET status='failed'")[-1][1] == "sync container timed out"


async def test_exception_finalizes_ledger_with_class(env):
    pool, st = env
    st["raise"] = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await sourcesvc.run_sync_task(dict(TASK))
    assert pool.by("UPDATE sync_runs SET status='failed'")[-1][1] == "RuntimeError"


async def test_fail_open_run_twice_updates_nothing(env):
    pool, _ = env
    assert await sourcesvc.fail_open_run(7, "x") == 1
    pool.fail_open_result = "UPDATE 0"
    assert await sourcesvc.fail_open_run(7, "x") == 0
    assert "status IN ('queued','running')" in [s for s, _ in pool.executed][-1]


async def test_secret_never_stored(env):
    pool, st = env
    secretbox.add_known(["pw-secret-123"])
    st["result"] = _res(_t("orders", status="failed", rows=0, error_class="Err",
                           error="login failed for pw-secret-123"), status="failed",
                        error="bad pw-secret-123")
    await sourcesvc.run_sync_task(dict(TASK))
    blob = repr(pool.executed)
    assert "pw-secret-123" not in blob.replace("'IC_SRC_PASSWORD'", "")


def test_next_after_run_windows():
    s = {"interval_minutes": 60, "window": [1, 5]}
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert sourcesvc.next_after_run(s, True, noon).hour == 1 and sourcesvc.next_after_run(s, True, noon).day == 2
    inside = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    assert sourcesvc.next_after_run(s, True, inside) == inside
    assert sourcesvc.next_after_run({"interval_minutes": 60}, False, noon) == noon + timedelta(minutes=60)


# ── worker sync lane ────────────────────────────────────────────────────────
def test_lane_timeout_margin(monkeypatch):
    assert worker.SYNC_LANE_MARGIN == 120
    assert worker._sync_lane_timeout() == worker._sync_timeout() + 120 == 3720
    monkeypatch.setenv("SYNC_TASK_TIMEOUT_SECONDS", "7200")
    config.get_settings.cache_clear()
    assert worker._sync_lane_timeout() == 7320


class LanePool:
    def __init__(self, generic=None, sync=None):
        self.generic, self.sync = generic, sync
        self.sqls = []
        self.executed = []

    def acquire(self):
        return _Acq(self)

    async def fetchrow(self, sql, *a):
        self.sqls.append(sql)
        if "t.kind <> 'sync'" in sql:
            return self.generic
        if "t.kind = 'sync'" in sql:
            return self.sync
        return None

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "UPDATE 1"


def _task(kind="sync"):
    return {"id": 7, "thread_id": "sync", "message_id": None, "kind": kind,
            "playbook_id": None, "case_id": None, "expectation_id": None}


@pytest.fixture
def lane(monkeypatch):
    pool = LanePool(sync=_task())

    async def gp():
        return pool
    monkeypatch.setattr(worker, "get_pool", gp)
    busy, beats, failed = [], [], []
    monkeypatch.setattr(heartbeat, "busy", lambda n, w: busy.append((n, w)))
    monkeypatch.setattr(heartbeat, "beat", lambda n, w: beats.append((n, w)))

    async def fo(tid, err):
        failed.append((tid, err))
        return 1
    monkeypatch.setattr(sourcesvc, "fail_open_run", fo)
    return pool, busy, beats, failed


async def test_generic_claim_never_takes_sync(lane):
    pool, busy, *_ = lane
    pool.generic = None  # the generic claim finds nothing even though a sync task is queued
    assert await worker._process_one() is False
    assert any("t.kind <> 'sync'" in s for s in pool.sqls)
    assert not any("t.kind = 'sync'" in s for s in pool.sqls) and busy == []


async def test_sync_claim_only_sync_with_thread_rule(lane, monkeypatch):
    pool, busy, _, failed = lane
    seen = {}

    async def ok(task):
        seen["task"] = task
    monkeypatch.setattr(worker, "_run_sync_task", ok)
    assert await worker._process_sync_one() is True
    sql = next(s for s in pool.sqls if "t.kind = 'sync'" in s)
    assert "r.thread_id = t.thread_id" in sql and "SKIP LOCKED" in sql
    assert busy == [("sync", worker._sync_lane_timeout() + 300)] and seen["task"]["id"] == 7 and failed == []


async def test_sync_claim_idle(lane):
    pool, *_ = lane
    pool.sync = None
    assert await worker._process_sync_one() is False


async def test_lane_timeout_fails_task_and_ledger(lane, monkeypatch):
    pool, _, _, failed = lane

    async def slow(task):
        await asyncio.sleep(5)
    monkeypatch.setattr(worker, "_run_sync_task", slow)
    monkeypatch.setattr(worker, "_sync_lane_timeout", lambda: 0.05)
    assert await worker._process_sync_one() is True
    assert failed == [(7, "sync container timed out")]
    assert ("UPDATE task_runs", "sync lane timed out") == (
        "UPDATE task_runs", pool.executed[-1][1][1])


async def test_lane_exception_fails_task_and_ledger(lane, monkeypatch):
    pool, _, _, failed = lane

    async def boom(task):
        raise RuntimeError("kaput")
    monkeypatch.setattr(worker, "_run_sync_task", boom)
    assert await worker._process_sync_one() is True
    assert failed == [(7, "RuntimeError")]
    assert pool.executed[-1][1][1] == "RuntimeError: kaput"


async def test_lane_error_in_fail_open_never_escapes(lane, monkeypatch):
    async def boom(task):
        raise RuntimeError("x")

    async def bad_fo(tid, err):
        raise ConnectionError("db")
    monkeypatch.setattr(worker, "_run_sync_task", boom)
    monkeypatch.setattr(sourcesvc, "fail_open_run", bad_fo)
    assert await worker._process_sync_one() is True


async def test_lane_ledger_never_left_open_then_enqueue_accepted(lane, monkeypatch):
    """A fake ledger with the in-flight rule: after a lane timeout the next enqueue is accepted."""
    ledger = {"status": "running"}

    async def fo(tid, err):
        if ledger["status"] in ("queued", "running"):
            ledger["status"] = "failed"
            return 1
        return 0
    monkeypatch.setattr(sourcesvc, "fail_open_run", fo)

    async def slow(task):
        await asyncio.sleep(5)
    monkeypatch.setattr(worker, "_run_sync_task", slow)
    monkeypatch.setattr(worker, "_sync_lane_timeout", lambda: 0.05)
    await worker._process_sync_one()
    assert ledger["status"] not in ("queued", "running")
    assert ledger["status"] != "running"  # an INSERT ... ON CONFLICT on the inflight index now succeeds


async def test_consume_sync_never_dies_and_uses_own_heartbeat(lane, monkeypatch):
    _, _, beats, _ = lane
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("x")
        if calls["n"] == 2:
            raise ConnectionError("blip")
        return False
    monkeypatch.setattr(worker, "_process_sync_one", flaky)
    monkeypatch.setattr(worker, "POLL_INTERVAL", 0.005)
    t = asyncio.create_task(worker._consume_sync())
    await asyncio.sleep(0.15)
    assert not t.done()
    t.cancel()
    assert calls["n"] >= 3
    assert ("sync", 30) in beats and all(n == "sync" for n, _ in beats)


async def test_run_forever_gathers_one_lane(monkeypatch):
    import inspect
    src = inspect.getsource(worker.run_forever)
    assert "_consume_sync()" in src and src.count("_consume_sync()") == 1


def test_orphan_recovery_sql_and_cleanup_call():
    import inspect
    src = inspect.getsource(worker.run_forever)
    assert "error='orphaned sync run'" in src and "UPDATE sync_runs" in src
    assert "t.id = sync_runs.task_run_id AND t.status = 'queued'" in src
    assert "syncrunner.cleanup_orphans()" in src


async def test_run_sync_task_delegates(monkeypatch):
    seen = []

    async def rs(task):
        seen.append(task)
    monkeypatch.setattr(sourcesvc, "run_sync_task", rs)
    await worker._run_sync_task({"id": 3})
    assert seen == [{"id": 3}] and worker.SYNC_RUNNER_MISSING == "sync runner not installed"
