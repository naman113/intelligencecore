"""Quick 008: the Runs control center API. Mounted with the ops key in main.py.

One feed over everything that runs: the agent's task runs (`task_runs`: chat,
WhatsApp, uploads, the inbox, playbooks) and the background work the loops
record in `job_runs` (jobruns.py). Read-only; `/api/runs` keeps its old shape
for the legacy console and Today.

Agent runs carry no kind of their own beyond user/playbook, so the kind and
trigger are derived here from what is stored: the thread id, the triggering
message and the playbook row. Labels are presentation, kept in one place so
both operator UIs could read them.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from . import jobruns
from .db import get_pool

router = APIRouter(tags=["runs"])

DOC_PREFIX = "[document received: "
TITLE_CHARS = 60

AGENT_KINDS = {
    "chat": "Chat", "whatsapp": "WhatsApp", "upload": "Upload", "inbox": "Inbox document",
    "playbook": "Playbook", "test": "Console test",
    "sync": "Data sync",  # M2 sync: Phase 14 task_runs kind='sync' (runner: Phase 15)
}
JOB_KINDS = {
    "watch_check": "Watch check", "watch_investigation": "Watch investigation",
    "dashboard_refresh": "Dashboard refresh", "playbook_dispatch": "Scheduled",
    "followup_sweep": "Overdue follow-ups", "playbook_review": "Weekly playbook review",
    "consolidation": "Nightly consolidation", "triage": "Triage",
    "db_poll": "Database poller", "inbox_intake": "Inbox intake",
    "document_extraction": "Document extraction", "artifact_retention": "File clean-up",
    "health": "Health alert", "loop": "Loop error", "forecast": "Forecast", "case": "Case",
}
FAMILIES = {
    "agent": ("chat", "whatsapp", "upload", "inbox", "test"),
    "playbooks": ("playbook", "playbook_dispatch", "followup_sweep", "playbook_review"),
    "watches": ("watch_check", "watch_investigation"),
    "dashboards": ("dashboard_refresh",),
    "triage": ("triage",),
    "intake": ("db_poll", "inbox_intake", "document_extraction", "sync"),
    "housekeeping": ("consolidation", "artifact_retention"),
    "system": ("health", "loop"),
    "forecasts": ("forecast",),
    "cases": ("case",),
}
# Phase 17 (CONS-05): table mode switches
JOB_KINDS["mode_switch"] = "Table mode switch"
FAMILIES["intake"] = FAMILIES["intake"] + ("mode_switch",)
FAMILY_OF = {k: fam for fam, kinds in FAMILIES.items() for k in kinds}
PLAYBOOK_LABELS = {
    "explore_data": "Data exploration",
    "daily_digest": "Daily digest",
    "followup_reminder": "Follow-up reminder",
}
SINCE = {"1h": timedelta(hours=1), "24h": timedelta(hours=24),
         "7d": timedelta(days=7), "30d": timedelta(days=30)}
STATUSES = ("running", "queued", "done", "failed")
REF_TYPES = ("watch", "playbook", "dashboard", "case", "forecast", "document", "loop", "health",
             "task_run")
MAX_LIMIT = 200
FAILED_WINDOW = timedelta(hours=24)

_KIND_RE = re.compile(r"^[a-z][a-z_]{0,39}$")
_REF_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,120}$")

# Derived agent kind (SQL), from the thread id and the triggering message.
AGENT_KIND_SQL = """CASE WHEN r.kind = 'playbook' THEN 'playbook'
  WHEN r.kind = 'sync' THEN 'sync'
  WHEN r.thread_id = 'inbox' THEN 'inbox'
  WHEN r.thread_id = 'dashboard' THEN 'upload'
  WHEN left(r.thread_id, 3) = 'ui:' AND left(m.content, 20) = '[document received: ' THEN 'upload'
  WHEN left(r.thread_id, 3) = 'ui:' THEN 'chat'
  WHEN left(r.thread_id, 4) = 'test' THEN 'test'
  ELSE 'whatsapp' END"""

AGENT_SQL = f"""SELECT r.id, r.thread_id, r.status, r.result_summary, r.error,
       r.created_at, r.started_at, r.ended_at, r.playbook_id, r.case_id,
       p.name AS playbook_name, p.trigger AS playbook_trigger,
       t.title AS thread_title, ch.subject AS chat_subject, pe.name AS person_name,
       left(m.content, 300) AS message, {AGENT_KIND_SQL} AS kind
  FROM task_runs r
  LEFT JOIN messages m ON m.id = r.message_id
  LEFT JOIN playbooks p ON p.id = r.playbook_id
  LEFT JOIN threads t ON t.thread_id = r.thread_id
  LEFT JOIN chats ch ON ch.jid = r.thread_id
  LEFT JOIN people pe ON pe.whatsapp = r.thread_id"""

JOB_SQL = """SELECT j.id, j.kind, j.name, j.trigger, j.status, j.summary, j.error, j.counts,
       j.ref_type, j.ref_id, j.task_run_id, j.trace_id, j.routine, j.repeats,
       j.duration_ms, j.started_at, j.ended_at, j.last_at,
       d.title AS dashboard_title, d.slug AS dashboard_slug
  FROM job_runs j
  LEFT JOIN dashboards d ON j.ref_type = 'dashboard' AND d.id::text = j.ref_id"""


# ── labels ────────────────────────────────────────────────────────────────

def humanize(name) -> str:
    s = " ".join(str(name or "").replace("_", " ").split())
    return s[:1].upper() + s[1:] if s else ""


def playbook_label(name) -> str:
    return PLAYBOOK_LABELS.get(str(name or ""), humanize(name) or "Playbook")


def kind_label(kind: str) -> str:
    return AGENT_KINDS.get(kind) or JOB_KINDS.get(kind) or humanize(kind)


def _clip(text, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _jsonish(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def doc_name(message) -> str | None:
    text = str(message or "")
    if text.startswith(DOC_PREFIX):
        return text[len(DOC_PREFIX):].split("]", 1)[0].strip() or None
    return None


def chat_title(row: dict, first_message=None) -> str:
    """The Ask chat's title, the way the chat list shows it."""
    if row.get("thread_title"):
        return row["thread_title"]
    parts = str(row.get("thread_id") or "").split(":")
    first = str(first_message or "").strip()
    if first.startswith(DOC_PREFIX):
        first = "Document: " + (doc_name(first) or "")
    first = _clip(first, TITLE_CHARS)
    if first:
        return first
    return "Main chat" if len(parts) == 2 else "New chat"


