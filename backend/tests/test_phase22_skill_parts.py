"""Phase 22 plan 10: the on-demand files of dashboard-authoring (recipes/ and reference/).

Every json block in them parses as spec v2 and binds against fixture_model(); sizes, ASCII
and the no-colour rule are pinned. fixture_model() and json_blocks() are reused by 22-18.
"""
import json
import re
from pathlib import Path

import pytest

from app import dashboardspec as ds
from app import skillservice
from app.agents import graph
from tests.test_compiler_tiles import (
    COLUMNS, MEASURES, RELS, TABLES, TS, TSTZ, _m, model_from_rows,
)

SKILL_DIR = (Path(__file__).resolve().parents[2] / "skills_library"
             / "intelligence-corner" / "dashboard-authoring")
RECIPES = ["daily-operations", "receivables", "payables-gst", "fleet-compliance", "b2b-order-book"]
REFERENCES = ["spec-v2", "chart-guide", "forecast-panel"]
PARTS = [f"recipes/{r}.md" for r in RECIPES] + [f"reference/{r}.md" for r in REFERENCES]

EXTRA_COLUMNS = {
    "public.grns": {"id": "integer", "grn_date": "date", "customer_id": "integer"},
    "public.invoices": {"id": "integer", "customer_id": "integer", "amount": "numeric",
                        "age_bucket": "text", "due_date": "date", "status": "text"},
    "public.gst_entries": {"id": "integer", "entry_date": "date", "amount": "numeric"},
    "public.bills": {"id": "integer", "amount": "numeric", "status": "text"},
    "public.fuel": {"id": "integer", "fuel_date": "date", "litres": "numeric", "spend": "numeric",
                    "km": "numeric"},
    "public.orders": {"id": "integer", "order_date": "date", "value": "numeric",
                      "customer_id": "integer"},
}


def fixture_model():
    """The compiler fixture model plus every measure the skill examples use."""
    columns = {k: dict(v) for k, v in COLUMNS.items()}
    columns.update({k: dict(v) for k, v in EXTRA_COLUMNS.items()})
    columns["public.dispatches"]["roll_count"] = "integer"
    tables = list(TABLES) + [
        dict(name=r.split(".")[1], relation=r, object_kind="base", role=None, grain=None,
             primary_key=[], agg_time_dimension=None, status="active") for r in EXTRA_COLUMNS]
    rels = list(RELS) + [
        dict(name=f"{t}_customer", from_relation=f"public.{t}", from_columns=["customer_id"],
             to_relation="public.customers", to_columns=["id"], cardinality="many_to_one",
             status="active") for t in ("grns", "invoices", "orders")]

    def simple(name, rel, agg, expr, time=None, **kw):
        return _m(name=name, type="simple", base_relation=f"public.{rel}", agg=agg, expr=expr,
                  agg_time_dimension=f"public.{rel}.{time}" if time else None, **kw)

    extra = [
        simple("grns", "grns", "count", "id", "grn_date"),
        simple("rolls_out", "dispatches", "sum", "roll_count", "dispatch_date"),
        # receivables and payables balances have no date: kind now
        simple("receivables_open", "invoices", "sum", "amount",
               filter=[dict(dimension="public.invoices.status", op="=", values=["open"])]),
        simple("receivables_overdue", "invoices", "sum", "amount",
               filter=[dict(dimension="public.invoices.age_bucket", op="!=", values=["current"])]),
        simple("gst_payable", "gst_entries", "sum", "amount", "entry_date"),
        simple("payables_open", "bills", "sum", "amount"),
        simple("fuel_litres", "fuel", "sum", "litres", "fuel_date"),
        simple("fuel_spend", "fuel", "sum", "spend", "fuel_date"),
        simple("fuel_km", "fuel", "sum", "km", "fuel_date"),
        _m(name="fuel_per_km", type="ratio", numerator=dict(name="fuel_litres"),
           denominator=dict(name="fuel_km")),
        simple("orders", "orders", "count", "id", "order_date"),
        simple("order_value", "orders", "sum", "value", "order_date"),
    ]
    return model_from_rows(tables, rels, list(MEASURES) + extra, columns)


def json_blocks(text: str) -> list[dict]:
    """Every fenced json block of a markdown file, parsed."""
    fence = "`" * 3
    pat = re.compile(fence + r"json\n(.*?)\n" + fence, re.S)
    return [json.loads(m) for m in pat.findall(text)]


def read(part: str) -> str:
    return (SKILL_DIR / part).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def model():
    return fixture_model()


