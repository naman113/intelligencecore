"""Business context store on pgvector.

The ONLY writer of context_sources / context_tables / context_columns /
business_context. It is called solely by the context_change approval executor
(an approved `actions` row). Readers go through knowledge.py. Every write is an
upsert keyed by ref and nothing in here ever removes a row, so approved text
and KPI-to-table links (text refs in meta.tables) survive re-discovery.

This is a leaf module: it must not import the agent graph.
"""

import json
import logging
import re
import unicodedata

from openai import AsyncOpenAI

from .config import get_settings
from .db import get_pool

log = logging.getLogger("contextstore")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
MAX_TEXT = 1500
MAX_TABLES = 20
KINDS = ("source", "table", "column", "rule", "kpi")

_cached: AsyncOpenAI | None = None


def reset_clients() -> None:
    """Drop the cached OpenAI client so a key change applies live."""
    global _cached
    _cached = None


def _client() -> AsyncOpenAI:
    global _cached
    if _cached is None:
        from . import usage  # Quick 005: usage ledger (a leaf too; never imports the graph)
        _cached = usage.wrap_openai(AsyncOpenAI(api_key=get_settings().openai_api_key,
                                                max_retries=1, timeout=8.0),
                                    role="context_store")
    return _cached


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


async def _embed_or_none(text: str) -> str | None:
    """Embedding as a pgvector literal, or None (no key, API error, bad size)."""
    try:
        if not get_settings().openai_api_key:
            return None
        resp = await _client().embeddings.create(model=EMBEDDING_MODEL, input=[text])
        vec = resp.data[0].embedding
        if len(vec) != EMBEDDING_DIM:
            log.warning("embedding has %d dims, expected %d; storing without",
                        len(vec), EMBEDDING_DIM)
            return None
        return _vec(vec)
    except Exception as e:  # never let an embedding failure lose an approval
        log.warning("embedding unavailable (%s); storing without", type(e).__name__)
        return None


_SLUG_RE = re.compile(r"^(rule|kpi):[a-z0-9-]{1,64}$")


def _has_hidden_chars(s: str) -> bool:
    """Invisible/format/private/control characters (bidi, zero-width, tags)."""
    return any(unicodedata.category(c) in ("Cf", "Co", "Cs", "Cc") and c not in "\n\t"
               for c in s)


def _valid(p: dict) -> bool:
    if not isinstance(p, dict) or p.get("kind") not in KINDS:
        return False
    text, ref = p.get("text"), p.get("ref")
    if not isinstance(text, str) or not (1 <= len(text.strip()) and len(text) <= MAX_TEXT):
        return False
    if not isinstance(ref, str) or not ref.strip():
        return False
    kind = p["kind"]
    if _has_hidden_chars(text) or _has_hidden_chars(p.get("title") or ""):
        return False
    if kind in ("rule", "kpi") and not (
            _SLUG_RE.match(ref) and ref.split(":", 1)[0] == kind):
        return False
    if kind in ("table", "column"):
        src = p.get("source") or "business_db"
        parts = [src, p.get("schema"), p.get("table")] + (
            [p.get("column")] if kind == "column" else [])
        if not all(isinstance(x, str) and x for x in parts):
            return False
        if ref != ".".join(parts):
            return False
    if kind in ("table", "column") and not (p.get("schema") and p.get("table")):
        return False
    if kind == "column" and not p.get("column"):
        return False
    tables = p.get("tables")
    if tables is not None:
        if not isinstance(tables, list) or len(tables) > MAX_TABLES:
            return False
        if not all(isinstance(t, str) and t for t in tables):
            return False
    return True


def _slug(ref: str) -> str:
    return ref.split(":", 1)[1] if ":" in ref else ref


