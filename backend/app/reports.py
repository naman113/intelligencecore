"""Quick 047: scheduled WhatsApp reports ("send me X every day at 9 PM").

The supervisor proposes one with `propose_scheduled_report` (reporttools.py);
the owner approves it on Approvals (action kind `create_scheduled_report`);
`create_from_proposal` writes an active cron playbook whose `report` column
holds the frozen spec and whose trigger carries the minute and the zone. The
expectation scheduler's cron dispatch queues it at HH:MM local like any
scheduled playbook (task_runs + worker: no second scheduler), and
`playbooks.run_playbook_task` hands such a row to `run` here before any agent
is built.

A run makes NO model call (llm-token-economy R7: a digest is rule-based work):
every item is read through the governed compiler (a measure) or guardrails (a
small details table), formatted by fixed code, and sent once through
`whatsapp.send_message` with an executed `actions` row. The agent authors the
spec; this module only executes it.
"""

import asyncio
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import asyncpg

from . import guardrails
from .config import get_settings
from .db import get_pool
from .guardrails import SQLGuardrailError

log = logging.getLogger("reports")

ACTION_KIND = "create_scheduled_report"
MAX_ITEMS = 6
MAX_BY = 2
MAX_TABLE_ROWS = 20
MAX_REPORTS = 20          # live report playbooks plus pending proposals
MAX_PER_RUN = 2           # proposals per chat run
TITLE_MAX = 80
LABEL_MAX = 80
SQL_MAX = 4000
SPEC_MAX = 20000          # serialized items
CELL_MAX = 40
LINE_MAX = 160
MESSAGE_MAX = 3500
PREVIEW_MAX = 1500
ITEM_TIMEOUT = 45
KEY_MAX = 64
DAY_RANGES = ("today", "yesterday")
REPORT_LOCK = asyncio.Lock()   # serialises the propose check-then-insert in this process

_AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TOKEN_RE = re.compile(r"\{(today|yesterday|tomorrow)\}")
_DM_RE = re.compile(r"^\d{6,20}(@lid)?$")   # a direct chat: phone digits or <lid>@lid
_MEASURE_KEYS = {"label", "measure", "range", "filters", "by"}
_SQL_KEYS = {"label", "sql"}


def ranges() -> tuple:
    from .modeltools import WATCH_RANGES
    return DAY_RANGES + tuple(WATCH_RANGES)


# ── validation (pure apart from ZoneInfo and the settings default) ────────────

