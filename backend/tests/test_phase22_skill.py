"""Phase 22 plan 18: dashboard-authoring SKILL.md teaches spec v2 in no more bytes than before.

Size, headings, examples (parse and bind as spec v2), the limits the code enforces, recipe names,
no colour codes, and the forecast-panel pointer. No DB.
"""
import re
from pathlib import Path

import pytest

from app import dashboardspec as ds
from app import skillservice
from app.config import Settings
from tests.test_phase22_skill_parts import RECIPES, fixture_model, json_blocks

Q045_BODY_MD5 = "88dd62dc888233f7524175260bcfba39"  # what Quick 045 shipped (5ae19a6)
P22_18_BODY_MD5 = "3bec77283bc851fbf913f9b235076169"  # the 22-18 body, superseded by the review fix d058d52
P22_FIX_BODY_MD5 = "421b0cc7a933e1894121cffb05d19e8e"  # the body after review fix d058d52, superseded by d3323d2
P22_REVIEW_BODY_MD5 = "f2d3377bf9b896adf024ee7e4291db2c"  # the body after review fix d3323d2, superseded by 22-16
P22_16_BODY_MD5 = "6c0af4286ca572d99cc86fdd47171fe1"  # the body after 22-16 (3aea0c3), superseded by the release-eval colour line
P22_COLOUR_BODY_MD5 = "e11d080cd4b291847a3815a31ab37b84"  # the body after the colour line (62273d3), superseded by the weekly last-year line
P22_WEEKLY_BODY_MD5 = "165335ac06d4499bf26df2e4f6db6201"  # the body after the weekly last-year line (e3d24d0), superseded by the date-control viewer features line
SKILL = skillservice.library_root() / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
MAX_BYTES = 16632
HEADINGS = ["When to use and workflow", "Say what is not possible", "Spec v2 in brief",
            "Pick the visual", "Date control", "Measures first", "Tones and colour",
            "Numbers, dates and titles", "Recipes", "KPI history", "Forecast panel",
            "Converting", "Pre-save checklist"]


def _text() -> str:
    return SKILL.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_size_ascii_and_frontmatter():
    raw = SKILL.read_bytes()
    assert len(raw) <= MAX_BYTES and raw.isascii()
    t = _text()
    assert t.startswith("---\nname: dashboard-authoring\ndescription: House style for live business "
                        "dashboards as chart specs")
    assert "\nlicense: Proprietary\n---\n" in t


def test_headings_in_order():
    t = _text()
    found = [h for h in re.findall(r"^## (.+)$", t, re.M) if h in HEADINGS]
    assert found == HEADINGS


def test_every_example_parses_and_binds_as_v2():
    model = fixture_model()
    blocks = json_blocks(_text())
    assert blocks
    for raw in blocks:
        spec = ds.parse(raw)
        assert spec.spec_version == 2
        compiled = ds.compile_measures_v2(spec, model)
        stored = ds.bind_v2(spec, list(raw.get("queries", [])), compiled)
        assert stored["spec_version"] == 2


def test_example_totals_row_has_a_column_to_total():
    # a query-bound table is totalled only on columns that say total: true (C-03)
    for raw in json_blocks(_text()):
        for tile in raw.get("tiles", []):
            if tile.get("type") == "table" and tile.get("totals") and tile.get("query"):
                assert any(c.get("total") for c in tile["columns"]), tile["id"]


def test_limits_match_the_code():
    t = _text()
    assert ds.MAX_TILES_V2 == 40 and ds.MAX_CHARTS_V2 == 12
    assert "40 tiles" in t and "12 chart tiles" in t
    props = ds.SpecV2.model_json_schema()["properties"]["filters"]
    assert props["maxItems"] == 3 and "at most 3" in t
    assert Settings().dashboard_preview_rounds == 3 and "at most 3 preview rounds" in t
    assert "50 values" in t
    assert ds.MAX_SPEC_BYTES_V2 == 131072 and "128 KB of JSON" in t
    assert "operator's time zone" in t and "UTC on hosted installs" not in t


def test_recipes_exist_and_reference_load_is_conditional():
    t = _text()
    sec = t[t.index("## Recipes"):t.index("## KPI history")]
    named = re.findall(r"^- `([a-z0-9-]+)`:", sec, re.M)
    assert sorted(named) == sorted(RECIPES)
    for name in named:
        assert (SKILL.parent / "recipes" / f"{name}.md").is_file()
    assert 'part="reference/spec-v2.md"` only when a field is unclear' in t


def test_no_colour_codes_and_no_sql_block():
    t = _text()
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", t)
    assert "`" * 3 + "sql" not in t


def test_forecast_section_declares_source_and_points_to_reference():
    t = _text()
    sec = t[t.index("## Forecast panel"):t.index("## Converting")]
    assert '{"id": "fc", "source": "forecast", "forecast_id": N, "refresh_minutes": 60}' in sec
    assert "period, actual, forecast, low, high, method, wape, as_of" in sec
    assert "get_forecast" in sec and 'part="reference/forecast-panel.md"' in sec


