"""Phase 13.1 (FCST-02): fixed forecasting script run nightly in the sandbox by forecasts.run_one_due.

Never agent-written. Reads /work/series.csv and /work/config.json, writes /work/result.json.
Deterministic; no network; stdlib + numpy + pandas + statsmodels only. The gateway reads this file as
text and passes it to sandbox.run_python; it is never imported by the gateway.
"""
import csv
import json
import math
import os
import sys
import warnings
from datetime import date, timedelta

SERIES = "/work/series.csv"
CONFIG = "/work/config.json"
RESULT = "/work/result.json"
METHODS = ("naive", "moving_avg", "seasonal_naive", "ets", "arima")
BASELINES = ("naive", "moving_avg", "seasonal_naive")
MA_WINDOW = {"day": 7, "week": 4, "month": 3}
MAX_ORIGINS = 5
GRAINS = ("day", "week", "month")
MAX_GRID = 100000
ARIMA_ORDERS = ((1, 1, 0), (0, 1, 1), (1, 1, 1), (2, 1, 1))
# Quick 013 (WR-02): complete periods with no rows after the last row before the series
# counts as stale (a stopped feed), not as a run of zeros
STALE_AFTER = {"day": 7, "week": 2, "month": 2}


class InputError(ValueError):
    pass


class StaleSeries(Exception):
    pass


def parse_series(text):
    rows = list(csv.reader(text.splitlines()))
    if not rows or [c.strip() for c in rows[0]] != ["period", "value"]:
        raise InputError("series.csv must start with the header period,value")
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != 2:
            raise InputError("line %d: expected 2 columns" % i)
        raw_d, raw_v = row[0].strip(), row[1].strip()
        try:
            if len(raw_d) != 10:
                raise ValueError(raw_d)
            d = date.fromisoformat(raw_d)
        except ValueError:
            raise InputError("line %d: bad date" % i)
        if raw_v == "":
            out.append((d, None))
            continue
        try:
            v = float(raw_v)
        except ValueError:
            raise InputError("line %d: value is not a number" % i)
        if not math.isfinite(v):
            raise InputError("line %d: value is not finite" % i)
        out.append((d, v))
    return out


def period_start(d, grain):
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    raise InputError("unknown grain")


def next_period(d, grain):
    if grain == "day":
        return d + timedelta(days=1)
    if grain == "week":
        return d + timedelta(days=7)
    if grain == "month":
        return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    raise InputError("unknown grain")


def _to_date(s):
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise InputError("bad as_of date")


def prepare(series, cfg):
    """Return (dates, values, notes) on the explicit grain grid, current incomplete period dropped."""
    grain = cfg.get("grain")
    if grain not in GRAINS:
        raise InputError("grain must be day, week or month")
    kind = cfg.get("kind", "flow")
    if kind not in ("flow", "level"):
        raise InputError("kind must be flow or level")
    as_of = _to_date(cfg.get("as_of"))
    max_history = int(cfg.get("max_history") or 3000)
    cutoff = period_start(as_of, grain)
    notes = []
    seen = {}
    for d, v in series:
        p = period_start(d, grain)
        if p in seen:
            raise InputError("duplicate period %s" % p.isoformat())
        seen[p] = v
    seen = {p: v for p, v in seen.items() if p < cutoff}
    if not seen:
        return [], [], notes
    first, last = min(seen), max(seen)
    # Quick 013 (WR-02): complete periods after the last row, up to the current one
    gap, p = 0, next_period(last, grain)
    while p < cutoff and gap <= STALE_AFTER[grain]:
        gap += 1
        p = next_period(p, grain)
    if gap > STALE_AFTER[grain]:
        raise StaleSeries("no rows after %s, more than %d complete %ss ago; the data may have stopped"
                          % (last.isoformat(), STALE_AFTER[grain], grain))
    if kind == "flow" and gap:
        # a flow period with no rows had no activity: extend to the last complete period
        notes.append("%d %s%s after %s had no rows; counted as 0"
                     % (gap, grain, "" if gap == 1 else "s", last.isoformat()))
        for _ in range(gap):
            last = next_period(last, grain)
    grid = []
    p = first
    while p <= last:
        grid.append(p)
        if len(grid) > MAX_GRID:
            raise InputError("series spans too many periods")
        p = next_period(p, grain)
    if kind == "flow":
        vals = [seen.get(p) if seen.get(p) is not None else 0.0 for p in grid]
    else:
        vals = [seen.get(p) for p in grid]
        lo = 0
        while lo < len(vals) and vals[lo] is None:
            lo += 1
        hi = len(vals)
        while hi > lo and vals[hi - 1] is None:
            hi -= 1
        grid, vals = grid[lo:hi], vals[lo:hi]
        seg = 0
        i = 0
        while i < len(vals):
            if vals[i] is None:
                j = i
                while vals[j] is None:
                    j += 1
                if j - i > 2:
                    seg = j
                i = j
            else:
                i += 1
        if seg:
            notes.append("gap longer than 2 periods; history before %s ignored" % grid[seg].isoformat())
            grid, vals = grid[seg:], vals[seg:]
        i = 0
        while i < len(vals):
            if vals[i] is None:
                j = i
                while vals[j] is None:
                    j += 1
                a, b = vals[i - 1], vals[j]
                for k in range(i, j):
                    vals[k] = a + (b - a) * (k - i + 1) / (j - i + 1)
                i = j
            else:
                i += 1
    if len(grid) > max_history:
        grid, vals = grid[-max_history:], vals[-max_history:]
    if grid:
        steps, p = 0, grid[-1]
        while p < cutoff and steps < 3:
            p = next_period(p, grain)
            steps += 1
        if steps > 2:
            notes.append("stale: last period %s is more than 2 periods before the current one" % grid[-1].isoformat())
    return grid, [float(v) for v in vals], notes