def parse_at(at) -> tuple[int, int] | None:
    """'21:00' / '9:05' -> (21, 0) / (9, 5); None when it is not HH:MM 24-hour."""
    m = _AT_RE.match(at.strip()) if isinstance(at, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def zone_name(tz) -> tuple[str | None, str | None]:
    """(IANA name, None) or (None, refusal). Empty means the operator's zone."""
    if tz is not None and not isinstance(tz, str):
        return None, "timezone must be an IANA name such as Asia/Kolkata"
    name = (tz or "").strip()
    if not name:
        from .kpihistory import _tz
        name = _tz()
    try:
        if len(name) > KEY_MAX:
            raise ValueError(name)
        ZoneInfo(name)
    except Exception:
        return None, (f"unknown timezone {name[:KEY_MAX]!r}: use an IANA name such as "
                      "Asia/Kolkata (IST) or Europe/London")
    return name, None


def _has_hidden(s: str) -> bool:
    from .insights import has_hidden_chars
    return has_hidden_chars(s)


def _clean_text(v, cap: int) -> str | None:
    """One line of plain text (whitespace collapsed, no WhatsApp bold markers), or None."""
    if not isinstance(v, str) or _has_hidden(v):
        return None
    s = " ".join(v.replace("*", "").split())
    return s[:cap] if s else None


def day_dates(today: date) -> dict:
    return {"today": today, "yesterday": today - timedelta(days=1),
            "tomorrow": today + timedelta(days=1)}


def fill_tokens(sql: str, today: date) -> str:
    """{today}, {yesterday}, {tomorrow} -> DATE 'YYYY-MM-DD' of the business day."""
    d = day_dates(today)
    return _TOKEN_RE.sub(lambda m: f"DATE '{d[m.group(1)].isoformat()}'", sql)


def validate_items(items) -> tuple[list | None, str | None]:
    from . import modelcompiler
    if not isinstance(items, list) or not items:
        return None, f"items must be a list of 1 to {MAX_ITEMS} report items"
    if len(items) > MAX_ITEMS:
        return None, f"at most {MAX_ITEMS} items in one report"
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            return None, f"item {i} must be an object"
        label = _clean_text(it.get("label"), LABEL_MAX)
        if not label:
            return None, f"item {i} needs a label (plain text)"
        if ("measure" in it) == ("sql" in it):
            return None, f"item {i} needs exactly one of measure or sql"
        if "measure" in it:
            extra = sorted(set(it) - _MEASURE_KEYS)
            if extra:
                return None, f"item {i}: a measure item takes only label, measure, range, filters and by"
            name = it.get("measure")
            if not isinstance(name, str) or not modelcompiler.NAME_RE.match(name):
                return None, f"item {i}: measure must be a modeled measure name (see get_model)"
            rng = it.get("range")
            if rng is not None and rng not in ranges():
                return None, f"item {i}: range must be one of " + ", ".join(ranges())
            fl = it.get("filters") or []
            if not isinstance(fl, list) or any(not isinstance(f, dict) for f in fl):
                return None, f"item {i}: filters must be a list of {{dimension, op, values}}"
            if len(fl) > modelcompiler.MAX_FILTERS:
                return None, f"item {i}: at most {modelcompiler.MAX_FILTERS} filters"
            by = it.get("by") or []
            if (not isinstance(by, list) or len(by) > MAX_BY
                    or any(not isinstance(d, str) or not d.strip() for d in by)):
                return None, f"item {i}: by is a list of at most {MAX_BY} dimensions"
            out.append({"label": label, "measure": name, "range": rng,
                        "filters": [dict(f) for f in fl], "by": [d.strip() for d in by]})
        else:
            extra = sorted(set(it) - _SQL_KEYS)
            if extra:
                return None, f"item {i}: a table item takes only label and sql"
            sql = it.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                return None, f"item {i}: sql must be one SELECT"
            sql = sql.strip()
            if len(sql) > SQL_MAX:
                return None, f"item {i}: sql is longer than {SQL_MAX} characters"
            if _has_hidden(sql):
                return None, f"item {i}: sql must not contain control or hidden characters"
            try:
                guardrails.validate_readonly_sql(fill_tokens(sql, date(2000, 1, 1)))
            except SQLGuardrailError as e:
                return None, f"item {i}: the SQL was rejected: {e}"
            out.append({"label": label, "sql": sql})
    if len(json.dumps(out, default=str)) > SPEC_MAX:
        return None, "the report items are too large; keep them short"
    return out, None


def validate_spec(title, at, timezone, items) -> tuple[dict | None, str | None]:
    from .insights import norm_title
    t = _clean_text(title, TITLE_MAX)
    if not t or not norm_title(t):
        return None, "title must be a short plain-text name"
    hm = parse_at(at)
    if hm is None:
        return None, "at must be HH:MM in 24-hour time (9 PM is 21:00)"
    zone, err = zone_name(timezone)
    if err:
        return None, err
    clean, err = validate_items(items)
    if err:
        return None, err
    return {"title": t, "title_key": norm_title(t), "at": f"{hm[0]:02d}:{hm[1]:02d}",
            "hour": hm[0], "minute": hm[1], "timezone": zone, "items": clean}, None


def trigger_for(spec: dict) -> dict:
    return {"type": "cron", "hour": spec["hour"], "minute": spec["minute"], "tz": spec["timezone"]}


# ── formatting (pure) ────────────────────────────────────────────────────────

def fmt_value(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float, Decimal)):
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return str(v)
        if not math.isfinite(f):
            return str(v)
        if isinstance(v, int):
            return f"{v:,}"
        if f == int(f) and abs(f) < 1e15:
            return f"{int(f):,}"
        return f"{f:,.2f}"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    s = " ".join(str(v).replace("*", "").split())
    return s if len(s) <= CELL_MAX else s[:CELL_MAX - 1] + "…"


def _line(s: str) -> str:
    return s if len(s) <= LINE_MAX else s[:LINE_MAX - 1] + "…"


@dataclass
class ItemResult:
    label: str
    ok: bool
    text: str
    error: str = ""
    sql: str = ""


