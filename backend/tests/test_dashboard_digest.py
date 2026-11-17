"""Digest parity with the renderer (Phase 22, DCAP-16): shared vectors and digest_lines cases."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import dashboarddigest as dd
from app import dashdates

FIX = Path(__file__).parent / "fixtures"
REAGG = json.loads((FIX / "dashboard_reagg_vectors.json").read_text())
DATES = json.loads((FIX / "dashboard_date_vectors.json").read_text())
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def mk_ctx(v, period="current"):
    return {"today": v.get("today") or "2026-11-04", "as_of": v.get("as_of"),
            "horizon_start": v.get("horizon_start"), "preset": v.get("preset"),
            "period": period, "preset_rows": v.get("preset_rows")}


def same(actual, expected, msg=""):
    if isinstance(expected, dict) and "na" in expected:
        assert actual == expected, msg
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and abs(actual - expected) < 1e-9, f"{msg}: {actual}"
    else:
        assert actual is None if expected is None else actual == expected, msg


def test_vector_counts():
    assert len(REAGG["cases"]) >= 30 and len(REAGG["formula"]) >= 9 and len(REAGG["compare"]) >= 15


@pytest.mark.parametrize("c", REAGG["cases"], ids=lambda c: c["name"])
def test_range_value_vectors(c):
    same(dd.range_value(c["meta"], c["rows"], c["range"], mk_ctx(c)), c["expect"], c["name"])


@pytest.mark.parametrize("c", REAGG["formula"], ids=lambda c: c["name"])
def test_formula_vectors(c):
    same(dd.eval_formula(c["tree"], c["inputs"]), c["expect"], c["name"])


@pytest.mark.parametrize("c", REAGG["compare"], ids=lambda c: c["name"])
def test_compare_vectors(c):
    got = dd.compare_value(c["meta"], c["rows"], c["range"], c["kind"], mk_ctx(c))
    exp = c["expect"]
    if "na" in exp:
        assert got == exp
    else:
        same(got["current"], exp["current"])
        same(got["previous"], exp["previous"])
        same(got["delta"], exp["delta"])


def test_as_of_day():
    assert dd.as_of_day("2026-11-03T19:30:00+00:00", "Asia/Kolkata") == date(2026, 11, 4)
    assert dd.as_of_day("nonsense", "UTC") is None


def test_date_vectors_match_dashdates():
    for v in DATES["cases"]:
        got = dashdates.preset_range(v["preset"], v["period"], date.fromisoformat(v["today"]),
                                     v["fy_start_month"])
        assert [x.isoformat() for x in got] == [v["from"], v["to"]]


# ---- digest_lines --------------------------------------------------------------------------------

def daily(start: date, end: date, col="rev", val=10):
    out, d = [], start
    while d <= end:
        out.append({"metric_time__day": d.isoformat(), col: val})
        d += timedelta(days=1)
    return out


def form(**tile):
    t = {"id": "t1", "type": "stat", "title": "Revenue", "qid": "q1", "follow": "control",
         "horizon_days": 400, "presets_qid": None, "format": "currency", "currency": "INR",
         "unit": None, "comparison": "previous_period", "good_direction": "up", "column": "rev",
         "measures": [{"column": "rev", "kind": "sum"}]}
    t.update(tile)
    return {"render_version": 2, "title": "Sales", "number_locale": "en-IN", "timezone": None,
            "date_control": {"default": "last_7_days", "presets": [], "horizon_days": 400,
                             "fy_start_month": 4},
            "sections": [{"title": "A", "tiles": ["t1"]}], "tiles": [t]}


def snap(rows, as_of="2026-11-04T04:45:00+00:00", tz=None):
    return {"rows": rows, "data_as_of": as_of, "timezone": tz, "error": None}


def test_digest_basic_400_day_snapshot():
    rows = daily(date(2025, 9, 30), date(2026, 11, 4), val=1000000)
    lines = dd.digest_lines(form(), {"q1": snap(rows)}, NOW, default_tz="Asia/Kolkata")
    assert lines[0] == "Sales (Last 7 days, as of 4 Nov 10:15)"
    assert lines[1] == "- Revenue: INR 70L (no change vs previous 7 days)"


def test_digest_delta_and_last_year():
    rows = daily(date(2025, 9, 30), date(2026, 11, 4), val=10)
    for r in rows:
        if r["metric_time__day"] >= "2026-10-29":
            r["rev"] = 20
    lines = dd.digest_lines(form(comparison="both"), {"q1": snap(rows)}, NOW)
    assert "up 100.0% vs previous 7 days" in lines[1]
    assert "up 100.0% vs same days last year" in lines[1]


def test_digest_no_stat_tiles():
    assert dd.digest_lines(form(type="line"), {}, NOW) == []


def test_digest_na_and_missing_snapshot():
    assert dd.digest_lines(form(), {}, NOW)[1] == "- Revenue: No data yet"
    f = form(horizon_days=31)
    f["date_control"]["default"] = "last_90_days"
    rows = daily(date(2026, 10, 1), date(2026, 11, 4))
    assert dd.digest_lines(f, {"q1": snap(rows)}, NOW)[1] == "- Revenue: Not available for this range"


def test_digest_empty_refreshed_snapshot_matches_page():
    refreshed = dict(snap([]), refreshed_at="2026-11-04T04:45:00+00:00")
    lines = dd.digest_lines(form(), {"q1": refreshed}, NOW)
    assert lines[1] == "- Revenue: INR 0"          # controlled: a real zero, as the page draws it
    assert lines[0] == "Sales (Last 7 days, as of 4 Nov 04:45)"
    unrefreshed = snap([])
    assert dd.digest_lines(form(), {"q1": unrefreshed}, NOW)[1] == "- Revenue: No data yet"
    errored = dict(refreshed, error="boom")
    assert dd.digest_lines(form(), {"q1": errored}, NOW)[1] == "- Revenue: No data yet"
    f = form(follow="fixed", pick="last", column="amt")
    f["date_control"] = None
    assert dd.digest_lines(f, {"q1": refreshed}, NOW)[1] == "- Revenue: No rows for this period"
    assert dd.digest_lines(f, {"q1": unrefreshed}, NOW)[1] == "- Revenue: No data yet"


def test_digest_owed_and_fixed_stat():
    f = form(follow="fixed", pick="last", compare="previous_row", compare_label="vs last month",
             format="number", currency=None, owed_when={"op": ">", "value": 5}, column="amt")
    f["date_control"] = None
    lines = dd.digest_lines(f, {"q1": snap([{"amt": 100}, {"amt": 110}])}, NOW)
    assert lines[1] == "- Revenue: 110 (owed) (up 10.0% vs last month)"


def test_digest_v1_rows_layout():
    f = {"render_version": 1, "title": "Old", "rows": [["t1"]],
         "tiles": [{"id": "t1", "type": "stat", "title": "Count", "qid": "q1", "column": "n",
                    "format": "integer", "compare": "none", "pick": "last"}]}
    lines = dd.digest_lines(f, {"q1": {"rows": [{"n": 1234567}], "data_as_of": None}}, NOW)
    assert lines == ["Old", "- Count: 1,234,567"]  # the v1 page does not compact
    f["number_locale"] = "en-IN"
    lines = dd.digest_lines(f, {"q1": {"rows": [{"n": 1234567}], "data_as_of": None}}, NOW)
    assert lines == ["Old", "- Count: 12,34,567"]


def test_controlled_stat_uses_the_measure_of_its_own_column():
    f = form(column="kg_out", measures=[{"column": "kg_in", "kind": "sum"}, {"column": "kg_out", "kind": "closing"}])
    meta = next(m for m in f["tiles"][0]["measures"] if m["column"] == f["tiles"][0]["column"])
    assert meta["kind"] == "closing"
    rows = [{"metric_time__day": d, "kg_in": 1, "kg_out": 5}
            for d in ("2026-11-02", "2026-11-03", "2026-11-04")]
    line = dd.digest_lines(f, {"q1": snap(rows)}, NOW)[1]
    assert line.startswith("- Revenue: INR 5"), line


def test_format_value_compact_locales():
    assert dd.format_value(1230000, "number", locale="en-IN") == "12.3L"
    assert dd.format_value(12000000, "number", locale="en-IN") == "1.2Cr"
    assert dd.format_value(1230000, "number", locale="en-US") == "1.2M"
    assert dd.format_value(3400000000, "number", locale="en-US") == "3.4B"
    assert dd.format_value(12300, "currency", locale="en-US", currency="USD") == "USD 12.3K"
    assert dd.format_value(0.123, "percent", locale="en-US") == "12.3%"
    assert dd.format_value(5, "number", unit="kg") == "5 kg"
    assert dd.format_value(None, "number") == "-"


def test_huge_values_do_not_raise():
    assert dd.format_value(1e30, "number", locale="en-US", compact=False).startswith("1,000,000")
    assert dd.format_value(1e30, "number", locale="en-US") == "1000000000000000000T"
    assert dd._delta_text(1e30, 1) != ""


def test_format_value_ties_and_big_steps_match_intl():
    # expected strings come from node Intl.NumberFormat / toFixed
    us = lambda v: dd.format_value(v, "number", locale="en-US")  # noqa: E731
    ind = lambda v: dd.format_value(v, "number", locale="en-IN")  # noqa: E731
    assert [us(1250), us(1250000), us(1225000), us(999950), us(-1250)] == ["1.3K", "1.3M", "1.2M", "1M", "-1.3K"]
    assert [us(1e12), us(1.5e12), us(123e12), us(1e11)] == ["1T", "1.5T", "123T", "100B"]
    assert [ind(1225000), ind(1250), ind(999950), ind(1e12), ind(1e11), ind(1e9)] == [
        "12.3L", "1.3K", "10L", "1LCr", "10KCr", "100Cr"]
    assert us(1.15e6) == "1.2M"
    assert [us(999.95), us(999.96), us(999.94), us(-999.96)] == ["1K", "1K", "999.9", "-1K"]
    assert [ind(999.95), ind(999.96)] == ["1K", "1K"]
    assert dd.format_value(2.25, "decimal1", locale="en-US", compact=False) == "2.3"
    assert dd.format_value(1.005, "decimal2", locale="en-US", compact=False) == "1.01"
    assert dd._delta_text(112.25, 100) == "up 12.3%"   # (12.25).toFixed(1) is 12.3


def test_digest_as_of_lag_cuts_previous_like_renderer():
    # data lags a day: current covers 29 Oct..3 Nov (6 days), previous must cover 22..27 Oct (6 days)
    rows = daily(date(2025, 9, 30), date(2026, 11, 3), val=10)
    lines = dd.digest_lines(form(format="number"),
                            {"q1": snap(rows, as_of="2026-11-03T23:00:00+00:00")}, NOW, default_tz="UTC")
    assert lines[1] == "- Revenue: 60 (no change vs previous 7 days)"


def _lag_cases():
    return [c for c in REAGG["compare"] if c["name"].startswith("as_of_lag")]


def test_lag_vectors_present():
    assert len(_lag_cases()) == 3


@pytest.mark.parametrize("c", _lag_cases(), ids=lambda c: c["name"])
def test_digest_uses_as_of_vectors(c):
    today = date(2026, 11, 4)
    preset = c["preset"] or "last_7_days"
    cur = dashdates.preset_range(preset, "current", today, 4)
    assert [x.isoformat() for x in cur] == [c["range"]["from"], c["range"]["to"]], c["name"]
    hs = date.fromisoformat(c["horizon_start"])
    f = form(format="number", comparison=c["kind"], horizon_days=(today - hs).days + 1)
    f["date_control"]["default"] = preset
    s = snap(c["rows"], as_of=c["as_of"] + "T12:00:00+00:00")
    lines = dd.digest_lines(f, {"q1": s}, NOW, default_tz="UTC")
    exp = c["expect"]
    label = dd._delta_label(c["kind"], c["range"], c["preset"])
    want = dd._delta_text(exp["current"], exp["previous"])
    assert f"({want} {label})" in lines[1], lines[1]
