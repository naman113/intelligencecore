"""Quick 030: the analysis skills take their numbers from the semantic model.

Order taught by every analysis skill: approved measures through query_measure, then the
model's tables through run_sql (with the measure's approved definition), then raw tables
(imported before live), and with no approved model the skill's own steps as before.
The method parts (scripts, thresholds, reporting) are pinned by the Quick 004, 006, 13.1,
013 and Phase 17 tests; this file pins the data access and the md5-guarded refresh that
carries the new bodies to unedited installs."""

import hashlib
import re
from pathlib import Path

import pytest

from app import skillservice
from app.agents import graph
from app.guardrails import validate_readonly_sql

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root() / "intelligence-corner"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")

ANALYSIS = ("anomaly-detection", "driver-analysis", "trend-and-seasonality", "forecasting",
            "statistical-tests", "exploratory-data-analysis")
SERIES = ("anomaly-detection", "trend-and-seasonality", "forecasting")
SECTION = "## Where the numbers come from"

START = "-- ── Quick 030: analysis skills use the model ──"
END = "-- End Quick 030: analysis skills use the model"
GUARD = "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN ("
TAGS = {"anomaly-detection": "$q030_anomaly_detection$",
        "driver-analysis": "$q030_driver_analysis$",
        "trend-and-seasonality": "$q030_trend_seasonality$",
        "forecasting": "$q030_forecasting$",
        "statistical-tests": "$q030_statistical_tests$",
        "exploratory-data-analysis": "$q030_eda$",
        "schema-documentation": "$q030_schema_doc$",
        "connect-data-source": "$q030_connect_data_source$"}
# md5 (CR removed, outer whitespace stripped) of every body each skill has had in git
# before Quick 030; the last one per skill is what milestone-2 shipped at da6affe.
EARLIER = {
    "anomaly-detection": {"8e8af290fc546f7c33fd937fa5d14dc3", "60694215701606d0644f7bd26c04f979",
                          "a7e790f91efdbf9223f3de78125f353a"},
    "driver-analysis": {"0675800e7a1f0a3a8c9d911f26a9fcbd", "7d855bdce5995d1d306d5e2195881970",
                        "88798a29f759f979f2a0bca235432c8d"},
    "trend-and-seasonality": {"df54faf49ea9fc99c32532201594d80b", "cd8bacec32e04bda3fee78ed10478e4e"},
    "forecasting": {"aeb81a7e4507054bd468052383ddf635", "60c1ccadd9a07f97f62a5c34f4e78c61",
                    "4ba0bf4f377af4c3026ff9bf5e8d30b5", "50bebd46684a72ff2e6dccc2cee319d7",
                    "f232bbcf8d59ac1b8a43cd051040205b"},
    "statistical-tests": {"6e8a9d48661b7cd94bfd34726f9be24d", "87a218a12376c60d5124856fc370231d",
                          "3f26d1aeac11ce6347dd73ec5052d037", "3351f363e059ede53c0fb37e50d3f611"},
    "exploratory-data-analysis": {"fe65ca8c49e9c2a9722c142e07a88772", "6732dd6d9a04a9274a3d1655c76447c7",
                                  "33707c374aee6fad24c9684cedba2e7b", "06126a5658f0e0d0d4e2ffa9377ba3f2"},
    "schema-documentation": {"69621a748ad5ba47bf18cd9b7095d8e0", "8888fca53fb9e75022f8377f0856f8e9",
                             "625dbe0592a6995561285b1b4f72ff21"},
    "connect-data-source": {"4d997b3b3f1ab361a0d4c8e16fe0ad47"},
}
# Quick 037: the six analysis guards also accept the unedited Quick 030 body (get_model sections line).
for _slug, _h in {
    "anomaly-detection": "b290b5733e4ae652e66cfb2ba477cd15", "driver-analysis": "83e49585d66d86b56767aeb6eb7b258a",
    "trend-and-seasonality": "e37897df5666f8506a9999ef6ca72b09", "forecasting": "3606051781555f130b6b380100645352",
    "statistical-tests": "f216ff5cba2327469504125d2fd4c3f5",
    "exploratory-data-analysis": "41444dbe3a5b745a377725d64f0f1025"}.items():
    EARLIER[_slug] = EARLIER[_slug] | {_h}
