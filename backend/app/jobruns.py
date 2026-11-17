"""Quick 008: one activity record for the work that runs outside task_runs.

The loops (watch checks, dashboard refresh, the scheduler, triage, the DB
poller, document intake, retention, health alerts) do real work that never
showed anywhere as a run. Each now records it here, in `job_runs`, and the Runs
page reads it next to the agent's `task_runs`.

This module is the only writer of job_runs. It is plumbing, never behaviour:
nothing reads a job row back into a decision and nothing claims from the table,
so task_runs stays the one execution path.

Rules that keep it safe inside loops that must never die:
  * Recording is fail-soft. Every DB call has a timeout and swallows its own
    errors (one warning a minute), so a broken recorder cannot stop a loop.
  * An exception raised by the tracked work is recorded as failed and re-raised
    unchanged: the loop sees exactly what it saw before.
  * It never opens a pool. It writes only once the app has opened the
    working-memory pool (startup does, before any loop starts), so the pure
    test suite never reaches a real database through it.
  * No-op ticks are never written: a tracked block that ends without an outcome
    records nothing. It is still listed by `active()` while it runs, which is
    how "running now" sees a slow watch query without a DB write.

Volume stays bounded. A routine success (and a repeat of the same failure)
updates the latest row for the same (kind, ref) instead of inserting, while
that row is fresh (last run under COLLAPSE_IDLE_HOURS ago, started under
COLLAPSE_SPAN_HOURS ago): repeats + 1, latest summary, counts replaced or
summed. Rows are pruned here at most hourly (routine after ROUTINE_KEEP_DAYS,
everything else after KEEP_DAYS, running never). This is the app's own
bounded activity log, not business or conversation data, so it does not wait
for `retention_dry_run` to be turned off.

Hook for later work (one line):
    await jobruns.record("forecast", name, summary="...", ref=("forecast", fid))
or, for work with a start and an end:
    async with jobruns.track("forecast", name, trigger="nightly", live=True) as job:
        ...; job.ok("12 series forecast", counts={"series": 12})
"""
import asyncio
import contextlib
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db

log = logging.getLogger("jobruns")

WRITE_TIMEOUT = 5.0
COLLAPSE_IDLE_HOURS = 6
COLLAPSE_SPAN_HOURS = 24
ROUTINE_KEEP_DAYS = 7
KEEP_DAYS = 30
PRUNE_EVERY_SECONDS = 3600
PRUNE_BATCH = 5000
PRUNE_MAX_BATCHES = 20
NAME_CHARS = 120
TRIGGER_CHARS = 60
SUMMARY_CHARS = 500
ERROR_CHARS = 500
REF_CHARS = 120
MAX_ACTIVE = 256
WARN_EVERY_SECONDS = 60
INTERRUPTED = "interrupted: the app restarted before it finished"
STOPPED = "stopped before it finished"

_active: dict[int, "Job"] = {}
_tokens = itertools.count(1)
_last_warn = 0.0
_last_prune: float | None = None
_bg: set = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text, n: int) -> str:
    s = "" if text is None else " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _err(exc: BaseException) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _ref(ref) -> tuple[str, str]:
    if not ref:
        return "", ""
    kind, rid = ref
    return _clip(kind, 40), _clip(rid, REF_CHARS)


def every(seconds) -> str:
    """'every 15 min' for a cadence in seconds (trigger text)."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "on a schedule"
    if s <= 0:
        return "on a schedule"
    if s % 86400 == 0:
        n = s // 86400
        return "every day" if n == 1 else f"every {n} days"
    if s % 3600 == 0:
        n = s // 3600
        return "every hour" if n == 1 else f"every {n} hours"
    if s >= 60:
        return f"every {round(s / 60)} min"
    return f"every {s} s"


def _counts(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in list(value.items())[:40]:
        if isinstance(v, bool) or v is None or isinstance(v, (int, float, str)):
            out[_clip(k, 60)] = _clip(v, 200) if isinstance(v, str) else v
        else:
            out[_clip(k, 60)] = _clip(json.dumps(v, default=str), 200)
    return out


def _sum_counts(old, new: dict) -> dict:
    base = old if isinstance(old, dict) else {}
    if isinstance(old, str):
        try:
            base = json.loads(old) or {}
        except ValueError:
            base = {}
    out = dict(base)
    for k, v in new.items():
        prev = out.get(k)
        if (isinstance(v, (int, float)) and not isinstance(v, bool)
                and isinstance(prev, (int, float)) and not isinstance(prev, bool)):
            out[k] = prev + v
        else:
            out[k] = v
    return out


def _warn(what: str, exc: BaseException) -> None:
    global _last_warn
    now = time.monotonic()
    if now - _last_warn >= WARN_EVERY_SECONDS:
        _last_warn = now
        log.warning("job record skipped (%s): %s", what, _err(exc))


def _pool_or_none():
    """The app's pool once startup opened it; never opens one itself."""
    return getattr(db, "_pool", None)


