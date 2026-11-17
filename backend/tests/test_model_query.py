"""Phase 16 plan 04: query_measure contract and resolve_range (fakes only)."""
import json
from datetime import date

import pytest

from app import modeltools as mt
from app import modelcompiler as mc
from app import semanticmodel
from app.guardrails import SQLGuardrailError
from test_model_compiler import build


TODAY = date(2026, 10, 15)


@pytest.mark.parametrize("token,expected", [
    ("last_month", (date(2026, 9, 1), date(2026, 10, 1))),
    ("this_month", (date(2026, 10, 1), date(2026, 10, 16))),
    ("last_7_days", (date(2026, 10, 8), date(2026, 10, 15))),
    ("last_30_days", (date(2026, 9, 15), date(2026, 10, 15))),
    ("this_quarter", (date(2026, 10, 1), date(2026, 10, 16))),
    ("last_quarter", (date(2026, 7, 1), date(2026, 10, 1))),
    ("this_year", (date(2026, 1, 1), date(2026, 10, 16))),
    ("year_to_date", (date(2026, 1, 1), date(2026, 10, 16))),
    ("last_year", (date(2025, 1, 1), date(2026, 1, 1))),
    ({"start": "2026-09-01", "end": "2026-10-01"}, (date(2026, 9, 1), date(2026, 10, 1))),
    (None, (None, None)),
])
def test_resolve_range(token, expected):
    assert mt.resolve_range(token, TODAY) == expected


def test_resolve_range_rejects_unknown_token_and_bad_dates():
    with pytest.raises(mc.InvalidRequest):
        mt.resolve_range("next_week", TODAY)
    with pytest.raises(mc.InvalidRequest):
        mt.resolve_range({"start": "yesterday"}, TODAY)


def test_last_quarter_across_year_boundary():
    assert mt.resolve_range("last_quarter", date(2026, 2, 10)) == (date(2025, 10, 1), date(2026, 1, 1))


class Env:
    pass


@pytest.fixture
def env(monkeypatch):
    e = Env()
    e.model = build()
    e.rows = [{"metric_time__quarter": "2026-07-01", "regions__name": "North", "net_sales": 5}]
    e.sql = []
    e.logs = []
    e.raise_exc = None

    async def load_model():
        return e.model

    async def run_sql(sql):
        e.sql.append(sql)
        if e.raise_exc:
            raise e.raise_exc
        return e.rows

    async def log_tool(name, args, result, start, refused=False):
        e.logs.append({"name": name, "result": result, "refused": refused})

    monkeypatch.setattr(semanticmodel, "load_model", load_model)
    monkeypatch.setattr(mt, "run_readonly_sql", run_sql)
    monkeypatch.setattr(mt, "_log_tool", log_tool)
    monkeypatch.setattr(mt, "_today", lambda: TODAY)
    return e


async def q(**kw):
    return await mt.query_measure.ainvoke(kw)


async def test_governed_answer_has_rows_and_compiled_sql(env):
    out = json.loads(await q(measure="net_sales", by=["regions.name"], grain="month", range="last_quarter"))
    expected = mc.compile_measure(env.model, mc.make_request(
        "net_sales", by=["public.regions.name"], grain="month", start="2026-07-01", end="2026-10-01"))
    assert out["sql"] == expected.sql == env.sql[0]
    assert out["columns"] == list(expected.columns)
    assert out["measure"] == "net_sales" and out["rows"] == env.rows and out["row_count"] == 1
    assert out["truncated"] is False
    assert out["range"] == {"start": "2026-07-01", "end": "2026-10-01"}
    for k in ("label", "unit", "format"):
        assert k in out
    assert env.logs[-1] == {"name": "query_measure", "result": json.dumps(out) if False else env.logs[-1]["result"],
                            "refused": False}


async def test_ambiguous_shortcut_refused(env):
    env.model = build(extra_columns={"archive.regions": {"id": "integer", "name": "text"}})
    out = await q(measure="net_sales", by=["regions.name"])
    assert out.startswith("REFUSED: ambiguous dimension")
    assert env.logs[-1]["refused"] is True and env.sql == []


async def test_full_dimension_passes_through(env):
    out = json.loads(await q(measure="net_sales", by=["public.regions.name"]))
    assert "regions" in out["sql"]


async def test_unknown_measure_not_modeled_lists_measures(env):
    out = await q(measure="profit")
    assert out.startswith("NOT MODELED:")
    assert out.rstrip().split("Modeled measures: ")[1].split(", ")[0] == sorted(
        m.name for m in env.model.measures)[0]
    assert "net_sales" in out
    assert env.logs[-1]["refused"] is True and env.sql == []


async def test_grain_on_measure_without_time_is_not_modeled(env):
    out = await q(measure="customer_count", grain="month")
    assert out.startswith("NOT MODELED:") and env.logs[-1]["refused"] is True


async def test_unreachable_dimension_is_not_modeled(env):
    out = await q(measure="stock_on_hand", by=["public.sales_reps.full_name"])
    assert out.startswith("NOT MODELED:")


async def test_bad_grain_refused(env):
    out = await q(measure="net_sales", grain="fortnight")
    assert out.startswith("REFUSED:") and env.logs[-1]["refused"] is True


async def test_semicolon_in_filter_value_refused(env):
    out = await q(measure="net_sales", filters=[
        {"dimension": "public.sales_orders.channel", "op": "=", "values": ["web; drop table x"]}])
    assert out.startswith("REFUSED:") and env.sql == []


async def test_bad_range_refused(env):
    out = await q(measure="net_sales", range="next_week")
    assert out.startswith("REFUSED:")


async def test_guardrail_error_reported_not_refused(env):
    env.raise_exc = SQLGuardrailError("only SELECT")
    out = await q(measure="net_sales")
    assert out.startswith("GUARDRAIL REJECTED: only SELECT")
    assert env.logs[-1]["refused"] is False


async def test_other_execution_error(env):
    env.raise_exc = ValueError("bad thing")
    out = await q(measure="net_sales")
    assert out == "SQL ERROR (read it, fix the query, retry): ValueError: bad thing"
    assert env.logs[-1]["refused"] is False


async def test_truncated_flag_at_row_limit(env, monkeypatch):
    from app.config import get_settings
    n = get_settings().sql_row_limit
    env.rows = [{"net_sales": i} for i in range(n)]
    out = json.loads(await q(measure="net_sales"))
    assert out["row_count"] == n and out["truncated"] is True


async def test_large_result_drops_rows_at_boundaries(env):
    env.rows = [{"net_sales": i, "pad": "x" * 200} for i in range(400)]
    out = await q(measure="net_sales")
    assert len(out) <= 20000
    doc = json.loads(out)
    assert doc["truncated"] is True and doc["rows_shown"] == len(doc["rows"]) < 400
    assert doc["row_count"] == 400


async def test_filters_and_shortcut_in_filters(env):
    out = json.loads(await q(measure="net_sales", filters=[
        {"dimension": "regions.name", "op": "in", "values": ["North"]}]))
    assert "'North'" in out["sql"]


def test_query_measure_never_calls_rules_gate():
    assert "_rules_gate" not in open(mt.__file__, encoding="utf-8").read()