def table_text(label: str, rows: list, columns: list | None = None) -> str:
    """A details table as plain lines: 'a · b · c', first MAX_TABLE_ROWS rows."""
    cols = list(columns) if columns else (list(rows[0].keys()) if rows else [])
    if not rows:
        return f"{label}: none"
    if len(rows) == 1 and len(cols) == 1:
        return f"{label}: {fmt_value(rows[0].get(cols[0]))}"
    lines = [f"{label}:", _line(" · ".join(str(c) for c in cols))]
    lines += [_line(" · ".join(fmt_value(r.get(c)) for c in cols)) for r in rows[:MAX_TABLE_ROWS]]
    if len(rows) > MAX_TABLE_ROWS:
        lines.append(f"(first {MAX_TABLE_ROWS} rows shown)")
    return "\n".join(lines)


def compose(title: str, now: datetime, results: list, notes: list | None = None) -> str:
    head = f"*{title}*\n{now.strftime('%a %d %b %Y, %H:%M')} {now.tzname() or ''}".rstrip()
    msg = head + "\n\n" + "\n\n".join(r.text for r in results)
    if notes:
        msg += "\n\n" + "\n".join(notes)
    if len(msg) > MESSAGE_MAX:
        msg = msg[:MESSAGE_MAX - 40].rstrip() + "\n(report cut to fit one message)"
    return msg


# ── reading the items ────────────────────────────────────────────────────────

def _short(e: Exception) -> str:
    return " ".join(str(e).split())[:200]


def reason(e: Exception) -> str:
    """Owner-readable cause of an item failure (never a driver connection message)."""
    from . import modelcompiler
    if isinstance(e, modelcompiler.NotModeled):
        return f"NOT MODELED: {_short(e)}"
    if isinstance(e, modelcompiler.InvalidRequest):
        return f"REFUSED: {_short(e)}"
    if isinstance(e, SQLGuardrailError):
        return f"GUARDRAIL REJECTED: {_short(e)}"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError, asyncpg.QueryCanceledError)):
        return "took too long; narrow the query"
    if isinstance(e, (ConnectionError, OSError, asyncpg.PostgresConnectionError,
                      asyncpg.InterfaceError, asyncpg.CannotConnectNowError)):
        return "the business database is unavailable"
    if isinstance(e, asyncpg.PostgresError):
        return f"SQL error: {_short(e)}"
    return type(e).__name__


async def _read_measure(item: dict, today: date, model) -> ItemResult:
    from . import modelcompiler, modeltools
    rng = item.get("range")
    if rng in DAY_RANGES:
        start = day_dates(today)[rng]
        rng = {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()}
    request, _s, _e = modeltools.build_measure_request(
        model, item["measure"], by=item.get("by") or None, filters=item.get("filters") or None,
        range=rng, today=today)
    compiled = modelcompiler.compile_measure(model, request)
    rows = await guardrails.run_readonly_sql(compiled.sql, limit=MAX_TABLE_ROWS + 1)
    m = model.measure(request.measure)
    unit = (getattr(m, "unit", None) or "").strip()
    value_col = compiled.columns[-1]
    label = item["label"]
    if not item.get("by"):
        v = rows[0].get(value_col) if rows else None
        text = f"{label}: {fmt_value(v)}" + (f" {unit}" if unit and v is not None else "")
    elif not rows:
        text = f"{label}: none"
    else:
        lines = [f"{label}:"]
        for r in rows[:MAX_TABLE_ROWS]:
            keys = " · ".join(fmt_value(r.get(c)) for c in compiled.columns[:-1])
            val = fmt_value(r.get(value_col)) + (f" {unit}" if unit else "")
            lines.append(_line(f"{keys}: {val}"))
        if len(rows) > MAX_TABLE_ROWS:
            lines.append(f"(first {MAX_TABLE_ROWS} rows shown)")
        text = "\n".join(lines)
    return ItemResult(label, True, text, sql=compiled.sql)


async def _read_sql(item: dict, today: date) -> ItemResult:
    sql = fill_tokens(item["sql"], today)
    rows = await guardrails.run_readonly_sql(sql, limit=MAX_TABLE_ROWS + 1)
    return ItemResult(item["label"], True, table_text(item["label"], rows), sql=sql)


async def _stale_notes(sqls: list) -> list:
    """One note per stale import a read touched (freshness never raises)."""
    from . import freshness
    seen: list = []
    for sql in sqls:
        try:
            f = await freshness.for_sql(sql)
        except Exception:
            continue
        if f.stale:
            s = f.summary()
            if s not in seen:
                seen.append(s)
    return [f"Note: {s}" for s in seen[:3]]


