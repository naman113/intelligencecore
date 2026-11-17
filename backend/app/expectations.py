"""Expectation scheduler — the ambient heartbeat.

Every expectation_poll_seconds it does two things:

  1. Violation sweep: open expectations whose due_at has passed are marked
     'violated' and a handler playbook is enqueued (the expectation's own
     playbook_id, else the active playbook whose trigger is expectation_violated).
     The followup_reminder playbook then reschedules or escalates via its tools.

  2. Cron dispatch: active playbooks with a {"type":"cron","hour":H} trigger are
     enqueued once per day when the hour matches (e.g. the daily digest). Quick 047:
     an optional "minute" (fire at or after HH:MM within that hour) and "tz" (an
     IANA zone instead of the operator's) for scheduled reports.

Read + enqueue only — all side effects happen inside playbook runs, tier-gated.
Follows worker.py loop conventions (crash-tolerant, blip backoff, never dies).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import asyncpg

from . import jobruns  # Quick 008: scheduler work shows on Runs
from .config import get_settings
from .db import get_pool

log = logging.getLogger("expectations")

CRON_STATE_KEY = "playbook_cron_state"


# ── violation sweep ────────────────────────────────────────────────────────

async def _handler_playbook_id(conn, expectation) -> int | None:
    if expectation["playbook_id"]:
        pid = await conn.fetchval(
            "SELECT id FROM playbooks WHERE id = $1 AND status = 'active'", expectation["playbook_id"])
        if pid:
            return pid
    return await conn.fetchval(
        """SELECT id FROM playbooks WHERE status = 'active'
             AND trigger->>'type' = 'expectation_violated'
           ORDER BY id LIMIT 1""")


async def _sweep_violations() -> int:
    from .playbooks import enqueue_playbook_run

    pool = await get_pool()
    async with pool.acquire() as conn:
        due = await conn.fetch(
            # Sandbox (test-thread) cases are excluded: a violated expectation
            # enqueues a playbook run that may SEND autonomous messages — test
            # fixtures must never fire real reminders. Quick 024: nor does an
            # expectation the assistant owes (its reply meets it; there is nobody
            # to remind), even if a test hook pushed a due date onto it.
            """SELECT e.id, e.case_id, e.playbook_id FROM expectations e
               JOIN cases c ON c.id = e.case_id
               WHERE e.status = 'open' AND e.due_at IS NOT NULL AND e.due_at < now()
                 AND NOT c.sandbox AND e.owed_by IS DISTINCT FROM 'assistant'
               ORDER BY e.due_at LIMIT 50""")

    fired = 0
    no_handler = 0  # Quick 008
    for exp in due:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Atomically claim the violation so it can't be enqueued twice.
            claimed = await conn.fetchval(
                """UPDATE expectations SET status = 'violated', last_checked_at = now()
                   WHERE id = $1 AND status = 'open' RETURNING id""", exp["id"])
            if not claimed:
                continue
            pid = await _handler_playbook_id(conn, exp)
        if pid:
            await enqueue_playbook_run(pid, case_id=exp["case_id"], expectation_id=exp["id"])
            fired += 1
        else:
            log.warning("expectation %s violated but no handler playbook is active", exp["id"])
            no_handler += 1
    if fired:
        log.info("violation sweep: %d expectation(s) → playbook runs", fired)
    if fired or no_handler:  # Quick 008
        await jobruns.record(
            "followup_sweep", "Overdue follow-ups", trigger="due date passed",
            status="failed" if no_handler else "done",
            summary=f"{fired} overdue expectation(s) handed to follow-up runs",
            error=(f"{no_handler} overdue expectation(s) have no active follow-up playbook"
                   if no_handler else None),
            counts={"queued": fired, "no_handler": no_handler})
    return fired


# ── cron dispatch ──────────────────────────────────────────────────────────

async def _load_cron_state() -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT config FROM agent_configs WHERE agent_name = $1", CRON_STATE_KEY)
    if not row or not row["config"]:
        return {}
    try:
        return json.loads(row["config"]) if isinstance(row["config"], str) else dict(row["config"])
    except (json.JSONDecodeError, TypeError):
        return {}


async def _save_cron_state(state: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO agent_configs (agent_name, config) VALUES ($1, $2::jsonb)
           ON CONFLICT (agent_name) DO UPDATE SET config = $2::jsonb, updated_at = now()""",
        CRON_STATE_KEY, json.dumps(state))


def _local_now() -> datetime:
    """Wall-clock time in the deployment's configured zone.

    "Daily digest at 09:00" means 09:00 where the client is. Comparing against
    UTC delivered it at 14:30 local for an IST client.
    """
    s = get_settings()
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(s.timezone))
    except Exception:
        log.warning("invalid timezone %r — falling back to UTC", s.timezone)
        return datetime.now(timezone.utc)


