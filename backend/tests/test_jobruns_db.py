"""Quick 008: real-Postgres checks for job_runs and the Runs API SQL.
Skipped unless DASH_TEST_DATABASE_URL is set (same gate as the other *_db tests).
Proves the schema block is idempotent, jobruns' collapse / live / recover /
prune statements run as written, and every runsapi query is valid SQL that
returns the right rows."""
import json
import os
import secrets
import types
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres job_runs tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def jdb(monkeypatch):
    import asyncpg

    from app import db, jobruns, runsapi

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "jobruns_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        conn = await asyncpg.connect(url)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.execute(sql)
        await conn.execute(sql)   # idempotent
        await conn.close()
        pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
        monkeypatch.setattr(db, "_pool", pool)
        monkeypatch.setattr(jobruns, "_last_prune", float("inf"))

        async def gp():
            return pool
        monkeypatch.setattr(runsapi, "get_pool", gp)
        yield types.SimpleNamespace(pool=pool, jobruns=jobruns, runsapi=runsapi)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_collapse_live_recover_and_prune(jdb):
    jr, pool = jdb.jobruns, jdb.pool
    for v in (1, 2, 3):
        async with jr.track("watch_check", "Stale orders", ref=("watch", 7),
                            trigger="every 15 min") as job:
            job.ok(f"stale = {v}", {"value": v}, routine=True)
    for n in (2, 5):
        await jr.record("db_poll", "Database poller", summary="rows", counts={"rows": n},
                        routine=True, collapse="sum")
    rows = await pool.fetch("SELECT * FROM job_runs ORDER BY id")
    assert [(r["kind"], r["repeats"]) for r in rows] == [("watch_check", 3), ("db_poll", 2)]
    assert json.loads(rows[0]["counts"]) == {"value": 3}
    assert json.loads(rows[1]["counts"]) == {"rows": 7}

    async with jr.track("consolidation", "Nightly consolidation", live=True) as job:
        running = await pool.fetchrow("SELECT status, trace_id FROM job_runs WHERE id = $1", job.id)
        assert running["status"] == "running"
        job.ok("Merged 0 learning cluster(s)")
    done = await pool.fetchrow("SELECT * FROM job_runs WHERE kind = 'consolidation'")
    assert done["status"] == "done" and done["trace_id"] == f"job:{done['id']}"
    assert done["duration_ms"] is not None and done["ended_at"] is not None

    left = await jr._start_row(jr.Job(kind="triage", name="3 new message(s)"))
    assert await jr.recover() == 1
    assert (await pool.fetchrow("SELECT status, error FROM job_runs WHERE id = $1", left))["error"] \
        == jr.INTERRUPTED

    # Collapse window: a row last seen 7 h ago is not collapsed into.
    await pool.execute("UPDATE job_runs SET last_at = now() - interval '7 hours' WHERE kind = 'watch_check'")
    async with jr.track("watch_check", "Stale orders", ref=("watch", 7)) as job:
        job.ok("stale = 4", routine=True)
    assert await pool.fetchval("SELECT count(*) FROM job_runs WHERE kind = 'watch_check'") == 2

    # Prune: routine after 7 days, the rest after 30, running never.
    await pool.execute("""INSERT INTO job_runs (kind, status, routine, last_at) VALUES
        ('watch_check', 'done', true, now() - interval '8 days'),
        ('watch_check', 'done', false, now() - interval '8 days'),
        ('loop', 'failed', false, now() - interval '31 days'),
        ('triage', 'running', true, now() - interval '40 days')""")
    assert await jr.prune() == 2
    kept = await pool.fetch("SELECT kind, status, routine FROM job_runs WHERE last_at < now() - interval '7 days'")
    assert sorted((k["kind"], k["status"]) for k in kept) == [("triage", "running"), ("watch_check", "done")]