async def render(spec: dict, now: datetime) -> tuple[str, list]:
    """(message, item results) for `now` on the business clock (business_now): its date
    is the report's "today". Never raises: a failing item reads 'could not be read'."""
    today = now.date()
    model = None
    results: list[ItemResult] = []
    for item in spec["items"]:
        try:
            if "measure" in item:
                if model is None:
                    from . import semanticmodel
                    model = await semanticmodel.load_model()
                res = await asyncio.wait_for(_read_measure(item, today, model), ITEM_TIMEOUT)
            else:
                res = await asyncio.wait_for(_read_sql(item, today), ITEM_TIMEOUT)
        except Exception as e:
            res = ItemResult(item["label"], False, f"{item['label']}: could not be read today.",
                             error=reason(e))
        results.append(res)
    notes = await _stale_notes([r.sql for r in results if r.ok and r.sql])
    return compose(spec["title"], now, results, notes), results


# ── who receives it ──────────────────────────────────────────────────────────

async def recipient_for_thread(thread_id) -> tuple[str, str]:
    """(deliver_to, label). Decided in code from the requesting thread, never by the
    model: the requester's own direct chat when it is a registered person or an
    allowlisted sender, else the owner ('owner' resolves to owner_number at send time)."""
    from . import memscope
    t = str(thread_id or "").strip()
    try:
        ident = await memscope.for_thread(t)
    except Exception:
        return "owner", "the owner"
    if ident.kind == "owner" or ident.origin not in ("whatsapp", "sender") or not _DM_RE.match(t):
        return "owner", "the owner"
    label = ident.label
    if ident.kind != "person" or not label or label.startswith("+"):
        label = f"the requester ({memscope.mask(ident.sender or t)})"
    return t, label[:LABEL_MAX]


def _target_phone(deliver_to) -> str:
    if deliver_to == "owner":
        return (get_settings().owner_number or "").strip()
    return deliver_to if isinstance(deliver_to, str) and _DM_RE.match(deliver_to) else ""


# ── propose (the tool's body) ────────────────────────────────────────────────

async def report_conflict(title_key: str, run_id=None) -> str | None:
    pool = await get_pool()
    pb = await pool.fetchval(
        "SELECT id FROM playbooks WHERE report IS NOT NULL AND status <> 'archived' "
        "AND report->>'title_key' = $1 ORDER BY id LIMIT 1", title_key)
    if pb:
        return (f"a daily report with this title already exists (playbook #{pb}); archive it "
                "on Playbooks first or use another title")
    a = await pool.fetchval(
        "SELECT id FROM actions WHERE kind = $1 AND status = 'proposed' "
        "AND payload->>'title_key' = $2 ORDER BY id DESC LIMIT 1", ACTION_KIND, title_key)
    if a:
        return f"already proposed (action #{a}), awaiting the owner's approval"
    live = await pool.fetchval(
        "SELECT count(*) FROM playbooks WHERE report IS NOT NULL AND status <> 'archived'") or 0
    pending = await pool.fetchval(
        "SELECT count(*) FROM actions WHERE kind = $1 AND status = 'proposed'", ACTION_KIND) or 0
    if live + pending >= MAX_REPORTS:
        return f"the daily report limit ({MAX_REPORTS}) is reached; archive one on Playbooks first"
    if run_id is not None:
        n = await pool.fetchval(
            "SELECT count(*) FROM actions WHERE kind = $1 AND task_run_id = $2",
            ACTION_KIND, run_id) or 0
        if n >= MAX_PER_RUN:
            return f"this run already proposed {MAX_PER_RUN} daily reports (the cap)"
    return None


def _utcnow() -> datetime:
    return datetime.now(dt_timezone.utc)


def business_zone() -> str:
    """The operator `timezone` setting (the business's zone), else UTC."""
    from .kpihistory import _tz
    return _tz()


def business_now() -> datetime:
    """Now on the business's clock. A report's "today" and its header always use it,
    whatever zone the send time was given in and wherever the reader is: a 21:00 IST
    report on an Asia/Kolkata install reports that IST day."""
    return _utcnow().astimezone(ZoneInfo(business_zone()))


