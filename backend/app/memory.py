"""Learning loop: reflect on completed tasks, store learnings, retrieve them later.

This is what makes the system iterative — each task can leave behind
facts, preferences, corrections, or recipes that improve future tasks.
"""

import asyncio
import json
import logging
import re
import time
from typing import NamedTuple

from openai import AsyncOpenAI

from . import llmclient
from .config import get_settings
from .db import get_pool

log = logging.getLogger("memory")

EMBEDDING_MODEL = "text-embedding-3-small"

REFLECTION_PROMPT = """You review a completed task from a business data assistant (used in the owner's web app and on WhatsApp) and extract durable learnings.

A learning is worth storing ONLY if it will make future tasks better. Kinds:
- fact: stable fact about the data/schema (e.g. "monthly spend lives in Invoices.TotalAmount, in rupees")
- preference: a STANDING preference the user explicitly stated for how they always want things, that applies to future work (not a one-off request)
- correction: the user corrected the assistant; store the right way
- recipe: a reusable SQL query for a recurring question — store the SQL ONLY, never delivery steps. Only a query that actually ran in the tools above, against tables that exist; never invent table names
- lesson: how to approach this KIND of task next time — what worked, what to avoid and why (a table too big to scan without a filter, a statement timeout, a wrong join, a column that does not mean what its name says, which skill fitted). Write it as an instruction ("To find X, first ...; avoid ... because ..."), self-contained, naming the tables and columns it applies to. A lesson is method, not output: never the numbers or the answer.

CRITICAL — do NOT store:
- one-off output-format choices. "make me an excel", "send a pdf" this one time is NOT a standing preference. Only store a format preference if the user says something like "always send reports as PDF".
- one-off requests as preferences. "Send me a report listing every missed fuel drop" is a request for this run, NOT "The user prefers to receive a report listing every missed fuel drop event".
- delivery steps ("save to CSV", "then send the file") — recipes capture SQL and lessons capture how to query, not how the result was delivered.
- greetings, one-off numbers, or anything already obvious.
- how a dashboard's charts are laid out (which chart sits where, panel order, colours) — that lives in the dashboard itself.

Return a JSON object {"learnings": [...]} (the list may be empty). Each item: {"kind": "...", "content": "..."}.
Content must be self-contained and specific. Max 3 items."""

# Quick 009: appended (whatever the configured prompt) when a run was hard, so
# the approach that finally worked — and the dead ends — are kept as lessons.
HARD_RUN_ADDENDUM = """

THIS RUN WAS HARD ({why}). Return up to {max_items} items, and at least one must be a "lesson": the approach that finally worked and every dead end with its cause, as instructions for the next run of this kind of task, naming the tables and columns. Example: "To find fuel drops in VehicleTelemetry (about 570k rows per plate), always filter by plate and a time window first; unfiltered scans hit the 30 s statement timeout (QueryCanceledError)." Lessons are method, not output."""

# Quick 014: appended to whatever reflection prompt is configured (never
# stored), because a saved custom prompt would otherwise keep storing one-off
# requests as preferences (the client's "The user prefers to receive a report
# listing every missed fuel drop event").
PREFERENCE_RULE = """

PREFERENCE TEST: store a preference only if it will change how FUTURE work is done (it says always, by default, from now on, every week, or states a standing unit, format or view). A request for one report, list, file or chart in this run is not a preference: store nothing for it."""

# Hard-run thresholds (Quick 009).
HARD_TOOL_CALLS = 12
HARD_SECONDS = 120
HARD_MAX_ITEMS = 5
NORMAL_MAX_ITEMS = 3
HARD_MAX_TOKENS = 1200
TRANSCRIPT_CAP = 12000
LESSON_LIMIT = 3


def _ledger(client, role: str):
    """Quick 005: record this client's calls in the usage ledger (usage.py)."""
    from . import usage
    return usage.wrap_openai(client, role=role)


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


async def _embed(text: str) -> str:
    client = _ledger(AsyncOpenAI(api_key=get_settings().openai_api_key, max_retries=6,
                                 timeout=30.0), "learnings")
    emb = await client.embeddings.create(model=EMBEDDING_MODEL, input=[text])
    return _vec(emb.data[0].embedding)


# Quick 009: learnings and lessons are recalled for the same message, so the
# query embedding is shared (one OpenAI call, not two).
_QUERY_VEC_CACHE: dict[str, str] = {}
_QUERY_VEC_CACHE_MAX = 16


async def _query_vec(text: str) -> str:
    vec = _QUERY_VEC_CACHE.get(text)
    if vec is None:
        vec = await _embed(text)
        if len(_QUERY_VEC_CACHE) >= _QUERY_VEC_CACHE_MAX:
            _QUERY_VEC_CACHE.pop(next(iter(_QUERY_VEC_CACHE)))
        _QUERY_VEC_CACHE[text] = vec
    return vec


async def retrieve_learnings(query: str, limit: int = 5, *,
                             scopes: list[str] | None = None) -> list[dict]:
    """Top-k learnings by cosine similarity to the query. Lessons are recalled
    separately (retrieve_lessons) so they never crowd facts out, or vice versa.

    Quick 020: `scopes` are the preference scopes this run may see
    (memscope.RunIdentity.recall). Facts, corrections and recipes (scope NULL)
    reach everyone; a preference only its own person. A preference stored
    before scopes existed and not yet migrated counts as the owner's. No
    scopes = shared learnings only."""
    try:
        qvec = await _query_vec(query)
        pool = await get_pool()
        rows = await pool.fetch(
            # status = 'active' only: consolidation marks merged-away rows
            # 'superseded' — they keep their content for audit but must never
            # resurface in retrieval. Quick 020: 'pending' (unattributed, waiting
            # for the owner) and 'rejected' rows never resurface either. Quick 019:
            # the id names the learning in the run's memory provenance
            # (task_runs.memory_used).
            """SELECT id, kind, content, source, 1 - (embedding <=> $1::vector) AS score
               FROM learnings WHERE embedding IS NOT NULL AND status = 'active'
                 AND kind <> 'lesson'
                 AND ((scope IS NULL AND kind <> 'preference')
                      OR scope = ANY($3::text[])
                      OR (scope IS NULL AND kind = 'preference' AND 'owner' = ANY($3::text[])))
               ORDER BY embedding <=> $1::vector LIMIT $2""",
            qvec, limit, list(scopes or []),
        )
        return [dict(r) for r in rows if r["score"] > 0.25]
    except Exception:
        log.exception("learning retrieval failed")
        return []


