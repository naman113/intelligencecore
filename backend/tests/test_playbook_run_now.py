"""Quick 023: POST /api/playbooks/{id}/run (Run now), the single-flight enqueue and
the cron interaction. Route tests use a fake pool; the real-Postgres block (skipped
without DASH_TEST_DATABASE_URL, same switch as the other *_db tests) proves the
concurrent double call queues exactly one run."""
import asyncio
import json
import os
import secrets
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import playbooks, usage
from app.config import get_settings

KEY = {"x-ops-key": "test-key"}
CRON = json.dumps({"hour": 2, "type": "cron"})


class Pool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *a):
        return self.row


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_KEY", "test-key")
    get_settings.cache_clear()
    yield TestClient(main.app)
    get_settings.cache_clear()


def _wire(monkeypatch, row, paused=None, once=(77, True)):
    async def gp():
        return Pool(row)

    async def reason():
        return paused

    async def state(pool=None):
        return {"resets_at": "2026-10-27T00:00:00+00:00"}
    calls = []

    async def enq(pid):
        calls.append(pid)
        return once
    monkeypatch.setattr(main.db, "get_pool", gp)
    monkeypatch.setattr(usage, "background_pause_reason", reason)
    monkeypatch.setattr(usage, "budget_state", state)
    monkeypatch.setattr(playbooks, "enqueue_playbook_run_once", enq)
    return calls


def pb(status="active", trigger=CRON):
    return {"id": 3, "name": "explore_data", "status": status, "trigger": trigger}


def test_auth_guard(client):
    assert client.post("/api/playbooks/3/run").status_code == 401
    assert client.post("/api/playbooks/3/run", headers={"x-ops-key": "bad"}).status_code == 401


def test_queues_a_run(client, monkeypatch):
    calls = _wire(monkeypatch, pb())
    r = client.post("/api/playbooks/3/run", headers=KEY)
    assert r.status_code == 200 and r.json() == {"task_run_id": 77}
    assert calls == [3]


def test_trigger_may_arrive_as_dict(client, monkeypatch):
    _wire(monkeypatch, pb(trigger={"type": "cron", "hour": 2}))
    assert client.post("/api/playbooks/3/run", headers=KEY).status_code == 200


def test_unknown_is_404(client, monkeypatch):
    calls = _wire(monkeypatch, None)
    r = client.post("/api/playbooks/9/run", headers=KEY)
    assert r.status_code == 404 and calls == []


@pytest.mark.parametrize("status,words", [("archived", "archived"),
                                          ("draft", "Activate this playbook first")])
def test_not_active_is_409_and_queues_nothing(client, monkeypatch, status, words):
    calls = _wire(monkeypatch, pb(status))
    r = client.post("/api/playbooks/3/run", headers=KEY)
    assert r.status_code == 409 and words in r.json()["detail"]["message"]
    assert calls == []


def test_event_playbook_is_422(client, monkeypatch):
    calls = _wire(monkeypatch, pb(trigger=json.dumps({"type": "expectation_violated"})))
    r = client.post("/api/playbooks/3/run", headers=KEY)
    assert r.status_code == 422 and calls == []


def test_already_running_is_409_with_the_run_id(client, monkeypatch):
    _wire(monkeypatch, pb(), once=(41, False))
    r = client.post("/api/playbooks/3/run", headers=KEY)
    assert r.status_code == 409
    assert r.json()["detail"]["task_run_id"] == 41


def test_budget_pause_is_423_with_reason_and_reset(client, monkeypatch):
    calls = _wire(monkeypatch, pb(),
                  paused="Today's AI budget is used up. Paused until midnight (UTC).")
    r = client.post("/api/playbooks/3/run", headers=KEY)
    d = r.json()["detail"]
    assert r.status_code == 423 and "midnight" in d["message"] and d["resets_at"]
    assert calls == []


# -- cron: a run in flight is today's run, never a second one ----------------

async def test_cron_skips_when_a_run_is_in_flight(monkeypatch):
    from app import expectations
    state, enq = {}, []

    class P:
        async def fetch(self, sql, *a):
            return [{"id": 3, "name": "explore_data", "trigger": CRON}]

    async def gp():
        return P()

    async def load():
        return state

    async def save(s):
        pass

    async def once(pid):
        enq.append(pid)
        return 41, False
    monkeypatch.setattr(expectations, "get_settings", lambda: types.SimpleNamespace(
        consolidation_enabled=False, consolidation_hour=9, anthropic_api_key="k",
        openai_api_key="", timezone="UTC", expectation_poll_seconds=60))
    monkeypatch.setattr(expectations, "get_pool", gp)
    monkeypatch.setattr(expectations, "_load_cron_state", load)
    monkeypatch.setattr(expectations, "_save_cron_state", save)
    monkeypatch.setattr(expectations, "_local_now",
                        lambda: datetime(2026, 10, 26, 2, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(playbooks, "enqueue_playbook_run_once", once)
    assert await expectations._dispatch_cron() == 0
    assert enq == [3] and state["3"] == "2026-10-26"  # marked done; nothing queued


# -- real Postgres: the race -------------------------------------------------

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
needs_db = pytest.mark.skipif(not os.getenv("DASH_TEST_DATABASE_URL"),
                              reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)")


@pytest.fixture
async def pool(monkeypatch):
    import asyncpg
    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "q023_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    p = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        sp = urlsplit(admin_url)
        url = urlunsplit((sp.scheme, sp.netloc, "/" + name, sp.query, sp.fragment))
        c = await asyncpg.connect(url)
        await c.execute(SCHEMA.read_text(encoding="utf-8"))
        await c.close()
        p = await asyncpg.create_pool(url, min_size=1, max_size=12)

        async def gp():
            return p
        monkeypatch.setattr(playbooks, "get_pool", gp)
        yield p
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _playbook(pool, name):
    return await pool.fetchval(
        "INSERT INTO playbooks (name, trigger, goal, status) "
        "VALUES ($1, $2::jsonb, 'g', 'active') RETURNING id", name, CRON)


@needs_db
async def test_concurrent_double_call_queues_one_run(pool):
    pid = await _playbook(pool, "x")
    res = await asyncio.gather(*[playbooks.enqueue_playbook_run_once(pid) for _ in range(8)])
    assert sum(1 for _, created in res if created) == 1
    assert len({rid for rid, _ in res}) == 1
    assert await pool.fetchval("SELECT count(*) FROM task_runs WHERE playbook_id = $1", pid) == 1


@needs_db
async def test_only_an_active_run_blocks_and_only_its_own_playbook(pool):
    pid = await _playbook(pool, "y")
    first, made = await playbooks.enqueue_playbook_run_once(pid)
    again, made2 = await playbooks.enqueue_playbook_run_once(pid)
    assert made and not made2 and again == first
    await pool.execute("UPDATE task_runs SET status = 'running' WHERE id = $1", first)
    assert (await playbooks.enqueue_playbook_run_once(pid))[1] is False
    await pool.execute("UPDATE task_runs SET status = 'failed' WHERE id = $1", first)
    nxt, made3 = await playbooks.enqueue_playbook_run_once(pid)
    assert made3 and nxt != first
    other = await _playbook(pool, "z")
    assert (await playbooks.enqueue_playbook_run_once(other))[1] is True
