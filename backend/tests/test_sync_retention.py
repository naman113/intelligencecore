"""CONS-06: sync_runs and stage_* retention. Pure: fake pool and fake owner connection."""
import types

import pytest

from app import consolidation, whsources, warehouse


def _settings(dry=True):
    return types.SimpleNamespace(retention_dry_run=dry, retention_days=90)


class Pool:
    def __init__(self, pending=(), recent=()):
        self.sql = []
        self.pending, self.recent = pending, recent

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        return 7

    async def execute(self, sql, *a):
        self.sql.append(sql)
        return "DELETE 3"

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        if "FROM actions" in sql:
            return [{"n": n} for n in self.pending]
        return [{"n": n} for n in self.recent]


class Conn:
    def __init__(self, schemas, rels, lock=True):
        self.schemas, self.rels, self.lock = schemas, rels, lock
        self.closed = False

    async def fetch(self, sql, *a):
        if "pg_namespace WHERE nspname" in sql:
            return [{"nspname": s} for s in self.schemas]
        if "relkind = 'S'" in sql:
            return []
        return self.rels.get(a[0], [])

    async def fetchval(self, sql, *a):
        return self.lock

    async def close(self):
        self.closed = True


def test_sync_runs_predicate_keep_rules():
    p = consolidation._SYNC_RUNS_PRED
    assert "status IN ('ok','partial','failed')" in p
    assert "rn <= 20" in p
    assert "max(id)" in p and "'ok','partial'" in p
    assert "queued" not in p and "running" not in p


async def test_sync_dry_run_counts_only(monkeypatch):
    pool = Pool()

    async def gp():
        return pool

    async def stages(**kw):
        assert kw["dry_run"] is True
        return {"stage_schemas": 2, "stage_tables": 5}

    monkeypatch.setattr(consolidation, "get_pool", gp)
    monkeypatch.setattr(consolidation, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(whsources, "prune_stage_schemas", stages)
    out = await consolidation.sync_retention()
    assert out["dry_run"] and out["sync_runs"] == 7
    assert out["stage_schemas"] == 2 and out["stage_tables"] == 5
    assert all(q.lstrip().upper().startswith("SELECT") for q in pool.sql)


async def test_sync_live_deletes_and_failures_are_soft(monkeypatch):
    pool = Pool()

    async def gp():
        return pool

    async def boom(**kw):
        raise RuntimeError("x")

    monkeypatch.setattr(consolidation, "get_pool", gp)
    monkeypatch.setattr(consolidation, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(whsources, "prune_stage_schemas", boom)
    out = await consolidation.sync_retention()
    assert out["sync_runs"] == 3 and out["stage_schemas"] == -1
    assert any("DELETE FROM sync_runs" in q for q in pool.sql)


async def test_no_warehouse_skips_stage_pruning(monkeypatch):
    async def gp():
        return Pool()

    async def never(**kw):
        raise AssertionError("must not run")

    monkeypatch.setattr(consolidation, "get_pool", gp)
    monkeypatch.setattr(consolidation, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    monkeypatch.setattr(whsources, "prune_stage_schemas", never)
    out = await consolidation.sync_retention()
    assert out["stage_schemas"] == 0 and out["sync_runs"] == 7


def _wire(monkeypatch, pool, conn):
    stmts = []

    async def gp():
        return pool

    async def oc(roles):
        return conn

    async def ef(c, template, *values):
        stmts.append((template, values))

    monkeypatch.setattr(whsources, "get_pool", gp)
    monkeypatch.setattr(whsources, "_owner_conn", oc)
    monkeypatch.setattr(whsources, "exec_fmt", ef)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    return stmts


RELS = {"stage_old": [{"relname": "t1", "relkind": "r"}, {"relname": "v1", "relkind": "v"}],
        "stage_old_staging": [{"relname": "t2", "relkind": "r"}],
        "stage_kept": [{"relname": "t3", "relkind": "r"}],
        "stage_fresh": [{"relname": "t4", "relkind": "r"}]}
SCHEMAS = ["stage_fresh", "stage_kept", "stage_old", "stage_old_staging"]


async def test_prune_dry_run_drops_nothing(monkeypatch):
    stmts = _wire(monkeypatch, Pool(pending=["kept"], recent=["fresh"]), Conn(SCHEMAS, RELS))
    out = await whsources.prune_stage_schemas(dry_run=True)
    assert out["schemas"] == ["stage_old", "stage_old_staging"]
    assert out["stage_schemas"] == 2 and out["stage_tables"] == 2
    assert stmts == []


async def test_prune_live_drops_views_first_without_cascade(monkeypatch):
    stmts = _wire(monkeypatch, Pool(pending=["kept"], recent=["fresh"]), Conn(SCHEMAS, RELS))
    out = await whsources.prune_stage_schemas(dry_run=False)
    assert out["stage_schemas"] == 2
    templates = [t for t, _ in stmts]
    assert not any("CASCADE" in t.upper() for t in templates)
    assert templates[0].startswith("DROP VIEW") and templates[1].startswith("DROP TABLE")
    assert templates[2] == "DROP SCHEMA IF EXISTS %I"
    assert all(v[0] in ("stage_old", "stage_old_staging") for _, v in stmts)


async def test_prune_busy_lock_skips(monkeypatch):
    stmts = _wire(monkeypatch, Pool(), Conn(["stage_old"], RELS, lock=False))
    out = await whsources.prune_stage_schemas(dry_run=False)
    assert out["busy"] == ["old"] and out["stage_schemas"] == 0 and stmts == []


def test_prune_relkind_is_selected_as_text():
    """asyncpg returns the "char" type as bytes, which never equals "r"; the query casts it."""
    import inspect
    src = inspect.getsource(whsources.prune_stage_schemas)
    assert "c.relkind::text AS relkind" in src
