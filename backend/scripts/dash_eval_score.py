"""Pure rubric scoring and the eval gate verdict for the dashboard eval (22-13, DCAP-17).

No network, no database. The runner (22-19) writes one record per request; this module
scores records against scripts/dash_eval/requests.json.

Record shape (what the runner must write):

    {"n": 1, "round": 0, "finished_at": "2026-11-05T10:00:00Z",
     "spec": <saved draft, authoring form (spec_version 2)> | None,
     "render": <render form of the draft> | None,
     "tools": [{"name": "use_skill", "args": {"name": ..., "part": "recipes/x.md"}, "result": ...},
               {"name": "preview_dashboard", "args": {...},
                "result": {"ok": bool, "errors": [...], "warnings": [{"rule": ...}]}}],
     "reply": "<final reply text>",
     "tokens": <input + output + cache read>,
     "comparisons": [{"match": bool, ...}],   # numbers gate, one per compared value
     "skips": [...], "errors": ["no draft", ...]}

Rubric (research 8.3): eight criteria worth 0, 1 or 2 points. Criteria 1 to 7 are decided by
the request's machine checks (2 when all pass, 1 when at least half pass, 0 otherwise; a
criterion without checks scores 2); criterion 8 is the token budget. The request's
reply_checks count towards criterion 1. The numbers gate is separate from the points.
A request may carry a `required` list of checks (request 12, the refusal): a failure fails the
request whatever its points, because empty criteria score a free 2.
SC5 verdict: gate passed and at least 13 of 16 points on at least 11 of 12 requests.
"""

import json
import re

PASS_POINTS = 13
REQUIRED = 11
_PERIOD = re.compile(r"\b(today|yesterday|daily|day|week|month|quarter|year|fy|last|this|since|now)"
                     r"|\b\d{4}-\d{2}|\b\d+\s*(d|days?|w|wks?|weeks?|months?|mos?|quarters?|years?|y)\b"
                     r"|\bq[1-4]\b|\bfy\s*\d", re.I)


# ------------------------------------------------------------------ record access

def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _tiles(record) -> list:
    spec = record.get("spec")
    return [t for t in (spec or {}).get("tiles", []) if isinstance(t, dict)]


def _bound(t) -> bool:
    return t.get("type") != "text"


def _measure_names(t) -> list:
    if isinstance(t.get("measure"), dict):
        return [t["measure"].get("name")]
    if isinstance(t.get("measures"), dict):
        return list(t["measures"].get("names") or [])
    return []


def _types(c) -> list:
    v = c.get("types", c.get("type"))
    return [v] if isinstance(v, str) else list(v or [])


def _calls(record, name) -> list:
    out = []
    for t in record.get("tools") or []:
        if t.get("name") == name:
            out.append({"args": _json(t.get("args")) or {}, "result": _json(t.get("result"))})
    return out


def _series_tones(t) -> set:
    tones = set()
    for y in t.get("y") or []:
        if isinstance(y, dict) and y.get("tone"):
            tones.add(y["tone"])
    if t.get("type") == "paired_bar":
        tones |= {"inward", "outward"} if not tones else set()
    return tones


# ------------------------------------------------------------------ checks

def _c_bound_measures_any(rec, c):
    names = {n for t in _tiles(rec) for n in _measure_names(t)}
    return bool(names & set(c["any"]))


def _c_governed_share_min(rec, c):
    data = [t for t in _tiles(rec) if _bound(t)]
    if not data:
        return False
    governed = sum(1 for t in data if _measure_names(t))
    return governed / len(data) >= c["min"] - 1e-9


def _c_raw_sql_needs_word(rec, c):
    raw = [t for t in _tiles(rec) if _bound(t) and t.get("query")]
    if not raw:
        return True
    text = " ".join([rec.get("reply") or ""] + [(t.get("note") or "") + " " + (t.get("title") or "")
                                                for t in raw]).lower()
    return c["word"].lower() in text


def _c_tile_type_any(rec, c):
    have = sum(1 for t in _tiles(rec) if t.get("type") in _types(c))
    return have >= c.get("min", 1)


