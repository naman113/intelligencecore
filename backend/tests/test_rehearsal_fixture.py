"""Phase 13 (GOLIVE-08): the Northwind rehearsal context pack conforms to the template."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND.parent
TEMPLATE = APP.parent / ".planning" / "research" / "context-pack-prompt.md"
SCHEMA = APP.parent / "deploy" / "demo-business.sql"
SCRIPT = BACKEND / "scripts" / "check_context_pack.py"
FIXTURE = BACKEND / "tests" / "fixtures" / "context_pack_rehearsal.json"

DEMO_COLUMNS = {
    "regions": ["id", "name"],
    "sales_reps": ["id", "full_name", "region_id", "hired_on", "is_active"],
    "customers": ["id", "name", "segment", "region_id", "rep_id", "city", "credit_limit",
                  "created_at", "is_active"],
    "products": ["id", "sku", "name", "category", "unit_price", "unit_cost", "is_discontinued"],
    "sales_orders": ["id", "customer_id", "order_date", "status", "channel"],
    "order_lines": ["id", "order_id", "product_id", "qty", "unit_price", "discount_pct"],
    "invoices": ["id", "order_id", "customer_id", "invoice_date", "due_date", "amount"],
    "payments": ["id", "invoice_id", "paid_at", "amount", "method"],
    "inventory_snapshots": ["product_id", "snapshot_date", "on_hand"],
}


def _schema_columns() -> dict:
    if not SCHEMA.exists():
        return DEMO_COLUMNS
    sql = SCHEMA.read_text(encoding="utf-8")
    out = {}
    for name, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        cols = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            first = re.match(r"(\w+)", line)
            if not first or first.group(1).upper() in (
                    "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                continue
            cols.append(first.group(1))
        out[name] = cols
    return out


def _pack() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_pinned_columns_are_48():
    assert len(DEMO_COLUMNS) == 9
    assert sum(len(v) for v in DEMO_COLUMNS.values()) == 48


def test_schema_parse_matches_pinned():
    assert _schema_columns() == DEMO_COLUMNS


def test_vendored_checker_matches_template():
    if not TEMPLATE.exists():
        pytest.skip("template not reachable")
    t = TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    i = t.index("## Self-check before you finish")
    block = re.search(r"```python\n(.*?)```", t[i:], re.S).group(1)
    mine = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = "\n".join(ln for ln in mine.split("\n")[0:]).lstrip("#")  # placeholder, replaced below
    lines = mine.split("\n")
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    assert "\n".join(lines) == block
    assert block.startswith("import json, re, sys, unicodedata")


def test_checker_prints_ok_on_fixture():
    r = subprocess.run([sys.executable, str(SCRIPT), str(FIXTURE)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "OK"


def test_every_column_described():
    pack = _pack()
    cols = _schema_columns()
    by_ref = {t["propose"]["ref"]: t["propose"]["columns"] for t in pack["tables"]}
    assert set(by_ref) == {f"business_db.public.{n}" for n in cols}
    for name, names in cols.items():
        described = by_ref[f"business_db.public.{name}"]
        for c in names:
            assert described.get(c, "").strip(), f"{name}.{c} undescribed"


def test_coverage_and_hide():
    pack = _pack()
    cov = pack["coverage"]["tables"]
    names = sorted(c["name"] for c in cov)
    assert names == sorted(f"public.{n}" for n in _schema_columns()) or \
        names == sorted(f"business_db.public.{n}" for n in _schema_columns())
    assert len(names) == 9 == len(set(names))
    assert all(c["where"] == "tables" for c in cov)
    assert pack["hide_tables"] == []
    for k in ("soft_delete", "test_data", "status", "money", "time", "tenancy",
              "derived_column", "audit", "archive", "naming"):
        assert str(pack["coverage"]["conventions"].get(k, "")).strip()


def test_watches_and_dashboards():
    pack = _pack()
    assert len(pack["watch_candidates"]) == 3
    for w in pack["watch_candidates"]:
        assert isinstance(w["trigger_hint"], dict)
        assert w["cadence_minutes"] >= 15
    assert len(pack["dashboard_ideas"]) == 2
    for d in pack["dashboard_ideas"]:
        assert d.get("confidence") and d.get("evidence")


def test_nothing_about_open_orders_reserved_for_track_this():
    pack = _pack()
    titles = [k["propose"]["title"] for k in pack["kpis"]]
    titles += [w["title"] for w in pack["watch_candidates"]]
    titles += [d["title"] for d in pack["dashboard_ideas"]]
    for t in titles:
        low = t.lower()
        assert not ("open" in low and "order" in low), t


def test_sections_present():
    pack = _pack()
    assert pack["pack_version"] == 1
    for k in ("meta", "source", "business_profile"):
        assert pack[k]
    cats = {r.get("category") for r in pack["rules"]}
    assert {"naming", "status", "money", "time"} <= cats
    assert len(pack["glossary"]) >= 2
    assert 3 <= len(pack["kpis"]) <= 4
    assert len(pack["open_questions"]) >= 1