def playbook_trigger(trigger) -> str:
    t = _jsonish(trigger)
    if not isinstance(t, dict):
        return "playbook"
    if t.get("type") == "cron":
        try:  # Quick 047: minute and zone when the trigger has them
            tz = t.get("tz")
            return (f"schedule (daily {int(t.get('hour')):02d}:{int(t.get('minute') or 0):02d}"
                    + (f" {tz}" if isinstance(tz, str) and tz else "") + ")")
        except (TypeError, ValueError):
            return "schedule"
    if t.get("type") == "expectation_violated":
        return "overdue follow-up"
    return humanize(t.get("type")) or "playbook"


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _ms(start, end) -> int | None:
    if start is None or end is None:
        return None
    try:
        return max(0, int((end - start).total_seconds() * 1000))
    except Exception:  # noqa: BLE001
        return None


def agent_item(row: dict, first_messages: dict | None = None, step: str | None = None) -> dict:
    kind = row.get("kind") or "whatsapp"
    tid = row.get("thread_id") or ""
    trigger = {"chat": "chat", "whatsapp": "WhatsApp", "upload": "upload", "inbox": "inbox",
               "test": "console"}.get(kind, "")
    chat = None
    if kind == "playbook":
        title = f"{playbook_label(row.get('playbook_name'))} (playbook)"
        trigger = playbook_trigger(row.get("playbook_trigger"))
    elif kind == "chat":
        chat = tid
        title = f"Chat: {chat_title(row, (first_messages or {}).get(tid))}"
    elif kind == "upload":
        chat = tid if tid.startswith("ui:") else None
        title = f"Upload: {doc_name(row.get('message')) or 'document'}"
    elif kind == "inbox":
        title = f"Inbox: {doc_name(row.get('message')) or 'document'}"
    elif kind == "test":
        title = "Console test"
    elif kind == "sync":
        title = "Data sync"
    else:
        who = row.get("chat_subject") or row.get("person_name") or tid
        title = f"WhatsApp: {who}"
    question = None
    if kind in ("chat", "whatsapp", "test", "upload") and row.get("message"):
        question = _clip(row["message"], 160)
    at = row.get("created_at")
    return {
        "uid": f"run-{row['id']}", "source": "agent", "id": row["id"],
        "kind": kind, "kind_label": kind_label(kind), "family": FAMILY_OF.get(kind, "agent"),
        "title": title, "trigger": trigger, "status": row.get("status"),
        "summary": row.get("result_summary"), "error": row.get("error"),
        "question": question,
        "at": _iso(at), "created_at": _iso(at), "started_at": _iso(row.get("started_at")),
        "ended_at": _iso(row.get("ended_at")), "last_at": _iso(row.get("ended_at") or at),
        "duration_ms": _ms(row.get("started_at"), row.get("ended_at")),
        "repeats": 1, "counts": {}, "step": step, "skills_used": [],
        "thread_id": tid, "chat": chat, "case_id": row.get("case_id"),
        "playbook_id": row.get("playbook_id"), "playbook_name": row.get("playbook_name"),
        "task_run_id": None, "trace_id": f"run:{row['id']}",
        "ref_type": "playbook" if row.get("playbook_id") else None,
        "ref_id": str(row["playbook_id"]) if row.get("playbook_id") else None,
    }


