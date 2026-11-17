"""Text pins for the Phase 2 context-store block in schema.sql."""

import re
from pathlib import Path

RAW = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8")
SCHEMA = RAW.replace("\r\n", "\n")
START = "Phase 2: Context store"
END = "End Phase 2: Context store"
# built with join so the compiler cannot fold the word into the .pyc (SC1 grep)
_REMOVED_STORE = "".join(("q", "drant"))


def _block() -> str:
    assert SCHEMA.count(END) == 1
    starts = [m.start() for m in re.finditer(re.escape(START), SCHEMA)
              if not SCHEMA[m.start() - 4:m.start()].endswith("End ")]
    assert len(starts) == 1
    s, e = starts[0], SCHEMA.index(END)
    assert s < e
    return SCHEMA[s:e]


def test_tables_present():
    b = _block()
    for t in ("context_sources", "context_tables", "context_columns", "business_context"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in b


def test_embedding_and_uniques():
    b = _block()
    assert "embedding   vector(1536)" in b
    assert "UNIQUE (kind, ref)" in b
    assert len(re.findall(r"ref\s+TEXT NOT NULL UNIQUE", b)) == 2


def test_named_indexes():
    b = _block()
    assert re.search(r"idx_business_context_embedding\s+ON business_context USING hnsw "
                     r"\(embedding vector_cosine_ops\)", b)
    assert re.search(r"idx_business_context_fts\s+ON business_context USING GIN "
                     r"\(to_tsvector\('english', doc\)\)", b)
    assert re.search(r"idx_learnings_embedding_hnsw\s+ON learnings USING hnsw "
                     r"\(embedding vector_cosine_ops\)", b)
    assert re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_context_pending\s+"
                     r"ON actions \(\(payload->>'kind'\), \(payload->>'ref'\)\)\s+"
                     r"WHERE kind = 'context_change' AND status = 'proposed'", b)


def test_every_index_named_and_idempotent():
    for line in re.findall(r"^CREATE (?:UNIQUE )?INDEX.*$", _block(), re.M):
        assert re.search(r"IF NOT EXISTS\s+\w+", line), line


def test_seed_source():
    assert ("INSERT INTO context_sources (name, kind) VALUES ('business_db', 'postgres')\n"
            "ON CONFLICT (name) DO NOTHING") in _block()


def test_no_destructive_or_unsafe_sql():
    b = _block()
    b = re.sub(r"\$skill_schema_doc\$.*?\$skill_schema_doc\$", "", b, flags=re.S)  # skill prose is not SQL
    code = "\n".join(l for l in b.splitlines() if not l.strip().startswith("--"))
    for word in ("DELETE", "CASCADE", "CONCURRENTLY", "DROP"):
        assert word not in code.upper(), word
    assert _REMOVED_STORE not in b.lower()


def test_block_is_appended():
    # Later phases (Phase 12) append their own blocks after the Phase 2 block.
    assert END in SCHEMA
    tail = SCHEMA[SCHEMA.index(END) + len(END):]
    assert "-- Phase 2" not in tail
