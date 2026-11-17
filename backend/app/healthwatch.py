"""Health watch (Phase 11, GOLIVE-05).

Turns missed loop heartbeats, dead loop tasks and an unreachable business
database into health_alerts rows. "Once per state change" is a database
guarantee: a partial unique index allows one open alert per component, and the
INSERT ... ON CONFLICT DO NOTHING RETURNING id tells the caller whether this
sweep is the one that opened it (so only that sweep notifies). Never-die loop
convention, like worker.py and sentinel.py.

Alert text is fixed server strings; the detail is an exception class name or a
fixed phrase, so no DSN, SQL or agent text can reach the owner.
"""
import asyncio
import importlib.util
import json
import logging
import time

import asyncpg

from . import guardrails, heartbeat, jobruns  # Quick 008: jobruns
from . import warehouse  # Phase 14 (WH-08)
from .config import get_settings
from .db import get_pool

log = logging.getLogger("healthwatch")

CHECK_SECONDS = 30
STARTUP_GRACE = 180
DEADLINE_SLACK = 30
DB_FAIL_THRESHOLD = 2
DB_PROBE_TIMEOUT = 20

LOOP_COMPONENTS = ("worker", "sentinel", "dashboard_refresh")
COMPONENTS = LOOP_COMPONENTS + ("business_db",)

TITLES = {
    "worker": "The task worker has stopped making progress",
    "dashboard_refresh": "Dashboard refresh has stopped",
    "sentinel": "KPI watch checks have stopped",
    "business_db": "The business database is unreachable",
    # Quick 005: raised and resolved by usage.evaluate_budget_alert, not by this sweep.
    "llm_budget": "Today's AI spend is past 80% of the daily budget",
}
RECOVERED = {
    "worker": "The task worker is working again",
    "dashboard_refresh": "Dashboard refresh is working again",
    "sentinel": "KPI watch checks are working again",
    "business_db": "The business database is reachable again",
}

# ── Phase 14 (WH-08): warehouse component; business_db through the FDW in warehouse mode; 'unknown' while the warehouse is down (no double alert). Fixed strings only (GOLIVE-05). ──
COMPONENTS = COMPONENTS + ("warehouse",)
TITLES["warehouse"] = "The warehouse is unreachable"
RECOVERED["warehouse"] = "The warehouse is reachable again"
WH_TITLES = {"business_db": "The business database is unreachable through the warehouse"}
WH_RECOVERED = {"business_db": "The business database is reachable through the warehouse again"}
_wh_failures = 0
_wh_error = ""


def title(component: str) -> str:
    if component in WH_TITLES and warehouse.warehouse_mode():
        return WH_TITLES[component]
    return TITLES.get(component, "System health alert")


def recovered(component: str) -> str:
    if component in WH_RECOVERED and warehouse.warehouse_mode():
        return WH_RECOVERED[component]
    return RECOVERED.get(component, "A health check is working again")
# ── End Phase 14 (WH-08) ──

# ── Phase 15 (SRC-10): source alert titles (fixed strings; the component suffix is never shown) ──
_SRC_TITLES = {"source_stale:": ("A data source is out of date", "A data source is up to date again"),
               "source_failing:": ("A data source failed two syncs in a row", "A data source is syncing again"),
               "source_unreachable:": ("A data source cannot be reached", "A data source is reachable again")}
_title_p14, _recovered_p14 = title, recovered


def title(component: str) -> str:
    for pre, (t, _r) in _SRC_TITLES.items():
        if component.startswith(pre):
            return t
    return _title_p14(component)


def recovered(component: str) -> str:
    for pre, (_t, r) in _SRC_TITLES.items():
        if component.startswith(pre):
            return r
    return _recovered_p14(component)
# ── End Phase 15 (SRC-10) ──

OPEN_SQL = ("INSERT INTO health_alerts (component, detail) VALUES ($1, $2) "
            "ON CONFLICT (component) WHERE resolved_at IS NULL DO NOTHING RETURNING id")
