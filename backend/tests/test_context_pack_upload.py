"""Phase 12 (CTX-08): POST /api/documents/upload for context packs, and the reflection skip.

Pure logic: fake pool recording executes; scratch dir pointed at tmp_path; no network."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import sandbox, worker

INSTRUCTION = ("This file is a context pack. Import it with the context specialist using the "
               "exact filename shown above: propose only, apply nothing, and tell me what is "
               "waiting for my approval.")


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        self.pool.executes.append((sql, args))

    async def fetchval(self, sql, *args):
        self.pool.fetchvals.append((sql, args))
        return 41 + len(self.pool.fetchvals)  # msg id 42, run id 43

    @asynccontextmanager
    async def transaction(self):
        yield


class _Pool:
    def __init__(self):
        self.executes, self.fetchvals = [], []

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self)


@pytest.fixture
def env(monkeypatch, tmp_path):
    pool = _Pool()

    async def gp():
        return pool

    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=gp))
    monkeypatch.setattr(sandbox, "scratch_dir", lambda run_id: _mk(tmp_path, run_id))
    main.app.dependency_overrides[main._require_ops_key] = lambda: None
    yield pool, tmp_path, TestClient(main.app)
    main.app.dependency_overrides.pop(main._require_ops_key, None)


def _mk(tmp_path, run_id):
    d = tmp_path / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _post(client, name, data=b"{}", **form):
    return client.post("/api/documents/upload", files={"file": (name, data)}, data=form)


def test_pack_on_owner_thread(env):
    pool, tmp, client = env
    r = _post(client, "pack.json", b'{"a": 1}', operator="owner", instruction=INSTRUCTION)
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 43 and body["filename"] == "pack.json"
    thread_sqls = [a for s, a in pool.executes if "INSERT INTO threads" in s]
    assert thread_sqls == [("ui:owner",)]
    msg = [a for s, a in pool.fetchvals if "INSERT INTO messages" in s][0]
    assert msg == ("ui:owner", f"[document received: pack.json] {INSTRUCTION}")
    run = [a for s, a in pool.fetchvals if "INSERT INTO task_runs" in s][0]
    assert run[0] == "ui:owner"
    assert (tmp / "43" / "pack.json").read_bytes() == b'{"a": 1}'


def test_safe_name_everywhere(env):
    pool, tmp, client = env
    r = _post(client, "my pack (v2).yaml", b"a: 1", operator="owner", instruction=INSTRUCTION)
    safe = sandbox.safe_name("my pack (v2).yaml")
    assert r.json()["filename"] == safe
    msg = [a for s, a in pool.fetchvals if "INSERT INTO messages" in s][0]
    assert msg[1] == f"[document received: {safe}] {INSTRUCTION}"
    assert (tmp / "43" / safe).exists()


@pytest.mark.parametrize("name", ["a.md", "a.txt", "a.yml"])
def test_accepted_extensions(env, name):
    assert _post(env[2], name, b"x", operator="owner").status_code == 200


def test_rejects_exe_and_missing_file(env):
    pool, _, client = env
    assert _post(client, "evil.exe").status_code == 422
    assert client.post("/api/documents/upload", data={"operator": "owner"}).status_code == 422
    assert pool.executes == [] and pool.fetchvals == []


def test_invalid_operator_writes_nothing(env):
    pool, tmp, client = env
    assert _post(client, "pack.json", operator="Owner!").status_code == 422
    assert pool.executes == [] and pool.fetchvals == []
    assert list(tmp.iterdir()) == []


def test_no_operator_keeps_dashboard(env):
    pool, _, client = env
    assert _post(client, "pack.json").status_code == 200
    assert [a for s, a in pool.executes if "INSERT INTO threads" in s] == [("dashboard",)]


@pytest.mark.parametrize("name", ["big.json", "big.yaml", "big.yml", "big.md"])
def test_oversize_pack_413(env, name):
    pool, tmp, client = env
    r = _post(client, name, b"x" * 2_000_001, operator="owner")
    assert r.status_code == 413
    assert pool.executes == [] and pool.fetchvals == []
    assert list(tmp.iterdir()) == []


def test_large_pdf_unaffected(env):
    assert _post(env[2], "big.pdf", b"x" * 2_000_001).status_code == 200


# ── worker reflection skip ───────────────────────────────────────────────

def test_skip_reflection_for_pack_tools():
    for t in worker.CONTEXT_PACK_TOOLS:
        assert worker._skip_reflection_for_pack([{"tool": "run_sql"}, {"tool": t}])
    assert not worker._skip_reflection_for_pack([{"tool": "run_sql"}])
    assert not worker._skip_reflection_for_pack([])
