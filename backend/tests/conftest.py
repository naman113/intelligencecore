"""Test-wide safety (Quick 009).

The suite is pure logic and must never reach a real FalkorDB. The memory graph
now retries a failed init on a backoff schedule instead of latching after one
attempt, so a test that touches memory_graph with default settings would keep
dialling localhost:6379, which on a dev machine can be a live install's graph.
Point the default host at a reserved name that never resolves; live graph runs
(RUN_GRAPH_TESTS=1) set FALKORDB_HOST explicitly and win over this default.
"""

import os

os.environ.setdefault("FALKORDB_HOST", "falkordb.invalid")


# ── Quick 039: triage's in-memory retry gate must not leak between tests ──
import sys  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _quick039_fresh_triage_gate():
    """A test whose fake triage model fails or refuses leaves triage backing off;
    a later test calling triage would then see no model call at all."""
    yield
    triage = sys.modules.get("app.triage")
    if triage is not None and hasattr(triage, "_gate"):
        triage._gate.reset()
        triage._requeue_mark = 0
