"""EXPL-04 pins for the two first-party exploration skills; skills are data,
seeded installed-but-disabled by the Phase 7 block of schema.sql."""

import json
import re
from pathlib import Path

import pytest

from app import skillservice
from app.guardrails import validate_readonly_sql

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root()
SLUGS = ("exploratory-data-analysis", "kpi-selection")
TAGS = {"exploratory-data-analysis": "$skill_eda$", "kpi-selection": "$skill_kpi_selection$"}
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
HEADER = "-- ── Phase 7: Exploration and insights ──"
END = "-- End Phase 7: Exploration and insights"


def _path(slug: str) -> Path:
    return LIB / "intelligence-corner" / slug / "SKILL.md"


def _text(slug: str) -> str:
    p = _path(slug)
    assert p.is_file(), f"missing {p}"
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _fenced(text: str, lang: str) -> str:
    m = re.search(r"```" + re.escape(lang) + r"[ \t]*\n(.*?)```", text, re.DOTALL)
    assert m, f"no fenced {lang} block"
    return m.group(1)


def _block() -> str:
    assert SCHEMA.count(HEADER) == 1 and SCHEMA.count(END) == 1
    a, b = SCHEMA.index(HEADER), SCHEMA.index(END)
    assert b > a
    return SCHEMA[a:b]


@pytest.mark.parametrize("slug", SLUGS)
def test_skill_parses(slug):
    p = skillservice.parse_skill_md(_text(slug))
    assert p["name"] == slug
    assert p["license"] == "Proprietary"
    assert 1 <= len(p["description"]) <= 200
    for ch in ("#", "`", "<", ">", "'"):
        assert ch not in p["description"], ch


@pytest.mark.parametrize("slug", SLUGS)
def test_skill_is_ascii_small_and_url_free(slug):
    raw = _path(slug).read_bytes()
    raw.decode("ascii")
    assert len(raw) < 8000
    t = raw.decode("ascii")
    for bad in ("http://", "https://", "www.", "$skill_"):
        assert bad not in t, bad


@pytest.mark.parametrize("slug", SLUGS)
def test_skill_has_no_write_sql(slug):
    assert not re.search(r"\b(DELETE|DROP|INSERT|UPDATE|TRUNCATE|ALTER|GRANT)\b", _text(slug))


def test_eda_sections_and_terms():
    t = _text("exploratory-data-analysis")
    for h in ("## When to use", "## Budget", "## Procedure", "## What counts as notable",
              "## Recording", "## Nothing to report", "## Grounding and safety"):
        assert h in t, h
    for term in ("record_insight", "run_sql", "run_sql_to_csv", "run_python",
                 "APPROVED TABLES", "ALREADY RECORDED", "DATE '", "now()"):
        assert term in t, term


def test_eda_example_sql_is_valid_readonly():
    sql = _fenced(_text("exploratory-data-analysis"), "sql")
    validate_readonly_sql(sql)
    low = sql.lower()
    assert "current_date" not in low and "now()" not in low


def test_kpi_sections_and_terms():
    t = _text("kpi-selection")
    for h in ("## When to use", "## A good KPI", "## Writing the condition", "## Cadence",
              "## Limits", "## Safety"):
        assert h in t, h
    for term in ("propose_kpi", "hourly", "daily", "weekly", "ALREADY WATCHED"):
        assert term in t, term


def test_manifest_entries():
    data = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    ids = [e["id"] for e in data]
    assert ids == sorted(ids)
    for slug in SLUGS:
        eid = f"intelligence-corner/{slug}"
        hits = [e for e in data if e["id"] == eid]
        assert len(hits) == 1
        e = hits[0]
        assert (e["name"], e["source"], e["license"], e["compat"], e["relevant"], e["path"]) == (
            slug, "intelligence-corner", "Proprietary", "general", True, eid)
        assert e["description"] == skillservice.parse_skill_md(_text(slug))["description"]


def test_relevant_slugs_list_both():
    src = (ROOT / "backend" / "scripts" / "fetch_skills_library.py").read_text(encoding="utf-8")
    for slug in SLUGS:
        assert f'"intelligence-corner/{slug}"' in src


def _seed_body(slug: str) -> str:
    s, tag = _block(), TAGS[slug]
    assert SCHEMA.count(tag) == 2, f"{tag} must occur exactly twice in schema.sql"
    a = s.index(tag)
    b = s.index(tag, a + len(tag))
    return s[a + len(tag):b]


@pytest.mark.parametrize("slug", SLUGS)
def test_seed_rows_disabled_and_idempotent(slug):
    s = _block()
    assert f"'{slug}'" in s
    tail = f"'library', 'intelligence-corner/{slug}', 'Proprietary', 'general', FALSE)"
    assert tail in s
    after = s[s.index(tail):]
    assert after[len(tail):].lstrip().startswith("ON CONFLICT (name) DO NOTHING;")


@pytest.mark.parametrize("slug", SLUGS)
def test_seed_body_equals_file(slug):
    assert _seed_body(slug).strip() == _text(slug).strip()


@pytest.mark.parametrize("slug", SLUGS)
def test_seed_description_equals_frontmatter(slug):
    d = skillservice.parse_skill_md(_text(slug))["description"].replace("'", "''")
    assert f"'{d}'" in _block()


def test_block_never_enables_or_overwrites():
    s = _block()
    assert "DO UPDATE" not in s
    assert "UPDATE skills" not in s
    assert "enabled = TRUE" not in s
    assert ", TRUE)" not in s
