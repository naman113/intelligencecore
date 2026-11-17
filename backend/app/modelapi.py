"""Read-only /api/model routes for the operator UIs (MODEL-07).

Approve and reject stay on /api/actions; model changes are requested by chat and
arrive as model_change proposals. Compiled SQL is shown, never run: this module
reads model rows and compiles, and does not query tenant data."""

import json

from fastapi import APIRouter, HTTPException

from . import db, modelcompiler, modeltools, semanticmodel

router = APIRouter(prefix="/api/model", tags=["model"])

TABLE_STATUSES = {"active", "retired", "all"}
_HIDDEN = ("meta", "action_id")


def _obj(v):
    """asyncpg returns jsonb as str: decode at the edge."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _list(v) -> list:
    v = _obj(v)
    return list(v) if isinstance(v, (list, tuple)) else []


def _dict(v) -> dict:
    v = _obj(v)
    return v if isinstance(v, dict) else {}


def _keep(row: dict, status: str) -> bool:
    return status == "all" or row.get("status") == status


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _HIDDEN}


@router.get("/tables")
async def list_tables(status: str = "active"):
    if status not in TABLE_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(TABLE_STATUSES)}")
    rows = await semanticmodel.load_rows(include_retired=status != "active")
    objects = await semanticmodel.warehouse_objects()
    tables = []
    for r in rows.get("tables") or []:
        if not _keep(r, status):
            continue
        t = _clean(dict(r))
        t["primary_key"] = _list(t.get("primary_key"))
        if t.get("object_kind") in ("view", "matview"):
            rel = str(t.get("relation") or "").split(".")[-1]
            t["in_warehouse"] = (rel in objects) or (t.get("name") in objects)
        else:
            t["in_warehouse"] = None
        tables.append(t)
    indexes = [_clean(dict(i)) for i in rows.get("indexes") or [] if _keep(i, status)]
    for i in indexes:
        i["columns"] = _list(i.get("columns"))
    return {"tables": tables, "indexes": indexes}


@router.get("/relationships")
async def list_relationships(status: str = "active"):
    if status not in TABLE_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(TABLE_STATUSES)}")
    rows = await semanticmodel.load_rows(include_retired=status != "active")
    out = []
    for r in rows.get("relationships") or []:
        if not _keep(r, status):
            continue
        d = _clean(dict(r))
        d["from_columns"] = _list(d.get("from_columns"))
        d["to_columns"] = _list(d.get("to_columns"))
        mr = d.get("match_rate")
        d["match_rate"] = float(mr) if mr is not None else None
        out.append(d)
    return {"relationships": out}


@router.get("/measures")
async def list_measures():
    rows = await semanticmodel.load_rows()
    model = await semanticmodel.load_model()
    today = modeltools._today()
    start, end = modeltools.resolve_range("last_30_days", today)
    out = []
    for r in rows.get("measures") or []:
        d = _clean(dict(r))
        d["filter"] = _list(d.get("filter"))
        d["input_metrics"] = _list(d.get("input_metrics")) if d.get("input_metrics") is not None else None
        for k in ("numerator", "denominator", "non_additive_dimension"):
            d[k] = _obj(d.get(k))
        d["sample_range"] = None
        m = model.measure(d["name"])
        if m is None:
            d["compile_error"] = "measure is not in the compiled model"
            out.append(d)
            continue
        try:
            d["kind"] = modelcompiler.measure_kind(model, m)  # C-03 kind, for dashboard binding
            req = modelcompiler.canonical_request(model, m, "month", start, end)
            d["compiled_sql"] = modelcompiler.compile_measure(model, req).sql
            if req.grain:
                d["sample_range"] = {"start": start.isoformat(), "end": end.isoformat()}
        except Exception as e:  # noqa: BLE001 - shown to the owner as text
            d["compile_error"] = str(e)[:400] or type(e).__name__
        out.append(d)
    return {"measures": out}


@router.get("/pending")
async def list_pending():
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT id, payload, created_at FROM actions "
        "WHERE kind = 'model_change' AND status = 'proposed' ORDER BY id DESC LIMIT 200")
    out = []
    for r in rows:
        p = _dict(r["payload"])
        out.append({"action_id": r["id"], "object": p.get("object"), "name": p.get("name"),
                    "op": p.get("op"), "summary": p.get("_summary"), "created_at": r["created_at"]})
    return {"pending": out}
