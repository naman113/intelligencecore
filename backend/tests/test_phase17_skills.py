"""CONS-04 / CONS-01: the exploration and KPI skills teach the model lanes, and the
Phase 17 refreshes are md5-guarded so owner edits survive."""
import hashlib
import re
from pathlib import Path

import pytest

from app import skillservice

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root()
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
START = "-- Phase 17: model consumers"
END = "-- End Phase 17: model consumers"
GUARD = "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN ("
TAGS = {"exploratory-data-analysis": "$p17_eda$", "kpi-selection": "$p17_kpi_selection$"}
# md5 of every body shipped before Phase 17 (seed, Quick 004 refresh, Quick 006 refresh).
EARLIER = {
    "exploratory-data-analysis": {"fe65ca8c49e9c2a9722c142e07a88772",
                                  "6732dd6d9a04a9274a3d1655c76447c7",
                                  "33707c374aee6fad24c9684cedba2e7b"},
    "kpi-selection": {"70b3d2ad76aeff1927c5809de5d1a7ac"},
}


def _text(slug):
    return (LIB / "intelligence-corner" / slug / "SKILL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t):
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _region():
    a = SCHEMA.index("-- Phase 17 (CONS-")
    return SCHEMA[a:SCHEMA.index(END)]


def _between(s, tag):
    a = s.index(tag)
    return s[a + len(tag):s.index(tag, a + len(tag))]


def _stmt(slug):
    r, tag = _region(), TAGS[slug]
    assert r.count(tag) == 2
    b = r.index(tag, r.index(tag) + len(tag)) + len(tag)
    return r[b:r.index(");", b) + 2]


@pytest.mark.parametrize("slug", TAGS)
def test_refresh_body_equals_file_and_is_guarded(slug):
    assert _between(_region(), TAGS[slug]).strip() == _text(slug).strip()
    stmt = _stmt(slug)
    assert stmt.startswith(f", updated_at = now()\n WHERE name = '{slug}' AND {GUARD}")
    hashes = set(re.findall(r"'([0-9a-f]{32})'", stmt))
    assert hashes == EARLIER[slug]
    assert _md5(_text(slug)) not in hashes


def test_refreshes_never_enable():
    r = _region()
    assert "SET enabled" not in r and "enabled = TRUE" not in r


def test_q006_copy_equals_file():
    assert _between(SCHEMA, "$q006_eda$").strip() == _text("exploratory-data-analysis").strip()


def test_every_body_copy_equals_file():
    for slug, tags in (("exploratory-data-analysis", ("$skill_eda$", "$q004_eda$", "$q006_eda$")),
                       ("kpi-selection", ("$skill_kpi_selection$",))):
        for tag in tags:
            assert _between(SCHEMA, tag).strip() == _text(slug).strip(), tag


def test_eda_teaches_lanes_and_budget():
    t = _text("exploratory-data-analysis")
    for w in ("get_model", "query_measure", "evidence_measure", "unverified", "LIVE QUERY BUDGET"):
        assert w in t, w


def test_kpi_selection_teaches_measures():
    t = _text("kpi-selection")
    assert "measure=" in t and "propose_kpi" in t