def test_pinned_predecessor_constant():
    assert re.fullmatch(r"[0-9a-f]{32}", Q045_BODY_MD5)


# ── schema.sql: body copies and the md5-guarded refresh ─────────────────────

import hashlib  # noqa: E402

from tests.test_quick045_skill import COPIES, GUARD  # noqa: E402

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")
START = "-- ── Phase 22: dashboard-authoring for spec v2 ──"
END = "-- End Phase 22: dashboard-authoring for spec v2"
TAG = "$p22_dashboard_authoring$"


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _between(s: str, tag: str) -> str:
    assert s.count(tag) == 2, f"{tag} must occur exactly twice"
    a = s.index(tag) + len(tag)
    return s[a:s.index(tag, a)]


def test_every_body_copy_equals_the_file():
    for tag in COPIES + (TAG,):
        assert _between(SCHEMA, tag).strip() == _text().strip(), tag


def test_refresh_is_guarded_on_the_quick045_body_and_never_enables():
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    block = SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]
    assert block.count("UPDATE skills SET body = ") == 1
    end = block.index(TAG, block.index(TAG) + len(TAG)) + len(TAG)
    stmt = block[end:block.index(");", end) + 2]
    assert stmt.startswith(f", updated_at = now()\n WHERE name = 'dashboard-authoring' AND {GUARD}")
    shipped = [Q045_BODY_MD5, P22_18_BODY_MD5, P22_FIX_BODY_MD5, P22_REVIEW_BODY_MD5, P22_16_BODY_MD5,
               P22_COLOUR_BODY_MD5, P22_WEEKLY_BODY_MD5]
    assert re.findall(r"'([0-9a-f]{32})'", stmt) == shipped
    assert _md5(_text()) not in shipped, "the new body is not in its own guard"
    assert "SET enabled" not in block and "enabled =" not in block and "INSERT" not in block
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER|GRANT)\b", block)


def test_blocks_come_after_the_22_02_block_in_order():
    after = SCHEMA.index("-- End Phase 22: dashboard capabilities")
    assert after < SCHEMA.index(START) < SCHEMA.index(END)


def test_specialist_prompt_mentions_spec_v2_and_recipes():
    from app.agents import graph
    p = graph.DEFAULT_PROMPTS["dashboard"]
    i = p.index("SPEC V2 (Phase 22)")
    line = p[i:].split("\n", 1)[0]
    assert len(line) <= 300 and "spec_version 2" in line and 'part="recipes/<name>.md"' in line


def test_movement_asks_find_in_and_out_measures():
    """22-16: a name search for 'stock' found only stock_kg and the agent said there was no in or out."""
    t = _text()
    assert "Call `get_model` with no arguments first" in t
    assert "A movement ask" in t and "in and out measures" in t


def test_receivables_recipe_keeps_gold_off_table_columns_and_the_page_governed():
    """22-16: request 2 put owed_when on a raw-SQL table column and built ungoverned tiles."""
    r = (SKILL.parent / "recipes" / "receivables.md").read_text(encoding="utf-8")
    assert "never on a table column" in r and "alone" in r and len(r.encode()) <= 6000


def test_colour_ask_says_why_red_is_kept_for_bad_news():
    """Release eval request 12 run 2: the reply said there is no colour setting and used the
    outward tone but never said why red is not used."""
    t = _text()
    sec = t[t.index("## Tones and colour"):t.index("## Numbers, dates and titles")]
    line = next(x for x in sec.splitlines() if "make the outward bars red" in x)
    assert "no colour field" in line and "red is kept for bad news and problems" in line
    assert "outward tone" in line and len(line) <= 300


def test_weekly_last_year_ask_is_a_governed_ghost_under_the_control():
    """Release eval request 9 runs 2 and 3: "Orders and value by week, compared with last year"
    became raw-SQL same-ISO-week overlays with no date control and no ghost."""
    t = _text()
    sec = t[t.index("## Date control"):t.index("## Measures first")]
    line = next(x for x in sec.splitlines() if 'A "by week, compared with last year" ask' in x)
    for phrase in ('`date: "control"`', '`ghost: "last_year"`', "governed measures", "`last_90_days`",
                   "`horizon_days` 732", "never build a raw-SQL"):
        assert phrase in line, phrase
    r = (SKILL.parent / "recipes" / "b2b-order-book.md").read_text(encoding="utf-8")
    assert "never add a raw-SQL" in r and len(r.encode()) <= 6000
    raw = json_blocks(r)[0]
    assert "queries" not in raw
    lines = [x for x in raw["tiles"] if x["type"] in ("line", "area", "bar")]
    assert lines and all(x.get("ghost") == "last_year" and x.get("date") == "control"
                         and x.get("measure") for x in lines)


def test_date_control_viewer_features_are_stated():
    """Quick 052: the agent twice told an owner a custom range and tap-a-day were not possible."""
    t = _text()
    sec = t[t.index("## Date control"):t.index("## Measures first")]
    line = next(x for x in sec.splitlines() if "Custom range" in x)
    for phrase in ("from and to", "Tapping a day", "chip that clears it", "Never tell the owner"):
        assert phrase in line, phrase