RESOLVE_SQL = ("UPDATE health_alerts SET resolved_at = now() "
               "WHERE component=$1 AND resolved_at IS NULL RETURNING id")

_started_at = time.monotonic()
_db_failures = 0
_db_error = ""
_last_checks: dict = {}
_monitored: tuple = ()


def _monitored_loops() -> tuple:
    loops = ["worker", "sentinel"]
    try:
        if importlib.util.find_spec("app.dashboard_refresh") is not None:
            loops.append("dashboard_refresh")
    except Exception:  # noqa: BLE001
        pass
    return tuple(loops)


def _db_configured() -> bool:
    s = get_settings()
    return bool(s.tenant_database_url or s.business_database_url)


def evaluate(snapshot: dict, now: float, started_at: float, db_state: dict,
             monitored: tuple = LOOP_COMPONENTS, wh_state: dict | None = None) -> dict:
    """Pure: {component: {"state": ok|failing|starting|unknown, "detail": str}}."""
    out: dict = {}
    for name in monitored:
        s = snapshot.get(name)
        if not s or not s.get("seen"):
            if now - started_at <= STARTUP_GRACE:
                out[name] = {"state": "starting", "detail": "waiting for first heartbeat"}
            else:
                out[name] = {"state": "failing", "detail": "never started"}
        elif not s.get("alive"):
            out[name] = {"state": "failing", "detail": "loop task is not running"}
        elif now > s["deadline"] + DEADLINE_SLACK:
            out[name] = {"state": "failing",
                         "detail": f"no progress for {int(now - s['deadline'])} s"}
        else:
            out[name] = {"state": "ok", "detail": ""}
    wh_on = bool(wh_state and wh_state.get("configured"))  # Phase 14 (WH-08)
    if wh_state is not None:  # Phase 14 (WH-08)
        if not wh_on:
            out["warehouse"] = {"state": "ok", "detail": "not configured"}
        elif wh_state.get("failures", 0) >= DB_FAIL_THRESHOLD:
            out["warehouse"] = {"state": "failing", "detail": wh_state.get("error") or "unreachable"}
        else:
            out["warehouse"] = {"state": "ok", "detail": ""}
    if not db_state.get("configured"):
        out["business_db"] = {"state": "ok", "detail": "not configured"}
    elif wh_on and wh_state.get("failures", 0) >= 1:  # Phase 14: not-configured rule above wins
        out["business_db"] = {"state": "unknown", "detail": "waiting for the warehouse"}
    elif db_state.get("failures", 0) >= DB_FAIL_THRESHOLD:
        detail = db_state.get("error") or "unreachable"
        if wh_on:  # Phase 14
            detail = f"unreachable through the warehouse ({detail})"
        out["business_db"] = {"state": "failing", "detail": detail}
    else:
        out["business_db"] = {"state": "ok", "detail": ""}
    return out


async def _probe_business_db() -> None:
    """Update the consecutive-failure count. Error text is the class name only."""
    global _db_failures, _db_error
    if not _db_configured():
        _db_failures, _db_error = 0, ""
        return
    try:
        sql = ("SELECT n FROM ic_probe.business_db_ping"  # Phase 14 (WH-08)
               if warehouse.warehouse_mode() else "SELECT 1")
        await asyncio.wait_for(guardrails.run_readonly_sql(sql), DB_PROBE_TIMEOUT)
        _db_failures, _db_error = 0, ""
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - includes TimeoutError
        _db_failures += 1
        _db_error = type(e).__name__


async def _probe_warehouse() -> None:  # Phase 14 (WH-08)
    """Own short-lived wh_reader connection; never guardrails.run_readonly_sql, so it is
    independent of the business DB URL. Error text is the class name only."""
    global _wh_failures, _wh_error
    if not warehouse.warehouse_mode():
        _wh_failures, _wh_error = 0, ""
        return
    async def _go() -> None:
        conn = await warehouse.connect("reader", timeout=10)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
    try:
        await asyncio.wait_for(_go(), DB_PROBE_TIMEOUT)
        _wh_failures, _wh_error = 0, ""
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - includes TimeoutError
        _wh_failures += 1
        _wh_error = type(e).__name__