def job_title(kind: str, name: str, row: dict | None = None) -> str:
    row = row or {}
    if kind == "dashboard_refresh":
        name = row.get("dashboard_title") or name or (
            f"dashboard {row.get('ref_id')}" if row.get("ref_id") else "")
    if kind == "playbook_dispatch":
        return f"Scheduled: {playbook_label(name)}"
    label = kind_label(kind)
    return f"{label}: {name}" if name else label


def job_item(row: dict) -> dict:
    kind = row.get("kind") or ""
    counts = _jsonish(row.get("counts")) or {}
    at = row.get("started_at")
    return {
        "uid": f"job-{row['id']}", "source": "job", "id": row["id"],
        "kind": kind, "kind_label": kind_label(kind), "family": FAMILY_OF.get(kind, "other"),
        "title": job_title(kind, row.get("name") or "", row),
        "name": row.get("name") or "", "trigger": row.get("trigger") or "",
        "status": row.get("status"), "summary": row.get("summary"), "error": row.get("error"),
        "question": None,
        "at": _iso(at), "created_at": _iso(at), "started_at": _iso(at),
        "ended_at": _iso(row.get("ended_at")), "last_at": _iso(row.get("last_at")),
        "duration_ms": row.get("duration_ms"), "repeats": row.get("repeats") or 1,
        "routine": bool(row.get("routine")),
        "counts": counts if isinstance(counts, dict) else {}, "step": None,
        "skills_used": [], "thread_id": None, "chat": None,
        "case_id": int(row["ref_id"]) if row.get("ref_type") == "case"
        and str(row.get("ref_id") or "").isdigit() else None,
        "playbook_id": None, "playbook_name": None,
        "task_run_id": row.get("task_run_id"), "trace_id": row.get("trace_id"),
        "ref_type": row.get("ref_type") or None, "ref_id": row.get("ref_id") or None,
        "dashboard_slug": row.get("dashboard_slug"),
    }


def live_item(view: dict) -> dict:
    """An in-process job with no row yet (a watch query or refresh batch in flight)."""
    kind = view.get("kind") or ""
    return {
        "uid": f"live-{view.get('token')}", "source": "job", "id": None,
        "kind": kind, "kind_label": kind_label(kind), "family": FAMILY_OF.get(kind, "other"),
        "title": job_title(kind, view.get("name") or "", view),
        "name": view.get("name") or "", "trigger": view.get("trigger") or "",
        "status": "running", "summary": None, "error": None, "question": None,
        "at": view.get("started_at"), "created_at": view.get("started_at"),
        "started_at": view.get("started_at"), "ended_at": None,
        "last_at": view.get("started_at"), "duration_ms": None, "repeats": 1, "counts": {},
        "step": None, "skills_used": [], "thread_id": None, "chat": None, "case_id": None,
        "playbook_id": None, "playbook_name": None, "task_run_id": None, "trace_id": None,
        "ref_type": view.get("ref_type"), "ref_id": view.get("ref_id"),
    }


