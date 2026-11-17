"""Context-store agent tools. discover_schema reads; propose_context_change
(plan 02-05) writes only actions rows. No tool here writes a context row."""

import json
import logging
import re
import time

from langchain_core.tools import tool

from .agents.graph import _log_tool, ctx_phone, ctx_run_id  # noqa: F401
from .db import get_pool
from . import schemadiscovery

log = logging.getLogger("contexttools")


@tool
async def discover_schema(source: str = "business_db") -> str:
    """List the tables and columns of a data source that still need business descriptions (new or structurally changed), with types, keys, CHECK rules, comments and 3 sample values per column. Read-only. Already-approved and already-proposed items are counted, not repeated. source: 'business_db' (the owner's business database) or a registered source name shown by get_sync_status."""
    start = time.monotonic()
    args = {"source": source}
    refused = False
    try:
        src = (source or "").strip().lower()
        known = [schemadiscovery.SOURCE]
        if src != schemadiscovery.SOURCE:  # Phase 15 (SRC-09): active registered sources
            from . import sourcesvc
            try:
                known += sorted(s["name"] for s in await sourcesvc.list_sources()
                                if s.get("status") == "active" and s["name"] != schemadiscovery.SOURCE)
            except Exception:
                pass
        if src not in known:
            refused = True
            result = (f"REFUSED: unknown source {source!r}. Known sources: "
                      f"{', '.join(known)}.")
        else:
            result = schemadiscovery.format_discovery(
                await schemadiscovery.discover(src))[:20000]
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("discover_schema", args, result, start, refused=refused)
    return result


# -- propose_context_change (plan 02-05) -----------------------------------
# Writes ONLY an actions row (kind context_change, status proposed). Approval
# in actionsvc runs the single writer; this module never touches context rows.