# ── Quick 047: minute and zone on a cron trigger ──
def cron_due(trig: dict, now: datetime) -> tuple[bool, str, str]:
    """(due, local day, label) for a cron trigger at `now` (aware, operator zone).

    Due when the local hour equals `hour` and the local minute is at least
    `minute` (default 0, so an hour-only trigger keeps its meaning). `tz` moves
    the clock to that IANA zone; an unknown zone falls back to the operator's.
    The day is the trigger's own local date, so "done today" follows its zone.
    A DST-skipped hour does not fire that day and a repeated hour fires once
    (crontab(5) runs a repeated time twice). Raises ValueError, TypeError or
    KeyError on a malformed trigger; the caller skips that playbook."""
    hour = int(trig["hour"])
    minute = int(trig.get("minute") or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("cron hour or minute out of range")
    tz = trig.get("tz")
    local = now
    if tz:
        try:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(str(tz)))
        except Exception:
            local = now
    label = f"daily at {hour:02d}:{minute:02d}" + (f" {tz}" if tz else "")
    return local.hour == hour and local.minute >= minute, local.date().isoformat(), label
# ── End Quick 047 ──


async def _dispatch_cron() -> int:
    from .playbooks import enqueue_playbook_run_once

    now = _local_now()
    today = now.date().isoformat()
    # A zero-config box must stay clean: without an LLM key every dispatched run
    # fails immediately (playbooks builds its agent outside the try), so a fresh
    # install would accumulate one mysterious failed task_run per day before the
    # operator has configured anything. That gate applies ONLY to LLM-dependent
    # work (playbook dispatch, weekly review) — the nightly consolidation/
    # retention hook below runs regardless: retention is pure SQL and every
    # LLM/graph step inside run_nightly is individually fail-soft.
    s = get_settings()
    have_llm = bool(s.anthropic_api_key or s.openai_api_key)
    pool = await get_pool()

    state = await _load_cron_state()
    fired = 0
    if have_llm:
        rows = await pool.fetch(
            "SELECT id, name, trigger FROM playbooks WHERE status = 'active' AND trigger->>'type' = 'cron'")
        for pb in rows:
            trig = pb["trigger"]
            trig = json.loads(trig) if isinstance(trig, str) else trig
            try:  # Quick 047: minute and zone; a malformed trigger skips one playbook
                due, day, when = cron_due(trig, now)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            if not due:
                continue
            if state.get(str(pb["id"])) == day:
                continue  # already ran today
            # Quick 023: a run already queued or running (a manual "Run now")
            # counts as today's run; never queue a second one beside it.
            run_id, created = await enqueue_playbook_run_once(pb["id"])
            state[str(pb["id"])] = day
            if not created:
                log.info("cron dispatch: playbook %s already has run %s in flight; skipped",
                         pb["name"], run_id)
                continue
            fired += 1
            log.info("cron dispatch: enqueued playbook %s", pb["name"])
            await jobruns.record("playbook_dispatch", pb["name"],  # Quick 008
                                 trigger=when,
                                 summary=f"Queued agent run #{run_id}",
                                 ref=("playbook", pb["id"]), task_run_id=run_id)
        # Weekly review of closed cases for repeating patterns → draft playbook
        # proposals (plan §8.5). Runs at most once per ISO week, near the top of
        # hour 10. LLM-dependent, so it stays inside the have_llm gate.
        week = now.strftime("%G-W%V")
        if now.hour == 10 and state.get("_playbook_review_week") != week:
            try:
                from .memory_pipeline import propose_playbooks
                async with jobruns.track("playbook_review", "Weekly playbook review",  # Quick 008
                                         trigger="weekly", live=True) as job:
                    await propose_playbooks()
                    job.ok("Reviewed closed cases for repeating patterns; proposals wait on Playbooks")
                state["_playbook_review_week"] = week   # mark done only after it ran
                fired += 1
            except Exception:
                log.exception("weekly playbook review failed (will retry next sweep)")

    # Nightly consolidation + retention (consolidation.py). At most once per
    # day, at the configured local hour, independent of cron playbooks and LLM
    # keys. run_nightly is per-step fail-soft and returns per-step results; the
    # day is marked done unless EVERY step failed (a night where the DB was
    # briefly down at the configured hour retries on the next sweep).
    if (getattr(s, "consolidation_enabled", False)
            and now.hour == int(getattr(s, "consolidation_hour", 3))
            and state.get("_consolidation_day") != today):
        try:
            from . import consolidation
            async with jobruns.track("consolidation", "Nightly consolidation",  # Quick 008
                                     trigger=f"nightly at {now.hour:02d}:00", live=True) as job:
                results = await consolidation.run_nightly()
                text, counts, failed = consolidation.summarize_nightly(results)
                if results and all(v == "error" for v in results.values()):
                    job.fail("every step failed; it will retry on the next sweep", text, counts)
                elif failed:
                    job.fail("some steps failed: " + ", ".join(failed), text, counts)
                else:
                    job.ok(text, counts)
            if results and all(v == "error" for v in results.values()):
                log.warning("nightly consolidation: every step failed — will retry next sweep")
            else:
                state["_consolidation_day"] = today   # mark done only after it ran
                fired += 1
        except Exception:
            log.exception("nightly consolidation failed (will retry next sweep)")

    if fired:
        await _save_cron_state(state)
    return fired


# ── loop ────────────────────────────────────────────────────────────────────

async def run_forever() -> None:
    log.info("expectation scheduler started (sweep every %ss)",
             get_settings().expectation_poll_seconds)
    while True:
        try:
            await _sweep_violations()
            await _dispatch_cron()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("scheduler DB connection blip, retrying: %s", type(e).__name__)
        except Exception as e:
            log.exception("scheduler loop error")
            await jobruns.loop_error("expectations", e)  # Quick 008 (never raises)
        await asyncio.sleep(max(15, get_settings().expectation_poll_seconds))
