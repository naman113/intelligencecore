"""22-19 Task 1: the numbers-gate planner maps shown values to reference queries (pure, no DB)."""

import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dash_eval_refs as dr  # noqa: E402

TODAY = date(2026, 10, 8)  # a Thursday; 2026-10-05 is a Monday
SQL = "SELECT COALESCE(SUM(x), 0) FROM t WHERE d BETWEEN {from} AND {to} {extra}"
NOW_SQL = "SELECT COALESCE(SUM(x), 0) FROM inv WHERE 1 = 1 {extra}"
DIMS = {"customers.name": "c.name", "suppliers.name": "s.name", "qualities.name": "q.name",
        "vehicles.reg_no": "v.reg_no", "invoices.age_bucket": "i.age_bucket",
        "invoices.is_overdue": "i.is_overdue", "invoices.amount": "i.amount"}
REFS = {"mill": {
    "rolls_in": {"kind": "sum", "sql": SQL, "dims": DIMS},
    "kg_in": {"kind": "sum", "sql": SQL, "dims": DIMS},
    "receivables_open": {"kind": "now", "sql": NOW_SQL, "dims": DIMS},
    "receivables_overdue": {"kind": "now", "sql": NOW_SQL, "dims": DIMS},
}}


def rt(tid="t1", col="rolls_in", kind="sum", **kw):
    t = {"id": tid, "type": "table", "qid": "q_" + tid, "grain": None, "follow": "control",
         "x": None, "series": None, "measures": [{"column": col, "kind": kind}],
         "by_columns": [], "horizon_days": 400}
    t.update(kw)
    return t


def at(tid="t1", name="rolls_in", **binding):
    return {"id": tid, "type": "table", "measure": {"name": name, **binding}}


def form(*tiles):
    return {"tiles": list(tiles), "date_control": {"default": "last_7_days", "fy_start_month": 4}}


def spec(*tiles):
    return {"tiles": list(tiles), "date_control": {"default": "last_7_days", "fy_start_month": 4}}


def plan(render_tiles, auth_tiles, values, refreshed=None):
    return dr.plan_checks(values, form(*render_tiles), spec(*auth_tiles), REFS, TODAY,
                          "mill", refreshed)


def v(row="", value=1.0, tile="t1", col="rolls_in", series=None):
    return {"tile": tile, "col": col, "row": row, "series": series, "value": value}


def span(e):
    return (e["span"]["from"], e["span"]["to"])


def test_controlled_day_row_cell():
    [e] = plan([rt()], [at()], [v("2026-10-05")])
    assert span(e) == ("2026-10-05", "2026-10-05")
    assert "DATE '2026-10-05'" in e["sql"] and "{" not in e["sql"]
    assert e["shown"] == 1.0 and e["member"] is None


def test_weekly_bucket_span():
    [e] = plan([rt()], [at()], [v("2026-09-28..2026-10-04")])
    assert span(e) == ("2026-09-28", "2026-10-04")


def test_controlled_stat_uses_range_and_default():
    [a, b] = plan([rt(type="stat")], [at()], [v("2026-10-03..2026-10-05"), v("")])
    assert span(a) == ("2026-10-03", "2026-10-05")
    assert span(b) == ("2026-10-02", "2026-10-08")  # last_7_days default


def test_member_row_under_control():
    [e] = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])], [v("Acme")])
    assert span(e) == ("2026-10-02", "2026-10-08")
    assert "AND c.name = 'Acme'" in e["sql"]
    assert e["member"] == "Acme"


def test_other_row_skipped():
    [e] = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])], [v("Other")])
    assert e["skip"] == "Other"


def test_aging_tile_row_by_age_bucket():
    r = rt(col="receivables_overdue", kind="now", type="aging", by_columns=["invoices__age_bucket"])
    [e] = plan([r], [at(name="receivables_overdue", by=["invoices.age_bucket"])],
               [v("31-60", col="receivables_overdue")])
    assert e["span"] is None and "AND i.age_bucket = '31-60'" in e["sql"]
    assert "{" not in e["sql"]


def test_fixed_last_days_with_month_grain_clips_first_bucket():
    r = rt(follow="fixed", grain="month")
    a = at(grain="month", window={"last_days": 30})
    [e, f] = plan([r], [a], [v("2026-09-01"), v("2026-10-01")])
    assert span(e) == ("2026-09-09", "2026-09-30")  # window 09-09..10-08
    assert span(f) == ("2026-10-01", "2026-10-08")