# ── filters ───────────────────────────────────────────────────────────────

def parse_kinds(kind: str | None) -> list[str] | None:
    """None = every kind. A family name expands; otherwise comma-separated kinds."""
    if not kind:
        return None
    out: list[str] = []
    for part in kind.split(","):
        part = part.strip()
        if not part:
            continue
        if part in FAMILIES:
            out.extend(FAMILIES[part])
        elif _KIND_RE.match(part):
            out.append(part)
        else:
            raise HTTPException(422, f"unknown kind {part!r}")
    return out or None


def parse_ref(ref: str | None) -> tuple[str, str] | None:
    if not ref:
        return None
    rtype, _, rid = ref.partition(":")
    if rtype not in REF_TYPES or not _REF_ID_RE.match(rid or ""):
        raise HTTPException(422, "ref must look like watch:12, playbook:3, dashboard:4 or case:9")
    return rtype, rid


def parse_before(before: str | None) -> datetime | None:
    if not before:
        return None
    try:
        dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "before must be an ISO time")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def agent_where(kinds, status, since, ref, before) -> tuple[str, list] | None:
    """WHERE clause for task_runs, or None when no agent run can match."""
    conds, args = [], []
    if kinds is not None:
        mine = [k for k in kinds if k in AGENT_KINDS]
        if not mine:
            return None
        args.append(mine)
        conds.append(f"({AGENT_KIND_SQL}) = ANY(${len(args)}::text[])")
    if status:
        args.append(status)
        conds.append(f"r.status = ${len(args)}")
    if since is not None:
        args.append(since)
        conds.append(f"r.created_at >= ${len(args)}")
    if ref is not None:
        rtype, rid = ref
        if rtype == "playbook":
            args.append(rid)
            conds.append(f"(r.playbook_id::text = ${len(args)} OR p.name = ${len(args)})")
        elif rtype == "case" and rid.isdigit():
            args.append(int(rid))
            conds.append(f"r.case_id = ${len(args)}")
        elif rtype == "task_run" and rid.isdigit():
            args.append(int(rid))
            conds.append(f"r.id = ${len(args)}")
        else:
            return None
    if before is not None:
        args.append(before)
        conds.append(f"r.created_at < ${len(args)}")
    return (" WHERE " + " AND ".join(conds)) if conds else "", args


def job_where(kinds, status, since, ref, before, playbook_ids=()) -> tuple[str, list] | None:
    conds, args = [], []
    if kinds is not None:
        mine = [k for k in kinds if k not in AGENT_KINDS]
        if not mine:
            return None
        args.append(mine)
        conds.append(f"j.kind = ANY(${len(args)}::text[])")
    if status:
        if status == "queued":
            return None
        args.append(status)
        conds.append(f"j.status = ${len(args)}")
    if since is not None:
        args.append(since)
        conds.append(f"j.started_at >= ${len(args)}")
    if ref is not None:
        rtype, rid = ref
        ids = [rid] + [str(i) for i in playbook_ids if str(i) != rid]
        args.append(rtype)
        args.append(ids)
        conds.append(f"j.ref_type = ${len(args) - 1} AND j.ref_id = ANY(${len(args)}::text[])")
    if before is not None:
        args.append(before)
        conds.append(f"j.started_at < ${len(args)}")
    return (" WHERE " + " AND ".join(conds)) if conds else "", args


def _sort_key(item: dict):
    return (item.get("at") or "", item.get("uid") or "")


def merge(agent: list[dict], jobs: list[dict], limit: int) -> tuple[list[dict], str | None]:
    """Newest first across both sources; next_before when more may exist."""
    items = sorted(agent + jobs, key=_sort_key, reverse=True)
    more = len(items) > limit or len(agent) >= limit or len(jobs) >= limit
    page = items[:limit]
    nxt = page[-1]["at"] if (more and page) else None
    return page, nxt