async def _seed_runs(pool):
    pb = await pool.fetchval("SELECT id FROM playbooks WHERE name = 'explore_data'")
    async with pool.acquire() as c:
        for tid in ("ui:owner", "playbook-global", "1203@g.us"):
            await c.execute("INSERT INTO threads (thread_id, phone) VALUES ($1, $1) ON CONFLICT DO NOTHING", tid)
        await c.execute("UPDATE threads SET title = 'Fuel costs' WHERE thread_id = 'ui:owner'")
        await c.execute("INSERT INTO chats (jid, subject) VALUES ('1203@g.us', 'Accounts team') ON CONFLICT DO NOTHING")
        m1 = await c.fetchval("INSERT INTO messages (thread_id, role, content) VALUES ('ui:owner','user','How did sales do?') RETURNING id")
        m2 = await c.fetchval("INSERT INTO messages (thread_id, role, content) VALUES ('playbook-global','system','playbook run') RETURNING id")
        m3 = await c.fetchval("INSERT INTO messages (thread_id, role, content) VALUES ('1203@g.us','user','hi') RETURNING id")
        chat = await c.fetchval("INSERT INTO task_runs (thread_id, message_id, status) VALUES ('ui:owner', $1, 'done') RETURNING id", m1)
        play = await c.fetchval("INSERT INTO task_runs (thread_id, message_id, status, kind, playbook_id) VALUES ('playbook-global', $1, 'running', 'playbook', $2) RETURNING id", m2, pb)
        wa = await c.fetchval("INSERT INTO task_runs (thread_id, message_id, status, error) VALUES ('1203@g.us', $1, 'failed', 'boom') RETURNING id", m3)
        await c.execute("""INSERT INTO tool_calls (task_run_id, tool, args) VALUES
            ($1, 'use_skill', '{"name": "exploratory-data-analysis"}'::jsonb),
            ($1, 'run_sql', '{"sql": "select 1"}'::jsonb),
            ($1, 'use_skill', '{"name": "kpi-selection"}'::jsonb),
            ($1, 'use_skill', '{"name": "exploratory-data-analysis"}'::jsonb)""", play)
    return pb, chat, play, wa


async def test_runs_api_sql_against_the_real_schema(jdb):
    jr, api, pool = jdb.jobruns, jdb.runsapi, jdb.pool
    pb, chat, play, wa = await _seed_runs(pool)
    await jr.record("playbook_dispatch", "explore_data", summary=f"Queued agent run #{play}",
                    ref=("playbook", pb), task_run_id=play, trigger="daily at 02:00")
    await jr.record("watch_check", "Stale orders", status="failed", error="relation gone",
                    ref=("watch", 7))

    feed = await api.activity(limit=50)
    titles = {i["uid"]: i["title"] for i in feed["items"]}
    assert titles[f"run-{chat}"] == "Chat: Fuel costs"
    assert titles[f"run-{play}"] == "Data exploration (playbook)"
    assert titles[f"run-{wa}"] == "WhatsApp: Accounts team"
    by_uid = {i["uid"]: i for i in feed["items"]}
    assert by_uid[f"run-{play}"]["skills_used"] == ["exploratory-data-analysis", "kpi-selection"]
    assert "Scheduled: Data exploration" in titles.values()
    assert feed["next_before"] is None

    only_pb = await api.activity(ref="playbook:explore_data")
    assert sorted(i["kind"] for i in only_pb["items"]) == ["playbook", "playbook_dispatch"]
    assert [i["uid"] for i in (await api.activity(kind="watches"))["items"]][0].startswith("job-")
    failed = await api.activity(status="failed", since="24h")
    assert {i["kind"] for i in failed["items"]} == {"whatsapp", "watch_check"}
    chats = await api.activity(kind="chat")
    assert [i["uid"] for i in chats["items"]] == [f"run-{chat}"]

    page = await api.activity(limit=2)
    assert len(page["items"]) == 2 and page["next_before"]
    rest = await api.activity(limit=50, before=page["next_before"])
    assert {i["uid"] for i in page["items"]}.isdisjoint({i["uid"] for i in rest["items"]})
    assert len(page["items"]) + len(rest["items"]) == len(feed["items"])

    now = await api.activity_now()
    assert [i["uid"] for i in now["running"]] == [f"run-{play}"]
    assert now["counts"]["running"] == 1 and now["counts"]["failed_24h"] == 2
    assert {i["kind"] for i in now["failed"]} == {"whatsapp", "watch_check"}

    detail = await api.activity_run(play)
    assert detail["ref"]["to"] == "/playbooks" and detail["trigger"] == "schedule (daily 02:00)"
    job_id = await pool.fetchval("SELECT id FROM job_runs WHERE kind = 'watch_check'")
    jd = await api.activity_job(job_id)
    assert jd["ref"]["label"] == "Watch #7 (deleted)"
