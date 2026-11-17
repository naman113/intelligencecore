"""Phase 13.1 (FCST-02): static and unit tests of the vendored forecast_runner.py.

The runner is run as text inside the sandbox; the gateway never imports it. These tests load it
by path, check its import/IO surface by AST, and unit-test the stdlib preparation layer (no
statsmodels needed). The modelling behaviour is proved by test_forecast_runner_sandbox.py.
"""
import ast
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "app" / "assets" / "forecast_runner.py"
SOURCE = RUNNER_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

STDLIB_OK = {"json", "csv", "datetime", "math", "os", "sys", "warnings"}
ANY_OK = STDLIB_OK | {"numpy", "pandas", "statsmodels"}
FORBIDDEN = {"socket", "urllib", "http", "requests", "subprocess", "random", "pickle",
             "shutil", "eval", "exec", "__import__", "compile"}


@pytest.fixture(scope="module")
def fr():
    mods_before = set(sys.modules)
    spec = importlib.util.spec_from_file_location("forecast_runner_under_test", RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # loading must not pull in the heavy libraries
    for heavy in ("pandas", "statsmodels"):
        assert heavy not in (set(sys.modules) - mods_before)
    return mod


def _top(name):
    return name.split(".")[0]


def _imports(nodes):
    out = []
    for n in nodes:
        if isinstance(n, ast.Import):
            out += [_top(a.name) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(_top(n.module or ""))
    return out


def test_file_layout():
    assert RUNNER_PATH.is_file()
    assert not (RUNNER_PATH.parent / "__init__.py").exists()
    assert "random" not in SOURCE
    assert SOURCE.rstrip().splitlines()[-1] == "    main()"
    assert 'if __name__ == "__main__":' in SOURCE.splitlines()[-2]


def test_module_level_imports_are_stdlib():
    top = _imports(TREE.body)
    assert top and set(top) <= STDLIB_OK


def test_all_imports_allowlisted():
    assert set(_imports(ast.walk(TREE))) <= ANY_OK


def test_heavy_imports_only_inside_forecast():
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "forecast")
    inside = {id(n) for n in ast.walk(fn)}
    for n in ast.walk(TREE):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = _imports([n])
            if set(names) & {"numpy", "statsmodels"}:
                assert id(n) in inside


def test_no_forbidden_names():
    for n in ast.walk(TREE):
        if isinstance(n, ast.Name):
            assert n.id not in FORBIDDEN, n.id
        if isinstance(n, ast.Attribute):
            assert n.attr not in FORBIDDEN, n.attr


def test_path_literals_are_work_only():
    docstrings = set()
    for n in ast.walk(TREE):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(n, clean=False):
            docstrings.add(id(n.body[0].value))
    for n in ast.walk(TREE):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            s = n.value
            if "/" in s and re.fullmatch(r"[\w./-]+", s):
                assert s.startswith("/work/"), s


def test_load_does_not_run_main(fr):
    assert fr.RESULT == "/work/result.json"
    assert set(fr.BASELINES) <= set(fr.METHODS)


def test_parse_series(fr):
    got = fr.parse_series("period,value\n2026-01-05,10\n2026-01-12,\n")
    assert got == [(date(2026, 1, 5), 10.0), (date(2026, 1, 12), None)]
    for bad in ("period,value\nnot-a-date,1\n", "period,value\n2026-01-05,abc\n",
                "a,b\n2026-01-05,1\n", "", "period,value\n2026-01-05,nan\n"):
        with pytest.raises(fr.InputError):
            fr.parse_series(bad)


def test_period_math(fr):
    assert fr.next_period(date(2026, 12, 1), "month") == date(2027, 1, 1)
    assert fr.next_period(date(2026, 1, 1), "month") == date(2026, 2, 1)
    assert fr.next_period(date(2026, 10, 19), "week") == date(2026, 10, 26)
    assert fr.next_period(date(2026, 2, 28), "day") == date(2026, 3, 1)
    assert fr.period_start(date(2026, 10, 25), "week") == date(2026, 10, 19)  # Sunday -> Monday
    assert fr.period_start(date(2026, 10, 25), "month") == date(2026, 10, 1)
    assert fr.period_start(date(2026, 10, 25), "day") == date(2026, 10, 25)


def _cfg(**kw):
    base = {"grain": "day", "horizon": 7, "season": 7, "kind": "flow",
            "as_of": "2026-01-20", "max_history": 3000, "actual_tail": 10}
    base.update(kw)
    return base


def _days(start, vals):
    out = []
    d = start
    for v in vals:
        out.append((d, v))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_current_period_dropped_each_grain(fr):
    ser = _days(date(2026, 1, 1), [1] * 20)  # 1..20 Jan
    dates, vals, _ = fr.prepare(ser, _cfg(as_of="2026-01-20"))
    assert dates[-1] == date(2026, 1, 19)
    wk = [(date(2026, 1, 5), 1.0), (date(2026, 1, 12), 2.0), (date(2026, 1, 19), 3.0)]
    dates, vals, _ = fr.prepare(wk, _cfg(grain="week", as_of="2026-01-22"))
    assert dates == [date(2026, 1, 5), date(2026, 1, 12)]
    mo = [(date(2025, 11, 1), 1.0), (date(2025, 12, 1), 2.0), (date(2026, 1, 1), 3.0)]
    dates, vals, _ = fr.prepare(mo, _cfg(grain="month", as_of="2026-01-31"))
    assert dates == [date(2025, 11, 1), date(2025, 12, 1)]


def test_flow_gaps_zero_filled_up_to_the_last_complete_period(fr):
    # Quick 013 (WR-02): trailing periods with no rows are zeros, so the forecast
    # starts at the current period instead of in the past
    ser = [(date(2026, 1, 1), 5.0), (date(2026, 1, 2), None), (date(2026, 1, 5), 7.0)]
    dates, vals, notes = fr.prepare(ser, _cfg(as_of="2026-01-10"))
    assert dates[0] == date(2026, 1, 1) and dates[-1] == date(2026, 1, 9)
    assert vals == [5.0, 0.0, 0.0, 0.0, 7.0, 0.0, 0.0, 0.0, 0.0]
    assert "4 days after 2026-01-05 had no rows; counted as 0" in notes


def test_weekly_flow_trailing_zero_and_stale(fr):
    ser = [(date(2025, 12, 22), 3.0), (date(2025, 12, 29), 4.0), (date(2026, 1, 5), 5.0)]
    dates, vals, notes = fr.prepare(ser, _cfg(grain="week", as_of="2026-01-28"))
    assert dates[-1] == date(2026, 1, 19) and vals[-3:] == [5.0, 0.0, 0.0]
    with pytest.raises(fr.StaleSeries, match="no rows after 2026-01-05"):
        fr.prepare(ser, _cfg(grain="week", as_of="2026-02-04"))


def test_stale_series_fails_and_short_level_gap_notes(fr):
    ser = _days(date(2026, 1, 1), [1] * 5)  # last = 5 Jan
    with pytest.raises(fr.StaleSeries):
        fr.prepare(ser, _cfg(as_of="2026-01-20"))          # 14 empty days: a stopped feed
    dates, _, notes = fr.prepare(ser, _cfg(as_of="2026-01-07"))
    assert dates[-1] == date(2026, 1, 6) and not any(n.startswith("stale") for n in notes)
    # a level series is never zero-filled: its grid keeps the last value, with the note
    dates, vals, notes = fr.prepare(ser, _cfg(kind="level", as_of="2026-01-09"))
    assert dates[-1] == date(2026, 1, 5) and vals[-1] == 1.0
    assert any(n.startswith("stale") for n in notes)
    with pytest.raises(fr.StaleSeries):
        fr.prepare(ser, _cfg(kind="level", as_of="2026-01-20"))


def test_main_reports_stale_as_a_readable_failure(fr, monkeypatch, tmp_path):
    (tmp_path / "c.json").write_text(json.dumps(_cfg(as_of="2026-03-01")))
    (tmp_path / "s.csv").write_text("period,value\n2026-01-01,1\n2026-01-02,2\n")
    seen = {}
    monkeypatch.setattr(fr, "CONFIG", str(tmp_path / "c.json"))
    monkeypatch.setattr(fr, "SERIES", str(tmp_path / "s.csv"))
    monkeypatch.setattr(fr, "write_result", lambda obj, path=None: seen.setdefault("r", obj))
    fr.main()
    assert seen["r"]["ok"] is False and seen["r"]["reason"] == "stale"
    assert "no rows after 2026-01-02" in seen["r"]["message"]


def test_level_short_gap_interpolated(fr):
    ser = [(date(2026, 1, 1), 10.0), (date(2026, 1, 2), None), (date(2026, 1, 3), None),
           (date(2026, 1, 4), 40.0)]
    _, vals, notes = fr.prepare(ser, _cfg(kind="level", as_of="2026-01-05"))
    assert vals == [10.0, 20.0, 30.0, 40.0]
    assert notes == []


def test_level_long_gap_keeps_latest_segment(fr):
    vals_in = [1.0, 2.0, None, None, None, 6.0, 7.0, None, 9.0]
    ser = _days(date(2026, 1, 1), vals_in)
    dates, vals, notes = fr.prepare(ser, _cfg(kind="level", as_of="2026-01-12"))
    assert vals == [6.0, 7.0, 8.0, 9.0]
    assert dates[0] == date(2026, 1, 6)
    assert any("gap" in n for n in notes)


def test_trim_to_max_history(fr):
    ser = _days(date(2026, 1, 1), list(range(1, 16)))
    dates, vals, _ = fr.prepare(ser, _cfg(max_history=5, as_of="2026-01-16"))
    assert len(dates) == 5 and vals == [11.0, 12.0, 13.0, 14.0, 15.0]


def test_duplicates_and_bad_config(fr):
    with pytest.raises(fr.InputError):
        fr.prepare([(date(2026, 1, 1), 1.0), (date(2026, 1, 1), 2.0)], _cfg())
    with pytest.raises(fr.InputError):
        fr.prepare([(date(2026, 1, 5), 1.0), (date(2026, 1, 6), 2.0)], _cfg(grain="week"))
    with pytest.raises(fr.InputError):
        fr.prepare([], _cfg(grain="hour"))
    with pytest.raises(fr.InputError):
        fr.prepare([], _cfg(as_of="soon"))


def test_empty_and_grid_guard(fr):
    assert fr.prepare([], _cfg()) == ([], [], [])
    with pytest.raises(fr.InputError):
        fr.prepare([(date(1500, 1, 1), 1.0), (date(2025, 1, 1), 1.0)], _cfg(as_of="2025-01-02"))


def test_result_failure_and_write(fr, tmp_path):
    assert fr.result_failure("too_short", "8 periods; needs 24") == {
        "ok": False, "reason": "too_short", "message": "8 periods; needs 24"}
    p = tmp_path / "result.json"
    fr.write_result({"ok": True, "x": [1.5, None]}, str(p))
    assert json.loads(p.read_text()) == {"ok": True, "x": [1.5, None]}
    assert not (tmp_path / "result.json.tmp").exists()
    with pytest.raises(ValueError):
        fr.write_result({"x": float("nan")}, str(p))


def test_forecast_too_short_needs_no_statsmodels(fr):
    res = fr.forecast([date(2026, 1, 5)] * 8, [1.0] * 8, _cfg(grain="week", horizon=4))
    assert res == {"ok": False, "reason": "too_short", "message": "8 periods; needs 12"}
