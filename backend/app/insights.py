"""Phase 7 exploration plumbing.

The one module that turns exploration tool calls into rows: notes via
cases_util.add_case_note, watches via the create_watch executor. The insights
route reads it too. Behaviour lives in the record_insight / propose_kpi tools
and the exploratory-data-analysis and kpi-selection skills; this module is
plumbing only. Tenant SQL goes through guardrails and nothing here writes the
business database. It must never import playbook_tools or agents (no cycle).
"""

import json
import asyncio
import logging
import re
import unicodedata

import asyncpg
import sqlglot
from sqlglot import expressions as exp

from . import appchannel, cases_util, guardrails
from .config import get_settings
from .db import get_pool

log = logging.getLogger("insights")

ORIGIN = "exploration"
INSIGHT_SEVERITIES = ("info", "warning", "critical")
INSIGHTS_CASE_TITLE = "Insights from data exploration"
LOCK_KEY = "ic:exploration-insights"
MAX_INSIGHTS_PER_RUN = 5
DEDUPE_DAYS = 30
SAMPLE_ROWS = 5
SAMPLE_VALUE_CAP = 200
NOTE_CAP = 6000

_RELATIVE_DATE_RE = re.compile(
    r"current_date|current_timestamp|now\s*\(\s*\)|localtimestamp", re.I)


# -- text helpers ----------------------------------------------------------

def norm_title(s) -> str:
    """Lowercase, collapse every run of non-alphanumerics (Unicode aware) to one
    space, strip. Mirrors SQL btrim(regexp_replace(lower(t), '[^[:alnum:]]+', ' ', 'g'))."""
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def has_hidden_chars(s: str) -> bool:
    """Same semantics as contextstore._has_hidden_chars (not imported: private)."""
    return any(unicodedata.category(c) in ("Cf", "Co", "Cs", "Cc") and c not in "\n\t"
               for c in s)


# -- evidence --------------------------------------------------------------

_STRING_RELATION_FUNCS = {"ts_stat", "ts_rewrite"}


def evidence_tables(sql: str) -> set[tuple[str, str]]:
    """(schema or '', table) pairs a SELECT reads, excluding CTE names and
    table functions. Raises guardrails.SQLGuardrailError on a non-read-only SQL."""
    root = sqlglot.parse_one(guardrails.validate_readonly_sql(sql), dialect="postgres")
    for f in root.find_all(exp.Anonymous, exp.Func):
        n = ((f.name if isinstance(f, exp.Anonymous) else f.sql_name()) or "").lower().lstrip("_")
        if n in _STRING_RELATION_FUNCS or "_to_xml" in n:
            raise guardrails.SQLGuardrailError(f"{n}() is not allowed in evidence SQL")
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    out = set()
    for t in root.find_all(exp.Table):
        name = (t.name or "").strip()
        if not name:                      # generate_series(...) etc.
            continue
        if not t.db and name.lower() in ctes:
            continue
        out.add(((t.db or "").lower(), name.lower()))
    return out


async def approved_tables() -> list[dict]:
    """Approved = active context_tables row WITH a description, in an active
    source. Phase 17 (CONS-04): every active context source, not only business_db."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT t.schema_name, t.table_name, t.ref, t.description
             FROM context_tables t JOIN context_sources s ON s.id = t.source_id
            WHERE s.status = 'active'
              AND t.status = 'active' AND t.description IS NOT NULL
            ORDER BY t.schema_name, t.table_name""")
    return [dict(r) for r in rows]


def unapproved_refs(tables, approved, search_path: list[str] | None = None) -> list[str]:
    """Unqualified names match an approved table in any schema; a qualified
    name must match schema and table. Phase 17 (CONS-04): with search_path given
    (warehouse mode) an unqualified name matches only approved tables whose schema
    is on that path, compared case-folded on both sides."""
    from . import freshness
    ok = {freshness.fold(a.get("schema_name"), a.get("table_name")) for a in approved}
    path = {(p or "").casefold() for p in search_path} if search_path is not None else None
    bad = []
    for sch, name in tables:
        sch_f, name_f = freshness.fold(sch, name)
        if sch_f:
            hit = (sch_f, name_f) in ok
        elif path is not None:
            hit = any(n == name_f and s in path for s, n in ok)
        else:
            hit = any(n == name_f for _s, n in ok)
        if not hit:
            bad.append(f"{sch}.{name}" if sch else name)
    return sorted(bad)


def _native(rows: list[dict]) -> list[dict]:
    out = json.loads(json.dumps(rows, default=str))
    for r in out:
        for k, v in r.items():
            if isinstance(v, str) and len(v) > SAMPLE_VALUE_CAP:
                r[k] = v[:SAMPLE_VALUE_CAP]
    return out


def _reader_search_path() -> list[str] | None:
    """Phase 17 (CONS-04): the reader search path in warehouse mode, else None."""
    try:
        from . import warehouse, whlink
        if not warehouse.warehouse_mode():
            return None
        raw = warehouse.reader_server_settings().get("search_path", "public")
        return [p.strip() for p in whlink._split_search_path(raw) if p.strip()] or ["public"]
    except Exception:
        return None


