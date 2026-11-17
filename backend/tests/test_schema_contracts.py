"""schema.sql contracts for the eager-document / case-chaining feature.

Text-level pins over the file db.init_schema executes on EVERY boot: the events
source CHECKs (both the inline CREATE TABLE list and the drop/re-add migration)
admit 'docintel'; documents carries the eager-extraction lifecycle columns;
cases carries the chaining link; and a UNIQUE partial index prevents two live
(pending/extracting) rows for the same sha256 racing into the queue."""

import re
from pathlib import Path

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8")


def test_every_events_source_check_includes_docintel():
    # Matches the inline CREATE TABLE CHECK and the re-add migration alike.
    lists = re.findall(r"\bsource\s+IN\s*\(([^)]*)\)", SCHEMA)
    assert len(lists) >= 2, \
        "expected BOTH the inline events CHECK and the events_source_check re-add"
    for lst in lists:
        assert "'docintel'" in lst, \
            f"a source CHECK list is missing 'docintel': ({lst.strip()})"


def test_events_source_check_migration_is_drop_then_readd():
    # Existing databases only get new source values via the drop/re-add pair.
    assert re.search(r"DROP CONSTRAINT IF EXISTS events_source_check", SCHEMA)
    m = re.search(
        r"ADD CONSTRAINT events_source_check\s+CHECK\s*\(\s*source\s+IN\s*\(([^)]*)\)",
        SCHEMA)
    assert m is not None
    assert "'docintel'" in m.group(1)


def test_documents_eager_lifecycle_columns_are_additive():
    # error/attempts/claimed_at drive the pending→extracting→failed lifecycle;
    # they must be ADD COLUMN IF NOT EXISTS so agentctl update applies them.
    for col in ("error", "attempts", "claimed_at"):
        assert re.search(
            rf"ALTER TABLE documents ADD COLUMN IF NOT EXISTS\s+{col}\b",
            SCHEMA), f"documents.{col} migration missing"


def test_cases_parent_case_id_is_additive():
    assert re.search(
        r"ALTER TABLE cases ADD COLUMN IF NOT EXISTS\s+parent_case_id\b", SCHEMA)


def test_unique_partial_index_on_live_document_sha():
    """Two concurrent eager intakes of the same bytes must not both queue an
    extraction: a UNIQUE index over sha256 restricted to the live statuses is
    the race guard (loose regex — formatting/DO-block wrapping is free)."""
    m = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?idx_documents_sha_live\s+"
        r"ON\s+documents\s*(?:USING\s+\w+\s*)?\(\s*sha256\s*\)"
        r"[^;]*WHERE[^;]*status\s+IN\s*\(\s*'pending'\s*,\s*'extracting'\s*\)",
        SCHEMA, re.IGNORECASE | re.DOTALL)
    assert m, ("expected UNIQUE partial index idx_documents_sha_live on "
               "documents(sha256) WHERE status IN ('pending','extracting')")


# ── Case aliases + supersedable facts ─────────────────────────────────────

def test_cases_aliases_is_additive_nonnull_array():
    # Existing client DBs only gain the column via ADD COLUMN IF NOT EXISTS;
    # NOT NULL DEFAULT '{}' so array_append/cardinality never meet a NULL.
    assert re.search(
        r"ALTER TABLE cases ADD COLUMN IF NOT EXISTS\s+aliases\s+TEXT\[\]"
        r"\s+NOT\s+NULL\s+DEFAULT\s+'\{\}'", SCHEMA), \
        "cases.aliases migration missing or not NOT NULL DEFAULT '{}'"


def test_case_facts_table_shape():
    m = re.search(r"CREATE TABLE IF NOT EXISTS case_facts\s*\(([^;]*)\);",
                  SCHEMA, re.DOTALL)
    assert m, "case_facts table missing"
    body = m.group(1)
    # cascade with the case: facts are meaningless without their case
    assert re.search(r"case_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+cases\s*\(id\)"
                     r"\s+ON\s+DELETE\s+CASCADE", body)
    # the supersession pointer is self-referential — latest-wins WITH history
    assert re.search(r"superseded_by\s+BIGINT\s+REFERENCES\s+case_facts\s*\(id\)",
                     body)
    assert re.search(r"source_event_id\s+BIGINT\s+REFERENCES\s+events\s*\(id\)",
                     body), "fact provenance (which event said it) missing"
    assert re.search(r"observed_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\(\)",
                     body)


