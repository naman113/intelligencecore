"""Phase 15 (SRC-02, SRC-10): /api/sources routes."""
import datetime
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import secretbox, sourcesapi, sourcesvc, sourcevault
from app.config import get_settings

PW = "hunter2-longsecret"
NOW = datetime.datetime(2026, 1, 1)
BODY = {"name": "erp_my", "kind": "mysql", "host": "h.example", "port": 3306, "database": "erp",
        "user": "ro", "password": PW}
VIEW = {"handle": "cred:erp_my", "name": "erp_my", "kind": "mysql",
        "display": {"host": "h.example", "port": 3306, "database": "erp", "user": "ro"},
        "read_only": True, "verify_status": "ok", "verify_detail": None, "verified_at": None}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "k" * 20)
    get_settings.cache_clear()
    yield TestClient(main.app, headers={"x-ops-key": "k" * 20})
    get_settings.cache_clear()


def _no_secret(resp):
    assert PW not in resp.text and '"password"' not in resp.text


def test_requires_ops_key(client):
    bare = TestClient(main.app)
    for m, p in (("get", "/api/sources"), ("get", "/api/sources/credentials"),
                 ("post", "/api/sources/credentials"), ("get", "/api/sources/x/runs"),
                 ("delete", "/api/sources/credentials/x"),
                 ("post", "/api/sources/credentials/x/verify")):
        assert getattr(bare, m)(p).status_code == 401, p


def test_save_ok_no_password(client, monkeypatch):
    async def save(name, kind, fields):
        assert fields["password"] == PW
        return VIEW
    monkeypatch.setattr(sourcevault, "save", save)
    r = client.post("/api/sources/credentials", json=BODY)
    assert r.status_code == 200 and r.json()["handle"] == "cred:erp_my"
    _no_secret(r)


def test_save_validation_422_and_key_409(client, monkeypatch):
    async def bad(*a):
        raise ValueError("host is invalid")

    monkeypatch.setattr(sourcevault, "save", bad)
    r = client.post("/api/sources/credentials", json=BODY)
    assert r.status_code == 422 and "host is invalid" in r.text
    _no_secret(r)

    async def nokey(*a):
        raise secretbox.KeyMissing("x")

    monkeypatch.setattr(sourcevault, "save", nokey)
    r = client.post("/api/sources/credentials", json=BODY)
    assert r.status_code == 409 and "SETTINGS_ENCRYPTION_KEY" in r.text


def test_list_credentials(client, monkeypatch):
    async def lh():
        return [VIEW]
    monkeypatch.setattr(sourcevault, "list_handles", lh)
    r = client.get("/api/sources/credentials")
    assert r.json() == {"credentials": [VIEW]}
    _no_secret(r)


def test_verify_and_404(client, monkeypatch):
    async def v(name):
        if name != "erp_my":
            raise LookupError("x")
        return VIEW
    monkeypatch.setattr(sourcevault, "verify", v)
    assert client.post("/api/sources/credentials/erp_my/verify").status_code == 200
    assert client.post("/api/sources/credentials/nope/verify").status_code == 404


def test_delete_codes(client, monkeypatch):
    async def d(name):
        if name == "gone":
            raise LookupError("x")
        if name == "used":
            raise ValueError(sourcevault.IN_USE)
        return {"handle": "cred:" + name, "deleted": True}
    monkeypatch.setattr(sourcevault, "delete", d)
    assert client.delete("/api/sources/credentials/ok").status_code == 200
    assert client.delete("/api/sources/credentials/gone").status_code == 404
    r = client.delete("/api/sources/credentials/used")
    assert r.status_code == 409 and "active source" in r.text


class Pool:
    def __init__(self):
        self.args = []

    async def fetch(self, sql, *a):
        s = " ".join(sql.split())
        self.args.append(a)
        if "DISTINCT ON" in s:
            return [{"id": 9, "source_id": 2, "kind": "sync", "status": "failed", "started_at": NOW,
                     "ended_at": NOW, "rows_loaded": 5, "error": "bad " + PW}]
        if "row_number" in s:
            return [{"source_id": 2, "status": "failed"}, {"source_id": 2, "status": "failed"}]
        if "source" in s and "unreachable" in s:
            return [{"component": "source_unreachable:erp_my"}]
        if "max(ended_at)" in s:
            return [{"source_id": 2, "t": NOW}]
        if "FROM sync_runs" in s:
            return [{"id": 9, "kind": "sync", "status": "ok", "started_at": NOW, "ended_at": NOW,
                     "duration_ms": 5, "rows_loaded": 5, "tables": json.dumps([{"t": "a"}]),
                     "error": None}]
        return []


@pytest.fixture
def pool(monkeypatch):
    p = Pool()

    async def gp():
        return p

    monkeypatch.setattr(sourcesapi, "get_pool", gp)
    secretbox.add_known([PW])
    yield p
    secretbox._known.clear()
    secretbox._extra_known.clear()


def test_list_sources_shape(client, pool, monkeypatch):
    tbl = {"warehouse_schema": "erp_my", "warehouse_table": "orders", "remote_schema": None,
           "remote_table": "orders", "mode": "import", "cursor_column": "id", "merge_key": ["id"],
           "status": "active", "last_success_at": NOW, "rows_total": 5, "backfill_done": False,
           "backfill_progress": 0.5, "last_error": None}

    async def ls():
        return [{"id": 1, "name": "business_db", "kind": "postgres", "status": "active",
                 "credential": None, "tables": []},
                {"id": 2, "name": "erp_my", "kind": "mysql", "status": "active",
                 "credential": "erp_my", "warehouse_schema": "erp_my", "schedule": {},
                 "freshness_minutes": 60, "next_sync_at": None, "tables": [tbl]}]

    async def lh():
        return [VIEW]

    monkeypatch.setattr(sourcesvc, "list_sources", ls)
    monkeypatch.setattr(sourcevault, "list_handles", lh)
    r = client.get("/api/sources")
    assert r.status_code == 200
    src = r.json()["sources"]
    assert [s["name"] for s in src] == ["business_db", "erp_my"]
    e = src[1]
    assert e["credential"] == "cred:erp_my" and e["display"]["host"] == "h.example"
    assert e["last_run"]["status"] == "failed" and PW not in e["last_run"]["error"]
    assert e["last_success_at"] and e["backfill"] == {"done": False, "progress": 0.5}
    assert e["tables"][0]["mode"] == "import"
    assert "pending_mode" in e["tables"][0] and e["tables"][0]["pending_mode"] is None  # Phase 17 (CONS-05)
    assert src[0]["last_run"] is None and src[0]["credential"] is None
    assert e["failing"] is True and e["stale"] is False and e["unreachable"] is True
    assert src[0]["failing"] is False and src[0]["unreachable"] is False
    _no_secret(r)


def test_runs_decode_and_clamp(client, pool, monkeypatch):
    async def gs(name):
        return {"id": 2} if name == "erp_my" else None

    monkeypatch.setattr(sourcesvc, "get_source", gs)
    r = client.get("/api/sources/erp_my/runs?limit=500")
    assert r.status_code == 200 and r.json()["runs"][0]["tables"] == [{"t": "a"}]
    assert pool.args[-1] == (2, 100)
    client.get("/api/sources/erp_my/runs?limit=-3")
    assert pool.args[-1] == (2, 1)
    assert client.get("/api/sources/nope/runs").status_code == 404