async def check_evidence(sql: str) -> tuple[dict | None, str]:
    """Prove an insight's evidence SQL. Returns (evidence, "") or (None, reason).
    Runs outside any working-memory transaction (the tenant query is slow)."""
    try:
        tables = evidence_tables(sql)
    except guardrails.SQLGuardrailError as e:
        return None, f"evidence_sql rejected by guardrails: {str(e)[:300]}"
    except Exception as e:  # sqlglot parse errors etc.
        return None, f"evidence_sql rejected by guardrails: {str(e)[:300]}"
    appr = await approved_tables()
    if not appr:
        return None, "no approved tables: nothing has been approved for study yet"
    sp = _reader_search_path()  # Phase 17 (CONS-04)
    bad = unapproved_refs(tables, appr, search_path=sp) if sp is not None else unapproved_refs(tables, appr)
    if bad:
        return None, f"evidence_sql reads tables that are not approved: {', '.join(bad)}"
    from . import livebudget  # Phase 17 (CONS-04)
    over, _live = await livebudget.check(sql)  # Phase 17 (CONS-04)
    if over:
        return None, over
    try:
        rows = await guardrails.run_readonly_sql(sql)
    except Exception as e:
        return None, f"evidence_sql failed: {type(e).__name__}: {str(e)[:300]}"
    if not rows:
        return None, "evidence_sql returned 0 rows"
    return ({
        "row_count": len(rows),
        "columns": list(rows[0].keys()),
        "sample": _native(rows[:SAMPLE_ROWS]),
        "truncated": len(rows) >= get_settings().sql_row_limit,
        "relative_dates": bool(_RELATIVE_DATE_RE.search(sql)),
    }, "")


async def check_measure_evidence(evidence_measure) -> tuple[dict | None, str, str, str]:
    """Phase 17 (CONS-04): prove a measure-backed finding. Returns
    (evidence, compiled_sql, measure_name, "") or (None, "", "", reason).
    Needs an absolute range so the finding reproduces."""
    from . import livebudget, modelcompiler, modeltools, semanticmodel
    em = evidence_measure
    if not isinstance(em, dict) or not isinstance(em.get("measure"), str):
        return None, "", "", "evidence_measure must be an object with a measure name"
    rng = em.get("range")
    if not (isinstance(rng, dict) and rng.get("start") and rng.get("end")):
        return None, "", "", ("evidence_measure needs an absolute {start, end} range "
                              "so the finding reproduces")
    try:
        model = await semanticmodel.load_model()
        request, _s, _e = modeltools.build_measure_request(
            model, em["measure"], by=em.get("by"), grain=em.get("grain"),
            filters=em.get("filters"), range=rng)
        sql = modelcompiler.compile_measure(model, request).sql
    except (modelcompiler.InvalidRequest, modelcompiler.NotModeled) as e:
        return None, "", "", f"evidence_measure rejected: {str(e)[:300]}"
    except Exception as e:
        return None, "", "", f"evidence_measure failed: {type(e).__name__}: {str(e)[:300]}"
    over, _live = await livebudget.check(sql)
    if over:
        return None, "", "", over
    try:
        rows = await guardrails.run_readonly_sql(sql)
    except Exception as e:
        return None, "", "", f"evidence_measure failed: {type(e).__name__}: {str(e)[:300]}"
    if not rows:
        return None, "", "", "evidence_measure returned 0 rows"
    return ({
        "row_count": len(rows),
        "columns": list(rows[0].keys()),
        "sample": _native(rows[:SAMPLE_ROWS]),
        "truncated": len(rows) >= get_settings().sql_row_limit,
        "relative_dates": False,
    }, sql, em["measure"], "")


# -- exploration notes -----------------------------------------------------

async def ensure_insights_case(conn) -> int:
    """Find or create the standing carrier case. The caller holds the
    transaction and the advisory lock."""
    cid = await conn.fetchval(
        """SELECT id FROM cases
            WHERE opened_reason = 'exploration' AND status = 'open' AND NOT sandbox
            ORDER BY id DESC LIMIT 1""")
    if cid:
        return cid
    return await conn.fetchval(  # Quick 006: origin 'exploration' (the shared case)
        "INSERT INTO cases (title, opened_reason, origin) VALUES ($1, $2, $2) RETURNING id",
        INSIGHTS_CASE_TITLE, ORIGIN)


def sample_text(evidence) -> str:
    """The evidence sample as plain lines (Quick 006: part of the note an insight case
    opens with, next to the finding and the evidence SQL)."""
    ev = evidence if isinstance(evidence, dict) else {}
    sample = ev.get("sample") if isinstance(ev.get("sample"), list) else []
    if not sample:
        return ""
    cols = ev.get("columns") or (list(sample[0]) if isinstance(sample[0], dict) else [])
    lines = [f"Sample ({len(sample)} of {ev.get('row_count', len(sample))} rows):"]
    for row in sample[:SAMPLE_ROWS]:
        if isinstance(row, dict):
            lines.append(", ".join(f"{c}={row.get(c)}" for c in cols)[:400])
    return "\n".join(lines)


OWNER_FIELD_MAX = 300
KEY_FIGURES_MAX = 4
KEY_FIGURE_MAX = 80


