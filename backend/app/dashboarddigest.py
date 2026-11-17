"""Plain-text digest of a dashboard's stat tiles (Phase 22, DCAP-16).

Pure module: no clock, no database. It mirrors the renderer's algorithms (ic-render 2.0.0 rangeValue,
compareValue, pickStat, formatValue for stat tiles) so the digest shows the numbers the page shows,
computed from the same stored snapshot rows. The shared vectors live in
tests/fixtures/dashboard_reagg_vectors.json and dashboard_date_vectors.json.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import dashdates

DAY_COL = "metric_time__day"
DASH = "-"

PRESET_LABELS = {
    "today": "Today", "yesterday": "Yesterday", "last_7_days": "Last 7 days",
    "last_30_days": "Last 30 days", "last_90_days": "Last 90 days", "this_month": "This month",
    "last_month": "Last month", "this_quarter": "This quarter", "this_fy": "This financial year",
}
_NA_TEXT = {"custom": "Not available for a custom range", "range": "Not available for this range"}
_ISO_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


# ---- numbers -----------------------------------------------------------------------------------

def to_num(v):
    """Same as the renderer's toNum: numbers and numeric strings, never booleans."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip() != "":
        try:
            n = float(v)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    try:  # Decimal from a database row
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _sum_col(rows, col) -> float:
    t = 0.0
    for r in rows:
        n = to_num(r.get(col))
        if n is not None:
            t += n
    return t


def _day_of(row) -> str:
    v = row.get(DAY_COL)
    return v[:10] if isinstance(v, str) else ""


def _rows_in(rows, frm: str, to: str) -> list:
    out = []
    for r in rows:
        d = _day_of(r)
        if d != "" and frm <= d <= to:
            out.append(r)
    return out


def _eval_node(node, inputs, depth):
    if not isinstance(node, dict) or depth > 12:
        return None
    if "num" in node:
        return to_num(node["num"])
    if "in" in node:
        return to_num(inputs.get(node["in"])) if inputs else None
    if "op" in node:
        a = _eval_node(node.get("a"), inputs, depth + 1)
        b = _eval_node(node.get("b"), inputs, depth + 1)
        if a is None or b is None:
            return None
        op = node["op"]
        if op == "+":
            out = a + b
        elif op == "-":
            out = a - b
        elif op == "*":
            out = a * b
        elif op == "/":
            out = None if b == 0 else a / b
        else:
            out = None
        return out if out is not None and math.isfinite(out) else None
    return None


def eval_formula(tree, inputs):
    return _eval_node(tree, inputs, 1)


def _part_cols(meta) -> dict:
    p = meta.get("parts") or {}
    c = meta.get("column")
    if meta.get("kind") == "avg":
        return {"sum": p.get("sum") or f"{c}__sum", "count": p.get("count") or f"{c}__count"}
    if meta.get("kind") == "ratio":
        return {"num": p.get("num") or f"{c}__num", "den": p.get("den") or f"{c}__den"}
    return p


def _aggregate(meta, rows):
    k = meta.get("kind")
    c = meta.get("column")
    if k == "sum":
        return _sum_col(rows, c)
    if k in ("min", "max"):
        best = None
        for r in rows:
            n = to_num(r.get(c))
            if n is None:
                continue
            if best is None or (n < best if k == "min" else n > best):
                best = n
        return best
    if k == "avg":
        cols = _part_cols(meta)
        cnt = _sum_col(rows, cols["count"])
        return None if cnt == 0 else _sum_col(rows, cols["sum"]) / cnt
    if k == "ratio":
        cols = _part_cols(meta)
        den = _sum_col(rows, cols["den"])
        return None if den == 0 else _sum_col(rows, cols["num"]) / den
    if k == "formula":
        inputs = {a: _sum_col(rows, col) for a, col in (meta.get("parts") or {}).items()}
        return eval_formula(meta.get("formula"), inputs)
    if k in ("closing", "opening"):
        pick = None
        for r in rows:
            d = _day_of(r)
            if d == "":
                continue
            if pick is None or (d > pick if k == "closing" else d < pick):
                pick = d
        if pick is None:
            return None
        return _sum_col([r for r in rows if _day_of(r) == pick], c)
    return None