async def _first_messages(pool, rows: list[dict]) -> dict:
    ids = sorted({r["thread_id"] for r in rows
                  if r.get("kind") == "chat" and not r.get("thread_title")})
    if not ids:
        return {}
    got = await pool.fetch(
        """SELECT DISTINCT ON (thread_id) thread_id, content FROM messages
            WHERE thread_id = ANY($1::text[]) AND role = 'user'
            ORDER BY thread_id, id""", ids)
    return {g["thread_id"]: g["content"] for g in got}


PLAYBOOK_FIRST_STEP = "Starting the playbook"
SYNC_FIRST_STEP = "Syncing data"  # M2 sync: Phase 14 kind='sync' runs


def _step(run_id: int, status: str, kind: str | None = None) -> str | None:
    """The live step of a running agent run (Quick 003 runstatus). Its first
    step is worded for a question, so a playbook run says what it is doing."""
    if status != "running":
        return None
    try:
        from . import runstatus
        step = runstatus.step(run_id) or runstatus.FIRST_STEP
        if kind == "playbook" and step == runstatus.FIRST_STEP:
            return PLAYBOOK_FIRST_STEP
        if kind == "sync" and step == runstatus.FIRST_STEP:
            return SYNC_FIRST_STEP
        return step
    except Exception:  # noqa: BLE001
        return None


async def _skills_used(pool, rows: list[dict]) -> dict:
    """Skills each run loaded: Quick 010's skillservice.skills_used (use_skill
    tool calls, first-use order), the same field the /api/runs list carries.
    Fail-soft: a lookup failure leaves the lists empty, never breaks the feed."""
    try:
        from . import skillservice
        return await skillservice.skills_used(pool, [r["id"] for r in rows])
    except Exception:  # noqa: BLE001
        return {}


async def _agent_items(pool, rows) -> list[dict]:
    rows = [dict(r) for r in rows]
    firsts = await _first_messages(pool, rows)
    skills = await _skills_used(pool, rows)
    items = []
    for r in rows:
        item = agent_item(r, firsts, _step(r["id"], r.get("status"), r.get("kind")))
        item["skills_used"] = skills.get(r["id"], [])
        items.append(item)
    return items


async def _playbook_ids(pool, ref) -> list:
    """`playbook:explore_data` also matches job rows recorded under its id."""
    if ref is None or ref[0] != "playbook" or ref[1].isdigit():
        return []
    rows = await pool.fetch("SELECT id FROM playbooks WHERE name = $1", ref[1])
    return [r["id"] for r in rows]


# ── routes ────────────────────────────────────────────────────────────────

@router.get("/api/activity")
async def activity(kind: str | None = None, status: str | None = None,
                   since: str | None = None, ref: str | None = None,
                   before: str | None = None, limit: int = 50):
    kinds = parse_kinds(kind)
    if status and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    if since and since not in SINCE:
        raise HTTPException(422, f"since must be one of {', '.join(SINCE)}")
    since_at = datetime.now(timezone.utc) - SINCE[since] if since else None
    reff = parse_ref(ref)
    before_at = parse_before(before)
    limit = max(1, min(int(limit), MAX_LIMIT))
    pool = await get_pool()

    agent: list[dict] = []
    aw = agent_where(kinds, status, since_at, reff, before_at)
    if aw is not None:
        where, args = aw
        args.append(limit)
        rows = await pool.fetch(
            f"{AGENT_SQL}{where} ORDER BY r.created_at DESC, r.id DESC LIMIT ${len(args)}", *args)
        agent = await _agent_items(pool, rows)

    jobs: list[dict] = []
    jw = job_where(kinds, status, since_at, reff, before_at, await _playbook_ids(pool, reff))
    if jw is not None:
        where, args = jw
        args.append(limit)
        rows = await pool.fetch(
            f"{JOB_SQL}{where} ORDER BY j.started_at DESC, j.id DESC LIMIT ${len(args)}", *args)
        jobs = [job_item(dict(r)) for r in rows]

    items, nxt = merge(agent, jobs, limit)
    return {"items": items, "next_before": nxt}


