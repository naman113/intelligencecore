"""Phase 15 (SRC-05): the scheduler sweep. It only enqueues task_runs kind='sync' and reaps ledger
rows whose task already ended; the worker's sync lane executes. Never-die loop."""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from . import sourcesvc, warehouse
from .config import get_settings
from .db import get_pool

log = logging.getLogger(__name__)

BUSINESS_EVERY = 10
REPROVISION_EVERY = 10

_sweep_n = 0
_reprovision: set[str] = set()

_INFLIGHT = "ON CONFLICT (source_id) WHERE status IN ('queued','running') DO NOTHING"

# Phase 17 (CONS-05): business_db and pending imports are due like registered sources
_DUE_SQL = """
SELECT s.* FROM sources s
WHERE s.status = 'active' AND s.kind <> 'csv'
  AND (s.next_sync_at IS NULL OR s.next_sync_at <= now())
  AND EXISTS (SELECT 1 FROM source_tables t WHERE t.source_id = s.id AND (t.mode = 'import' OR t.pending_mode = 'import')
              AND t.status <> 'missing')
ORDER BY s.next_sync_at NULLS FIRST, s.id"""

_REAP_SQL = ("UPDATE sync_runs SET status='failed', ended_at=now(), error='sync run ended without a result' "
             "WHERE status IN ('queued','running') AND task_run_id IN "
             "(SELECT id FROM task_runs WHERE status IN ('done','failed'))")


async def _insert_run(conn, source_id: int, kind: str) -> int | None:
    await conn.execute("INSERT INTO threads (thread_id, phone) VALUES ('sync','sync') ON CONFLICT DO NOTHING")
    run_id = await conn.fetchval(
        f"INSERT INTO sync_runs (source_id, kind, status) VALUES ($1, $2, 'queued') {_INFLIGHT} RETURNING id",
        source_id, kind)
    if run_id is None:
        return None
    task_id = await conn.fetchval(
        "INSERT INTO task_runs (thread_id, message_id, kind) VALUES ('sync', NULL, 'sync') RETURNING id")
    await conn.execute("UPDATE sync_runs SET task_run_id=$2 WHERE id=$1", run_id, task_id)
    return run_id


async def enqueue_now(source_id: int, kind: str, *, conn=None) -> int | None:
    """The shared enqueue path: a queued sync_runs row plus its task_runs row, or None when a run
    is already in flight."""
    if conn is not None:
        return await _insert_run(conn, source_id, kind)
    pool = await get_pool()
    async with pool.acquire() as c:
        async with c.transaction():
            return await _insert_run(c, source_id, kind)


def _run_kind(src: dict, tables: list[dict], now_local) -> str:
    imp = [t for t in tables if t.get("mode") == "import" and t.get("status") != "missing"]
    # Phase 17 (CONS-05): a table waiting for its first full copy is a backfill
    if any(t.get("pending_mode") == "import" and t.get("status") != "missing" for t in tables):
        return "backfill"
    if any(not t.get("backfill_done") for t in imp):
        return "backfill"
    sched = sourcesvc._j(src.get("schedule")) or {}
    rh = sched.get("reconcile_hour", get_settings().sync_reconcile_hour)
    last = src.get("last_reconcile_on")
    if now_local.hour >= rh and (last is None or last < now_local.date()):
        return "reconcile"
    return "sync"


async def enqueue_due(now=None) -> list[int]:
    now_local = now or sourcesvc.local_now()
    pool = await get_pool()
    due = [sourcesvc._decode(r) for r in await pool.fetch(_DUE_SQL)]
    made: list[int] = []
    for src in due:
        try:
            tabs = [dict(r) for r in await pool.fetch(
                "SELECT mode, pending_mode, status, backfill_done FROM source_tables WHERE source_id=$1", src["id"])]
            kind = _run_kind(src, tabs, now_local)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rid = await _insert_run(conn, src["id"], kind)
                    if rid is None:
                        continue
                    nxt = sourcesvc.next_due(src.get("schedule") or {}, now_local)
                    await conn.execute(
                        "UPDATE sources SET next_sync_at=$2, updated_at=now() WHERE id=$1", src["id"], nxt)
                    made.append(rid)
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError):
            raise
        except Exception as e:
            log.warning("enqueue of source %s failed: %s", src.get("name"), type(e).__name__)
    return made


async def reap_stuck_runs() -> int:
    pool = await get_pool()
    r = await pool.execute(_REAP_SQL)
    try:
        return int(str(r).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def request_reprovision(name: str) -> None:
    _reprovision.add(name)


async def _reprovision_lost(force: bool) -> list[str]:
    from . import whsources
    pool = await get_pool()
    names = [r["name"] for r in await pool.fetch(
        "SELECT name FROM sources WHERE status='active' AND warehouse_schema IS NOT NULL "
        "AND name <> 'business_db'")]
    forced = [n for n in names if n in _reprovision]
    _reprovision.difference_update(forced)
    lost = await whsources.missing_objects(names) if force and names else []
    done = []
    for n in dict.fromkeys(lost + forced):
        res = await whsources.provision(n)
        done.append(n)
        if n in lost and res.get("status") not in ("error", "busy"):
            await pool.execute(
                "UPDATE source_tables SET backfill_done=false, backfill_progress=0, updated_at=now() "
                "WHERE mode='import' AND source_id=(SELECT id FROM sources WHERE name=$1)", n)
            await pool.execute("UPDATE sources SET next_sync_at=now() WHERE name=$1", n)
    return done


async def sweep() -> dict:
    global _sweep_n
    out: dict = {"reaped": await reap_stuck_runs()}
    _sweep_n += 1
    if not warehouse.warehouse_mode():
        return out
    if _sweep_n % BUSINESS_EVERY == 1:  # first sweep after start, then every 10th
        out["business_db"] = await sourcesvc.sync_business_db_tables()
    out["reprovisioned"] = await _reprovision_lost(_sweep_n % REPROVISION_EVERY == 0)
    out["enqueued"] = await enqueue_due()
    # Phase 17 (CONS-05): cut over finished imports
    try:
        from . import modeflip
        out["mode_switches"] = await modeflip.advance()
    except (asyncpg.PostgresConnectionError, ConnectionError, OSError):
        raise
    except Exception as e:
        log.warning("mode switch cutover failed: %s", type(e).__name__)
    # End Phase 17 (CONS-05)
    # Phase 15 (15-13): source alerts
    try:
        from . import sourcealerts
        out["alerts"] = await sourcealerts.evaluate()
    except (asyncpg.PostgresConnectionError, ConnectionError, OSError):
        raise
    except Exception as e:
        log.warning("source alert evaluation failed: %s", type(e).__name__)
    # End Phase 15 (15-13)
    return out


async def run_forever() -> None:
    log.info("source sync scheduler started")
    while True:
        try:
            await sweep()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("sync scheduler DB connection blip, retrying: %s", type(e).__name__)
        except Exception:
            log.exception("sync scheduler sweep error")
        await asyncio.sleep(max(1, get_settings().sync_sweep_seconds))
