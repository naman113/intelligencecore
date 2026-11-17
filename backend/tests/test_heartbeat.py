"""Phase 11 (GOLIVE-05): heartbeat registry and the three beat sites."""
import asyncio
import time

import pytest

from app import dashboard_refresh, heartbeat, sentinel, worker


@pytest.fixture(autouse=True)
def _reset():
    heartbeat.reset()
    yield
    heartbeat.reset()


class _Stop(Exception):
    pass


async def _stop_sleep(*_a, **_k):
    raise _Stop


@pytest.mark.asyncio
async def test_beat_and_busy_deadlines():
    heartbeat.beat("worker", 30)
    s = heartbeat.snapshot()["worker"]
    assert s["alive"] and s["seen"]
    assert 25 < s["deadline"] - time.monotonic() <= 30.5
    heartbeat.busy("worker", 900)
    assert heartbeat.snapshot()["worker"]["deadline"] - time.monotonic() > 800
    heartbeat.beat("worker", 30)
    assert heartbeat.snapshot()["worker"]["deadline"] - time.monotonic() < 31


@pytest.mark.asyncio
async def test_max_over_tasks_and_dead_task():
    async def a():
        heartbeat.beat("w", 10)
        await asyncio.sleep(10)

    async def b():
        heartbeat.beat("w", 100)

    ta = asyncio.create_task(a())
    await asyncio.create_task(b())
    await asyncio.sleep(0)
    s = heartbeat.snapshot()["w"]
    assert s["alive"] and s["deadline"] - time.monotonic() > 90
    ta.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ta
    s = heartbeat.snapshot()["w"]
    assert s["alive"] is False and s["seen"] is True


@pytest.mark.asyncio
async def test_stop_cancels_and_unknown_is_zero():
    async def loop():
        heartbeat.beat("sentinel", 30)
        await asyncio.sleep(30)

    t = asyncio.create_task(loop())
    await asyncio.sleep(0.01)
    assert heartbeat.stop("sentinel") == 1
    with pytest.raises(asyncio.CancelledError):
        await t
    assert heartbeat.stop("nope") == 0


def test_outside_task_never_raises():
    heartbeat.beat("x", 5)
    heartbeat.busy("x", 5)
    heartbeat.beat(None, "bad")  # type: ignore[arg-type]
    assert heartbeat.snapshot()["x"]["seen"]


@pytest.mark.asyncio
async def test_worker_consume_beats_only_on_success(monkeypatch):
    async def once_false():
        return False

    monkeypatch.setattr(worker, "_process_one", once_false)
    monkeypatch.setattr(worker.asyncio, "sleep", _stop_sleep)
    with pytest.raises(_Stop):
        await worker._consume(0)
    assert "worker" in heartbeat.snapshot()

    heartbeat.reset()

    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(worker, "_process_one", boom)
    with pytest.raises(_Stop):
        await worker._consume(0)
    assert "worker" not in heartbeat.snapshot()


@pytest.mark.asyncio
async def test_sentinel_beats_on_clean_sweep_only(monkeypatch):
    async def none_due():
        return []

    monkeypatch.setattr(sentinel, "_due_watches", none_due)
    monkeypatch.setattr(sentinel.asyncio, "sleep", _stop_sleep)
    with pytest.raises(_Stop):
        await sentinel.run_forever()
    s = heartbeat.snapshot()["sentinel"]
    assert s["deadline"] - time.monotonic() > sentinel.POLL_INTERVAL

    heartbeat.reset()

    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(sentinel, "_due_watches", boom)
    with pytest.raises(_Stop):
        await sentinel.run_forever()
    # busy() ran before the failure, but no beat: deadline is the long busy one
    assert heartbeat.snapshot()["sentinel"]["deadline"] - time.monotonic() > 800


@pytest.mark.asyncio
async def test_dashboard_refresh_busy_then_beat(monkeypatch):
    calls = []
    monkeypatch.setattr(heartbeat, "busy", lambda n, w: calls.append(("busy", n, w)))
    monkeypatch.setattr(heartbeat, "beat", lambda n, w: calls.append(("beat", n, w)))

    async def ok():
        return False

    monkeypatch.setattr(dashboard_refresh, "refresh_due", ok)
    monkeypatch.setattr(dashboard_refresh.asyncio, "sleep", _stop_sleep)
    with pytest.raises(_Stop):
        await dashboard_refresh.run_forever()
    assert [c[0] for c in calls] == ["busy", "beat"]

    calls.clear()

    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(dashboard_refresh, "refresh_due", boom)
    with pytest.raises(_Stop):
        await dashboard_refresh.run_forever()
    assert [c[0] for c in calls] == ["busy"]


@pytest.mark.asyncio
async def test_repeated_busy_without_beat_does_not_renew_deadline():
    # CR-02: a loop that raises every iteration calls busy() each time and never beats.
    heartbeat.busy("looper", 900)
    first = heartbeat.snapshot()["looper"]["deadline"]
    time.sleep(0.05)
    heartbeat.busy("looper", 900)
    assert heartbeat.snapshot()["looper"]["deadline"] == first
    heartbeat.beat("looper", 30)
    heartbeat.busy("looper", 900)  # a beat re-arms busy
    assert heartbeat.snapshot()["looper"]["deadline"] - time.monotonic() > 800
