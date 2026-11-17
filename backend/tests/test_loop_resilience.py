"""Ambient loops must survive a failing sweep.

These loops run for the lifetime of the process with nothing watching their
exceptions (main.py stores the tasks but never retrieves results), so a single
escaping exception silently disables a whole subsystem while the dashboard
keeps showing it as healthy. The sentinel shipped exactly that bug: a NameError
raised while EVALUATING the `except (asyncpg.PostgresConnectionError, ...)`
tuple is not caught by the `except Exception` below it — it kills run_forever.
"""

import asyncio

import pytest

from app import docintel, sentinel


async def _run_briefly(coro_fn, seconds: float = 0.05):
    """Run a forever-loop for a moment; return True if it was still alive."""
    task = asyncio.create_task(coro_fn())
    await asyncio.sleep(seconds)
    alive = not task.done()
    if task.done() and task.exception() is not None:
        task.result()          # re-raise so the test failure names the cause
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return alive


@pytest.mark.asyncio
async def test_sentinel_survives_a_raising_sweep(monkeypatch):
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ConnectionError("DB went away mid-sweep")

    monkeypatch.setattr(sentinel, "_due_watches", boom)
    monkeypatch.setattr(sentinel, "POLL_INTERVAL", 0.01)

    assert await _run_briefly(sentinel.run_forever), "sentinel died on a sweep error"
    assert calls["n"] >= 1, "sweep never ran"


@pytest.mark.asyncio
async def test_sentinel_handler_names_are_all_importable(monkeypatch):
    """Guards the specific defect: every name in the except clause must resolve.

    A generic `raise Exception` would pass even with the name missing, because
    the tuple is only evaluated when an exception is in flight and the broken
    lookup happens before `except Exception` can catch it.
    """
    async def boom():
        raise OSError("socket closed")

    monkeypatch.setattr(sentinel, "_due_watches", boom)
    monkeypatch.setattr(sentinel, "POLL_INTERVAL", 0.01)

    assert await _run_briefly(sentinel.run_forever), \
        "sentinel died — an exception-handler name is unresolvable"


def _quiet_docintel(monkeypatch, tmp_path, drain):
    """Point the docintel loop at an empty inbox, a fast tick, and `drain`."""
    async def no_inbox():
        pass

    monkeypatch.setattr(docintel, "_inbox_dir", lambda: tmp_path)
    monkeypatch.setattr(docintel, "_poll_inbox", no_inbox)
    monkeypatch.setattr(docintel, "_drain_pending", drain)
    monkeypatch.setattr(docintel, "INBOX_POLL_SECONDS", 0.01)


@pytest.mark.asyncio
async def test_docintel_survives_a_raising_drain(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ConnectionError("DB went away mid-drain")

    _quiet_docintel(monkeypatch, tmp_path, boom)

    assert await _run_briefly(docintel.run_forever), "docintel died on a drain error"
    assert calls["n"] >= 1, "drain never ran"


@pytest.mark.asyncio
async def test_docintel_handler_names_are_all_importable(monkeypatch, tmp_path):
    """Same defect class as the sentinel: every name in the narrow blip clause
    must resolve while an exception is already in flight."""
    async def boom():
        raise OSError("socket closed")

    _quiet_docintel(monkeypatch, tmp_path, boom)

    assert await _run_briefly(docintel.run_forever), \
        "docintel died — an exception-handler name is unresolvable"


# Phase 11: GOLIVE-05 health watch must survive a failing sweep
@pytest.mark.asyncio
async def test_healthwatch_survives_a_raising_sweep(monkeypatch):
    from app import healthwatch
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ConnectionError("DB went away mid-sweep")

    monkeypatch.setattr(healthwatch, "sweep", boom)
    monkeypatch.setattr(healthwatch, "CHECK_SECONDS", 0.01)

    assert await _run_briefly(healthwatch.run_forever), "healthwatch died on a sweep error"
    assert calls["n"] >= 1


# Phase 14: the warehouse loop must survive a failing tick (WH-09)
@pytest.mark.asyncio
async def test_warehouse_survives_a_raising_tick(monkeypatch):
    from app import warehouse
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        if calls["n"] % 2:
            raise ConnectionError("warehouse went away")
        raise RuntimeError("unexpected")

    async def no_state():
        return False

    monkeypatch.setattr(warehouse, "_tick", boom)
    monkeypatch.setattr(warehouse, "load_link_state", no_state)
    monkeypatch.setattr(warehouse, "WAREHOUSE_TICK_SECONDS", 0.01)

    assert await _run_briefly(warehouse.run_forever), "warehouse loop died on a tick error"
    assert calls["n"] >= 1


def test_main_ambient_loops_include_warehouse():
    from app import main, warehouse
    assert warehouse.run_forever in main._ambient_loops()


# Phase 15: the source sync scheduler must survive a failing sweep (SRC-05)
@pytest.mark.asyncio
async def test_syncsched_survives_a_raising_sweep(monkeypatch):
    from app import syncsched
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        if calls["n"] % 2:
            raise ConnectionError("db went away")
        raise RuntimeError("unexpected")

    class S:
        sync_sweep_seconds = 0.01

    monkeypatch.setattr(syncsched, "sweep", boom)
    monkeypatch.setattr(syncsched, "get_settings", lambda: S())
    assert await _run_briefly(syncsched.run_forever), "sync scheduler died on a sweep error"
    assert calls["n"] >= 1


def test_main_ambient_loops_include_syncsched():
    from app import main, syncsched
    assert syncsched.run_forever in main._ambient_loops()
