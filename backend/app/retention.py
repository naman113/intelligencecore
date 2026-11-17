"""Artifact retention — the only unbounded term in this system.

Every task run gets `artifacts/run_<id>/`, and nothing ever removed it. A CAD
deconstruction alone emits a layer-isolated PDF *and* a PNG preview per group,
so one drawing can leave tens of files behind. Left alone, disk is what fills
first on a document-heavy client — long before memory does.

Two rules, because either alone fails:

  age   — the honest primary rule. An artifact nobody fetched in two weeks is
          not going to be fetched.
  size  — the backstop age cannot provide. Forty drawings on one Tuesday blows
          the budget while every directory is still comfortably "recent".

Three things are never deleted, each for a concrete reason rather than caution:

  * The newest few runs per thread. `sandbox.carry_over` copies files from
    recent runs into a new one, which is what makes "here's revision A… now
    diff them" work across separate messages. Deleting inside that window
    breaks the feature.
  * Runs backing a `proposed` action or an un-recorded document. A human is
    still looking at those, and the file is the evidence behind the number
    they are being asked to approve.
  * Anything the age and size rules do not actually require removing. This
    sweep deletes the minimum, not everything it is permitted to.

Deletion is safe for carried-over files specifically because carry_over copies
(mtime-preserving copy2, independent inodes): removing the source directory
cannot touch the bytes a newer run carried forward.
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path

from . import jobruns  # Quick 008: clean-ups show on Runs
from .config import get_settings
from .db import get_pool
from .sandbox import ARTIFACTS_ROOT

log = logging.getLogger("retention")

# carry_over copies from the previous 3 runs on a thread; keep a margin.
KEEP_RECENT_PER_THREAD = 4


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue          # vanished mid-walk; it is not our problem
    return total


def _run_id(path: Path) -> int | None:
    name = path.name
    if not name.startswith("run_"):
        return None
    tail = name[4:]
    return int(tail) if tail.isdigit() else None      # 'run_adhoc' → None


async def _protected_run_ids() -> set[int]:
    """Runs whose artifacts must survive regardless of age or size pressure."""
    pool = await get_pool()
    protected: set[int] = set()

    rows = await pool.fetch(
        f"""SELECT id FROM (
              SELECT id, row_number() OVER (PARTITION BY thread_id ORDER BY id DESC) AS rn
              FROM task_runs) t
            WHERE rn <= {KEEP_RECENT_PER_THREAD}""")
    protected |= {r["id"] for r in rows}

    # A human is mid-decision on these; the file is the evidence.
    rows = await pool.fetch(
        """SELECT DISTINCT task_run_id FROM actions
            WHERE status = 'proposed' AND task_run_id IS NOT NULL""")
    protected |= {r["task_run_id"] for r in rows}

    rows = await pool.fetch(
        """SELECT DISTINCT task_run_id FROM documents
            WHERE status = 'extracted' AND task_run_id IS NOT NULL""")
    protected |= {r["task_run_id"] for r in rows}
    return protected


async def sweep() -> dict:
    """Delete what age and size require, and nothing more. Returns a summary."""
    settings = get_settings()
    root = ARTIFACTS_ROOT
    if not root.is_dir():
        return {"scanned": 0, "deleted": 0, "freed_mb": 0}

    protected = await _protected_run_ids()
    cutoff = time.time() - settings.artifact_retention_days * 86400

    entries = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        rid = _run_id(d)
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        entries.append({"path": d, "run_id": rid, "mtime": mtime,
                        "bytes": _dir_bytes(d),
                        # 'run_adhoc' has no run row and no owner; age alone governs it.
                        "protected": rid is not None and rid in protected})

    deleted, freed = 0, 0

    def _remove(e) -> None:
        nonlocal deleted, freed
        try:
            shutil.rmtree(e["path"])
            deleted += 1
            freed += e["bytes"]
            e["gone"] = True
        except OSError:
            log.warning("could not remove %s", e["path"], exc_info=True)

    # 1. Age.
    for e in entries:
        if not e["protected"] and e["mtime"] < cutoff:
            _remove(e)

    # 2. Size backstop — oldest first, still respecting the protections.
    cap = int(settings.artifact_max_gb * 1024 ** 3)
    total = sum(e["bytes"] for e in entries if not e.get("gone"))
    if total > cap:
        for e in sorted((x for x in entries if not x.get("gone") and not x["protected"]),
                        key=lambda x: x["mtime"]):
            if total <= cap:
                break
            _remove(e)
            total -= e["bytes"]
        if total > cap:
            # Worth saying out loud: the cap is being held open by directories
            # we are not allowed to touch, so raising the cap is the only fix.
            log.warning("artifacts still %.1fGB over cap after sweep — protected "
                        "runs account for the remainder", (total - cap) / 1024 ** 3)

    summary = {"scanned": len(entries), "deleted": deleted,
               "freed_mb": round(freed / 1024 ** 2, 1),
               "remaining_mb": round(total / 1024 ** 2, 1)}
    if deleted:
        log.info("artifact sweep: %s", summary)
    return summary


async def run_forever() -> None:
    """Hourly sweep. Follows the loop convention in worker.py: never dies, backs
    off on connection blips. A retention loop that quietly stopped looks exactly
    like one that is working — right up until the disk fills."""
    import asyncpg

    log.info("artifact retention started (%dd / %.0fGB cap)",
             get_settings().artifact_retention_days, get_settings().artifact_max_gb)
    while True:
        try:
            async with jobruns.track("artifact_retention", "Workspace files",  # Quick 008
                                     trigger=jobruns.every(max(300, get_settings().artifact_sweep_seconds))) as job:
                summary = await sweep()
                if summary.get("deleted"):
                    job.ok(f"Deleted {summary['deleted']} old run folder(s), freed "
                           f"{summary.get('freed_mb', 0)} MB; {summary.get('remaining_mb', 0)} MB left",
                           summary, routine=True, collapse="sum")
            await asyncio.sleep(max(300, get_settings().artifact_sweep_seconds))
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("retention DB connection blip, retrying: %s", type(e).__name__)
            await asyncio.sleep(60)
        except Exception as e:
            log.exception("artifact sweep failed; continuing")
            await jobruns.loop_error("retention", e)  # Quick 008 (never raises)
            await asyncio.sleep(300)
