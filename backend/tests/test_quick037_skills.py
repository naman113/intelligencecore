"""Quick 037: the data-modeling and analysis skills say how to read one measure in full.

get_model now answers a summary; the skills name get_model(section="measures", name=...) for a
measure's full definition (data-modeling also for tables and candidates). Every copy of each body
in schema.sql equals its file, and the md5 guards also accept the body each skill shipped before
Quick 037, so unedited installs get the line and owner-edited copies are never touched."""
import hashlib
import re
from pathlib import Path

import pytest

from app import skillservice

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root() / "intelligence-corner"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
DRILL = 'get_model(section="measures", name="<measure>")'
GUARD = r"md5\(btrim\(replace\(body, chr\(13\), ''\), ' ' \|\| chr\(10\) \|\| chr\(9\)\)\) IN \((.*?)\);"

# slug: (md5 of the body shipped before Quick 037, the refresh tag whose guard must accept it, every copy)
SKILLS = {
    "anomaly-detection": ("b290b5733e4ae652e66cfb2ba477cd15", "$q030_anomaly_detection$",
                          ("$skill_anomaly_detection$", "$q006_anomaly_detection$")),
    "driver-analysis": ("83e49585d66d86b56767aeb6eb7b258a", "$q030_driver_analysis$", ("$skill_driver_analysis$",)),
    "trend-and-seasonality": ("e37897df5666f8506a9999ef6ca72b09", "$q030_trend_seasonality$",
                              ("$skill_trend_seasonality$",)),
    "forecasting": ("3606051781555f130b6b380100645352", "$q030_forecasting$",
                    ("$skill_forecasting$", "$p131_forecasting$", "$q013_forecasting$")),
    "statistical-tests": ("f216ff5cba2327469504125d2fd4c3f5", "$q030_statistical_tests$",
                          ("$skill_statistical_tests$", "$q006_statistical_tests$")),
    "exploratory-data-analysis": ("41444dbe3a5b745a377725d64f0f1025", "$q030_eda$",
                                  ("$skill_eda$", "$q004_eda$", "$q006_eda$", "$p17_eda$")),
    "data-modeling": ("68346f88ff151f598197ba7b44d05424", "$p16_data_modeling$", ("$skill_data_modeling$",)),
}


def _text(slug: str) -> str:
    return (LIB / slug / "SKILL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _between(tag: str) -> str:
    assert SCHEMA.count(tag) == 2, tag
    a = SCHEMA.index(tag) + len(tag)
    return SCHEMA[a:SCHEMA.index(tag, a)]


def _guard(tag: str) -> set:
    end = SCHEMA.index(tag, SCHEMA.index(tag) + len(tag)) + len(tag)
    m = re.compile(GUARD, re.S).search(SCHEMA, end)
    assert m and m.start() - end < 200, tag
    return set(re.findall(r"'([0-9a-f]{32})'", m.group(1)))


@pytest.mark.parametrize("slug", SKILLS)
def test_skill_names_the_measure_section(slug):
    t = _text(slug)
    assert DRILL in t
    assert "approved expr, filter and time column" in t or slug == "data-modeling"


def test_data_modeling_names_tables_and_candidates():
    t = _text("data-modeling")
    step = next(ln for ln in t.splitlines() if ln.startswith("1. Call get_model"))
    assert DRILL in step and 'section="tables"' in step and 'section="candidates"' in step
    assert "derived_sql" not in t, "Quick 032 pins the view shape wording"
    assert t.isascii()


@pytest.mark.parametrize("slug", SKILLS)
def test_every_copy_equals_the_file(slug):
    _, refresh, copies = SKILLS[slug]
    for tag in copies + (refresh,):
        assert _between(tag).strip() == _text(slug).strip(), tag


@pytest.mark.parametrize("slug", SKILLS)
def test_guard_accepts_the_previous_body_and_not_the_new_one(slug):
    old, refresh, _ = SKILLS[slug]
    hashes = _guard(refresh)
    assert old in hashes
    assert _md5(_text(slug)) not in hashes and _md5(_text(slug)) != old


def test_data_modeling_v3_fixture_is_the_previous_body():
    fx = (Path(__file__).parent / "fixtures" / "data_modeling_skill_v3.md").read_bytes().decode("ascii")
    assert _md5(fx) == SKILLS["data-modeling"][0]
    assert DRILL not in fx and "A view may use AND/OR/NOT" in fx


def test_only_bodies_and_guards_changed():
    """No description, enabled flag or new statement: the skill rows keep their consent state."""
    for slug in SKILLS:
        d = skillservice.parse_skill_md(_text(slug))["description"]
        assert f"'{d.replace(chr(39), chr(39) * 2)}'" in SCHEMA
    assert SCHEMA.count("-- Quick 037:") == 2