def _c_tile_type_none(rec, c):
    return not any(t.get("type") in _types(c) for t in _tiles(rec))


def _c_control_default(rec, c):
    dc = (rec.get("spec") or {}).get("date_control")
    if not isinstance(dc, dict):
        return False
    allowed = c.get("presets") or [c.get("preset")]
    return dc.get("default", "last_7_days") in allowed


def _c_follows_control(rec, c):
    return sum(1 for t in _tiles(rec) if t.get("date") == "control") >= c.get("min_tiles", 1)


def _c_presets_for_distinct(rec, c):
    found = False
    for t in (rec.get("render") or {}).get("tiles", []):
        if any(m.get("kind") == "distinct" for m in t.get("measures") or []):
            found = True
            if t.get("follow") == "control" and not t.get("presets_qid"):
                return False
    return found


def _c_tones_on(rec, c):
    need = set(c["tones"])
    return any(need <= _series_tones(t) for t in _tiles(rec) if t.get("type") in _types(c))


def _c_ghost_on(rec, c):
    return any(t.get("ghost") == c["ghost"] for t in _tiles(rec) if t.get("type") in _types(c))


def _c_good_direction_set(rec, c):
    allowed = c.get("allowed")
    for t in _tiles(rec):
        if t.get("type") == "stat":
            gd = t.get("good_direction")
            if gd is None or (allowed is not None and gd not in allowed):
                return False
        for col in t.get("columns") or []:
            if isinstance(col, dict) and col.get("delta"):
                gd = col.get("good_direction")
                if gd is None or (allowed is not None and gd not in allowed):
                    return False
    return True


def _c_owed_only_on(rec, c):
    entries = []
    for t in _tiles(rec):
        if t.get("type") == "stat" and t.get("owed_when"):
            entries.append(set(_measure_names(t)) | {t.get("column")})
        for col in t.get("columns") or []:
            if isinstance(col, dict) and col.get("owed_when"):
                entries.append({col.get("column")})
    if c.get("required") and not entries:
        return False
    allowed = c.get("allowed")
    return allowed is None or all(e & set(allowed) for e in entries)


def _c_sections_min(rec, c):
    return len((rec.get("spec") or {}).get("sections") or []) >= c["min"]


def _c_first_section_stats_max(rec, c):
    secs = (rec.get("spec") or {}).get("sections") or []
    if not secs:
        return False
    ids = set(secs[0].get("tiles") or [])
    stats = sum(1 for t in _tiles(rec) if t.get("id") in ids and t.get("type") == "stat")
    return 1 <= stats <= c["max"]


def _c_titles_have_period(rec, c):
    tiles = [t for t in _tiles(rec) if _bound(t) and t.get("date") != "control"]
    return bool(_tiles(rec)) and all(_PERIOD.search(t.get("title") or "") for t in tiles)


def _c_recipe_loaded(rec, c):
    want = f"recipes/{c['name']}.md"
    return any(str(x["args"].get("part") or "") == want for x in _calls(rec, "use_skill"))


def _c_preview_rounds_max(rec, c):
    return len(_calls(rec, "preview_dashboard")) <= c["max"]


def _last_preview(rec):
    calls = _calls(rec, "preview_dashboard")
    return calls[-1]["result"] if calls and isinstance(calls[-1]["result"], dict) else None


def _unparsed_preview(rec) -> bool:
    """True when a preview was called but its logged result is not a JSON object (for example
    cut to 1000 characters): the runner must log full results, so this is an error."""
    calls = _calls(rec, "preview_dashboard")
    return bool(calls) and not isinstance(calls[-1]["result"], dict)


def _c_no_errors(rec, c):
    last = _last_preview(rec)
    return bool(last) and not last.get("errors") and last.get("ok", True) is not False


def _c_warnings_explained(rec, c):
    last = _last_preview(rec)
    if last is None:
        return False
    reply = (rec.get("reply") or "").lower()
    for w in last.get("warnings") or []:
        rule = str(w.get("rule", "")).lower() if isinstance(w, dict) else ""
        if rule:
            if rule not in reply:
                return False
        elif "warning" not in reply:
            return False
    return True


