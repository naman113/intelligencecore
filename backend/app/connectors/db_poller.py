"""DB poller — read-only polling of the tenant Postgres with a watermark.

This is the connector that makes the ambient system AWARE of database changes: it
turns new rows into ambient `events`, which flow into triage → cases/expectations
and, downstream, into the knowledge graph. (Contrast the watchdog/sentinel, which
only *notifies* a person on a state change and touches no ambient memory.)

Compile-once / execute-mindlessly: the table + watermark column are frozen in
config (proposed once, human-approved). Every poll is then a plain cached SELECT
past the watermark — NO LLM. Config lives in an `agent_configs` row named
'db_sources':

    {"sources": [
        {"table": "\"Machines\"", "watermark_column": "\"CreatedAt\"", "label": "machine"}
    ]}

On first sight of a table (no watermark) we record the current max as a baseline
and emit nothing — we watch from now on, we don't replay all history. A webhook-
based source can later replace this by producing the same RawEvents.
"""

import json
import logging
from datetime import datetime, timezone

from . import RawEvent
from ..db import get_pool
from ..guardrails import run_readonly_sql

log = logging.getLogger("connectors.db")

MAX_ROWS_PER_POLL = 50   # bound the blast radius of a backfill
CONFIG_KEY = "db_sources"
POLL_SECONDS_KEY = "db_poll_seconds"   # runtime override (dashboard-editable, no restart)
MIN_POLL_SECONDS = 5


async def get_poll_seconds() -> int:
    """Live poll interval: dashboard override if set, else the .env default. Read
    each loop so changes apply without a restart."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT config FROM agent_configs WHERE agent_name = $1", POLL_SECONDS_KEY)
    if row and row["config"]:
        try:
            cfg = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            n = int(cfg.get("seconds"))
            if n > 0:
                return max(MIN_POLL_SECONDS, n)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    from ..config import get_settings
    return max(MIN_POLL_SECONDS, get_settings().db_poll_seconds)


async def set_poll_seconds(seconds: int) -> int:
    n = max(MIN_POLL_SECONDS, int(seconds))
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO agent_configs (agent_name, config) VALUES ($1, $2::jsonb)
           ON CONFLICT (agent_name) DO UPDATE SET config = $2::jsonb, updated_at = now()""",
        POLL_SECONDS_KEY, json.dumps({"seconds": n}))
    return n