def clean_owner_fields(summary="", impact="", next_step="", key_figures=None) -> tuple[dict, list[str]]:
    """Quick 006: the plain-words parts of an insight card (all optional). Bad parts
    are dropped and named, never a reason to refuse the insight itself."""
    out: dict = {}
    dropped: list[str] = []
    for name, val in (("summary", summary), ("impact", impact), ("next_step", next_step)):
        v = val.strip() if isinstance(val, str) else ""
        if not v:
            continue
        if len(v) > OWNER_FIELD_MAX or has_hidden_chars(v):
            dropped.append(f"{name} (over {OWNER_FIELD_MAX} characters or hidden characters)")
            continue
        out[name] = v
    figs = []
    items = key_figures if isinstance(key_figures, list) else (
        [key_figures] if isinstance(key_figures, str) and key_figures.strip() else [])
    for raw in items[:KEY_FIGURES_MAX]:
        t = raw.strip() if isinstance(raw, str) else ""
        label, sep, value = t.partition(":")
        if (not sep or not label.strip() or not value.strip() or len(t) > KEY_FIGURE_MAX
                or has_hidden_chars(t)):
            dropped.append(f"key figure {t[:40]!r} (needs 'Label: value', under {KEY_FIGURE_MAX} characters)")
            continue
        figs.append({"label": label.strip(), "value": value.strip()})
    if len(items) > KEY_FIGURES_MAX:
        dropped.append(f"key figures past the first {KEY_FIGURES_MAX}")
    if figs:
        out["key_figures"] = figs
    return out, dropped


