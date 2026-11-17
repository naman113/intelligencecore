"""Phase 8: owner decisions on agent proposals. Edit is part of consent, not
creation; hide is an owner decision audited by an executed actions row."""
import asyncpg
from fastapi import APIRouter, HTTPException

from . import contextstore, contexttools, db

router = APIRouter(tags=["owner"])


@router.post("/api/actions/{action_id}/edit")
async def edit_proposal(action_id: int, body: dict | None = None):
    text = contexttools._clean((body or {}).get("text"))
    if (not text or len(text) > contexttools.MAX_TEXT
            or contextstore._has_hidden_chars(text)):
        raise HTTPException(
            422, f"text must be 1-{contexttools.MAX_TEXT} visible characters")
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """UPDATE actions SET payload = payload || jsonb_build_object(
               'text', $2::text, 'edited_by', 'owner',
               'agent_text', COALESCE(payload->'agent_text', payload->'text'))
            WHERE id = $1 AND kind = 'context_change' AND status = 'proposed'
            RETURNING id""", action_id, text)
    if row is None:
        raise HTTPException(409, "only a proposed context_change can be edited")
    return {"edited": action_id}


_REF_MSG = "ref must be a business_db table ref like business_db.public.customers"


@router.post("/api/schema/tables/hide")
async def hide_table(body: dict | None = None):
    body = body or {}
    ref = body.get("ref")
    if (not isinstance(ref, str) or len(ref) > 300
            or not ref.startswith("business_db.")
            or len(ref.split(".")) != 3 or not all(ref.split("."))):
        raise HTTPException(422, _REF_MSG)
    hidden = bool(body.get("hidden", True))
    decided_by = body.get("decided_by")
    if decided_by is not None and (isinstance(decided_by, bool)
                                   or not isinstance(decided_by, int)):
        raise HTTPException(422, "decided_by must be an integer person id")
    # Phase 11: WR-04 -- readable 422/503/409 instead of a 500
    unknown = "decided_by is not a known person id"
    if decided_by is not None:
        if not 0 < decided_by < 2**63:
            raise HTTPException(422, unknown)
        pool = await db.get_pool()
        if not await pool.fetchval("SELECT 1 FROM people WHERE id = $1", decided_by):
            raise HTTPException(422, unknown)
    try:
        out = await contextstore.set_table_hidden(ref, hidden, decided_by)
    except contextstore.BusinessDbUnavailable as e:
        raise HTTPException(503, f"business database unavailable: {e}")
    except contextstore.CatalogTruncated:
        raise HTTPException(
            409, "table list is truncated at sql_row_limit; raise it on Setup and retry")
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(422, unknown)
    if out is None:
        raise HTTPException(404, "table not found")
    return out
