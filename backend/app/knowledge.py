"""Read-only business-context search for agents.

Reads approved rows of business_context (agent_memory Postgres, pgvector) via
contextstore.search. This module exposes ONLY search; writes happen only when
the owner approves a context_change action (contextstore.apply).
"""

import json
import logging

from . import contextstore
from .config import get_settings

log = logging.getLogger("knowledge")

EMPTY_HINT = ("No approved business context matched yet. The owner approves table and "
              "column descriptions proposed by schema discovery on the Approvals page. "
              "Answering from the business DB schema only.")


def reset_clients() -> None:
    """Drop cached clients so the next search picks up changed config."""
    contextstore.reset_clients()


# Payload hygiene: everything returned here lands verbatim in the agent's
# context window, so cap both breadth (keys) and depth (string length).
_MAX_PAYLOAD_KEYS = 8
_MAX_VALUE_CHARS = 1000


def _slim_payload(payload: dict) -> dict:
    out: dict = {}
    for key in list(payload)[:_MAX_PAYLOAD_KEYS]:
        val = payload[key]
        if isinstance(val, str) and len(val) > _MAX_VALUE_CHARS:
            val = val[:_MAX_VALUE_CHARS]
        out[key] = val
    return out


async def search_business_context(query: str, limit: int = 8) -> list[dict]:
    """Search approved business context. Read-only."""
    raw = await contextstore.search(query, limit)
    if not raw:
        return [{"text": EMPTY_HINT}]
    floor = get_settings().knowledge_score_floor
    return [
        {"score": h.get("score"), **_slim_payload({k: v for k, v in h.items() if k != "score"})}
        for h in raw
        if h.get("score") is None or h["score"] >= floor
    ]


# Phase 12 (CTX-09b): approved KPI definitions, read-only.
async def approved_kpis(limit: int = 12) -> list[dict]:
    try:
        from .db import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT ref, title, body FROM business_context "
            "WHERE kind = 'kpi' AND status = 'approved' "
            "ORDER BY updated_at DESC, id DESC LIMIT $1", limit)
        return [{"ref": r["ref"], "title": r["title"], "body": r["body"]} for r in rows]
    except Exception:
        return []


# ── Quick 002: approved rules and KPIs reach the SQL step by table ──
# Semantic search ranks a rule among hundreds of column rows, so an approved
# rule can miss the top hits for the very query it governs. Every rule/kpi row
# names its tables in meta.tables; these helpers fetch rules by table so the
# search and run_sql tools can hand them over deterministically.
RULES_BLOCK_CHARS = 6000   # full rule text per block; the rest is listed by ref
RULE_BODY_CHARS = 1200
RULES_HEADER = ("APPROVED RULES AND KPIS FOR TABLES {tables} (owner-approved; follow them in "
                "the SQL you write; they override your own assumptions and anything remembered "
                "from earlier conversations):")
RULES_MORE = "Also applies (search_business_context with the ref returns the text): "
RULES_COMBINE = ("Combining sources: when a rule takes different rows from different tables, "
                 "compute each source in its own subquery filtered to its own rows, UNION ALL "
                 "them, then aggregate once. Never join the combined rows back to a table on a "
                 "non-unique column such as a type or status; that multiplies rows.")


def table_key(name) -> str:
    """Bare lowercase table name from a context ref, schema.table or quoted name."""
    s = str(name or "").strip()
    return s.rsplit(".", 1)[-1].strip().strip('"').lower() if s else ""


