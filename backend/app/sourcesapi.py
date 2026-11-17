"""Phase 15 (SRC-02, SRC-10): sources registry reads and the owner-only credentials routes.
Mounted with the ops key in main.py. No response ever carries a secret."""
import datetime
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import secretbox, sourcealerts, sourcesvc, sourcevault, whsources
from .db import get_pool

log = logging.getLogger("sourcesapi")
router = APIRouter(prefix="/api/sources", tags=["sources"])

KEY_MISSING = "Credential storage needs SETTINGS_ENCRYPTION_KEY (install.sh creates it)"


class CredentialBody(BaseModel):
    name: str
    kind: str
    host: str
    port: int
    database: str
    user: str
    password: str = ""
    options: dict | None = None


def _jd(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


@router.post("/credentials")
async def save_credential(body: CredentialBody):
    fields = body.model_dump(exclude={"name", "kind"})
    try:
        out = await sourcevault.save(body.name, body.kind, fields)
        # Phase 15 (15-08): keep live mappings in step with the vault
        try:
            res = await whsources.refresh_mapping(body.name)
            if res.get("status") not in ("ok",):
                out = dict(out, mapping_refresh="failed")
        except Exception as e:
            log.warning("mapping refresh failed: %s", type(e).__name__)
            out = dict(out, mapping_refresh="failed")
        # End Phase 15 (15-08)
        return out
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except secretbox.KeyMissing:
        raise HTTPException(409, KEY_MISSING) from None


@router.get("/credentials")
async def list_credentials():
    return {"credentials": await sourcevault.list_handles()}


@router.post("/credentials/{name}/verify")
async def verify_credential(name: str):
    try:
        return await sourcevault.verify(name)
    except LookupError:
        raise HTTPException(404, "unknown credential") from None
    except secretbox.KeyMissing:
        raise HTTPException(409, KEY_MISSING) from None
    except secretbox.Undecryptable:
        raise HTTPException(409, "Stored credential cannot be decrypted with this server's key") from None


@router.delete("/credentials/{name}")
async def delete_credential(name: str):
    try:
        return await sourcevault.delete(name)
    except LookupError:
        raise HTTPException(404, "unknown credential") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


def _run_view(r) -> dict:
    d = dict(r)
    d["tables"] = _jd(d.get("tables")) or []
    if d.get("error"):
        d["error"] = secretbox.redact(d["error"])
    return d


@router.get("")
async def list_sources():
    srcs = await sourcesvc.list_sources()
    pool = await get_pool()
    creds = {c["name"]: c for c in await sourcevault.list_handles()}
    last = {r["source_id"]: dict(r) for r in await pool.fetch(
        """SELECT DISTINCT ON (source_id) id, source_id, kind, status, started_at, ended_at,
                  rows_loaded, error FROM sync_runs ORDER BY source_id, id DESC""")}
    ok = {r["source_id"]: r["t"] for r in await pool.fetch(
        """SELECT source_id, max(ended_at) t FROM sync_runs
            WHERE status IN ('ok','partial') GROUP BY source_id""")}
    # Phase 15 (15-13): same rule as the alerts
    fin_runs, _ok = await sourcealerts.finished_runs(pool)
    unreach = {r["component"].removeprefix(sourcealerts.UNREACHABLE_PREFIX) for r in await pool.fetch(
        "SELECT component FROM health_alerts WHERE resolved_at IS NULL "
        "AND component LIKE 'source\\_unreachable:%'")}
    now = datetime.datetime.now(datetime.timezone.utc)
    # End Phase 15 (15-13)
    out = []
    for s in srcs:
        dec = sourcealerts.decide(dict(s, last_success_at=ok.get(s["id"])),
                                  fin_runs.get(s["id"], []), now)  # Phase 15 (15-13)
        cname = (s.get("credential") or "").removeprefix("cred:") or None
        cred = creds.get(cname) if cname else None
        lr = last.get(s["id"])
        if lr:
            lr = {k: lr[k] for k in ("id", "kind", "status", "started_at", "ended_at", "rows_loaded")}
            lr["error"] = secretbox.redact(last[s["id"]]["error"]) if last[s["id"]]["error"] else None
        tables = [{
            "warehouse_schema": t["warehouse_schema"], "warehouse_table": t["warehouse_table"],
            "remote_schema": t["remote_schema"], "remote_table": t["remote_table"],
            "mode": t["mode"], "cursor_column": t["cursor_column"], "merge_key": t["merge_key"],
            "status": t["status"], "last_success_at": t["last_success_at"],
            "rows_total": t["rows_total"], "backfill_done": t["backfill_done"],
            "backfill_progress": t["backfill_progress"],
            "pending_mode": t.get("pending_mode"),  # Phase 17 (CONS-05)
            "last_error": secretbox.redact(t["last_error"]) if t["last_error"] else None,
        } for t in s.get("tables", [])]
        prog = [t["backfill_progress"] for t in tables if t["backfill_progress"] is not None]
        out.append({
            "name": s["name"], "kind": s["kind"], "status": s["status"],
            "credential": ("cred:" + cname) if cname else None,
            "display": cred["display"] if cred else None,
            "warehouse_schema": s.get("warehouse_schema"), "schedule": s.get("schedule"),
            "freshness_minutes": s.get("freshness_minutes"), "next_sync_at": s.get("next_sync_at"),
            "last_run": lr, "last_success_at": ok.get(s["id"]),
            "backfill": {"done": all(t["backfill_done"] for t in tables) if tables else True,
                         "progress": (sum(prog) / len(prog)) if prog else None},
            "tables": tables,
            "stale": dec["stale"], "failing": dec["failing"],  # Phase 15 (15-13)
            "unreachable": s["name"] in unreach,  # Phase 15 (15-13)
        })
    return {"sources": out}


@router.get("/{name}/runs")
async def source_runs(name: str, limit: int = 20):
    limit = max(1, min(100, int(limit)))
    src = await sourcesvc.get_source(name)
    if src is None:
        raise HTTPException(404, "unknown source")
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, kind, status, started_at, ended_at, duration_ms, rows_loaded, tables, error
             FROM sync_runs WHERE source_id = $1 ORDER BY id DESC LIMIT $2""", src["id"], limit)
    return {"runs": [_run_view(r) for r in rows]}