def _preset_value(ctx) -> dict:
    if not ctx.get("preset") or ctx.get("preset_rows") is None:
        return {"na": "custom"}
    period = ctx.get("period") or "current"
    for pr in ctx["preset_rows"]:
        if pr.get("preset") == ctx["preset"] and pr.get("period") == period:
            if pr.get("today") != ctx.get("today"):
                return {"na": "nodata"}
            return {"value": to_num(pr.get("value"))}
    return {"na": "nodata"}


def _range_agg(meta, rows, rng, ctx) -> dict:
    """{'value': v} or {'na': code}, as the renderer's rangeAgg."""
    rows = rows or []
    if meta.get("kind") == "now":
        if len(rows) > 1:
            return {"na": "nodata"}
        return {"value": to_num(rows[0].get(meta.get("column"))) if rows else None}
    hs = ctx.get("horizon_start")
    if hs and rng["from"] < hs:
        return {"na": "range"}
    to = rng["to"]
    as_of = ctx.get("as_of")
    if as_of:
        if rng["from"] > as_of:
            return {"na": "nodata"}
        if to > as_of:
            to = as_of
    if meta.get("kind") == "distinct":
        if rng["from"] == rng["to"]:
            day = _rows_in(rows, rng["from"], rng["from"])
            return {"value": _sum_col(day, meta.get("column")) if day else 0.0}
        return _preset_value(ctx)
    return {"value": _aggregate(meta, _rows_in(rows, rng["from"], to))}


def range_value(meta, rows, rng, ctx):
    """The value of one range, or {'na': code}. rng is {'from': iso, 'to': iso}."""
    r = _range_agg(meta, rows, rng, ctx)
    return {"na": r["na"]} if "na" in r else r["value"]


def _iso(d: date) -> str:
    return d.isoformat()


def compare_value(meta, rows, rng, kind, ctx) -> dict:
    """{'current','previous','delta'} or {'na': code}, with the as-of cut of the renderer."""
    period = "last_year" if kind == "last_year" else "previous"
    cur = _range_agg(meta, rows, rng, {**ctx, "period": "current"})
    if "na" in cur:
        return {"na": cur["na"]}
    f = date.fromisoformat(rng["from"])
    t = date.fromisoformat(rng["to"])
    if period == "last_year":
        pf, pt = dashdates.last_year_range(f, t)
    else:
        pf, pt = dashdates.previous_range(f, t, ctx.get("preset"))
    as_of = ctx.get("as_of")
    # Data lags today: the current side stops at as_of, so cut the comparison by the same number
    # of days. Count-distinct and now values are server-computed and are not cut.
    if as_of and rng["to"] > as_of and meta.get("kind") not in ("distinct", "now"):
        lag = (t - date.fromisoformat(as_of)).days
        pt = pt - timedelta(days=lag)
        if pt < pf:
            pt = pf
    prev = _range_agg(meta, rows, {"from": _iso(pf), "to": _iso(pt)}, {**ctx, "period": period})
    if "na" in prev:
        return {"na": prev["na"]}
    a = to_num(cur["value"])
    b = to_num(prev["value"])
    delta = None if a is None or b is None or b == 0 else (a - b) / abs(b)
    return {"current": cur["value"], "previous": prev["value"], "delta": delta}


