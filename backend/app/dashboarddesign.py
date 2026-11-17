"""Phase 22 (DCAP-17, DCAP-02, DCAP-11): design lint and a text wireframe for spec v2 previews.

Pure module: no database, no settings reads. Everything the checks need is passed in:

- spec: the parsed SpecV2 (its model_fields_set tells an explicit "none" from a missing field)
- stored: the bound stored dict (sections, tiles with qid, date_control, filters)
- queries: the stored query entries (compiled measure entries carry request.follow, request.kind)
- units: {measure name: unit}; measure_sql: {name: (agg, expr, table)}
- samples: {"rows": {qid: [sample rows]}, "counts": {qid: row count}, "bytes": {qid: mean
  JSON bytes per row}, "filters": {filter id: distinct value count}}
- notes: compile notes (strings); caps: (snapshot rows, snapshot bytes)

design_lint returns [{rule, path, message, level}], level "warning" or "error". v1 specs get [].
"""

import re

from . import dashboardspec as ds

MAX_FILTER_VALUES = 50
WIREFRAME_CAP = 1500

_PERIOD = re.compile(r"\b(today|yesterday|daily|day|week|month|quarter|year|fy|last|this|since"
                     r"|now)|\d", re.I)
_OWED = re.compile(r"sales|revenue|orders|dispatch|inward|production", re.I)
# Longest range (in days) each preset can cover, for the last-year history check.
_PRESET_SPAN = {"today": 1, "yesterday": 1, "last_7_days": 7, "last_30_days": 30,
                "last_90_days": 90, "this_month": 31, "last_month": 62, "this_quarter": 92,
                "this_fy": 366}


def _f(rule, path, message, level="warning") -> dict:
    return {"rule": rule, "path": path, "message": message, "level": level}


def _is_v2(spec) -> bool:
    return isinstance(spec, ds.SpecV2)


def _names(t) -> list:
    b = getattr(t, "measure", None)
    if b is not None:
        return [b.name]
    m = getattr(t, "measures", None)
    return list(m.names) if m is not None else []


def _rows(samples, qid) -> list:
    return list(((samples or {}).get("rows") or {}).get(qid) or [])


def _qid_of(stored, tile_id):
    for t in (stored or {}).get("tiles", []):
        if t.get("id") == tile_id:
            return t.get("qid")
    return None


def _presets_tiles(queries) -> set:
    return {(q.get("request") or {}).get("tile") for q in queries or []
            if (q.get("request") or {}).get("kind") == "presets"}


# -- one function per rule ---------------------------------------------------------------