async def _question_tables(query: str) -> set[str]:
    """Lowercase table names this question is about: tables the approved
    business context ranks for it (contextstore search, through knowledge.py)
    plus known table names written in the question itself. Never raises."""
    tables: set[str] = set()
    try:
        from . import knowledge
        for hit in await knowledge.search_business_context(query, limit=6):
            if hit.get("table_name"):
                tables.add(knowledge.table_key(hit["table_name"]))
            for ref in hit.get("tables") or []:
                tables.add(knowledge.table_key(ref))
    except Exception:
        log.debug("lesson recall: business-context search failed", exc_info=True)
    try:
        known = await known_tables()
        text = (query or "").lower()
        for name in known.names:
            if "." not in name and len(name) >= 4 and re.search(
                    rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text):
                tables.add(name)
    except Exception:
        log.debug("lesson recall: table-name scan failed", exc_info=True)
    tables.discard("")
    return tables


async def retrieve_lessons(query: str, limit: int = LESSON_LIMIT) -> list[dict]:
    """Up to `limit` active lessons for this question (Quick 009): lessons
    that name a table the question is about rank first, then the nearest by
    meaning (the kind of task). [] without an OpenAI key or any lessons.
    Never raises."""
    try:
        if not get_settings().openai_api_key:
            return []
        pool = await get_pool()
        if not await pool.fetchval(
                "SELECT 1 FROM learnings WHERE kind = 'lesson' AND status = 'active' LIMIT 1"):
            return []
        qvec = await _query_vec(query)
        tables = sorted(await _question_tables(query))
        patterns = [f"%{t}%" for t in tables][:20]
        rows = await pool.fetch(
            """SELECT id, kind, content, source,
                      1 - (embedding <=> $1::vector) AS score,
                      (cardinality($3::text[]) > 0
                       AND content ILIKE ANY($3::text[])) AS table_match
               FROM learnings
               WHERE kind = 'lesson' AND status = 'active' AND embedding IS NOT NULL
               ORDER BY (cardinality($3::text[]) > 0 AND content ILIKE ANY($3::text[])) DESC,
                        embedding <=> $1::vector
               LIMIT $2""",
            qvec, limit * 3, patterns)
        out = [dict(r) for r in rows if r["table_match"] or r["score"] > 0.2][:limit]
        if out:
            log.info("lesson recall: %d lesson(s) %s for tables %s",
                     len(out), [r["id"] for r in out], tables[:8])
        return out
    except Exception:
        log.exception("lesson retrieval failed")
        return []


VALID_KINDS = ("fact", "preference", "correction", "recipe", "lesson")


# ── Quick 009: hygiene checks before a learning is stored ──────────────────
#
# A recipe is reusable SQL, and agents reuse it, so a hallucinated one (a
# table that does not exist) is worse than none. Every store path —
# reflection, store_learning, the remember_fact tool — runs check_learning
# first; nightly consolidation runs the same recipe check over what is
# already stored (consolidation.supersede_bad_recipes). The known tables are
# the context store's tables plus the live tenant catalog (read through
# guardrails, cached), minus tables the owner hid.

CATALOG_TTL_SECONDS = 600
_catalog_cache: tuple[float, frozenset, bool] | None = None   # (at, names, complete)


class KnownTables(NamedTuple):
    names: frozenset          # lowercased "table" and "schema.table" spellings
    hidden: frozenset         # owner-hidden, same spellings
    documented: bool          # the context store lists at least one table
    live_complete: bool       # the tenant catalog was read in full this time

    @property
    def judgeable(self) -> bool:
        """Enough to call a table unknown: a full live catalog, or (with the
        tenant DB unreachable) the owner-approved context store."""
        return self.live_complete or self.documented


def _spellings(schema: str | None, table: str) -> set[str]:
    t = (table or "").strip().lower()
    out = {t} if t else set()
    if t and schema:
        out.add(f"{schema.strip().lower()}.{t}")
    return out


async def known_tables(*, refresh: bool = False) -> KnownTables:
    """Never raises: an unreachable store just contributes nothing."""
    global _catalog_cache
    names: set[str] = set()
    hidden: set[str] = set()
    documented = False
    try:
        pool = await get_pool()
        for r in await pool.fetch(
                "SELECT schema_name, table_name, status FROM context_tables"):
            sp = _spellings(r["schema_name"], r["table_name"])
            if r["status"] == "hidden":
                hidden |= sp
            elif r["status"] == "active":
                names |= sp
                documented = True
    except Exception:
        log.warning("recipe check: context store unreadable", exc_info=True)
    live_complete = False
    now = time.monotonic()
    if (not refresh and _catalog_cache is not None
            and now - _catalog_cache[0] < CATALOG_TTL_SECONDS):
        names |= _catalog_cache[1]
        live_complete = _catalog_cache[2]
    else:
        try:
            from . import schemadiscovery
            pairs, truncated = await schemadiscovery.table_names()
            live: set[str] = set()
            for schema, table in pairs:
                live |= _spellings(schema, table)
            _catalog_cache = (now, frozenset(live), not truncated)
            names |= live
            live_complete = not truncated
        except Exception as e:
            log.info("recipe check: tenant catalog unavailable (%s)", type(e).__name__)
    return KnownTables(frozenset(names - hidden), frozenset(hidden),
                       documented, live_complete)


_FENCE_RE = re.compile(r"```(?:sql|postgres|postgresql)?\s*\n?(.*?)```", re.I | re.S)
_START_RE = re.compile(r"\b(select|with)\b", re.I)
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}")


def extract_sql(content: str) -> str | None:
    """The SQL a learning carries, or None. A ```sql fence wins; otherwise
    the first SELECT/WITH from which the rest parses as one SELECT (so prose
    like "Select the top machines: SELECT ..." still finds the query). Text
    after the first ';' is ignored. {{placeholders}} become NULL."""
    import sqlglot
    from sqlglot import expressions as exp

    text = content or ""
    fence = _FENCE_RE.search(text)
    candidates = [fence.group(1)] if fence else [text[m.start():]
                                                for m in _START_RE.finditer(text)]
    for cand in candidates:
        sql = _TEMPLATE_RE.sub("NULL", cand.split(";", 1)[0]).strip()
        if not sql:
            continue
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
        except Exception:
            continue
        if isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)) \
                and tree.find(exp.Table) is not None:
            return sql
    if fence:
        return _TEMPLATE_RE.sub("NULL", fence.group(1).split(";", 1)[0]).strip() or None
    return None


