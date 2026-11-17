"""Phase 17 (CONS-05): business_db and pending imports on the Phase 15 sync lane."""
import logging
from datetime import date, datetime, timezone

import pytest

from app import sourcesvc, syncsched, syncrunner, whsources
from app.config import get_settings

PW = "s3cr3t-Pw-Zq9"
URL = f"postgresql://demo_ro:{PW}@db.example.com:5433/northwind?sslmode=require"
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _t(table, **kw):
    d = {"warehouse_schema": "public", "remote_schema": "public", "warehouse_table": table,
         "remote_table": table, "mode": "live", "pending_mode": None, "status": "active",
         "import_reset": False, "cursor_column": None, "merge_key": [], "backfill_done": False}
    d.update(kw)
    return d


def _bdb(tables):
    return {"id": 1, "name": "business_db", "kind": "postgres", "status": "active",
            "config": {"legacy": True}, "credential": None, "schedule": {}, "tables": tables}


@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setattr(get_settings(), "tenant_database_url", URL)


def test_job_for_pending_import_with_reset():
    s = _bdb([_t("sales_orders", pending_mode="import", import_reset=True, cursor_column="updated_at",
                 merge_key=["id"]), _t("regions")])
    cmd, job, stdin = sourcesvc.build_sync_job(s, s["tables"], "backfill")
    assert cmd == "sync" and stdin is None
    assert job["dataset"] == "ic_imp_business_db" and job["pipeline"] == "ic_business_db"
    assert job["remote_schema"] == "public" and job["kind"] == "postgres"
    assert len(job["tables"]) == 1
    t = job["tables"][0]
    assert t["remote_table"] == "sales_orders" and t["reset"] is True
    assert t["cursor"] == "updated_at" and t["merge_key"] == ["id"]


def test_business_db_two_schemas_refused_and_all_live_nothing():
    s = _bdb([_t("a", mode="import"), _t("b", pending_mode="import", remote_schema="sales",
                                         warehouse_schema="sales")])
    with pytest.raises(ValueError, match="one schema"):
        sourcesvc.build_sync_job(s, s["tables"], "sync")
    live = _bdb([_t("a"), _t("b")])
    with pytest.raises(ValueError, match="nothing to sync"):
        sourcesvc.build_sync_job(live, live["tables"], "sync")


def test_registered_source_includes_pending():
    s = {"id": 2, "name": "shop", "kind": "mysql", "status": "active", "config": {"remote_schema": "shop"},
         "tables": [_t("a", mode="import"), _t("b", pending_mode="import"), _t("c")]}
    _, job, _ = sourcesvc.build_sync_job(s, s["tables"], "backfill")
    assert {t["warehouse_table"] for t in job["tables"]} == {"a", "b"}
    assert job["remote_schema"] == "shop"
    assert all(t["reset"] is False for t in job["tables"])


def test_cred_from_tenant_url(tenant):
    c = sourcesvc.business_db_cred()
    assert c == {"host": "db.example.com", "port": "5433", "database": "northwind", "user": "demo_ro",
                 "password": PW, "options": {"sslmode": "require"}}
    env = syncrunner.source_env(c)
    assert env["IC_SRC_PASSWORD"] == PW and PW not in env["IC_SRC_OPTIONS"]


def test_cred_multi_host_refused(monkeypatch):
    monkeypatch.setattr(get_settings(), "tenant_database_url", f"postgresql://u:{PW}@a:5432,b:5432/db")
    with pytest.raises(ValueError, match="exactly one") as e:
        sourcesvc.business_db_cred()
    assert PW not in str(e.value)


class _Pool:
    def __init__(self):
        self.log = []

    async def execute(self, sql, *a):
        self.log.append((sql, a))
        return "UPDATE 1"

    async def fetchval(self, sql, *a):
        self.log.append((sql, a))
        return "sync"

    def sqls(self, needle):
        return [(s, a) for s, a in self.log if needle in s]