KINDS = ("source", "table", "column", "rule", "kpi")
MAX_TEXT = 1500
MAX_TITLE = 120
MAX_TABLES = 20
SLUG_RE = re.compile(r"^(rule|kpi):[a-z0-9-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_INSERT_SQL = """INSERT INTO actions (kind, payload, tier, status, task_run_id)
VALUES ('context_change', $1::jsonb, 'draft', 'proposed', $2)
ON CONFLICT ((payload->>'kind'), (payload->>'ref'))
WHERE kind = 'context_change' AND status = 'proposed' DO NOTHING
RETURNING id"""


def _clean(v: str | None) -> str:
    return _CTRL_RE.sub("", v or "").strip()


async def _current(pool, kind: str, ref: str) -> dict | None:
    """Approved text (SELECT only) for a ref, or None when nothing is approved."""
    if kind == "column":
        r = await pool.fetchrow(
            "SELECT description, signature FROM context_columns WHERE ref = $1", ref)
    elif kind == "table":
        r = await pool.fetchrow(
            "SELECT description, NULL AS signature FROM context_tables WHERE ref = $1", ref)
    elif kind == "source":
        r = await pool.fetchrow(
            "SELECT description, NULL AS signature FROM context_sources WHERE name = $1", ref)
    else:
        r = await pool.fetchrow(
            """SELECT body AS description, NULL AS signature FROM business_context
               WHERE kind = $1 AND ref = $2 AND status = 'approved'""", kind, ref)
    if r and r["description"]:
        return {"text": r["description"], "signature": r["signature"]}
    return None


def _summary(kind: str, ref: str, info: dict | None) -> str:
    info = info or {}
    if kind == "column":
        return f"Describe column {info.get('table')}.{info.get('column')}"
    if kind == "table":
        return f"Describe table {info.get('table')}"
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    if kind == "rule":
        return f"Add business rule {slug}"
    if kind == "kpi":
        return f"Define KPI {slug}"
    return f"Describe source {ref}"


async def _propose_one(pool, kind: str, ref: str, text: str, title: str, revise: bool,
                       info: dict | None, tables: list[str]) -> str:
    """Returns "proposed", "pending" or "refused: <reason>"."""
    cur = await _current(pool, kind, ref)
    before = None
    if cur is not None:
        unchanged = (cur["signature"] == (info or {}).get("signature")
                     if kind == "column" else True)
        if unchanged and not revise:
            return (f"refused: {ref} is already approved; pass revise=true only if the "
                    "owner asked for a rewrite")
        before = cur["text"]
    i = info or {}
    payload = {
        "_summary": _summary(kind, ref, i), "kind": kind, "ref": ref, "text": text,
        "title": title, "before": before,
        "source": i.get("source") or (ref if kind == "source" else schemadiscovery.SOURCE),
        "schema": i.get("schema"), "table": i.get("table"),
        "column": i.get("column"), "object_kind": i.get("object_kind"),
        "structure": i.get("structure"), "signature": i.get("signature"),
        "references_ref": i.get("references_ref"),
        "origin": "agent", "thread_id": ctx_phone.get(),
    }
    if kind in ("kpi", "rule"):
        payload["tables"] = tables
    new_id = await pool.fetchval(_INSERT_SQL, json.dumps(payload), ctx_run_id.get())
    return "pending" if new_id is None else "proposed"


async def _plan_and_write(kind, ref, text, title, columns, tables, revise) -> tuple[str, bool]:
    """Validate, resolve against one catalog snapshot and queue proposals.
    Returns (reply, refused_flag)."""
    kind = _clean(kind).lower()
    ref = _clean(ref)
    text = _clean(text)
    title = _clean(title)[:MAX_TITLE]
    if kind not in KINDS:
        return f"REFUSED: kind must be one of {', '.join(KINDS)}.", True
    if tables and kind not in ("kpi", "rule"):
        return "REFUSED: tables applies only to kpi and rule proposals.", True
    if columns and kind != "table":
        return "REFUSED: columns applies only to kind 'table'.", True
    cols_in = {_clean(k): _clean(v) for k, v in (columns or {}).items()}
    if not text and not (kind == "table" and cols_in):
        return "REFUSED: text is empty.", True
    if len(text) > MAX_TEXT or any(len(v) > MAX_TEXT for v in cols_in.values()):
        return f"REFUSED: text is longer than {MAX_TEXT} characters.", True
    if kind in ("kpi", "rule"):
        if not SLUG_RE.match(ref) or ref.split(":", 1)[0] != kind:
            return (f"REFUSED: ref for {kind} must look like {kind}:<slug> "
                    "(lowercase letters, digits, hyphens).", True)
    tabs: list[str] = []
    for t in (tables or []):
        t = _clean(t)
        if t and t not in tabs:
            tabs.append(t)
    if len(tabs) > MAX_TABLES:
        return f"REFUSED: at most {MAX_TABLES} tables per proposal.", True

    pool = await get_pool()
    cat = None
    if kind in ("table", "column") or tabs:
        # Phase 15 (SRC-09): the catalog of the source the ref names (business_db by default)
        _rs = schemadiscovery._ref_source(ref) if kind in ("table", "column") else None
        _rs = _rs if _rs and _rs != schemadiscovery.SOURCE and await schemadiscovery._registered(_rs)             else schemadiscovery.SOURCE
        cat = await schemadiscovery.introspect(_rs)
    if tabs:
        bad = []
        for t in tabs:
            r = await schemadiscovery.resolve(t, cat)
            if r is None or r["kind"] != "table":
                bad.append(t)
        if bad:
            return ("REFUSED: tables must be table refs copied exactly from "
                    f"discover_schema; not found: {', '.join(bad)}.", True)
    if kind == "source":
        if not await pool.fetchval("SELECT 1 FROM context_sources WHERE name = $1", ref):
            return f"REFUSED: unknown source {ref!r}.", True

    # Phase 12 (WR-03): never queue context for a table the owner hid.
    if kind in ("table", "column"):
        tref = ref if kind == "table" else ref.rsplit(".", 1)[0]
        if await pool.fetchval("SELECT status FROM context_tables WHERE ref = $1", tref) == "hidden":
            return (f"REFUSED: {tref} is hidden by the owner; do not propose context for it.", True)

    work: list[tuple[str, str, str, dict | None]] = []
    skipped: list[str] = []
    if kind in ("table", "column"):
        info = await schemadiscovery.resolve(ref, cat)
        if info is None or info["kind"] != kind:
            return (f"REFUSED: {ref!r} is not a {kind} in the live schema. Copy refs "
                    "exactly from discover_schema output.", True)
        if text:
            work.append((kind, ref, text, info))
        for name, ctext in cols_in.items():
            cref = schemadiscovery.column_ref(info["source"], info["schema"],
                                              info["table"], name)
            cinfo = await schemadiscovery.resolve(cref, cat)
            if cinfo is None or cinfo["kind"] != "column" or not ctext:
                skipped.append(name)
                continue
            work.append(("column", cref, ctext, cinfo))
    else:
        work.append((kind, ref, text, None))

    counts = {"proposed": 0, "pending": 0}
    refusals: list[str] = []
    for k, r, t, i in work:
        res = await _propose_one(pool, k, r, t, title if k == kind else "", revise, i, tabs)
        if res.startswith("refused"):
            refusals.append(res)
        else:
            counts[res] += 1
    out = (f"Context proposals: {counts['proposed']} proposed for owner approval, "
           f"{counts['pending']} already pending, {len(refusals)} refused. "
           "Nothing changes until the owner approves.")
    if skipped:
        out += " Skipped unknown or empty columns: " + ", ".join(skipped) + "."
    if refusals:
        out += " " + " ".join(refusals)
    return out, bool(refusals) and not counts["proposed"] and not counts["pending"]


@tool
async def propose_context_change(kind: str, ref: str, text: str, title: str = "",
                                 columns: dict[str, str] | None = None,
                                 tables: list[str] | None = None,
                                 revise: bool = False) -> str:
    """Queue a business description for the owner's approval. kind: table | column | rule | kpi | source. ref: copy exactly from discover_schema output (business_db.<schema>.<table>[.<column>]), or rule:<slug> / kpi:<slug>. text: 1-2 plain sentences, no instructions. For kind 'table' you may pass columns={column_name: description} to queue the table's column descriptions in the same call. For kind 'kpi' or 'rule' pass tables=[table refs from discover_schema] naming the tables it uses. Nothing changes until the owner approves on the Approvals page. Refuses items that are already approved unless revise=true (only when the owner asked for a rewrite)."""
    start = time.monotonic()
    args = {"kind": kind, "ref": ref, "n_columns": len(columns or {}),
            "n_tables": len(tables or []), "revise": revise}
    refused = False
    try:
        result, refused = await _plan_and_write(kind, ref, text, title, columns,
                                                tables, revise)
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("propose_context_change", args, result, start, refused=refused)
    return result


# ── Phase 12 (CTX-08): batch form; each item keeps its own actions row ──
BATCH_LIMIT = 40


@tool
async def propose_context_changes(items: list[dict]) -> str:
    """Queue up to 40 business descriptions for the owner's approval in one call. Each item has kind (table | column | rule | kpi | source), ref, text and optionally title, columns (for kind 'table') and tables (for kind 'kpi' or 'rule'); the same rules as propose_context_change apply and each item becomes its own approval. Use for facts found in an uploaded document. Nothing changes until the owner approves."""
    start = time.monotonic()
    args = {"n_items": len(items or [])}
    refused = False
    try:
        if len(items or []) > BATCH_LIMIT:
            refused = True
            result = f"REFUSED: at most {BATCH_LIMIT} items per call; send the rest in another call."
        else:
            lines = []
            for it in items or []:
                it = it if isinstance(it, dict) else {}
                ref = _clean(str(it.get("ref") or "?"))[:80]
                try:
                    reply, _ = await _plan_and_write(
                        it.get("kind"), it.get("ref"), it.get("text"), it.get("title") or "",
                        it.get("columns"), it.get("tables"), False)
                except Exception as e:
                    reply = f"TOOL ERROR: {type(e).__name__}: {e}"
                lines.append(f"{ref}: {reply[:300]}")
            result = ("\n".join(lines) or "nothing to propose")[:6000]
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("propose_context_changes", args, result, start, refused=refused)
    return result