def test_now_tile_has_no_span():
    r = rt(col="receivables_open", kind="now", follow="now", type="stat")
    [e] = plan([r], [at(name="receivables_open")], [v("", col="receivables_open")])
    assert e["span"] is None and "error" not in e and "skip" not in e


def test_fixed_ranked_bar_receivables_open_by_customer():
    r = rt(col="receivables_open", kind="now", follow="fixed", type="ranked_bar",
           by_columns=["customers__name"])
    a = at(name="receivables_open", by=["customers.name"])
    [e] = plan([r], [a], [v("Acme", col="receivables_open")])
    assert e["span"] is None and "c.name = 'Acme'" in e["sql"] and "BETWEEN" not in e["sql"]


def test_fixed_aging_no_window():
    r = rt(col="receivables_overdue", kind="now", follow="fixed", type="aging",
           by_columns=["invoices__age_bucket"])
    a = at(name="receivables_overdue", by=["invoices.age_bucket"])
    [e] = plan([r], [a], [v("61-90", col="receivables_overdue")])
    assert e["span"] is None and "i.age_bucket = '61-90'" in e["sql"]


def test_fixed_week_grain_default_window():
    # 182 // 7 = 26 whole weeks: the first kept bucket is date_trunc('week', today) - 25 weeks
    r = rt(follow="fixed", grain="week")
    [e, f, g] = plan([r], [at(grain="week")], [v("2026-04-13"), v("2026-10-05"), v("2026-04-06")])
    assert span(e) == ("2026-04-13", "2026-04-19")  # a full week
    assert span(f) == ("2026-10-05", "2026-10-08")
    assert g["skip"] == "outside window"  # the compiled SQL never returns this bucket


def test_null_member_is_matched_with_is_null():
    [e] = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])], [v("Not set")])
    assert "AND (c.name IS NULL OR c.name = 'Not set')" in e["sql"]


def test_two_not_set_members_are_skipped_with_a_reason():
    es = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])],
              [v("Not set", 1.0), v("Not set", 2.0)])
    assert len(es) == 2
    for e in es:
        assert "sql" not in e and "Not set" in e["skip"] and "several" in e["skip"]


def test_single_not_set_alongside_named_members_still_checked():
    es = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])],
              [v("Not set"), v("Acme")])
    assert all("sql" in e for e in es)


def test_fixed_no_window_no_grain_skipped():
    [e] = plan([rt(follow="fixed")], [at()], [v("")])
    assert e["skip"] == "no window"


def test_binding_filters_translated_or_skipped():
    filt = [{"dimension": "invoices.is_overdue", "op": "=", "values": [True]},
            {"dimension": "customers.name", "op": "in", "values": ["O'Neil", "B"]},
            {"dimension": "invoices.amount", "op": ">=", "values": [100]},
            {"dimension": "suppliers.name", "op": "is_null", "values": []}]
    [e] = plan([rt()], [at(filters=filt)], [v("2026-10-05")])
    assert "AND i.is_overdue = TRUE" in e["sql"]
    assert "AND c.name IN ('O''Neil', 'B')" in e["sql"]
    assert "AND i.amount >= 100" in e["sql"]
    assert "AND s.name IS NULL" in e["sql"]
    bad = [{"dimension": "trips.km", "op": "=", "values": [1]}]
    [g] = plan([rt()], [at(filters=bad)], [v("2026-10-05")])
    assert g["skip"] == "filter not translatable"


def test_member_name_with_quote_escaped():
    [e] = plan([rt(by_columns=["customers__name"])], [at(by=["customers.name"])], [v("D'Souza")])
    assert "c.name = 'D''Souza'" in e["sql"]


def test_query_bound_column_skipped_and_unknown_tile_error():
    [e] = plan([rt()], [at()], [v("2026-10-05", col="label")])
    assert e["skip"] == "not a measure column"
    [g] = plan([rt()], [at()], [v("2026-10-05", tile="zz")])
    assert g["error"] == "unknown tile"
    [h] = plan([rt(col="mystery")], [at(name="mystery")], [v("2026-10-05", col="mystery")])
    assert h["error"] == "no reference"


