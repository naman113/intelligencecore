"""Phase 16 (MODEL-01): text checks of the Phase 16 schema block."""

import re
from pathlib import Path

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
HEADER = "-- Phase 16: semantic model (MODEL-01, MODEL-06)"
END = "-- End Phase 16: semantic model"


def _block() -> str:
    assert SCHEMA.count(HEADER) == 1 and SCHEMA.count(END) == 1
    assert SCHEMA.index(HEADER) > SCHEMA.index("-- End Phase 14: WH-07")
    assert SCHEMA.index(END) > SCHEMA.index(HEADER)
    return SCHEMA[SCHEMA.index(HEADER):SCHEMA.index(END)]


def test_four_tables_once():
    b = _block()
    for t in ("model_tables", "model_relationships", "model_measures", "model_indexes"):
        assert b.count(f"CREATE TABLE IF NOT EXISTS {t} (") == 1
    assert "INSERT INTO model_" not in SCHEMA


def test_no_destructive_statements():
    b = _block()
    for w in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert w not in b.upper()


def test_metricflow_ossie_columns():
    b = _block()
    for c in ("relation", "primary_key", "agg_time_dimension", "from_relation", "from_columns",
              "to_relation", "to_columns", "base_relation", "agg", "expr", "filter", "numerator",
              "denominator", "input_metrics", "non_additive_dimension", "additivity", "unit",
              "format", "label", "description", "derived_sql", "object_kind", "origin",
              "match_rate", "cardinality"):
        assert re.search(rf"^\s+{c} ", b, re.M), c


def test_check_lists():
    b = _block()
    for lst in ("'fact','dimension'", "'simple','ratio','derived'",
                "'sum','count','count_distinct','average','min','max','sum_boolean'",
                "'additive','semi_additive','non_additive'", "'many_to_one','one_to_one'",
                "'foreign_key','agent'", "'base','view','matview'", "'active','retired'"):
        assert lst in b, lst


def test_pending_index():
    b = _block()
    assert b.count("CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_model_pending") == 1
    assert "ON actions ((payload->>'object'), (payload->>'name'))" in b
    assert "WHERE kind = 'model_change' AND status = 'proposed'" in b