# Every dollar-quoted copy of each body in schema.sql (seed plus earlier refreshes).
COPIES = {
    "anomaly-detection": ("$skill_anomaly_detection$", "$q006_anomaly_detection$"),
    "driver-analysis": ("$skill_driver_analysis$",),
    "trend-and-seasonality": ("$skill_trend_seasonality$",),
    "forecasting": ("$skill_forecasting$", "$p131_forecasting$", "$q013_forecasting$"),
    "statistical-tests": ("$skill_statistical_tests$", "$q006_statistical_tests$"),
    "exploratory-data-analysis": ("$skill_eda$", "$q004_eda$", "$q006_eda$", "$p17_eda$"),
    "schema-documentation": ("$skill_schema_doc$", "$p16_schema_doc$"),
    "connect-data-source": ("$skill_connect_data_source$",),
}


def _text(slug: str) -> str:
    return (LIB / slug / "SKILL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _section(t: str) -> str:
    a = t.index(SECTION)
    nxt = t.find("\n## ", a + len(SECTION))
    return t[a:nxt if nxt != -1 else len(t)]


def _between(s: str, tag: str) -> str:
    assert s.count(tag) == 2, f"{tag} must occur exactly twice"
    a = s.index(tag) + len(tag)
    return s[a:s.index(tag, a)]


def _block() -> str:
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    a, b = SCHEMA.index(START), SCHEMA.index(END)
    assert b > a
    return SCHEMA[a:b]


# ── the order every analysis skill teaches ─────────────────────────────────

@pytest.mark.parametrize("slug", ANALYSIS)
def test_section_teaches_model_then_model_tables_then_raw(slug):
    t = _text(slug)
    assert t.count(SECTION) == 1, "one data-access section per skill"
    sec = _section(t)
    order = ("get_model", "query_measure", "Model tables next", "Raw tables last", "No approved model")
    pos = [sec.find(w) for w in order]
    assert all(p >= 0 for p in pos), dict(zip(order, pos))
    assert pos == sorted(pos), f"{slug}: steps out of order {dict(zip(order, pos))}"
    for term in ("data_as_of", "imported before live", "unverified", "approved expr, filter and time column",
                 "run_sql_to_csv", "unchanged"):
        assert term in sec, (slug, term)


@pytest.mark.parametrize("slug", ANALYSIS)
def test_section_comes_before_the_method(slug):
    t = _text(slug)
    a = t.index(SECTION)
    first_script = min((i for i in (t.find("```python"), t.find("```sql")) if i != -1), default=len(t))
    assert a < first_script, "data access is read before any query or script"
    for h in ("## Reporting", "## Recording"):
        if h in t:
            assert a < t.index(h), h


@pytest.mark.parametrize("slug", ANALYSIS)
def test_queries_count_query_measure_in_the_budget(slug):
    budget = _text(slug).split("## Budget", 1)[1].split("\n## ", 1)[0]
    assert "query_measure" in budget, slug


@pytest.mark.parametrize("slug", SERIES)
def test_series_skills_fill_empty_periods_around_the_measure(slug):
    t = _text(slug)
    assert "generate_series" in t and "metric_time__" in t, slug
    assert "levels" in t.lower(), "levels such as stock are never 0-filled"


@pytest.mark.parametrize("slug", ("anomaly-detection", "trend-and-seasonality", "statistical-tests",
                                  "driver-analysis"))
def test_wrapper_example_is_named_and_not_a_sql_block(slug):
    """The wrapper carries placeholders, so it is a text block; every sql block still passes."""
    t = _text(slug)
    sec = _section(t)
    assert "```text" in sec and "WITH s AS (" in sec and "LEFT JOIN s ON s.metric_time__" in sec
    for sql in re.findall(r"```sql[ \t]*\n(.*?)```", t, re.DOTALL):
        validate_readonly_sql(sql)


def test_forecasting_step4_and_nightly_definition_can_use_the_measure():
    t = _text("forecasting")
    steps = t[t.index("## Steps"):t.index("## Every night")]
    assert "query_measure" in steps and "metric_time__week" in steps
    night = t[t.index("## Every night: a forecast definition"):t.index("## Reporting")]
    assert "get_model" in night and "query_measure" in night and "no range" in night


def test_driver_analysis_reconciles_the_split_to_the_measure_totals():
    t = _text("driver-analysis")
    contrib = t[t.index("## 1. Contribution"):t.index("## 2. Factors")]
    assert "query_measure" in contrib and "reconcile" in contrib.lower() and "unverified" in contrib


def test_statistical_tests_export_one_row_per_unit_from_the_measure():
    t = _text("statistical-tests")
    steps = t[t.index("## Steps"):t.index("## Reading the result")]
    assert "query_measure" in steps and "UNION ALL" in steps


def test_eda_keeps_its_lanes_and_budget():
    t = _text("exploratory-data-analysis")
    for w in ("evidence_measure", "LIVE QUERY BUDGET", "evidence_sql", "unverified"):
        assert w in _section(t), w
    assert "## Two lanes on the warehouse" not in t, "merged into the shared section"


# ── schema-documentation and connect-data-source wording ───────────────────

def test_schema_documentation_names_connected_sources_and_the_model():
    t = _text("schema-documentation")
    assert "discover_schema(<source name>)" in t and "business_db" in t
    assert "Do not call run_sql for sampling" in t
    lanes = t[t.index("## Two lanes"):t.index("## Grounding and safety")]
    assert "data-modeling" in lanes and "query_measure" in lanes and "get_model" in lanes


def test_connect_data_source_models_after_approval_and_prefers_import():
    t = _text("connect-data-source")
    after = t[t.index("## After approval"):t.index("## Status and problems")]
    assert "data-modeling" in after and "query_measure" in after
    assert "live-query budget" in t and "Import" in t


# ── the prompt line that could override measure-first ─────────────────────

def test_quick_lookup_routing_names_query_measure():
    r = " ".join(graph.SQL_ROUTING_RULE.split())
    assert "QUICK LOOKUP: answer it yourself with search_business_context + run_sql" in r
    q = r[r.index("QUICK LOOKUP"):r.index("HEAVY INVESTIGATION")]
    assert "query_measure if get_model lists its measure" in q
    assert len(graph.SQL_ROUTING_RULE) <= 1800


# ── schema.sql: every copy equals its file, and the guarded refresh ────────

@pytest.mark.parametrize("slug", TAGS)
def test_every_body_copy_equals_the_file(slug):
    for tag in COPIES[slug] + (TAGS[slug],):
        assert _between(SCHEMA, tag).strip() == _text(slug).strip(), tag


@pytest.mark.parametrize("slug", TAGS)
def test_refresh_is_guarded_on_every_earlier_body(slug):
    b, tag = _block(), TAGS[slug]
    head = f"UPDATE skills SET body = {tag}"
    assert b.count(head) == 1
    end = b.index(tag, b.index(head) + len(head)) + len(tag)
    stmt = b[end:b.index(");", end) + 2]
    assert stmt.startswith(f", updated_at = now()\n WHERE name = '{slug}' AND {GUARD}")
    hashes = set(re.findall(r"'([0-9a-f]{32})'", stmt))
    assert hashes == EARLIER[slug]
    assert _md5(_text(slug)) not in hashes, "the new body must not be in its own guard"


def test_block_is_last_never_enables_and_only_updates_skills():
    b = _block()
    assert SCHEMA.index(START) > SCHEMA.index("$p17_kpi_selection$", SCHEMA.index("$p17_kpi_selection$") + 1)
    assert "SET enabled" not in b and "enabled = TRUE" not in b and "INSERT" not in b
    assert "SET description" not in b and ", description" not in b, "descriptions (and the manifest) are unchanged"
    assert b.count("UPDATE skills SET body = ") == len(TAGS)
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER|GRANT)\b", b)


@pytest.mark.parametrize("slug", TAGS)
def test_descriptions_unchanged(slug):
    """The frontmatter description is the seed's and the manifest's; Quick 030 changes bodies only."""
    d = skillservice.parse_skill_md(_text(slug))["description"]
    assert f"'{d.replace(chr(39), chr(39) * 2)}'" in SCHEMA
