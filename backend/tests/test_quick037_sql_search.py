"""Quick 037: run_sql and search_business_context results are capped in code.

run_sql shows whole rows up to sql_result_chars, then a footer with the row count, the rows
shown and how to get the rest. search_business_context returns search_context_hits hits of at
most search_context_hit_chars characters each; the defaults keep every document the context-pack
fixtures produce intact (run this file as a script to print the measured sizes).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setting(monkeypatch):
    """Override one setting through the environment (the configservice path) for one test."""
    def _set(name, value):
        monkeypatch.setenv(name.upper(), str(value))
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


# ── run_sql ────────────────────────────────────────────────────────────────

def _sql_setup(monkeypatch, rows, note=""):
    calls = {}

    async def rr(sql, limit=None):
        calls["limit"] = limit
        return rows[:limit] if limit else rows

    async def gate(sql):
        return ""

    async def lt(*a, **k):
        return None

    async def nt():
        return note

    async def budget(sql):
        return None, []

    from app import livebudget
    monkeypatch.setattr(graph, "run_readonly_sql", rr)
    monkeypatch.setattr(graph, "_rules_gate", gate)
    monkeypatch.setattr(graph, "_log_tool", lt)
    monkeypatch.setattr(graph, "_p16_unverified_note", nt)
    monkeypatch.setattr(livebudget, "check", budget)
    return calls


def _row(i, width=120):
    return {"invoice_id": i, "customer": f"Customer {i:04d}", "amount": f"{i * 10.5:.2f}",
            "note": "x" * width}


async def test_run_sql_small_result_is_unchanged(monkeypatch):
    calls = _sql_setup(monkeypatch, [{"n": 1}])
    out = await graph.run_sql.ainvoke({"sql": "SELECT 1 AS n"})
    assert out == json.dumps([{"n": 1}])
    assert calls["limit"] == get_settings().sql_row_limit + 1, "one extra row tells 'more than the cap'"


async def test_run_sql_cut_by_characters_reports_rows_and_next_steps(monkeypatch):
    rows = [_row(i) for i in range(200)]
    _sql_setup(monkeypatch, rows)
    out = await graph.run_sql.ainvoke({"sql": "SELECT * FROM invoices"})
    cap = get_settings().sql_result_chars
    assert len(out) <= cap
    body, footer = out.split("\nTRUNCATED: ", 1)
    shown = json.loads(body)
    assert shown == rows[:len(shown)] and 0 < len(shown) < 200
    assert f"showing {len(shown)} of 200 rows" in footer
    for w in ("Do not total or count", "GROUP BY", "WHERE", "run_sql_to_csv", "data agent"):
        assert w in footer, w


async def test_run_sql_over_the_row_limit_says_more_than(monkeypatch, setting):
    setting("sql_row_limit", 50)
    rows = [{"n": i} for i in range(80)]
    calls = _sql_setup(monkeypatch, rows)
    out = await graph.run_sql.ainvoke({"sql": "SELECT n FROM t"})
    assert calls["limit"] == 51
    body, footer = out.split("\nTRUNCATED: ", 1)
    assert json.loads(body) == rows[:50], "every row the row limit allows still fits"
    assert "showing 50 of more than 50 rows" in footer and "count(*)" in footer


async def test_run_sql_exactly_the_row_limit_is_not_flagged(monkeypatch, setting):
    setting("sql_row_limit", 50)
    rows = [{"n": i} for i in range(50)]
    _sql_setup(monkeypatch, rows)
    out = await graph.run_sql.ainvoke({"sql": "SELECT n FROM t"})
    assert out == json.dumps(rows)


async def test_run_sql_one_huge_row_is_shown_with_long_values_cut(monkeypatch):
    rows = [{"id": 1, "payload": "y" * 30000}, {"id": 2, "payload": "z" * 30000}]
    _sql_setup(monkeypatch, rows)
    out = await graph.run_sql.ainvoke({"sql": "SELECT * FROM raw"})
    assert len(out) <= get_settings().sql_result_chars
    body, footer = out.split("\nTRUNCATED: ", 1)
    shown = json.loads(body)
    assert len(shown) == 1 and shown[0]["id"] == 1 and len(shown[0]["payload"]) < 400
    assert "showing 1 of 2 rows" in footer and "long text values cut" in footer


async def test_run_sql_cap_is_a_live_setting(monkeypatch, setting):
    setting("sql_result_chars", 3000)
    _sql_setup(monkeypatch, [_row(i) for i in range(200)])
    out = await graph.run_sql.ainvoke({"sql": "SELECT * FROM invoices"})
    assert len(out) <= 3000 and "TRUNCATED" in out


async def test_run_sql_note_still_follows_the_footer(monkeypatch):
    _sql_setup(monkeypatch, [_row(i) for i in range(200)], note="\n\nUNVERIFIED: x")
    out = await graph.run_sql.ainvoke({"sql": "SELECT * FROM invoices"})
    assert "TRUNCATED" in out and out.endswith("\n\nUNVERIFIED: x")


# ── search_business_context ────────────────────────────────────────────────

def _pack_hits():
    """Every business-context document the three context-pack fixtures produce, as search hits."""
    from app.contextstore import _compose
    hits = []
    for f in ("context_pack_northwind.json", "context_pack_northwind_full.json", "context_pack_rehearsal.json"):
        d = json.loads((Path(__file__).parent / "fixtures" / f).read_text(encoding="utf-8"))
        for t in d.get("tables") or []:
            p = t.get("propose") or t
            ref = p.get("ref", "")
            schema, table = ref.split(".")[-2:] if ref.count(".") >= 2 else ("public", ref)
            _, doc, meta = _compose({"kind": "table", "text": p.get("text") or "", "schema": schema,
                                     "table": table, "ref": ref})
            hits.append({"score": 0.71234567, "type": "table", "text": doc, "table_name": table,
                         "column_name": None, "kpi_name": None, "source": "business_db", "ref": ref})
            for c, txt in (p.get("columns") or {}).items():
                txt = txt.get("text", "") if isinstance(txt, dict) else txt
                _, doc, _ = _compose({"kind": "column", "text": txt or "", "schema": schema, "table": table,
                                      "column": c, "structure": {"type": "integer", "nullable": True}})
                hits.append({"score": 0.6512345, "type": "column", "text": doc, "table_name": table,
                             "column_name": c, "kpi_name": None, "source": "business_db",
                             "ref": f"{ref}.{c}"})
        for sec in ("rules", "glossary", "kpis"):
            for r in d.get(sec) or []:
                p = r.get("propose") or r
                if not p.get("text"):
                    continue
                kind = p.get("kind") or ("kpi" if sec == "kpis" else "rule")
                _, doc, _ = _compose({"kind": kind, "text": p["text"], "title": p.get("title"),
                                      "ref": p.get("ref", "x:y")})
                hits.append({"score": None, "type": kind, "text": doc, "table_name": None,
                             "column_name": None, "kpi_name": p.get("title") if kind == "kpi" else None,
                             "source": None, "ref": p.get("ref")})
    return hits


def _search_setup(monkeypatch, hits):
    seen = {}

    async def search(q, limit=8):
        seen["limit"] = limit
        return hits[:limit]

    async def no_note(*a, **k):
        return ""

    async def lt(*a, **k):
        return None
    monkeypatch.setattr(graph, "_search_ctx", search)
    monkeypatch.setattr(graph, "_rules_note", no_note)
    monkeypatch.setattr(graph, "_sizes_note", no_note)
    monkeypatch.setattr(graph, "_log_tool", lt)
    return seen


def test_fixture_documents_fit_the_default_hit_cap():
    """Measured: every document the context-pack fixtures produce is under 500 characters
    (median about 120), so the 800-character default never cuts one."""
    docs = [len(h["text"]) for h in _pack_hits()]
    assert len(docs) > 150
    assert max(docs) < Settings().search_context_hit_chars
    assert sorted(docs)[len(docs) // 2] < 200


async def test_search_keeps_the_top_hits_intact_and_drops_empty_keys(monkeypatch):
    hits = _pack_hits()
    seen = _search_setup(monkeypatch, hits)
    out = await graph.search_business_context.ainvoke({"query": "orders"})
    assert seen["limit"] == Settings().search_context_hits == 8
    got = json.loads(out)
    assert [h["text"] for h in got] == [h["text"] for h in hits[:8]]
    assert all(None not in h.values() for h in got)
    assert got[0]["score"] == 0.712
    old = json.dumps(hits[:8], default=str)
    assert len(out) < len(old)


async def test_search_cuts_a_long_hit_and_says_so(monkeypatch, setting):
    setting("search_context_hit_chars", 300)
    long_rule = {"score": 0.9, "type": "rule", "ref": "rule:x", "text": "Business rule x: " + "a" * 2000}
    long_col = {"score": 0.8, "type": "column", "ref": "c", "text": "Column t.c: " + "b" * 2000,
                "table_name": "t"}
    _search_setup(monkeypatch, [long_rule, long_col])
    got = json.loads(await graph.search_business_context.ainvoke({"query": "x"}))
    assert got[0]["text"].startswith("Business rule x: aaa") and len(got[0]["text"]) < 420
    assert "cut at 300 of 2017 characters" in got[0]["text"] and "rule:x" in got[0]["text"]
    assert "cut at 300 of" in got[1]["text"]


async def test_search_hit_count_is_a_setting(monkeypatch, setting):
    setting("search_context_hits", 3)
    seen = _search_setup(monkeypatch, _pack_hits())
    got = json.loads(await graph.search_business_context.ainvoke({"query": "x"}))
    assert seen["limit"] == 3 and len(got) == 3


async def test_search_by_ref_is_never_cut_per_hit(monkeypatch, setting):
    setting("search_context_hit_chars", 200)

    async def by_ref(refs):
        return [{"type": "rule", "ref": refs[0], "text": "Business rule x: " + "c" * 1500, "tables": []}]
    _search_setup(monkeypatch, [])
    monkeypatch.setattr(graph._knowledge, "rules_by_ref", by_ref)
    got = json.loads(await graph.search_business_context.ainvoke({"query": "rule:fuel-day-ist"}))
    assert got[0]["text"] == "Business rule x: " + "c" * 1500


async def test_search_overall_cap_cuts_at_a_hit_boundary(monkeypatch):
    async def by_ref(refs):
        return [{"type": "rule", "ref": r, "text": "r" * 1990} for r in refs]
    _search_setup(monkeypatch, [])
    monkeypatch.setattr(graph._knowledge, "rules_by_ref", by_ref)
    q = ", ".join(f"rule:r{i}" for i in range(20))
    out = await graph.search_business_context.ainvoke({"query": q})
    assert len(out) <= 20000
    body, note = out.split("\n\n", 1)
    shown = json.loads(body)
    assert 0 < len(shown) < 20 and all(len(h["text"]) == 1990 for h in shown)
    assert note.startswith(f"NOT SHOWN: {20 - len(shown)} more hits") and "fewer refs" in note




if __name__ == "__main__":
    hits = _pack_hits()
    docs = sorted(len(h["text"]) for h in hits)
    print(f"context documents from the fixtures: n={len(docs)} median={docs[len(docs) // 2]} "
          f"p95={docs[int(len(docs) * 0.95)]} max={docs[-1]}")
    old = json.dumps(hits[:8], default=str)
    new = graph._hits_json(hits[:8], Settings().search_context_hit_chars)
    print(f"8 hits: {len(old):,} characters before, {len(new):,} after")