def _compose(p: dict) -> tuple[str, str, dict]:
    kind, text = p["kind"], p["text"].strip()
    source = p.get("source") or "business_db"
    schema, table, column = p.get("schema"), p.get("table"), p.get("column")
    title = (p.get("title") or "").strip()
    meta = {"source": source, "schema": schema, "table_name": table,
            "column_name": column, "tables": list(p.get("tables") or [])}
    if kind == "table":
        doc = f"Table {schema}.{table} ({p.get('object_kind') or 'table'}) in source {source}. {text}"
    elif kind == "column":
        st = p.get("structure") or {}
        bits = [f"type {st.get('type') or 'unknown'}",
                "nullable" if st.get("nullable") else "not null"]
        if st.get("pk"):
            bits.append("primary key")
        fk = st.get("fk")
        if fk and fk.get("table"):
            bits.append(f"references {fk['table']}.{fk.get('column')}")
        doc = f"Column {table}.{column} of {schema}.{table}: {', '.join(bits)}. {text}"
    elif kind == "rule":
        title = title or _slug(p["ref"])
        doc = f"Business rule {title}: {text}"
    elif kind == "kpi":
        title = title or _slug(p["ref"])
        meta["kpi_name"] = title
        doc = f"KPI {title}: {text}"
    else:
        doc = f"Data source {p['ref']}: {text}"
    return title, doc, meta


async def apply(payload: dict) -> bool:
    """Upsert one approved context item (plus its catalog row). Returns False
    for an invalid payload without touching the database."""
    if not _valid(payload):
        return False
    p = payload
    kind, ref, text = p["kind"], p["ref"], p["text"].strip()
    title, doc, meta = _compose(p)
    vec = await _embed_or_none(doc)  # once, before the transaction
    source = ref if kind == "source" else (p.get("source") or "business_db")
    # Phase 12 (WR-11): edited_by is written only by the owner edit route; any
    # origin field in the payload is ignored.
    origin = "human" if p.get("edited_by") == "owner" else "agent"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if kind == "source":
                source_id = await conn.fetchval(
                    """INSERT INTO context_sources (name, description)
                       VALUES ($1, $2)
                       ON CONFLICT (name) DO UPDATE
                         SET description = EXCLUDED.description, updated_at = now()
                       RETURNING id""", source, text)
            else:
                source_id = await conn.fetchval(
                    """INSERT INTO context_sources (name) VALUES ($1)
                       ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                       RETURNING id""", source)
            # Phase 12 (WR-03): an owner hide survives later approvals.
            hidden = False
            if kind in ("table", "column"):
                hide_ref = ref if kind == "table" else f"{source}.{p['schema']}.{p['table']}"
                hidden = (await conn.fetchval(
                    "SELECT status FROM context_tables WHERE ref = $1", hide_ref)) == "hidden"
            if kind == "table":
                await conn.execute(
                    """INSERT INTO context_tables
                         (source_id, schema_name, table_name, ref, object_kind,
                          description, approved_at)
                       VALUES ($1, $2, $3, $4, $5, $6, now())
                       ON CONFLICT (ref) DO UPDATE SET
                         description = EXCLUDED.description,
                         object_kind = EXCLUDED.object_kind,
                         approved_at = now(), updated_at = now(),
                         status = CASE WHEN context_tables.status = 'hidden'
                                       THEN 'hidden' ELSE 'active' END""",
                    source_id, p["schema"], p["table"], ref,
                    p.get("object_kind") or "table", text)
            elif kind == "column":
                table_ref = f"{source}.{p['schema']}.{p['table']}"
                table_id = await conn.fetchval(
                    """INSERT INTO context_tables
                         (source_id, schema_name, table_name, ref, object_kind)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (ref) DO UPDATE SET ref = EXCLUDED.ref
                       RETURNING id""",
                    source_id, p["schema"], p["table"], table_ref,
                    p.get("object_kind") or "table")
                st = p.get("structure") or {}
                await conn.execute(
                    """INSERT INTO context_columns
                         (table_id, column_name, ref, data_type, is_nullable,
                          is_primary_key, references_ref, signature,
                          description, approved_at, status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(),
                               CASE WHEN $10 THEN 'hidden' ELSE 'active' END)
                       ON CONFLICT (ref) DO UPDATE SET
                         data_type = EXCLUDED.data_type,
                         is_nullable = EXCLUDED.is_nullable,
                         is_primary_key = EXCLUDED.is_primary_key,
                         references_ref = EXCLUDED.references_ref,
                         signature = EXCLUDED.signature,
                         description = EXCLUDED.description,
                         approved_at = now(), updated_at = now(),
                         status = CASE WHEN context_columns.status = 'hidden'
                                       THEN 'hidden' ELSE 'active' END""",
                    table_id, p["column"], ref, st.get("type"), st.get("nullable"),
                    bool(st.get("pk")), p.get("references_ref"), p.get("signature"),
                    text, hidden)
            await conn.execute(
                """INSERT INTO business_context
                     (kind, ref, source_id, title, body, doc, meta, embedding,
                      embedded_at, status, origin)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::vector,
                           CASE WHEN $8::vector IS NOT NULL THEN now() END,
                           CASE WHEN $10 THEN 'hidden' ELSE 'approved' END, $9)
                   ON CONFLICT (kind, ref) DO UPDATE SET
                     source_id = EXCLUDED.source_id, title = EXCLUDED.title,
                     body = EXCLUDED.body, doc = EXCLUDED.doc, meta = EXCLUDED.meta,
                     embedding = EXCLUDED.embedding,
                     embedded_at = EXCLUDED.embedded_at,
                     status = CASE WHEN $10 OR business_context.status = 'hidden'
                                   THEN 'hidden' ELSE 'approved' END,
                     origin = EXCLUDED.origin,
                     updated_at = now()""",
                kind, ref, source_id, title or None, text, doc,
                json.dumps(meta), vec, origin, hidden)
    return True