async def propose(title, at, items, timezone="", *, thread_id, run_id) -> str:
    """Record a proposed create_scheduled_report action (approval-gated)."""
    spec, err = validate_spec(title, at, timezone, items)
    if err:
        return f"REFUSED: {err}"
    conflict = await report_conflict(spec["title_key"], run_id)
    if conflict:
        return f"REFUSED: {conflict}"
    message, results = await render(spec, business_now())
    bad = [r for r in results if not r.ok]
    if bad:
        return ("REFUSED: " + "; ".join(f"item '{r.label}': {r.error}" for r in bad)
                + ". Fix the item(s) and call again; nothing was proposed.")
    target, who = await recipient_for_thread(thread_id)
    when = f"{spec['at']} {spec['timezone']}"
    payload = {
        "_summary": f"Daily report: {spec['title']} at {when}, to {who} on WhatsApp",
        **spec, "deliver_to": target, "deliver_label": who, "origin": "chat",
        "thread_id": thread_id, "proposal_key": uuid.uuid4().hex,
        "preview": message[:PREVIEW_MAX],
    }
    async with REPORT_LOCK:
        conflict = await report_conflict(spec["title_key"], run_id)
        if conflict:
            return f"REFUSED: {conflict}"
        pool = await get_pool()
        aid = await pool.fetchval(
            "INSERT INTO actions (kind, payload, tier, status, task_run_id) "
            "VALUES ($1, $2::jsonb, 'draft', 'proposed', $3) RETURNING id",
            ACTION_KIND, json.dumps(payload, default=str), run_id)
    return (f"proposed daily report (action #{aid}): '{spec['title']}' every day at {when}, "
            f"sent to {who} on WhatsApp - awaiting owner approval on Approvals. Nothing is sent "
            f"until approved; then it is read and sent by fixed code with no AI run. "
            f"Preview with today's data:\n{message[:PREVIEW_MAX]}")


# ── approval executor ────────────────────────────────────────────────────────

def describe(spec: dict, who: str) -> str:
    parts = []
    for it in spec["items"]:
        if "measure" in it:
            parts.append(f"{it['label']} (measure {it['measure']}"
                         + (f", {it['range']}" if it.get("range") else "") + ")")
        else:
            parts.append(f"{it['label']} (table)")
    return (f"Daily report, no AI run: sends '{spec['title']}' to {who} on WhatsApp every day "
            f"at {spec['at']} {spec['timezone']}. Items: " + "; ".join(parts) + ".")[:1000]


async def create_from_proposal(payload) -> bool:
    """Body of the create_scheduled_report executor. The payload is stored JSON and
    untrusted: re-validate, decide everything in code, insert idempotently."""
    if not isinstance(payload, dict):
        log.warning("create_scheduled_report: payload is not an object")
        return False
    key = payload.get("proposal_key")
    if not isinstance(key, str) or not key.strip() or len(key) > KEY_MAX:
        log.warning("create_scheduled_report: missing or invalid proposal_key")
        return False
    spec, err = validate_spec(payload.get("title"), payload.get("at"), payload.get("timezone"),
                              payload.get("items"))
    if err:
        log.warning("create_scheduled_report refused: %s", err)
        return False
    target = payload.get("deliver_to")
    if target != "owner" and not (isinstance(target, str) and _DM_RE.match(target)):
        log.warning("create_scheduled_report refused: invalid recipient")
        return False
    who = _clean_text(payload.get("deliver_label"), LABEL_MAX) or (
        "the owner" if target == "owner" else "the requester")
    pool = await get_pool()
    if await pool.fetchval("SELECT id FROM playbooks WHERE report->>'proposal_key' = $1", key):
        return True                       # a retry of an already-created report
    live = await pool.fetchval(
        "SELECT count(*) FROM playbooks WHERE report IS NOT NULL AND status <> 'archived'") or 0
    if live >= MAX_REPORTS:
        log.warning("create_scheduled_report refused: report limit reached")
        return False
    report = {**spec, "deliver_to": target, "deliver_label": who, "proposal_key": key}
    name = f"Report: {spec['title']}"
    if await pool.fetchval("SELECT 1 FROM playbooks WHERE name = $1", name):
        name = f"{name} ({key[:6]})"
    pid = await pool.fetchval(
        """INSERT INTO playbooks (name, trigger, goal, tool_grants, autonomy_tier, escalation,
                                  status, proposed_by, approved_by, report)
           VALUES ($1, $2::jsonb, $3, ARRAY[]::text[], 'notify', NULL, 'active', 'human',
                   'human', $4::jsonb)
           ON CONFLICT (name) DO NOTHING RETURNING id""",
        name, json.dumps(trigger_for(spec)), describe(spec, who), json.dumps(report, default=str))
    if pid:
        log.info("scheduled report created as playbook %s", pid)
        return True
    return bool(await pool.fetchval(
        "SELECT 1 FROM playbooks WHERE report->>'proposal_key' = $1", key))


