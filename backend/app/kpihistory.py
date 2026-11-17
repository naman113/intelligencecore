"""kpihistory: the ONE module that writes and reads metric_history.

The writer is record(), called once per successful check by sentinel._check_one.
Readers (series(), added in plan 05-03) serve both the ops route and the
get_kpi_history tool, so the UI and agents see identical points. The table lives
in the working-memory DB; consolidation.history_retention (Phase 9, KPI-04)
is the only deleter: it prunes rows older than MAX_DAYS nightly, behind
retention_dry_run.

series() output is bounded by OUTPUT_BUDGET, so route and tool callers get
compact, identical JSON.

The Phase 6 readers (daily_rows, watch_in_scope) feed dashboard snapshots (via
dashboard_refresh) and the dashboard tools' scope check.

Deliberately imports nothing from guardrails, sentinel or graph: no business-DB
path and no import cycle (sentinel imports this module).
"""

import json
import logging
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import get_pool

log = logging.getLogger("kpihistory")

MIN_DAYS = 1  # smallest history window a caller may request
MAX_DAYS = 400  # the KPI-04 retention horizon
MAX_RAW_POINTS = 200  # above this, bucket (first-pass sizing; _fit_budget enforces OUTPUT_BUDGET)
OBSERVATION_MAX = 1000  # chars of the latest observation echoed back (first-pass sizing; _fit_budget enforces OUTPUT_BUDGET)
# Hard limit on len(json.dumps(series())), with headroom under the 20,000-char tool-output convention.
OUTPUT_BUDGET = 19000
# The only source of a stepped-up date_trunc field; never caller input.
_COARSER = {"raw": "hour", "hour": "day", "day": "week"}

# Window predicate: constant over bind params $1 (watch_id) and $2 (days); never interpolate caller input.
# Phase 17 (CONS-02): stale marker rows (value-less) are never read as values.
_WINDOW = "watch_id = $1 AND observed_at >= now() - make_interval(days => $2) AND NOT stale"


