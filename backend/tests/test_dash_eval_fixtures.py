"""22-13 Task 1: synthetic eval fixtures, models and reference SQL."""

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "scripts" / "dash_eval"
sys.path.insert(0, str(ROOT))

import gen_fixture  # noqa: E402

from app import guardrails, modelcompiler as mc  # noqa: E402

CLIENTS = ("mill", "fleet", "distributor")
TODAY = date(2026, 11, 4)
MODELS = {c: json.loads((ROOT / "models" / f"{c}.json").read_text(encoding="utf-8")) for c in CLIENTS}
REFS = json.loads((ROOT / "references.json").read_text(encoding="utf-8"))
_TYPES = {"timestamptz": "timestamp with time zone", "int": "integer"}


def schema_columns(client: str) -> dict:
    """{'public.<table>': {col: declared type}} read from the schema file."""
    text = (ROOT / "fixtures" / f"{client}.schema.sql").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S):
        cols = {}
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY", "FOREIGN", "CONSTRAINT")):
                continue
            name, typ = line.split()[:2]
            typ = typ.split("(")[0].lower()
            cols[name] = _TYPES.get(typ, typ)
        out[f"public.{m.group(1)}"] = cols
    return out


def build_model(client: str) -> mc.Model:
    tables, rels, measures = [], [], []
    for item in MODELS[client]:
        norm = mc.normalize_definition(item["object"], item["definition"])
        {"table": tables, "relationship": rels, "measure": measures}[item["object"]].append(norm)
    return mc.model_from_rows(tables, rels, measures, schema_columns(client))


@pytest.mark.parametrize("client", CLIENTS)
def test_generator_is_deterministic_and_sized(client):
    a = gen_fixture.render(client, TODAY, 7)
    b = gen_fixture.render(client, TODAY, 7)
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()
    assert a != gen_fixture.render(client, TODAY, 8)
    tables = gen_fixture.build(client, TODAY, 7)
    assert set(tables) == set(schema_table_names(client))
    big = {"mill": "rolls_inward", "fleet": "trips", "distributor": "orders"}[client]
    assert 3000 <= len(tables[big]) <= 12000
    for name, rows in tables.items():
        width = len(gen_fixture.COLUMNS[client][name])
        assert rows and all(len(r) == width for r in rows), name
        assert set(gen_fixture.COLUMNS[client][name]) == set(schema_columns(client)[f"public.{name}"])


def schema_table_names(client):
    return [k.split(".")[1] for k in schema_columns(client)]


def test_mill_shape():
    t = gen_fixture.build("mill", TODAY, 7)
    days = {r[0] for r in t["stock_daily"]}
    assert len(days) == 450 and max(days) == TODAY
    assert len(t["stock_daily"]) == 450 * len(t["qualities"])  # one row per quality per day
    # some events fall after UTC midnight (IST morning)
    assert any(r[1][11:16] < "05:30" for r in t["rolls_inward"])
    buckets = {r[7] for r in t["invoices"]}
    assert buckets == {"0-30", "31-60", "61-90", "90+"}
    for r in t["invoices"]:
        assert r[6] == (r[3] < TODAY and r[5] < r[4])


def test_today_moves_the_window():
    a = gen_fixture.build("distributor", date(2026, 11, 4), 7)["orders"]
    b = gen_fixture.build("distributor", date(2026, 12, 4), 7)["orders"]
    assert max(r[1] for r in a) == date(2026, 11, 4)
    assert max(r[1] for r in b) == date(2026, 12, 4)


@pytest.mark.parametrize("client", CLIENTS)
def test_model_normalises_and_dims_resolve(client):
    model = build_model(client)
    assert model.measures and model.tables
    cols = schema_columns(client)
    for t in model.tables:
        assert t.relation in cols
    for r in model.relationships:
        assert r.from_relation in cols and r.to_relation in cols
        assert set(r.from_columns) <= set(cols[r.from_relation])


@pytest.mark.parametrize("client", CLIENTS)
def test_every_measure_has_reference_with_matching_kind(client):
    model = build_model(client)
    refs = REFS[client]
    assert {m.name for m in model.measures} == set(refs)
    for m in model.measures:
        assert refs[m.name]["kind"] == mc.measure_kind(model, m), m.name


def _fill(sql, extra):
    return sql.replace("{from}", "DATE '2026-10-01'").replace("{to}", "DATE '2026-10-31'").replace("{extra}", extra)


@pytest.mark.parametrize("client", CLIENTS)
def test_reference_sql_validates_and_has_hooks(client):
    cols = schema_columns(client)
    for name, ref in REFS[client].items():
        sql = ref["sql"]
        assert "{extra}" in sql, name
        assert ("{from}" in sql and "{to}" in sql) == (ref["kind"] != "now"), name
        guardrails.validate_readonly_sql(_fill(sql, ""))
        for dim, expr in ref["dims"].items():
            guardrails.validate_readonly_sql(_fill(sql, f"AND {expr} = 'x'"))
            table, col = dim.split(".")
            assert col in cols[f"public.{table}"], dim
        if ref["kind"] in ("sum", "closing", "now", "distinct"):
            assert "COALESCE(" in sql, name


def test_dims_cover_tables_read_by_the_sql():
    for client in CLIENTS:
        for name, ref in REFS[client].items():
            for table in re.findall(r"(?:FROM|JOIN) (\w+) \w+", ref["sql"]):
                if table in ("customers", "suppliers", "qualities", "vehicles", "channels"):
                    assert any(d.startswith(table + ".") for d in ref["dims"]), (client, name, table)
    assert {"invoices.age_bucket", "invoices.is_overdue", "invoices.due_date"} <= set(
        REFS["mill"]["receivables_open"]["dims"])
    assert {"invoices.age_bucket", "invoices.is_overdue", "invoices.due_date"} <= set(
        REFS["distributor"]["receivables_open"]["dims"])


def test_no_client_references():
    bad = re.compile(r"agentic-|distributor-|client-|railway", re.I)
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".json", ".sql", ".md"):
            assert not bad.search(p.read_text(encoding="utf-8")), p


def test_requests_measures_exist():
    req = ROOT / "requests.json"
    if not req.exists():
        pytest.skip("requests.json not written yet")
    for r in json.loads(req.read_text(encoding="utf-8")):
        names = {m.name for m in build_model(r["client"]).measures}
        for checks in r["checks"].values():
            for c in checks:
                if c["check"] == "bound_measures_any":
                    assert set(c["any"]) <= names, (r["n"], c["any"])