def as_of_day(ts, zone: str):
    """The calendar day of an ISO timestamp in a zone, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace(" ", "T").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_zone(zone)).date()
    except ValueError:
        return None


def _zone(name):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


# ---- fixed stats (v1 semantics) ------------------------------------------------------------------

def _period_key(v, grain, today_iso=None):
    if not isinstance(v, str):
        return "" if v is None else str(v)
    m = _ISO_PREFIX.match(v)
    if not m:
        return v
    if grain == "week":
        d = date.fromisoformat(m.group(0))
        return _iso(d - timedelta(days=d.weekday()))
    if grain == "month":
        return f"{m.group(1)}-{m.group(2)}"
    if grain == "quarter":
        return f"{m.group(1)}-Q{(int(m.group(2)) - 1) // 3 + 1}"
    if grain == "year":
        return m.group(1)
    return v[:10]


def pick_stat(rows, tile, today_iso) -> dict:
    rows = rows or []
    idx = len(rows) - 1
    if idx >= 0 and tile.get("pick") == "last_complete" and tile.get("grain") and tile.get("x"):
        if _period_key(rows[idx].get(tile["x"]), tile["grain"]) == _period_key(today_iso, tile["grain"]):
            idx -= 1
    if idx < 0:
        return {"value": None, "previous": None}
    row = rows[idx]
    previous = None
    cmp = tile.get("compare")
    if cmp == "previous_row":
        previous = rows[idx - 1].get(tile.get("column")) if idx > 0 else None
    elif isinstance(cmp, dict) and cmp.get("column"):
        previous = row.get(cmp["column"])
    return {"value": row.get(tile.get("column")), "previous": previous}


# ---- text ----------------------------------------------------------------------------------------

def _group(int_digits: str, indian: bool) -> str:
    if len(int_digits) <= 3:
        return int_digits
    if not indian:
        return f"{int(int_digits):,}"
    head, tail = int_digits[:-3], int_digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _half_up(x, digits: int, exact: bool = False) -> str:
    """x to `digits` places, ties up (not Python's half to even). Intl.NumberFormat rounds the
    shortest decimal of the double; Number.prototype.toFixed (deltas) rounds its exact binary value."""
    d = x if isinstance(x, Decimal) else (Decimal(x) if exact else Decimal(repr(float(x))))
    try:
        # The default context (28 digits) overflows for |x| >= ~1e26: widen it to fit the result.
        with localcontext() as ctx:
            ctx.prec = max(28, d.adjusted() + digits + 2) if d.is_finite() else 28
            q = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
            return f"{q:.{digits}f}"
    except InvalidOperation:
        return f"{float(x):.{digits}f}"


def _fixed(n: float, digits_min: int, digits_max: int, indian: bool) -> str:
    s = _half_up(abs(n), digits_max)
    whole, _, frac = s.partition(".")
    while len(frac) > digits_min and frac.endswith("0"):
        frac = frac[:-1]
    out = _group(whole, indian) + ("." + frac if frac else "")
    return ("-" if n < 0 and float(s) != 0 else "") + out


def _compact(n: float, locale: str | None) -> str:
    indian = locale == "en-IN"
    steps = ([(1e12, "LCr"), (1e10, "KCr"), (1e7, "Cr"), (1e5, "L"), (1e3, "K")] if indian
             else [(1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")])
    a = abs(n)
    for i, (size, suffix) in enumerate(steps):
        if a >= size:
            v = Decimal(_half_up(Decimal(repr(float(a))) / Decimal(int(size)), 1))
            if i > 0 and v * Decimal(int(size)) >= Decimal(int(steps[i - 1][0])):  # 999.95K carries to 1M
                size, suffix = steps[i - 1]
                v = Decimal(_half_up(Decimal(repr(float(a))) / Decimal(int(size)), 1))
            txt = f"{v:.1f}".rstrip("0").rstrip(".")
            return ("-" if n < 0 else "") + txt + suffix
    if _half_up(a, 1) == "1000.0":  # 999.95 to 999.99 round up to 1K, as Intl prints it
        return ("-" if n < 0 else "") + "1K"
    return _fixed(n, 0, 1, indian)


def format_value(v, fmt, *, locale=None, currency=None, unit=None, compact=True) -> str:
    """Plain text of a value in the render form's locale; en-IN compacts to L and Cr."""
    if v is None:
        return DASH
    n = to_num(v)
    if n is None:
        return str(v)
    indian = locale == "en-IN"
    if fmt in ("percent", "percent_value"):
        out = _fixed(n * 100 if fmt == "percent" else n, 0, 1, indian) + "%"
    elif fmt == "currency":
        out = ((currency or "USD") + " ") + (_compact(n, locale) if compact else _fixed(n, 2, 2, indian))
    elif compact or fmt == "compact":
        out = _compact(n, locale)
    elif fmt == "integer":
        out = _fixed(n, 0, 0, indian)
    elif fmt == "decimal1":
        out = _fixed(n, 1, 1, indian)
    elif fmt == "decimal2":
        out = _fixed(n, 2, 2, indian)
    else:
        out = _fixed(n, 0, 2, indian)
    if unit:
        out += " " + unit
    return out


def _owed(value, cond) -> bool:
    a = to_num(value)
    b = to_num(cond.get("value")) if isinstance(cond, dict) else None
    if a is None or b is None:
        return False
    op = cond.get("op")
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b, "!=": a != b}.get(op, False)


