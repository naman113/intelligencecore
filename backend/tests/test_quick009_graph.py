"""Quick 009 — the knowledge graph recovers on its own and says truthfully
whether it is up.

Pure logic: graphiti's classes are replaced by fakes, no FalkorDB is dialled.
Proves:
  * a failed init is retried after a backoff (30 s doubling to 300 s), not
    latched for the process lifetime; inside the window calls are fast no-ops
  * success resets the failure count; close()/reconnect() retry immediately
  * a connection-type error on a write, or a failed ping, marks the graph down
  * status() reports connected / empty / not_connected (reason) / disabled
  * run_forever survives a tick that raises (loops never die)
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import memoryservice
from app.memoryservice import MemoryService

class FakeClient:
    def __init__(self, graphs=None, ping_error=None):
        self.graphs = graphs or []
        self.ping_error = ping_error

    async def list_graphs(self):
        if self.ping_error:
            raise self.ping_error
        return list(self.graphs)


class FakeFalkorDriver:
    """Named so memoryservice's `"falkor" in type(d).__name__` check passes."""

    def __init__(self, host=None, port=None, client=None, counts=None):
        self.client = client or FakeClient()
        self.counts = counts or {}
        self.database = "default_db"

    def clone(self, database):
        d = FakeFalkorDriver(client=self.client, counts=self.counts)
        d.database = database
        return d

    async def execute_query(self, cypher, **params):
        if "count(e)" in cypher and "WHERE" not in cypher:
            return ([{"n": self.counts.get(self.database, 0)}], ["n"], None)
        return ([{"n": 0}], ["n"], None)



class FakeGraphiti:
    build_error: Exception | None = None
    instances: list = []

    def __init__(self, graph_driver=None, llm_client=None, **kw):
        self.driver = graph_driver
        self.closed = False
        FakeGraphiti.instances.append(self)

    async def build_indices_and_constraints(self):
        if FakeGraphiti.build_error is not None:
            raise FakeGraphiti.build_error

    async def close(self):
        self.closed = True

    async def add_episode(self, **kw):
        raise RedisConnectionError("Connection refused")


class RedisConnectionError(Exception):
    """Same class name as redis-py's, which does not subclass the builtin."""


RedisConnectionError.__name__ = "ConnectionError"


@pytest.fixture
def fake_graphiti(monkeypatch):
    import graphiti_core
    import graphiti_core.driver.falkordb_driver as fd

    FakeGraphiti.build_error = None
    FakeGraphiti.instances = []
    monkeypatch.setattr(graphiti_core, "Graphiti", FakeGraphiti)
    monkeypatch.setattr(fd, "FalkorDriver", FakeFalkorDriver)
    monkeypatch.setattr(memoryservice, "get_settings", lambda: SimpleNamespace(
        memory_graph_enabled=True, falkordb_host="falkordb", falkordb_port=6379, falkordb_password="",
        graphiti_llm="auto", anthropic_api_key="", openai_api_key="sk-test",
        graph_search_recipe="rrf"))
    return FakeGraphiti


def test_backoff_schedule():
    assert [memoryservice._backoff(n) for n in range(1, 8)] == \
        [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]


async def test_failed_init_is_retried_after_backoff_not_latched(fake_graphiti):
    fake_graphiti.build_error = RuntimeError(
        "Received 5 arguments to procedure 'db.idx.fulltext.createNodeIndex', expected at most 1")
    svc = MemoryService()
    assert await svc._ensure() is False
    assert len(fake_graphiti.instances) == 1
    assert fake_graphiti.instances[0].closed            # half-built client released
    assert "createNodeIndex" in svc._last_error
    wait = svc._next_attempt_at - time.monotonic()
    assert 25 < wait <= 30

    # Inside the window: fast no-op, no new connection attempt.
    assert await svc.add_episode("x is y", source="test") is False
    assert await svc.search("x") == []
    assert len(fake_graphiti.instances) == 1

    # Window passes, the store is fixed: the next call reconnects by itself.
    fake_graphiti.build_error = None
    svc._next_attempt_at = time.monotonic() - 1
    assert await svc._ensure() is True
    assert svc._failures == 0 and svc._last_error == ""
    assert len(fake_graphiti.instances) == 2


async def test_second_failure_doubles_the_wait(fake_graphiti):
    fake_graphiti.build_error = OSError("Connection refused")
    svc = MemoryService()
    await svc._ensure()
    svc._next_attempt_at = time.monotonic() - 1
    await svc._ensure()
    assert svc._failures == 2
    assert 55 < svc._next_attempt_at - time.monotonic() <= 60


async def test_reconnect_skips_the_backoff(fake_graphiti):
    fake_graphiti.build_error = OSError("Connection refused")
    svc = MemoryService()
    await svc._ensure()
    fake_graphiti.build_error = None
    assert await svc.reconnect() is True