async def _safe(what: str, fn):
    """Run fn(pool) with a timeout; None on any failure or when no pool is open."""
    pool = _pool_or_none()
    if pool is None:
        return None
    try:
        return await asyncio.wait_for(fn(pool), WRITE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the recorder never raises into a loop
        _warn(what, e)
        return None


# ── the job ───────────────────────────────────────────────────────────────

@dataclass
class Job:
    kind: str
    name: str = ""
    trigger: str = ""
    ref_type: str = ""
    ref_id: str = ""
    live: bool = False
    id: int | None = None
    status: str | None = None          # done | failed | None (nothing to record)
    summary: str = ""
    error: str | None = None
    counts: dict = field(default_factory=dict)
    routine: bool = False
    collapse: str | None = None        # None | "replace" | "sum"
    same_error: bool = True            # a failure collapses only into the same error
    task_run_id: int | None = None
    trace_id: str | None = None
    started_at: datetime = field(default_factory=_now)
    t0: float = field(default_factory=time.monotonic)
    token: int = 0

    def ok(self, summary="", counts=None, *, routine: bool = False,
           collapse: str | None = None) -> None:
        """Record a success. Routine successes collapse (counts replaced) unless
        collapse says otherwise ("sum" adds numeric counts)."""
        self.status = "done"
        self.summary = _clip(summary, SUMMARY_CHARS)
        self.error = None
        self.counts = _counts(counts)
        self.routine = bool(routine)
        self.collapse = collapse or ("replace" if routine else None)

    def fail(self, error, summary=None, counts=None, *, collapse: bool = True,
             same_error: bool = True) -> None:
        """Record a failure. A repeat of the same error collapses by default."""
        self.status = "failed"
        self.error = _clip(_err(error) if isinstance(error, BaseException) else error,
                           ERROR_CHARS) or "failed"
        if summary is not None:
            self.summary = _clip(summary, SUMMARY_CHARS)
        if counts is not None:
            self.counts = _counts(counts)
        self.collapse = "replace" if collapse else None
        self.same_error = same_error

    def skip(self) -> None:
        """Nothing worth recording happened (the default for a non-live job)."""
        self.status = None

    def link(self, *, task_run_id=None, trace_id=None, ref=None, name=None) -> None:
        if task_run_id is not None:
            self.task_run_id = task_run_id
        if trace_id:
            self.trace_id = _clip(trace_id, REF_CHARS)
        if ref:
            self.ref_type, self.ref_id = _ref(ref)
        if name:
            self.name = _clip(name, NAME_CHARS)

    async def go_live(self, name=None) -> None:
        """Turn a quiet job into a visible running row once it has real work
        (a triage tick that found messages). Fail-soft like everything here."""
        if name:
            self.name = _clip(name, NAME_CHARS)
        if not self.live:
            self.live = True
            self.id = await _start_row(self)

    def view(self) -> dict:
        return {"token": self.token, "id": self.id, "kind": self.kind, "name": self.name,
                "trigger": self.trigger, "ref_type": self.ref_type or None,
                "ref_id": self.ref_id or None, "live": self.live,
                "started_at": self.started_at.isoformat()}


def active() -> list[dict]:
    """Every tracked job in flight in this process (the gateway is one process)."""
    try:
        return [j.view() for j in list(_active.values())]
    except Exception:  # noqa: BLE001
        return []


def _register(job: Job) -> None:
    job.token = next(_tokens)
    if len(_active) < MAX_ACTIVE:
        _active[job.token] = job


def _unregister(job: Job) -> None:
    _active.pop(job.token, None)


# ── writes ────────────────────────────────────────────────────────────────

_START_SQL = """INSERT INTO job_runs (kind, name, trigger, status, ref_type, ref_id, started_at, last_at)
VALUES ($1, $2, $3, 'running', $4, $5, $6, now()) RETURNING id"""

_LAST_SQL = """SELECT id, status, error, routine, counts,
       (last_at > now() - make_interval(hours => $4)
        AND started_at > now() - make_interval(hours => $5)) AS fresh
  FROM job_runs WHERE kind = $1 AND ref_type = $2 AND ref_id = $3
 ORDER BY id DESC LIMIT 1"""

_BUMP_SQL = """UPDATE job_runs SET repeats = repeats + 1, summary = $2, error = $3,
       counts = $4::jsonb, duration_ms = $5, trigger = $6, name = $7,
       task_run_id = COALESCE($8, task_run_id), trace_id = COALESCE($9, trace_id),
       ended_at = now(), last_at = now()
 WHERE id = $1"""

_INSERT_SQL = """INSERT INTO job_runs (kind, name, trigger, status, summary, error, counts,
       ref_type, ref_id, task_run_id, trace_id, routine, duration_ms, started_at, ended_at, last_at)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13, $14, now(), now())
RETURNING id"""

_FINISH_SQL = """UPDATE job_runs SET status = $2, summary = $3, error = $4, counts = $5::jsonb,
       name = $6, trigger = $7, ref_type = $8, ref_id = $9, task_run_id = $10,
       trace_id = $11, routine = $12, duration_ms = $13, ended_at = now(), last_at = now()
 WHERE id = $1"""


async def _start_row(job: Job) -> int | None:
    async def go(pool):
        return await pool.fetchval(_START_SQL, job.kind, job.name, job.trigger,
                                   job.ref_type, job.ref_id, job.started_at)
    return await _safe("start", go)


def _duration_ms(job: Job) -> int:
    return max(0, int((time.monotonic() - job.t0) * 1000))


async def _write(job: Job) -> int | None:
    """Persist a finished job: update its live row, collapse into the previous
    row, or insert. Returns the row id (None when nothing was written)."""
    if job.status is None and job.id is None:
        return None
    status = job.status or "done"
    ms = _duration_ms(job)
    counts_json = json.dumps(job.counts, default=str)

    async def go(pool):
        if job.id is not None:
            await pool.execute(_FINISH_SQL, job.id, status, job.summary, job.error,
                               counts_json, job.name, job.trigger, job.ref_type, job.ref_id,
                               job.task_run_id, job.trace_id, job.routine, ms)
            return job.id
        if job.collapse:
            last = await pool.fetchrow(_LAST_SQL, job.kind, job.ref_type, job.ref_id,
                                       COLLAPSE_IDLE_HOURS, COLLAPSE_SPAN_HOURS)
            if last is not None and last["fresh"] and last["status"] == status and (
                    (status == "done" and last["routine"] and job.routine)
                    or (status == "failed" and (not job.same_error
                                                or last["error"] == job.error))):
                counts = (_sum_counts(last["counts"], job.counts) if job.collapse == "sum"
                          else job.counts)
                await pool.execute(_BUMP_SQL, last["id"], job.summary, job.error,
                                   json.dumps(counts, default=str), ms, job.trigger, job.name,
                                   job.task_run_id, job.trace_id)
                return last["id"]
        return await pool.fetchval(
            _INSERT_SQL, job.kind, job.name, job.trigger, status, job.summary, job.error,
            counts_json, job.ref_type, job.ref_id, job.task_run_id, job.trace_id,
            job.routine, ms, job.started_at)

    rid = await _safe("write", go)
    if rid is not None:
        _maybe_prune()
    return rid


def _trace_scope(job: Job):
    """LLM calls inside a live job link to the 005 usage trace 'job:<id>',
    unless the work sets its own trace key. Import-soft."""
    if job.id is None:
        return contextlib.nullcontext()
    try:
        from . import usage
        job.trace_id = job.trace_id or f"job:{job.id}"
        return usage.attribute(trace_id=job.trace_id)
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext()


@contextlib.asynccontextmanager
async def track(kind: str, name="", *, trigger="", ref=None, live: bool = False):
    """Track one piece of background work. See the module docstring."""
    job = Job(kind=_clip(kind, 40), name=_clip(name, NAME_CHARS),
              trigger=_clip(trigger, TRIGGER_CHARS), live=live)
    job.ref_type, job.ref_id = _ref(ref)
    _register(job)
    try:
        if live:
            job.id = await _start_row(job)
        with _trace_scope(job):
            try:
                yield job
            except asyncio.CancelledError:
                if job.id is not None:
                    job.fail(STOPPED, collapse=False)
                    with contextlib.suppress(BaseException):
                        await _write(job)
                raise
            except Exception as e:
                job.fail(e if job.status != "failed" else job.error)
                await _write(job)
                raise
        if job.live and job.status is None:
            job.status = "done"
        await _write(job)
    finally:
        _unregister(job)


async def record(kind: str, name="", *, status: str = "done", summary="", error=None,
                 counts=None, trigger="", ref=None, routine: bool = False,
                 collapse: str | None = None, task_run_id=None, trace_id=None,
                 started_at: datetime | None = None, duration_ms: int | None = None,
                 same_error: bool = True) -> int | None:
    """One-shot record of something that already happened. Never raises."""
    try:
        job = Job(kind=_clip(kind, 40), name=_clip(name, NAME_CHARS),
                  trigger=_clip(trigger, TRIGGER_CHARS))
        job.ref_type, job.ref_id = _ref(ref)
        if started_at is not None:
            job.started_at = started_at
        if duration_ms is not None:
            job.t0 = time.monotonic() - max(0, duration_ms) / 1000
        if status == "failed":
            job.fail(error or summary or "failed", summary=summary, counts=counts,
                     collapse=bool(collapse), same_error=same_error)
        else:
            job.ok(summary, counts, routine=routine, collapse=collapse)
        job.link(task_run_id=task_run_id, trace_id=trace_id)
        return await _write(job)
    except Exception as e:  # noqa: BLE001
        _warn("record", e)
        return None


LOOP_NAMES = {
    "sentinel": "Watch sweep", "dashboard_refresh": "Dashboard refresh loop",
    "expectations": "Scheduler", "triage": "Triage loop", "connectors": "Data intake loop",
    "docintel": "Document loop", "retention": "File clean-up loop", "healthwatch": "Health watch",
    "worker": "Agent worker",
    "warehouse": "Warehouse link loop",  # M2 sync: Phase 14 warehouse.run_forever
}


async def loop_error(loop: str, exc: BaseException) -> None:
    """A loop's sweep raised (its `except Exception` branch). Repeats collapse
    into one row per loop and day, whatever the message."""
    await record("loop", LOOP_NAMES.get(loop, loop), status="failed", error=_err(exc),
                 trigger="loop sweep", ref=("loop", loop), collapse="replace",
                 same_error=False)


async def recover() -> int:
    """At startup: rows left running by a process that died are failed. The
    gateway is one process, so nothing else can still be running them."""
    async def go(pool):
        status = await pool.execute(
            """UPDATE job_runs SET status = 'failed', error = COALESCE(error, $1),
                      ended_at = now(), last_at = now()
                WHERE status = 'running'""", INTERRUPTED)
        try:
            return int(str(status).split()[-1])
        except (ValueError, IndexError):
            return 0
    n = await _safe("recover", go)
    return n or 0


# ── retention ─────────────────────────────────────────────────────────────

_PRUNE_SQL = """DELETE FROM job_runs WHERE id IN (
    SELECT id FROM job_runs
     WHERE status <> 'running'
       AND ((routine AND last_at < now() - make_interval(days => $1))
            OR last_at < now() - make_interval(days => $2))
     ORDER BY id LIMIT $3)"""


async def prune() -> int:
    """Delete old rows in batches. Returns rows deleted (0 when it could not run)."""
    total = 0
    for _ in range(PRUNE_MAX_BATCHES):
        async def go(pool):
            return await pool.execute(_PRUNE_SQL, ROUTINE_KEEP_DAYS, KEEP_DAYS, PRUNE_BATCH)
        status = await _safe("prune", go)
        try:
            n = int(str(status).split()[-1]) if status else 0
        except (ValueError, IndexError):
            n = 0
        total += n
        if n < PRUNE_BATCH:
            break
    if total:
        log.info("job_runs retention: deleted %d old row(s)", total)
    return total


def _maybe_prune() -> None:
    """At most hourly, in the background, so a write never waits for a delete."""
    global _last_prune
    now = time.monotonic()
    if _last_prune is not None and now - _last_prune < PRUNE_EVERY_SECONDS:
        return
    _last_prune = now
    try:
        task = asyncio.get_running_loop().create_task(prune())
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    except Exception as e:  # noqa: BLE001
        _warn("prune", e)