_WRITE_RE = re.compile(
    r"^\s*(insert\s+into|update\s+\w+\s+set|delete\s+from|drop|alter|create|truncate|grant)\b",
    re.I | re.M)


def _looks_like_sql(content: str) -> bool:
    return bool(re.search(r"\bselect\b[\s\S]*\bfrom\b", content or "", re.I))


def recipe_tables(sql: str) -> list[tuple[str, str]]:
    """(schema, table) for every real table the query reads; CTE names and
    table functions excluded."""
    import sqlglot
    from sqlglot import expressions as exp

    tree = sqlglot.parse_one(sql, dialect="postgres")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[tuple[str, str]] = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier) or not t.name:
            continue
        if not t.db and t.name.lower() in ctes:
            continue
        pair = (t.db or "", t.name)
        if pair not in out:
            out.append(pair)
    return out


def recipe_problem(content: str, known: KnownTables | None) -> str | None:
    """Why this recipe must not be stored, or None. Pure (no I/O).

    A recipe needs SQL that passes the same read-only validation as every
    tenant query (guardrails.validate_readonly_sql) and reads only known
    tables. With known=None or a catalog that cannot judge, table names are
    not checked (the SQL still has to be a valid read-only query)."""
    from .guardrails import SQLGuardrailError, validate_readonly_sql

    sql = extract_sql(content)
    if sql is None:
        if _WRITE_RE.search(content or ""):
            return "its SQL is not a read-only query"
        if _looks_like_sql(content):
            return "its SQL does not parse"
        return "a recipe must be a SQL query (store prose as a fact)"
    try:
        validate_readonly_sql(sql)
    except SQLGuardrailError as e:
        return f"its SQL fails the read-only check: {e}"
    if known is None or not known.judgeable:
        return None
    unknown, hidden = [], []
    for schema, table in recipe_tables(sql):
        key = f"{schema}.{table}".lower() if schema else table.lower()
        if key in known.names:
            continue
        (hidden if key in known.hidden else unknown).append(
            f"{schema}.{table}" if schema else table)
    if hidden:
        return f"it reads tables the owner hid: {', '.join(hidden)}"
    if unknown:
        return f"it reads tables that do not exist in the business database: {', '.join(unknown)}"
    return None


# A lesson names tables in prose. Only code-like identifiers are taken as
# table names (snake_case, CamelCase, schema.table, or quoted), so "the
# telemetry table" is not mistaken for a table called telemetry.
_CODE_IDENT = r"(?:[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]*|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)"
# Keywords are case-insensitive via scoped (?i:...); the identifier is not,
# or every plain word would look like CamelCase.
_CAMEL_IDENT = r"(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)"
_LESSON_TABLE_RES = (
    re.compile(rf"(?i:\b(?:from|join|into|table))"
               rf"\s+[`\"]?((?:[A-Za-z_]\w*\.)?{_CODE_IDENT})\b[`\"]?(?!\()"),
    # After a weak word ("in", "scan") only CamelCase counts: snake_case
    # there is usually a column ("filter in created_at").
    re.compile(rf"(?i:\b(?:in|scan|scanning|query|querying))"
               rf"\s+[`\"]?((?:[A-Za-z_]\w*\.)?{_CAMEL_IDENT})\b[`\"]?(?!\()"),
    re.compile(rf"\b((?:[A-Za-z_]\w*\.)?{_CODE_IDENT})[`\"]?\s+(?i:table|view)\b"),
    re.compile(r"[`\"]((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)[`\"]\s+(?i:table|view)\b"),
    re.compile(rf"\b({_CODE_IDENT})\.[a-z_][a-z0-9_]*\b"),        # table.column
)
_NOT_TABLES = {"queryCanceledError", "statement_timeout", "run_sql", "run_sql_to_csv",
               "run_python", "use_skill", "remember_fact", "search_business_context"}


def lesson_tables(content: str) -> list[str]:
    """Table names a lesson mentions (prose patterns plus any SQL it quotes)."""
    found: list[str] = []
    for rx in _LESSON_TABLE_RES:
        for mt in rx.finditer(content or ""):
            name = mt.group(1)
            if name and name not in found and name not in _NOT_TABLES \
                    and not name.lower().endswith("error"):
                found.append(name)
    sql = extract_sql(content or "")
    if sql:
        try:
            for schema, table in recipe_tables(sql):
                name = f"{schema}.{table}" if schema else table
                if name not in found:
                    found.append(name)
        except Exception:
            pass
    return found


def lesson_problem(content: str, known: KnownTables | None) -> str | None:
    """Why this lesson must not be stored, or None. Pure. Same rule as a
    recipe's tables: every table it names must exist (and not be hidden)."""
    if known is None or not known.judgeable:
        return None
    unknown, hidden = [], []
    for name in lesson_tables(content):
        key = name.lower()
        if key in known.names:
            continue
        if "." in key and any(part in known.names for part in key.split(".", 1)):
            continue                  # schema.table or table.column
        (hidden if key in known.hidden else unknown).append(name)
    if hidden:
        return f"it names tables the owner hid: {', '.join(hidden)}"
    if unknown:
        return f"it names tables that do not exist in the business database: {', '.join(unknown)}"
    return None


_LAYOUT_TERMS = re.compile(
    r"\b(bar chart|line chart|pie chart|donut|scatter|histogram|heatmap|chart|"
    r"panel|tile|widget|card|layout|grid|legend|x-axis|y-axis|axis|"
    r"top[- ]left|top[- ]right|bottom[- ]left|bottom[- ]right|left column|"
    r"right column|first row|second row|top row|bottom row|sidebar|colour|color)s?\b",
    re.I)


def layout_fact_problem(content: str) -> str | None:
    """A "fact" that only describes how a dashboard's charts are laid out is
    not business knowledge. Detected cheaply: it names a dashboard and uses
    two or more distinct layout terms. Pure."""
    text = content or ""
    if not re.search(r"\bdashboards?\b", text, re.I):
        return None
    terms = {m.group(1).lower().replace("-", " ") for m in _LAYOUT_TERMS.finditer(text)}
    if len(terms) >= 2:
        return "it describes a dashboard's chart layout, not the business"
    return None


# Quick 014: a "preference" that is a request for one deliverable. Matched on
# the deliverable phrase ("prefers to receive a report", "asked for a list")
# with no standing marker; "every" alone is not one (it is usually part of the
# request: "a report listing every missed fuel drop").
_ONE_OFF_REQUEST = re.compile(
    r"\b(?:prefers?|preferred|wants?|wanted|asked|asks|requested|requests|would\s+like|needs?|needed)"
    r"\s+(?:to\s+(?:receive|get|see|have|be\s+sent|be\s+given)\s+|for\s+)?"
    r"(?:a|an|the|this|that|one)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:report|list|listing|file|excel|spreadsheet|sheet|csv|pdf|chart|graph|table|summary|"
    r"breakdown|export|document|dashboard)s?\b",
    re.I)
