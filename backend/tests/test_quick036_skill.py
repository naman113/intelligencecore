"""Quick 036 (skill): dashboard-authoring names the enforced preview limit and saves by
preview_id. Every copy of the body in schema.sql equals SKILL.md, and an md5-guarded
refresh carries it to installs that still hold the Phase 18 body (owner-edited copies
and the enabled flag are never touched). No DB."""

import hashlib
import re
from pathlib import Path

from app import skillservice

SKILL = skillservice.library_root() / "intelligence-corner" / "dashboard-authoring" / "SKILL.md"
SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")
START = "-- ── Quick 036: dashboard-authoring saves by preview_id ──"
END = "-- End Quick 036: dashboard-authoring saves by preview_id"
TAG = "$q036_dashboard_authoring$"
P18_BODY_MD5 = "abe006bccdb202e6060fcf25c630f1c3"  # what Phase 18 shipped (milestone-2 f82e715)
COPIES = ("$skill_dashboard_authoring$", "$q004_dashboard_authoring$",
          "$p131_dashboard_authoring$", "$p18_dashboard_authoring$", TAG)
GUARD = "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN ("


def _text() -> str:
    return SKILL.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _between(s: str, tag: str) -> str:
    assert s.count(tag) == 2, f"{tag} must occur exactly twice"
    a = s.index(tag) + len(tag)
    return s[a:s.index(tag, a)]


def test_steps_name_the_preview_handle_and_the_enforced_limit():
    t = _text()
    # Phase 22: the write-and-preview step is 6 and the save step is 7
    step6 = next(ln for ln in t.splitlines() if ln.startswith("6. "))
    step7 = next(ln for ln in t.splitlines() if ln.startswith("7. "))
    assert "at most 3 preview rounds" in step6 and "refuses" in step6
    assert "save_dashboard_version(slug, change_note, preview_id)" in step7
    assert "pass the `preview_id` of your last preview" in step7
    assert "send `spec` instead only if" in step7
    assert t.isascii()


def test_every_body_copy_equals_the_file():
    for tag in COPIES:
        assert _between(SCHEMA, tag).strip() == _text().strip(), tag


def test_refresh_is_guarded_on_the_phase18_body_and_never_enables():
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    block = SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]
    assert block.count("UPDATE skills SET body = ") == 1
    end = block.index(TAG, block.index(TAG) + len(TAG)) + len(TAG)
    stmt = block[end:block.index(");", end) + 2]
    assert stmt.startswith(f", updated_at = now()\n WHERE name = 'dashboard-authoring' AND {GUARD}")
    assert re.findall(r"'([0-9a-f]{32})'", stmt) == [P18_BODY_MD5]
    assert _md5(_text()) != P18_BODY_MD5, "the new body is not in its own guard"
    assert "SET enabled" not in block and "enabled =" not in block and "INSERT" not in block
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER|GRANT)\b", block)
    assert SCHEMA.index(START) > SCHEMA.index("-- End Phase 18: chart-spec dashboards")