def query_tables(sql: str) -> set[str]:
    """Bare lowercase names of the tables a SELECT reads (CTE names excluded).
    Empty set when the SQL does not parse; guardrails reports that error."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
        out: set[str] = set()
        for tree in sqlglot.parse(sql or "", dialect="postgres"):
            if tree is None:
                continue
            ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
            out |= {t.name.lower() for t in tree.find_all(exp.Table)
                    if t.name and t.name.lower() not in ctes}
        return out
    except Exception:
        return set()


async def rules_for_tables(tables, extra_refs=()) -> list[dict]:
    """Approved rule and KPI rows whose meta.tables name any of `tables` (bare
    names or refs, any case), plus the tables of `extra_refs` (rule/kpi refs
    already shown). Most specific first: more matched tables, rules before
    KPIs, fewer tables overall. Read-only; [] on any error."""
    keys = sorted({k for k in (table_key(t) for t in tables or ()) if k})
    refs = sorted({str(r) for r in extra_refs or () if r})
    if not keys and not refs:
        return []
    try:
        from .db import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            r"""WITH tabs AS (
                    SELECT k FROM unnest($1::text[]) AS k
                    UNION
                    SELECT lower(regexp_replace(t, '^.*\.', ''))
                      FROM business_context bc,
                           jsonb_array_elements_text(CASE WHEN jsonb_typeof(bc.meta->'tables') = 'array'
                                                          THEN bc.meta->'tables' ELSE '[]'::jsonb END) t
                     WHERE bc.ref = ANY($2::text[]) AND bc.status = 'approved')
                SELECT kind, ref, title, body, meta->'tables' AS tables
                  FROM business_context
                 WHERE kind IN ('rule', 'kpi') AND status = 'approved'
                   AND EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(
                           CASE WHEN jsonb_typeof(meta->'tables') = 'array'
                                THEN meta->'tables' ELSE '[]'::jsonb END) t
                        WHERE lower(regexp_replace(t, '^.*\.', '')) IN (SELECT k FROM tabs))""",
            keys, refs)
    except Exception:
        log.warning("rules_for_tables failed", exc_info=True)
        return []
    wanted = set(keys)
    out = []
    for r in rows:
        tabs = r["tables"]
        tabs = json.loads(tabs) if isinstance(tabs, str) else (tabs or [])
        tabs = [t for t in tabs if isinstance(t, str)]
        names = {table_key(t) for t in tabs}
        out.append({"kind": r["kind"], "ref": r["ref"], "title": r["title"] or "",
                    "body": r["body"] or "", "tables": sorted(names),
                    "matched": sorted(names & wanted) if wanted else sorted(names)})
    out.sort(key=lambda x: (-len(x["matched"]), x["kind"] != "rule", len(x["tables"]), x["ref"]))
    return out


def format_rules(items: list[dict], seen: set | None = None,
                 tables=(), budget: int = RULES_BLOCK_CHARS) -> tuple[str, list[str]]:
    """Render the rules block for the tool result. Items already in `seen`
    are skipped. Returns (text, refs mentioned); ("", []) when nothing is new.
    Items past the char budget are listed by ref and title, which also counts
    as shown: the agent can fetch them by ref."""
    fresh = [i for i in items if not seen or i["ref"] not in seen]
    if not fresh:
        return "", []
    names = sorted({table_key(t) for t in tables if table_key(t)}) or sorted(
        {m for i in fresh for m in i["matched"]})
    lines, more, used = [], [], 0
    for i in fresh:
        body = i["body"][:RULE_BODY_CHARS]
        line = f"- {i['ref']} ({i['title']}): {body}" if i["title"] else f"- {i['ref']}: {body}"
        if used + len(line) <= budget or not lines:
            lines.append(line)
            used += len(line)
        else:
            more.append(f"{i['ref']} ({i['title']})" if i["title"] else i["ref"])
    text = RULES_HEADER.format(tables=", ".join(names) or "in this query") + "\n" + "\n".join(lines)
    if more:
        text += "\n" + RULES_MORE + "; ".join(more)
    text += "\n" + RULES_COMBINE
    return text, [i["ref"] for i in fresh]
# ── End Quick 002 ──


async def rules_by_ref(refs) -> list[dict]:
    """Approved rule/kpi rows by exact ref, in search-hit shape. Read-only."""
    refs = [str(r) for r in refs or () if r][:20]
    if not refs:
        return []
    try:
        from .db import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            "SELECT kind, ref, doc, meta->'tables' AS tables FROM business_context "
            "WHERE ref = ANY($1::text[]) AND kind IN ('rule', 'kpi') AND status = 'approved' "
            "ORDER BY ref", refs)
    except Exception:
        log.warning("rules_by_ref failed", exc_info=True)
        return []
    out = []
    for r in rows:
        tabs = r["tables"]
        tabs = json.loads(tabs) if isinstance(tabs, str) else (tabs or [])
        out.append({"type": r["kind"], "ref": r["ref"], "text": (r["doc"] or "")[:_MAX_VALUE_CHARS * 2],
                    "tables": [table_key(t) for t in tabs if isinstance(t, str)]})
    return out or [{"text": f"No approved rule or KPI with ref {', '.join(refs)}."}]
