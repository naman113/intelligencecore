"""Read-only context-store routes for the operator UIs. Approve/reject stays on
/api/actions; schema discovery is an agent tool, never a route."""

import json

from fastapi import APIRouter, HTTPException

from . import db, knowledge

router = APIRouter(prefix="/api/context", tags=["context"])

TABLE_STATUSES = {"approved", "proposed", "undocumented", "hidden", "missing", "all"}
RULE_KINDS = {"rule", "kpi", "all"}
RULE_STATUSES = {"approved", "proposed", "all"}


def _check(value: str, allowed: set, name: str) -> None:
    if value not in allowed:
        raise HTTPException(status_code=422,
                            detail=f"{name} must be one of {sorted(allowed)}")


def _clamp(n: int, hi: int = 1000) -> int:
    return max(1, min(int(n), hi))


def _obj(v) -> dict:
    """asyncpg returns jsonb as str: decode at the edge."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _pending(row) -> dict:
    p = _obj(row["payload"])
    return {"action_id": row["id"], "text": p.get("text"),
            "before": p.get("before"), "created_at": row["created_at"],
            "origin": p.get("origin"), "edited_by": p.get("edited_by")}


_PENDING_SQL = ("SELECT id, payload, created_at FROM actions "
                "WHERE kind = 'context_change' AND status = 'proposed' "
                "AND payload->>'kind' = ANY($1::text[]) ORDER BY id")


def _cat_status(status: str, description) -> str:
    if status in ("hidden", "missing"):
        return status
    return "approved" if description else "undocumented"


def _keep(item: dict, status: str) -> bool:
    return status == "all" or item["status"] == status


@router.get("/sources")
async def list_sources():
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT s.name, s.kind, s.description, s.status,
                  (SELECT count(*) FROM context_tables t
                    WHERE t.source_id = s.id AND t.description IS NOT NULL) AS tables_approved,
                  (SELECT count(*) FROM context_columns c
                     JOIN context_tables t ON t.id = c.table_id
                    WHERE t.source_id = s.id AND c.description IS NOT NULL) AS columns_approved,
                  (SELECT count(*) FROM actions a
                    WHERE a.kind = 'context_change' AND a.status = 'proposed'
                      AND COALESCE(a.payload->>'source', 'business_db') = s.name) AS pending
             FROM context_sources s ORDER BY s.name""")
    return {"sources": [dict(r) for r in rows]}


@router.get("/tables")
async def list_tables(source: str = "business_db", status: str = "all", limit: int = 200):
    _check(status, TABLE_STATUSES, "status")
    limit = _clamp(limit)
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT t.ref, t.schema_name, t.table_name, t.object_kind, t.description,
                  t.status, t.approved_at
             FROM context_tables t JOIN context_sources s ON s.id = t.source_id
            WHERE s.name = $1 ORDER BY t.ref""", source)
    acts = await pool.fetch(_PENDING_SQL, ["table"])
    items: dict[str, dict] = {}
    for r in rows:
        items[r["ref"]] = {
            "ref": r["ref"], "schema": r["schema_name"], "table": r["table_name"],
            "object_kind": r["object_kind"], "description": r["description"],
            "status": _cat_status(r["status"], r["description"]),
            "approved_at": r["approved_at"], "pending": None}
    for a in acts:
        p = _obj(a["payload"])
        if (p.get("source") or "business_db") != source or not p.get("ref"):
            continue
        item = items.get(p["ref"])
        if item is None:
            item = items[p["ref"]] = {
                "ref": p["ref"], "schema": p.get("schema"), "table": p.get("table"),
                "object_kind": p.get("object_kind") or "table", "description": None,
                "status": "proposed", "approved_at": None, "pending": None}
        item["pending"] = _pending(a)
    out = [i for i in items.values() if _keep(i, status)]
    return {"tables": out[:limit]}


@router.get("/columns")
async def list_columns(source: str = "business_db", table: str = "",
                       status: str = "all", limit: int = 500):
    _check(status, TABLE_STATUSES, "status")
    limit = _clamp(limit)
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT c.ref, t.ref AS table_ref, c.column_name, c.data_type, c.is_nullable,
                  c.is_primary_key, c.references_ref, c.description, c.status, c.approved_at
             FROM context_columns c JOIN context_tables t ON t.id = c.table_id
             JOIN context_sources s ON s.id = t.source_id
            WHERE s.name = $1 ORDER BY c.ref""", source)
    acts = await pool.fetch(_PENDING_SQL, ["column"])
    items: dict[str, dict] = {}
    for r in rows:
        items[r["ref"]] = {
            "ref": r["ref"], "table_ref": r["table_ref"], "column": r["column_name"],
            "data_type": r["data_type"], "is_nullable": r["is_nullable"],
            "is_primary_key": r["is_primary_key"], "references_ref": r["references_ref"],
            "description": r["description"],
            "status": _cat_status(r["status"], r["description"]),
            "approved_at": r["approved_at"], "pending": None}
    for a in acts:
        p = _obj(a["payload"])
        ref = p.get("ref")
        if (p.get("source") or "business_db") != source or not ref:
            continue
        item = items.get(ref)
        if item is None:
            tref = ref.rsplit(".", 1)[0]
            item = items[ref] = {
                "ref": ref, "table_ref": tref, "column": p.get("column"),
                "data_type": (p.get("structure") or {}).get("type"), "is_nullable": None,
                "is_primary_key": None, "references_ref": None, "description": None,
                "status": "proposed", "approved_at": None, "pending": None}
        item["pending"] = _pending(a)
    out = [i for i in items.values()
           if _keep(i, status) and (not table or i["table_ref"] == table)]
    return {"columns": out[:limit]}


@router.get("/rules")
async def list_rules(kind: str = "all", status: str = "all", limit: int = 200):
    _check(kind, RULE_KINDS, "kind")
    _check(status, RULE_STATUSES, "status")
    limit = _clamp(limit)
    kinds = ["rule", "kpi"] if kind == "all" else [kind]
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT kind, ref, title, body, meta, status, origin, updated_at
             FROM business_context
            WHERE kind = ANY($1::text[]) AND status = 'approved' ORDER BY kind, ref""", kinds)
    acts = await pool.fetch(_PENDING_SQL, kinds)
    items: dict[tuple, dict] = {}
    for r in rows:
        items[(r["kind"], r["ref"])] = {
            "kind": r["kind"], "ref": r["ref"], "title": r["title"], "text": r["body"],
            "tables": _obj(r["meta"]).get("tables") or [], "status": "approved",
            "updated_at": r["updated_at"], "origin": r.get("origin"), "pending": None}
    for a in acts:
        p = _obj(a["payload"])
        key = (p.get("kind"), p.get("ref"))
        if not key[1]:
            continue
        item = items.get(key)
        if item is None:
            item = items[key] = {
                "kind": key[0], "ref": key[1], "title": p.get("title"), "text": None,
                "tables": p.get("tables") or [], "status": "proposed",
                "updated_at": None, "origin": None, "pending": None}
        item["pending"] = _pending(a)
    out = [i for i in items.values() if status in ("all", i["status"])]
    return {"rules": out[:limit]}


@router.get("/search")
async def search(q: str = "", limit: int = 8):
    if not q.strip():
        raise HTTPException(status_code=422, detail="q is required")
    return await knowledge.search_business_context(q, _clamp(limit, 50))
