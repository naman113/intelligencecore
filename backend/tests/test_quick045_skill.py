"""Quick 045 (skill): dashboard-authoring states what is not possible first, prefers governed
measures, defines the last_days window and marks sections. Every body copy in schema.sql equals
SKILL.md and an md5-guarded refresh carries it to installs that still hold the Quick 036 body
(owner-edited copies and the enabled flag are never touched). No DB."""

import hashlib
import re
from pathlib import Path

from app import dashboardspec, skillservice

SKILL = skillservice.library_root() / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")
START = "-- ── Quick 045: dashboard-authoring states limits first, measures first, window meaning, sections ──"
END = "-- End Quick 045: dashboard-authoring states limits first"
TAG = "$q045_dashboard_authoring$"
Q036_BODY_MD5 = "2e615e7554ad7e1bcb790d423ffc03f3"  # what Quick 036 shipped
COPIES = ("$skill_dashboard_authoring$", "$q004_dashboard_authoring$",
          "$p131_dashboard_authoring$", "$p18_dashboard_authoring$",
          "$q036_dashboard_authoring$", TAG)
GUARD = "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN ("


def _text() -> str:
    return SKILL.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _between(s: str, tag: str) -> str:
    assert s.count(tag) == 2, f"{tag} must occur exactly twice"
    a = s.index(tag) + len(tag)
    return s[a:s.index(tag, a)]


def test_guidance_is_present_and_matches_the_spec_limits():
    t = _text()
    assert t.isascii() and len(t) < 19000
    # Phase 22: spec v2 limits (the v1 limits of 24 tiles and 6 charts moved to 40 and 12)
    sec = t[t.index("## Say what is not possible"):t.index("## Spec v2 in brief")]
    for phrase in ("Colours", "40 tiles", "12 chart tiles",
                   "Two value axes", "50 values"):
        assert phrase in sec or phrase.lower() in sec.lower()
    # cross-filtering ships in spec v2 (DCAP-11): the skill must not call it impossible
    assert "Click-to-filter" not in sec and "Click-to-filter: declare a page filter" in t
    # the limits the skill names are the ones the spec enforces
    assert dashboardspec.MAX_TILES_V2 == 40 and dashboardspec.MAX_CHARTS_V2 == 12
    assert "exactly N calendar days ending today" in t
    assert "`current_date - 6` to `current_date`" in t
    assert "Use raw SQL only when no measure" in t and "`measures`" in t
    assert "`sections`" in t


def test_every_body_copy_equals_the_file():
    for tag in COPIES:
        assert _between(SCHEMA, tag).strip() == _text().strip(), tag


def test_refresh_is_guarded_on_the_quick036_body_and_never_enables():
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    block = SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]
    assert block.count("UPDATE skills SET body = ") == 1
    end = block.index(TAG, block.index(TAG) + len(TAG)) + len(TAG)
    stmt = block[end:block.index(");", end) + 2]
    assert stmt.startswith(f", updated_at = now()\n WHERE name = 'dashboard-authoring' AND {GUARD}")
    assert re.findall(r"'([0-9a-f]{32})'", stmt) == [Q036_BODY_MD5]
    assert _md5(_text()) != Q036_BODY_MD5, "the new body is not in its own guard"
    assert "SET enabled" not in block and "enabled =" not in block and "INSERT" not in block
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER|GRANT)\b", block)
    assert SCHEMA.index(START) > SCHEMA.index("-- End Quick 036: dashboard-authoring saves by preview_id")
