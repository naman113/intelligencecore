"""In-memory log ring buffer + live subscriber fan-out for the dashboard."""

import asyncio
import logging
import time
from collections import deque

from . import secretbox  # Phase 11: GOLIVE-02

BUFFER_SIZE = 500

_buffer: deque[dict] = deque(maxlen=BUFFER_SIZE)
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def _deliver(q: asyncio.Queue, entry: dict) -> None:
    """Push one entry, dropping the oldest if the subscriber has stalled.

    This must NEVER raise. A bare `q.put_nowait` on a full queue raises
    QueueFull inside a loop callback; asyncio's default handler logs that via
    logging.error, which re-enters BufferHandler.emit, which schedules another
    put_nowait on the same still-full queue — a self-feeding cycle that pins the
    event loop at 100% CPU and starves every ambient loop. The trigger is
    mundane: an operator leaves the Live-logs panel open and their laptop
    sleeps, so TCP backpressure stalls the SSE generator before its `finally`
    can unsubscribe.
    """
    try:
        q.put_nowait(entry)
    except asyncio.QueueFull:
        try:
            q.get_nowait()          # drop oldest, keep the tail moving
            q.put_nowait(entry)
        except Exception:
            pass
    except Exception:
        pass


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": self.format(record),
        }
        _buffer.append(entry)
        if _loop is not None:
            for q in list(_subscribers):
                try:
                    _loop.call_soon_threadsafe(_deliver, q, entry)
                except RuntimeError:
                    pass            # loop already closed (shutdown)


def install() -> None:
    """Attach the buffer handler to the root logger. Call once at startup."""
    global _loop
    _loop = asyncio.get_event_loop()
    handler = BufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    # Phase 11: GOLIVE-02 redaction; Quick 029 F7 widens it to every handler
    # (uvicorn's own, which do not propagate) and the last-resort handler.
    secretbox.install_log_redaction()
    secretbox.refresh_known_secrets()


def recent(limit: int = 200) -> list[dict]:
    return list(_buffer)[-limit:]


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)
