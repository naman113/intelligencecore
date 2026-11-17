"""Numbers-gate planner for the dashboard eval (22-19, DCAP-17). Pure: no clock, no DB.

plan_checks maps every value the rendered draft shows (data-ic-value with tile, column, row and
series member, C-11) to the reference query that means the same thing, or records why it is not
checked. compare decides whether a shown value equals the reference result.
"""

import calendar
import re
from datetime import date, timedelta

from app import dashdates, modelcompiler
from app.dashboardspec import DEFAULT_WINDOW_DAYS, GRAIN_DAYS

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SPAN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


def _d(s):
    return date.fromisoformat(s)


def _period_start(d, grain):
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    if grain == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if grain == "year":
        return date(d.year, 1, 1)
    return d


def _add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def _period_end(d, grain):
    if grain == "week":
        return d + timedelta(days=6)
    if grain == "month":
        return d.replace(day=calendar.monthrange(d.year, d.month)[1])
    if grain == "quarter":
        return _add_months(d, 3) - timedelta(days=1)
    if grain == "year":
        return date(d.year, 12, 31)
    return d


def _back_periods(d, grain, n):
    """First day of the period n periods before the one holding d."""
    s = _period_start(d, grain)
    if grain == "week":
        return s - timedelta(weeks=n)
    if grain == "month":
        return _add_months(s, -n)
    if grain == "quarter":
        return _add_months(s, -3 * n)
    if grain == "year":
        return date(s.year - n, 1, 1)
    return s - timedelta(days=n)


def _lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _dim_expr(dims, dim):
    if dim in dims:
        return dims[dim]
    short = ".".join(dim.split(".")[-2:])
    return dims.get(short)


def _filter_cond(f, dims):
    expr = _dim_expr(dims, f.get("dimension", ""))
    op = f.get("op")
    vals = f.get("values") or []
    if expr is None or op not in modelcompiler.FILTER_OPS:
        return None
    if op == "is_null":
        return f"AND {expr} IS NULL"
    if op == "is_not_null":
        return f"AND {expr} IS NOT NULL"
    if op in ("in", "not_in"):
        if not vals:
            return None
        return f"AND {expr} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(_lit(x) for x in vals)})"
    if not vals:
        return None
    return f"AND {expr} {op} {_lit(vals[0])}"


def _out(dim):
    return modelcompiler._out_name(dim)


def _fixed_window(binding, grain, t):
    """(from, to) of a fixed tile's window, or None for 'no window'."""
    w = binding.get("window")
    if w:
        if "last_days" in w:
            return t - timedelta(days=int(w["last_days"]) - 1), t
        if "start" in w and "end" in w:
            return _d(w["start"]), _d(w["end"]) - timedelta(days=1)  # end is exclusive
        if "last_periods" in w and grain:
            return _back_periods(t, grain, int(w["last_periods"]) - 1), t
        return None
    if grain:
        # the compiled SQL keeps whole periods only (_fixed_sql): n periods back from today
        n = DEFAULT_WINDOW_DAYS[grain] // GRAIN_DAYS[grain]
        return _back_periods(t, grain, n - 1), t
    return None


def plan_checks(values, render_form, authoring, refs, today, client, refreshed=None):
    rtiles = {t["id"]: t for t in render_form.get("tiles", [])}
    atiles = {t["id"]: t for t in authoring.get("tiles", [])}
    dc = render_form.get("date_control") or authoring.get("date_control") or {}
    cref = (refs.get(client) if isinstance(refs, dict) else None) or {}
    out = []
    # The renderer shows a NULL member and a real text member "Not set" the same way, so two shown
    # "Not set" members with one key (tile, column, row, series) cannot be told apart: skip those.
    seen, dup = set(), set()
    for val in values:
        if _member_text(val) == "Not set":
            k = (val.get("tile"), val.get("col"), val.get("row"), val.get("series"))
            (dup if k in seen else seen).add(k)
    for val in values:
        base = {"tile": val.get("tile"), "col": val.get("col"), "row": val.get("row"),
                "shown": val.get("value")}
        k = (val.get("tile"), val.get("col"), val.get("row"), val.get("series"))
        if k in dup:
            out.append({**base, "skip": "several Not set members (NULL and the text Not set look the same)"})
            continue
        out.append(_one(val, base, rtiles, atiles, dc, cref, today, refreshed or {}))
    return out