_STANDING = re.compile(
    r"\b(?:always|whenever|from\s+now\s+on|going\s+forward|by\s+default|in\s+(?:the\s+)?future|"
    r"every\s+time|each\s+time|as\s+a\s+rule|never|daily|weekly|monthly|quarterly|"
    r"(?:every|each)\s+(?:day|week|month|morning|evening|night|quarter|year|shift|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I)


def preference_problem(content: str) -> str | None:
    """Why this "preference" is a one-off request, or None. Pure."""
    text = content or ""
    if _ONE_OFF_REQUEST.search(text) and not _STANDING.search(text):
        return ("it is a one-off request, not a standing preference "
                "(a preference must apply to future work)")
    return None


async def check_learning(kind: str, content: str) -> str | None:
    """Reason to refuse storing this learning, or None to store it. Never
    raises: a check that cannot run lets the learning through (logged)."""
    try:
        if kind == "recipe":
            known = await known_tables()
            if not known.judgeable:
                log.info("recipe check: no table catalog available; storing "
                         "without the table check")
            return recipe_problem(content, known)
        if kind == "lesson":
            known = await known_tables()
            return lesson_problem(content, known)
        if kind == "fact":
            return layout_fact_problem(content)
        if kind == "preference":
            return preference_problem(content)
    except Exception:
        log.warning("learning check failed; storing unchecked", exc_info=True)
    return None


async def _dedup_store(client: AsyncOpenAI, pool, kind: str, content: str, *,
                       thread_id: str | None = None, run_id: int | None = None,
                       source: str | None = None, scope: str | None = None,
                       graph_skip: str | None = None, kept: list | None = None) -> str:
    """One code path for storing a learning with cosine dedup/merge.

    >= 0.97 similarity to an existing learning of the same kind → duplicate, skip.
    0.85–0.97 → LLM-merge into the existing row (UPDATE; keeps its existing source).
    Otherwise → INSERT a new row carrying `source`.
    Quick 020: only learnings in the same scope are compared (one person's
    preference never merges into another's); `scope` lands on inserted rows.
    Quick 038: `graph_skip` (graph_skip_reason, from reflection only) keeps a
    fact out of the knowledge graph: it is stored and recalled like any other
    learning, learnings.graph_skip says why, and its id is appended to `kept`.
    A merge keeps a row out only when the row was kept out already; a merge
    into a row the graph holds (or is waiting for) updates the graph as before,
    and a merge from an ungated turn clears the mark.
    Returns "duplicate" | "merged" | "stored".
    """
    emb = await _embed(content)
    keep_out = graph_skip if kind in GRAPHED_KINDS else None

    # Find the closest existing ACTIVE learning of the same kind. Superseded
    # rows (nightly consolidation) must never absorb or shadow new knowledge:
    # a dup/merge against one would be invisible to retrieve_learnings, which
    # filters to status='active'.
    match = await pool.fetchrow(
        """SELECT id, content, graph_skip, 1 - (embedding <=> $1::vector) AS score
           FROM learnings WHERE kind = $2 AND embedding IS NOT NULL
             AND status = 'active'
             AND scope IS NOT DISTINCT FROM $3
           ORDER BY embedding <=> $1::vector LIMIT 1""",
        emb, kind, scope,
    )

    if match and match["score"] >= 0.97:
        return "duplicate"  # effectively a duplicate — nothing new to keep

    if match and match["score"] >= 0.85:
        # Same topic, new information: merge and UPDATE the existing memory.
        merged = await _merge_learnings(client, match["content"], content)
        merged_emb = await _embed(merged)
        skip = keep_out if keep_out and match.get("graph_skip") else None
        revisions = await pool.fetchval(
            """UPDATE learnings SET content = $2, embedding = $3::vector,
               source_run = COALESCE($4, source_run), updated_at = now(),
               revisions = revisions + 1, graph_skip = $5
               WHERE id = $1 RETURNING revisions""",
            match["id"], merged, merged_emb, run_id, skip,
        )
        if skip:
            _note_kept(kept, match["id"])
        else:
            await to_graph(match["id"], merged, kind, revisions or 0, pool=pool)
        return "merged"

    new_id = await pool.fetchval(
        """INSERT INTO learnings (thread_id, kind, content, embedding, source_run, source, scope,
                                  graph_skip)
           VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8) RETURNING id""",
        thread_id, kind, content, emb, run_id, source, scope, keep_out,
    )
    if keep_out:
        _note_kept(kept, new_id)
    else:
        await to_graph(new_id, content, kind, 0, pool=pool)
    return "stored"


def _note_kept(kept: list | None, learning_id) -> None:
    if kept is not None:
        kept.append(learning_id)


# ── Quick 020: a learning nobody we can name wrote waits for the owner ─────

MAX_PENDING_LEARNINGS = 50   # open approval cards; a chatty group cannot flood Approvals

async def _store_pending(pool, kind: str, content: str, *, identity,
                         thread_id: str | None, run_id: int | None) -> str:
    """Store an unattributed learning as status 'pending' with its approval
    card (memscope.propose_activation), in one transaction. Never merged into
    an existing row and never sent to the graph: it is not knowledge until the
    owner approves it. A >= 0.97 match to an active or pending learning of the
    same kind and scope is a duplicate (nothing new to approve).
    Returns "pending" | "duplicate" (an active one says it) |
    "pending_duplicate" (one already waits) | "pending_full" (too many wait)."""
    from . import memscope
    scope = identity.scope_for(kind)
    emb = await _embed(content)
    async with pool.acquire() as conn:
        async with conn.transaction():
            match = await conn.fetchrow(
                """SELECT id, status, 1 - (embedding <=> $1::vector) AS score
                   FROM learnings WHERE kind = $2 AND embedding IS NOT NULL
                     AND status IN ('active', 'pending')
                     AND scope IS NOT DISTINCT FROM $3
                   ORDER BY embedding <=> $1::vector LIMIT 1""",
                emb, kind, scope)
            if match and match["score"] >= 0.97:
                return "duplicate" if match["status"] == "active" else "pending_duplicate"
            waiting = await conn.fetchval(
                "SELECT count(*) FROM actions WHERE kind = 'activate_learning' "
                "AND status = 'proposed'")
            if (waiting or 0) >= MAX_PENDING_LEARNINGS:
                return "pending_full"
            new_id = await conn.fetchval(
                """INSERT INTO learnings (thread_id, kind, content, embedding, source_run,
                                          source, scope, status, status_reason)
                   VALUES ($1, $2, $3, $4::vector, $5, $6, $7, 'pending', $8) RETURNING id""",
                thread_id, kind, content, emb, run_id, identity.source, scope,
                f"waiting for the owner's approval: from {identity.label}"[:500])
            action_id = await memscope.propose_activation(
                conn, learning_id=new_id, kind=kind, content=content,
                identity=identity, run_id=run_id)
    log.info("memory: %s learning %s from %s held for approval (action #%s)",
             kind, new_id, identity.source, action_id)
    return "pending"


async def store_learning(content: str, kind: str, source: str | None = None, *,
                         thread_id: str | None = None,
                         run_id: int | None = None,
                         scope: str | None = None,
                         identity=None) -> str:
    """Store a single learning through the same dedup/merge path as reflection.

    `thread_id`/`run_id` preserve the which-conversation/which-run attribution
    trail on inserted rows (e.g. the remember_fact tool passes its context vars).
    Quick 020: `identity` (memscope.RunIdentity) decides the provenance, where
    a preference applies, and whether the learning is held: a run for nobody we
    can name (a group, an unknown sender, an automatic run) stores it pending
    with an approval card. Returns "stored" | "merged" | "duplicate" |
    "pending". Raises ValueError on an invalid kind.
    """
    content = (content or "").strip()
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid learning kind: {kind!r}")
    if len(content) < 10:
        raise ValueError("learning content too short")
    problem = await check_learning(kind, content)
    if problem:
        log.warning("memory: refused %s learning (%s): %r", kind, problem, content[:200])
        raise ValueError(f"not stored: {problem}")
    pool = await get_pool()
    if identity is not None:                       # Quick 020
        if not identity.attributed:
            return await _store_pending(pool, kind, content, identity=identity,
                                        thread_id=thread_id, run_id=run_id)
        source = source or identity.source
        scope = identity.scope_for(kind)
    client = _ledger(AsyncOpenAI(api_key=get_settings().openai_api_key, max_retries=6,
                                 timeout=30.0), "learnings")
    return await _dedup_store(client, pool, kind, content,
                              thread_id=thread_id, run_id=run_id, source=source,
                              scope=scope)


_ERROR_MARKERS = ("SQL ERROR", "TOOL ERROR", "ERROR:", "QueryCanceledError",
                  "statement timeout", "canceling statement", "Traceback")


def _is_error(t: dict) -> bool:
    digest = str(t.get("result_digest") or "")
    return any(mk in digest[:400] for mk in _ERROR_MARKERS)


def run_hardness(tool_log: list[dict], duration_s: float | None = None) -> list[str]:
    """Why a run counts as hard (empty list = not hard): many tool calls, any
    tool/SQL error or timeout (each one is followed by a retry), or a long
    duration. Pure."""
    why: list[str] = []
    n = len(tool_log or [])
    errors = sum(1 for t in tool_log or [] if _is_error(t))
    timeouts = sum(1 for t in tool_log or []
                   if "QueryCanceledError" in str(t.get("result_digest") or "")
                   or "statement timeout" in str(t.get("result_digest") or ""))
    if n >= HARD_TOOL_CALLS:
        why.append(f"{n} tool calls")
    if errors:
        why.append(f"{errors} failed tool call(s)" + (f", {timeouts} timeout(s)" if timeouts else ""))
    if duration_s is not None and duration_s >= HARD_SECONDS:
        why.append(f"{int(duration_s)} s")
    return why


def _tool_entry(t: dict, i: int, args_chars: int, result_chars: int) -> dict:
    args = t.get("args")
    if not isinstance(args, str):
        args = json.dumps(args, default=str)
    return {"step": i + 1, "tool": t.get("tool"), "args": (args or "")[:args_chars],
            "result_snippet": str(t.get("result_digest") or "")[:result_chars]}


def build_transcript(user_message: str, reply: str, tool_log: list[dict],
                     cap: int = TRANSCRIPT_CAP) -> str:
    """The reflection transcript, fitted to `cap` characters by choosing WHAT
    to keep rather than cutting the tail (Quick 009). Always kept: the first
    message, the final reply, every failed call with the call that followed it
    (the fix), and the last successful calls (what finally worked); then the
    rest in order while there is room. Dropped stretches are marked."""
    log_ = list(tool_log or [])
    user = (user_message or "")[:2000]
    final = (reply or "")[:2500]

    def render(keep: set[int], args_chars: int, result_chars: int) -> str:
        tools, last = [], -1
        for i in sorted(keep):
            if i - last > 1:
                tools.append({"omitted_calls": i - last - 1})
            tools.append(_tool_entry(log_[i], i, args_chars, result_chars))
            last = i
        if log_ and last < len(log_) - 1:
            tools.append({"omitted_calls": len(log_) - 1 - last})
        return json.dumps({"user": user, "assistant": final, "tool_calls": len(log_),
                           "tools": tools}, default=str)

    must: set[int] = set()
    for i, t in enumerate(log_):
        if _is_error(t):
            must.add(i)
            if i + 1 < len(log_):
                must.add(i + 1)
    ok = [i for i, t in enumerate(log_) if not _is_error(t)]
    must.update(ok[-3:])                       # what finally worked
    must.update(range(min(2, len(log_))))      # how it started
    for args_chars, result_chars in ((900, 400), (600, 300), (400, 200), (250, 150)):
        if len(render(must, args_chars, result_chars)) <= cap:
            break
    keep = set(must)
    for i in range(len(log_)):                 # fill the rest in order
        if i in keep:
            continue
        keep.add(i)
        if len(render(keep, args_chars, result_chars)) > cap:
            keep.discard(i)
            break
    out = render(keep, args_chars, result_chars)
    return out if len(out) <= cap else out[:cap]


async def _run_seconds(run_id: int) -> float | None:
    try:
        pool = await get_pool()
        v = await pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM (COALESCE(ended_at, now()) - started_at)) "
            "FROM task_runs WHERE id = $1", run_id)
        return float(v) if v is not None else None
    except Exception:
        return None


