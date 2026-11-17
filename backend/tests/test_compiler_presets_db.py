"""Phase 22 plan 03: preset_bounds_sql on real Postgres against the shared C-04 vectors.

Skipped unless DASH_TEST_DATABASE_URL is set (same pattern as test_dashboard_refresh_db.py).
CURRENT_DATE occurs once, inside the _today CTE; the test swaps that one occurrence for a
fixed date, so every bound is computed from the same "today".
"""
import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from app import dashdates as dd
from app import modelcompiler as mc
from app.modelcompiler import preset_bounds_sql

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres dashboard tests)",
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "dashboard_date_vectors.json").read_text())

GROUPS = defaultdict(list)
for _c in VECTORS["cases"]:
    GROUPS[(_c["today"], _c["fy_start_month"])].append(_c)
# 4 distinct todays: the base day, a month-end clamp day, a leap day and a financial-year edge
CHOSEN = [("2026-11-04", 4), ("2026-03-31", 4), ("2028-02-29", 4), ("2026-01-15", 4), ("2026-11-04", 10)]


@pytest.mark.parametrize("today,fy", CHOSEN)
async def test_bounds_match_vectors(today, fy):
    import asyncpg

    sql = preset_bounds_sql(dd.PRESETS, fy)
    assert sql.count("CURRENT_DATE") == 1
    sql = sql.replace("CURRENT_DATE", f"DATE '{today}'")
    conn = await asyncpg.connect(os.environ["DASH_TEST_DATABASE_URL"])
    try:
        rows = await conn.fetch(sql)
    finally:
        await conn.close()
    got = {(r["preset"], r["period"]): (r["from_date"], r["to_date"]) for r in rows}
    assert len(rows) == 27
    # every row equals the Python rules, and every hand-checked vector for this today
    for (preset, period), (lo, hi) in got.items():
        assert (lo, hi) == dd.preset_range(preset, period, date.fromisoformat(today), fy)
    for c in GROUPS[(today, fy)]:
        assert got[(c["preset"], c["period"])] == (date.fromisoformat(c["from"]), date.fromisoformat(c["to"]))


REL = "pg_temp.ic_presets_fixture"


def _fixture_model():
    cols = {REL: {"id": "integer", "cust": "integer", "d": "date"}}
    tables = [dict(name="ic_presets_fixture", relation=REL, object_kind="base", role=None, grain=None,
                   primary_key=[], agg_time_dimension=None, status="active")]
    measure = dict(name="dc", type="simple", label=None, description=None, base_relation=REL,
                   agg="count_distinct", expr="cust", agg_time_dimension=f"{REL}.d", filter=[],
                   numerator=None, denominator=None, input_metrics=None, non_additive_dimension=None,
                   additivity="additive", unit=None, format=None, status="active")
    return mc.model_from_rows(tables, [], [measure], cols)


@pytest.mark.parametrize("today,fy", CHOSEN)
async def test_compile_presets_matches_python_ranges(today, fy):
    """The production C-05 SQL (compile_presets), not just preset_bounds_sql, against the vectors.

    One customer per calendar day over 2024-01-01..2030-12-31, so COUNT(DISTINCT cust) for a
    range equals the number of days in it; every preset and period must equal the Python rule.
    """
    import asyncpg

    c = mc.compile_presets(_fixture_model(), "dc", presets=list(mc.DASH_PRESETS), fy_start_month=fy)
    assert c.sql.count("CURRENT_DATE") == 1
    sql = c.sql.replace("CURRENT_DATE", f"DATE '{today}'")
    conn = await asyncpg.connect(os.environ["DASH_TEST_DATABASE_URL"])
    try:
        await conn.execute(
            f"CREATE TEMP TABLE ic_presets_fixture AS SELECT n AS id, n AS cust, "
            f"(DATE '2024-01-01' + n) AS d FROM generate_series(0, 2556) AS n")
        rows = await conn.fetch(sql)
    finally:
        await conn.close()
    assert len(rows) == 27
    t = date.fromisoformat(today)
    for r in rows:
        lo, hi = dd.preset_range(r["preset"], r["period"], t, fy)
        assert r["value"] == (hi - lo).days + 1, (r["preset"], r["period"])
        assert r["today"] == today
