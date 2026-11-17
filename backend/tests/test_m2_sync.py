"""M2 sync (quick 016): Milestone 1 add-ons meeting the Phase 14 warehouse.

Pure tests, no database: the warehouse-mode SQL for Quick 012's table sizes and
Quick 009's table names, the Runs page view of task_runs kind='sync' (Quick 008
x Phase 14), the sync timeout floor (Quick 003 setting) and the warehouse loop's
job_runs error record.
"""
import inspect

from app import config, jobruns, runsapi, schemadiscovery as sd, warehouse, worker
from app.guardrails import WarehouseUnreachable, validate_readonly_sql


def _wh_mode(monkeypatch, on=True):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: on)


def _record_sql(monkeypatch, rows=None, fail=None):
    seen = []

    async def fake(sql, limit=None):
        seen.append(sql)
        if fail is not None:
            raise fail
        return rows or []

    monkeypatch.setattr(sd, "run_readonly_sql", fake)
    sd.reset_row_estimates()
    return seen


# ── Quick 012 sizes and Quick 009 names through the warehouse ──────────────

def test_warehouse_sql_passes_the_guardrail():
    for sql in (sd.WAREHOUSE_ROW_ESTIMATE_SQL, sd.WAREHOUSE_TABLE_NAMES_SQL):
        validate_readonly_sql(sql)
        assert "ic_meta.source_catalog" in sql and "has_table_privilege" in sql
        assert "reltuples" not in sql, "the warehouse's own reltuples knows nothing of the views"
    assert "row_estimate" in sd.WAREHOUSE_ROW_ESTIMATE_SQL
    assert "'r', 'p', 'm'" in sd.WAREHOUSE_ROW_ESTIMATE_SQL


async def test_row_estimates_read_the_snapshot_in_warehouse_mode(monkeypatch):
    _wh_mode(monkeypatch)
    seen = _record_sql(monkeypatch, rows=[
        {"table_name": "VehicleTelemetry", "row_estimate": 4_412_000},
        {"table_name": "fresh", "row_estimate": -1}])
    est = await sd.row_estimates()
    assert seen == [sd.WAREHOUSE_ROW_ESTIMATE_SQL]
    assert est == {"vehicletelemetry": ("VehicleTelemetry", 4_412_000)}
    sd.reset_row_estimates()


async def test_row_estimates_direct_path_unchanged(monkeypatch):
    _wh_mode(monkeypatch, False)
    seen = _record_sql(monkeypatch)
    await sd.row_estimates()
    assert seen == [sd.ROW_ESTIMATE_SQL]
    sd.reset_row_estimates()


async def test_row_estimates_fail_soft_when_the_warehouse_is_down(monkeypatch):
    _wh_mode(monkeypatch)
    _record_sql(monkeypatch, fail=WarehouseUnreachable())
    assert await sd.row_estimates() == {}
    sd.reset_row_estimates()


async def test_table_names_read_the_snapshot_in_warehouse_mode(monkeypatch):
    _wh_mode(monkeypatch)
    seen = _record_sql(monkeypatch, rows=[{"schema_name": "public", "table_name": "orders"}])
    pairs, truncated = await sd.table_names()
    assert seen == [sd.WAREHOUSE_TABLE_NAMES_SQL]
    assert pairs == [("public", "orders")] and truncated is False


async def test_table_names_direct_path_unchanged(monkeypatch):
    _wh_mode(monkeypatch, False)
    seen = _record_sql(monkeypatch)
    await sd.table_names()
    assert seen == [sd.TABLE_NAMES_SQL]


# ── Quick 008 Runs page x Phase 14 task_runs kind='sync' ───────────────────

def test_sync_runs_are_their_own_kind():
    assert "WHEN r.kind = 'sync' THEN 'sync'" in runsapi.AGENT_KIND_SQL
    assert runsapi.AGENT_KIND_SQL.index("'sync'") < runsapi.AGENT_KIND_SQL.index("'whatsapp'")
    it = runsapi.agent_item({"id": 7, "thread_id": "sync", "status": "running", "kind": "sync"})
    assert it["title"] == "Data sync" and it["kind_label"] == "Data sync"
    assert it["family"] == "intake"
    assert "sync" in runsapi.parse_kinds("intake")


def test_sync_runs_get_a_sync_first_step():
    assert runsapi._step(10**9, "running", "sync") == runsapi.SYNC_FIRST_STEP
    assert runsapi._step(10**9, "done", "sync") is None


# ── Quick 003 task_timeout setting x Phase 14 sync timeout ────────────────

def test_sync_timeout_floor_is_the_live_task_timeout(monkeypatch):
    monkeypatch.setenv("SYNC_TASK_TIMEOUT_SECONDS", "100")
    monkeypatch.setenv("TASK_TIMEOUT_SECONDS", "900")
    config.get_settings.cache_clear()
    try:
        assert worker._sync_timeout() == 900
    finally:
        monkeypatch.delenv("SYNC_TASK_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("TASK_TIMEOUT_SECONDS", raising=False)
        config.get_settings.cache_clear()


def test_sync_branch_holds_a_live_step_before_its_heartbeat():
    proc = inspect.getsource(worker._process_one)
    start = proc.index('runstatus.start(task["id"])')
    assert start < proc.index('if task["kind"] == "sync"') < proc.index('if task["kind"] == "playbook"')


# ── Quick 008 loop errors x Phase 14 warehouse loop ────────────────────────

def test_warehouse_loop_records_sweep_errors():
    assert jobruns.LOOP_NAMES["warehouse"]
    assert 'jobruns.loop_error("warehouse", e)' in inspect.getsource(warehouse.run_forever)
