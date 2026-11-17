"""Health alerts API (Phase 11, GOLIVE-05). Mounted with the ops key in main.py."""
import logging
import os

from fastapi import APIRouter, HTTPException

from . import healthwatch, heartbeat
from .db import get_pool

log = logging.getLogger("healthapi")
router = APIRouter(tags=["health"])
# Test-only routes (Quick 029 F6). Mounted by main._mount_test_hooks only when
# the process has IC_TEST_HOOKS=1 in its environment.
test_router = APIRouter(tags=["test"])


def test_hooks_enabled(env=None) -> bool:
    """True only for IC_TEST_HOOKS == "1" in the process environment. Deliberately
    not a config.py / configservice setting, so the Setup page cannot turn it on."""
    env = os.environ if env is None else env
    return env.get("IC_TEST_HOOKS") == "1"


@router.get("/api/health/alerts")
async def health_alerts(include_resolved: bool = False, limit: int = 20):
    limit = max(1, min(100, int(limit)))
    pool = await get_pool()
    where = "" if include_resolved else "WHERE resolved_at IS NULL"
    rows = await pool.fetch(
        f"SELECT id, component, detail, created_at, resolved_at FROM health_alerts "
        f"{where} ORDER BY created_at DESC, id DESC LIMIT $1", limit)
    alerts = [{
        "id": r["id"], "component": r["component"],
        "title": healthwatch.title(r["component"]),  # Phase 14 (WH-08)
        "detail": r["detail"], "created_at": r["created_at"],
        "resolved_at": r["resolved_at"],
    } for r in rows]
    return {"alerts": alerts, "checks": healthwatch._last_checks}


@test_router.post("/api/test-stop-loop/{name}")
async def test_stop_loop(name: str):
    if name not in healthwatch.LOOP_COMPONENTS:
        raise HTTPException(404, "unknown loop")
    n = heartbeat.stop(name)
    log.warning("test hook stopped loop %s (%d task(s))", name, n)
    return {"stopped": n}