def _member_text(val):
    return val.get("series") or val.get("row") or ""


def _one(val, base, rtiles, atiles, dc, cref, today, refreshed):
    rt = rtiles.get(val.get("tile"))
    if rt is None:
        return {**base, "error": "unknown tile"}
    col = val.get("col")
    mcol = next((m for m in rt.get("measures") or [] if m.get("column") == col), None)
    if mcol is None:
        return {**base, "skip": "not a measure column"}
    ref = cref.get(col)
    if ref is None:
        return {**base, "error": "no reference"}
    at = atiles.get(rt["id"]) or {}
    binding = at.get("measure") or at.get("measures") or {}
    dims = ref.get("dims") or {}
    row = val.get("row") or ""
    series = val.get("series")
    follow = rt.get("follow")
    grain = binding.get("grain") or rt.get("grain")
    controlled = follow == "control"
    row_is_date = bool(ISO.match(row))
    row_is_span = bool(SPAN.match(row))

    # step 2: day span
    span = None
    if mcol.get("kind") != "now":
        if controlled:
            if row_is_date:
                span = (_d(row), _d(row))
            elif row_is_span:
                m = SPAN.match(row)
                span = (_d(m.group(1)), _d(m.group(2)))
            else:
                span = dashdates.preset_range(dc.get("default", "last_7_days"), "current", today,
                                              int(dc.get("fy_start_month") or 4))
        else:
            t = refreshed.get(rt.get("qid")) or today
            win = _fixed_window(binding, grain, t)
            if win is None:
                return {**base, "skip": "no window"}
            span = win
            if grain and row_is_date:
                r = _d(row)
                span = (max(r, win[0]), min(_period_end(r, grain), win[1]))
                if span[0] > span[1]:
                    return {**base, "skip": "outside window"}
    row_is_member = bool(row) and not row_is_span and not (row_is_date and (controlled or grain))

    # step 3: member
    by = binding.get("by") or []
    member = series if series else (row if row_is_member else None)
    cond = []
    if rt.get("series") and not series:
        return {**base, "skip": "pivoted series without member"}
    if member is None:
        if by:
            return {**base, "skip": "member not shown"}
    else:
        outs = {_out(b): b for b in by}
        if rt.get("series"):
            mdim = outs.get(rt["series"])
        elif rt.get("x") in outs:
            mdim = outs[rt["x"]]
        elif by:
            first = (rt.get("by_columns") or [None])[0]
            mdim = outs.get(first) or by[0]
        else:
            mdim = None
        if mdim is None:
            return {**base, "skip": "member dimension unknown"}
        if not controlled and len(by) > 1:
            return {**base, "skip": "several by dimensions"}
        if member == "Other":
            return {**base, "skip": "Other"}
        expr = _dim_expr(dims, mdim)
        if expr is None:
            return {**base, "skip": "member dimension not in reference"}
        if member == "Not set":  # the renderer shows a NULL member as "Not set"; a text "Not set" matches too
            cond.append(f"AND ({expr} IS NULL OR {expr} = 'Not set')")
        else:
            cond.append(f"AND {expr} = {_lit(member)}")

    # step 4: binding filters
    for f in binding.get("filters") or []:
        c = _filter_cond(f, dims)
        if c is None:
            return {**base, "skip": "filter not translatable"}
        cond.append(c)

    sql = ref["sql"]
    if span is not None:
        sql = sql.replace("{from}", f"DATE '{span[0].isoformat()}'")
        sql = sql.replace("{to}", f"DATE '{span[1].isoformat()}'")
    sql = sql.replace("{extra}", " ".join(cond))
    return {**base, "sql": sql, "member": member,
            "span": ({"from": span[0].isoformat(), "to": span[1].isoformat()} if span else None)}


def compare(shown, expected, tol=1e-6):
    if expected is None or shown is None:
        return False
    return abs(float(shown) - float(expected)) <= tol * max(1.0, abs(float(expected)))
