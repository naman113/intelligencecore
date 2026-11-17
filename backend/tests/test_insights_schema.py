"""Phase 7 schema block pins.

The block must sit before the Phase 2 banner because a Phase 2 test requires
schema.sql to end with the Phase 2 block."""

import re
from pathlib import Path

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")

HEAD = "-- ── Phase 7: Exploration and insights ──"
END = "-- End Phase 7: Exploration and insights"
P2_BANNER = "Phase 2: Context store - business context on pgvector"


def _block() -> str:
    i = SCHEMA.index(HEAD)
    j = SCHEMA.index(END) + len(END)
    return SCHEMA[i:j]


def test_block_present_once():
    assert SCHEMA.count(HEAD) == 1
    assert SCHEMA.count(END) == 1
    assert SCHEMA.index(HEAD) < SCHEMA.index(END)


def test_block_position():
    assert SCHEMA.index(HEAD) > SCHEMA.index("-- end Phase 3")
    assert SCHEMA.index(END) < SCHEMA.index(P2_BANNER)
    # Phase 12 appends its own blocks after the Phase 2 block.
    m = "-- End Phase 2: Context store"
    assert m in SCHEMA
    assert "-- Phase 2" not in SCHEMA[SCHEMA.index(m) + len(m):]


def test_block_ddl():
    b = _block()
    assert "ALTER TABLE watches ADD COLUMN IF NOT EXISTS origin TEXT;" in b
    assert "ALTER TABLE watches ADD COLUMN IF NOT EXISTS origin_ref TEXT;" in b
    assert ("CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_origin_ref ON watches "
            "(origin_ref) WHERE origin_ref IS NOT NULL;") in b
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_create_watch_pending" in b
    assert "((payload->>'title_key'))" in b
    assert "WHERE kind = 'create_watch' AND status = 'proposed'" in b
    assert ("CREATE INDEX IF NOT EXISTS idx_case_events_exploration ON case_events "
            "(event_id) WHERE note = 'exploration';") in b


def _code(b: str) -> str:
    return "\n".join(ln for ln in b.splitlines() if not ln.strip().startswith("--"))


def test_block_is_additive():
    code = _code(_block())
    for bad in ("DROP", "DELETE", "TRUNCATE", "CASCADE", "RENAME", "ALTER COLUMN"):
        assert bad not in code.upper(), bad
    for m in re.finditer(r"CREATE\s+(UNIQUE\s+)?(INDEX|TABLE)\b[^;]*", code, re.I):
        assert "IF NOT EXISTS" in m.group(0).upper()
    for ln in code.splitlines():
        if ln.upper().startswith("ALTER TABLE"):
            assert "ADD COLUMN IF NOT EXISTS" in ln.upper()


def test_block_markers_do_not_collide():
    b = _block()
    assert "Phase 2: Context store" not in b
    assert "Phase 5: dashboard-authoring skill" not in b