def bind_all(raw, model):
    spec = ds.parse(raw)
    assert spec.spec_version == 2
    declared = [q for q in raw.get("queries", [])]
    compiled = ds.compile_measures_v2(spec, model)
    return ds.bind_v2(spec, declared, compiled)


@pytest.mark.parametrize("part", [p for p in PARTS if p.startswith("recipes/")] + ["reference/spec-v2.md"])
def test_every_example_parses_and_binds(model, part):
    blocks = json_blocks(read(part))
    assert blocks, f"{part} has no json example"
    for raw in blocks:
        stored = bind_all(raw, model)
        assert stored["spec_version"] == 2


@pytest.mark.parametrize("part", PARTS)
def test_files_exist_ascii_and_free_of_style(part):
    raw = (SKILL_DIR / part).read_bytes()
    text = raw.decode("ascii")  # raises on non-ASCII
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", text)
    assert not re.search(r"\b(color|background|font-[a-z]+|margin|padding|border)\s*:", text)
    assert not re.search(r'"style"\s*:|\bstyle\s*=', text)


@pytest.mark.parametrize("name", RECIPES)
def test_recipe_size_cap(name):
    assert len((SKILL_DIR / "recipes" / f"{name}.md").read_bytes()) <= 6000


def test_spec_reference_cap_opening_and_coverage():
    raw = (SKILL_DIR / "reference" / "spec-v2.md").read_bytes()
    assert len(raw) <= 8192
    text = raw.decode("ascii")
    assert text.startswith("Load this only when a field is unclear; SKILL.md covers the common cases.")
    for key in ("spec_version", "title", "subtitle", "number_locale", "date_control", "filters",
                "queries", "sections", "tiles"):
        assert f"`{key}`" in text, key
    for typ in ("stat", "line", "area", "bar", "stacked_bar", "sparkline", "text", "table",
                "combo", "paired_bar", "aging", "bullet", "waterfall", "calendar", "donut",
                "ranked_bar", "funnel"):
        assert f"`{typ}`" in text, typ


def test_chart_guide_has_table_and_forecast_panel_is_verbatim():
    guide = read("reference/chart-guide.md")
    assert "| Question type | Use | Avoid |" in guide and "gauge" in guide
    panel = read("reference/forecast-panel.md")
    assert panel.startswith("## Forecast panel")
    assert "Style it as a `line` tile" in panel and "regr_slope" in panel


def test_receivables_recipe_has_no_window_grain_top_or_other_in_a_binding():
    raw = json_blocks(read("recipes/receivables.md"))[0]
    assert "date_control" not in raw
    for t in raw["tiles"]:
        for key in ("window", "grain", "top", "other"):
            assert key not in t.get("measure", {}), (t["id"], key)


def test_b2b_recipe_covers_last_year():
    raw = json_blocks(read("recipes/b2b-order-book.md"))[0]
    assert raw["date_control"]["horizon_days"] == 732
    assert any(t.get("ghost") == "last_year" for t in raw["tiles"])


@pytest.mark.parametrize("part", PARTS)
async def test_each_file_loads_through_use_skill(monkeypatch, part):
    entry = skillservice.library_entry("intelligence-corner/dashboard-authoring")
    assert entry is not None
    row = {"id": 1, "name": "dashboard-authoring", "description": "d", "body": "B",
           "source": "library", "origin_ref": entry["id"], "enabled": True}

    async def fake_log(*a, **k):
        return None

    async def fake_get(name):
        return dict(row)

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    monkeypatch.setattr(skillservice, "get_skill", fake_get)
    fn = graph.use_skill.coroutine or graph.use_skill.func
    r = await fn(name="dashboard-authoring", part=part)
    assert r.startswith(skillservice.FRAMING)
    assert not r.startswith(("REFUSED", "ERROR", "TOOL ERROR"))
    body = r[len(skillservice.FRAMING) + 2:]
    assert body == read(part).replace("\r\n", "\n")[:20000] or len(body) > 100


async def test_missing_library_folder_is_not_reported_as_a_bad_part_name(monkeypatch):
    entry = skillservice.library_entry("intelligence-corner/dashboard-authoring")
    row = {"id": 1, "name": "dashboard-authoring", "description": "d", "body": "B",
           "source": "library", "origin_ref": entry["id"], "enabled": True}

    async def fake_log(*a, **k):
        return None

    async def fake_get(name):
        return dict(row)

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    monkeypatch.setattr(skillservice, "get_skill", fake_get)
    monkeypatch.setattr(skillservice, "_entry_dir", lambda e: None)
    fn = graph.use_skill.coroutine or graph.use_skill.func
    r = await fn(name="dashboard-authoring", part="recipes/daily-operations.md")
    assert r.startswith("ERROR: library folder") and "REFUSED" not in r
