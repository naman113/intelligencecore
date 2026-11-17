"""Quick 027 (audit #7): configservice.save_many coerces non-string JSON values."""
import os

import pytest

from app import configservice as cs
from app.config import get_settings


class Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.fixture
def pool(monkeypatch):
    p = Pool()

    async def gp():
        return p

    async def noinv(groups):
        return None

    monkeypatch.setattr(cs, "get_pool", gp)
    monkeypatch.setattr(cs, "_invalidate", noinv)
    monkeypatch.delenv("REACTIVE_TOKEN_BUDGET", raising=False)
    get_settings.cache_clear()
    yield p
    os.environ.pop("REACTIVE_TOKEN_BUDGET", None)
    get_settings.cache_clear()


def test_as_text_rules():
    assert cs._as_text(400000) == "400000"
    assert cs._as_text(1.5) == "1.5"
    assert cs._as_text(True) == "true" and cs._as_text(False) == "false"
    assert cs._as_text(None) == "" and cs._as_text("  x ") == "x"
    for bad in ({"a": 1}, [1]):
        with pytest.raises(ValueError):
            cs._as_text(bad)


async def test_save_many_accepts_int_like_scenario_dashboard_agent(pool):
    # scenario_dashboard_agent.py step 7b sends {"reactive_token_budget": 400000}
    assert await cs.save_many({"reactive_token_budget": 400000}) == ["reactive_token_budget"]
    assert os.environ["REACTIVE_TOKEN_BUDGET"] == "400000"
    assert pool.executed[0][1][1] == "400000"


async def test_save_many_int_still_validated(pool):
    with pytest.raises(ValueError):
        await cs.save_many({"reactive_token_budget": {"x": 1}})
    assert pool.executed == []


async def test_save_many_none_clears_override(pool):
    await cs.save_many({"reactive_token_budget": None})
    assert "DELETE FROM system_configs" in pool.executed[0][0]
