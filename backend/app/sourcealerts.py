"""Phase 15 (SRC-10): freshness and failure alerts for sources, once per state change, on the Phase 11 health_alerts table. Fixed text only."""
from __future__ import annotations

import asyncio
import datetime
import logging
import time

from . import guardrails, healthwatch
from .db import get_pool

log = logging.getLogger(__name__)

STALE_PREFIX = "source_stale:"
FAILING_PREFIX = "source_failing:"
UNREACHABLE_PREFIX = "source_unreachable:"
LIVE_PROBE_SECONDS = 300
LIVE_PROBE_TIMEOUT = 15

_last_probe: dict[str, float] = {}
_FINISHED = ("ok", "partial", "failed")


def _aware(t):
    if t is not None and t.tzinfo is None:
        return t.replace(tzinfo=datetime.timezone.utc)
    return t


def _last_success(source: dict):
    ls = source.get("last_success_at")
    if ls is None:
        ts = [t.get("last_success_at") for t in source.get("tables") or []
              if t.get("mode") == "import" and t.get("last_success_at")]
        ls = max((_aware(x) for x in ts), default=None)
    return _aware(ls)


def decide(source: dict, runs: list[dict], now) -> dict:
    """Pure. runs: sync runs newest first. Returns {"stale", "failing", "minutes"}."""
    out = {"stale": False, "failing": False, "minutes": None}
    if source.get("status") != "active":
        return out
    # Phase 17 (CONS-05): business_db alerts like any source once it has import tables; all-live stays silent
    imp = [t for t in source.get("tables") or []
           if (t.get("mode") == "import" or t.get("pending_mode") == "import")
           and t.get("status") != "missing"]
    if not imp:  # live-only (or empty): no sync runs, never stale or failing
        return out
    fin = [r for r in runs if r.get("status") in _FINISHED][:2]
    out["failing"] = len(fin) == 2 and all(r["status"] == "failed" for r in fin)
    fm = source.get("freshness_minutes")
    last = _last_success(source)
    if fm and last is not None and all(t.get("backfill_done") for t in imp):
        age = (_aware(now) - last).total_seconds() / 60
        out["minutes"] = int(age)
        out["stale"] = age > fm
    return out


async def probe_live(source: dict) -> bool | None:
    """True = unreachable, False = reachable, None = unknown. One reader query through guardrails."""
    # Quick 028 (WR-03): a live view over a wrapper reads the whole remote table, so probe the
    # live table with the fewest estimated rows (unknown sizes last, then by name)
    cands = [t for t in source.get("tables") or []
             if t.get("mode") == "live" and t.get("status") != "missing"]
    live = sorted(cands, key=lambda t: (t.get("est_rows") is None, t.get("est_rows") or 0,
                                        t["warehouse_table"]))
    live = [t["warehouse_table"] for t in live]
    if not live:
        return None
    sql = f'SELECT 1 FROM "src_{source["name"]}"."{live[0]}" LIMIT 1'
    try:
        await asyncio.wait_for(guardrails.run_readonly_sql(sql), LIVE_PROBE_TIMEOUT)
        return False
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return None  # Quick 028 (WR-03): slow is unknown, never "unreachable"
    except guardrails.SourceUnreachable:
        return True
    except guardrails.WarehouseUnreachable:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("live probe of %s failed: %s", source.get("name"), type(e).__name__)
        return None


async def _apply(pool, component: str, want: bool, detail: str) -> None:
    if want:
        aid = await pool.fetchval(healthwatch.OPEN_SQL, component, detail)
        if aid is not None:
            await healthwatch._notify(component, aid, True, detail)
    else:
        aid = await pool.fetchval(healthwatch.RESOLVE_SQL, component)
        if aid is not None:
            await healthwatch._notify(component, aid, False)


_RUNS_SQL = """SELECT source_id, status FROM (
    SELECT source_id, status, row_number() OVER (PARTITION BY source_id ORDER BY id DESC) rn
      FROM sync_runs WHERE status IN ('ok','partial','failed')) x WHERE rn <= 2 ORDER BY source_id, rn"""
_OK_SQL = ("SELECT source_id, max(ended_at) t FROM sync_runs "
           "WHERE status IN ('ok','partial') GROUP BY source_id")
_OPEN_SQL = ("SELECT component FROM health_alerts WHERE resolved_at IS NULL AND "
             "(component LIKE 'source\\_stale:%' OR component LIKE 'source\\_failing:%' "
             "OR component LIKE 'source\\_unreachable:%')")


async def finished_runs(pool) -> tuple[dict, dict]:
    runs: dict[int, list[dict]] = {}
    for r in await pool.fetch(_RUNS_SQL):
        runs.setdefault(r["source_id"], []).append({"status": r["status"]})
    ok = {r["source_id"]: r["t"] for r in await pool.fetch(_OK_SQL)}
    return runs, ok


async def evaluate(now=None, clock=time.monotonic) -> dict:
    from . import sourcesvc
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pool = await get_pool()
    srcs = await sourcesvc.list_sources()  # Phase 17 (CONS-05): includes business_db
    runs, ok = await finished_runs(pool)
    open_rows = {r["component"] for r in await pool.fetch(_OPEN_SQL)}
    out: dict = {"stale": [], "failing": [], "unreachable": []}
    names = set()
    for s in srcs:
        n = s["name"]
        names.add(n)
        s = dict(s, last_success_at=ok.get(s["id"]))
        d = decide(s, runs.get(s["id"], []), now)
        await _apply(pool, STALE_PREFIX + n, d["stale"],
                     f"Source {n}: last successful sync {d['minutes']} minutes ago "
                     f"(target {s.get('freshness_minutes')})")
        await _apply(pool, FAILING_PREFIX + n, d["failing"], f"Source {n}: the last two syncs failed")
        if d["stale"]:
            out["stale"].append(n)
        if d["failing"]:
            out["failing"].append(n)
        has_live = any(t.get("mode") == "live" and t.get("status") != "missing"
                       for t in s.get("tables") or [])
        comp = UNREACHABLE_PREFIX + n
        if s.get("status") == "active" and has_live and s.get("kind") != "csv" and n != "business_db":
            # Phase 17 (CONS-05): business_db live tables have the Warehouse probe; no src_ view to probe
            t0 = clock()
            if n in _last_probe and t0 - _last_probe[n] < LIVE_PROBE_SECONDS:
                continue
            _last_probe[n] = t0
            bad = await probe_live(s)
            if bad is None:
                continue
            await _apply(pool, comp, bad, f"Source {n}: its live tables could not be read")
            if bad:
                out["unreachable"].append(n)
        elif comp in open_rows:
            await _apply(pool, comp, False, "")
    # sources removed or gone from the registry: resolve what is still open
    for comp in open_rows:
        if comp.partition(":")[2] not in names:
            await _apply(pool, comp, False, "")
    for n in list(_last_probe):
        if n not in names:
            _last_probe.pop(n, None)
    return out
