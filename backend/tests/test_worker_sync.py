"""Phase 14 (WH-07): kind='sync' task_runs have their own handler, timeout and heartbeat window."""
import asyncio

import pytest

from app import config, heartbeat, worker


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        self.pool.queries.append(sql)
        if "UPDATE task_runs SET status = 'running'" in sql:
            return self.pool.task
        return None


class _Acq:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


class Pool:
    def __init__(self, kind="sync"):
        self.task = {"id": 7, "thread_id": "sync", "message_id": None, "kind": kind,
                     "playbook_id": None, "case_id": None, "expectation_id": None}
        self.queries = []
        self.executed = []
        self.conn = _Conn(self)

    def acquire(self):
        return _Acq(self.conn)

    async def execute(self, sql, *args):
        self.queries.append(sql)
        self.executed.append((sql, args))

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        return 0


@pytest.fixture
def wired(monkeypatch):
    pool = Pool()

    async def gp():
        return pool
    monkeypatch.setattr(worker, "get_pool", gp)
    busy = []
    monkeypatch.setattr(heartbeat, "busy", lambda n, w: busy.append((n, w)))
    from app import playbooks

    async def no_pb(task):
        raise AssertionError("playbook runner called for a sync task")
    monkeypatch.setattr(playbooks, "run_playbook_task", no_pb)
    monkeypatch.delenv("SYNC_TASK_TIMEOUT_SECONDS", raising=False)
    config.get_settings.cache_clear()
    yield pool, busy
    config.get_settings.cache_clear()


def _failed(pool):
    return [a for sql, a in pool.executed if "status='failed'" in sql]


async def test_sync_task_reaches_sourcesvc_through_lane(wired, monkeypatch):
    pool, busy = wired
    from app import sourcesvc
    seen = []

    async def rs(task):
        seen.append(task["id"])
    monkeypatch.setattr(sourcesvc, "run_sync_task", rs)
    assert await worker._process_sync_one() is True
    assert seen == [7] and not any("FROM messages" in q for q in pool.queries)
    assert busy == [("sync", worker._sync_lane_timeout() + 300)]
    assert ("worker", 900) not in busy


async def test_missing_image_text_is_still_produced(wired, monkeypatch):
    pool, _ = wired
    from app import sourcesvc, syncrunner

    async def gs(name):
        return {"id": 1, "name": "s", "kind": "mysql", "credential": "c", "status": "active",
                "config": {}, "schedule": {}, "tables": [
                    {"mode": "import", "status": "active", "remote_table": "t", "warehouse_table": "t"}]}

    async def load(name):
        return {}

    async def net(cmd):
        return "n"

    async def run(*a, **k):
        raise syncrunner.SyncImageMissing()

    class P(Pool):
        async def fetchval(self, sql, *a):
            return "s"

        def acquire(self):
            outer = self

            class C:
                async def __aenter__(s):
                    return s

                async def __aexit__(s, *a):
                    return False

                def transaction(s):
                    return C()

                async def fetchrow(s, sql, *a):
                    if "FROM sync_runs" in sql:
                        return {"id": 1, "source_id": 1, "kind": "sync"}
                    return outer.task

                async def execute(s, sql, *a):
                    outer.executed.append((sql, a))
            return C()
    p2 = P()

    async def gp():
        return p2
    from app import sourcevault
    monkeypatch.setattr(worker, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "get_pool", gp)
    monkeypatch.setattr(sourcesvc, "get_source", gs)
    monkeypatch.setattr(sourcevault, "load", load)
    monkeypatch.setattr(syncrunner, "network_for", net)
    monkeypatch.setattr(syncrunner, "source_env", lambda c: {})
    monkeypatch.setattr(syncrunner, "loader_env", lambda: {})
    monkeypatch.setattr(syncrunner, "run", run)
    assert await worker._process_sync_one() is True
    assert _failed(p2) and (7, "sync runner not installed") in _failed(p2)


def test_sync_timeout_values(monkeypatch):
    monkeypatch.delenv("SYNC_TASK_TIMEOUT_SECONDS", raising=False)
    config.get_settings.cache_clear()
    assert worker._sync_timeout() == 3600
    monkeypatch.setenv("SYNC_TASK_TIMEOUT_SECONDS", "100")
    config.get_settings.cache_clear()
    assert worker._sync_timeout() == 600
    monkeypatch.setenv("SYNC_TASK_TIMEOUT_SECONDS", "7200")
    config.get_settings.cache_clear()
    assert worker._sync_timeout() == 7200
    config.get_settings.cache_clear()


async def test_sync_exception_marks_failed(wired, monkeypatch):
    pool, _ = wired
    from app import sourcesvc

    async def fo(tid, err):
        return 1
    monkeypatch.setattr(sourcesvc, "fail_open_run", fo)

    async def boom(task):
        raise RuntimeError("kaput")
    monkeypatch.setattr(worker, "_run_sync_task", boom)
    assert await worker._process_sync_one() is True
    assert _failed(pool) == [(7, "RuntimeError: kaput")]


async def test_sync_timeout_marks_failed(wired, monkeypatch):
    pool, _ = wired
    from app import sourcesvc

    async def fo(tid, err):
        return 1
    monkeypatch.setattr(sourcesvc, "fail_open_run", fo)

    async def slow(task):
        await asyncio.sleep(5)
    monkeypatch.setattr(worker, "_run_sync_task", slow)
    monkeypatch.setattr(worker, "_sync_lane_timeout", lambda: 0.05)
    assert await worker._process_sync_one() is True
    assert _failed(pool) == [(7, "sync lane timed out")]


async def test_playbook_keeps_600_timeout(wired, monkeypatch):
    pool, busy = wired
    pool.task["kind"] = "playbook"
    seen = {}
    from app import playbooks

    async def pb(task):
        return None
    monkeypatch.setattr(playbooks, "run_playbook_task", pb)
    real = asyncio.wait_for

    async def spy(aw, timeout):
        seen["t"] = timeout
        return await real(aw, timeout)
    monkeypatch.setattr(worker.asyncio, "wait_for", spy)
    assert await worker._process_one() is True
    assert seen["t"] == worker.TASK_TIMEOUT_SECONDS == 600
    assert busy == [("worker", 900)]


def test_orphan_recovery_fails_running_sync_rows():
    import inspect
    src = inspect.getsource(worker.run_forever)
    assert "orphaned sync run (not auto-retried)" in src
    assert "kind = 'sync'" in src


def test_sync_timeout_is_live_tuning_setting():
    from app import configservice
    e = configservice.REGISTRY["sync_task_timeout_seconds"]
    assert e["group"] == "tuning" and "live" in e["label"]
    assert config.Settings().sync_task_timeout_seconds == 3600
