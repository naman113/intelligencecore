"""Phase 12 (CTX-09b): KPI watch proposals come from approved definitions."""

import inspect

import pytest

import app.main as main
from app import knowledge


class _Pool:
    def __init__(self, titles):
        self.titles = titles

    async def fetch(self, sql, *a):
        return [{"title": t} for t in self.titles]


def _patch(monkeypatch, kpis, watched=(), hits=None):
    async def get_pool():
        return _Pool(watched)

    async def approved(limit=12):
        return kpis

    async def search(q, limit=8):
        search.q = q
        return hits or []

    monkeypatch.setattr(main.db, "get_pool", get_pool)
    monkeypatch.setattr(knowledge, "approved_kpis", approved)
    monkeypatch.setattr(knowledge, "search_business_context", search)
    return search


@pytest.mark.asyncio
async def test_proposals_skip_watched_titles(monkeypatch):
    _patch(monkeypatch, [{"ref": "kpi:a", "title": "Gross Margin", "body": "margin formula"},
                         {"ref": "kpi:b", "title": "Late Orders", "body": "late"}],
           watched=["late orders"])
    out = await main.propose_kpi_watches({})
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    assert p["title"] == "Gross Margin"
    assert p["condition"].startswith("Monitor the KPI 'Gross Margin':")
    assert p["kind"] == "kpi" and p["cadence_seconds"] == 3600


@pytest.mark.asyncio
async def test_fallback_to_neutral_search_and_cap(monkeypatch):
    hits = [{"kind": "kpi", "title": f"K{i}", "body": "b"} for i in range(12)]
    hits.append({"kind": "table", "title": "T", "body": "x"})
    search = _patch(monkeypatch, [], hits=hits)
    out = await main.propose_kpi_watches({})
    assert search.q == "key performance indicator KPI metric"
    assert len(out["proposals"]) == 8
    assert all(p["title"].startswith("K") for p in out["proposals"])


def test_route_has_no_domain_query():
    assert "fuel" not in inspect.getsource(main.propose_kpi_watches).lower()