def result_failure(reason, message):
    return {"ok": False, "reason": reason, "message": message}


def write_result(obj, path=RESULT):
    text = json.dumps(obj, allow_nan=False, sort_keys=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def forecast(dates, values, cfg):
    h = int(cfg.get("horizon") or 0)
    if h < 1:
        raise InputError("horizon must be at least 1")
    grain = cfg["grain"]
    kind = cfg.get("kind", "flow")
    M = int(cfg.get("season") or 0)
    if M < 2:
        M = 0
    tail = int(cfg.get("actual_tail") or 26)
    n = len(values)
    need = max(12, 2 * h)
    if n < need:
        return result_failure("too_short", "%d periods; needs %d" % (n, need))

    warnings.filterwarnings("ignore")
    import numpy as np
    import pandas as pd
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.stats.diagnostic import acorr_ljungbox

    y = np.asarray(values, dtype=float)
    notes = []
    const = float(np.ptp(y)) == 0.0
    state = {"order": (1, 1, 0)}

    def fit(m, t):
        if m == "ets":
            seasonal = M > 0 and len(t) >= 2 * M + h
            return ETSModel(pd.Series(t), error="add", trend="add", damped_trend=True,
                            seasonal="add" if seasonal else None,
                            seasonal_periods=M if seasonal else None).fit(disp=False)
        return ARIMA(t, order=state["order"]).fit()

    def fc(m, t, k):
        if m == "naive":
            return np.repeat(t[-1], k), None
        if m == "moving_avg":
            return np.repeat(t[-MA_WINDOW[grain]:].mean(), k), None
        if m == "seasonal_naive":
            b = t[-M:][np.arange(k) % M]
            if M >= 12:
                den = t[-M - 8:-M].mean()
                if den > 0:
                    b = b * t[-8:].mean() / den
            return np.asarray(b, dtype=float), None
        sf = fit(m, t).get_prediction(start=len(t), end=len(t) + k - 1).summary_frame(alpha=0.2)
        return np.asarray(sf["mean"].values, dtype=float), np.asarray(sf.iloc[:, -2:].values, dtype=float)

    models = ["naive", "moving_avg"]
    seasonal_ok = M > 0 and n >= M + h + 8
    if seasonal_ok:
        models.append("seasonal_naive")
    elif M == 0:
        notes.append("no season set; seasonal_naive skipped")
    else:
        notes.append("history too short for seasonal_naive")
    lb = None
    if not const:
        models.append("ets")
        try:
            resid = np.asarray(fit("ets", y).resid, dtype=float)
            lb = float(acorr_ljungbox(resid, lags=[min(10, n // 5)]).lb_pvalue.iloc[0])
        except Exception as exc:
            notes.append("ets residual check failed: %s" % type(exc).__name__)
        if lb is not None and lb < 0.05 and n >= 50:
            aics = []
            for o in ARIMA_ORDERS:
                try:
                    aics.append((float(ARIMA(y, order=o).fit().aic), o))
                except Exception:
                    pass
            if aics:
                state["order"] = min(aics)[1]
                models.append("arima")
    else:
        notes.append("constant series; ets and arima skipped")

    origins = [n - k * h for k in range(MAX_ORIGINS, 0, -1) if n - k * h >= max(n // 2, 2 * h)]
    if not origins and n >= 2 * h:
        origins = [n - h]
    if "seasonal_naive" in models:
        need_len = M + 8 if M >= 12 else M
        keep = [o for o in origins if o >= need_len]
        if keep:
            origins = keep
        else:
            models.remove("seasonal_naive")
            notes.append("history too short to backtest seasonal_naive")
    if not origins:
        return result_failure("too_short", "%d periods; needs %d" % (n, need))

    E, C = {}, {}
    for m in list(models):
        errs, cov = [], []
        try:
            for o in origins:
                a = y[o:o + h]
                f, band = fc(m, y[:o], h)
                errs.append(a - f)
                if band is not None:
                    cov += [bool(x) for x in ((a >= band[:, 0]) & (a <= band[:, 1]))]
        except Exception as exc:
            models.remove(m)
            notes.append("%s dropped: %s" % (m, type(exc).__name__))
            continue
        E[m], C[m] = errs, cov
    notes.append("candidates: " + ", ".join(models))

    act = np.concatenate([y[o:o + h] for o in origins])

    def score(m):
        e = np.abs(np.concatenate(E[m]))
        pos = act > 0
        mae = float(e.mean())
        mape = float(100 * np.mean(e[pos] / act[pos])) if pos.any() else None
        den = float(np.abs(act).sum())
        wape = float(100 * e.sum() / den) if den > 0 else None
        return mae, mape, wape

    base = min([m for m in models if m in BASELINES], key=lambda m: score(m)[0])
    best = min(models, key=lambda m: score(m)[0])
    d = (np.array([np.abs(e).mean() for e in E[base]])
         - np.array([np.abs(e).mean() for e in E[best]]))
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else 0.0
    if best in BASELINES or float(d.mean()) < max(0.05 * score(base)[0], se):
        best = base

    try:
        f, band = fc(best, y, h)
    except Exception as exc:
        notes.append("%s final fit failed: %s; using %s" % (best, type(exc).__name__, base))
        best = base
        f, band = fc(best, y, h)
    e = np.concatenate(E[best])
    cov = float(np.mean(C[best])) if C[best] else None
    lo = f + np.quantile(e, 0.1)
    hi = f + np.quantile(e, 0.9)
    if band is not None:
        if cov is not None and cov >= 0.7:
            lo, hi = band[:, 0], band[:, 1]
        else:
            lo, hi = np.minimum(lo, band[:, 0]), np.maximum(hi, band[:, 1])
            notes.append("model band widened from past misses")
    mean = np.asarray(f, dtype=float)
    if kind == "flow" and float(y.min()) >= 0:
        mean = np.maximum(mean, 0.0)
    low = np.minimum(lo, mean)
    high = np.maximum(hi, mean)
    if kind == "flow" and float(y.min()) >= 0:
        low = np.maximum(low, 0.0)
    for arr in (mean, low, high):
        if not np.all(np.isfinite(arr)):
            raise ArithmeticError("non-finite forecast")

    points, p = [], dates[-1]
    for i in range(h):
        p = next_period(p, grain)
        points.append({"period": p.isoformat(), "mean": float(mean[i]),
                       "low": float(low[i]), "high": float(high[i])})
    actuals = [{"period": dt.isoformat(), "value": float(v)}
               for dt, v in list(zip(dates, values))[-tail:]]
    mae, mape, wape = score(best)
    return {"ok": True, "method": best, "baseline": base, "mae": mae, "mape": mape,
            "wape": wape, "baseline_wape": score(base)[2], "coverage": cov,
            "n_periods": n, "h": h, "origins": len(origins), "notes": notes,
            "points": points, "actuals": actuals}


def main():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        with open(SERIES, encoding="utf-8") as f:
            series = parse_series(f.read())
        dates, values, notes = prepare(series, cfg)
        res = forecast(dates, values, cfg)
        if res.get("ok"):
            res["notes"] = notes + res["notes"]
    except InputError as exc:
        res = result_failure("bad_input", str(exc))
    except StaleSeries as exc:
        res = result_failure("stale", str(exc))
    except Exception as exc:
        res = result_failure("error", type(exc).__name__)
    try:
        write_result(res)
    except Exception as exc:
        write_result(result_failure("error", type(exc).__name__))


if __name__ == "__main__":
    main()