# ── Quick 022: memory calls on Anthropic (Haiku 5.5) or OpenAI ──
# A Claude model runs on Anthropic through llmclient (ledger, effort from
# reflection_effort, no sampling keys); anything else, or a Claude model with no
# Anthropic key, runs the pre-022 OpenAI path. gpt-4o-mini is that path's model.
OPENAI_MEMORY_MODEL = "gpt-4o-mini"
MEMORY_MODEL_DEFAULT = "claude-haiku-5-5"

REFLECTION_SCHEMA = {
    "type": "object",
    "properties": {"learnings": {"type": "array", "items": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": list(VALID_KINDS)},
                       "content": {"type": "string"}},
        "required": ["kind", "content"], "additionalProperties": False}}},
    "required": ["learnings"], "additionalProperties": False}


def memory_model(settings=None) -> str:
    """The model for learning merges, case distillation and playbook proposals
    (setting reflection_model). Reflection itself uses its Agents-page config."""
    s = settings or get_settings()
    return str(getattr(s, "reflection_model", "") or "").strip() or MEMORY_MODEL_DEFAULT


def openai_memory_model(model: str) -> str:
    """The OpenAI model a memory call uses when `model` cannot run on Anthropic."""
    name = (model or "").strip()
    if not name or name.lower().startswith("claude"):
        return OPENAI_MEMORY_MODEL
    return name