@router.get("/api/activity/now")
async def activity_now():
    """What is running right now, what failed recently, and the counts."""
    pool = await get_pool()
    since = datetime.now(timezone.utc) - FAILED_WINDOW
    agent_rows = await pool.fetch(
        f"{AGENT_SQL} WHERE r.status IN ('running','queued') "
        "ORDER BY (r.status = 'running') DESC, r.id LIMIT 50")
    agent = await _agent_items(pool, agent_rows)
    job_rows = await pool.fetch(
        f"{JOB_SQL} WHERE j.status = 'running' ORDER BY j.started_at DESC LIMIT 50")
    jobs = [job_item(dict(r)) for r in job_rows]
    seen = {j["id"] for j in jobs}
    jobs += [live_item(v) for v in jobruns.active()
             if v.get("id") is None or v.get("id") not in seen]

    failed_agent = await _agent_items(pool, await pool.fetch(
        f"{AGENT_SQL} WHERE r.status = 'failed' AND r.created_at >= $1 "
        "ORDER BY r.id DESC LIMIT 10", since))
    failed_jobs = [job_item(dict(r)) for r in await pool.fetch(
        f"{JOB_SQL} WHERE j.status = 'failed' AND j.last_at >= $1 "
        "ORDER BY j.last_at DESC LIMIT 10", since)]
    for f in failed_jobs:
        f["at"] = f["last_at"] or f["at"]
    failed = sorted(failed_agent + failed_jobs, key=_sort_key, reverse=True)[:10]

    c = await pool.fetchrow(
        """SELECT (SELECT count(*) FROM task_runs WHERE status = 'queued') AS queued,
                  (SELECT count(*) FROM task_runs WHERE status = 'failed'
                     AND created_at >= $1) AS agent_failed,
                  (SELECT count(*) FROM task_runs WHERE status = 'done'
                     AND created_at >= $1) AS agent_done,
                  (SELECT count(*) FROM job_runs WHERE status = 'failed'
                     AND last_at >= $1) AS job_failed,
                  (SELECT count(*) FROM job_runs WHERE status = 'done'
                     AND last_at >= $1) AS job_done""", since)
    running = [a for a in agent if a["status"] == "running"] + jobs
    queued = [a for a in agent if a["status"] == "queued"]
    return {
        "running": sorted(running, key=_sort_key, reverse=True),
        "queued": queued,
        "failed": failed,
        "counts": {
            "running": len(running),
            "queued": int(c["queued"] or 0),
            "failed_24h": int((c["agent_failed"] or 0) + (c["job_failed"] or 0)),
            "done_24h": int((c["agent_done"] or 0) + (c["job_done"] or 0)),
        },
    }


async def _ref_link(pool, item: dict) -> dict | None:
    """Where the linked object lives in the UI, with its current name."""
    rtype, rid = item.get("ref_type"), item.get("ref_id")
    if not rtype or not rid:
        return None
    if rtype == "watch" and rid.isdigit():
        w = await pool.fetchrow("SELECT id, kind, title FROM watches WHERE id = $1", int(rid))
        if w is None:
            return {"type": "watch", "id": rid, "label": f"Watch #{rid} (deleted)", "to": None}
        to = f"/kpis/{w['id']}" if w["kind"] == "kpi" else "/watches"
        return {"type": "watch", "id": rid, "label": w["title"], "to": to}
    if rtype == "dashboard":
        slug = item.get("dashboard_slug")
        return {"type": "dashboard", "id": rid, "label": item.get("title"),
                "to": f"/dashboards/{slug}" if slug else "/dashboards"}
    if rtype == "playbook":
        return {"type": "playbook", "id": rid,
                "label": playbook_label(item.get("playbook_name") or item.get("name")),
                "to": "/playbooks"}
    if rtype == "case" and rid.isdigit():
        return {"type": "case", "id": rid, "label": f"Case #{rid}", "to": f"/cases/{rid}"}
    if rtype == "document" and rid.isdigit():
        return {"type": "document", "id": rid, "label": f"Document #{rid}",
                "to": f"/documents/{rid}"}
    if rtype == "task_run" and rid.isdigit():
        return {"type": "task_run", "id": rid, "label": f"Run #{rid}",
                "to": f"/system/runs/{rid}"}
    if rtype == "forecast" and rid.isdigit():  # Quick 013: the nightly forecast step
        from . import forecasts  # the one reader of forecast_defs
        f = await forecasts.name_status(int(rid))
        if f is None:
            return {"type": "forecast", "id": rid, "label": f"Forecast #{rid} (deleted)",
                    "to": None}
        label = f["name"] + (" (retired)" if f["status"] == "retired" else "")
        return {"type": "forecast", "id": rid, "label": label, "to": f"/forecasts/{rid}"}
    return {"type": rtype, "id": rid, "label": f"{humanize(rtype)} {rid}", "to": None}