async def _load_sources() -> list[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT config FROM agent_configs WHERE agent_name = $1", CONFIG_KEY)
    if not row or not row["config"]:
        return []
    try:
        cfg = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
        return [s for s in cfg.get("sources", []) if s.get("table") and s.get("watermark_column")]
    except (json.JSONDecodeError, AttributeError, TypeError):
        log.warning("db_sources config is malformed; ignoring")
        return []


async def _watermark(table: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT watermark FROM db_watermarks WHERE source_table = $1", table)
    if not row:
        return None
    return json.loads(row["watermark"]) if isinstance(row["watermark"], str) else row["watermark"]


async def _set_watermark(table: str, column: str, value) -> None:
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO db_watermarks (source_table, watermark, updated_at)
           VALUES ($1, $2::jsonb, now())
           ON CONFLICT (source_table) DO UPDATE SET watermark = $2::jsonb, updated_at = now()""",
        table, json.dumps({"column": column, "value": str(value)}, default=str))


class DbPoller:
    name = "db_poller"

    async def poll(self) -> list[RawEvent]:
        sources = await _load_sources()
        out: list[RawEvent] = []
        self.errors: list[str] = []  # Quick 008: per-source failures, for Runs
        for src in sources:
            try:
                out.extend(await self._poll_source(src))
            except Exception as e:
                log.exception("db poll failed for %s", src.get("table"))
                self.errors.append(f"{str(src.get('table') or '?').strip(chr(34))}: "
                                   f"{type(e).__name__}: {str(e)[:160]}")
        return out

    async def _poll_source(self, src: dict) -> list[RawEvent]:
        table = src["table"]              # already double-quoted PascalCase by config
        col = src["watermark_column"]     # already double-quoted
        label = src.get("label") or table.strip('"')
        wm = await _watermark(table)

        if wm is None:
            # Baseline: record current max, emit nothing.
            rows = await run_readonly_sql(f"SELECT max({col}) AS mx FROM {table}")
            mx = rows[0]["mx"] if rows else None
            await _set_watermark(table, col, mx if mx is not None else "")
            log.info("db: baselined %s at %s", table, mx)
            return []

        last = wm.get("value")
        # Use '>=' (not '>') so rows tied at the last watermark value aren't skipped
        # when they fell past a previous LIMIT page; the (source, external_id) unique
        # constraint drops the re-emitted boundary rows. An untyped quoted literal
        # ('5' / '2026-..') is coerced by Postgres to the column's type, so this works
        # for int/timestamp/uuid. No watermark yet (empty baseline) → no predicate.
        where = f" WHERE {col} >= {_sql_literal(last)}" if last not in (None, "") else ""
        rows = await run_readonly_sql(
            f"SELECT * FROM {table}{where} ORDER BY {col} LIMIT {MAX_ROWS_PER_POLL}")

        events: list[RawEvent] = []
        new_wm = last
        for row in rows:
            wm_val = row.get(_unquote(col))
            new_wm = wm_val if wm_val is not None else new_wm
            events.append(RawEvent(
                source="db",
                # Hash the whole row so two rows sharing a watermark value get
                # distinct external_ids (keying on wm_val alone silently drops one).
                external_id=f"{table}:{wm_val}:{_row_fingerprint(row)}",
                content=f"New {label}: " + json.dumps(row, default=str)[:2000],
                occurred_at=_row_time(wm_val),
                thread_ref=table,
                actor_raw="db",
                meta={"table": table, "label": label, "row": _jsonable(row)},
            ))
        if new_wm != last:
            await _set_watermark(table, col, new_wm)
        if events:
            log.info("db: %d new row(s) from %s", len(events), table)
        return events


# ── config: read / write / propose (the one-time LLM-assisted setup) ───────

async def get_sources() -> list[dict]:
    """Current watch-list (the frozen config the poller executes mindlessly)."""
    return await _load_sources()


async def set_sources(sources: list[dict]) -> list[dict]:
    """Persist the approved watch-list. Only table + watermark_column are stored;
    both must be present (that's what makes execution mindless afterward)."""
    clean = [{"table": s["table"], "watermark_column": s["watermark_column"],
              "label": s.get("label") or _unquote(s["table"])}
             for s in sources if s.get("table") and s.get("watermark_column")]
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO agent_configs (agent_name, config) VALUES ($1, $2::jsonb)
           ON CONFLICT (agent_name) DO UPDATE SET config = $2::jsonb, updated_at = now()""",
        CONFIG_KEY, json.dumps({"sources": clean}))
    return clean


async def add_source(table: str, watermark_column: str | None = None) -> dict:
    """Manually add one table to the watch-list. Verifies it exists; auto-detects a
    watermark column if none given. If no obvious watermark and none supplied,
    returns the table's columns so the caller can pick one."""
    table = (table or "").strip().strip('"')
    if not table:
        return {"error": "table name required"}
    safe = table.replace("'", "''")
    exists = await run_readonly_sql(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' "
        f"AND table_name='{safe}' LIMIT 1")
    if not exists:
        return {"error": f"table “{table}” not found in the tenant DB"}

    col = (watermark_column or "").strip().strip('"')
    if not col:
        wm = await _detect_watermark(table)
        if wm:
            col = wm["col"]
        else:
            cols = await run_readonly_sql(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{safe}' ORDER BY ordinal_position")
            return {"error": f"no obvious watermark on “{table}” — pick a column",
                    "columns": [c["column_name"] for c in cols]}

    sources = await _load_sources()
    if any(_unquote(s["table"]).lower() == table.lower() for s in sources):
        return {"added": table, "note": "already watched"}
    sources.append({"table": f'"{table}"', "watermark_column": f'"{col}"', "label": table})
    await set_sources(sources)
    return {"added": table, "watermark_column": col}


async def propose_sources() -> list[dict]:
    """The ONLY intelligent step (runs once, at setup). Recall from the REAL schema,
    precision from a cheap model:
      1. enumerate actual base tables from information_schema (so nothing is missed),
      2. detect a monotonic watermark column on each,
      3. classify which are genuine business ENTITIES/EVENTS worth watching vs.
         internal plumbing (auth, migrations, logs, lookups).
    Returns candidates (recommended first) for human approval. After approval the
    poller never needs a model again."""
    try:
        # Phase 14 (WH-03, Pitfall 7): mirrored relations are all VIEWs in information_schema
        from .. import warehouse
        if warehouse.warehouse_mode():
            rows = await run_readonly_sql(
                "SELECT table_name FROM ic_meta.source_catalog WHERE source = 'business_db' "
                "AND schema_name = 'public' AND relkind IN ('r','p') ORDER BY table_name")
        else:
            rows = await run_readonly_sql(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name")
    except Exception:
        log.exception("propose_sources: could not list tables")
        return []

    existing = {_unquote(s["table"]).lower() for s in await _load_sources()}
    candidates: list[dict] = []
    for r in rows:
        table = r["table_name"]
        if not table or table.lower() in existing:
            continue
        wm = await _detect_watermark(table)          # only watchable tables qualify
        if wm:
            candidates.append({
                "table": f'"{table}"', "watermark_column": f'"{wm["col"]}"',
                "label": table, "reason": wm["reason"]})
    if not candidates:
        return []
    return await _rank_watch_worthy(candidates)


def _looks_systemy(name: str) -> bool:
    n = name.lower()
    return (n.startswith("aspnet") or n.startswith("__") or "migration" in n
            or n.endswith("logs") or n.endswith("log") or n.endswith("state")
            or n.endswith("types") or n.endswith("statuses") or "queue" in n
            or "audit" in n or n.endswith("settings") or n.endswith("config"))


CLASSIFY_PROMPT = """You are given DB table names from a port/fleet operations system. \
Return the ones representing real-world BUSINESS ENTITIES or EVENTS worth watching for \
newly-created rows — e.g. machines, certificates, production/loading batches, fuel events, \
work orders, inspections, deliveries. EXCLUDE internal/plumbing: auth (AspNet*), \
migrations, audit/query/system logs, ingestion-state, lookup/reference/type/status tables, \
app settings. Return ONLY JSON: {"watch": ["ExactTableName", ...]} — business tables only, \
most operationally important first."""


async def _rank_watch_worthy(candidates: list[dict]) -> list[dict]:
    """Ask a cheap model which candidate tables are business entities/events. Falls
    back to a name heuristic if the model is unavailable. Recommended first."""
    names = [_unquote(c["table"]) for c in candidates]
    watch: list[str] = []
    try:
        out = await _cheap_model().ainvoke(
            [("system", CLASSIFY_PROMPT), ("user", json.dumps({"tables": names}))])
        from .. import llmclient
        if llmclient.is_refusal(out):  # Phase 12 (CTX-09e): fall back to the heuristic
            raise RuntimeError("model declined (refusal)")
        content = out.content
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        start, end = content.find("{"), content.rfind("}")
        parsed = json.loads(content[start:end + 1]) if start != -1 else {}
        watch = [w for w in parsed.get("watch", []) if isinstance(w, str)]
    except Exception:
        log.warning("propose_sources: classifier unavailable, using heuristic", exc_info=True)
        watch = [n for n in names if not _looks_systemy(n)]

    by_name = {_unquote(c["table"]): c for c in candidates}
    ranked, taken = [], set()
    for n in watch:                                  # recommended, in model order
        if n in by_name and n not in taken:
            ranked.append({**by_name[n], "recommended": True})
            taken.add(n)
    for n, c in by_name.items():                     # the rest, flagged as maybe-system
        if n not in taken:
            ranked.append({**c, "recommended": False})
    return ranked


def _cheap_model():
    from ..config import get_settings
    from .. import llmclient
    s = get_settings()
    if s.anthropic_api_key:
        return llmclient.chat_anthropic(s.triage_model, max_tokens=1500, temperature=0.0,
                                        api_key=s.anthropic_api_key,
                                        role="triage", settings=s)  # Phase 13 (GOLIVE-01)
    return llmclient.chat_openai("gpt-4o-mini", max_tokens=1500, temperature=0.0,
                                 api_key=s.openai_api_key, role="triage")  # Quick 005


async def _detect_watermark(table: str) -> dict | None:
    """Pick a monotonic 'new-row' column for a table via information_schema:
    prefer a creation timestamp, else an increasing id. Read-only, through the
    shared gateway."""
    safe = table.replace("'", "''")
    try:
        cols = await run_readonly_sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{safe}' ORDER BY ordinal_position")
    except Exception:
        return None
    if not cols:
        return None
    typed = [(c["column_name"], (c.get("data_type") or "").lower()) for c in cols]

    # 1. creation timestamp
    for name, typ in typed:
        n = name.lower()
        if ("timestamp" in typ or typ == "date") and any(
                k in n for k in ("created", "inserted", "added", "timestamp")):
            return {"col": name, "reason": f"creation timestamp column “{name}”"}
    # 2. increasing integer id
    for name, typ in typed:
        n = name.lower()
        if ("int" in typ or "serial" in typ) and (n == "id" or n.endswith("id")):
            return {"col": name, "reason": f"increasing id column “{name}”"}
    return None


# ── helpers ──────────────────────────────────────────────────────────────

def _unquote(ident: str) -> str:
    return ident.strip().strip('"')


def _sql_literal(value) -> str:
    """Render the watermark as an untyped, quote-escaped SQL literal. Values
    originate from our own prior SELECT on this column (numbers/timestamps), not
    user input; the guardrail additionally enforces SELECT-only. Postgres coerces
    the untyped literal to the column's actual type during comparison."""
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _row_time(value) -> datetime:
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _jsonable(row: dict) -> dict:
    return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
            for k, v in row.items()}


def _row_fingerprint(row: dict) -> str:
    """Short stable hash of a row, so rows sharing a watermark value still get a
    unique external_id (dedupe is exact-match, not lossy)."""
    import hashlib
    payload = json.dumps(_jsonable(row), sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]