def _parse_learnings(text) -> list:
    """{"learnings": [...]} or a bare array; [] for anything else."""
    parsed = json.loads(text or "{}")
    items = parsed if isinstance(parsed, list) else parsed.get("learnings", [])
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


async def reflection_items(rcfg: dict, prompt: str, transcript: str, max_tokens: int,
                           *, settings=None) -> tuple:
    """One reflection call. Returns (items, client): the OpenAI client is handed on
    to the dedup merges (None on the Anthropic path, which builds its own)."""
    settings = settings or get_settings()
    model = str(rcfg.get("model") or "").strip() or MEMORY_MODEL_DEFAULT
    if llmclient.anthropic_model(model, settings):
        text = await llmclient.anthropic_text(
            model, system=prompt, user=transcript, role="reflection",
            ledger_role="reflection", max_tokens=int(max_tokens or 500),
            json_schema=REFLECTION_SCHEMA, settings=settings)
        return _parse_learnings(text), None
    client = _ledger(AsyncOpenAI(api_key=settings.openai_api_key, max_retries=6,
                                 timeout=30.0), "reflection")
    res = await client.chat.completions.create(
        model=openai_memory_model(model),
        max_tokens=max_tokens,
        temperature=rcfg.get("temperature"),
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": transcript}],
        response_format={"type": "json_object"},
    )
    return _parse_learnings(res.choices[0].message.content), client


async def memory_text(system: str, user: str, *, ledger_role: str, max_tokens: int,
                      temperature: float = 0.2, json_object: bool = False,
                      settings=None) -> str:
    """One memory-model call (case distillation, playbook proposals). Returns the
    text; raises on failure (callers fail soft)."""
    settings = settings or get_settings()
    model = memory_model(settings)
    if llmclient.anthropic_model(model, settings):
        return await llmclient.anthropic_text(
            model, system=system, user=user, role="reflection", ledger_role=ledger_role,
            max_tokens=max_tokens, settings=settings)
    client = _ledger(AsyncOpenAI(api_key=settings.openai_api_key, max_retries=6,
                                 timeout=30.0), ledger_role)
    kw = {"response_format": {"type": "json_object"}} if json_object else {}
    res = await client.chat.completions.create(
        model=openai_memory_model(model), max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}], **kw)
    return (res.choices[0].message.content or "").strip()


# ── Quick 038: what a turn sends to the knowledge graph, and trivial turns ──
# Every fact reflection stores is mirrored into the graph, and one graph episode
# costs about six calls on the graph extraction model (gpt-5.5 by default). Two
# kinds of turn carry nothing the graph needs:
#   * a dashboard build or revision: its reply is a long summary of what was built,
#     which reflection mines for "facts" the dashboard itself already holds;
#   * a tool-only turn: status checks and proposals or actions whose outcome is kept
#     in its own record (an approval, a watch, a case, a sync), with no tool that
#     reads business data or knowledge and no message to a person.
# Those turns still reflect and every learning is still stored and recalled from
# Postgres; only the graph mirror is skipped (_dedup_store, learnings.graph_skip).
# A turn where the owner stated something (remember_fact) is never gated, and
# remember_fact itself always graphs. Setting graph_skip_low_value_turns.
GRAPH_SKIP_DASHBOARD = "dashboard turn"
GRAPH_SKIP_TOOL_ONLY = "tool-only turn"
DASHBOARD_TURN_TOOLS = frozenset({
    "delegate_to_dashboard_agent", "get_dashboard", "preview_dashboard",
    "save_dashboard_version"})
TOOL_ONLY_TOOLS = frozenset({
    # status checks
    "list_dashboard_proposals", "list_cases", "get_sync_status",
    # proposals and actions whose outcome lives in its own record
    "create_watch", "propose_kpi", "propose_forecast", "propose_next_action", "open_case",
    "propose_model_change", "propose_relationships_from_keys", "propose_source",
    "request_sync", "propose_mode_change", "propose_context_change"})
NEUTRAL_TOOLS = frozenset({"send_progress", "use_skill", "skill_asset"})
GATE_NEVER_TOOLS = frozenset({"remember_fact"})


def _tool_names(tool_log) -> set[str]:
    """Tool names of a run's calls; a refusal ('<name>:refused') counts as its tool."""
    return {str((t.get("tool") if isinstance(t, dict) else "") or "").split(":", 1)[0]
            for t in tool_log or []} - {""}


def graph_skip_reason(tool_log) -> str | None:
    """Why the facts reflection draws from this turn stay out of the graph
    (GRAPH_SKIP_DASHBOARD or GRAPH_SKIP_TOOL_ONLY), or None. Pure."""
    names = _tool_names(tool_log)
    if names & GATE_NEVER_TOOLS:
        return None
    if names & DASHBOARD_TURN_TOOLS:
        return GRAPH_SKIP_DASHBOARD
    acting = names - NEUTRAL_TOOLS
    if acting and acting <= TOOL_ONLY_TOOLS:
        return GRAPH_SKIP_TOOL_ONLY
    return None