def _has_word(reply, word) -> bool:
    """Whole-word match ('red' must not match 'rendered'); a plural s/es is allowed."""
    return re.search(r"\b" + re.escape(word.lower()) + r"(?:s|es)?\b", reply) is not None


def _c_reply_contains_all(rec, c):
    reply = (rec.get("reply") or "").lower()
    return all(_has_word(reply, w) for w in c["words"])


def _c_reply_contains_any(rec, c):
    reply = (rec.get("reply") or "").lower()
    return any(_has_word(reply, w) for w in c["words"])


CHECKS = {k[3:]: v for k, v in globals().items() if k.startswith("_c_")}


def run_check(record, check) -> bool:
    fn = CHECKS.get(check.get("check"))
    if fn is None:
        raise ValueError(f"unknown check {check.get('check')!r}")
    return bool(fn(record, check))


# ------------------------------------------------------------------ scoring

def _points(passed: int, total: int) -> int:
    if total == 0 or passed == total:
        return 2
    return 1 if passed * 2 >= total else 0


def _gate(record, request) -> bool:
    comps = record.get("comparisons") or []
    mismatch = any(not (c.get("match", c.get("ok", False))) for c in comps)
    if request.get("gate", True):
        return bool(comps) and not mismatch and not record.get("errors")
    return not mismatch


def score_request(record, request, budget) -> dict:
    points, notes = {}, []
    for crit in range(1, 8):
        checks = list(request["checks"].get(str(crit), []))
        if crit == 1:
            checks += list(request.get("reply_checks") or [])
        ok = 0
        for c in checks:
            if run_check(record, c):
                ok += 1
            else:
                notes.append(f"criterion {crit}: {c['check']} failed")
        points[crit] = _points(ok, len(checks))
    tokens = record.get("tokens") or 0
    unparsed = _unparsed_preview(record)
    if unparsed:
        notes.append("preview result could not be parsed (the runner must log the full result)")
    if not tokens:
        points[8] = 0
        notes.append("criterion 8: no token count")
    elif budget is None:
        points[8] = 2
        notes.append("criterion 8: baseline")
    elif tokens <= budget:
        points[8] = 2
    elif tokens <= 1.5 * budget:
        points[8] = 1
    else:
        points[8] = 0
    gate = _gate(record, request)
    if not gate:
        notes.append("numbers gate not passed")
    required = not unparsed
    for c in request.get("required") or []:
        if not run_check(record, c):
            required = False
            notes.append(f"required: {c['check']} failed")
    return {"points": points, "total": sum(points.values()), "gate": gate, "required": required,
            "notes": notes}


def _later(a, b) -> bool:
    ka = (a.get("round") or 0, a.get("finished_at") or "")
    kb = (b.get("round") or 0, b.get("finished_at") or "")
    return ka > kb


def score_run(records, requests, budget) -> dict:
    latest = {}
    for r in records:
        n = r.get("n")
        if n not in latest or _later(r, latest[n]):
            latest[n] = r
    results, passed, failing = {}, 0, []
    for req in requests:
        n = req["n"]
        rec = latest.get(n)
        if rec is None:
            res = {"points": {i: 0 for i in range(1, 9)}, "total": 0, "gate": False,
                   "notes": ["no record"], "round": None}
        else:
            res = score_request(rec, req, budget)
            res["round"] = rec.get("round") or 0
        res["pass"] = bool(res["gate"] and res.get("required", True) and res["total"] >= PASS_POINTS)
        results[n] = res
        if res["pass"]:
            passed += 1
        else:
            failing.append(n)
    return {"results": results, "passed": passed, "required": REQUIRED,
            "pass": passed >= REQUIRED, "failing": failing, "total_requests": len(requests)}


def gate_line(result) -> str:
    total = result.get("total_requests", len(result["results"]))
    if result["pass"]:
        return f"eval gate: PASS ({result['passed']}/{total} requests with gate and 13+)"
    failing = ", ".join(str(n) for n in result["failing"])
    return (f"eval gate: FAIL ({result['passed']}/{total} requests with gate and 13+; "
            f"failing: {failing})")