async def _backfill_embeddings(batch: int = 20) -> int:
    """Embed approved rows stored without a vector (no key / API error at
    approval time). Best-effort; never raises."""
    try:
        pool = await get_pool()
        todo = await pool.fetch(
            "SELECT id, doc FROM business_context "
            "WHERE status = 'approved' AND embedding IS NULL LIMIT $1", batch)
        done = 0
        for r in todo:
            v = await _embed_or_none(r["doc"])
            if v is None:
                break
            await pool.execute(
                "UPDATE business_context SET embedding = $2::vector, embedded_at = now() "
                "WHERE id = $1", r["id"], v)
            done += 1
        return done
    except Exception:
        log.warning("embedding backfill failed", exc_info=True)
        return 0


async def search(query: str, limit: int = 8) -> list[dict]:
    """Approved context ranked by cosine similarity (HNSW), with a full-text
    fallback for un-embedded rows or when no embedding is available."""
    limit = max(1, min(int(limit), 50))
    vec = await _embed_or_none(query)
    pool = await get_pool()
    rows: list = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            if vec is not None:
                try:  # hnsw.iterative_scan needs pgvector >= 0.8; degrade, never fail
                    async with conn.transaction():
                        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                except Exception:
                    log.debug("hnsw.iterative_scan unavailable", exc_info=True)
                await conn.execute("SET LOCAL hnsw.ef_search = 100")
                rows += await conn.fetch(
                    """SELECT kind, ref, doc, meta,
                              1 - (embedding <=> $1::vector) AS score
                       FROM business_context
                       WHERE status = 'approved' AND embedding IS NOT NULL
                       ORDER BY embedding <=> $1::vector LIMIT $2""", vec, limit)
            # Keyword leg ALWAYS covers un-embedded rows (they never rank in the
            # vector leg); with no query embedding it covers every row.
            rows += await conn.fetch(
                """SELECT kind, ref, doc, meta, NULL::float AS score
                   FROM business_context
                   WHERE status = 'approved' AND (embedding IS NULL OR $3)
                     AND to_tsvector('english', doc) @@ to_tsquery('english',
                         replace(plainto_tsquery('english', $1)::text, ' & ', ' | '))
                   ORDER BY ts_rank(to_tsvector('english', doc),
                         to_tsquery('english',
                           replace(plainto_tsquery('english', $1)::text, ' & ', ' | '))) DESC
                   LIMIT $2""", query, limit, vec is None)
    seen: set = set()
    deduped = []
    for r in rows:
        k = (r["kind"], r["ref"])
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    rows = sorted(deduped, key=lambda r: (r["score"] is None, -(r["score"] or 0)))[:limit]
    if vec is not None:
        await _backfill_embeddings()
    out = []
    for r in rows:
        m = r["meta"]
        m = json.loads(m) if isinstance(m, str) else (m or {})
        out.append({"score": r["score"], "type": r["kind"], "text": r["doc"],
                    "table_name": m.get("table_name"), "column_name": m.get("column_name"),
                    "kpi_name": m.get("kpi_name"), "source": m.get("source"),
                    "ref": r["ref"]})
    return out