async def record_note(title, finding, evidence_sql, severity, evidence,
                      case_id=None, run_id=None, same_as_case=None,
                      extra: dict | None = None,
                      lane: dict | None = None) -> tuple[int | None, int | None, str]:  # Phase 17 (CONS-04)
    """Write one exploration insight as a case note. Returns
    (event_id, case_id, "") or (None, None, refusal reason).

    Owner change (Quick 006): a warning or critical insight is its own case (title,
    finding, evidence SQL and sample as its first entry); a later one on the same
    subject (same_as_case named by the agent, or the same normalised title) is added
    to it. An info insight opens no case: it is kept on the shared exploration case,
    an internal log the Insights page reads and the Cases page hides."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('ic:exploration-insights', 0))")
            dup = await conn.fetchval(
                """SELECT 1 FROM case_events ce JOIN events e ON e.id = ce.event_id
                    WHERE ce.note = 'exploration'
                      AND btrim(regexp_replace(lower(e.meta->>'title'), '[^[:alnum:]]+', ' ', 'g')) = $1
                      AND e.occurred_at > now() - make_interval(days => $2)
                    LIMIT 1""", norm_title(title), DEDUPE_DAYS)
            if dup:
                return None, None, f"already recorded within {DEDUPE_DAYS} days: {title}"
            if run_id is not None:
                # DISTINCT: a note split onto its own case (Quick 006) has two links.
                n = await conn.fetchval(
                    """SELECT count(*) FROM (SELECT DISTINCT e.id
                         FROM case_events ce JOIN events e ON e.id = ce.event_id
                        WHERE ce.note = 'exploration' AND e.meta->>'task_run_id' = $1::text) x""",
                    str(run_id)) or 0
                if n >= MAX_INSIGHTS_PER_RUN:
                    return None, None, (f"this run already recorded {MAX_INSIGHTS_PER_RUN} "
                                        f"insights (the cap)")
            from . import findingcases
            own = not case_id and severity in findingcases.INSIGHT_CASE_SEVERITIES
            if case_id:
                carrier = case_id
            elif own:
                carrier, _ = await findingcases.insight_case(conn, title, finding, severity,
                                                             same_as=same_as_case)
            else:
                carrier = await ensure_insights_case(conn)
            ex = extra or {}
            words = [ex.get("summary", "")]
            if ex.get("impact"):
                words.append(f"Why it matters: {ex['impact']}")
            if ex.get("next_step"):
                words.append(f"Next step: {ex['next_step']}")
            if ex.get("key_figures"):
                words.append("Key figures: " + "; ".join(
                    f"{k['label']}: {k['value']}" for k in ex["key_figures"]))
            lead = "\n".join(w for w in words if w)
            content = (f"{title}\n\n" + (f"{lead}\n\n" if lead else "")
                       + f"{finding}\n\nEvidence SQL:\n{evidence_sql}")
            sample = sample_text(evidence)
            if sample:
                content += f"\n\n{sample}"
            meta = {"origin": ORIGIN, "title": title, "severity": severity,
                    "finding": finding, "evidence_sql": evidence_sql,
                    "evidence": evidence, "task_run_id": run_id, **(extra or {}), **(lane or {})}
            eid = await cases_util.add_case_note(
                conn, carrier, content[:NOTE_CAP], kind=ORIGIN, cap=NOTE_CAP, meta=meta)
            if own and eid:
                await findingcases.link_insight(conn, carrier, eid, severity)
            return eid, carrier, ""


# ── KPI proposals (create_watch) ──

CADENCES = {"hourly": 3600, "daily": 86400, "weekly": 604800}
MIN_CADENCE = 900
MAX_CADENCE = 604800
MAX_KPI_PER_RUN = 2
KPI_TITLE_MAX = 120
KPI_CONDITION_MAX = 2000
PROPOSAL_KEY_MAX = 64

_TITLE_SQL = "btrim(regexp_replace(lower({c}), '[^[:alnum:]]+', ' ', 'g'))"


def _clamp_cadence(n: int) -> int:
    return max(MIN_CADENCE, min(int(n), MAX_CADENCE))


def parse_cadence(cadence) -> int | None:
    """A CADENCES key (case-insensitive) or all digits meaning minutes."""
    s = str(cadence or "").strip().lower()
    if s in CADENCES:
        return CADENCES[s]
    if s.isascii() and s.isdigit():
        return _clamp_cadence(int(s) * 60)
    return None


def cadence_label(seconds: int) -> str:
    for name, secs in CADENCES.items():
        if secs == seconds:
            return name
    return f"every {int(seconds) // 60} min"


def validate_kpi(title, condition) -> str | None:
    t = (title or "").strip() if isinstance(title, str) else ""
    c = (condition or "").strip() if isinstance(condition, str) else ""
    if not 1 <= len(t) <= KPI_TITLE_MAX:
        return f"title must be 1-{KPI_TITLE_MAX} characters"
    if not 1 <= len(c) <= KPI_CONDITION_MAX:
        return f"condition must be 1-{KPI_CONDITION_MAX} characters"
    if has_hidden_chars(t) or has_hidden_chars(c):
        return "title and condition must not contain control or hidden characters"
    return None


KPI_LOCK = asyncio.Lock()   # serialises propose_kpi check-then-insert in this process


async def kpi_conflict(title_key: str, run_id=None, cap: int = MAX_KPI_PER_RUN) -> str | None:  # Phase 12: cap
    """Reason to refuse a KPI proposal (already watched, proposed or declined,
    or the per-run cap), else None."""
    pool = await get_pool()
    w = await pool.fetchrow(
        f"""SELECT id, title FROM watches
             WHERE {_TITLE_SQL.format(c='title')} = $1 AND status <> 'done'
             LIMIT 1""", title_key)
    if w:
        return f"already watched: {w['title']}"
    a = await pool.fetchrow(
        """SELECT id, status FROM actions
            WHERE kind = 'create_watch' AND payload->>'title_key' = $1
              AND status <> 'failed'
            ORDER BY id DESC LIMIT 1""", title_key)
    if a:
        return f"already proposed or declined (action #{a['id']}, {a['status']})"
    if run_id is not None:
        n = await pool.fetchval(
            "SELECT count(*) FROM actions WHERE kind = 'create_watch' AND task_run_id = $1",
            run_id) or 0
        if n >= cap:
            return f"this run already proposed {cap} KPIs (the cap)"
    return None


TRIGGER_TYPES = ("increase", "threshold", "change")
TRIGGER_OPS = (">", "<", ">=", "<=")
DRAFT_SQL_MAX = 6000


def clean_draft(sql, trigger) -> tuple[str | None, dict | None, list[str]]:
    """Validate a context-pack draft (SQL + trigger hint) before it is stored as the
    compiler's starting point. Untrusted data: shape-check only; the sentinel still
    runs the SQL through the read-only guardrails before caching it. Returns
    (sql | None, trigger | None, reasons it was dropped)."""
    notes: list[str] = []
    out_sql = None
    if sql not in (None, ""):
        t = sql.strip() if isinstance(sql, str) else ""
        if not t:
            notes.append("draft SQL is not text")
        elif len(t) > DRAFT_SQL_MAX:
            notes.append(f"draft SQL is longer than {DRAFT_SQL_MAX} characters")
        elif has_hidden_chars(t):
            notes.append("draft SQL has control or hidden characters")
        elif not re.match(r"(?is)^\s*(select|with)\b", t):
            notes.append("draft SQL is not a SELECT")
        else:
            out_sql = t
    out_trig = None
    if trigger not in (None, {}, ""):
        ok = isinstance(trigger, dict) and trigger.get("type") in TRIGGER_TYPES
        col = trigger.get("column") if isinstance(trigger, dict) else None
        ok = ok and isinstance(col, str) and 1 <= len(col) <= 63 and not has_hidden_chars(col)
        clean = None
        if ok:
            clean = {"type": trigger["type"], "column": col}
            if trigger["type"] == "threshold":
                op = trigger.get("op", ">")
                val = trigger.get("value")
                if op in TRIGGER_OPS and isinstance(val, (int, float)) and not isinstance(val, bool):
                    clean.update(op=op, value=val)
                else:
                    clean = None
        if clean:
            out_trig = clean
        else:
            notes.append("trigger_hint is not a valid trigger (type increase|threshold|change, column; "
                         "threshold needs op and a numeric value)")
    return out_sql, out_trig, notes


async def create_watch_from_proposal(payload) -> bool:
    """Body of the create_watch approval executor. The payload is stored JSON
    and untrusted: re-validate, hard-code provenance, insert idempotently."""
    if not isinstance(payload, dict):
        log.warning("create_watch: payload is not an object")
        return False
    key = payload.get("proposal_key")
    if not isinstance(key, str) or not key.strip() or len(key) > PROPOSAL_KEY_MAX:
        log.warning("create_watch: missing or invalid proposal_key")
        return False
    reason = validate_kpi(payload.get("title"), payload.get("condition"))
    if reason:
        log.warning("create_watch refused: %s", reason)
        return False
    secs = payload.get("cadence_seconds")
    if isinstance(secs, int) and not isinstance(secs, bool):
        cadence = _clamp_cadence(secs)
    else:
        cadence = parse_cadence(payload.get("cadence"))
        if cadence is None:
            log.warning("create_watch refused: unparseable cadence")
            return False
    title = payload["title"].strip()[:KPI_TITLE_MAX]
    condition = payload["condition"].strip()
    # Phase 12 (CTX-09d): honour an app thread and a known origin only
    origin = payload.get("origin") if payload.get("origin") in PROPOSAL_ORIGINS else "exploration"
    req_thread = payload.get("thread_id")
    thread = (req_thread if isinstance(req_thread, str) and appchannel.is_app_thread(req_thread)
              else (get_settings().owner_number or "dashboard"))
    d_sql, d_trig, _ = clean_draft(payload.get("draft_sql"), payload.get("trigger_hint"))
    pool = await get_pool()
    if payload.get("measure_ref") is not None:  # Phase 17 (CONS-01)
        return await _create_measure_watch(pool, payload, thread, title, condition, cadence, origin, key)
    wid = await pool.fetchval(
        """INSERT INTO watches (thread_id, kind, title, condition, cadence_seconds,
                                one_shot, origin, origin_ref, draft_sql, draft_trigger)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
           ON CONFLICT (origin_ref) WHERE origin_ref IS NOT NULL DO NOTHING
           RETURNING id""",
        thread, "kpi", title, condition, cadence, False, origin, key,
        d_sql, json.dumps(d_trig) if d_trig else None)
    if wid:
        return True
    # A retry of the same proposal still means exactly one watch.
    return bool(await pool.fetchval(
        "SELECT 1 FROM watches WHERE origin_ref = $1", key))


# ── Phase 17 (CONS-01): measure-backed KPI proposals ──
ALERT_TYPES = ("record", "increase", "change")


def measure_trigger(measure_name, alert) -> tuple:
    """(trigger, refusal). None or "" records only; "increase"/"change"; or {op, value}."""
    if alert in (None, "", "record"):
        return {"type": "record", "column": measure_name}, None
    if alert in ("increase", "change"):
        return {"type": alert, "column": measure_name}, None
    if isinstance(alert, dict):
        val = alert.get("value")
        if (alert.get("op") in TRIGGER_OPS and isinstance(val, (int, float))
                and not isinstance(val, bool)):
            return {"type": "threshold", "column": measure_name, "op": alert["op"], "value": val}, None
    return None, ("REFUSED: alert must be increase, change or {\"op\": one of > < >= <=, "
                  "\"value\": a number}")


def _alert_from_trigger(trig) -> object:
    if not isinstance(trig, dict):
        return None
    if trig.get("type") == "threshold":
        return {"op": trig.get("op"), "value": trig.get("value")}
    return trig.get("type")


async def measure_payload(measure, filters, range, alert, title, condition) -> tuple:  # noqa: A002
    """(fields, refusal): measure_ref, trigger_preset and condition for a measure KPI."""
    from . import modeltools, semanticmodel
    ref = {"measure": measure, "filters": filters or [], "range": range}
    clean, _sql, col, refusal = await modeltools.check_watch_measure(ref)
    if refusal:
        return None, refusal
    trig, refusal = measure_trigger(col, alert)
    if refusal:
        return None, refusal
    cond = (condition or "").strip()
    if not cond:
        label = clean["measure"]
        try:
            m = (await semanticmodel.load_model()).measure(clean["measure"])
            label = (m.label if m and m.label else label)
        except Exception:
            pass
        cond = f"Track {label} ({clean['range'] or 'all time'})"
    return {"measure_ref": clean, "trigger_preset": trig, "condition": cond}, None


async def _create_measure_watch(pool, payload, thread, title, condition, cadence, origin, key) -> bool:
    from . import modeltools
    clean, _sql, col, refusal = await modeltools.check_watch_measure(payload.get("measure_ref"))
    if refusal:
        log.warning("create_watch refused: %s", refusal)
        return False
    trig, refusal = measure_trigger(col, _alert_from_trigger(payload.get("trigger_preset")))
    if refusal:
        log.warning("create_watch refused: %s", refusal)
        return False
    wid = await pool.fetchval(
        """INSERT INTO watches (thread_id, kind, title, condition, cadence_seconds,
                                one_shot, origin, origin_ref, measure_ref, trigger_rule)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
           ON CONFLICT (origin_ref) WHERE origin_ref IS NOT NULL DO NOTHING
           RETURNING id""",
        thread, "kpi", title, condition, cadence, False, origin, key,
        json.dumps(clean), json.dumps(trig))
    if wid:
        return True
    return bool(await pool.fetchval("SELECT 1 FROM watches WHERE origin_ref = $1", key))
# ── End Phase 17 (CONS-01) ──


# ── Phase 12: reactive and context-pack KPI proposals (CTX-09d, CTX-08) ──
PROPOSAL_ORIGINS = ("exploration", "chat", "context_pack")


async def propose_kpi_action(title, condition, cadence="daily", rationale="", *,
                             origin, thread_id, run_id, cap: int = MAX_KPI_PER_RUN,
                             draft_sql=None, trigger_hint=None,
                             measure=None, filters=None, range=None, alert=None) -> str:  # Phase 17
    """Record a proposed create_watch action (approval-gated). Shared by the
    chat propose_kpi tool (cap 2) and context-pack KPI candidates (cap 8)."""
    import uuid
    if origin not in PROPOSAL_ORIGINS:
        return "REFUSED: unknown proposal origin"
    mfields = None
    if measure:  # Phase 17 (CONS-01): refuse an unmodeled measure before anything is written
        mfields, mrefusal = await measure_payload(measure, filters, range, alert, title, condition)
        if mrefusal:
            return mrefusal
        condition = mfields["condition"]
    reason = validate_kpi(title, condition)
    if reason:
        return f"REFUSED: {reason}"
    seconds = parse_cadence(cadence)
    if seconds is None:
        return ("REFUSED: cadence must be hourly, daily, weekly or a number of "
                "minutes (at least 15)")
    title_key = norm_title(title)
    if not title_key:
        return "REFUSED: title needs letters or digits"
    d_sql, d_trig, d_notes = clean_draft(draft_sql, trigger_hint)
    async with KPI_LOCK:
        conflict = await kpi_conflict(title_key, run_id, cap=cap)
        if conflict:
            return f"REFUSED: {conflict}"
        label = cadence_label(seconds)
        payload = {
            "_summary": f"Watch a new KPI: {title.strip()} ({label})",
            "title": title.strip(), "title_key": title_key,
            "condition": condition.strip(), "cadence": label,
            "cadence_seconds": seconds,
            "rationale": (rationale or "").strip()[:1000],
            "origin": origin, "thread_id": thread_id,
            "proposal_key": uuid.uuid4().hex,
        }
        if d_sql:
            payload["draft_sql"] = d_sql
        if d_trig:
            payload["trigger_hint"] = d_trig
        if mfields:  # Phase 17 (CONS-01)
            payload["measure_ref"] = mfields["measure_ref"]
            payload["trigger_preset"] = mfields["trigger_preset"]
            payload["_summary"] = f"Watch a new KPI: {title.strip()} ({label}), measure {measure}"
            payload.pop("draft_sql", None)
            payload.pop("trigger_hint", None)
        pool = await get_pool()
        try:
            aid = await pool.fetchval(
                "INSERT INTO actions (kind, payload, tier, status, task_run_id) "
                "VALUES ('create_watch', $1::jsonb, 'draft', 'proposed', $2) RETURNING id",
                json.dumps(payload), run_id)
        except asyncpg.UniqueViolationError:
            return "already proposed: a KPI with this title is awaiting approval"
    extra = ""
    if d_sql:
        extra += " Draft SQL attached (validated again when the watch is compiled)."
    if d_notes:
        extra += " Not attached: " + "; ".join(d_notes) + "."
    return (f"proposed KPI watch (action #{aid}): '{title.strip()}' every {label} "
            f"- awaiting owner approval. Nothing runs until approved.{extra}")
# ── End Phase 12 KPI block ──


# ── Playbook context lines (read by playbooks._assemble_context in 07-05) ──

MAX_CTX_TABLES = 40
MAX_CTX_TITLES = 30
MAX_CTX_KPIS = 40
MAX_CTX_OPEN_CASES = 20   # Quick 006


async def exploration_context(include_kpis: bool = True) -> str:
    """Context blocks for the nightly exploration run."""
    lines: list[str] = []
    appr = await approved_tables()
    if not appr:
        lines.append("APPROVED TABLES: none. Nothing has been approved for study yet. "
                     "Stop now without calling record_insight or propose_kpi.")
    else:
        lines.append("APPROVED TABLES (study only these):")
        for t in appr[:MAX_CTX_TABLES]:
            ref = t.get("ref") or f"{t.get('schema_name')}.{t.get('table_name')}"
            lines.append(f"- {ref}: {(t.get('description') or '')[:160]}")
    pool = await get_pool()
    notes = await pool.fetch(
        """SELECT e.meta->>'title' AS title, e.occurred_at::date AS day
             FROM case_events ce JOIN events e ON e.id = ce.event_id
            WHERE ce.note = 'exploration' AND e.occurred_at > now() - make_interval(days => $1)
            ORDER BY e.occurred_at DESC LIMIT $2""", DEDUPE_DAYS, MAX_CTX_TITLES)
    seen: set = set()
    uniq = []
    for n in notes:   # Quick 006: a split note has two links; list it once
        if n["title"] and (n["title"], n["day"]) not in seen:
            seen.add((n["title"], n["day"]))
            uniq.append(n)
    notes = uniq[:MAX_CTX_TITLES]
    if notes:
        lines.append(f"ALREADY RECORDED (last {DEDUPE_DAYS} days; do not repeat):")
        lines.extend(f"- {n['day']}: {n['title']}" for n in notes)
    # Quick 006: same subject, same case. The agent names an open insight case when a
    # new finding is about the same thing (record_insight same_as_case).
    open_cases = await pool.fetch(
        """SELECT id, title FROM cases
            WHERE origin = 'insight' AND status = 'open' AND NOT sandbox
            ORDER BY last_activity_at DESC NULLS LAST LIMIT $1""", MAX_CTX_OPEN_CASES)
    if open_cases:
        lines.append("OPEN INSIGHT CASES (a new finding about the same subject as one of these "
                     "is added to it: pass its number as same_as_case):")
        lines.extend(f"- #{r['id']}: {r['title']}" for r in open_cases)
    if include_kpis:
        titles: list[str] = []
        for sql in ("SELECT title FROM watches WHERE status <> 'done' "
                    "ORDER BY id DESC LIMIT 40",
                    "SELECT payload->>'title' AS title FROM actions "
                    "WHERE kind = 'create_watch' AND status <> 'failed' "
                    "ORDER BY id DESC LIMIT 40"):
            for r in await pool.fetch(sql):
                if r["title"] and r["title"] not in titles:
                    titles.append(r["title"])
        if titles:
            lines.append("ALREADY WATCHED, PROPOSED OR DECLINED KPIs (do not propose again):")
            lines.extend(f"- {t}" for t in titles[:MAX_CTX_KPIS])
    return "\n".join(lines)


# ── Insights listing (GET /api/insights; read-only, never tenant SQL) ──

ORIGINS = {"exploration", "investigation", "all"}
CASE_FILTERS = {"open", "closed"}   # Quick 006
SEVERITY_FILTERS = {"info", "warning", "critical"}
MAX_LIST = 200


def _obj(v) -> dict:
    """asyncpg returns jsonb as str: decode at the edge."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