def test_case_facts_current_partial_index():
    """Every current-facts read filters superseded_by IS NULL — the partial
    index keeps that cheap no matter how long a correction history grows."""
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_case_facts_current\s+"
        r"ON\s+case_facts\s*\(\s*case_id\s*,\s*field\s*\)"
        r"[^;]*WHERE[^;]*superseded_by\s+IS\s+NULL",
        SCHEMA, re.IGNORECASE | re.DOTALL), \
        "partial index idx_case_facts_current (WHERE superseded_by IS NULL) missing"


# ── Sandbox isolation (test-thread quarantine) ────────────────────────────

def test_skills_table_shape():
    """Agent Skills: body TEXT is the runtime source of truth (the on-disk
    library is only the install source and may be absent), name is the UNIQUE
    slug handle every route/tool keys on, and enabled defaults FALSE —
    installing is not consent, mirroring the chats.py approval invariant."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS skills\s*\(([^;]*)\);",
                  SCHEMA, re.DOTALL)
    assert m, "skills table missing"
    body = m.group(1)
    assert re.search(r"name\s+TEXT\s+NOT\s+NULL\s+UNIQUE", body), \
        "skills.name must be TEXT NOT NULL UNIQUE (the slug is the identity)"
    assert re.search(r"enabled\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+FALSE",
                     body, re.IGNORECASE), \
        "skills.enabled must default FALSE — a skill never reaches a prompt by existing"
    assert re.search(r"body\s+TEXT\s+NOT\s+NULL", body), \
        "skills.body (the SKILL.md text) is the runtime source of truth"
    # scope arrives as an additive migration with a safe default: existing
    # rows must become 'agent' (supervisor index), never triage policies.
    assert re.search(
        r"ALTER TABLE skills ADD COLUMN IF NOT EXISTS scope\s+TEXT\s+"
        r"NOT\s+NULL\s+DEFAULT\s+'agent'", SCHEMA), \
        "skills.scope must be additive with DEFAULT 'agent'"


def test_cases_sandbox_flag_is_additive_boolean_default_false():
    """Cases born from test threads carry sandbox=TRUE so E2E traffic can
    never touch real cases. Additive so agentctl update applies it on client
    VMs; NOT NULL DEFAULT FALSE so every pre-existing case is a real case and
    NOT c.sandbox / sandbox = $n predicates never meet a NULL."""
    assert re.search(
        r"ALTER TABLE cases ADD COLUMN IF NOT EXISTS\s+sandbox\s+BOOLEAN"
        r"\s+NOT\s+NULL\s+DEFAULT\s+FALSE",
        SCHEMA, re.IGNORECASE), \
        "cases.sandbox migration missing or not NOT NULL DEFAULT FALSE"


# ── Phase 5: KPI history (metric_history) ──

def _metric_history_body() -> str:
    m = re.search(r"CREATE TABLE IF NOT EXISTS metric_history\s*\((.*?)\n\);", SCHEMA, re.S)
    assert m, "metric_history CREATE TABLE missing"
    return m.group(1)


def test_metric_history_table_shape():
    body = _metric_history_body()
    for pat in (
        r"id\s+BIGSERIAL PRIMARY KEY",
        r"watch_id\s+BIGINT NOT NULL REFERENCES watches\(id\) ON DELETE CASCADE",
        r"observed_at\s+TIMESTAMPTZ NOT NULL DEFAULT now\(\)",
        r"metric\s+TEXT",
        r"value\s+DOUBLE PRECISION",
        r"observation\s+JSONB NOT NULL DEFAULT '\{\}'::jsonb",
    ):
        assert re.search(pat, body), f"metric_history missing: {pat}"


def test_metric_history_index_is_idempotent():
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS idx_metric_history_watch_time\s+ON metric_history"
        r"\s*\(watch_id, observed_at DESC\)", SCHEMA)


def test_metric_history_block_sits_after_watch_events():
    we = SCHEMA.index("CREATE TABLE IF NOT EXISTS watch_events")
    mh = SCHEMA.index("CREATE TABLE IF NOT EXISTS metric_history")
    art = SCHEMA.index("CREATE TABLE IF NOT EXISTS artifacts")
    assert we < mh < art
    assert SCHEMA.count("-- ── Phase 5: KPI history") == 1
    assert SCHEMA.count("-- End Phase 5: KPI history") == 1
    end = SCHEMA.index("-- End Phase 5: KPI history")
    assert SCHEMA.index("idx_metric_history_watch_time") < end < art


def test_phase14_task_runs_kind_widening_block():
    assert "-- Phase 14: WH-07" in SCHEMA
    assert "task_runs_kind_check" in SCHEMA
    assert "'user','playbook','sync'" in SCHEMA
    assert SCHEMA.index("-- Phase 14: WH-07") > SCHEMA.index("-- End Phase 12 (CTX-08)")