# A trivial turn: no tool calls at all and a message made only of greeting,
# thanks and acknowledgement words (emoji and punctuation ignored). Answers such
# as yes, no and sure are not trivial: they can confirm a standing rule.
TRIVIAL_MAX_CHARS = 60
TRIVIAL_MAX_WORDS = 8
TRIVIAL_WORDS = frozenset("""
    hi hii hiii hello helo hey heya hiya yo hola namaste namaskar greetings
    good morning afternoon evening night day gm gn
    thanks thank thanku thankyou thx thnx thnks tnx ty tysm cheers appreciated appreciate
    it you so very much a lot lots again all ji sir
    ok okay okey okk k kk alright fine cool great nice perfect awesome super excellent
    wonderful lovely brilliant got noted understood sounds job work well done
    bye byebye goodbye see later cya ttyl take care welcome
    haha lol hmm theek thik hai acha accha achha shukriya dhanyavad dhanyawad
""".split())
_TRIVIAL_WORD_RE = re.compile(r"[^\W\d_]+")


def is_trivial_turn(user_message: str | None, tool_log) -> bool:
    """True for a turn reflection has nothing to learn from: no tool calls, a
    short message, no figures, every word a pleasantry. Pure."""
    if tool_log:
        return False
    text = (user_message or "").strip()
    if len(text) > TRIVIAL_MAX_CHARS or any(ch.isdigit() for ch in text):
        return False
    words = [w.lower() for w in _TRIVIAL_WORD_RE.findall(text)]
    return len(words) <= TRIVIAL_MAX_WORDS and all(w in TRIVIAL_WORDS for w in words)


async def _record_skip(name: str, ref: tuple[str, str], run_id, summary: str,
                       counts: dict) -> None:
    """One job_runs row per kind of skip, summed while fresh (System > Runs), so
    the saving can be counted later. Never raises."""
    try:
        from . import jobruns
        await jobruns.record("memory_skip", name, summary=summary, counts=counts, ref=ref,
                             routine=True, collapse="sum", task_run_id=run_id)
    except Exception:
        log.debug("recording a memory skip failed", exc_info=True)
# ── End Quick 038 ──


async def reflect_and_store(run_id: int, thread_id: str, user_message: str,
                            reply: str, tool_log: list[dict],
                            source: str | None = None,
                            scope: str | None = None) -> int:
    """Distill learnings from a finished task. Returns number stored.

    Quick 009: a hard run (run_hardness) gets a fitted transcript that keeps
    its errors and the fix, room for up to 5 items, and an instruction to keep
    at least one lesson. Quick 020: a preference lands in `scope` (the person
    the run is for, memscope); every other kind is shared. Quick 038: a trivial
    turn is not reflected on (never a hard run), and the facts of a dashboard or
    tool-only turn stay out of the graph (graph_skip_reason)."""
    settings = get_settings()
    try:
        why = run_hardness(tool_log, await _run_seconds(run_id))
        if (not why and getattr(settings, "reflection_skip_trivial", True)
                and is_trivial_turn(user_message, tool_log)):
            log.info("memory: run %s is a trivial turn (no tools, a pleasantry); "
                     "not reflected", run_id)
            await _record_skip("Reflection skipped: trivial turn", ("reflection", "trivial_turn"),
                               run_id, "Turns with no tools that were only a greeting, thanks "
                               "or ok were not reflected on", {"turns": 1})
            return 0
        graph_skip = (graph_skip_reason(tool_log)
                      if getattr(settings, "graph_skip_low_value_turns", True) else None)

        # Full reflection config editable via dashboard (agent_configs key 'reflection').
        from .agents.graph import effective_configs
        rcfg = (await effective_configs())["reflection"]
        prompt = rcfg["prompt"] or REFLECTION_PROMPT

        max_items = HARD_MAX_ITEMS if why else NORMAL_MAX_ITEMS
        max_tokens = max(int(rcfg["max_tokens"] or 0), HARD_MAX_TOKENS) if why \
            else rcfg["max_tokens"]
        prompt = prompt + PREFERENCE_RULE
        if why:
            prompt = prompt + HARD_RUN_ADDENDUM.format(why=", ".join(why),
                                                       max_items=max_items)
            log.info("memory: run %s was hard (%s); reflecting for lessons",
                     run_id, ", ".join(why))

        transcript = build_transcript(user_message, reply, tool_log)
        items, client = await reflection_items(rcfg, prompt, transcript, max_tokens,
                                               settings=settings)

        pool = await get_pool()
        stored = updated = 0
        kept: list = []                                  # Quick 038
        for item in items[:max_items]:
            kind = item.get("kind")
            content = (item.get("content") or "").strip()
            if kind not in VALID_KINDS or len(content) < 10:
                continue
            problem = await check_learning(kind, content)
            if problem:
                log.warning("memory: refused %s learning from run %s (%s): %r",
                            kind, run_id, problem, content[:200])
                continue

            outcome = await _dedup_store(client, pool, kind, content,
                                         thread_id=thread_id, run_id=run_id,
                                         source=source,
                                         scope=scope if kind == "preference" else None,
                                         graph_skip=graph_skip, kept=kept)
            if outcome == "stored":
                stored += 1
            elif outcome == "merged":
                updated += 1
        if stored or updated:
            log.info("memory: %d new, %d updated learning(s) from run %s", stored, updated, run_id)
        if kept:
            log.info("memory: %d fact(s) from run %s kept out of the graph (%s)",
                     len(kept), run_id, graph_skip)
            await _record_skip(f"Graph ingestion skipped: {graph_skip}",
                               ("graph", graph_skip.replace(" ", "_").replace("-", "_")),
                               run_id, f"Facts from {graph_skip}s were stored as learnings, "
                               "not sent to the knowledge graph",
                               {"facts": len(kept), "turns": 1})
        return stored + updated
    except Exception:
        log.exception("reflection failed for run %s", run_id)
        return 0


MERGE_PROMPT = """Two learnings about the same topic — an existing one and a newer one.
Merge them into ONE self-contained learning. Prefer the newer information where they
conflict; keep concrete specifics (SQL, table/column names, numbers, preferences).
Return ONLY the merged learning text, no preamble."""


async def merge_learning_contents(contents: list[str]) -> str:
    """Merge several learnings about one topic into a single text, oldest first
    (later items win conflicts, matching _merge_learnings semantics). Public
    entry point for the nightly consolidation job — reuses the same merge
    prompt/model as the reflection dedup path. Falls back to the newest text on
    any LLM failure (via _merge_learnings' own fail-soft)."""
    contents = [c for c in (contents or []) if (c or "").strip()]
    if not contents:
        return ""
    if len(contents) == 1:
        return contents[0]
    merged = contents[0]
    for newer in contents[1:]:
        merged = await _merge_learnings(None, merged, newer, ledger_role="consolidation")
    return merged