_NOTES_BASE = """SELECT e.id, e.content, e.meta, e.occurred_at, ce.note,
       c.id AS case_id, c.title AS case_title, c.origin AS case_origin, c.status AS case_status
  FROM case_events ce
  JOIN events e ON e.id = ce.event_id
  JOIN cases c ON c.id = ce.case_id"""

# Quick 006: a note split onto its own insight case is linked twice (its own case and
# the archived shared case); list it once, under its own case.
_NOTES_SQL = """SELECT * FROM (
SELECT DISTINCT ON (e.id) e.id, e.content, e.meta, e.occurred_at, ce.note,
       c.id AS case_id, c.title AS case_title, c.origin AS case_origin, c.status AS case_status
  FROM case_events ce
  JOIN events e ON e.id = ce.event_id
  JOIN cases c ON c.id = ce.case_id
 WHERE ce.note = ANY($1::text[]) AND NOT c.sandbox
   AND ($2::timestamptz IS NULL OR e.occurred_at < $2)
   AND ($3::text IS NULL OR COALESCE(e.meta->>'severity', 'info') = $3)
 ORDER BY e.id, (c.origin IS NOT DISTINCT FROM 'exploration'), c.id DESC) n
 WHERE ($4::text IS NULL OR (n.case_origin IS DISTINCT FROM 'exploration'
        AND CASE WHEN n.case_status IN ('open', 'pending_ack') THEN 'open' ELSE 'closed' END = $4))
 ORDER BY n.occurred_at DESC, n.id DESC
 LIMIT $5"""