def test_pivoted_controlled_line_cell_and_missing_series():
    r = rt(type="line", x="metric_time__day", series="qualities__name", by_columns=["qualities__name"])
    a = at(by=["qualities.name"])
    [e, f] = plan([r], [a], [v("2026-10-05", series="A grade"), v("2026-10-05")])
    assert span(e) == ("2026-10-05", "2026-10-05")
    assert "AND q.name = 'A grade'" in e["sql"]
    assert f["skip"] == "pivoted series without member"


def test_fixed_daily_table_with_series_clips_to_window():
    r = rt(follow="fixed", grain="day", by_columns=["suppliers__name"])
    a = at(grain="day", by=["suppliers.name"], window={"last_days": 7})
    [e, f] = plan([r], [a], [v("2026-10-05", series="Acme"), v("2026-09-30", series="Acme")])
    assert span(e) == ("2026-10-05", "2026-10-05") and "s.name = 'Acme'" in e["sql"]
    assert f["skip"] == "outside window"


def test_fixed_two_by_entries_skipped():
    r = rt(follow="fixed", by_columns=["customers__name", "suppliers__name"])
    a = at(by=["customers.name", "suppliers.name"], window={"last_days": 30})
    [e] = plan([r], [a], [v("Acme")])
    assert e["skip"] == "several by dimensions"


def test_member_dimension_unknown():
    [e] = plan([rt()], [at()], [v("Acme")])  # a member row on a tile with no by
    assert e["skip"] == "member dimension unknown"


def test_fixed_combo_with_grain_and_by_without_series_skipped():
    r = rt(type="combo", follow="fixed", grain="month", by_columns=["customers__name"])
    a = at(grain="month", by=["customers.name"], window={"last_days": 90})
    [e] = plan([r], [a], [v("2026-09-01")])
    assert e["skip"] == "member not shown"


def test_controlled_table_two_by_uses_first_by_entry():
    r = rt(by_columns=["customers__name", "channels__name"],
           columns=[{"column": "channels__name"}, {"column": "customers__name"}])
    [e] = plan([r], [at(by=["customers.name", "channels.name"])], [v("Acme")])
    assert "c.name = 'Acme'" in e["sql"] and e["member"] == "Acme"


def test_fixed_table_by_without_grain_member_not_in_columns():
    r = rt(follow="fixed", by_columns=["customers__name"], columns=[{"column": "rolls_in"}])
    a = at(by=["customers.name"], window={"last_days": 30})
    [e] = plan([r], [a], [v("Acme")])
    assert "c.name = 'Acme'" in e["sql"] and span(e) == ("2026-09-09", "2026-10-08")


def test_member_dimension_not_in_reference():
    r = rt(by_columns=["channels__name"])
    [e] = plan([r], [at(by=["channels.name"])], [v("Web")])
    assert e["skip"] == "member dimension not in reference"


def test_refreshed_day_shifts_fixed_window():
    r = rt(follow="fixed")
    a = at(window={"last_days": 7})
    [e] = plan([r], [a], [v("")], refreshed={"q_t1": date(2026, 10, 7)})
    assert span(e) == ("2026-10-01", "2026-10-07")
    [f] = plan([r], [a], [v("")])
    assert span(f) == ("2026-10-02", "2026-10-08")


def test_last_periods_window():
    r = rt(follow="fixed", grain="month")
    a = at(grain="month", window={"last_periods": 3})
    [e] = plan([r], [a], [v("2026-08-01")])
    assert span(e) == ("2026-08-01", "2026-08-31")
    [f] = plan([r], [a], [v("2026-07-01")])
    assert f["skip"] == "outside window"


def test_compare():
    assert dr.compare(10.0, 10.0)
    assert dr.compare(1000000.0, 1000000.0000005)
    assert not dr.compare(10.0, 10.1)
    assert not dr.compare(10.0, None)
    assert dr.compare(0.0, 1e-7)


def test_start_end_window_end_is_exclusive():
    r = rt(follow="fixed")
    a = at(window={"start": "2026-09-01", "end": "2026-10-01"})
    [e] = plan([r], [a], [v("")])
    assert span(e) == ("2026-09-01", "2026-09-30")
    rg = rt(follow="fixed", grain="month")
    ag = at(grain="month", window={"start": "2026-08-15", "end": "2026-10-01"})
    [f, g] = plan([rg], [ag], [v("2026-08-01"), v("2026-09-01")])
    assert span(f) == ("2026-08-15", "2026-08-31")
    assert span(g) == ("2026-09-01", "2026-09-30")
