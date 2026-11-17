"""The dashboard-authoring skill's spec example parses, and the contract's legacy HTML
example still lints clean (the HTML linter stays for legacy versions, Phase 18)."""

import json
import re
from pathlib import Path

from app import dashboardlint as dl
from app import dashboardspec

APP_ROOT = Path(__file__).resolve().parents[2]
SKILL = APP_ROOT / "skills_library" / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
CONTRACT = APP_ROOT / "docs" / "DASHBOARD_CONTRACT.md"


def _fenced(text, lang, nth=0):
    ms = re.findall(r"```" + re.escape(lang) + r"[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    assert len(ms) > nth, f"no fenced {lang} block"
    return ms[nth]


def _skill():
    return SKILL.read_text(encoding="utf-8")


def test_contract_legacy_html_example_lints_clean():
    t = CONTRACT.read_text(encoding="utf-8")
    res = dl.lint(_fenced(t, "html"), json.loads(_fenced(t, "json")))
    assert res["errors"] == [], res
    rules = {w["rule"] for w in res["warnings"]}
    assert not rules & {"unused_query", "dynamic_ids"}, rules


def test_skill_example_spec_parses_and_matches_the_contract_example():
    # Phase 22: the skill example is spec v2; the contract's v1 example still parses
    spec = json.loads(_fenced(_skill(), "json"))
    assert dashboardspec.parse(spec).spec_version == 2
    c = CONTRACT.read_text(encoding="utf-8")
    v1 = json.loads(c[c.index("## Spec example"):].split("```json\n", 1)[1].split("```", 1)[0])
    assert dashboardspec.parse(v1).spec_version == 1


def test_skill_teaches_the_spec_rules():
    s = _skill()
    low = s.lower()
    for needle in ("kpi_history", "watch_id", "unique snake_case aliases", "plain text",
                   "never write html", "dashed", "band", "pick: \"last_complete\""):
        assert needle in low or needle in s, needle
    for needle in ("innerHTML", "location.search", "Chart.js"):
        assert needle not in s, needle


def test_skill_size_and_ascii():
    s = _skill()
    assert len(s) < 19000 and s.isascii()