_WATCH_SQL = """SELECT we.id, we.severity, we.summary, we.created_at,
       w.id AS watch_id, w.title AS watch_title, w.compiled_sql, w.origin AS watch_origin,
       we.case_id, wc.title AS case_title, wc.status AS case_status
  FROM watch_events we
  JOIN watches w ON w.id = we.watch_id
  LEFT JOIN cases wc ON wc.id = we.case_id
 WHERE we.kind = 'fire' AND {origin_filter}($1::timestamptz IS NULL OR we.created_at < $1)
   AND ($2::text IS NULL OR COALESCE(we.severity, 'info') = $2)
   AND ($3::text IS NULL OR (wc.id IS NOT NULL
        AND CASE WHEN wc.status IN ('open', 'pending_ack') THEN 'open' ELSE 'closed' END = $3))
 ORDER BY we.created_at DESC, we.id DESC
 LIMIT $4"""


def _note_item(r) -> dict:
    m = _obj(r["meta"])
    content = r["content"] or ""
    title = m.get("title") or (content.strip().splitlines() or [""])[0][:160]
    return {"type": "note", "id": f"note:{r['id']}", "origin": r["note"],
            "title": title, "finding": m.get("finding") or content,
            "severity": m.get("severity") or "info",
            "evidence_sql": m.get("evidence_sql"),
            "evidence": m.get("evidence") if isinstance(m.get("evidence"), dict) else None,
            # Quick 006: the shared exploration case is an internal log (hidden on
            # Cases), so an info insight there links to no case.
            "case_id": None if _get(r, "case_origin") == "exploration" else r["case_id"],
            "case_title": None if _get(r, "case_origin") == "exploration" else r["case_title"],
            "case_status": None if _get(r, "case_origin") == "exploration" else _get(r, "case_status"),
            "watch_id": None, "watch_title": None,
            "task_run_id": m.get("task_run_id"),
            "lane": m.get("lane"), "verified": m.get("verified"),  # Phase 17 (CONS-04)
            "measure": m.get("measure") if m.get("lane") == "measure" else None,  # WR-10
            # Quick 006: the plain-words parts of the card (absent on older insights).
            "summary": m.get("summary") or None, "impact": m.get("impact") or None,
            "next_step": m.get("next_step") or None,
            "key_figures": m.get("key_figures") if isinstance(m.get("key_figures"), list) else [],
            "created_at": r["occurred_at"].isoformat(), "_n": r["id"]}