def _delta_text(cur, prev):
    a, b = to_num(cur), to_num(prev)
    if a is None or b is None or b == 0:
        return None
    pct = (a - b) / abs(b) * 100
    shown = _half_up(abs(pct), 1, exact=True)
    if shown == "0.0":
        return "no change"
    return ("up " if pct > 0 else "down ") + f"{shown}%"


def _delta_label(kind, rng, preset) -> str:
    if kind == "last_year":
        return "vs same days last year"
    if preset in ("this_month", "last_month"):
        return "vs previous month"
    if preset == "this_quarter":
        return "vs previous quarter"
    if preset == "this_fy":
        return "vs previous year"
    n = (date.fromisoformat(rng["to"]) - date.fromisoformat(rng["from"])).days + 1
    return "vs previous day" if n == 1 else f"vs previous {n} days"


def _na_text(code) -> str:
    return _NA_TEXT.get(code, "No data yet")


def _snap_rows(snap) -> list:
    if not isinstance(snap, dict):
        return []
    rows = snap.get("rows")
    if rows is None:
        rows = snap.get("data")
    return [r for r in (rows or []) if isinstance(r, dict)]


def _stat_tiles(form: dict) -> list:
    by_id = {t["id"]: t for t in form.get("tiles", []) if isinstance(t, dict) and "id" in t}
    order: list = []
    if form.get("sections") is not None:
        for sec in form["sections"]:
            order.extend(sec.get("tiles") or [])
    else:
        for row in form.get("rows") or []:
            order.extend(row if isinstance(row, list) else [])
    order.extend(i for i in by_id if i not in order)
    seen: set = set()
    out = []
    for tid in order:
        t = by_id.get(tid)
        if t and tid not in seen and t.get("type") == "stat":
            seen.add(tid)
            out.append(t)
    return out


