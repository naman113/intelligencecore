"""Phase 16 plan 06: the pure parts of scripts/scenario_model.py (no stack needed)."""
import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app import modelcompiler as mc
from app.modelcompiler import canonical_request, compile_measure, model_from_rows, normalize_definition

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scenario_model.py"
spec = importlib.util.spec_from_file_location("scenario_model", SCRIPT)
sm = importlib.util.module_from_spec(spec)
sys.modules["scenario_model"] = sm
spec.loader.exec_module(sm)

COLUMNS = {
    "public.regions": {"id": "integer", "name": "text"},
    "public.sales_reps": {"id": "integer", "full_name": "text", "region_id": "integer",
                          "hired_on": "date", "is_active": "boolean"},
    "public.customers": {"id": "integer", "name": "text", "segment": "text", "region_id": "integer",
                         "rep_id": "integer", "city": "text", "credit_limit": "numeric(12,2)",
                         "created_at": "date", "is_active": "boolean"},
    "public.products": {"id": "integer", "sku": "text", "name": "text", "category": "text",
                        "unit_price": "numeric(10,2)", "unit_cost": "numeric(10,2)",
                        "is_discontinued": "boolean"},
    "public.sales_orders": {"id": "integer", "customer_id": "integer", "order_date": "date",
                            "status": "text", "channel": "text"},
    "public.order_lines": {"id": "integer", "order_id": "integer", "product_id": "integer",
                           "qty": "integer", "unit_price": "numeric(10,2)",
                           "discount_pct": "numeric(5,2)"},
    "public.invoices": {"id": "integer", "order_id": "integer", "customer_id": "integer",
                        "invoice_date": "date", "due_date": "date", "amount": "numeric(12,2)"},
    "public.payments": {"id": "integer", "invoice_id": "integer", "paid_at": "date",
                        "amount": "numeric(12,2)", "method": "text"},
    "public.inventory_snapshots": {"product_id": "integer", "snapshot_date": "date",
                                   "on_hand": "integer"},
}
FKS = [
    ("sales_reps", "region_id", "regions"), ("customers", "region_id", "regions"),
    ("customers", "rep_id", "sales_reps"), ("sales_orders", "customer_id", "customers"),
    ("order_lines", "order_id", "sales_orders"), ("order_lines", "product_id", "products"),
    ("invoices", "order_id", "sales_orders"), ("invoices", "customer_id", "customers"),
    ("payments", "invoice_id", "invoices"), ("inventory_snapshots", "product_id", "products"),
]


def test_stack_config_p26():
    c = sm.stack_config("p26")
    assert c["base"] == "http://127.0.0.1:9261"
    assert c["ui"] == "http://127.0.0.1:9260"
    assert c["app"] == "ic-p26-app-1"
    assert c["pg"] == "ic-p26-postgres-1"
    assert c["wh"] == "ic-p26-warehouse"


def test_refusal_rules():
    cfg = sm.load_config({"STACK": "p26", "OPS_KEY": "k"}, [])
    assert sm.refusal(cfg, {}) is not None
    assert sm.refusal(cfg, {"SCENARIO_DEMO_STACK": "1"}) is None
    bad = dict(cfg, base="http://example.com:9261")
    assert "loopback" in sm.refusal(bad, {"SCENARIO_DEMO_STACK": "1"})


def test_llm_keys_present(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("ANTHROPIC_API_KEY=\nOPENAI_API_KEY=\n", encoding="utf-8")
    assert sm.llm_keys_present(str(f)) is False
    f.write_text("ANTHROPIC_API_KEY=REPLACE_ME\nOPENAI_API_KEY=REPLACE_ME_TOO\n", encoding="utf-8")
    assert sm.llm_keys_present(str(f)) is False
    f.write_text("ANTHROPIC_API_KEY=\nOPENAI_API_KEY=sk-secret-value\n", encoding="utf-8")
    assert sm.llm_keys_present(str(f)) is True
    assert sm.llm_keys_present(str(tmp_path / "missing.env")) is False


def test_agent_check_mode():
    zero = {"model_tables": 0, "model_relationships": 0, "model_measures": 0, "model_indexes": 0}
    assert sm.agent_check_mode(zero) == "full"
    assert sm.agent_check_mode({**zero, "model_measures": 3}) == "delta"
    assert sm.agent_check_mode({**zero, "model_indexes": 1}) == "delta"


def test_parse_check_lines():
    out = sm.parse_check_lines("PASS 1 a\nSKIP 2 b: no LLM key\nFAIL 3 c: x\nnoise")
    assert out == {"PASS": [1], "SKIP": [2], "FAIL": [3]}


def test_tables_shape():
    assert len(sm.TABLES) == 9
    roles = [t["role"] for t in sm.TABLES]
    assert roles.count("dimension") == 4 and roles.count("fact") == 5
    ol = next(t for t in sm.TABLES if t["name"] == "order_lines")
    assert ol["agg_time_dimension"] == "public.sales_orders.order_date"


@pytest.mark.parametrize("obj,items", [
    ("table", "TABLES"), ("measure", "SIMPLE_MEASURES"), ("measure", "COMPOSED_MEASURES"),
])
def test_definitions_normalize(obj, items):
    for d in getattr(sm, items):
        normalize_definition(obj, dict(d))


def test_derived_objects_normalize():
    normalize_definition("view", dict(sm.VIEW))
    normalize_definition("materialized_view", dict(sm.MATVIEW))
    normalize_definition("index", dict(sm.INDEX))


def _rows():
    tables = []
    for t in sm.TABLES:
        d = normalize_definition("table", dict(t))
        d["status"] = "active"
        tables.append(d)
    rels = [dict(name=f"{f}_{c}", from_relation=f"public.{f}", from_columns=[c],
                 to_relation=f"public.{t}", to_columns=["id"], cardinality="many_to_one",
                 status="active") for f, c, t in FKS]
    measures = []
    for d in sm.SIMPLE_MEASURES + sm.COMPOSED_MEASURES:
        n = normalize_definition("measure", dict(d))
        n["status"] = "active"
        measures.append(n)
    return tables, rels, measures


def test_canonical_compile_every_scripted_measure():
    tables, rels, measures = _rows()
    model = model_from_rows(tables, rels, measures, COLUMNS)
    names = {m.name for m in model.measures}
    assert names == {"order_count", "net_sales", "invoiced_amount", "collected_amount",
                     "stock_on_hand", "average_order_value", "net_sales_mom_growth"}
    for m in model.measures:
        req = canonical_request(model, m, "month", date(2026, 9, 1), date(2026, 10, 1))
        sql = compile_measure(model, req).sql
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), m.name


def test_approval_order():
    o = sm.APPROVAL_ORDER
    assert o[0] == "relationships"
    assert o.index("tables") < o.index("composed_measures")
    assert o.index("simple_measures") < o.index("composed_measures")
    assert o.index("composed_measures") < o.index("view") < o.index("materialized_view")
    assert o.index("materialized_view") < o.index("index") < o.index("refresh")


def test_stage_items_cover_all_objects():
    assert len(sm.stage_items("tables")) == 9
    assert len(sm.stage_items("simple_measures")) == 5
    assert len(sm.stage_items("composed_measures")) == 2
    assert sm.stage_items("refresh")[0]["op"] == "refresh"


def test_script_refuses_without_demo_flag():
    import os
    env = {k: v for k, v in os.environ.items() if k != "SCENARIO_DEMO_STACK"}
    env["STACK"] = "p26"
    r = subprocess.run([sys.executable, str(SCRIPT), "--secrets", "/nonexistent/secrets.env"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0
    assert "REFUSED" in r.stdout
