"""Phase 3 (DASH-01) schema.sql contract; pure text, no DB.

Kept out of test_schema_contracts.py so parallel phases do not conflict.
"""
import re
from pathlib import Path

import pytest

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8")
START = "-- Phase 3: Dashboard storage"
END = "-- end Phase 3"


def _block() -> str:
    assert SCHEMA.count(START) == 1 and SCHEMA.count(END) == 1
    return SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]


@pytest.fixture(scope="module")
def block() -> str:
    return _block()


def test_phase3_block_is_delimited_once():
    assert SCHEMA.count(START) == 1
    assert SCHEMA.count(END) == 1
    assert SCHEMA.index(START) > SCHEMA.index("ALTER TABLE skills ADD COLUMN IF NOT EXISTS scope")
    assert SCHEMA.index(END) > SCHEMA.index(START)


def test_three_tables_created_idempotently(block):
    for t in ("dashboards", "dashboard_versions", "dashboard_snapshots"):
        assert f"CREATE TABLE IF NOT EXISTS {t} " in block or f"CREATE TABLE IF NOT EXISTS {t}(" in block
    assert not re.search(r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", block)


def test_versions_columns(block):
    pats = [
        r"octet_length\(code\)\s*<=\s*262144",
        r"script_hashes\s+TEXT\[\]\s+NOT\s+NULL",
        r"queries\s+JSONB\s+NOT\s+NULL",
        r"jsonb_typeof\(queries\)\s*=\s*'array'",
        r"status\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'draft'\s+CHECK\s*\(\s*status\s+IN\s*\(\s*'draft'\s*,\s*'published'\s*,\s*'retired'\s*\)\s*\)",
        r"last_error\s+TEXT",
        r"author\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'agent'\s+CHECK\s*\(\s*author\s+IN\s*\(\s*'agent'\s*,\s*'operator'\s*\)\s*\)",
        r"UNIQUE\s*\(dashboard_id,\s*version\)",
        r"UNIQUE\s*\(dashboard_id,\s*id\)",
    ]
    for p in pats:
        assert re.search(p, block), p


def test_task_run_id_has_no_fk(block):
    lines = [ln for ln in block.splitlines() if re.match(r"\s*task_run_id\b", ln)]
    assert len(lines) == 1
    assert "REFERENCES" not in lines[0].upper()


def test_one_published_partial_unique_index(block):
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_dashboard_versions_one_published\s+"
        r"ON\s+dashboard_versions\s*\(dashboard_id\)\s+WHERE\s+status\s*=\s*'published'",
        block,
    )


def test_pointer_fk_is_guarded(block):
    assert "dashboards_current_version_fk" in block
    assert "FROM pg_constraint WHERE conname = 'dashboards_current_version_fk'" in block
    assert re.search(
        r"FOREIGN\s+KEY\s+\(id,\s*current_version_id\)\s+REFERENCES\s+dashboard_versions\s*\(dashboard_id,\s*id\)",
        block,
    )


def test_immutability_trigger_is_guarded(block):
    assert "CREATE OR REPLACE FUNCTION dashboard_versions_immutable()" in block
    assert "trg_dashboard_versions_immutable" in block
    assert "FROM pg_trigger" in block
    assert "BEFORE UPDATE ON dashboard_versions" in block
    compared = block[block.index("IF ("):block.index("IS DISTINCT FROM")]
    for col in ("code", "queries", "script_hashes", "title", "change_note"):
        assert f"NEW.{col}" in compared
    for col in ("status", "last_error", "published_at", "retired_at"):
        assert col not in compared


def test_snapshots_shape(block):
    assert re.search(r"PRIMARY\s+KEY\s+\(version_id,\s*query_id\)", block)
    assert re.search(r"refresh_minutes\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+15", block)
    assert "BETWEEN 5 AND 1440" in block
    assert re.search(r"\bdata\s+JSONB", block)
    assert re.search(r"\bcolumns\s+JSONB", block)
    assert "refreshed_at" in block
    assert re.search(r"\berror\s+TEXT", block)
    assert "next_refresh_at" in block
    assert "CREATE INDEX IF NOT EXISTS idx_dashboard_snapshots_due" in block


def test_block_is_additive_only(block):
    assert "DROP " not in block.upper().replace("DROPS", "")
    assert "metric_history" not in block
