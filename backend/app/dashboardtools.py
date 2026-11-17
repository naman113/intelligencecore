"""Dashboard agent tools (Phase 6). get_dashboard and preview_dashboard only read;
save_dashboard_version writes only a draft version plus one proposed
publish_dashboard action; nothing here can publish (publishing happens only on
human approval in actionsvc or through the operator route). View tokens and
preview tokens never appear in tool results or logs."""

import asyncio
import json
import logging
import time

import asyncpg
from langchain_core.tools import tool

from .agents.graph import _log_tool, ctx_phone, ctx_run_id  # noqa: F401
from .db import get_pool
from . import dashboards, guardrails, kpihistory, dashboard_refresh, forecasts
from . import dashguard  # Quick 036
from . import dashsettings  # Phase 22
from . import dashboarddesign  # Phase 22 (DCAP-17)


def _tz_kw() -> dict:
    """Phase 22 (DCAP-08): the operator timezone as a keyword argument, only when set."""
    tz = dashsettings.day_timezone()
    return {} if tz is None else {"timezone": tz}

log = logging.getLogger("dashboardtools")

SAMPLE_LIMIT = 5
SAMPLE_QUERY_TIMEOUT = 30
SAMPLE_TOTAL_BUDGET = 90
PROBE_TIMEOUT = 30
CELL_CAP = 200
OUTPUT_CAP = 20000
CODE_RETURN_CAP = 60000
VERSIONS_SHOWN = 20
SLUG_LIST_CAP = 50
DRAFT_PREVIEW_TTL = 1440
FINDINGS_SHOWN = 30

_KPI_OUT_OF_SCOPE = "watch #{n} is unknown or not chartable from this conversation"
# Phase 13.1
_FORECAST_UNKNOWN = "forecast #{n} is unknown or retired; get_forecast lists the forecasts"
_FORECAST_EMPTY_NOTE = ("no forecast run is stored yet; the panel fills after the first run "
                        "(within a minute of approval, then nightly)")


def _cell(v):
    v = dashboard_refresh.encode_value(v)
    if isinstance(v, str) and len(v) > CELL_CAP:
        v = v[:CELL_CAP]
    return v