# ── the daily run (called by playbooks.run_playbook_task) ────────────────────

def report_of(pb):
    """The playbook row's report spec (decoded), or None for an agent playbook."""
    try:
        v = pb["report"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return {"_invalid": True}
    return v if v else None


async def is_report_playbook(playbook_id, pool=None) -> bool:
    """Worker: a report run spends no AI, so the daily AI budget never pauses it.
    `pool` is the caller's (the worker passes its own). Never raises."""
    try:
        pool = pool or await get_pool()
        return bool(await pool.fetchval(
            "SELECT report IS NOT NULL FROM playbooks WHERE id = $1", playbook_id))
    except Exception:
        return False


async def _record_send(pb, run_id, deliver_to, phone, message, sent) -> None:
    try:
        pool = await get_pool()
        await pool.execute(
            """INSERT INTO actions (playbook_id, task_run_id, kind, payload, tier, status, executed_at)
               VALUES ($1, $2, 'whatsapp_message', $3::jsonb, 'notify', $4,
                       CASE WHEN $4 = 'executed' THEN now() END)""",
            pb["id"], run_id,
            json.dumps({"recipient": "owner" if deliver_to == "owner" else "requester",
                        "phone": phone, "body": message, "report": pb["name"]}),
            "executed" if sent else "failed")
    except Exception as e:
        log.warning("report send not recorded (%s)", type(e).__name__)


async def _run(run_id, pb) -> tuple[str, str | None, str | None]:
    report = report_of(pb)
    if not isinstance(report, dict) or report.get("_invalid"):
        return "failed", None, "the report spec is missing or unreadable"
    spec, err = validate_spec(report.get("title"), report.get("at"), report.get("timezone"),
                              report.get("items"))
    if err:
        return "failed", None, f"the report spec is invalid: {err}"
    target = report.get("deliver_to")
    phone = _target_phone(target)
    if not phone:
        return "failed", None, ("no WhatsApp number to send to: set owner_number on Setup"
                                if target == "owner" else "the report's recipient is invalid")
    message, results = await render(spec, business_now())
    try:
        sent = bool(await whatsapp_send(phone, message))
    except Exception as e:
        log.warning("report %s: WhatsApp send raised %s", pb["id"], type(e).__name__)
        sent = False
    await _record_send(pb, run_id, target, phone, message, sent)
    bad = [r for r in results if not r.ok]
    who = report.get("deliver_label") or ("the owner" if target == "owner" else "the requester")
    summary = (f"{'Sent' if sent else 'Not sent'}: '{spec['title']}' to {who}, "
               f"{len(results) - len(bad)} of {len(results)} items read")[:500]
    if not sent:
        return "failed", summary, "the WhatsApp send failed (connector not configured or unreachable)"
    if bad:
        return "failed", summary, ("sent, but these items could not be read: "
                                   + "; ".join(f"{r.label} ({r.error})" for r in bad))[:1000]
    return "done", summary, None


async def whatsapp_send(phone: str, message: str) -> bool:
    from . import whatsapp
    return await whatsapp.send_message(phone, message)


async def run(task: dict, pb) -> None:
    """Run one report: read, format, send, record. Never raises into the worker."""
    run_id = task["id"]
    try:
        status, summary, error = await _run(run_id, pb)
    except Exception as e:
        log.exception("report playbook %s run %s failed", pb["id"], run_id)
        status, summary, error = "failed", None, f"{type(e).__name__}: {_short(e)}"
    try:
        pool = await get_pool()
        await pool.execute(
            "UPDATE task_runs SET status = $2, ended_at = now(), result_summary = $3, error = $4 "
            "WHERE id = $1", run_id, status, summary, error)
    except Exception as e:
        log.warning("report run %s status not recorded (%s)", run_id, type(e).__name__)
    log.info("report playbook %s run %s %s", pb["id"], run_id, status)
