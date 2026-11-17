"""Semantic-model agent tools (Phase 16, MODEL-02 / MODEL-03 / MODEL-05).

get_model reads, propose_model_change and propose_relationships_from_keys write ONLY
``actions`` rows (kind model_change, status proposed): the owner approves on Approvals and
``semanticmodel.apply`` is the single writer of model rows and DDL. query_measure answers a
number from deterministically compiled SQL, or says plainly 'NOT MODELED:'.

Every SQL statement here runs through ``guardrails.run_readonly_sql`` (AST check, read-only
session, timeout, row cap). Identifiers are quoted by the evidence helpers; values are
literals that this module computed itself.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

from .agents.graph import _log_tool, ctx_phone, ctx_run_id  # noqa: F401
from .config import get_settings
from .db import get_pool
from .guardrails import SQLGuardrailError, run_readonly_sql
from . import freshness, kpihistory, modelcompiler, schemadiscovery, semanticmodel, warehouse

log = logging.getLogger("modeltools")

MAX_ITEMS = 12
MAX_SEED = 25
RESULT_CAP = 20000
MAX_CANDIDATES = 60

_OPS = ("create", "update", "retire", "refresh")
_WH_OBJECTS = ("view", "materialized_view", "index")
_DROP_ITEM_KEYS = ("object", "op", "definition")

_INSERT_SQL = """INSERT INTO actions (kind, payload, tier, status, task_run_id)
VALUES ('model_change', $1::jsonb, 'draft', 'proposed', $2)
ON CONFLICT ((payload->>'object'), (payload->>'name'))
WHERE kind = 'model_change' AND status = 'proposed' DO NOTHING
RETURNING id"""


def _today() -> date:
    return datetime.now(ZoneInfo(kpihistory._tz())).date()


def _decode(v):
    return semanticmodel._decode(v)


def _split_dim(dim: str) -> tuple:
    rel, _, col = dim.rpartition(".")
    return rel, col


def _is_time_type(t: str) -> bool:
    t = (t or "").lower()
    return t.startswith("date") or t.startswith("timestamp")


class _Refused(Exception):
    """A proposal item was turned away. The message is fixed text written in this module."""


# ── get_model ───────────────────────────────────────────────────────────────
_HIDE = ("id", "action_id", "approved_at", "updated_at", "meta", "status")


def _clean_row(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in _HIDE}


# ── Quick 037: get_model answers a compact summary; one section on demand ──
# The old answer serialised the whole model (62,074 characters on one client model: 15 views with their
# SQL, 50 measures, relationships, candidates) and stayed in the conversation for the rest of
# the run. The summary keeps what picks the right measure: each measure's aggregation, table,
# filter, time column, unit and description on one line; each table's grain, key, time column
# and freshness; relationships as lines; pending proposals by name. Full rows come back one
# section at a time. Both stay under model_result_chars, and a cut always says what to ask next.
SECTIONS = ("summary", "tables", "measures", "relationships", "indexes", "pending", "candidates")
_SECTION_ALIASES = {
    "summary": "summary", "tables": "tables", "table": "tables", "views": "tables", "view": "tables",
    "materialized_view": "tables", "measures": "measures", "measure": "measures", "metrics": "measures",
    "metric": "measures", "relationships": "relationships", "relationship": "relationships",
    "joins": "relationships", "indexes": "indexes", "index": "indexes", "pending": "pending",
    "proposals": "pending", "candidates": "candidates", "catalog": "candidates"}
_ONE = {"tables": "table", "measures": "measure", "relationships": "relationship", "indexes": "index",
        "pending": "pending proposal", "candidates": "candidate"}
_MATCH_KEYS = {"tables": ("name", "relation"), "measures": ("name",),
               "relationships": ("name", "from_relation", "to_relation"), "indexes": ("name", "relation"),
               "pending": ("name",), "candidates": ("relation",)}
_KIND = {"base": "table", "view": "view", "matview": "materialized view"}
_DESC_CHARS = 140
_EXPR_CHARS = 160
_VALUES_SHOWN = 8
_PENDING_SHOWN = 30
_DRILL = ('get_model(section="measures", name="<measure>") for one definition in full, '
          'get_model(section="tables", name="<table>") for its keys, columns and view SQL, or '
          'section="relationships" | "indexes" | "pending" | "candidates"')


def _result_cap() -> int:
    try:
        cap = int(get_settings().model_result_chars)
    except Exception:
        cap = RESULT_CAP
    return max(2000, cap)


def _hard_cap(text: str, cap: int) -> str:
    """Last resort; the renderers below fit themselves first."""
    if len(text) <= cap:
        return text
    tail = " ...[cut at the model_result_chars limit]"
    return text[:cap - len(tail)] + tail


def _dump(d) -> str:
    return json.dumps(d, default=str, separators=(",", ":"))


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {} and v != ""}


def _one_line(text, n: int, mark: str = "...") -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:max(0, n - len(mark))].rstrip() + mark


def _approx(n) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{round(n / 1_000)}k" if n >= 1_000 else str(n)


def _short_ts(v) -> str:
    try:
        d = datetime.fromisoformat(str(v))
        if d.tzinfo is None:
            return d.strftime("%Y-%m-%d %H:%M")
        return d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(v)[:19]


def _col(dim, base) -> str:
    """'schema.table.column' as just the column when it sits on the base relation."""
    if not dim:
        return ""
    rel, col = _split_dim(str(dim))
    return col if base and rel == base else str(dim)


def _filter_text(f, base) -> str:
    if not isinstance(f, dict):
        return _one_line(f, 80)
    op = f.get("op") or "?"
    s = f"{_col(f.get('dimension'), base)} {op}"
    if op in ("is_null", "is_not_null"):
        return s
    v = f.get("values", f.get("value"))
    if isinstance(v, list):
        shown = [str(x) for x in v[:_VALUES_SHOWN]]
        more = len(v) - len(shown)
        return f"{s} ({', '.join(shown)}{f', +{more} more' if more > 0 else ''})"
    return s if v is None else f"{s} {v}"


def _input_text(i) -> str:
    if not isinstance(i, dict):
        return str(i)
    s = str(i.get("name") or "?")
    if i.get("offset_window"):
        s += f" ({i['offset_window']} earlier)"
    if i.get("filter"):
        s += " (filtered)"
    return s


def _measure_line(m: dict, desc_chars: int) -> str:
    t, base = m.get("type"), m.get("base_relation")
    cut = "...(cut)"
    if t == "ratio":
        s = f"ratio {_input_text(m.get('numerator'))} / {_input_text(m.get('denominator'))}"
    elif t == "derived":
        s = (f"derived {_one_line(m.get('expr'), _EXPR_CHARS, cut)} from "
             + ", ".join(_input_text(i) for i in m.get("input_metrics") or []))
    else:
        s = f"{m.get('agg') or '?'}({_one_line(m.get('expr') or '*', _EXPR_CHARS, cut)}) on {base}"
        if m.get("filter"):
            s += " where " + " and ".join(_filter_text(f, base) for f in m["filter"])
        td = m.get("agg_time_dimension")
        s += f"; time {_col(td, base)}" if td else "; no time column"
    nad = m.get("non_additive_dimension")
    if isinstance(nad, dict) and nad.get("name"):
        s += f"; semi-additive ({nad.get('window_choice') or 'max'} {_col(nad['name'], base)})"
    if m.get("unit"):
        s += f"; unit {m['unit']}"
    desc = _one_line(m.get("description") or m.get("label"), desc_chars) if desc_chars else ""
    return f"- {m['name']}: {s}" + (f" -- {desc}" if desc else "")


def _table_line(t: dict) -> str:
    rel = t.get("relation")
    bits = [_KIND.get(t.get("object_kind"), "table")] + [b for b in (t.get("role"), t.get("grain")) if b]
    s = f"- {t['name']} = {rel} ({', '.join(bits)})"
    if t.get("primary_key"):
        s += f"; key {', '.join(t['primary_key'])}"
    if t.get("agg_time_dimension"):
        s += f"; time {_col(t['agg_time_dimension'], rel)}"
    if t.get("mode"):
        s += f"; {t['mode']}"
        if t.get("data_as_of"):
            s += f", data_as_of {_short_ts(t['data_as_of'])}"
        if t.get("stale"):
            s += ", STALE"
    if t.get("row_estimate") is not None:
        s += f"; ~{_approx(t['row_estimate'])} rows"
    if t.get("in_warehouse") is False:
        s += "; missing in the warehouse"
    return s


def _rel_text(r: dict) -> str:
    def side(rel, cols):
        cols = list(cols or [])
        return f"{rel}.{cols[0]}" if len(cols) == 1 else f"{rel}.({', '.join(cols)})"
    s = f"{side(r.get('from_relation'), r.get('from_columns'))} -> {side(r.get('to_relation'), r.get('to_columns'))}"
    card = r.get("cardinality") or "many_to_one"
    return s if card == "many_to_one" else f"{s} ({card.replace('_', '-')})"


async def _model_doc(fresh: bool = True, catalog: bool = True, seed: bool = True) -> dict:
    """Everything get_model can show. Raises only when the model rows themselves are unreadable."""
    rows = await semanticmodel.load_rows()
    wh = await semanticmodel.warehouse_objects()
    facts = None
    if fresh:
        try:
            facts = await freshness.load_facts()  # WR-04: agent_memory only, never the tenant DB
        except Exception:
            facts = None
    tables = []
    for t in rows["tables"]:
        t = _clean_row(t)
        if t.get("object_kind") in ("view", "matview"):
            t["in_warehouse"] = t["name"] in wh
        if facts is not None and t.get("relation"):
            try:
                f = await freshness.for_sql(f"SELECT 1 FROM {t['relation']}", cache=facts)
                if not f.failed:
                    t["mode"] = "live" if f.live_reads and not f.import_reads else (
                        "import" if f.import_reads and not f.live_reads else "mixed")
                    if f.import_reads:
                        t["data_as_of"] = f.as_of_iso
                        t["stale"] = f.stale
            except Exception:
                pass
        tables.append(t)
    indexes = [dict(_clean_row(i), in_warehouse=i["name"] in wh) for i in rows["indexes"]]
    pool = await get_pool()
    pend = await pool.fetch(
        "SELECT id, payload FROM actions WHERE kind = 'model_change' AND status = 'proposed' ORDER BY id")
    pending = []
    for p in pend:
        pl = _decode(p["payload"]) or {}
        pending.append({"object": pl.get("object"), "name": pl.get("name"),
                        "op": pl.get("op"), "action_id": p["id"]})
    doc = {"tables": tables,
           "relationships": [_clean_row(r) for r in rows["relationships"]],
           "measures": [_clean_row(m) for m in rows["measures"]],
           "indexes": indexes, "pending": pending,
           "candidates": [], "candidates_total": None, "unseeded_foreign_keys": None}
    if catalog:
        try:
            snap = await schemadiscovery.introspect()
            modeled = {t.get("relation") for t in rows["tables"]}
            # Quick 027: registered sources' src_* tables are modelling candidates too
            snaps = [snap, *await semanticmodel.source_snapshots()]
            sizes, cands = {}, []
            for sn in snaps:
                for t in sn.get("tables") or []:
                    rel = f"{t['schema']}.{t['table']}"
                    sizes.setdefault(rel, t.get("row_estimate"))
                    if rel not in modeled:
                        cands.append({"relation": rel, "row_estimate": t.get("row_estimate")})
            for t in tables:
                n = sizes.get(t.get("relation"))
                if isinstance(n, int) and n >= 0:
                    t["row_estimate"] = n
            doc["candidates"] = cands[:MAX_CANDIDATES]
            doc["candidates_total"] = len(cands)
            if seed:
                plan = await _seed_plan(rows, pend)
                doc["unseeded_foreign_keys"] = len(plan["items"])
        except Exception as e:
            doc["catalog_error"] = type(e).__name__
    return doc


# -- the summary ----------------------------------------------------------------
def _summary_head(doc: dict) -> list:
    t, r, m = len(doc["tables"]), len(doc["relationships"]), len(doc["measures"])
    head = [f"SEMANTIC MODEL SUMMARY: {t} tables, {r} relationships, {m} measures, "
            f"{len(doc['indexes'])} indexes, {len(doc['pending'])} pending proposals.",
            "Answer a governed number with query_measure and a measure name listed below. "
            f"This is a summary. Full detail on demand: {_DRILL}."]
    if not t and not m:
        head.append("No approved model yet: no tables or measures are approved.")
    return head


def _tail_lines(doc: dict) -> list:
    out = []
    if doc["indexes"]:
        out += ["", "INDEXES: " + "; ".join(
            f"{i['name']} on {i['relation']} ({', '.join(i.get('columns') or [])})" for i in doc["indexes"])]
    pend = doc["pending"]
    if pend:
        names = [f"{p['object']} {p['name']} ({p['op']})" for p in pend[:_PENDING_SHOWN]]
        more = len(pend) - len(names)
        out += ["", f"PENDING PROPOSALS ({len(pend)}, waiting for the owner on Approvals; do not propose "
                    "them again): " + "; ".join(names)
                + (f"; +{more} more (section=\"pending\")" if more > 0 else "")]
    if doc.get("catalog_error"):
        out += ["", f"CATALOG: catalog unavailable ({doc['catalog_error']}), so the tables not modeled yet "
                    "are not listed."]
    else:
        line = (f"CATALOG: {doc.get('candidates_total') or 0} catalog tables not modeled yet "
                "(get_model(section=\"candidates\") lists them)")
        if doc.get("unseeded_foreign_keys") is not None:
            line += (f"; {doc['unseeded_foreign_keys']} foreign keys not proposed yet as relationships "
                     "(propose_relationships_from_keys)")
        out += ["", line + "."]
    if pend:
        out[-1] += ' Pending proposals in full: get_model(section="pending").'
    return out


def _measure_block(doc: dict, desc_chars: int, compact: bool) -> list:
    meas = doc["measures"]
    if not compact:
        return ["", "MEASURES (name: aggregation on table where filter; time column; unit -- description):"] + \
               [_measure_line(m, desc_chars) for m in meas]
    groups: dict = {}
    for m in meas:
        key = m.get("base_relation") or ("ratio and derived" if m.get("type") in ("ratio", "derived") else "?")
        groups.setdefault(key, []).append(m["name"])
    return ["", "MEASURES (names only, by table):"] + [f"- on {k}: {', '.join(v)}" for k, v in groups.items()]


def _summary_text(doc: dict, desc_chars: int, compact: bool, note: str) -> str:
    lines = _summary_head(doc) + ([note] if note else [])
    lines += ["", "TABLES (name = relation (kind, role, grain); key; time column; mode, data_as_of; rows):"]
    lines += [_table_line(t) for t in doc["tables"]]
    lines += ["", "RELATIONSHIPS (many-to-one, from -> to):"] + [f"- {_rel_text(r)}" for r in doc["relationships"]]
    lines += _measure_block(doc, desc_chars, compact)
    lines += _tail_lines(doc)
    return "\n".join(lines)


def _squeezed_summary(doc: dict, cap: int, note: str) -> str:
    """Too big even with names only: measure names first, then the other lines, until the cap."""
    lines = _summary_head(doc) + [note]
    used = sum(len(x) + 1 for x in lines)
    budget = cap - used - 400
    names, shown = [m["name"] for m in doc["measures"]], []
    size = len("MEASURES (names only): ")
    for n in names:
        if size + len(n) + 2 > budget:
            break
        shown.append(n)
        size += len(n) + 2
    lines += ["", "MEASURES (names only): " + ", ".join(shown)]
    budget -= size
    missing = {"measures": len(names) - len(shown)}
    for what, block in (("tables", [_table_line(t) for t in doc["tables"]]),
                        ("relationships", [f"- {_rel_text(r)}" for r in doc["relationships"]])):
        title = "TABLES:" if what == "tables" else "RELATIONSHIPS:"
        kept = []
        for ln in block:
            if budget - len(ln) - 1 < len(title) + 2:
                break
            kept.append(ln)
            budget -= len(ln) + 1
        if kept:
            lines += ["", title] + kept
            budget -= len(title) + 2
        missing[what] = len(block) - len(kept)
    gone = [f"{n} {w} not listed" for w, n in missing.items() if n]
    if gone:
        lines += ["", f"CHARACTER LIMIT: {', '.join(gone)}. Ask get_model("
                      'section="measures", name="<part of a name>") to find a measure, or section="tables" '
                      'or "relationships" for those lists.']
    return _hard_cap("\n".join(lines), cap)


def _model_summary(doc: dict, cap: int) -> str:
    note = (f"NOTE: shortened to fit the {cap}-character limit; ask get_model(section=\"measures\", "
            "name=\"<measure>\") for a definition in full.")
    for i, (desc, compact) in enumerate(((_DESC_CHARS, False), (60, False), (0, False), (0, True))):
        text = _summary_text(doc, desc, compact, note if i else "")
        if len(text) <= cap:
            return text
    return _squeezed_summary(doc, cap, note)


# -- sections ---------------------------------------------------------------------
def _match(items: list, keys: tuple, name: str) -> tuple:
    n = name.strip().lower()
    exact = [i for i in items if any(str(i.get(k) or "").lower() == n for k in keys)]
    if exact:
        return exact, True
    return [i for i in items if any(n in str(i.get(k) or "").lower() for k in keys)], False


def _cut_note(cuts: list) -> str:
    return "; ".join(f"{k} cut at {keep} of {total} characters" for k, keep, total in cuts) + \
        " (the full text is on the Model page)"


def _fit_one(base: dict, item: dict, cap: int, more: int) -> str:
    """One item that does not fit: cut its longest texts, then drop its extras, with a note."""
    it, cuts = dict(item), []
    extra = f"; {more} more not shown, ask for them by name" if more else ""
    for _ in range(8):
        note = (_cut_note(cuts) if cuts else "too large for the limit") + extra
        out = _dump(dict(base, items=[it], truncated=note))
        if len(out) <= cap:
            return out
        texts = [k for k, v in it.items() if isinstance(v, str) and len(v) > 200]
        if not texts:
            break
        k = max(texts, key=lambda x: len(it[x]))
        total = len(item[k]) if isinstance(item.get(k), str) else len(it[k])
        keep = max(200, len(it[k]) - (len(out) - cap) - 120)
        it[k] = it[k][:keep]
        cuts = [c for c in cuts if c[0] != k] + [(k, keep, total)]
    for k in ("columns", "relationships", "measures"):
        if k in it:
            it.pop(k)
            cuts.append((k, 0, 0))
            note = _cut_note([c for c in cuts if c[2]]) + f"; {k} left out" + extra
            out = _dump(dict(base, items=[it], truncated=note))
            if len(out) <= cap:
                return out
    return _hard_cap(_dump(dict(base, items=[it], truncated="cut at the model_result_chars limit")), cap)


def _fit_items(section: str, items: list, cap: int, extra: dict | None = None,
               total: int | None = None) -> str:
    base = {"section": section, "count": len(items) if total is None else total, **(extra or {})}
    out = _dump(dict(base, items=items))
    if len(out) <= cap and (total is None or total == len(items)):
        return out
    note_room = 260
    size = len(_dump(dict(base, items=[]))) + note_room
    kept = []
    for it in items:
        s = len(_dump(it)) + 1
        if size + s > cap:
            break
        kept.append(it)
        size += s
    if not kept and items:
        return _fit_one(base, items[0], cap, len(items) - 1)
    shown_of = base["count"]
    note = (f'showing {len(kept)} of {shown_of} {section}; ask get_model(section="{section}", name="<name>") '
            'for one in full, or name="<part of a name>" for the matching ones')
    return _hard_cap(_dump(dict(base, items=kept, truncated=note)), cap)


def _not_found(section: str, name: str, items: list, cap: int) -> str:
    key = _MATCH_KEYS[section][0]
    names = ", ".join(str(i.get(key)) for i in items)
    text = f'No {_ONE[section]} matches "{name}". {section.capitalize()}: {names or "none"}.'
    return _hard_cap(text, cap)


async def _section(doc: dict, section: str, name: str | None, cap: int) -> str:
    items = list(doc[section])
    extra: dict = {}
    total = None
    if section == "candidates":
        if doc.get("catalog_error"):
            extra["catalog_error"] = doc["catalog_error"]
        if doc.get("unseeded_foreign_keys") is not None:
            extra["unseeded_foreign_keys"] = doc["unseeded_foreign_keys"]
        total = doc.get("candidates_total")
        if total is not None:
            extra["total"] = total
    exact = False
    if name:
        found, exact = _match(items, _MATCH_KEYS[section], name)
        if not found:
            return _not_found(section, name, items, cap)
        items, total = found, None
        extra.pop("total", None)
    if section in ("tables", "measures", "relationships", "indexes"):
        items = [_compact(i) for i in items]
    if section == "tables":
        if exact and len(items) == 1:
            t = items[0]
            rel = t.get("relation")
            try:
                cols = (await semanticmodel.catalog_columns()).get(rel)
                if cols:
                    t["columns"] = cols
            except Exception:
                pass
            t["measures"] = [m["name"] for m in doc["measures"] if m.get("base_relation") == rel]
            t["relationships"] = [_rel_text(r) for r in doc["relationships"]
                                  if rel in (r.get("from_relation"), r.get("to_relation"))]
        else:
            items = [{k: v for k, v in t.items() if k != "derived_sql"} for t in items]
            extra["note"] = ('view SQL, columns and the measures on a table: get_model(section="tables", '
                             'name="<table>")')
    return _fit_items(section, items, cap, extra, total)


async def _by_name(name: str, cap: int) -> str:
    doc = await _model_doc(catalog=False, seed=False)
    for sec in ("measures", "tables", "relationships", "indexes"):
        _, exact = _match(doc[sec], _MATCH_KEYS[sec], name)
        if exact:
            if sec == "tables":
                doc = await _model_doc(seed=False)
            return await _section(doc, sec, name, cap)
    n = name.strip().lower()
    ms = [m["name"] for m in doc["measures"] if n in m["name"].lower()]
    ts = [t["name"] for t in doc["tables"] if n in t["name"].lower() or n in str(t.get("relation")).lower()]
    if not ms and not ts:
        return _hard_cap(f'Nothing in the model is named like "{name}". Call get_model() for the summary.', cap)
    return _hard_cap(_dump({"section": "search", "name": name, "measures": ms, "tables": ts,
                            "hint": 'ask get_model(section="measures" or "tables", name="<exact name>") '
                                    'for one in full'}), cap)


@tool
async def get_model(section: str | None = None, name: str | None = None) -> str:
    """Read the approved semantic model. No arguments: a compact summary of every table (grain, key, time column, import or live mode, data_as_of), relationship, measure (aggregation, table, filter, time column, unit, description) and pending proposal, enough to pick a measure for query_measure. Call it before answering any number the owner relies on and before proposing model changes. Detail on demand: section = tables | measures | relationships | indexes | pending | candidates, and name = a table or measure for its full definition (a table adds its columns and view SQL), or part of a name to list the matches. Read-only."""
    start = time.monotonic()
    cap = _result_cap()
    args = {k: v for k, v in (("section", section), ("name", name)) if v}
    try:
        sec = _SECTION_ALIASES.get(str(section).strip().lower()) if section else "summary"
        if sec is None:
            result = (f'Unknown section "{section}". Use one of: {", ".join(SECTIONS)}; or call '
                      "get_model() with no arguments for the summary.")
        elif sec == "summary" and name and not section:
            result = await _by_name(str(name), cap)
        elif sec == "summary":
            result = _model_summary(await _model_doc(), cap)
        else:
            doc = await _model_doc(fresh=sec == "tables", catalog=sec in ("tables", "candidates"),
                                   seed=sec == "candidates")
            result = await _section(doc, sec, str(name) if name else None, cap)
        result = _hard_cap(result, cap)
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}"
    await _log_tool("get_model", args, result, start)
    return result
# ── End Quick 037 (get_model) ──


# ── proposal pipeline ───────────────────────────────────────────────────────
def _model_refs(obj: str, defn: dict) -> list:
    refs: list = []
    if obj == "table":
        refs.append(defn["relation"])
        if defn.get("agg_time_dimension"):
            refs.append(_split_dim(defn["agg_time_dimension"])[0])
    elif obj == "relationship":
        refs += [defn["from_relation"], defn["to_relation"]]
    elif obj == "measure":
        if defn.get("base_relation"):
            refs.append(defn["base_relation"])
        if defn.get("type") == "simple":  # Quick 054: schema.table.column in the expression
            refs += list(modelcompiler.expr_relations(defn.get("expr")))
        if defn.get("agg_time_dimension"):
            refs.append(_split_dim(defn["agg_time_dimension"])[0])
        for f in defn.get("filter") or []:
            refs.append(_split_dim(f["dimension"])[0])
        nad = defn.get("non_additive_dimension")
        if nad:
            refs.append(_split_dim(nad["name"])[0])
    elif obj in ("view", "materialized_view"):
        refs += list(defn.get("refs") or [])
    elif obj == "index":
        refs.append(defn["relation"])
    return refs


def _check_cols(model, relation: str, columns, what: str) -> None:
    cols = model.columns.get(relation)
    if cols is None:
        raise _Refused(f"{what} relation {relation} is not in the schema catalog")
    for c in columns:
        if c not in cols:
            raise _Refused(f"{what} column {relation}.{c} does not exist")


def _pct(rate) -> str:
    return "no key values" if rate is None else f"{rate * 100:.1f}%"


def _summary(obj: str, op: str, name: str, defn: dict, evidence: dict) -> str:
    if op == "retire":
        return f"Retire {obj.replace('_', ' ')} {name}"
    if op == "refresh":
        return f"Refresh materialized view model.{name}"
    if obj == "measure":
        return f"{'Update' if op == 'update' else 'Model'} measure {name}"
    if obj == "relationship":
        f = defn["from_relation"].split(".")[1]
        t = defn["to_relation"].split(".")[1]
        card = "" if (defn.get("cardinality") or "many_to_one") == "many_to_one" else             str(defn["cardinality"]).replace("_", "-") + ", "
        return (f"Relate {f}.{', '.join(defn['from_columns'])} to {t}.{', '.join(defn['to_columns'])} "
                f"({card}match {_pct(evidence.get('match_rate'))})")
    if obj == "table":
        bits = [b for b in (defn.get("role"), defn.get("grain")) if b]
        return f"Set table {name}" + (": " + ", ".join(bits) if bits else "")
    if obj == "view":
        return f"{'Update' if op == 'update' else 'Create'} view model.{name}"
    if obj == "materialized_view":
        return f"Create materialized view model.{name}"
    return f"Index {defn['relation']} ({', '.join(defn['columns'])})"


class _State:
    def __init__(self, rows, model, wh, today, pool):
        self.rows, self.model, self.wh, self.today, self.pool = rows, model, wh, today, pool


async def _build_state() -> _State:
    return _State(await semanticmodel.load_rows(), await semanticmodel.load_model(),
                  await semanticmodel.warehouse_objects(), _today(), await get_pool())


def _item_parts(item) -> tuple:
    if not isinstance(item, dict):
        raise _Refused("an item must be an object")
    obj, op = item.get("object"), item.get("op") or "create"
    if obj not in modelcompiler.OBJECTS:
        raise _Refused(f"object must be one of {', '.join(modelcompiler.OBJECTS)}")
    if op not in _OPS:
        raise _Refused(f"op must be one of {', '.join(_OPS)}")
    raw = item.get("definition")
    if raw is None:
        raw = {k: v for k, v in item.items() if k not in _DROP_ITEM_KEYS}
    if not isinstance(raw, dict):
        raise _Refused("definition must be an object")
    name = raw.get("name") or item.get("name")
    if not isinstance(name, str) or not modelcompiler.NAME_RE.match(name):
        raise _Refused("name must be lowercase letters, digits and underscores (max 63)")
    return obj, op, name, {**raw, "name": name}


async def _evidence(st: _State, obj: str, op: str, name: str, defn: dict) -> dict:
    model = st.model
    if obj == "measure":
        candidate = modelcompiler.measure_from_definition(defn)
        for i in modelcompiler._inputs_of(candidate):
            if i.name == name or model.measure(i.name) is None:
                raise _Refused(f"input measure {i.name} is not active; approve it first")
        try:
            return await semanticmodel.measure_evidence(model, candidate, st.today)
        except (modelcompiler.NotModeled, modelcompiler.InvalidRequest) as e:
            raise _Refused(str(e)) from None
    if obj == "relationship":
        _check_cols(model, defn["from_relation"], defn["from_columns"], "from")
        _check_cols(model, defn["to_relation"], defn["to_columns"], "to")
        ev = await semanticmodel.relationship_evidence(defn)
        if ev.get("duplicate_keys", 0) > 0:
            raise _Refused(f"{defn['to_relation']} ({', '.join(defn['to_columns'])}) is not unique, "
                           "so this is not many-to-one")
        if defn.get("cardinality") == "one_to_one" and ev.get("from_duplicate_keys", 0) > 0:
            raise _Refused(f"{defn['from_relation']} ({', '.join(defn['from_columns'])}) is not unique, "
                           "so this is not one-to-one")
        return ev
    if obj == "table":
        if defn["object_kind"] != "base":
            raise _Refused("a table item must be a base relation; views are separate items")
        rel = defn["relation"]
        _check_cols(model, rel, defn["primary_key"], "primary key")
        atd = defn.get("agg_time_dimension")
        if atd:
            trel, tcol = _split_dim(atd)
            tcols = model.columns.get(trel)
            if tcols is None or tcol not in tcols:
                raise _Refused("the time column does not exist")
            if not _is_time_type(tcols[tcol]):
                raise _Refused("the time column must be a date or timestamp")
            if trel != rel:
                try:
                    modelcompiler.plan_joins(model, rel, [trel])
                except modelcompiler.NotModeled:
                    raise _Refused(f"needs an approved relationship from {rel} to {trel}; "
                                   "approve relationships first") from None
        return await semanticmodel.table_evidence(rel, defn["primary_key"], atd, st.today)
    if obj in ("view", "materialized_view"):
        return await semanticmodel.derived_evidence(defn["sql"])
    return {}


def _check_state(st: _State, obj: str, op: str, name: str, defn: dict, cur) -> None:
    if obj in _WH_OBJECTS and not warehouse.warehouse_mode():
        raise _Refused("needs the warehouse")
    if op == "refresh" and obj != "materialized_view":
        raise _Refused("refresh applies only to materialized views")
    if obj in ("table", "view", "materialized_view"):
        kind = {"table": "base", "view": "view", "materialized_view": "matview"}[obj]
        for t in st.rows["tables"]:
            if t.get("name") == name and t.get("object_kind") != kind:
                raise _Refused("that name belongs to another model object; pick a different name")
    if op == "create":
        if cur is not None:
            recreate = obj in _WH_OBJECTS and name not in st.wh
            if not recreate:
                raise _Refused(f"{name} is already approved; use op update")
    elif cur is None:
        raise _Refused(f"no active {obj.replace('_', ' ')} named {name} to {op}")
    if op in ("create", "update"):
        active_models = {t["name"] for t in st.rows["tables"]
                         if t.get("object_kind") in ("view", "matview")}
        for ref in ([] if obj == "index" else _model_refs(obj, defn)):
            if ref.startswith("model.") and ref.split(".", 1)[1] not in active_models:
                raise _Refused(f"{ref} has no active model view; approve it first")
        if obj == "materialized_view" and op == "update":
            raise _Refused("a materialized view cannot be changed in place; retire it and create a new one")
        if obj == "index":
            mv = defn["relation"].split(".", 1)[1]
            if mv not in {t["name"] for t in st.rows["tables"] if t.get("object_kind") == "matview"}:
                raise _Refused("indexes need an active materialized view; approve it first")
    if op == "retire":
        if obj == "measure":
            for m in st.rows["measures"]:
                names = [(m.get(k) or {}).get("name") for k in ("numerator", "denominator")]
                names += [i.get("name") for i in (m.get("input_metrics") or [])]
                if name in names:
                    raise _Refused(f"measure {m['name']} uses {name} as an input; retire it first")
        if obj in ("view", "materialized_view"):
            rel = f"model.{name}"
            if any(rel in modelcompiler.measure_relations(m) for m in st.rows["measures"]):  # Quick 054
                raise _Refused("an active measure is built on this view; retire the measure first")


async def _propose_one(st: _State, item, default_origin: str = "agent") -> str:
    """Returns 'proposed #<id>', 'pending' or 'refused: <reason>'. Never raises."""
    try:
        obj, op, name, raw = _item_parts(item)
        defn: dict = {}
        if op in ("create", "update"):
            try:
                defn = modelcompiler.normalize_definition(obj, raw)
            except modelcompiler.InvalidRequest as e:
                raise _Refused(str(e)) from None
            if obj == "relationship":  # origin and match_rate belong to the payload / executor
                defn.pop("origin", None)
                defn.pop("match_rate", None)
        cur = await semanticmodel.current_definition(obj, name)
        _check_state(st, obj, op, name, defn, cur)
        evidence: dict = {}
        if op in ("create", "update"):
            try:
                evidence = await _evidence(st, obj, op, name, defn)
            except _Refused:
                raise
            except Exception as e:
                raise _Refused(f"evidence failed: {type(e).__name__}") from None
        payload = {
            "_summary": _summary(obj, op, name, defn, evidence),
            "object": obj, "name": name, "op": op,
            "definition": defn or None,
            "before": cur if op in ("update", "retire") else None,
            "origin": default_origin, "thread_id": ctx_phone.get(), "evidence": evidence,
        }
        new_id = await st.pool.fetchval(_INSERT_SQL, json.dumps(payload, default=str), ctx_run_id.get())
        return "pending" if new_id is None else f"proposed #{new_id}"
    except _Refused as e:
        return f"refused: {e}"
    except Exception as e:  # never leak a driver message
        return f"refused: {type(e).__name__}"


def _label(item) -> str:
    if isinstance(item, dict):
        raw = item.get("definition") if isinstance(item.get("definition"), dict) else item
        return f"{item.get('object', '?')} {raw.get('name') or item.get('name') or '?'}"
    return "item"


def _report(lines: list, notes: list) -> tuple:
    proposed = sum(1 for _, r in lines if r.startswith("proposed"))
    pending = sum(1 for _, r in lines if r == "pending")
    refused = sum(1 for _, r in lines if r.startswith("refused"))
    head = (f"{proposed} proposed, {pending} pending, {refused} refused. Nothing is applied "
            "until the owner approves on Approvals.")
    body = [head] + [f"{i + 1}. {lab}: {res}" for i, (lab, res) in enumerate(lines)] + notes
    return "\n".join(body)[:RESULT_CAP], bool(lines) and proposed == 0 and pending == 0


@tool
async def propose_model_change(items: list[dict]) -> str:
    """Propose semantic-model changes for the owner to approve (nothing is applied until they approve on Approvals). At most 12 items per call. Each item is {object, op, definition} where object is table | relationship | measure | view | materialized_view | index and op is create | update | retire | refresh (default create). table: {name, relation 'schema.table', role fact|dimension, grain, primary_key [cols], agg_time_dimension 'schema.table.column'}. relationship: {name, from_relation, from_columns, to_relation, to_columns} (many-to-one; the to side must be unique). measure: {name, type simple|ratio|derived, base_relation, agg sum|count|count_distinct|average|min|max, expr, agg_time_dimension, filter, unit, format, ...}; expr names base_relation columns, and a column of a table linked many-to-one from it as schema.table.column, counted once per base row (the cost of the product or roll on each line; no view needed). view / materialized_view: {name, sql} (a SELECT over approved relations). index: {name, relation 'model.<matview>', columns}. Every card carries evidence (compiled SQL, 30 days of samples, key match rate). Call get_model first and follow the data-modeling skill."""
    start = time.monotonic()
    args = {"items": items}
    if not isinstance(items, list) or not items:
        result, refused = "REFUSED: items must be a non-empty list", True
    elif len(items) > MAX_ITEMS:
        result, refused = f"REFUSED: at most {MAX_ITEMS} items per call", True
    else:
        try:
            st = await _build_state()
            lines = [(_label(it), await _propose_one(st, it)) for it in items]
            result, refused = _report(lines, [])
        except Exception as e:
            result, refused = f"TOOL ERROR: {type(e).__name__}", False
    await _log_tool("propose_model_change", args, result, start, refused=refused)
    return result


# ── foreign-key seeding ─────────────────────────────────────────────────────
def _seed_name(table: str, col: str, to_table: str) -> str:
    raw = f"{table}__{col}__to__{to_table}".lower()
    return re.sub(r"[^a-z0-9_]", "_", raw)[:63]


def _fk_targets(candidates: list, from_schema: str) -> list:
    """Quick 027: the catalog relation(s) a foreign key may point at. Same schema first
    (a source table's FK stays inside its own src_ schema); a business_db key then takes
    the unique non-source candidate, else the public one. More than one left = ambiguous."""
    same = [c for c in candidates if c.split(".", 1)[0] == from_schema]
    if same:
        return same[:1]
    if from_schema.startswith("src_"):
        return []
    own = [c for c in candidates if not c.startswith("src_")]
    if len(own) > 1:
        public = [c for c in own if c.startswith("public.")]
        if len(public) == 1:
            return public
    return own


async def _seed_plan(rows: dict, pending_rows) -> dict:
    """items to propose plus notes; relationships already approved or pending are left out."""
    fks = await semanticmodel.foreign_keys()
    catalog = [r for r in (await semanticmodel.catalog_columns()) if not r.startswith("model.")]
    by_table: dict = {}
    for rel in catalog:
        by_table.setdefault(rel.split(".", 1)[1], []).append(rel)

    def key(d):
        return (d["from_relation"], tuple(d["from_columns"]), d["to_relation"], tuple(d["to_columns"]))

    known = {key(r) for r in rows["relationships"]}
    pending = set()
    for p in pending_rows:
        pl = _decode(p["payload"]) or {}
        d = pl.get("definition") or {}
        if pl.get("object") == "relationship" and d.get("from_relation"):
            pending.add(key(d))
    groups: dict = {}
    for fk in fks:
        groups.setdefault((f"{fk['schema']}.{fk['table']}", fk["to_table"]), []).append(fk)
    items, notes = [], []
    for (frel, to_table), grp in sorted(groups.items()):
        if len(grp) > 1:
            notes.append(f"{frel} -> {to_table}: multiple foreign keys to the same table "
                         "(composite or role-playing), skipped; propose it by hand if wanted")
            continue
        targets = _fk_targets(by_table.get(to_table, []), frel.split(".")[0])
        if len(targets) != 1:
            notes.append(f"{frel} -> {to_table}: " + (
                "the target table name is ambiguous across schemas, refused"
                if targets else "the target table is not in the catalog, skipped"))
            continue
        fk = grp[0]
        d = {"name": _seed_name(frel.split(".")[1], fk["column"], to_table),
             "from_relation": frel, "from_columns": [fk["column"]],
             "to_relation": targets[0], "to_columns": [fk["to_column"]]}
        k = key(d)
        if k in known or k in pending:
            continue
        items.append({"object": "relationship", "op": "create", "definition": d})
    return {"items": items, "notes": notes, "approved": known, "pending": pending}


@tool
async def propose_relationships_from_keys() -> str:
    """Seed relationship proposals from the live catalog's foreign keys (works before any business context is approved). Each card shows the key match rate and whether the to side is unique. Skips relationships already approved or pending and tables with several foreign keys to the same target. At most 25 per call; call again for more. Nothing is applied until the owner approves on Approvals."""
    start = time.monotonic()
    try:
        st = await _build_state()
        pend = await st.pool.fetch(
            "SELECT id, payload FROM actions WHERE kind = 'model_change' AND status = 'proposed'")
        plan = await _seed_plan(st.rows, pend)
        batch, rest = plan["items"][:MAX_SEED], plan["items"][MAX_SEED:]
        lines = [(_label(it), await _propose_one(st, it, "foreign_key")) for it in batch]
        notes = list(plan["notes"])
        if rest:
            notes.append(f"{len(rest)} more foreign keys remain: call again.")
        if not lines and not notes:
            notes.append("No new foreign keys to propose (all are approved or pending).")
        result, refused = _report(lines, notes)
    except Exception as e:
        result, refused = f"TOOL ERROR: {type(e).__name__}", False
    await _log_tool("propose_relationships_from_keys", {}, result, start, refused=refused)
    return result


# ── query_measure ───────────────────────────────────────────────────────────
def _parse_iso(v, what: str) -> date | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise modelcompiler.InvalidRequest(f"range {what} must be an ISO date")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise modelcompiler.InvalidRequest(f"range {what} must be an ISO date (YYYY-MM-DD)") from None


def resolve_range(range, today: date) -> tuple:  # noqa: A002 - contract name
    """(start, end) with end exclusive; (None, None) for no range."""
    if range is None:
        return None, None
    if isinstance(range, dict):
        return _parse_iso(range.get("start"), "start"), _parse_iso(range.get("end"), "end")
    if not isinstance(range, str):
        raise modelcompiler.InvalidRequest("range must be a token or {start, end}")
    tomorrow = today + timedelta(days=1)
    q0 = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    if range == "last_7_days":
        return today - timedelta(days=7), today
    if range == "last_30_days":
        return today - timedelta(days=30), today
    if range == "this_month":
        return today.replace(day=1), tomorrow
    if range == "last_month":
        first = today.replace(day=1)
        return (first - timedelta(days=1)).replace(day=1), first
    if range == "this_quarter":
        return q0, tomorrow
    if range == "last_quarter":
        prev = q0 - timedelta(days=1)
        return date(prev.year, 3 * ((prev.month - 1) // 3) + 1, 1), q0
    if range in ("this_year", "year_to_date"):
        return date(today.year, 1, 1), tomorrow
    if range == "last_year":
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    raise modelcompiler.InvalidRequest(
        "range must be one of last_7_days, last_30_days, this_month, last_month, this_quarter, "
        "last_quarter, this_year, year_to_date, last_year or {start, end}")


def _expand_dim(model, dim):
    """'table.column' -> 'schema.table.column' when exactly one catalog relation has that table name."""
    if not isinstance(dim, str) or dim.count(".") != 1:
        return dim
    table, col = dim.split(".")
    hits = [r for r, cols in model.columns.items() if r.split(".", 1)[1] == table and col in cols]
    if len(hits) > 1:
        raise modelcompiler.InvalidRequest(
            f"ambiguous dimension {dim}: use one of " + ", ".join(f"{h}.{col}" for h in sorted(hits)))
    return f"{hits[0]}.{col}" if hits else dim


def _result_json(doc: dict) -> str:
    out = json.dumps(doc, default=str)
    if len(out) <= RESULT_CAP:
        return out
    rows = list(doc["rows"])
    doc["truncated"] = True
    while rows and len(out) > RESULT_CAP:
        rows = rows[: max(0, int(len(rows) * 0.8) - 1)]
        doc["rows"], doc["rows_shown"] = rows, len(rows)
        out = json.dumps(doc, default=str)
    return out


# ── Phase 17 (CONS-01): shared measure requests ──
WATCH_RANGES = ("last_7_days", "last_30_days", "this_month", "last_month", "this_quarter",
                "last_quarter", "this_year", "year_to_date", "last_year")
_WATCH_KEYS = ("measure", "filters", "range")


def build_measure_request(model, measure, by=None, grain=None, filters=None, range=None, today=None):  # noqa: A002
    """(request, start, end): the one builder behind query_measure and measure watches."""
    dims = [_expand_dim(model, d) for d in (by or [])]
    fl = None
    if filters:
        if not isinstance(filters, list):
            raise modelcompiler.InvalidRequest("filters must be a list")
        fl = [({**f, "dimension": _expand_dim(model, f.get("dimension"))} if isinstance(f, dict) else f)
              for f in filters]
    s, e = resolve_range(range, today or _today())
    request = modelcompiler.make_request(measure, by=dims, grain=grain, filters=fl, start=s, end=e)
    return request, s, e


def validate_watch_ref(ref) -> tuple:
    """A watch holds one scalar: measure, optional filters, optional relative range."""
    if not isinstance(ref, dict):
        return None, "REFUSED: measure_ref must be an object with a measure name"
    if "by" in ref or "grain" in ref:
        return None, "REFUSED: a KPI watch records one number; by and grain are not allowed"
    extra = [k for k in ref if k not in _WATCH_KEYS]
    if extra:
        return None, "REFUSED: a KPI watch measure takes only measure, filters and range"
    name = ref.get("measure")
    if not isinstance(name, str) or not modelcompiler.NAME_RE.match(name):
        return None, "REFUSED: measure must be a modeled measure name"
    rng = ref.get("range")
    if rng is not None and (not isinstance(rng, str) or rng not in WATCH_RANGES):
        return None, ("REFUSED: a KPI watch range must be a relative token (" + ", ".join(WATCH_RANGES)
                      + "); fixed dates are not allowed because a watch moves with time")
    fl = ref.get("filters")
    if fl is None:
        fl = []
    if not isinstance(fl, list) or any(not isinstance(f, dict) for f in fl):
        return None, "REFUSED: filters must be a list of {dimension, op, values}"
    if len(fl) > modelcompiler.MAX_FILTERS:
        return None, f"REFUSED: at most {modelcompiler.MAX_FILTERS} filters"
    return {"measure": name, "filters": [dict(f) for f in fl], "range": rng}, None


async def check_watch_measure(ref, model=None, today=None) -> tuple:
    """(clean_ref, compiled_sql, value_column, refusal). Pure apart from the model load."""
    clean, refusal = validate_watch_ref(ref)
    if refusal:
        return None, None, None, refusal
    try:
        if model is None:
            model = await semanticmodel.load_model()
        request, _s, _e = build_measure_request(
            model, clean["measure"], filters=clean["filters"] or None, range=clean["range"], today=today)
        compiled = modelcompiler.compile_measure(model, request)
    except modelcompiler.InvalidRequest as ex:
        return None, None, None, f"REFUSED: {ex}"
    except modelcompiler.NotModeled as ex:
        return None, None, None, f"NOT MODELED: {ex}"
    return clean, compiled.sql, compiled.columns[-1], None
# ── End Phase 17 (CONS-01) shared requests ──


@tool
async def query_measure(measure: str, by: list[str] | None = None, grain: str | None = None,
                        filters: list[dict] | None = None, range: dict | str | None = None) -> str:
    """Answer a number from the approved semantic model with deterministically compiled SQL. Use it for every number the owner relies on. measure: an approved measure name (see get_model). by: dimensions as schema.table.column (or table.column when the table name is unique). grain: day | week | month | quarter | year. filters: [{"dimension", "op", "values"}] with op = != in not_in > >= < <= is_null is_not_null. range: last_7_days | last_30_days | this_month | last_month | this_quarter | last_quarter | this_year | last_year | year_to_date, or {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} (end exclusive). Returns rows plus the SQL. A NOT MODELED answer means the model cannot answer it: propose a measure with propose_model_change or answer with run_sql and label the number unverified."""
    start = time.monotonic()
    args = {"measure": measure, "by": by, "grain": grain, "filters": filters, "range": range}
    refused = False
    try:
        model = await semanticmodel.load_model()
        request, s, e = build_measure_request(model, measure, by=by, grain=grain, filters=filters, range=range)
        compiled = modelcompiler.compile_measure(model, request)
        from . import livebudget  # Phase 17 (CONS-04)
        over, live = await livebudget.check(compiled.sql)  # Phase 17 (CONS-04)
        if over:
            await _log_tool("query_measure", args, over, start, refused=True)
            return over
        if live:
            args = {**args, "live": live}
        rows = await run_readonly_sql(compiled.sql)
        m = model.measure(request.measure)
        doc = {"measure": m.name, "label": m.label, "unit": m.unit, "format": m.format,
               "sql": compiled.sql, "columns": list(compiled.columns), "rows": rows,
               "row_count": len(rows), "truncated": len(rows) >= get_settings().sql_row_limit,
               "range": {"start": s.isoformat() if s else None, "end": e.isoformat() if e else None}
               if (s or e) else None}
        result = _result_json(doc)
    except modelcompiler.InvalidRequest as ex:
        refused, result = True, f"REFUSED: {ex}"
    except modelcompiler.NotModeled as ex:
        refused = True
        try:
            names = [m.name for m in (await semanticmodel.load_model()).measures]
        except Exception:
            names = []
        result = f"NOT MODELED: {ex}. Modeled measures: {', '.join(names) if names else '(none yet)'}"
    except SQLGuardrailError as ex:
        result = f"GUARDRAIL REJECTED: {ex}"
    except Exception as ex:
        result = f"SQL ERROR (read it, fix the query, retry): {type(ex).__name__}: {ex}"
    await _log_tool("query_measure", args, result, start, refused=refused)
    return result


# ── Phase 16 gap closure (16-11): raw SQL numbers are unverified ──
UNVERIFIED_NOTE = (
    "\n\nUNVERIFIED: raw SQL result, not a governed measure. Write the word "
    "unverified next to every number from it that you quote."
)
_ACTIVE_MEASURES_SQL = "SELECT count(*) FROM model_measures WHERE status = 'active'"


async def raw_sql_note() -> str:
    """UNVERIFIED_NOTE while at least one measure is active, else "". Read-only
    lookup on agent_memory (never tenant SQL); never raises."""
    try:
        from . import db as _db
        pool = await _db.get_pool()
        n = await pool.fetchval(_ACTIVE_MEASURES_SQL)
        if isinstance(n, int) and n > 0:
            return UNVERIFIED_NOTE
    except Exception as e:
        log.debug("raw_sql_note lookup failed: %s", e)
    return ""