async def test_connection_error_on_write_marks_graph_down(fake_graphiti):
    svc = MemoryService()
    assert await svc._ensure() is True
    # FakeGraphiti.add_episode raises a redis-style ConnectionError.
    assert await svc.add_episode("ACME supplies steel", source="test") is False
    assert svc._available is False
    assert "ConnectionError" in svc._last_error
    assert svc._next_attempt_at > time.monotonic()


async def test_other_errors_leave_graph_up(fake_graphiti):
    svc = MemoryService()
    await svc._ensure()
    await svc._note_error(ValueError("bad extraction"))
    assert svc._available is True


async def test_failed_ping_marks_down_and_status_says_why(fake_graphiti):
    svc = MemoryService()
    await svc._ensure()
    svc._graphiti.driver.client.ping_error = RedisConnectionError("Error 111 connecting")
    st = await svc.status()
    assert st["state"] == "not_connected"
    assert "Error 111" in st["reason"]
    assert 0 < st["next_retry_in"] <= 30


async def test_status_connected_and_empty(fake_graphiti):
    svc = MemoryService()
    st = await svc.status()                      # probe connects
    assert st["state"] == "empty" and st["episodes"] == 0

    client = FakeClient(graphs=["global", "chat_1", "someone_elses"])
    svc._graphiti.driver = FakeFalkorDriver(client=client, counts={"global": 3, "chat_1": 2})
    st = await svc.status()
    assert st["state"] == "connected"
    assert st["episodes"] == 5 and st["scopes"] == 2
    assert st["connected_since"]


async def test_status_disabled(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: SimpleNamespace(
        memory_graph_enabled=False, falkordb_host="h", falkordb_port=1))
    st = await MemoryService().status()
    assert st["state"] == "disabled" and "memory_graph_enabled" in st["reason"]


async def test_has_episode_matches_name_or_content(fake_graphiti):
    svc = MemoryService()
    await svc._ensure()
    assert await svc.has_episode("global", name="learning_1_r0") is False  # no graph yet

    class HitFalkorDriver(FakeFalkorDriver):
        async def execute_query(self, cypher, **params):
            hit = params.get("v") == "learning_1_r0"
            return ([{"n": 1 if hit else 0}], ["n"], None)

    svc._graphiti.driver = HitFalkorDriver(client=FakeClient(graphs=["global"]))
    svc._graphiti.driver.clone = lambda database: svc._graphiti.driver
    assert await svc.has_episode("global", name="learning_1_r0", content="zzz") is True
    assert await svc.has_episode("global", name="learning_9_r0", content="zzz") is False


async def test_run_forever_survives_a_failing_tick(monkeypatch):
    svc = MemoryService()
    ticks = []

    async def boom():
        ticks.append(1)
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(svc, "watch_once", boom)
    monkeypatch.setattr(memoryservice, "WATCH_SECONDS", 0.01)
    task = asyncio.create_task(svc.run_forever())
    await asyncio.sleep(0.08)
    assert not task.done(), "memory graph watch died on a tick error"
    task.cancel()
    assert len(ticks) >= 2


def test_reason_is_one_short_line():
    r = memoryservice._reason(RuntimeError("line one\nline two " + "x" * 500))
    assert r.startswith("RuntimeError: line one") and "\n" not in r
    assert len(r) <= memoryservice.REASON_MAX


def test_main_starts_the_graph_watch_loop():
    from app import main
    names = [getattr(f, "__qualname__", "") for f in main._ambient_loops()]
    assert "MemoryService.run_forever" in names


def test_extraction_models_are_pinned_not_graphiti_defaults():
    """graphiti-core 0.29.2 defaults its small model and reranker to
    gpt-4.1-nano, which OpenAI rejects; every add_episode then failed."""
    s = SimpleNamespace(graphiti_llm="auto", anthropic_api_key="", openai_api_key="sk-test")
    llm = MemoryService._llm_client(s)
    assert llm is not None
    assert llm.small_model == memoryservice.GRAPHITI_OPENAI_SMALL_MODEL != "gpt-4.1-nano"
    assert llm.model == memoryservice.GRAPHITI_OPENAI_MODEL
    rr = MemoryService._cross_encoder(s)
    assert rr.config.model == memoryservice.GRAPHITI_RERANK_MODEL != "gpt-4.1-nano"
    emb = MemoryService._embedder(s)
    assert emb.config.embedding_model == "text-embedding-3-small"
    # Setup can move them without a code change.
    s2 = SimpleNamespace(graphiti_llm="auto", anthropic_api_key="", openai_api_key="sk-test",
                         graphiti_openai_model="gpt-5.5", graphiti_openai_small_model="gpt-5-nano")
    assert MemoryService._llm_client(s2).small_model == "gpt-5-nano"


def test_graph_model_settings_are_dashboard_editable():
    from app import configservice
    from app.config import Settings
    s = Settings(_env_file=None)
    for key in ("graphiti_llm", "graphiti_openai_model", "graphiti_openai_small_model"):
        assert configservice.REGISTRY[key]["group"] == "memory_graph"
    assert s.graphiti_openai_small_model != "gpt-4.1-nano"