def _fit(out: dict) -> dict:
    def size():
        return len(json.dumps(out, default=str))
    for n in (SAMPLE_LIMIT, 3, 1, 0):
        if size() <= OUTPUT_CAP:
            return out
        for q in out.get("queries", []):
            if isinstance(q.get("sample"), list):
                q["sample"] = q["sample"][:n]
    for key in ("warnings", "errors"):
        while size() > OUTPUT_CAP and out.get(key):
            out[key] = out[key][: len(out[key]) // 2]
    return out


def _is_conn_error(e: Exception) -> bool:
    return isinstance(e, (ConnectionError, OSError, asyncpg.exceptions.PostgresConnectionError,
                          asyncpg.exceptions.CannotConnectNowError,
                          asyncpg.exceptions.InterfaceError))


def _err_text(e: Exception) -> str:
    if isinstance(e, guardrails.SQLGuardrailError):
        return str(e)
    if isinstance(e, asyncio.TimeoutError):
        return "query timed out"
    if _is_conn_error(e):
        return "data source unavailable"
    return f"{type(e).__name__}: {e}"


# -- get_dashboard ---------------------------------------------------------

_CONVERT_HINT = ("This is an old HTML dashboard. Rewrite it as a chart spec with the same "
                 "figures (reuse its queries; use measures where get_model has them) and save "
                 "it with save_dashboard_version under the same slug; the owner approves it.")


@tool
async def get_dashboard(slug: str) -> str:
    """Read an existing dashboard before revising it. Returns the published version, or the latest draft when nothing is published, plus its version history and any refresh errors. A chart-spec version comes back as its spec (the form you send to save_dashboard_version); an old HTML version comes back with its code and queries and needs_conversion, because new versions are chart specs only. Change only what the owner asked for. An unknown slug lists the dashboards that exist."""
    start = time.monotonic()
    args = {"slug": slug}
    refused = False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                slug_ok = dashboards.validate_slug(slug)
            except dashboards.DashboardError as e:
                refused = True
                result = f"REFUSED: {e}"
            else:
                try:
                    d = await dashboards.get_dashboard(conn, slug=slug_ok)
                    dash, versions = d["dashboard"], d["versions"]
                    cur = dash.get("current_version")
                    shown = cur if cur is not None else (
                        versions[0]["version"] if versions else None)
                    if shown is None:
                        raise dashboards.DashboardError(404, "dashboard has no versions")
                    v = await dashboards.get_version(conn, slug=slug_ok, version=shown)
                    out = {
                        "slug": dash["slug"], "title": dash["title"],
                        "status": v["status"], "current_version": cur,
                        "version_shown": shown,
                        "is_published": cur is not None and bool(v["is_current"]),
                        "format": v.get("format", "html"),
                        "versions": [{"version": x["version"], "status": x["status"],
                                      "change_note": x.get("change_note"),
                                      "created_at": x.get("created_at"),
                                      "last_error": x.get("last_error")}
                                     for x in versions[:VERSIONS_SHOWN]],
                        "snapshot_errors": [{"query_id": s["query_id"], "error": s["error"]}
                                            for s in v["snapshots"] if s.get("error")],
                    }
                    if v.get("format") == "spec":
                        out["spec"] = v.get("spec")
                        out["compiled_measures"] = [
                            {"id": q.get("id"), "measure": q.get("measure"),
                             "columns": q.get("columns")}
                            for q in v["queries"] if q.get("source") == dashboards.MEASURE_SOURCE]
                    else:
                        code = v.get("code") or ""
                        out["needs_conversion"] = not dashboards.legacy_html_on()
                        out["code"] = code[:CODE_RETURN_CAP]
                        out["queries"] = v["queries"]
                        out["convert_hint"] = _CONVERT_HINT
                        if len(code) > CODE_RETURN_CAP:
                            out["code_truncated"] = True
                    result = json.dumps(out, default=str)
                except dashboards.DashboardError as e:
                    refused = True
                    if getattr(e, "status", None) != 404:
                        result = f"REFUSED: {e}"
                    else:
                        rows = await dashboards.list_dashboards(conn, limit=SLUG_LIST_CAP)
                        names = "; ".join(f"{r['slug']} ({r['title']})"
                                          for r in rows[:SLUG_LIST_CAP])
                        result = (f"REFUSED: no dashboard with slug {slug!r}. Existing "
                                  f"dashboards: {names or 'none'}.")
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("get_dashboard", args, result[:1000], start, refused=refused)
    return result


# -- preview_dashboard -----------------------------------------------------

async def _sample(q: dict, deadline: float) -> dict:
    qid = q.get("id")
    entry = {"id": qid, "source": "sql", "columns": [], "sample": [], "error": None}
    if q.get("source") == dashboards.KPI_SOURCE:
        entry["source"] = "kpi_history"
    elif q.get("source") == dashboards.FORECAST_SOURCE:  # Phase 13.1
        entry["source"] = "forecast"
    elif q.get("source") == dashboards.MEASURE_SOURCE:  # Phase 18
        entry["source"] = "measure"
        entry["measure"] = q.get("measure")
    if deadline - time.monotonic() < 0.02:  # coarse Windows clock: a timed-out sibling may end a tick early
        entry["error"] = "not sampled (time budget)"
        return entry
    try:
        if entry["source"] == "kpi_history":
            wid = q.get("watch_id")
            if await kpihistory.watch_in_scope(wid, ctx_phone.get()) is None:
                entry["error"] = _KPI_OUT_OF_SCOPE.format(n=wid)
                return entry
            rows = await kpihistory.daily_rows(wid, q.get("days") or 30)
            rows = rows[-SAMPLE_LIMIT:]
        elif entry["source"] == "forecast":
            # Phase 13.1: sample across the actual/forecast boundary
            fid = q.get("forecast_id")
            try:
                allrows = await forecasts.dashboard_rows(fid)
            except LookupError:
                entry["error"] = _FORECAST_UNKNOWN.format(n=fid)
                return entry
            if not allrows:
                entry["note"] = _FORECAST_EMPTY_NOTE
            hist = [r for r in allrows if r.get("actual") is not None]
            fut = [r for r in allrows if r.get("forecast") is not None]
            rows = (hist[-2:] + fut[:3])[:SAMPLE_LIMIT]
        else:
            validated = guardrails.validate_readonly_sql(q.get("sql"))
            remaining = max(0.01, min(SAMPLE_QUERY_TIMEOUT, deadline - time.monotonic()))
            rows = await asyncio.wait_for(
                guardrails.run_readonly_sql(
                    f"SELECT * FROM ({validated}\n) AS _preview LIMIT {SAMPLE_LIMIT}", **_tz_kw()),
                remaining)
        rows = list(rows)[:SAMPLE_LIMIT]
        entry["columns"] = list(rows[0].keys()) if rows else []
        entry["sample"] = [{k: _cell(v) for k, v in r.items()} for r in rows]
    except Exception as e:
        entry["error"] = _err_text(e)
    return entry


def _coerce_spec(spec):
    """A spec as a dict (JSON text is parsed); anything else is None."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        try:
            v = json.loads(spec)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None
    return None


def _spec_args(spec) -> dict:
    s = _coerce_spec(spec)
    raw = spec if isinstance(spec, str) else json.dumps(spec, default=str)
    out = {"spec_bytes": len(raw.encode("utf-8", "replace")), "tile_ids": [], "query_ids": []}
    if s:
        out["tile_ids"] = [t.get("id") for t in (s.get("tiles") or []) if isinstance(t, dict)]
        out["query_ids"] = [q.get("id") for q in (s.get("queries") or []) if isinstance(q, dict)]
    return out


async def _row_cap_warning(entry: dict, sql: str, deadline: float, cap: int, tiles: list[str]):
    """Warn when a compiled measure query returns at least the guardrail row cap."""
    remaining = max(0.01, min(SAMPLE_QUERY_TIMEOUT, deadline - time.monotonic()))
    try:
        rows = await asyncio.wait_for(guardrails.run_readonly_sql(
            f"SELECT count(*) AS n FROM ({sql}\n) AS _c", **_tz_kw()), remaining)
        n = int(list(rows)[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001 - the count is advice; sampling already reports errors
        return None
    if n >= cap:
        which = ", ".join(tiles) or entry["id"]
        return {"rule": "row_cap", "path": f"tiles[{which}]",
                "message": (f"measure query {entry['id']} returns {n} rows, at or above the "
                            f"{cap}-row cap, so the tile would be cut off; use a shorter window "
                            "or a top limit.")}
    return None


# -- Phase 22 (DCAP-17): the spec v2 preview ---------------------------------------------

V2_RENDERER = "ic-render-2.0.0"
MAX_FILTER_COUNTS = 3
COUNT_PARALLEL = 4


def _is_v2_stored(stored) -> bool:
    return isinstance(stored, dict) and stored.get("spec_version") == 2


async def _count_one(sql: str, deadline: float, distinct_col: str | None = None):
    """One advice count over an already validated compiled query; None when it cannot run."""
    remaining = max(0.01, min(SAMPLE_QUERY_TIMEOUT, deadline - time.monotonic()))
    what = "count(*)"
    if distinct_col:
        what = 'count(DISTINCT "' + distinct_col.replace('"', '""') + '")'
    try:
        rows = await asyncio.wait_for(guardrails.run_readonly_sql(
            f"SELECT {what} AS n FROM ({sql}\n) AS _f", **_tz_kw()), remaining)
        return int(list(rows)[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001 - counts are advice; sampling reports query errors
        return None


async def _filter_counts(stored: dict, qs: list[dict], deadline: float) -> dict:
    """{filter id: distinct values} over one following tile's query, at most 3 counts."""
    out: dict = {}
    for fid, col, sql in _filter_targets(stored, qs):
        n = await _count_one(sql, deadline, col)
        if n is not None:
            out[fid] = n
    return out


def _filter_targets(stored: dict, qs: list[dict]) -> list[tuple]:
    """[(filter id, output column, sql)] for each page filter a following tile query can count."""
    from . import modelcompiler

    by = {q["id"]: q for q in qs}
    out = []
    for f in (stored.get("filters") or [])[:MAX_FILTER_COUNTS]:
        try:
            col = modelcompiler._out_name(f["dimension"])
        except Exception:  # noqa: BLE001
            continue
        for t in stored.get("tiles", []):
            q = by.get(t.get("qid"))
            if (t.get("follow_filters") and q and q.get("sql")
                    and col in (q.get("columns") or [col])):
                out.append((f["id"], col, q["sql"]))
                break
    return out


def _uncounted_filters(stored: dict, qs: list[dict], counts: dict) -> list[str]:
    """Followed filters whose values could not be counted (the 50-value gate cannot pass)."""
    return [fid for fid, _c, _s in _filter_targets(stored, qs) if fid not in counts]


def _uncounted_errors(stored: dict, ids: list[str]) -> list[dict]:
    order = [f.get("id") for f in stored.get("filters") or []]
    return [{"rule": "filters",
             "path": f"filters[{order.index(i) if i in order else 0}]",
             "message": f"could not count the values of filter {i}; try again"} for i in ids]


def _filter_deadline() -> float:
    """The filter counts get their own budget, not what sampling left over."""
    return time.monotonic() + SAMPLE_QUERY_TIMEOUT * MAX_FILTER_COUNTS


async def _count_and_size(qs: list[dict], entries: list[dict], deadline: float):
    """({query id: row count}, {query id: mean bytes per sample row}) for the snapshot caps."""
    sem = asyncio.Semaphore(COUNT_PARALLEL)

    async def count(q, e):
        if e["error"] or not q.get("sql") or q.get("source") in (
                dashboards.KPI_SOURCE, dashboards.FORECAST_SOURCE):
            return None
        async with sem:
            return await _count_one(q["sql"], deadline)

    counts_l = await asyncio.gather(*[count(q, e) for q, e in zip(qs, entries)])
    counts, sizes = {}, {}
    for q, e, n in zip(qs, entries, counts_l):
        if n is None:
            continue
        counts[q["id"]] = n
        if e["sample"]:
            total = sum(len(json.dumps(r, default=str)) for r in e["sample"])
            sizes[q["id"]] = total / len(e["sample"])
    return counts, sizes


async def _model_hints(spec, qs: list[dict]):
    """(units, measure_sql) from the semantic model, loaded only when a rule needs it."""
    need = any(t.type == "combo" for t in spec.tiles) or any(
        q.get("source") not in (dashboards.KPI_SOURCE, dashboards.FORECAST_SOURCE,
                                dashboards.MEASURE_SOURCE) for q in qs)
    if not need:
        return None, None
    try:
        from . import semanticmodel
        model = await semanticmodel.load_model()
    except Exception:  # noqa: BLE001 - lint advice only
        return None, None
    units = {m.name: m.unit for m in model.measures if m.unit}
    sql = {m.name: (m.agg, m.expr, m.base_relation) for m in model.measures
           if m.type == "simple" and m.agg and m.expr and m.base_relation}
    return units, sql


async def _preview_v2(prepared: dict, entries: list[dict], deadline: float, left):
    from . import dashboardspec

    spec, stored, qs = prepared["spec"], prepared["stored"], prepared["queries"]
    caps = dashsettings.snapshot_caps()
    counts, sizes = await _count_and_size(qs, entries, deadline)
    fcounts = await _filter_counts(stored, qs, _filter_deadline())
    members = {}
    for t in stored.get("tiles", []):  # donut parts are distinct x members, not rows
        q = next((x for x in qs if x.get("id") == t.get("qid")), None)
        if t.get("type") == "donut" and t.get("x") and q and q.get("sql") \
                and q["id"] not in members and q["id"] in counts:
            n = await _count_one(q["sql"], deadline, t["x"])
            if n is not None:
                members[q["id"]] = n
    samples = {"rows": {e["id"]: e["sample"] for e in entries}, "counts": counts,
               "bytes": sizes, "filters": fcounts, "members": members}
    units, measure_sql = await _model_hints(spec, qs)
    notes = [n for q in qs for n in ((q.get("request") or {}).get("notes") or [])]
    found = dashboarddesign.design_lint(
        spec, stored, qs, units=units, samples=samples, notes=notes,
        measure_sql=measure_sql, caps=caps)
    errors = [{k: v for k, v in f.items() if k != "level"} for f in found
              if f["level"] == "error"]
    errors += _uncounted_errors(stored, _uncounted_filters(stored, qs, fcounts))
    warnings = [{k: v for k, v in f.items() if k != "level"} for f in found
                if f["level"] != "error"]
    summ = dashboardspec.summary(stored, qs)
    out = {"ok": not errors, "errors": errors, "warnings": warnings,
           "wireframe": dashboarddesign.wireframe(stored, qs),
           "tiles": summ["tiles"], "measures": summ["measures"], "queries": entries,
           "size": [{"id": i, "rows": n, "est_bytes": int(n * sizes.get(i, 0)),
                     "rows_cap": caps[0], "bytes_cap": caps[1]} for i, n in counts.items()],
           "size_bytes": len(json.dumps(stored, ensure_ascii=False).encode("utf-8")),
           "renderer": V2_RENDERER}
    if left is not None:
        out["preview_rounds_left"] = left
    if errors:
        out["next"] = "Fix the errors and preview again."
        return out
    out["next"] = ("Check column names and values against the samples, then call "
                   "save_dashboard_version.")
    pid = dashguard.remember_preview(prepared)
    if pid:
        out["preview_id"] = pid
        out["next"] = (
            "Check column names and values against the samples, then call "
            f"save_dashboard_version(slug, change_note, preview_id=\"{pid}\") to save "
            "exactly this spec; send a spec instead only if you change it.")
    return out


def _fit_v2(out: dict) -> dict:
    before = len(out.get("warnings") or [])
    out = _fit(out)
    cut = before - len(out.get("warnings") or [])
    if cut > 0:
        out["warnings_trimmed"] = cut
    return out


@tool
async def preview_dashboard(spec: dict | str) -> str:
    """Check a dashboard chart spec and try its queries before saving. Spec v1, or spec_version: 2 (sections, tile widths, date_control, filters, measures lists, tones). Tile types: stat, table, text, line, area, bar, stacked_bar, sparkline, combo, paired_bar, aging, bullet, waterfall, calendar, donut, ranked_bar, funnel. Fields: use_skill("dashboard-authoring", part="reference/spec-v2.md"). Declare queries as {"id","sql","refresh_minutes"}, {"id", "source": "kpi_history", "watch_id", "days"} or {"id", "source": "forecast", "forecast_id"} (get_forecast lists forecasts), or bind a tile to a get_model measure. Returns every finding with its path (errors must be fixed, warnings are advice; v2 adds design lint, a page wireframe and size per query) and up to 5 sample rows per query. It saves nothing. A preview without errors returns a preview_id: save_dashboard_version(slug, change_note, preview_id) saves exactly that spec. At most 3 rounds (enforced). HTML is refused."""
    start = time.monotonic()
    args = _spec_args(spec)
    refusal, left = dashguard.take_preview_round()  # Quick 036: no await before reserving
    if refusal:
        await _log_tool("preview_dashboard", args, refusal[:1000], start, refused=True)
        return refusal
    refused = False
    try:
        from . import dashboardspec
        from .config import get_settings
        try:
            if not isinstance(spec, (dict, str)):
                raise dashboards.SpecRejected([{
                    "rule": "spec", "path": "spec", "message": dashboardspec._HTML_MSG}])
            prepared = await dashboards.prepare_spec(spec)
        except dashboards.SpecRejected as e:
            out = {"ok": False, "errors": e.findings[:FINDINGS_SHOWN], "queries": [],
                   "next": "Fix the errors and preview again."}
            if left is not None:  # Quick 036
                out["preview_rounds_left"] = left
            result = json.dumps(_fit(out), default=str)
        else:
            cap = get_settings().sql_row_limit
            stored, qs = prepared["stored"], prepared["queries"]
            deadline = time.monotonic() + SAMPLE_TOTAL_BUDGET
            entries = [await _sample(q, deadline) for q in qs]
            if prepared.get("renderer") == V2_RENDERER:  # Phase 22: spec v2
                out = await _preview_v2(prepared, entries, deadline, left)
                result = json.dumps(_fit_v2(out), default=str)
                await _log_tool("preview_dashboard", args, result[:1000], start, refused=refused)
                return result
            warnings = []
            for q, e in zip(qs, entries):
                if q.get("source") == dashboards.MEASURE_SOURCE and not e["error"]:
                    tiles = [t["id"] for t in stored["tiles"] if t.get("qid") == q["id"]]
                    w = await _row_cap_warning(e, q["sql"], deadline, cap, tiles)
                    if w:
                        warnings.append(w)
            summ = dashboardspec.summary(stored, qs)
            out = {"ok": True, "errors": [], "warnings": warnings, "tiles": summ["tiles"],
                   "measures": summ["measures"], "queries": entries,
                   "size_bytes": len(json.dumps(stored, ensure_ascii=False).encode("utf-8")),
                   "next": "Check column names and values against the samples, then call "
                           "save_dashboard_version."}
            pid = dashguard.remember_preview(prepared)  # Quick 036: save by handle
            if pid:
                out["preview_id"] = pid
                out["preview_rounds_left"] = left
                out["next"] = (
                    "Check column names and values against the samples, then call "
                    f"save_dashboard_version(slug, change_note, preview_id=\"{pid}\") to save "
                    "exactly this spec; send a spec instead only if you change it.")
            result = json.dumps(_fit(out), default=str)
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("preview_dashboard", args, result[:1000], start, refused=refused)
    return result


# -- save_dashboard_version ------------------------------------------------

def _diff(cur: list[dict], new: list[dict]):
    old = {q["id"]: q for q in cur}
    nw = {q["id"]: q for q in new}
    added = [i for i in nw if i not in old]
    removed = [i for i in old if i not in nw]
    changed = [i for i in nw if i in old and
               {k: v for k, v in nw[i].items() if k != "id"} !=
               {k: v for k, v in old[i].items() if k != "id"}]
    return added, removed, changed


def _refuse(msg: str) -> tuple[str, bool]:
    return f"REFUSED: {msg}", True


_NO_HTML = "new dashboard versions are chart specs; send {spec, change_note}"
TILES_SHOWN = 24


def _json_col(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _has_spec(spec) -> bool:
    return spec is not None and spec != ""


async def _save(slug, spec, change_note, preview_id=None) -> tuple[str, bool]:
    from . import dashboardspec
    from .dashboardrender import CURRENT_RENDERER

    note = (change_note or "").strip()
    if not note:
        return _refuse("pass a one-line change_note saying what changed and why.")
    if preview_id and _has_spec(spec):  # Quick 036: one source of truth per save
        return _refuse("pass either preview_id or spec, not both.")
    if preview_id:
        # Quick 036: exactly what this run last previewed without errors (stored spec,
        # frozen measure SQL, title), never re-prepared; the checks below still run.
        prepared = dashguard.previewed(preview_id)
        if prepared is None:
            latest = dashguard.latest_preview_id()
            return _refuse(
                f"preview_id {str(preview_id)[:40]!r} is not the latest preview without "
                "errors in this request"
                + (f"; the latest is {latest!r}" if latest else "; there is none")
                + ". Pass that id, or send the spec.")
    elif not _has_spec(spec):
        return _refuse("pass preview_id (from the last preview without errors) or the spec.")
    elif not isinstance(spec, (dict, str)):
        return _refuse(_NO_HTML)
    else:
        try:
            prepared = await dashboards.prepare_spec(spec)
        except dashboards.SpecRejected as e:
            return _refuse("the spec has errors; fix them (preview_dashboard shows them all): "
                           + json.dumps(e.findings[:FINDINGS_SHOWN]))
        except dashboards.DashboardError as e:
            return _refuse(str(e))
    stored, qs, title = prepared["stored"], prepared["queries"], prepared["title"]
    thread = ctx_phone.get()
    for q in qs:
        if q.get("source") == dashboards.KPI_SOURCE:
            if await kpihistory.watch_in_scope(q["watch_id"], thread) is None:
                return _refuse(f"query {q['id']}: " + _KPI_OUT_OF_SCOPE.format(n=q["watch_id"]))
        elif q.get("source") == dashboards.FORECAST_SOURCE:  # Phase 13.1
            if await forecasts.def_usable(q["forecast_id"]) is None:
                return _refuse(f"query {q['id']}: "
                               + _FORECAST_UNKNOWN.format(n=q["forecast_id"]))
        else:  # declared SQL and compiled measures
            try:
                await asyncio.wait_for(guardrails.run_readonly_sql(
                    f"SELECT * FROM ({q['sql']}\n) AS _probe LIMIT 0", **_tz_kw()), PROBE_TIMEOUT)
            except Exception as e:
                msg = ("data source unavailable; try again shortly" if _is_conn_error(e)
                       else _err_text(e))
                return _refuse(f"query {q['id']} failed its dry run: {msg}")
    if _is_v2_stored(stored):  # Phase 22 (DCAP-11): at most 50 values per page filter
        counts = await _filter_counts(stored, qs, _filter_deadline())
        for fid, n in counts.items():
            if n > dashboarddesign.MAX_FILTER_VALUES:
                return _refuse(f"filter {fid} has {n} values; at most "
                               f"{dashboarddesign.MAX_FILTER_VALUES}")
        missing = _uncounted_filters(stored, qs, counts)
        if missing:
            return _refuse(_uncounted_errors(stored, missing)[0]["message"])
        # Same row and byte caps as the preview: a version over them fails every refresh.
        sdl = time.monotonic() + SAMPLE_TOTAL_BUDGET
        ents = [await _sample(q, sdl) for q in qs]
        scounts, ssizes = await _count_and_size(qs, ents, sdl)
        size = dashboarddesign.size_errors(
            qs, {"counts": scounts, "bytes": ssizes}, dashsettings.snapshot_caps())
        if size:
            return _refuse(size[0]["message"])
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    "SELECT v.version, v.queries, v.spec FROM dashboards d JOIN "
                    "dashboard_versions v ON v.id = d.current_version_id "
                    "WHERE d.slug = $1", slug)
                cur_q, cur_spec = [], None
                if cur is not None:
                    cur_q = _json_col(cur["queries"]) or []
                    cs = _json_col(cur.get("spec"))
                    cur_spec = cs if isinstance(cs, dict) else None
                res = await dashboards.save_spec_version(
                    conn, slug=slug, stored=stored, queries=qs, title=title,
                    renderer=prepared.get("renderer") or CURRENT_RENDERER,
                    change_note=note, author="agent",
                    task_run_id=ctx_run_id.get())
                tok = await dashboards.mint_preview_token(
                    conn, slug=res["slug"], version=res["version"], ttl_minutes=DRAFT_PREVIEW_TTL)
                added, removed, changed = _diff(cur_q, qs)
                tdiff = dashboardspec.tile_diff(cur_spec, stored)
                summ = dashboardspec.summary(stored, qs)
                expires = tok["expires_at"]
                payload = {
                    "_summary": f"Publish dashboard {res['slug']} v{res['version']}: {note}"[:200],
                    "slug": res["slug"], "dashboard_id": res["dashboard_id"],
                    "version_id": res["version_id"], "version": res["version"],
                    "title": title, "change_note": note, "path": res["path"],
                    "draft_url": f"{tok['path']}?p={tok['preview_token']}",
                    "preview_expires_at": expires.isoformat() if hasattr(expires, "isoformat")
                    else str(expires),
                    "from_version": cur["version"] if cur is not None else None,
                    "is_new_dashboard": res["created"],
                    "queries_added": added, "queries_removed": removed,
                    "queries_changed": changed,
                    "origin": "agent", "thread_id": thread,
                    "format": "spec",
                    "tiles_added": tdiff["added"], "tiles_removed": tdiff["removed"],
                    "tiles_changed": tdiff["changed"],
                    "tiles": summ["tiles"][:TILES_SHOWN],
                    "measures": summ["measures"], "queries": summ["queries"],
                }
                action_id = await conn.fetchval(
                    "INSERT INTO actions (kind, payload, tier, status, task_run_id) "
                    "VALUES ('publish_dashboard', $1::jsonb, 'draft', 'proposed', $2) "
                    "RETURNING id", json.dumps(payload), ctx_run_id.get())
    except dashboards.DashboardError as e:
        return _refuse(str(e))
    except asyncpg.exceptions.UniqueViolationError:
        return _refuse("conflicting concurrent change; retry")
    return (f"Saved draft v{res['version']} of '{title}' (slug {res['slug']}); it is awaiting "
            f"the owner's approval as action #{action_id}. It is NOT published and not live. "
            "Its data fills within about a minute."), False


@tool
async def save_dashboard_version(slug: str, change_note: str, preview_id: str | None = None,
                                 spec: dict | str | None = None) -> str:
    """Save a dashboard chart spec as a NEW DRAFT version for the owner's approval; it never publishes. Pass preview_id from your last preview_dashboard without errors, or the spec if you changed it (never both). Spec v1, or spec_version: 2 (sections, tile widths, date_control, filters, measures lists, tones). Types: stat, table, text, line, area, bar, stacked_bar, sparkline, combo, paired_bar, aging, bullet, waterfall, calendar, donut, ranked_bar, funnel. Fields: use_skill("dashboard-authoring", part="reference/spec-v2.md"). Measure SQL is compiled and frozen. Refuses what preview reports as an error, a filter over 50 values, or a failed dry run. HTML is refused. Pass a one-line change_note. Revising a slug makes the next version."""
    start = time.monotonic()
    args = (dict(_spec_args(spec)) if _has_spec(spec) else {}) | {
        "slug": slug, "change_note": change_note}
    if preview_id:
        args["preview_id"] = str(preview_id)[:40]  # Quick 036
    refused = False
    try:
        result, refused = await _save(slug, spec, change_note, preview_id)
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("save_dashboard_version", args, result[:1000], start, refused=refused)
    return result


# ── Phase 12 (AGENT-08): read-only view of pending dashboard proposals ──
async def pending_dashboard_proposals(limit: int = 20) -> list[dict]:
    """Pending publish_dashboard proposals, newest first. Whitelists six keys so the
    raw preview token in payload.draft_url can never leak."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id, payload, created_at FROM actions "
        "WHERE kind = 'publish_dashboard' AND status = 'proposed' "
        "ORDER BY id DESC LIMIT $1", min(max(int(limit), 1), 20))
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        p = p if isinstance(p, dict) else {}
        ca = r["created_at"]
        out.append({"action_id": r["id"], "slug": p.get("slug"), "version": p.get("version"),
                    "title": p.get("title"), "change_note": p.get("change_note"),
                    "created_at": ca.isoformat() if hasattr(ca, "isoformat") else str(ca)})
    return out


def is_owner_thread(thread) -> bool:
    """True for the owner's surfaces only: in-app threads, the console, or the owner's
    own WhatsApp number. Group chats and other numbers never count (review CR-02)."""
    from . import appchannel
    from .config import get_settings
    if not thread:
        return False
    if appchannel.is_app_thread(thread) or thread == "dashboard":
        return True
    owner = "".join(c for c in (get_settings().owner_number or "") if c.isdigit())
    t = "".join(c for c in str(thread) if c.isdigit())
    return bool(owner) and not str(thread).endswith("@g.us") and t == owner


@tool
async def list_dashboard_proposals() -> str:
    """List dashboard drafts that are awaiting the owner's approval (read-only). Use it to answer status questions truthfully; it never creates or changes anything."""
    start = time.monotonic()
    try:
        items = await pending_dashboard_proposals()
        if items and not is_owner_thread(ctx_phone.get()):
            result = (f"{len(items)} dashboard proposal(s) are waiting for the owner's approval. "
                      "Details are shown only to the owner.")
        else:
            result = (json.dumps(items, default=str) if items
                      else "There are no dashboard drafts awaiting approval.")
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("list_dashboard_proposals", {}, result[:1000], start)
    return result
# ── End Phase 12 (AGENT-08) ──