async def _merge_learnings(client, old: str, new: str, *,
                           ledger_role: str = "learnings") -> str:
    """Quick 022: on the memory model (reflection_model, default claude-haiku-5-5).
    `client` is an OpenAI client to reuse on the OpenAI path, or None."""
    try:
        user = f"EXISTING:\n{old}\n\nNEWER:\n{new}"
        settings = get_settings()
        model = memory_model(settings)
        if llmclient.anthropic_model(model, settings):
            merged = await llmclient.anthropic_text(
                model, system=MERGE_PROMPT, user=user, role="reflection",
                ledger_role=ledger_role, max_tokens=400, settings=settings)
        else:
            client = client or _ledger(AsyncOpenAI(api_key=settings.openai_api_key,
                                                   max_retries=6, timeout=30.0), ledger_role)
            res = await client.chat.completions.create(
                model=openai_memory_model(model),
                max_tokens=400,
                temperature=0.1,
                messages=[{"role": "system", "content": MERGE_PROMPT},
                          {"role": "user", "content": user}],
            )
            merged = (res.choices[0].message.content or "").strip()
        return merged if len(merged) >= 10 else new
    except Exception:
        log.exception("learning merge failed — keeping newer version")
        return new


# Quick 020: facts only. A preference belongs to the person who stated it
# (learnings.scope); in the shared graph every run would recall it. Episodes
# written before this are hidden from agents by memoryservice.search and
# removed by a rebuild.
GRAPHED_KINDS = ("fact",)
# Kinds whose graph episodes are retired on delete or supersede (Quick 019):
# preferences were mirrored before Quick 020, so theirs still are.
RETIRE_KINDS = ("fact", "preference")


def learning_episode_name(learning_id: int, revisions: int = 0) -> str:
    """Graph episode name for one revision of a learning (backfill matches it)."""
    return f"learning_{learning_id}_r{int(revisions or 0)}"


async def to_graph(learning_id: int | None, content: str, kind: str,
                   revisions: int = 0, *, pool=None) -> bool:
    """Mirror a learning into the temporal knowledge graph (Phase 4). Kept in
    parallel with the pgvector `learnings` table. Facts are worth graphing;
    corrections/recipes stay pgvector-only (they're procedural, not entity
    knowledge), and so do preferences since Quick 020 (they are personal). Quick 009: on success the row's
    graphed_at is stamped, so learnings written while the graph was down are
    found and sent later (memory_pipeline.backfill_graph). Fail-soft; returns
    whether the episode landed."""
    if kind not in GRAPHED_KINDS:
        return False
    try:
        from .memoryservice import memory_graph
        name = learning_episode_name(learning_id, revisions) if learning_id else None
        ok = await memory_graph.add_episode(content, source=f"reflection:{kind}",
                                            name=name)
        if ok and learning_id:
            pool = pool or await get_pool()
            # Quick 038: once in the graph, a fact is no longer kept out of it.
            await pool.execute(
                "UPDATE learnings SET graphed_at = now(), graph_skip = NULL WHERE id = $1",
                learning_id)
        return bool(ok)
    except Exception:
        log.debug("graph mirror of learning failed (non-fatal)", exc_info=True)
        return False


RETIRE_TIMEOUT_SECONDS = 45
_TRIM = "chr(32) || chr(9) || chr(10) || chr(13)"


async def _retire_learning_episodes(learning_id: int, text: str, pool) -> dict | None:
    from .memoryservice import GLOBAL_GROUP, memory_graph
    eps = await memory_graph.find_learning_episodes(
        GLOBAL_GROUP, f"learning_{learning_id}_r", [text] if text else [])
    if eps is None:
        return None
    texts = sorted({str(e.get("content") or "").strip() for e in eps} - {""})
    shared: set[str] = set()
    if texts:
        pool = pool or await get_pool()
        shared = {r["t"] for r in await pool.fetch(
            f"""SELECT DISTINCT btrim(content, {_TRIM}) AS t FROM learnings
                 WHERE id <> $1 AND status = 'active' AND kind = ANY($3::text[])
                   AND btrim(content, {_TRIM}) = ANY($2::text[])""",
            learning_id, texts, list(GRAPHED_KINDS))}
    keep = [e for e in eps if str(e.get("content") or "").strip() in shared]
    res = await memory_graph.retire_episodes(
        GLOBAL_GROUP, [str(e["uuid"]) for e in eps if e not in keep and e.get("uuid")])
    if res is not None:
        res["shared"] = len(keep)
    return res


async def retire_learning_graph(learning_id: int, *, kind: str | None = None,
                                content: str | None = None, pool=None) -> dict | None:
    """Quick 019 (audit Fix 5(c)): take a deleted or superseded learning out
    of the knowledge graph: its episodes (every revision, learning_<id>_r*,
    plus a learning episode holding exactly its text, which add_episode may
    have stored under another learning's name) and what only those episodes
    support (memoryservice.retire_episodes). Facts other episodes also
    support stay.

    An episode whose exact text an active graphed learning still has is kept
    and counted as "shared": add_episode stores an identical text once, so
    that episode serves the active one too (a consolidation keeper whose
    merged text equals a loser's, for one). Skipped (returns {"skipped":
    why}) for kinds that are never graphed and when the graph is switched
    off. Returns the counts, or None when the graph could not be reached;
    that is recorded in job_runs (System > Runs), and a rebuild (POST
    /api/memory/rebuild) cleans it up. Never raises."""
    try:
        if kind is not None and kind not in RETIRE_KINDS:     # Quick 020
            return {"skipped": f"{kind} learnings are not in the graph"}
        if not get_settings().memory_graph_enabled:
            return {"skipped": "the knowledge graph is switched off"}
        res = await asyncio.wait_for(
            _retire_learning_episodes(int(learning_id), (content or "").strip(), pool),
            RETIRE_TIMEOUT_SECONDS)
    except Exception:
        log.warning("graph clean-up for learning %s failed", learning_id, exc_info=True)
        res = None
    if res is None:
        try:
            from . import jobruns
            await jobruns.record(
                "memory_graph", "graph clean-up", status="failed",
                summary=(f"learning {learning_id} left memory but its graph facts could "
                         "not be removed (graph not reachable); run a graph rebuild"),
                ref=("learning", str(learning_id)))
        except Exception:
            log.debug("recording the graph clean-up failure failed", exc_info=True)
    return res


def format_learnings(learnings: list[dict]) -> str:
    if not learnings:
        return ""
    lines = "\n".join(f"- [{l['kind']}] {l['content']}" for l in learnings)
    return f"\n\nLearned from previous conversations (use when relevant):\n{lines}"