async def _notify(component: str, alert_id: int, failing: bool, detail: str = "") -> None:
    """One WhatsApp message per state change; one actions row per send. Never raises."""
    try:
        from . import configservice, people, whatsapp
        if not configservice._group_configured("whatsapp"):
            return
        person = await people.primary_person()
        phone = (person or {}).get("whatsapp")
        if not phone:
            return
        if failing:
            body = (f"Intelligence Corner: {title(component)}. "  # Phase 14
                    f"{detail}. Open System > Health in the app.")
        else:
            body = f"Intelligence Corner: {recovered(component)}."  # Phase 14
        try:
            ok = bool(await whatsapp.send_message(phone, body))
        except Exception:  # noqa: BLE001
            log.exception("health notification send failed for %s", component)
            ok = False
        pool = await get_pool()
        action_id = await pool.fetchval(
            """INSERT INTO actions (kind, payload, tier, status, executed_at)
               VALUES ('whatsapp_message', $1::jsonb, 'notify',
                       CASE WHEN $2 THEN 'executed' ELSE 'failed' END,
                       CASE WHEN $2 THEN now() END) RETURNING id""",
            json.dumps({"health_alert_id": alert_id, "component": component,
                        "body": body, "phone": phone}), ok)
        col = "notify_action_id" if failing else "resolve_action_id"
        await pool.execute(f"UPDATE health_alerts SET {col} = $2 WHERE id = $1",
                           alert_id, action_id)
    except Exception:  # noqa: BLE001
        log.exception("health notification failed for %s", component)


async def sweep() -> dict:
    global _last_checks, _monitored
    if not _monitored:
        _monitored = _monitored_loops()
    await _probe_warehouse()  # Phase 14 (WH-08)
    # Phase 14: a probe through a dead warehouse would blame the business database; skip it.
    if not (warehouse.warehouse_mode() and _wh_failures >= 1):
        await _probe_business_db()
    checks = evaluate(
        heartbeat.snapshot(), time.monotonic(), _started_at,
        {"configured": _db_configured(), "failures": _db_failures, "error": _db_error},
        _monitored,
        wh_state={"configured": warehouse.warehouse_mode(),  # Phase 14 (WH-08)
                  "failures": _wh_failures, "error": _wh_error})
    _last_checks = checks
    pool = await get_pool()
    for comp, c in checks.items():
        if c["state"] == "failing":
            alert_id = await pool.fetchval(OPEN_SQL, comp, c["detail"])
            if alert_id is not None:
                log.warning("health alert opened: %s (%s)", comp, c["detail"])
                await jobruns.record("health", TITLES.get(comp, comp), status="failed",  # Quick 008
                                     error=c["detail"] or "failing", trigger="health check",
                                     summary="Alert opened", ref=("health", comp))
                await _notify(comp, alert_id, True, c["detail"])
        elif c["state"] == "ok":
            alert_id = await pool.fetchval(RESOLVE_SQL, comp)
            if alert_id is not None:
                log.info("health alert resolved: %s", comp)
                await jobruns.record("health", TITLES.get(comp, comp), trigger="health check",  # Quick 008
                                     summary=RECOVERED.get(comp, "Working again"),
                                     ref=("health", comp))
                await _notify(comp, alert_id, False)
    return checks


async def run_forever() -> None:
    global _started_at
    _started_at = time.monotonic()
    log.info("health watch started")
    while True:
        try:
            await sweep()
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("health watch DB connection blip, retrying: %s", type(e).__name__)
        except Exception as e:
            log.exception("health watch sweep error")
            await jobruns.loop_error("healthwatch", e)  # Quick 008 (never raises)
        await asyncio.sleep(CHECK_SECONDS)