# ── Phase 8: owner hides or unhides a table ──
# The owner's decision, not an agent proposal: contextstore stays the only
# writer of context state, and this is its second caller (audited below by an
# executed actions row written in the same transaction).
class BusinessDbUnavailable(Exception):  # Phase 11: WR-04
    pass


class CatalogTruncated(Exception):  # Phase 11: WR-04
    pass


async def set_table_hidden(ref: str, hidden: bool,
                           decided_by: int | None = None) -> dict | None:
    from . import schemadiscovery
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, source_id FROM context_tables WHERE ref = $1", ref)
    info = None
    if row is None:
        if not hidden:
            return None
        # Phase 11: WR-04 -- tenant failures and truncated catalogs get named errors
        try:
            info, truncated = await schemadiscovery.resolve_checked(ref)
        except Exception as e:
            raise BusinessDbUnavailable(type(e).__name__) from None
        if info is None and truncated:
            raise CatalogTruncated(ref)
        if not info or info.get("kind") != "table":
            return None
    if decided_by is None:
        try:
            from . import people
            primary = await people.primary_person()
            decided_by = primary["id"] if primary else None
        except Exception:
            decided_by = None
    op = "hide" if hidden else "unhide"
    t_from, t_to = ("active", "hidden") if hidden else ("hidden", "active")
    b_from, b_to = ("approved", "hidden") if hidden else ("hidden", "approved")
    rejected: list[int] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            if row is None:
                source = (info or {}).get("source") or "business_db"
                source_id = await conn.fetchval(
                    """INSERT INTO context_sources (name) VALUES ($1)
                       ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                       RETURNING id""", source)
                table_id = await conn.fetchval(
                    """INSERT INTO context_tables
                         (source_id, schema_name, table_name, ref, object_kind, status)
                       VALUES ($1, $2, $3, $4, $5, 'hidden')
                       ON CONFLICT (ref) DO UPDATE SET status = 'hidden', updated_at = now()
                       RETURNING id""",
                    source_id, info["schema"], info["table"], ref,
                    info.get("object_kind") or "table")
            else:
                table_id = row["id"]
                await conn.execute(
                    "UPDATE context_tables SET status = $3, updated_at = now() "
                    "WHERE id = $1 AND status = $2", table_id, t_from, t_to)
            await conn.execute(
                "UPDATE context_columns SET status = $3 "
                "WHERE table_id = $1 AND status = $2", table_id, t_from, t_to)
            await conn.execute(
                """UPDATE business_context SET status = $3
                    WHERE status = $2
                      AND ((kind = 'table' AND ref = $1)
                        OR (kind = 'column' AND left(ref, length($1) + 1) = $1 || '.'))""",
                ref, b_from, b_to)
            if hidden:
                recs = await conn.fetch(
                    """UPDATE actions SET status = 'rejected', decided_by = $2
                        WHERE kind = 'context_change' AND status = 'proposed'
                          AND (payload->>'ref' = $1
                               OR left(payload->>'ref', length($1) + 1) = $1 || '.')
                        RETURNING id""", ref, decided_by)
                rejected = [r["id"] for r in recs]
            payload = {
                "op": op, "kind": "table", "ref": ref, "origin": "owner",
                "rejected_action_ids": rejected,
                "_summary": f"Owner {op}d table {ref}",
            }
            action_id = await conn.fetchval(
                """INSERT INTO actions (kind, payload, tier, status, decided_by, executed_at)
                   VALUES ('context_change', $1::jsonb, 'operator', 'executed', $2, now())
                   RETURNING id""", json.dumps(payload), decided_by)
    return {"ref": ref, "hidden": hidden, "rejected": len(rejected),
            "action_id": action_id}