def _tile_line(tile, form, snapshots, now_utc, form_tz, dc) -> tuple:
    """(line, data_as_of or None, preset label or None)"""
    snap = snapshots.get(tile.get("qid")) if tile.get("qid") else None
    rows = _snap_rows(snap)
    zone = (snap or {}).get("timezone") or form_tz
    today = now_utc.astimezone(_zone(zone)).date()
    as_of = as_of_day((snap or {}).get("data_as_of"), zone)
    opts = {"locale": form.get("number_locale"), "currency": tile.get("currency") or None,
            "unit": tile.get("unit") or None,
            # the v1 page draws stats in full ("1,234,567"), not compact; only spec v2 compacts
            "compact": form.get("render_version") != 1}
    title = tile.get("title") or tile.get("column") or "Stat"
    stamp = (snap or {}).get("data_as_of")
    controlled = bool(dc) and tile.get("follow") == "control"
    if snap is None:
        return f"{title}: No data yet", stamp, None
    if not rows:
        # the page treats a refreshed, error-free, empty controlled snapshot as a real zero
        real_zero = bool(snap.get("refreshed_at")) and not snap.get("error") and controlled
        if not real_zero:
            msg = ("No rows for this period" if snap.get("refreshed_at") and not snap.get("error")
                   else "No data yet")
            return f"{title}: {msg}", stamp, None
    if not controlled:
        picked = pick_stat(rows, tile, _iso(today))
        txt = format_value(picked["value"], tile.get("format"), **opts)
        if _owed(picked["value"], tile.get("owed_when")):
            txt += " (owed)"
        cmp = tile.get("compare")
        if cmp and cmp != "none":
            d = _delta_text(picked["value"], picked["previous"])
            if d:
                txt += f" ({d} {tile['compare_label']})" if tile.get("compare_label") else f" ({d})"
        return f"{title}: {txt}", stamp, None
    measures = tile.get("measures") or []
    meta = (next((m for m in measures if m.get("column") == tile.get("column")), None)
            or (measures[0] if measures else None) or {"kind": "sum", "column": tile.get("column")})
    preset = dc.get("default") or "last_7_days"
    cur_rng = dashdates.preset_range(preset, "current", today, dc.get("fy_start_month", 4))
    rng = {"from": _iso(cur_rng[0]), "to": _iso(cur_rng[1])}
    hd = tile.get("horizon_days")
    pq = tile.get("presets_qid")
    ctx = {
        "today": _iso(today), "as_of": _iso(as_of) if as_of else None,
        "horizon_start": _iso(dashdates.horizon_start(today, hd)) if hd else None,
        "preset_rows": _snap_rows(snapshots.get(pq)) if pq and snapshots.get(pq) else None,
        "preset": preset, "period": "current",
    }
    cur = range_value(meta, rows, rng, ctx)
    label = PRESET_LABELS.get(preset)
    if isinstance(cur, dict):
        return f"{title}: {_na_text(cur['na'])}", stamp, label
    txt = format_value(cur, tile.get("format"), **opts)
    if _owed(cur, tile.get("owed_when")):
        txt += " (owed)"
    cmp = tile.get("comparison") or "previous_period"
    kinds = ["previous_period", "last_year"] if cmp == "both" else (
        [cmp] if cmp in ("previous_period", "last_year") else [])
    notes: list = []
    for k in kinds:
        res = compare_value(meta, rows, rng, k, ctx)
        if "na" in res:
            m = _na_text(res["na"])
            if m not in notes:
                notes.append(m)
            continue
        d = _delta_text(res["current"], res["previous"])
        if d:
            notes.append(f"{d} {_delta_label(k, rng, preset)}")
    if notes:
        txt += " (" + ", ".join(notes) + ")"
    return f"{title}: {txt}", stamp, label


def _stamp(ts: str | None, zone: str) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(_zone(zone))
    return f"{dt.day} {dt:%b} {dt:%H:%M}"


def digest_lines(render_form: dict, snapshots: dict, now_utc: datetime, default_tz: str = "UTC") -> list:
    """Lines for the visible stat tiles of a render form, from stored snapshots only.

    snapshots maps query id to {'rows' (or 'data'), 'data_as_of', 'timezone', 'error'}. A render form
    whose timezone is null (as served) takes default_tz. Returns [] with no stat tiles."""
    tiles = _stat_tiles(render_form)
    if not tiles:
        return []
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    form_tz = render_form.get("timezone") or default_tz or "UTC"
    dc = render_form.get("date_control")
    lines: list = []
    stamps: list = []
    label = None
    for t in tiles:
        line, stamp, lab = _tile_line(t, render_form, snapshots or {}, now_utc, form_tz, dc)
        lines.append("- " + line)
        if stamp:
            stamps.append(stamp)
        label = label or lab
    newest = None
    for s in stamps:
        a = _parse_ts(s)
        if a is not None and (newest is None or a > newest[0]):
            newest = (a, s)
    inside = []
    if label:
        inside.append(label)
    st = _stamp(newest[1], form_tz) if newest else None
    if st:
        inside.append(f"as of {st}")
    head = str(render_form.get("title") or "Dashboard")
    if inside:
        head += " (" + ", ".join(inside) + ")"
    return [head] + lines


def _parse_ts(s):
    try:
        dt = datetime.fromisoformat(str(s).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
