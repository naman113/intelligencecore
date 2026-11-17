"""Phase 22 plan 03: pure C-04 date math against the hand-checked shared vectors."""
import json
from datetime import date
from pathlib import Path

import pytest

from app import dashdates as dd

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "dashboard_date_vectors.json").read_text())


def _d(s):
    return date.fromisoformat(s)


def test_vector_count():
    assert len(VECTORS["cases"]) >= 60


@pytest.mark.parametrize("c", VECTORS["cases"], ids=lambda c: f"{c['today']}-fy{c['fy_start_month']}-{c['preset']}-{c['period']}")
def test_preset_range_matches_vectors(c):
    got = dd.preset_range(c["preset"], c["period"], _d(c["today"]), c["fy_start_month"])
    assert got == (_d(c["from"]), _d(c["to"]))


def test_every_preset_and_period_covered():
    seen = {(c["preset"], c["period"]) for c in VECTORS["cases"]}
    assert seen == {(p, q) for p in dd.PRESETS for q in dd.PERIODS}


@pytest.mark.parametrize("c", VECTORS["custom"])
def test_custom_ranges(c):
    s, e = _d(c["from"]), _d(c["to"])
    assert dd.previous_range(s, e) == tuple(_d(x) for x in c["previous"])
    assert dd.last_year_range(s, e) == tuple(_d(x) for x in c["last_year"])


@pytest.mark.parametrize("c", VECTORS["buckets"])
def test_bucket_for(c):
    assert dd.bucket_for(_d(c["from"]), _d(c["to"])) == c["bucket"]


@pytest.mark.parametrize("c", VECTORS["horizon"])
def test_horizon_start(c):
    assert dd.horizon_start(_d(c["today"]), c["days"]) == _d(c["start"])
