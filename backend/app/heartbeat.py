"""In-process loop heartbeat registry (Phase 11, GOLIVE-05).

A deadline is "the latest time by which this loop must beat again". Each loop
calls beat() after a successful iteration; before legitimately long work it
calls busy() with a longer deadline so the health watch does not raise a false
alert. healthwatch reads snapshot() and turns a missed deadline, or a dead
loop task, into one alert.

Stdlib only and no app imports, so any loop module can import it without a
cycle. Every public function swallows its own errors: a heartbeat must never
take down the loop it observes.
"""
import asyncio
import time

# (name, id(task) or 0) -> (deadline monotonic, task or None, busy_pending)
# busy_pending is True after busy() until the next beat(); while it is set, a
# further busy() does NOT renew the deadline, so a loop that announces busy and
# then fails every iteration still misses its deadline (CR-02).
_entries: dict[tuple[str, int], tuple[float, "asyncio.Task | None", bool]] = {}
_seen: set[str] = set()
_PRUNE_AFTER = 3600.0


def _current():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _record(name: str, within: float, is_busy: bool = False) -> None:
    now = time.monotonic()
    task = _current()
    key = (name, id(task) if task is not None else 0)
    prev = _entries.get(key)
    if is_busy and prev is not None and prev[2]:
        return  # busy again without an intervening beat: no progress, keep deadline
    _entries[key] = (now + float(within), task, is_busy)
    _seen.add(name)
    if len(_entries) > 64:
        for k, (dl, t, _b) in list(_entries.items()):
            if t is not None and t.done() and now - dl > _PRUNE_AFTER:
                _entries.pop(k, None)


def beat(name: str, within: float) -> None:
    """Record progress: this task must beat again within `within` seconds."""
    try:
        _record(name, within)
    except Exception:  # noqa: BLE001 - never raise into a loop
        pass


def busy(name: str, within: float) -> None:
    """Announce long work: extend this task's deadline to now + `within`."""
    try:
        _record(name, within, is_busy=True)
    except Exception:  # noqa: BLE001
        pass


def snapshot(now: float | None = None) -> dict[str, dict]:
    """{name: {"deadline", "alive", "seen"}}; deadline is the max over entries."""
    out: dict[str, dict] = {}
    try:
        for (name, _), (dl, task, _b) in list(_entries.items()):
            cur = out.setdefault(name, {"deadline": dl, "alive": False, "seen": True})
            cur["deadline"] = max(cur["deadline"], dl)
            if task is None or not task.done():
                cur["alive"] = True
        for name in _seen:
            out.setdefault(name, {"deadline": 0.0, "alive": False, "seen": True})
    except Exception:  # noqa: BLE001
        pass
    return out


def stop(name: str) -> int:
    """Cancel every registered task for `name`; return the count."""
    n = 0
    try:
        for (nm, _), (_dl, task, _b) in list(_entries.items()):
            if nm == name and task is not None and not task.done():
                task.cancel()
                n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


def reset() -> None:
    """Test helper: clear the registry."""
    _entries.clear()
    _seen.clear()