@router.get("/api/activity/runs/{run_id}")
async def activity_run(run_id: int):
    pool = await get_pool()
    row = await pool.fetchrow(f"{AGENT_SQL} WHERE r.id = $1", run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    item = (await _agent_items(pool, [row]))[0]
    item["message"] = row["message"]
    item["ref"] = await _ref_link(pool, item)
    return item


@router.get("/api/activity/jobs/{job_id}")
async def activity_job(job_id: int):
    pool = await get_pool()
    row = await pool.fetchrow(f"{JOB_SQL} WHERE j.id = $1", job_id)
    if row is None:
        raise HTTPException(404, "background run not found")
    item = job_item(dict(row))
    item["ref"] = await _ref_link(pool, item)
    return item


# ── Quick 019: the memory a run used, and what it stored ─────────────────

STORED_LIMIT = 20


def memory_view(run_id: int, used, statuses: dict, stored: list[dict]) -> dict:
    """GET /api/runs/{id}/memory body. `used` is task_runs.memory_used (written
    by the worker from contextassembler.assemble_with_provenance; None for a
    run that never assembled memory); `statuses` maps learning id to its row
    now ({"status", "status_reason"}), so a learning superseded or deleted
    since the run reads that way; `stored` are the learnings whose source_run
    is this run. Pure."""
    used = _jsonish(used)
    out = {"run_id": run_id, "recorded": isinstance(used, dict) and bool(used),
           "used": None, "stored": []}
    if out["recorded"]:
        view = {"lessons": [], "learnings": [], "graph": [], "cases": [],
                "group": {"purpose": False, "members": False},
                "chars": 0, "budget_chars": 0, "dropped": 0}
        view.update({k: v for k, v in used.items() if k in view})
        for key in ("lessons", "learnings"):
            items = []
            for it in view.get(key) or []:
                if not isinstance(it, dict):
                    continue
                now = statuses.get(it.get("id")) if it.get("id") is not None else None
                items.append({**it,
                              "status": (now or {}).get("status") or
                                        ("deleted" if it.get("id") is not None else None),
                              "status_reason": (now or {}).get("status_reason")})
            view[key] = items
        out["used"] = view
    for r in stored:
        out["stored"].append({
            "id": r["id"], "kind": r["kind"], "content": r["content"],
            "source": r.get("source"),
            "outcome": "updated" if (r.get("revisions") or 0) > 0 else "new",
            "status": r.get("status") or "active", "status_reason": r.get("status_reason"),
            "updated_at": _iso(r.get("updated_at")),
        })
    return out


@router.get("/api/runs/{run_id}/memory")
async def run_memory(run_id: int):
    """Quick 019: which lessons, learnings, knowledge-graph items, open cases
    and group context fed this run's memory message, and the learnings the
    run stored afterwards (reflection, remember_fact; a merge moves a
    learning's source_run to the latest run that rewrote it)."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id, memory_used FROM task_runs WHERE id = $1", run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    used = _jsonish(row["memory_used"])
    ids = []
    if isinstance(used, dict):
        for key in ("lessons", "learnings"):
            for it in used.get(key) or []:
                if isinstance(it, dict) and isinstance(it.get("id"), int):
                    ids.append(it["id"])
    statuses = {}
    if ids:
        statuses = {r["id"]: {"status": r["status"], "status_reason": r["status_reason"]}
                    for r in await pool.fetch(
                        "SELECT id, status, status_reason FROM learnings "
                        "WHERE id = ANY($1::bigint[])", ids)}
    stored = [dict(r) for r in await pool.fetch(
        """SELECT id, kind, content, source, revisions, status, status_reason, updated_at
             FROM learnings WHERE source_run = $1 ORDER BY id LIMIT $2""",
        run_id, STORED_LIMIT)]
    return memory_view(run_id, used, statuses, stored)