def _num(v) -> float | None:
    """Finite float or None. Bools, NaN and Infinity are rejected (NaN/Infinity
    would make Starlette's JSONResponse fail with a 500)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pick_value(trigger: dict | None, current: dict) -> tuple[str | None, float | None]:
    """Choose (metric, value) for a row. Prefer the trigger column; a NULL there
    stays NULL (never switches metric); a non-numeric value stays NULL (text lives in
    the observation). Only when the trigger names no column does it fall back to
    the first numeric column in SELECT order.

    Phase 13 (GOLIVE-08 gap): KPI value column. value_column, set by the watch
    compiler for KPI watches, names the measure to record; the trigger column only
    decides firing."""
    vcol = (trigger or {}).get("value_column") if isinstance(trigger, dict) else None
    if isinstance(vcol, str) and vcol and vcol in current:
        return vcol, _num(current[vcol])
    col = (trigger or {}).get("column")
    if col is not None and col in current:
        return col, _num(current[col])  # non-numeric stays NULL; metric never switches
    if col is None:
        for k, v in current.items():
            n = _num(v)
            if n is not None:
                return k, n
    return col, None


async def record(pool, watch_id: int, trigger: dict | None, current: dict,
                 as_of: datetime | None = None) -> bool:
    """Insert one metric_history row. Fail-soft: history is telemetry and must
    never stop the sentinel sweep or the fire path (loops never die).

    Phase 17 (CONS-02): as_of, when given, is stored in the observation. A result
    column literally named as_of moves to as_of_column; the freshness value wins."""
    try:
        metric, value = pick_value(trigger, current)
        obs = current
        if as_of is not None:  # Phase 17 (CONS-02)
            obs = {k: v for k, v in current.items() if k != "as_of"}
            if "as_of" in current:
                obs["as_of_column"] = current["as_of"]
            obs["as_of"] = as_of.isoformat()
        await pool.execute(
            "INSERT INTO metric_history (watch_id, metric, value, observation) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            watch_id, metric, value, json.dumps(obs, default=str))
        return True
    except Exception as e:
        log.warning("watch %s: history write failed (%s: %s)",
                    watch_id, type(e).__name__, e)
        return False


async def record_stale(pool, watch_id: int, trigger: dict | None, as_of: datetime,
                       stale_tables=()) -> bool:
    """Phase 17 (CONS-02): a skipped stale check writes a value-less marker row
    (value NULL, stale true) carrying as_of. Readers ignore it; latest_as_of
    includes it. Fail-soft like record."""
    try:
        t = trigger if isinstance(trigger, dict) else {}
        metric = t.get("value_column") or t.get("column") or None
        obs = {"as_of": as_of.isoformat(), "stale": True, "stale_tables": list(stale_tables)[:20]}
        await pool.execute(
            "INSERT INTO metric_history (watch_id, metric, value, observation, stale) "
            "VALUES ($1, $2, NULL, $3::jsonb, true)",
            watch_id, metric, json.dumps(obs, default=str))
        return True
    except Exception as e:
        log.warning("watch %s: stale marker write failed (%s: %s)",
                    watch_id, type(e).__name__, e)
        return False


async def latest_as_of(watch_id: int) -> datetime | None:
    """Phase 17 (CONS-02): as_of of the newest row, marker rows included on purpose
    (a stale watch's dashboard tile must show the old as_of). Falls back to
    observed_at; None with no rows."""
    pool = await get_pool()
    r = await pool.fetchrow(
        "SELECT observation->>'as_of' AS a, observed_at FROM metric_history "
        "WHERE watch_id = $1 ORDER BY observed_at DESC, id DESC LIMIT 1", watch_id)
    if r is None:
        return None
    if r["a"]:
        try:
            d = datetime.fromisoformat(str(r["a"]))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return r["observed_at"]


# ── reader (05-03) ──

def clamp_days(days) -> int:
    """int(days) clamped to MIN_DAYS..MAX_DAYS; unparseable input gives 30."""
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 30
    return max(MIN_DAYS, min(MAX_DAYS, d))


def choose_resolution(n_rows: int, days: int) -> str:
    """raw / hour / day / week. The result is the date_trunc field, always from
    this allowlist and never from a caller."""
    if n_rows <= MAX_RAW_POINTS:
        return "raw"
    if days <= 7:
        return "hour"
    if days <= 120:
        return "day"
    return "week"


def _tz() -> str:
    """Operator-configured timezone if it is a valid zone, else UTC."""
    try:
        name = get_settings().timezone or "UTC"
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


def _decode(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _f(v) -> float | None:
    """Round a value for output. Caps float noise from avg()/ratio watches at 6
    significant digits; values of a million or more are rounded to whole numbers
    so counts and currency totals keep every integer digit (never 12,345,700
    for 12,345,678)."""
    if v is None:
        return None
    x = float(v)
    if abs(x) >= 1e6:
        return float(round(x))
    return float(f"{x:.6g}")


def _iso(dt) -> str:
    """Second-precision ISO timestamp."""
    return dt.replace(microsecond=0).isoformat()


async def _points(pool, watch_id: int, days: int, resolution: str, tz: str) -> list[dict]:
    if resolution == "raw":
        rows = await pool.fetch(
            f"SELECT observed_at AS t, metric, value FROM metric_history WHERE {_WINDOW} "
            "ORDER BY observed_at", watch_id, days)
        return [{"t": _iso(r["t"]), "metric": r["metric"],
                 "value": _f(r["value"])} for r in rows]
    rows = await pool.fetch(
        "SELECT date_trunc($3, observed_at, $4) AS t, metric, "
        "(array_agg(value ORDER BY observed_at DESC))[1] AS value, "
        "min(value) AS min, max(value) AS max, count(*) AS n "
        f"FROM metric_history WHERE {_WINDOW} GROUP BY 1, 2 ORDER BY 1, 2",
        watch_id, days, resolution, tz)
    return [{"t": _iso(r["t"]), "metric": r["metric"], "value": _f(r["value"]),
             "min": _f(r["min"]), "max": _f(r["max"]), "n": int(r["n"])}
            for r in rows]


def _size(out: dict) -> int:
    return len(json.dumps(out))


async def _fit_budget(pool, out: dict, watch_id: int, days: int, tz: str) -> None:
    """Bring out under OUTPUT_BUDGET, in place. Order: shed the echoed
    observation, shed per-point min/max, step to one coarser resolution
    (re-queried, min/max back on; up to week), then evenly thin keeping the newest."""
    if _size(out) > OUTPUT_BUDGET and out["latest"] is not None:
        out["latest"]["observation"] = {"_truncated": True}
    while _size(out) > OUTPUT_BUDGET:
        for p in out["points"]:
            p.pop("min", None)
            p.pop("max", None)
        if _size(out) <= OUTPUT_BUDGET:
            return
        nxt = _COARSER.get(out["resolution"])
        if nxt is None:
            break
        out["resolution"] = nxt
        out["points"] = await _points(pool, watch_id, days, nxt, tz)
    while _size(out) > OUTPUT_BUDGET and len(out["points"]) > 1:
        out["points"] = out["points"][::-1][::2][::-1]


async def series(watch_id: int, days: int = 30) -> dict | None:
    """Stored series of one watch, JSON-native; None if the watch is unknown.
    Raw points when <= MAX_RAW_POINTS observations, else hour/day/week buckets
    carrying the closing value plus min, max and n. Bounded by OUTPUT_BUDGET."""
    days = clamp_days(days)
    pool = await get_pool()
    w = await pool.fetchrow(
        "SELECT id, title, kind, status, trigger_rule FROM watches WHERE id = $1",
        watch_id)
    if w is None:
        return None
    stats = await pool.fetchrow(
        "SELECT count(*) AS n, array_remove(array_agg(DISTINCT metric), NULL) AS metrics "
        f"FROM metric_history WHERE {_WINDOW}", watch_id, days)
    n = int(stats["n"] or 0) if stats else 0
    metrics = list(stats["metrics"] or []) if stats else []
    resolution = choose_resolution(n, days)
    tz = _tz()
    points = await _points(pool, watch_id, days, resolution, tz)
    lr = await pool.fetchrow(
        "SELECT observed_at, metric, value, observation FROM metric_history "
        "WHERE watch_id = $1 AND NOT stale ORDER BY observed_at DESC LIMIT 1", watch_id)
    latest = None
    if lr is not None:
        obs = _decode(lr["observation"])
        if len(json.dumps(obs, default=str)) > OBSERVATION_MAX:
            obs = {"_truncated": True}
        latest = {"t": _iso(lr["observed_at"]), "metric": lr["metric"],
                  "value": _f(lr["value"]), "observation": obs}
    rule = _decode(w["trigger_rule"]) or {}
    metric = (latest or {}).get("metric") or (
        (rule.get("value_column") or rule.get("column")) if isinstance(rule, dict) else None)
    out = {"watch_id": w["id"], "title": w["title"], "kind": w["kind"],
            "status": w["status"], "metric": metric, "metrics": metrics,
            "days": days, "resolution": resolution, "timezone": tz,
            "points": points, "latest": latest}
    await _fit_budget(pool, out, watch_id, days, tz)
    return out


# ── Phase 6: dashboard KPI-history source ──

# Daily buckets of the watch's newest metric. Binds: $1 watch_id, $2 days, $3 tz.
# Never interpolate caller input.
_DAILY_SQL = (
    "WITH latest AS (SELECT metric FROM metric_history WHERE watch_id = $1 AND NOT stale "
    "ORDER BY observed_at DESC, id DESC LIMIT 1) "
    "SELECT (h.observed_at AT TIME ZONE $3)::date AS d, "
    "(array_agg(h.value ORDER BY h.observed_at DESC, h.id DESC))[1] AS value, "
    "min(h.value) AS min, max(h.value) AS max, count(*) AS n, "
    "min(h.metric) AS metric "
    "FROM metric_history h "
    "WHERE h.watch_id = $1 AND h.observed_at >= now() - make_interval(days => $2) AND NOT h.stale "
    "AND h.metric IS NOT DISTINCT FROM (SELECT metric FROM latest) "
    "GROUP BY 1 ORDER BY 1"
)


async def daily_rows(watch_id: int, days: int = 30) -> list[dict]:
    """Daily buckets (operator timezone) of the watch's latest metric, ascending.
    Raises LookupError when the watch is gone."""
    days = clamp_days(days)
    pool = await get_pool()
    if await pool.fetchval("SELECT id FROM watches WHERE id = $1", watch_id) is None:
        raise LookupError(f"watch {watch_id} no longer exists")
    rows = await pool.fetch(_DAILY_SQL, watch_id, days, _tz())
    return [{"day": r["d"].isoformat(), "value": _f(r["value"]), "min": _f(r["min"]),
             "max": _f(r["max"]), "n": int(r["n"]), "metric": r["metric"]}
            for r in rows]


async def watch_in_scope(watch_id: int, thread_id: str | None) -> dict | None:
    """The watch when kpi-kind, or user-kind and owned by thread_id (or by
    another Ask chat of the same operator, Quick 003); else None (unknown and
    out-of-scope are indistinguishable)."""
    from . import appchannel
    pool = await get_pool()
    w = await pool.fetchrow(
        "SELECT id, title, kind, status, thread_id FROM watches WHERE id = $1", watch_id)
    if w is None:
        return None
    if w["kind"] == "kpi" or (w["kind"] == "user" and thread_id is not None
                              and appchannel.same_owner(w["thread_id"], thread_id)):
        return {"id": w["id"], "title": w["title"], "kind": w["kind"], "status": w["status"]}
    return None
# ── end Phase 6 ──


async def list_watches_brief(limit: int = 50) -> list[dict]:
    """Compact watch list (id, title, kind, status) for agent discovery."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, title, kind, status FROM watches ORDER BY id LIMIT $1", limit)
    return [{"id": r["id"], "title": r["title"], "kind": r["kind"],
             "status": r["status"]} for r in rows]