def design_good_direction(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type == "stat":
            controlled = t.date == "control" and t.comparison != "none"
            fixed = t.compare != "none"
            if (controlled or fixed) and "good_direction" not in t.model_fields_set:
                out.append(_f("design_good_direction", f"tiles[{i}].good_direction",
                              "Set good_direction to up, down or none so the change is coloured "
                              "good or bad (none keeps it neutral)."))
        elif t.type == "table":
            for j, c in enumerate(t.columns):
                if c.delta and "good_direction" not in c.model_fields_set:
                    out.append(_f("design_good_direction",
                                  f"tiles[{i}].columns[{j}].good_direction",
                                  "Set good_direction on a column that shows a change."))
    return out


def _stored_tile(stored, tile_id) -> dict:
    for t in (stored or {}).get("tiles", []):
        if t.get("id") == tile_id:
            return t
    return {}


def design_series(spec, samples=None, stored=None, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type not in ds.CHART_TYPES_V2 or getattr(t, "y", None) is None:
            continue
        # Measure-bound tiles get y and series only in the stored form (bind_v2).
        st = _stored_tile(stored, t.id)
        ys = st["y"] if st.get("y") is not None else t.y
        series = st.get("series") or getattr(t, "series", None)
        n = len(ys)
        if series:
            n = max(n, len({r.get(series) for r in _rows(samples, st.get("qid"))}))
        if n > 4:
            out.append(_f("design_series", f"tiles[{i}]",
                          "More than 4 coloured series are hard to tell apart; keep 4 or fewer "
                          "or switch to ranked_bar or a table."))
    return out


def design_donut_parts(spec, samples=None, stored=None, queries=None, **_):
    """Parts are members (distinct x), not rows: a controlled donut has a row per day per member
    and a top-N donut has a compiled Other row."""
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type != "donut":
            continue
        st = _stored_tile(stored, t.id)
        qid = st.get("qid")
        req = next(((q.get("request") or {}) for q in queries or [] if q.get("id") == qid), {})
        n = ((samples or {}).get("members") or {}).get(qid)
        if n is None:
            rows = _rows(samples, qid)
            if req.get("follow") == "control":
                n = len({r.get(st.get("x")) for r in rows})
            else:
                n = ((samples or {}).get("counts") or {}).get(qid)
                if n is None:
                    n = len(rows)
                if req.get("other"):
                    n -= 1
        elif req.get("other"):
            n -= 1  # the compiled Other row is not one of the parts
        if n > 5:
            out.append(_f("design_donut_parts", f"tiles[{i}]",
                          "A donut shows at most 5 parts and groups smaller parts as Other; "
                          "consider ranked_bar."))
    return out


def design_combo_units(spec, units=None, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type != "combo":
            continue
        us = {(units or {}).get(n) for n in _names(t)} - {None}
        if len(us) > 1:
            out.append(_f("design_combo_units", f"tiles[{i}]",
                          "One axis means one unit; use two charts instead of a combo."))
    return out


def design_title_period(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type in ("text", "table") or getattr(t, "date", "fixed") != "fixed":
            continue
        if not _PERIOD.search(t.title or ""):
            out.append(_f("design_title_period", f"tiles[{i}].title",
                          "A fixed tile's title should say its period, such as 'last 30 days' "
                          "or 'this month'."))
    return out


def design_wide_table(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        if t.type == "table" and len(t.columns) > 6 and t.width not in (None, "full"):
            out.append(_f("design_wide_table", f"tiles[{i}]",
                          "A table with more than 6 columns needs width full."))
    return out


def design_distinct_custom(spec, queries=None, **_):
    owners = _presets_tiles(queries)
    return [_f("design_distinct_custom", f"tiles[{i}]",
               "A distinct count on a controlled tile shows Not available on custom ranges; "
               "restrict the presets or use a fixed tile.")
            for i, t in enumerate(spec.tiles) if t.id in owners]


def _norm(s) -> str:
    return re.sub(r"[\s\"]+", "", str(s or "").lower())


def _sql_forms(agg, expr) -> list:
    """How a measure's aggregation reads in plain SQL (agg names are not SQL function names)."""
    a, e = _norm(agg), _norm(expr)
    if a == "average":
        return [f"avg({e})"]
    if a == "count_distinct":
        return [f"count(distinct{e})"]
    if a == "sum_boolean":
        return [f"sum(casewhen{e}then1else0end)"]
    return [f"{a}({e})"]


def design_raw_sql(spec, measure_sql=None, **_):
    out = []
    for i, q in enumerate(spec.queries):
        sql = _norm(getattr(q, "sql", None))
        if not sql:
            continue
        for name, (agg, expr, table) in (measure_sql or {}).items():
            base = _norm(table).split(".")[-1]
            if any(form in sql for form in _sql_forms(agg, expr)) and base and base in sql:
                out.append(_f("design_raw_sql", f"queries[{i}]",
                              f"This SQL recomputes the measure {name}; bind the measure "
                              "instead of declaring SQL."))
                break
    return out


def design_owed(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        cols = [c for c in getattr(t, "columns", []) if c.owed_when is not None]
        has = getattr(t, "owed_when", None) is not None or cols
        if not has:
            continue
        text = " ".join([t.title or ""] + _names(t) + [c.column for c in cols])
        if _OWED.search(text):
            out.append(_f("design_owed", f"tiles[{i}].owed_when",
                          "Gold means the viewer owes an action; remove owed_when from a number "
                          "nobody acts on, such as a sales total."))
    return out


def design_tone(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        ys = getattr(t, "y", None)
        if not ys or t.type not in ds.CHART_TYPES_V2:
            continue
        if len(ys) == 1 and not getattr(t, "series", None) and ys[0].tone is not None:
            out.append(_f("design_tone", f"tiles[{i}].y[0].tone",
                          "A tone on a single-series chart adds no meaning; remove it."))
    return out


def design_tone_pair(spec, **_):
    out = []
    for i, t in enumerate(spec.tiles):
        ys = getattr(t, "y", None) or []
        tones = {y.tone for y in ys}
        if {"accent", "outward"} <= tones and not t.note:
            out.append(_f("design_tone_pair", f"tiles[{i}]",
                          "Accent and outward on one chart need a legend note saying what each "
                          "colour means (set note)."))
    return out


def design_horizon(spec, **_):
    dc = spec.date_control
    if dc is None:
        return []
    offered = list(dc.presets or ds.PRESETS)
    out = []
    if "this_fy" in offered and dc.horizon_days < 732:
        out.append(_f("design_horizon", "date_control.horizon_days",
                      "this_fy can reach back almost two years; set horizon_days to 732 or "
                      "drop this_fy from presets."))
    last_year = any(
        getattr(t, "date", "fixed") == "control" and (
            (t.type == "stat" and t.comparison in ("last_year", "both"))
            or getattr(t, "ghost", "none") == "last_year")
        for t in spec.tiles)
    longest = max((_PRESET_SPAN.get(p, 0) for p in offered), default=0)
    if last_year and 365 + longest > dc.horizon_days:
        out.append(_f("design_horizon", "date_control",
                      f"A last-year comparison over the longest offered range ({longest} days) "
                      f"needs horizon_days of at least {365 + longest}; raise horizon_days or "
                      "offer only shorter presets."))
    # C-08: a controlled tile split by a dimension or following page filters stores at most 90 days,
    # so a last-year comparison on it can never find last year's rows.
    for i, t in enumerate(spec.tiles):
        asks = ((t.type == "stat" and t.comparison in ("last_year", "both"))
                or getattr(t, "ghost", "none") == "last_year")
        binding = getattr(t, "measure", None) or getattr(t, "measures", None)
        narrow = bool(getattr(binding, "by", None)) or (
            bool(getattr(t, "follow_filters", False)) and bool(spec.filters))
        if getattr(t, "date", "fixed") == "control" and asks and narrow:
            out.append(_f("design_horizon", f"tiles[{i}]",
                          "This tile is split by a dimension or follows page filters, so it stores "
                          "at most 90 days and last year is never available; use a fixed tile, or "
                          "drop the split and the filters."))
    return out


def design_unused_control(spec, **_):
    if spec.date_control is not None and not any(
            getattr(t, "date", "fixed") == "control" for t in spec.tiles):
        return [_f("design_unused_control", "date_control",
                   "No tile follows the date control; set date control on a tile or remove "
                   "date_control.")]
    return []


def design_unused_filter(spec, **_):
    if any(getattr(t, "follow_filters", False) for t in spec.tiles):
        return []
    return [_f("design_unused_filter", f"filters[{i}]",
               "No tile follows this filter; set follow_filters on a tile or remove it.")
            for i, _x in enumerate(spec.filters)]


def design_naive_time(spec, notes=None, **_):
    return [_f("design_naive_time", "queries",
               f"{n}; confirm the clock before trusting day totals.")
            for n in (notes or []) if "has no time zone" in str(n)]


RULES = (design_good_direction, design_series, design_donut_parts, design_combo_units,
         design_title_period, design_wide_table, design_distinct_custom, design_raw_sql,
         design_owed, design_tone, design_tone_pair, design_horizon, design_unused_control,
         design_unused_filter, design_naive_time)


def _errors(spec, stored, queries, samples, caps):
    out = []
    for i, f in enumerate(spec.filters):
        n = ((samples or {}).get("filters") or {}).get(f.id)
        if n is not None and n > MAX_FILTER_VALUES:
            out.append(_f("filters", f"filters[{i}]",
                          f"Filter {f.id} has {n} values; at most {MAX_FILTER_VALUES}. Use a "
                          "dimension with fewer values.", "error"))
    out.extend(size_errors(queries, samples, caps))
    return out


def size_errors(queries, samples, caps):
    """Errors for queries over the snapshot row or byte caps (preview and save share this)."""
    out = []
    if caps:
        rcap, bcap = caps
        counts = (samples or {}).get("counts") or {}
        sizes = (samples or {}).get("bytes") or {}
        for q in queries or []:
            n = counts.get(q.get("id"))
            if n is None:
                continue
            est = n * (sizes.get(q.get("id")) or 0)
            if n > rcap or est > bcap:
                out.append(_f("size", f"queries[{q.get('id')}]",
                              f"Query {q.get('id')} returns {n} rows (about {int(est)} bytes), "
                              f"over the {rcap}-row or {bcap}-byte snapshot cap; narrow the "
                              "window, add top or drop a by.", "error"))
    return out


def design_lint(spec, stored, queries, *, units=None, samples=None, notes=None,
                measure_sql=None, caps=None) -> list[dict]:
    if not _is_v2(spec):
        return []
    kw = dict(stored=stored, queries=queries, units=units, samples=samples, notes=notes,
              measure_sql=measure_sql)
    out = _errors(spec, stored, queries, samples, caps)
    for rule in RULES:
        out.extend(rule(spec, **kw))
    return out


# -- wireframe -----------------------------------------------------------------------------

def wireframe(stored: dict, queries: list[dict]) -> str:
    follow = {q.get("id"): (q.get("request") or {}).get("follow") or "fixed"
              for q in queries or []}
    tiles = {t["id"]: t for t in stored.get("tiles", [])}
    dc = stored.get("date_control")
    head = f"Page '{stored.get('title', '')}': "
    head += (f"date control default {dc.get('default')}, horizon {dc.get('horizon_days')} days"
             if dc else "no date control")
    head += f"; locale {stored.get('number_locale') or 'default'}"
    lines = [head]
    for n, sec in enumerate(stored.get("sections", []), 1):
        items, stats, stat_at = [], 0, None
        for tid in sec.get("tiles", []):
            t = tiles.get(tid)
            if t is None:
                continue
            if t["type"] == "stat":
                stats += 1
                if stat_at is None:
                    stat_at = len(items)
                    items.append(None)
                continue
            width = t.get("width") or ds.DEFAULT_WIDTH.get(t["type"], "half")
            if t["type"] == "text":
                items.append(f"text ({width})")
            else:
                f = follow.get(t.get("qid"), "fixed")
                items.append(f"{t['type']} '{t.get('title', '')}' ({width}, "
                             f"{'follows control' if f == 'control' else f})")
        if stat_at is not None:
            items[stat_at] = f"{stats} stat{'s' if stats != 1 else ''} (2-up on phones)"
        count = len(sec.get("tiles", []))
        lines.append(f"Section {n} '{sec.get('title') or ''}': {count} tiles - "
                     + ", ".join(items))
    text = "\n".join(lines)
    return text if len(text) <= WIREFRAME_CAP else text[:WIREFRAME_CAP - 3] + "..."
