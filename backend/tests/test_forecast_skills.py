"""Phase 13.1 pins: forecasting and dashboard-authoring teach forecast definitions,
and schema.sql ships the new bodies safely (md5-guarded, never enabling)."""

import hashlib
import re
from pathlib import Path

from app import skillservice

ROOT = Path(__file__).resolve().parents[2]
LIB = skillservice.library_root() / "intelligence-corner"
SCHEMA = (ROOT / "backend" / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
START = "-- ── Phase 13.1: forecast definitions"
END = "-- End Phase 13.1: forecast definitions"
BLOCK = SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]
FC_OLD = "4ba0bf4f377af4c3026ff9bf5e8d30b5"
DA_OLD = ["7a249efa72db9a28f4be85794b482aaf", "03a347e1ccb572d5ce05e53ceb71c6f7",
          "3e5ed7b8074dc1a03933be4d38f92dc0", "4630fbcc8c5fd4efc4658383482213ae",
          "6ec8e36be097482bdb50f18e902bf419", "98864baa0bcd815ed97b27afbc9247c9",
          "e7e7a00aaf2c650ad39cf4648d420983"]
REFRESH = {"forecasting": ("$p131_forecasting$", [FC_OLD]),
           "dashboard-authoring": ("$p131_dashboard_authoring$", DA_OLD)}


def _text(slug: str) -> str:
    return (LIB / slug / "SKILL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")


def _md5(t: str) -> str:
    return hashlib.md5(t.replace("\r", "").strip(" \n\t").encode()).hexdigest()


def _between(s: str, tag: str) -> str:
    a = s.index(tag) + len(tag)
    return s[a:s.index(tag, a)]


def test_forecasting_teaches_the_definition_path():
    raw = (LIB / "forecasting" / "SKILL.md").read_bytes()
    assert len(raw) < 14000 and raw.isascii()  # Quick 030: plus the model data-access section
    t = raw.decode()
    sec = t[t.index("## Every night: a forecast definition"):t.index("## Reporting")]
    for term in ("propose_forecast", "get_forecast", "exactly period", "proposed forecast (action #",
                 "Approvals", "never schedule a recurring agent run", "draft"):
        assert term in sec, term
    assert "every night" in t[t.index("## Who runs what"):t.index("## Budget")]
    assert "$p131_" not in t


def test_forecasting_dashboard_section_uses_the_definition_id():
    t = _text("forecasting")
    sec = t[t.index("## Dashboard forecast panel"):]
    assert "dashboard-authoring" in sec and "cannot run Python" in sec
    assert "get_forecast" in sec and "forecast source" in sec


def test_dashboard_authoring_declares_the_forecast_source():
    t = _text("dashboard-authoring")
    assert len(t) < 19000 and "$p131_" not in t
    sec = t[t.index("## Forecast panel"):]
    assert '{"id": "fc", "source": "forecast", "forecast_id": N, "refresh_minutes": 60}' in sec
    assert "period, actual, forecast, low, high, method, wape, as_of" in sec
    assert "get_forecast" in sec
    assert "```sql" not in t
    ref = (LIB / "dashboard-authoring" / "reference" / "forecast-panel.md").read_text(encoding="utf-8")
    assert ref.count("```sql") == 1


def test_refreshes_are_guarded_equal_the_files_and_never_enable():
    assert "SET enabled" not in BLOCK and "enabled = TRUE" not in BLOCK and ", TRUE)" not in BLOCK
    assert BLOCK.count("UPDATE skills SET ") == 2
    for slug, (tag, old) in REFRESH.items():
        assert SCHEMA.count(tag) == 2
        head = f"UPDATE skills SET body = {tag}"
        b = BLOCK.index(tag, BLOCK.index(head) + len(head)) + len(tag)
        stmt = BLOCK[b:BLOCK.index(");", b) + 2]
        assert stmt.startswith(f", updated_at = now()\n WHERE name = '{slug}' AND ")
        assert "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN (" in stmt
        assert re.findall(r"'([0-9a-f]{32})'", stmt) == old
        assert _md5(_text(slug)) not in old
        assert _between(BLOCK, tag).strip() == _text(slug).strip()


def test_seed_bodies_equal_the_files():
    assert _between(SCHEMA, "$skill_forecasting$").strip() == _text("forecasting").strip()
    for tag in ("$skill_dashboard_authoring$", "$q004_dashboard_authoring$"):
        assert _between(SCHEMA, tag).strip() == _text("dashboard-authoring").strip()


# -- Quick 013: the forecasting body reaches installs that still carry the 13.1 body --------

Q013_START = "-- ── Quick 013: forecast follow-ups ──"
Q013_END = "-- End Quick 013: forecast follow-ups"
FC_131 = "50bebd46684a72ff2e6dccc2cee319d7"   # the body Phase 13.1 shipped


def test_quick013_refresh_is_guarded_on_the_131_body_and_never_enables():
    block = SCHEMA[SCHEMA.index(Q013_START):SCHEMA.index(Q013_END)]
    assert SCHEMA.index(Q013_START) > SCHEMA.index(END)        # after the 13.1 refresh
    assert "SET enabled" not in block and "enabled = TRUE" not in block and "INSERT" not in block
    assert block.count("UPDATE skills SET ") == 1 and SCHEMA.count("$q013_forecasting$") == 2
    head = "UPDATE skills SET body = $q013_forecasting$"
    b = block.index("$q013_forecasting$", block.index(head) + len(head)) + len("$q013_forecasting$")
    stmt = block[b:block.index(");", b) + 2]
    assert stmt.startswith(", updated_at = now()\n WHERE name = 'forecasting' AND ")
    assert "md5(btrim(replace(body, chr(13), ''), ' ' || chr(10) || chr(9))) IN (" in stmt
    assert re.findall(r"'([0-9a-f]{32})'", stmt) == [FC_131]
    assert _md5(_text("forecasting")) not in (FC_131, FC_OLD)
    assert _between(block, "$q013_forecasting$").strip() == _text("forecasting").strip()


def test_nightly_step_teaches_cast_and_tail():
    t = _text("forecasting")
    sec = t[t.index("## Every night: a forecast definition"):t.index("## Reporting")]
    step3 = next(ln for ln in sec.splitlines() if ln.startswith("3. "))
    for term in ("::date", "no date window", "newest periods it needs", "count as 0"):
        assert term in step3, term
