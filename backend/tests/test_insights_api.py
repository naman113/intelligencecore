"""GET /api/insights: auth, validation, mapping, merge order, read-only."""

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import insights, insights_api


def ts(day, hour=0):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


class Pool:
    def __init__(self, notes=(), watch=()):
        self.notes, self.watch = list(notes), list(watch)
        self.calls = []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        if "FROM case_events" in s:
            return self.notes
        if "FROM watch_events" in s:
            return self.watch
        raise AssertionError(s)


def _wire(monkeypatch, pool):
    async def gp():
        return pool
    monkeypatch.setattr(insights, "get_pool", gp)
    return pool


def _flat():
    out = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inherited = list(r.include_context.dependencies)
            for sub in r.original_router.routes:
                out.append((sub.path, set(sub.methods or ()),
                            inherited + list(sub.dependencies)))
        elif hasattr(r, "path"):
            out.append((r.path, set(getattr(r, "methods", None) or ()),
                        list(getattr(r, "dependencies", []))))
    return out


def note(i, when, meta=None, content="Title line\n\nbody"):
    return {"id": i, "content": content, "meta": meta if meta is not None else "{}",
            "occurred_at": when, "note": "exploration", "case_id": 5, "case_title": "Insights"}


def wev(i, when, severity="warning"):
    return {"id": i, "severity": severity, "summary": "Revenue fell", "created_at": when,
            "watch_id": 3, "watch_title": "Revenue watch", "compiled_sql": "SELECT 1", "watch_origin": "exploration"}


def test_route_registered_with_ops_key():
    hits = [t for t in _flat() if t[0] == "/api/insights"]
    assert hits and "GET" in hits[0][1]
    assert any(getattr(d, "dependency", None) is main._require_ops_key for d in hits[0][2])
    assert TestClient(main.app).get("/api/insights").status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [{"origin": "bogus"}, {"severity": "urgent"},
                                {"before": "yesterday"}])
async def test_bad_params_422(monkeypatch, kw):
    _wire(monkeypatch, Pool())
    with pytest.raises(HTTPException) as e:
        await insights_api.list_insights_route(**kw)
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_limit_clamped(monkeypatch):
    p = _wire(monkeypatch, Pool())
    await insights_api.list_insights_route(limit=0)
    assert all(a[-1] == 1 for _, a in p.calls)
    p.calls.clear()
    await insights_api.list_insights_route(limit=999)
    assert all(a[-1] == 200 for _, a in p.calls)


@pytest.mark.asyncio
async def test_note_mapping_decodes_meta(monkeypatch):
    meta = json.dumps({"title": "Late payers", "finding": "F", "severity": "warning",
                       "evidence": {"row_count": 2, "columns": ["a"], "sample": [{"a": 1}],
                                    "truncated": False},
                       "evidence_sql": "SELECT a FROM t", "task_run_id": 377})
    _wire(monkeypatch, Pool(notes=[note(812, ts(20), meta)]))
    out = await insights.list_insights()
    it = out["items"][0]
    assert it["type"] == "note" and it["id"] == "note:812"
    assert it["title"] == "Late payers" and it["severity"] == "warning"
    assert isinstance(it["evidence"], dict) and it["evidence_sql"] == "SELECT a FROM t"
    assert it["task_run_id"] == 377 and it["case_id"] == 5 and it["case_title"] == "Insights"
    assert it["created_at"] == ts(20).isoformat()


@pytest.mark.asyncio
async def test_note_without_meta(monkeypatch):
    _wire(monkeypatch, Pool(notes=[note(1, ts(20), "{}", content="First line\nmore")]))
    it = (await insights.list_insights(origin="investigation"))["items"][0]
    assert it["title"] == "First line" and it["finding"] == "First line\nmore"
    assert it["severity"] == "info" and it["evidence"] is None and it["evidence_sql"] is None


@pytest.mark.asyncio
async def test_watch_event_mapping(monkeypatch):
    _wire(monkeypatch, Pool(watch=[wev(19, ts(21))]))
    it = (await insights.list_insights())["items"][0]
    assert it["type"] == "watch_event" and it["id"] == "watch_event:19"
    assert it["title"] == "Revenue watch" and it["finding"] == "Revenue fell"
    assert it["evidence_sql"] == "SELECT 1" and it["watch_id"] == 3 and it["evidence"] is None


@pytest.mark.asyncio
async def test_merge_order_and_cursor(monkeypatch):
    _wire(monkeypatch, Pool(notes=[note(2, ts(22)), note(1, ts(20))],
                            watch=[wev(9, ts(21)), wev(8, ts(19))]))
    out = await insights.list_insights(limit=3)
    assert [i["id"] for i in out["items"]] == ["note:2", "watch_event:9", "note:1"]
    assert out["next_before"] == out["items"][2]["created_at"]
    out = await insights.list_insights(limit=50)
    assert len(out["items"]) == 4 and out["next_before"] is None


@pytest.mark.asyncio
async def test_origin_filters_in_sql(monkeypatch):
    p = _wire(monkeypatch, Pool())
    await insights.list_insights(origin="exploration")
    n = [c for c in p.calls if "FROM case_events" in c[0]][0]
    w = [c for c in p.calls if "FROM watch_events" in c[0]]
    assert n[1][0] == ["exploration"] and "NOT c.sandbox" in n[0]
    assert w and "w.origin = 'exploration'" in w[0][0]
    p.calls.clear()
    await insights.list_insights(origin="investigation")
    assert not [c for c in p.calls if "FROM watch_events" in c[0]]
    p.calls.clear()
    await insights.list_insights(origin="all")
    n = [c for c in p.calls if "FROM case_events" in c[0]][0]
    w = [c for c in p.calls if "FROM watch_events" in c[0]][0]
    assert n[1][0] == ["exploration", "investigation"] and "w.origin = " not in w[0]


@pytest.mark.asyncio
async def test_never_runs_tenant_sql(monkeypatch):
    from app import guardrails

    async def boom(*a, **k):
        raise AssertionError("tenant SQL")
    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    _wire(monkeypatch, Pool(notes=[note(1, ts(20))]))
    out = await insights_api.list_insights_route()
    assert len(out["items"]) == 1