async def test_record_result_business_db(monkeypatch, caplog):
    pool, calls = _Pool(), {"cat": 0, "views": []}

    async def gp():
        return pool

    async def est(name, tabs):
        return {}

    async def views(name, names):
        calls["views"].append((name, list(names)))

    async def cat(conn, name):
        calls["cat"] += 1

    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "_estimate_rows", est)
    monkeypatch.setattr(whsources, "refresh_import_views", views)
    monkeypatch.setattr(whsources, "write_catalog", cat)
    s = _bdb([_t("sales_orders", pending_mode="import", import_reset=True),
              _t("invoices", mode="import", backfill_done=True), _t("regions")])
    res = {"status": "ok", "tables": [{"table": "sales_orders", "status": "ok", "rows": 5},
                                      {"table": "invoices", "status": "ok", "rows": 3},
                                      {"table": "regions", "status": "ok", "rows": 1}]}
    with caplog.at_level(logging.DEBUG):
        out = await sourcesvc.record_sync_result(9, s, s["tables"], res)
    assert out["status"] == "ok" and calls["cat"] == 0
    assert calls["views"] == [("business_db", ["invoices"])]  # pending keeps its live view
    ups = pool.sqls("UPDATE source_tables")
    assert len(ups) == 2  # the live regions row is not an import row, so it is skipped
    for sql, a in ups:
        assert "warehouse_schema=$6" in sql and "pending_mode = 'import'" in sql
        assert a[-1] == "public"
    assert all("import_reset=false" in sql for sql, _ in ups)
    assert PW not in caplog.text


async def test_run_sync_task_business_db_uses_cred_not_vault(tenant, monkeypatch):
    from app import sourcevault
    seen = {}

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class C:
        def transaction(self):
            return _Tx()

        async def fetchrow(self, sql, *a):
            return {"id": 8, "kind": "backfill", "source_id": 1}

        async def execute(self, sql, *a):
            pass

    class A:
        async def __aenter__(self):
            return C()

        async def __aexit__(self, *a):
            return False

    class P(_Pool):
        def acquire(self):
            return A()

        async def fetchval(self, *a):
            return "business_db"

    async def gp():
        return P()

    async def gs(n):
        return _bdb([_t("sales_orders", pending_mode="import")])

    async def run(cmd, job, env, *, name, network, timeout, stdin_path=None):
        seen.update(cmd=cmd, job=job, env=env)
        return {"status": "ok", "tables": []}

    async def net(cmd):
        return "n"

    async def rec(*a):
        return {"status": "ok"}

    async def load(h):
        raise AssertionError("vault must not be used for business_db")

    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    monkeypatch.setattr(sourcesvc, "record_sync_result", rec)
    monkeypatch.setattr(sourcevault, "load", load)
    monkeypatch.setattr(syncrunner, "run", run)
    monkeypatch.setattr(syncrunner, "network_for", net)
    monkeypatch.setattr(syncrunner, "loader_env", lambda: {"IC_WH_HOST": "wh"})
    await sourcesvc.run_sync_task({"id": 7})
    assert seen["env"]["IC_SRC_PASSWORD"] == PW and seen["env"]["IC_SRC_HOST"] == "db.example.com"
    assert PW not in repr(seen["job"])


def test_due_sql_and_run_kind():
    assert "business_db" not in syncsched._DUE_SQL
    assert "pending_mode = 'import'" in syncsched._DUE_SQL
    src = {"schedule": {"interval_minutes": 60, "reconcile_hour": 2}, "last_reconcile_on": date(2026, 5, 1)}
    done = {"mode": "import", "status": "active", "backfill_done": True}
    pend = {"mode": "live", "pending_mode": "import", "status": "active", "backfill_done": False}
    assert syncsched._run_kind(src, [done], NOW) == "sync"
    assert syncsched._run_kind(src, [done, pend], NOW) == "backfill"


async def test_tools_for_importing_business_db(monkeypatch):
    from app import sourcetools as st
    live = _bdb([_t("a"), _t("b")])
    assert "always current" in st._source_line(live)
    imp = _bdb([_t("a", mode="import"), _t("b", pending_mode="import")])
    line = st._source_line(imp)
    assert "always current" not in line and "a [import]" in line

    async def gs(n):
        return live
    monkeypatch.setattr(st.sourcesvc, "get_source", gs)

    async def lg(*a, **k):
        return None
    monkeypatch.setattr(st, "_log_tool", lg)
    out = await st.request_sync.ainvoke({"name": "business_db"})
    assert "business_db is live; nothing to sync" in out

    async def gs2(n):
        return imp

    async def en(sid, kind, conn=None):
        return 4

    async def audit(*a, **k):
        return None
    monkeypatch.setattr(st.sourcesvc, "get_source", gs2)
    monkeypatch.setattr(st.syncsched, "enqueue_now", en)
    monkeypatch.setattr(st, "_audit", audit)
    assert await st.request_sync.ainvoke({"name": "business_db"}) == "sync run #4 queued"
