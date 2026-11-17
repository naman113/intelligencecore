"""Phase 22: the schema block is additive, idempotent and leaves Phase 18's DDL alone."""
import pathlib
import subprocess

import pytest

SCHEMA = (pathlib.Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8")
OPEN = "-- ── Phase 22: dashboard capabilities"
END = "-- End Phase 22: dashboard capabilities"


def test_block_once_and_ordered():
    assert SCHEMA.count(OPEN) == 1 and SCHEMA.count(END) == 1
    assert SCHEMA.index("-- End Quick 045") < SCHEMA.index(OPEN) < SCHEMA.index(END)


def test_statements():
    block = SCHEMA[SCHEMA.index(OPEN):SCHEMA.index(END)]
    assert "ALTER TABLE dashboard_snapshots ADD COLUMN IF NOT EXISTS day_timezone TEXT;" in block
    assert "pg_get_constraintdef(oid) LIKE '%65536%'" in block
    assert "octet_length(spec::text) <= 262144" in block
    assert "jsonb_typeof(spec) = 'object' AND code = '' AND renderer IS NOT NULL" in block
    assert "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dashboard_versions_spec_shape'" in block


def _p18(text):
    a = text.index("-- ── Phase 18: chart-spec dashboards")
    b = text.index("-- Phase 18 (SPEC-04)")
    return text[a:b]


def test_phase18_ddl_unchanged():
    app_dir = pathlib.Path(__file__).resolve().parents[2]
    try:
        old = subprocess.run(["git", "show", "5ae19a6:backend/app/schema.sql"], cwd=app_dir,
                             capture_output=True, check=True, timeout=30).stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git or ref 5ae19a6 unavailable")
    assert _p18(SCHEMA) == _p18(old)
