"""Pins for the first-party dashboard-authoring skill (the house style the dashboard
agent follows).

The skill is data, seeded installed-but-disabled by a Phase 5 schema.sql block
(D-P5-03, same mechanism as Phase 2's schema-documentation). It must fit
use_skill's 20,000-char window. Phase 18 rewrote it for chart specs: its example is
a complete spec that dashboardspec.parse accepts, and Phase 18 md5-gates a refresh of
installs that still carry the Phase 13.1 body."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import app.main as main
from app import skillservice
from app.agents import graph
from app import dashboardspec

APP_ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root()
ENTRY_ID = "intelligence-corner/dashboard-authoring"
SKILL = LIB / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8")
TAG = "$skill_dashboard_authoring$"
BUDGET = 19000
P18_START = "-- Phase 18 (SPEC-04): dashboard-authoring rewritten"
P18_END = "-- End Phase 18: chart-spec dashboards"
P131_MD5 = "ecefeb36f1cdf200a5ac07bdfac95037"
HEADER = "-- ── Phase 5: dashboard-authoring skill"
END = "-- End Phase 5: dashboard-authoring skill"


def _norm(t: str) -> str:
    return t.replace(chr(13) + chr(10), chr(10)).strip()


def _text() -> str:
    assert SKILL.is_file(), f"missing {SKILL}"
    return SKILL.read_text(encoding="utf-8")


def _entry() -> dict:
    data = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    hits = [e for e in data if e["id"] == ENTRY_ID]
    assert len(hits) == 1, "manifest must hold exactly one dashboard-authoring entry"
    return hits[0]


def _fenced(text: str, lang: str) -> str:
    m = re.search(r"```" + re.escape(lang) + r"[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    assert m, f"no fenced {lang} block"
    return m.group(1)


def _seed_slice() -> str:
    assert SCHEMA.count(HEADER) == 1, "Phase 5 skill header must occur once"
    start = SCHEMA.index(HEADER)
    assert SCHEMA.count(END) == 1, "Phase 5 skill end marker must occur once"
    end = SCHEMA.index(END)
    assert end > start
    return SCHEMA[start:end]


def _seed_body() -> str:
    s = _seed_slice()
    a = s.index(TAG)
    b = s.index(TAG, a + len(TAG))
    return s[a + len(TAG):b]


def test_manifest_entry_is_first_party_and_relevant():
    e = _entry()
    assert e["name"] == "dashboard-authoring"
    assert e["source"] == "intelligence-corner"
    assert e["license"] == "Proprietary"
    assert e["compat"] == "general"
    assert e["path"] == ENTRY_ID
    assert e["relevant"] is True
    assert len(e["description"]) <= 200
    for ch in ("#", "`", "<", ">", "'"):
        assert ch not in e["description"], ch


def test_skill_md_fits_use_skill_budget():
    t = _text()
    assert len(t) < BUDGET
    assert len(t.replace("\r\n", "\n").replace("\n", "\r\n")) < 20000
    assert len(t.encode("utf-8")) <= skillservice.MAX_BODY


def test_skill_md_is_ascii_and_seed_safe():
    t = _text()
    assert t.isascii()
    assert TAG not in t


def test_frontmatter_matches_manifest():
    t = _text()
    p = skillservice.parse_skill_md(t)
    assert p["name"] == "dashboard-authoring"
    assert p["license"] == "Proprietary"
    assert p["description"] == _entry()["description"]
    assert p["body"].strip()


def test_required_sections_present():
    t = _text()
    heads = ("## When to use and workflow", "## Say what is not possible", "## Spec v2 in brief",
             "## Pick the visual", "## Date control", "## Measures first",
             "## Tones and colour", "## Numbers, dates and titles", "## Recipes",
             "## KPI history", "## Forecast panel", "## Converting", "## Example",
             "## Pre-save checklist")
    pos = [t.index(h) for h in heads]
    assert pos == sorted(pos), "sections must keep the documented order"
    assert "## File contract" not in t and "## Chart.js 4 essentials" not in t


def test_required_content_markers():
    t = _text()
    for m in ("get_model", "query_measure", "get_dashboard", "preview_dashboard",
              "save_dashboard_version", "get_kpi_history", "search_business_context",
              "last_days", "last_periods", "this_month", "NOT MODELED", "unverified",
              "m_", "refresh_minutes", "[a-z][a-z0-9_]{0,47}", "owed_when",
              "good_direction", "top", "approval", "needs_conversion", "same slug"):
        assert m in t, m
    low = t.lower()
    assert "gold means the viewer owes an action" in low


def test_teaches_every_tile_type_and_format():
    t = _text()
    for tile in dashboardspec.TILE_TYPES:
        assert f"`{tile}`" in t, tile
    for fmt in dashboardspec.FORMATS:
        assert f"`{fmt}`" in t, fmt


def test_no_html_era_content():
    t = _text()
    low = t.lower()
    for bad in ("<script", "<!doctype", "fetch(", "new chart(", "innerhtml",
                "/d/_lib/", "integrity="):
        assert bad not in low, bad
    assert not re.search(r"(?i)(?:https?|wss?|ftp)://", t)
    assert "http" not in t


def test_example_spec_parses():
    spec = json.loads(_fenced(_text(), "json"))
    out = dashboardspec.parse(spec)
    assert out is not None
    types = {tl["type"] for tl in spec["tiles"]}
    assert spec["spec_version"] == 2
    assert {"stat", "line", "ranked_bar", "table", "text"} <= types
    assert any("measure" in tl for tl in spec["tiles"])
    assert any(tl.get("top") for tl in spec["tiles"])
    assert any(tl.get("query") for tl in spec["tiles"])


def test_seed_block_position_and_shape():
    s = _seed_slice()
    assert SCHEMA.index(HEADER) > SCHEMA.index("CREATE TABLE IF NOT EXISTS skills")
    assert SCHEMA.index(END) < SCHEMA.index(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS scope")
    for need in ("INSERT INTO skills (name, description, body, source, origin_ref, license, compat, enabled)",
                 "'dashboard-authoring'", "'library'",
                 "'intelligence-corner/dashboard-authoring'", "'Proprietary'",
                 "'general'", "FALSE", "ON CONFLICT (name) DO NOTHING;"):
        assert need in s, need
    a = s.index(TAG)
    b = s.index(TAG, a + len(TAG)) + len(TAG)
    rest = s[:a] + s[b:]
    assert "DO UPDATE" not in rest.upper()
    assert "true" not in rest.lower()


def test_seed_body_matches_skill_md():
    got = _seed_body().replace("\r\n", "\n")
    want = _text().replace("\r\n", "\n")
    assert got == want, "regenerate the Phase 5 seed from SKILL.md"


def test_seed_description_matches_frontmatter():
    m = re.search(r"VALUES\s*\(\s*'dashboard-authoring',\s*'((?:[^']|'')*)'",
                  _seed_slice())
    assert m
    desc = skillservice.parse_skill_md(_text())["description"]
    assert m.group(1).replace("''", "'") == desc


class _RecPool:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {"id": 1, "name": args[0], "source": args[2], "enabled": False}


async def test_reinstall_through_real_route_is_born_disabled(monkeypatch):
    pool = _RecPool()

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    counter = {"n": 0}
    monkeypatch.setattr(graph, "reset_agents",
                        lambda: counter.__setitem__("n", counter["n"] + 1))
    out = await main.api_install_skill({"id": ENTRY_ID})
    assert out["enabled"] is False
    _sql, args = pool.calls[0]
    slug, _desc, source, origin, lic, compat, body = args
    assert (slug, source, origin, lic, compat) == (
        "dashboard-authoring", "intelligence-corner", ENTRY_ID,
        "Proprietary", "general")
    expected = skillservice.library_skill_body(skillservice.library_entry(ENTRY_ID))
    assert body == expected
    assert counter["n"] == 1


def test_library_search_finds_it():
    ids = [e["id"] for e in skillservice.search_library("dashboard-authoring")]
    assert ENTRY_ID in ids


def test_phase18_refresh_is_guarded_on_the_131_body_and_never_enables():
    assert SCHEMA.count(P18_START) == 1 and SCHEMA.count("$p18_dashboard_authoring$") == 2
    block = SCHEMA[SCHEMA.index(P18_START):SCHEMA.index(P18_END)]
    assert block.count("UPDATE skills SET ") == 1
    assert "SET enabled" not in block and "enabled =" not in block and "INSERT" not in block
    tag = "$p18_dashboard_authoring$"
    a = block.index(tag) + len(tag)
    b = block.index(tag, a)
    assert _norm(block[a:b]) == _norm(_text())
    stmt = block[b + len(tag):block.index(");", b) + 2]
    assert "WHERE name = 'dashboard-authoring' AND " in stmt
    assert "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN (" in stmt
    assert re.findall(r"'([0-9a-f]{32})'", stmt) == [P131_MD5]
    import hashlib
    cur = hashlib.md5(_text().replace(chr(13), '').strip(' ' + chr(10) + chr(9)).encode()).hexdigest()
    assert cur != P131_MD5
