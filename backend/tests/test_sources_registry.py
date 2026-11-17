"""Phase 15 (SRC-01): settings defaults, schedule rules, name rules and the business_db table sync."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import config, configservice, sourcesvc

Z = ZoneInfo("UTC")


def _t(h, m=0, d=1):
    return datetime(2026, 1, d, h, m, tzinfo=Z)


def test_settings_defaults(monkeypatch):
    for k in ("SYNC_IMAGE", "SYNC_NETWORK"):
        monkeypatch.delenv(k, raising=False)
    s = config.Settings(_env_file=None)
    assert (s.sync_image, s.sync_network, s.sync_csv_network) == ("agentic-sync:latest", "", "")
    assert (s.sync_warehouse_host, s.sync_warehouse_port, s.sync_memory, s.sync_cpus) == (
        "agentic-warehouse", 5432, "1g", "1.0")
    assert (s.sync_sweep_seconds, s.sync_max_rows_per_run, s.sync_max_seconds_per_run,
            s.sync_chunk_size) == (60, 2_000_000, 1800, 50_000)
    assert (s.sync_default_interval_minutes, s.sync_reconcile_hour, s.sync_reconcile_max_rows,
            s.mssql_live_max_rows, s.csv_source_max_mb, s.sync_probe_timeout_seconds) == (
        60, 2, 200_000, 10_000, 100, 180)


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("SYNC_IMAGE", "x:1")
    monkeypatch.setenv("SYNC_NETWORK", "net")
    s = config.Settings(_env_file=None)
    assert s.sync_image == "x:1" and s.sync_network == "net"


TUNING = ["sync_sweep_seconds", "sync_max_rows_per_run", "sync_max_seconds_per_run", "sync_chunk_size",
          "sync_default_interval_minutes", "sync_reconcile_hour", "sync_reconcile_max_rows",
          "mssql_live_max_rows", "csv_source_max_mb", "sync_probe_timeout_seconds"]


def test_settings_registry_tuning_entries():
    for n in TUNING:
        e = configservice.REGISTRY[n]
        assert e["group"] == "tuning" and e["secret"] is False and e["required"] is False


def test_settings_deploy_fields_env_only():
    for n in ("sync_image", "sync_network", "sync_csv_network", "sync_warehouse_host",
              "sync_warehouse_port", "sync_memory", "sync_cpus"):
        assert n not in configservice.REGISTRY


@pytest.mark.parametrize("n", ["northwind_my", "ab"])
def test_validate_name_ok(n):
    assert sourcesvc.validate_name(n) is None


@pytest.mark.parametrize("n", ["", "a", "Upper", "1abc", "x" * 32, "business_db", "public", "model",
                               "warehouse", "ic_x", "pg_x", "src_x", "stage_x"])
def test_validate_name_bad(n):
    assert sourcesvc.validate_name(n) == sourcesvc.NAME_ERROR


def test_normalize_schedule_default_and_errors():
    assert sourcesvc.normalize_schedule({}) == {"interval_minutes": 60, "window": None, "reconcile_hour": 2}
    assert sourcesvc.normalize_schedule({"interval_minutes": 15, "window": [22, 6]})["window"] == [22, 6]
    for bad in ({"interval_minutes": 14}, {"interval_minutes": 10081}, {"window": [24, 3]},
                {"window": [5, 5]}, {"foo": 1}, {"reconcile_hour": 24}):
        with pytest.raises(ValueError):
            sourcesvc.normalize_schedule(bad)


def test_next_due_cases():
    assert sourcesvc.next_due({"interval_minutes": 60, "window": None}, _t(10)) == _t(11)
    assert sourcesvc.next_due({"interval_minutes": 60, "window": [22, 6]}, _t(7, 30)) == _t(22)
    assert sourcesvc.next_due({"interval_minutes": 60, "window": [22, 6]}, _t(23, 10)) == _t(0, 10, 2)
    assert sourcesvc.next_due({"interval_minutes": 60, "window": [6, 22]}, _t(21, 30)) == _t(6, 0, 2)


class FakePool:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    async def fetchval(self, sql, *a):
        return 1

    async def fetch(self, sql, *a):
        return [{"warehouse_schema": s, "warehouse_table": t, "status": st} for (s, t), st in self.rows.items()]

    async def execute(self, sql, *a):
        self.writes.append((sql, a))


def _wire(monkeypatch, pool, tables):
    from app import schemadiscovery

    async def intro(source="business_db"):
        return {"tables": [{"schema": s, "table": t} for s, t in tables]}

    async def gp():
        return pool
    monkeypatch.setattr(schemadiscovery, "introspect", intro)
    monkeypatch.setattr(schemadiscovery, "business_db_configured", lambda: True)  # Quick 031
    monkeypatch.setattr(sourcesvc, "get_pool", gp)


async def test_sync_inserts_then_idempotent_then_missing(monkeypatch):
    tabs = [("public", "a"), ("public", "b"), ("public", "c")]
    pool = FakePool({})
    _wire(monkeypatch, pool, tabs)
    assert (await sourcesvc.sync_business_db_tables())["inserted"] == 3
    pool = FakePool({t: "active" for t in tabs})
    _wire(monkeypatch, pool, tabs)
    r = await sourcesvc.sync_business_db_tables()
    assert pool.writes == [] and r["unchanged"] == 3
    _wire(monkeypatch, pool, tabs[:2])
    r = await sourcesvc.sync_business_db_tables()
    assert r["missing"] == 1 and "UPDATE" in pool.writes[0][0] and "DELETE" not in pool.writes[0][0]
    pool = FakePool({**{t: "active" for t in tabs[:2]}, tabs[2]: "missing"})
    _wire(monkeypatch, pool, tabs)
    assert (await sourcesvc.sync_business_db_tables())["restored"] == 1


async def test_sync_never_raises(monkeypatch):
    from app import schemadiscovery

    async def boom(source="business_db"):
        raise RuntimeError("x")
    monkeypatch.setattr(schemadiscovery, "introspect", boom)
    monkeypatch.setattr(schemadiscovery, "business_db_configured", lambda: True)  # Quick 031
    assert await sourcesvc.sync_business_db_tables() == {"error": "RuntimeError"}


async def test_list_and_has_active(monkeypatch):
    class P:
        n = 0

        async def fetch(self, sql, *a):
            if "FROM sources" in sql:
                return [{"id": 1, "name": "business_db", "config": '{"legacy": true}'},
                        {"id": 2, "name": "x", "config": "{}"}]
            return [{"source_id": 1, "warehouse_table": "a"}]

        async def fetchval(self, sql, *a):
            self.n += 1
            return True
    p = P()

    async def gp():
        return p
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    out = await sourcesvc.list_sources()
    assert out[0]["name"] == "business_db" and out[0]["config"] == {"legacy": True}
    assert out[0]["tables"][0]["warehouse_table"] == "a" and out[1]["tables"] == []
    sourcesvc._reset_cache_for_tests()
    assert await sourcesvc.has_active_sources() and await sourcesvc.has_active_sources()
    assert p.n == 1