def _get(r, key):
    """Quick 006 columns, read tolerantly (a row from an older query shape lacks them)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _watch_item(r) -> dict:
    return {"type": "watch_event", "id": f"watch_event:{r['id']}", "origin": r["watch_origin"] or "watch",
            "title": r["watch_title"], "finding": r["summary"] or "",
            "severity": r["severity"] or "info", "evidence_sql": r["compiled_sql"],
            "evidence": None, "case_id": _get(r, "case_id"), "case_title": _get(r, "case_title"),
            "case_status": _get(r, "case_status"),
            "summary": None, "impact": None, "next_step": None, "key_figures": [],
            "watch_id": r["watch_id"], "watch_title": r["watch_title"],
            "task_run_id": None,
            "created_at": r["created_at"].isoformat(), "_n": r["id"], "_w": r["created_at"]}


async def list_insights(origin: str = "exploration", limit: int = 50, before=None,
                        severity: str | None = None, case: str | None = None) -> dict:
    """Exploration notes plus exploration-watch events, newest first. Shows the
    stored evidence only; never runs SQL against the business database. case='open'
    or 'closed' keeps insights whose case is open or closed (Quick 006)."""
    limit = max(1, min(int(limit), MAX_LIST))
    origins = ["exploration", "investigation"] if origin == "all" else [origin]
    case = case if case in CASE_FILTERS else None
    pool = await get_pool()
    rows = await pool.fetch(_NOTES_SQL, origins, before, severity, case, limit)
    items = []
    for r in rows:
        it = _note_item(r)
        it["_w"] = r["occurred_at"]
        items.append(it)
    if origin != "investigation":
        flt = "w.origin = 'exploration' AND " if origin == "exploration" else ""
        for r in await pool.fetch(_WATCH_SQL.format(origin_filter=flt),
                                  before, severity, case, limit):
            items.append(_watch_item(r))
    items.sort(key=lambda i: (i["_w"], i["_n"]), reverse=True)
    items = items[:limit]
    for i in items:
        i.pop("_w", None)
        i.pop("_n", None)
    next_before = items[-1]["created_at"] if len(items) == limit else None
    return {"origin": origin, "items": items, "next_before": next_before}


async def insights_summary(days: int = DEDUPE_DAYS) -> dict:
    """Quick 006: the Insights page summary strip: exploration insights by severity over
    the last `days` days (each counted once), open insight cases, and the latest
    exploration run. Read-only, working-memory DB only."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT COALESCE(e.meta->>'severity', 'info') AS severity, count(DISTINCT e.id) AS n
             FROM case_events ce JOIN events e ON e.id = ce.event_id
             JOIN cases c ON c.id = ce.case_id
            WHERE ce.note = 'exploration' AND NOT c.sandbox
              AND e.occurred_at > now() - make_interval(days => $1)
            GROUP BY 1""", days)
    by = {s: 0 for s in INSIGHT_SEVERITIES}
    for r in rows:
        if r["severity"] in by:
            by[r["severity"]] = int(r["n"])
    open_cases = await pool.fetchval(
        "SELECT count(*) FROM cases WHERE origin = 'insight' AND status IN ('open', 'pending_ack') "
        "AND NOT sandbox") or 0
    run = await pool.fetchrow(
        """SELECT t.id, t.status, t.created_at, t.ended_at, t.error FROM task_runs t
             JOIN playbooks p ON p.id = t.playbook_id
            WHERE p.name = 'explore_data' ORDER BY t.id DESC LIMIT 1""")
    pb = await pool.fetchrow(
        "SELECT id, status FROM playbooks WHERE name = 'explore_data' ORDER BY id LIMIT 1")
    return {"days": days, "by_severity": by, "open_cases": int(open_cases),
            "last_run": ({"id": run["id"], "status": run["status"],
                          "started_at": run["created_at"], "ended_at": run["ended_at"],
                          "error": run["error"]} if run else None),
            # Quick 023: what the "Run exploration now" button needs
            "playbook": ({"id": pb["id"], "status": pb["status"]} if pb else None)}


# ── Phase 10: single-insight reader (get_insight tool; read-only) ──

INSIGHT_KEY_RE = re.compile(r"^(note|watch_event):([1-9][0-9]{0,17})$")
INSIGHT_NOTE_ORIGINS = ["exploration", "investigation"]


def parse_insight_key(key) -> tuple[str, int] | None:
    if not isinstance(key, str):
        return None
    m = INSIGHT_KEY_RE.match(key.strip())
    return (m.group(1), int(m.group(2))) if m else None


async def get_insight(key, thread_id: str | None = None) -> dict | None:
    """One insight by key (note:N or watch_event:N), scoped to the calling
    thread: user-kind watch events and investigation notes from other chats
    look exactly like unknown keys (an operator's Ask chats count as one
    conversation, Quick 003). Never runs tenant SQL."""
    parsed = parse_insight_key(key)
    if not parsed:
        return None
    kind, n = parsed
    pool = await get_pool()
    if kind == "note":
        sql = _NOTES_BASE + (
            " WHERE e.id = $1 AND ce.note = ANY($2::text[]) AND NOT c.sandbox"
            " AND (ce.note = 'exploration' OR ($3::text IS NOT NULL"
            " AND c.origin_chat_jid = $3))"
            " ORDER BY (c.origin IS NOT DISTINCT FROM 'exploration'), ce.case_id LIMIT 1")
        row = await pool.fetchrow(sql, n, INSIGHT_NOTE_ORIGINS, thread_id)
        item = _note_item(row) if row else None
    else:
        # Quick 003: an owner's Ask chats are one scope ($3 = that operator's main chat).
        sql = _WATCH_SQL.split(" WHERE ")[0] + (
            " WHERE we.id = $1 AND we.kind = 'fire' AND (w.kind = 'kpi' OR (w.kind = 'user'"
            " AND $2::text IS NOT NULL AND (w.thread_id = $2 OR ($3::text IS NOT NULL"
            " AND (w.thread_id = $3 OR left(w.thread_id, length($3) + 1) = $3 || ':')))))"
            " LIMIT 1")
        row = await pool.fetchrow(sql, n, thread_id, appchannel.main_thread_of(thread_id))
        item = _watch_item(row) if row else None
    if item:
        item.pop("_n", None)
        item.pop("_w", None)
    return item
