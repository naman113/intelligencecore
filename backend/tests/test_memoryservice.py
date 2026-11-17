"""Phase 0 acceptance — MemoryService over Graphiti/FalkorDB.

Two layers:
  * Fail-soft tests run ANYWHERE (no graph needed): prove the service degrades
    to no-op writes / empty reads instead of raising.
  * The live acceptance test (add "ACME is our steel vendor since June", search,
    assert it returns) runs only when a FalkorDB is reachable; otherwise skipped.
    Enable it with the graph up:  RUN_GRAPH_TESTS=1 pytest tests/test_memoryservice.py
"""

import os
from datetime import datetime, timezone

import pytest

from app.memoryservice import MemoryService, memory_graph

pytestmark = pytest.mark.asyncio


async def test_disabled_graph_is_noop(monkeypatch):
    """memory_graph_enabled=False → writes no-op, reads empty, nothing raises."""
    from app import config
    monkeypatch.setattr(config.get_settings(), "memory_graph_enabled", False, raising=False)
    svc = MemoryService()
    assert await svc.add_episode("anything", source="test") is False
    assert await svc.search("anything") == []
    assert await svc.entity_context("ACME") == ""


async def test_unreachable_graph_fails_soft(monkeypatch):
    """A bad FalkorDB host must not crash callers — degrade to memory-less."""
    from app import config
    s = config.get_settings()
    monkeypatch.setattr(s, "memory_graph_enabled", True, raising=False)
    monkeypatch.setattr(s, "falkordb_host", "10.255.255.1", raising=False)  # unroutable
    monkeypatch.setattr(s, "falkordb_port", 6399, raising=False)
    svc = MemoryService()
    assert await svc.add_episode("x", source="test") is False
    # Second call is inside the retry backoff window — fast no-op, no raise.
    assert await svc.search("x") == []


async def test_add_episode_skips_empty():
    svc = MemoryService()
    assert await svc.add_episode("", source="test") is False
    assert await svc.add_episode("   ", source="test") is False


@pytest.mark.skipif(not os.getenv("RUN_GRAPH_TESTS"),
                    reason="needs a live FalkorDB (RUN_GRAPH_TESTS=1, docker compose up -d falkordb)")
async def test_live_add_and_search_roundtrip():
    """Plan §2 acceptance: add a fact, search, assert it returns."""
    when = datetime(2025, 6, 15, tzinfo=timezone.utc)
    ok = await memory_graph.add_episode(
        "ACME is our steel vendor since June.",
        source="test", occurred_at=when, group_id="test_phase0")
    assert ok, "episode should be stored when the graph is up"

    facts = await memory_graph.search("Who is our steel vendor?", scope=["test_phase0"])
    joined = " ".join(f["fact"] for f in facts).lower()
    assert "acme" in joined, f"expected ACME in graph search results, got: {facts}"
