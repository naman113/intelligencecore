"""Phase 8 (WR-01): kind / exclude_kind filters on GET /api/actions."""
import pytest
from fastapi import HTTPException

import app.main as main
from app import actionsvc


class FakePool:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return []


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()

    async def gp():
        return p
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    return p


async def test_default_no_filters(pool):
    assert await actionsvc.list_actions(status="proposed", limit=100) == []
    sql, args = pool.calls[0]
    assert args == (100, "proposed", None, None)
    assert "$3::text IS NULL" in sql and "$4::text IS NULL" in sql


async def test_exclude_kind(pool):
    await actionsvc.list_actions("proposed", 100, exclude_kind="context_change")
    sql, args = pool.calls[0]
    assert args == (100, "proposed", None, "context_change")
    assert "a.kind <> $4" in sql


async def test_kind(pool):
    await actionsvc.list_actions("proposed", 50, kind="publish_dashboard")
    sql, args = pool.calls[0]
    assert args == (50, "proposed", "publish_dashboard", None)
    assert "a.kind = $3" in sql


async def test_no_status_with_kind(pool):
    await actionsvc.list_actions(status="", kind="context_change")
    sql, args = pool.calls[0]
    assert args == (100, "context_change", None)
    assert "a.status" not in sql and "a.kind = $2" in sql


async def test_values_never_in_sql(pool):
    await actionsvc.list_actions("proposed", 10, kind="abc", exclude_kind="xyz")
    sql, _ = pool.calls[0]
    assert "abc" not in sql and "xyz" not in sql


@pytest.mark.parametrize("kw", [{"kind": "bad;drop"}, {"exclude_kind": "X"},
                                {"kind": "a" * 65}, {"exclude_kind": ""}])
async def test_malformed_422(kw):
    with pytest.raises(HTTPException) as e:
        await main.api_list_actions(**kw)
    assert e.value.status_code == 422


async def test_route_passthrough(pool):
    assert await main.api_list_actions() == []
    assert pool.calls[0][1] == (100, "proposed", None, None)
    await main.api_list_actions(kind="context_change", exclude_kind="x_y")
    assert pool.calls[1][1] == (100, "proposed", "context_change", "x_y")
