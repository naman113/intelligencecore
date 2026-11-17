"""Phase 15 gap 2 (WR-14): pure warehouse naming rules."""
import pytest

from app.sourcesvc import (PG_KEYWORDS, _WH_TABLE_RE, table_name_problem, warehouse_names,
                           warehouse_table_name as w)


@pytest.mark.parametrize("remote,want", [
    ("OrderLines", "order_lines"), ("OrderID", "order_id"), ("HTTPLogs", "http_logs"),
    ("Order Details", "order_details"), ("Customers", "customers"), ("Order", "order_tbl"),
    ("User", "user_tbl"), ("group", "group_tbl"), ("2024 Sales", "t_2024_sales"),
    ("Bestellungen-Ä", "bestellungen"), ("###", "t"),
    ("order_lines", "order_lines"), ("a__b", "a__b"), ("_tmp", "_tmp"), ("carriers", "carriers"),
])
def test_warehouse_table_name(remote, want):
    assert w(remote) == want


def test_long_name_cut():
    r = w("AbCdEfGhIj" * 7)
    assert len(r.encode()) <= 55 and not r.endswith("_") and _WH_TABLE_RE.match(r)


@pytest.mark.parametrize("k", ["order", "user", "group", "select", "table", "join", "left"])
def test_keywords_present(k):
    assert k in PG_KEYWORDS


def test_outputs_always_valid():
    samples = ["Order", "x" * 80, "A B C", "éè", "9", "Table", "SELECT", "_", "-", "a-b", "Ab_Cd",
               "ORDER", "Left", "snake_Case", "weird	name"]
    for s in samples:
        r = w(s)
        assert _WH_TABLE_RE.match(r) and r not in PG_KEYWORDS, (s, r)


def _snap(*pairs):
    return {"tables": [{"schema": s, "table": t} for s, t in pairs]}


def test_warehouse_names_dedupe_and_order():
    pairs = [("dbo", "OrderLines"), ("dbo", "order_lines"), ("dbo", "Order_Lines"), ("dbo", "Region"),
             ("sales", "OrderLines")]
    want = {"order_lines": "order_lines", "OrderLines": "order_lines_2",
            "Order_Lines": "order_lines_3", "Region": "region"}
    assert warehouse_names(_snap(*pairs), "dbo") == want
    assert warehouse_names(_snap(*reversed(pairs)), "dbo") == want


def test_suffix_never_takes_another_base():
    out = warehouse_names(_snap(("s", "AB"), ("s", "ab"), ("s", "ab_2")), "s")
    assert len(set(out.values())) == 3 and out["ab"] == "ab" and out["ab_2"] == "ab_2"


@pytest.mark.parametrize("name,frag", [
    ("OrderLines", None), ("Order Details", None), ("dbo_x", None),
    ("", "empty"), ("x" * 64, "63 bytes"), ("a\x00b", "control characters"), ("a\nb", "control characters"),
])
def test_table_name_problem(name, frag):
    r = table_name_problem(name)
    if frag is None:
        assert r is None
    else:
        assert frag in r and "\x00" not in r and "\n" not in r
