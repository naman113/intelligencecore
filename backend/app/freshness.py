"""Phase 17 (CONS-02, CONS-03): read-only freshness facts for a SQL statement.

Reads agent_memory only; never opens a tenant pool.

Given a SQL statement, ``for_sql`` names the warehouse relations it reads, whether
each is imported or live, when it last synced, the ``as_of`` of the data and
whether any imported input is stale. Every consumer (sentinel, dashboard refresh,
mode flips, the live budget) calls this one module so freshness has one definition.

Names are matched case-insensitively (``fold``) and reported in the canonical
``source_tables`` spelling, so a mirrored mixed-case table such as ``public."Order"``
resolves as imported. ``for_sql`` never raises: a failure comes back as
``failed=True`` (not stale, every read live) and the caller decides how to fail safe.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import expressions as exp

from . import guardrails, kpihistory, sourcesvc, warehouse
from .config import get_settings
from .db import get_pool

log = logging.getLogger("freshness")

CLOCK_FUNCS = frozenset({
    "now", "current_timestamp", "localtimestamp", "clock_timestamp",
    "statement_timestamp", "transaction_timestamp", "current_time", "localtime"})
MAX_MODEL_DEPTH = 5


def fold(schema: str, table: str) -> tuple[str, str]:
    """Case-folded lookup key; every index and search-path comparison uses it."""
    return ((schema or "").casefold(), (table or "").casefold())


# -- pure SQL helpers --------------------------------------------------------

def relations_of(sql: str) -> set[tuple[str, str]]:
    """(schema or '', table) lowercase pairs a SELECT reads, CTE names and table
    functions dropped. Raises guardrails.SQLGuardrailError on non-read-only SQL."""
    root = sqlglot.parse_one(guardrails.validate_readonly_sql(sql), dialect="postgres")
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    out = set()
    for t in root.find_all(exp.Table):
        name = (t.name or "").strip()
        if not name or isinstance(t.this, exp.Func):
            continue
        if not t.db and name.lower() in ctes:
            continue
        out.add(((t.db or "").lower(), name.lower()))
    return out


def clock_kind(sql: str) -> str:
    """'clock' when a clock function appears, 'date' for CURRENT_DATE only, else 'none'."""
    try:
        root = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        low = (sql or "").lower()
        if any(re.search(rf"\b{f}\b", low) for f in CLOCK_FUNCS):
            return "clock"
        return "date" if re.search(r"\bcurrent_date\b", low) else "none"
    date = False
    for n in root.walk():
        if isinstance(n, (exp.CurrentTimestamp, exp.CurrentTime)):
            return "clock"
        if isinstance(n, exp.CurrentDate):
            date = True
        elif isinstance(n, exp.Anonymous) and (n.name or "").lower() in CLOCK_FUNCS:
            return "clock"
        elif isinstance(n, exp.Func) and n.sql_name().lower() in CLOCK_FUNCS:
            return "clock"
        elif isinstance(n, exp.Column) and not n.table and (n.name or "").lower() in CLOCK_FUNCS:
            return "clock"
        elif isinstance(n, exp.Column) and not n.table and (n.name or "").lower() == "current_date":
            date = True
    return "date" if date else "none"


def stale_after(schedule: dict, last_success: datetime, tz, default_interval: int) -> datetime | None:
    """When an import table counts as stale: the next due sync after last_success plus
    one more interval. None for an empty schedule (CSV, never scheduled)."""
    if not schedule:
        return None
    interval = int(schedule.get("interval_minutes") or default_interval)
    sched = {**schedule, "interval_minutes": interval}
    return sourcesvc.next_due(sched, last_success.astimezone(tz)) + timedelta(minutes=interval)


# -- facts -------------------------------------------------------------------

@dataclass
class FactCache:
    tables: dict = field(default_factory=dict)      # fold(schema, table) -> row dict
    by_table: dict = field(default_factory=dict)    # folded table -> [canonical schemas]
    models: dict = field(default_factory=dict)      # folded "model.<name>" -> row dict
    search_path: list = field(default_factory=lambda: ["public"])
    tz: object = timezone.utc
    loaded_at: datetime | None = None
    collisions: dict = field(default_factory=dict)  # key -> every row sharing a folded key

    def add_table(self, row: dict) -> None:
        key = fold(row["warehouse_schema"], row["warehouse_table"])
        self.collisions.setdefault(key, []).append(row)
        cur = self.tables.get(key)
        # An unquoted identifier is lowercase in Postgres: that spelling wins a collision.
        if cur is None or (row["warehouse_table"] == row["warehouse_table"].lower()
                           and cur["warehouse_table"] != cur["warehouse_table"].lower()):
            self.tables[key] = row
        schemas = self.by_table.setdefault(key[1], [])
        if row["warehouse_schema"] not in schemas:
            schemas.append(row["warehouse_schema"])

    def add_model(self, row: dict) -> None:
        rel = row.get("relation") or f"model.{row.get('name', '')}"
        self.models[rel.casefold()] = row

    def lookup(self, schema: str, table: str) -> dict | None:
        return self.tables.get(fold(schema, table))


def _j(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


async def load_facts() -> FactCache:
    """Two agent_memory SELECTs (source_tables joined to sources, model_tables)."""
    from . import whlink
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT st.warehouse_schema, st.warehouse_table, st.mode, st.pending_mode, st.status, "
        "st.backfill_done, st.last_success_at, s.name AS source_name, s.kind AS source_kind, s.schedule "
        "FROM source_tables st JOIN sources s ON s.id = st.source_id "
        "WHERE s.status <> 'removed' AND st.status <> 'missing'")
    models = await pool.fetch(
        "SELECT name, relation, object_kind, derived_sql, updated_at FROM model_tables "
        "WHERE status = 'active'")
    try:
        tz = ZoneInfo(kpihistory._tz())
    except Exception:
        tz = timezone.utc
    raw_path = warehouse.reader_server_settings().get("search_path", "public")
    path = [p.strip() for p in whlink._split_search_path(raw_path) if p.strip()] or ["public"]
    c = FactCache(search_path=path, tz=tz, loaded_at=datetime.now(timezone.utc))
    for r in rows:
        d = dict(r)
        d["schedule"] = _j(d.get("schedule"))
        c.add_table(d)
    for m in models:
        c.add_model(dict(m))
    return c


# -- result ------------------------------------------------------------------

@dataclass
class Freshness:
    as_of: datetime
    stale: bool
    reads: tuple = ()
    import_reads: tuple = ()
    live_reads: tuple = ()
    stale_tables: tuple = ()
    inputs_synced_at: datetime | None = None
    failed: bool = False
    last_sync: dict = field(default_factory=dict, repr=False)

    @property
    def as_of_iso(self) -> str:
        return self.as_of.isoformat()

    def summary(self) -> str:
        if not self.stale_tables:
            return f"Data as of {self.as_of_iso}"[:300]
        parts = []
        for t in self.stale_tables:
            ls = self.last_sync.get(t)
            parts.append(f"{t} (last sync {ls.isoformat(timespec='minutes') if ls else 'never'})")
        return ("Stale data: " + "; ".join(parts))[:300]


def _name(schema: str, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _live_result(relations, now: datetime, failed: bool) -> Freshness:
    names = tuple(sorted(_name(s, t) for s, t in relations))
    return Freshness(as_of=now, stale=False, reads=names, import_reads=(), live_reads=names, failed=failed)


class _Acc:
    def __init__(self):
        self.imports: dict[str, datetime | None] = {}
        self.live: set[str] = set()
        self.stale: set[str] = set()
        self.extra_asof: list[datetime] = []
        self.rows: dict[str, dict] = {}                  # Quick 027: import name -> its fact row
        self.mv_last: dict[str, datetime] = {}           # Quick 027: stale matview -> last refresh


def _merge(acc: _Acc, sub: _Acc) -> None:
    acc.imports.update(sub.imports)
    acc.live |= sub.live
    acc.stale |= sub.stale
    acc.extra_asof += sub.extra_asof
    acc.rows.update(sub.rows)
    acc.mv_last.update(sub.mv_last)


def _matview_stale(refreshed: datetime, sub: _Acc, facts: FactCache, now: datetime) -> bool:
    """Quick 027: a matview is stale when an imported input synced after its last
    refresh and the refresh is overdue by the same threshold an import table gets
    (``stale_after`` from that refresh time, using the input's schedule). A matview
    over live or CSV inputs, or whose inputs have not synced since, is never stale
    by this rule (a stale input already marks itself)."""
    for name, last in sub.imports.items():
        row = sub.rows.get(name)
        if last is None or row is None or row.get("source_kind") == "csv" or last <= refreshed:
            continue
        sa = stale_after(row.get("schedule") or {}, refreshed, facts.tz,
                         get_settings().sync_default_interval_minutes)
        if sa is not None and now > sa:
            return True
    return False


def _walk(rel, facts: FactCache, now: datetime, acc: _Acc, depth: int, seen: frozenset) -> None:
    sch, tbl = rel
    if sch.casefold() == "model":
        mk = f"model.{tbl}".casefold()
        m = facts.models.get(mk)
        if m is not None and m.get("object_kind") in ("view", "matview") and m.get("derived_sql"):
            if mk in seen or depth >= MAX_MODEL_DEPTH:
                return
            from . import modelcompiler
            is_mv = m["object_kind"] == "matview" and m.get("updated_at") is not None
            if is_mv:
                acc.extra_asof.append(m["updated_at"])
            sub = _Acc() if is_mv else acc
            for ref in modelcompiler.derived_select_refs(m["derived_sql"]):
                s, _, t = ref.partition(".")
                _walk((s, t), facts, now, sub, depth + 1, seen | {mk})
            if is_mv:
                _merge(acc, sub)
                if _matview_stale(m["updated_at"], sub, facts, now):
                    acc.stale.add(f"model.{tbl}")
                    acc.mv_last[f"model.{tbl}"] = m["updated_at"]
            return
    row = None
    if sch:
        row = facts.lookup(sch, tbl)
    else:
        for s in facts.search_path:
            row = facts.lookup(s, tbl)
            if row is not None:
                break
    if row is None:
        acc.live.add(_name(sch, tbl))
        return
    name = _name(row["warehouse_schema"], row["warehouse_table"])
    if row.get("mode") != "import":       # live, including pending_mode 'import' mid-backfill
        acc.live.add(name)
        return
    last = row.get("last_success_at")
    acc.imports[name] = last
    acc.rows[name] = row
    if row.get("source_kind") == "csv":
        return
    if not row.get("backfill_done") or last is None:
        acc.stale.add(name)
        return
    sa = stale_after(row.get("schedule") or {}, last, facts.tz,
                     get_settings().sync_default_interval_minutes)
    if sa is not None and now > sa:
        acc.stale.add(name)


def resolve(relations, facts: FactCache, now: datetime, *, warehouse: bool) -> Freshness:
    """Pure: facts plus relations in, Freshness out."""
    if not warehouse:
        return _live_result(relations, now, False)
    acc = _Acc()
    for rel in sorted(relations):
        _walk(rel, facts, now, acc, 0, frozenset())
    synced = [v for v in acc.imports.values() if v is not None]
    as_of = min(synced + acc.extra_asof + [now])
    return Freshness(
        as_of=as_of, stale=bool(acc.stale),
        reads=tuple(sorted(set(acc.imports) | acc.live)),
        import_reads=tuple(sorted(acc.imports)), live_reads=tuple(sorted(acc.live)),
        stale_tables=tuple(sorted(acc.stale)),
        inputs_synced_at=max(synced) if synced else None,
        last_sync={n: (acc.imports[n] if n in acc.imports else acc.mv_last.get(n)) for n in acc.stale})


async def for_sql(sql: str, *, cache: FactCache | None = None, now: datetime | None = None) -> Freshness:
    """Freshness of a SQL statement. Never raises; failure returns failed=True."""
    now = now or datetime.now(timezone.utc)
    try:
        relations = relations_of(sql)
    except Exception as e:
        log.debug("freshness: SQL not parsed (%s)", type(e).__name__)
        return _live_result(set(), now, True)
    try:
        if not warehouse.warehouse_mode():
            return _live_result(relations, now, False)
        facts = cache if cache is not None else await load_facts()
        return resolve(relations, facts, now, warehouse=True)
    except Exception as e:
        log.debug("freshness: lookup failed (%s)", type(e).__name__)
        return _live_result(relations, now, True)
