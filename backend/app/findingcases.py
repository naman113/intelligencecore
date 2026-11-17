"""Quick 006: cases opened from data findings. The one writer of finding cases.

Triage turns messages from people into cases. This module does the same for what
the system finds in the data:

  * a critical KPI alert (sentinel._fire)             origin 'watch'
  * a critical exploration insight (insights)         origin 'insight'
  * the owner's "open a case for this" (casetools)    origin 'chat'

Plumbing only: nothing here calls an LLM, and the words the agent reads live in
the open_case / record_insight tool text and the skills.

Watch case lifecycle
  * A critical fire finds or opens the ONE open case for that watch (origin_ref
    'watch:<id>', unique while open), attaches the fire as a 'watch' note,
    records the watched value as a case fact, resets the recovery streak and
    clears seen_at (the owner's gold card is back).
  * The escalation's findings are attached as an 'investigation' note.
  * Every later successful check counts towards watches.normal_streak while the
    threshold is not crossed; a crossed check resets it. At
    watch_case_recovery_checks the case closes ("resolved: back to normal").
  * A paused, finished or deleted watch closes its case: the watch routes call
    close_for_watch at once, and sweep_watch_cases in the sentinel loop catches
    every other path.

Every close goes through triage._close_case (the one close path: notify,
distill), after write_summary() gives the case a summary built from its own
notes. Finding cases never enter the triage prompt (triage._open_cases_index),
never chain, and watch cases are not swept as stale.
"""

import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import appchannel, cases_util
from .config import get_settings
from .db import get_pool

log = logging.getLogger("findingcases")

FINDING_ORIGINS = ("watch", "insight", "chat")
OPEN_STATUSES = ("open", "pending_ack")
# Only a critical fire opens a case: it is the one severity with an abnormal and a
# normal state (a threshold crossed or not), which auto-close needs. Warning fires
# come from increase/change rules: transitions, not states.
CASE_SEVERITY = "critical"
RESOLVED_REASON = "resolved: back to normal"
STOPPED_REASONS = {
    "paused": "closed: the watch was paused",
    "done": "closed: the watch was finished",
    "deleted": "closed: the watch was deleted",
}
REF_CAP = 200
TITLE_CAP = 200
SUMMARY_CAP = 2000
VALUES_CAP = 1500
MIN_RECOVERY, MAX_RECOVERY, DEFAULT_RECOVERY = 1, 50, 3


# ── small helpers ──────────────────────────────────────────────────────────

def norm(s) -> str:
    """Same normalisation as insights.norm_title (not imported: insights imports us)."""
    return re.sub(r"[\W_]+", " ", str(s or "").lower()).strip()


def watch_ref(watch_id) -> str:
    return f"watch:{int(watch_id)}"


def insight_ref(title) -> str:
    return ("insight:" + norm(title))[:REF_CAP]


def chat_ref(title) -> str:
    return ("chat:" + norm(title))[:REF_CAP]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _obj(v) -> dict:
    """asyncpg returns jsonb as str: decode at the edge."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _excerpt(text, cap: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def _when(ts) -> str:
    if not isinstance(ts, datetime):
        return str(ts or "")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        ts = ts.astimezone(ZoneInfo(get_settings().timezone or "UTC"))
    except Exception:
        pass
    return ts.strftime("%d %b %H:%M")


def recovery_checks() -> int:
    try:
        n = int(getattr(get_settings(), "watch_case_recovery_checks", DEFAULT_RECOVERY))
    except (TypeError, ValueError):
        n = DEFAULT_RECOVERY
    return max(MIN_RECOVERY, min(MAX_RECOVERY, n))


def in_alert(trigger, current) -> bool | None:
    """True when a threshold rule's value is crossed, False when it is not, None when
    this check cannot say (not a threshold rule, no value, unknown operator)."""
    if not isinstance(trigger, dict) or trigger.get("type") != "threshold":
        return None
    col = trigger.get("column")
    if not isinstance(current, dict) or col not in current:
        return None
    n, thr = _num(current.get(col)), _num(trigger.get("value"))
    if n is None or thr is None:
        return None
    return {">": n > thr, "<": n < thr, ">=": n >= thr, "<=": n <= thr}.get(
        trigger.get("op", ">"))


def _values(current) -> dict:
    out = json.loads(json.dumps(current or {}, default=str))
    while out and len(json.dumps(out)) > VALUES_CAP:
        out.pop(next(reversed(out)))
    return out


async def _record_run(case_id, title, summary: str, trigger: str) -> None:
    """Quick 008: a finding case opening or closing shows on System > Runs.
    jobruns never raises, so this cannot break the case lifecycle."""
    from . import jobruns
    await jobruns.record("case", title or f"Case #{case_id}", summary=summary,
                         trigger=trigger, ref=("case", case_id))


async def is_finding_case(pool, case_id: int) -> bool:
    try:
        return (await pool.fetchval("SELECT origin FROM cases WHERE id = $1", case_id)
                in FINDING_ORIGINS)
    except Exception:
        return False


# ── summaries (deterministic, from the case's own notes) ───────────────────

async def write_summary(conn, case_id: int, ending: str | None = None) -> str:
    """Rebuild a watch case's summary from its fire and investigation notes (plus an
    ending line on close). notify_case_closed and distill_case read it; finding cases
    get no rolling LLM summary on an install without message traffic."""
    origin = await conn.fetchval("SELECT origin FROM cases WHERE id = $1", case_id)
    if origin != "watch":
        if ending:
            await conn.execute(
                """UPDATE cases SET summary = left(concat_ws(' ', summary, $2::text), $3)
                    WHERE id = $1""", case_id, ending, SUMMARY_CAP)
        return ""
    rows = await conn.fetch(
        """SELECT ce.note AS kind, e.content, e.meta, e.occurred_at
             FROM case_events ce JOIN events e ON e.id = ce.event_id
            WHERE ce.case_id = $1 AND ce.note IN ('watch', 'investigation')
            ORDER BY e.occurred_at, e.id""", case_id)
    fires = [r for r in rows if r["kind"] == "watch"]
    inv = [r for r in rows if r["kind"] == "investigation"]
    parts: list[str] = []
    if fires:
        latest = _obj(fires[-1]["meta"]).get("summary") or _excerpt(fires[-1]["content"], 200)
        if len(fires) == 1:
            parts.append(f"Fired once, {_when(fires[0]['occurred_at'])}: {latest}")
        else:
            parts.append(f"Fired {len(fires)} times between {_when(fires[0]['occurred_at'])} "
                         f"and {_when(fires[-1]['occurred_at'])}. Latest: {latest}")
    if inv:
        found = _obj(inv[-1]["meta"]).get("finding") or inv[-1]["content"]
        parts.append("Investigation: " + _excerpt(found, 400))
    if ending:
        parts.append(ending)
    summary = " ".join(parts)[:SUMMARY_CAP]
    if summary:
        await conn.execute("UPDATE cases SET summary = $2 WHERE id = $1", case_id, summary)
    return summary


# ── watch cases ────────────────────────────────────────────────────────────

async def on_watch_fire(pool, watch: dict, *, severity: str, summary: str,
                        current: dict, trigger: dict,
                        watch_event_id: int | None = None) -> tuple[int | None, bool]:
    """Attach a critical fire to the watch's open case, opening one if needed.
    Returns (case id, opened now); (None, False) for a severity that opens no case."""
    if severity != CASE_SEVERITY:
        return None, False
    wid = int(watch["id"])
    ref = watch_ref(wid)
    trigger = trigger if isinstance(trigger, dict) else {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            # One open case per watch: the lock serialises two fires of the same
            # watch; the partial unique index on origin_ref is the backstop.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", ref)
            case_id = await conn.fetchval(
                """SELECT id FROM cases WHERE origin_ref = $1
                      AND status = ANY($2::text[]) ORDER BY id DESC LIMIT 1""",
                ref, list(OPEN_STATUSES))
            opened = case_id is None
            if opened:
                links = {"watch_id": wid, "watch_title": watch.get("title"),
                         "watch_kind": watch.get("kind"), "thread_id": watch.get("thread_id")}
                case_id = await conn.fetchval(
                    """INSERT INTO cases (title, opened_reason, origin, origin_ref, watch_id,
                                          links, origin_chat_jid)
                       VALUES ($1, $2, 'watch', $3, $4, $5::jsonb, $6) RETURNING id""",
                    f"KPI alert: {watch.get('title') or f'watch #{wid}'}"[:TITLE_CAP],
                    f"critical alert from watch #{wid}", ref, wid, json.dumps(links),
                    watch.get("thread_id"))
            meta = {"origin": "watch", "watch_id": wid, "watch_event_id": watch_event_id,
                    "severity": severity, "summary": summary, "values": _values(current)}
            eid = await cases_util.add_case_note(
                conn, case_id, f"Alert fired ({severity}): {summary}", kind="watch", meta=meta)
            col = trigger.get("column")
            if col and isinstance(current, dict) and current.get(col) is not None:
                # str(): record_case_fact drops falsy values, and 0 is a real reading.
                await cases_util.record_case_fact(conn, case_id, col, str(current[col]),
                                                  source_event_id=eid)
            if opened and trigger.get("type") == "threshold" and col:
                await cases_util.record_case_fact(
                    conn, case_id, "alert_rule",
                    f"{col} {trigger.get('op', '>')} {trigger.get('value')}", source_event_id=eid)
            # A new fire is new work for the owner: the gold card comes back.
            await conn.execute("UPDATE cases SET seen_at = NULL WHERE id = $1", case_id)
            await conn.execute("UPDATE watches SET normal_streak = 0 WHERE id = $1", wid)
            if watch_event_id:
                await conn.execute("UPDATE watch_events SET case_id = $2 WHERE id = $1",
                                   watch_event_id, case_id)
            await write_summary(conn, case_id)
    log.info("watch %s fire %s case %s", wid, "opened" if opened else "attached to", case_id)
    if opened:  # Quick 008
        await _record_run(case_id, f"KPI alert: {watch.get('title') or f'watch #{wid}'}",
                          f"Opened from a critical alert: {summary}", "critical alert")
    return case_id, opened


async def record_investigation(pool, case_id: int, watch: dict, findings: str) -> int | None:
    """The escalation's findings, as an 'investigation' note on the alert's case."""
    text = (findings or "").strip()
    if not case_id or not text:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            eid = await cases_util.add_case_note(
                conn, case_id, f"Investigation of the alert '{watch.get('title')}':\n\n{text}",
                kind="investigation", cap=8000,
                meta={"origin": "watch_escalation", "watch_id": watch.get("id"),
                      "title": f"Investigation: {watch.get('title')}"[:160],
                      "finding": text[:2000]})
            await write_summary(conn, case_id)
    return eid


async def after_check(pool, watch: dict, trigger, current: dict, fired: bool) -> bool:
    """Count one successful check towards closing the watch's open case. Returns True
    when this check closed it."""
    wid = int(watch["id"])
    case_id = await pool.fetchval(
        "SELECT id FROM cases WHERE origin_ref = $1 AND status = 'open' LIMIT 1",
        watch_ref(wid))
    if not case_id:
        return False
    trigger = trigger if isinstance(trigger, dict) else {}
    state = in_alert(trigger, current)
    if state is None:
        if trigger.get("type") == "threshold":
            return False          # this check cannot say either way
        state = bool(fired)       # a recompiled non-threshold rule: a fire is abnormal
    if state:
        await pool.execute(
            "UPDATE watches SET normal_streak = 0 WHERE id = $1 AND normal_streak <> 0", wid)
        return False
    streak = await pool.fetchval(
        "UPDATE watches SET normal_streak = normal_streak + 1 WHERE id = $1 RETURNING normal_streak",
        wid)
    if streak is None or streak < recovery_checks():
        return False
    col = trigger.get("column")
    val = current.get(col) if col else None
    shown = f"{col} {val}" if col else "the watched value"
    return await close_case(
        case_id, RESOLVED_REASON, kind="resolved", pool=pool,
        note=f"Back to normal: {shown} after {streak} normal checks in a row.",
        ending=f"Back to normal ({shown}) after {streak} normal checks in a row; closed.",
        fact=(col, val))


async def close_for_watch(watch_id: int, why: str, pool=None) -> int:
    """Close the open case of a watch that was paused, finished or deleted."""
    pool = pool or await get_pool()
    rows = await pool.fetch(
        """SELECT id FROM cases WHERE origin = 'watch' AND status = ANY($3::text[])
              AND (watch_id = $1 OR origin_ref = $2)""",
        int(watch_id), watch_ref(watch_id), list(OPEN_STATUSES))
    reason = STOPPED_REASONS.get(why, f"closed: the watch is {why}")
    n = 0
    for r in rows:
        if await close_case(r["id"], reason, kind="watch_off", pool=pool,
                            note=f"The watch was {('deleted' if why == 'deleted' else why)}, "
                                 "so nothing checks this any more.",
                            ending=reason[0].upper() + reason[1:] + "."):
            n += 1
    return n


async def sweep_watch_cases(pool=None) -> int:
    """Sentinel-loop sweep: close open watch cases whose watch is gone, paused or
    finished, whichever path got it there (route, compile failures, one-shot 'no',
    a direct delete)."""
    pool = pool or await get_pool()
    rows = await pool.fetch(
        """SELECT c.id, c.watch_id, w.id AS live_id, w.status AS watch_status
             FROM cases c LEFT JOIN watches w ON w.id = c.watch_id
            WHERE c.origin = 'watch' AND c.status = ANY($1::text[])
              AND (w.id IS NULL OR w.status IN ('paused', 'done'))
            LIMIT 20""", list(OPEN_STATUSES))
    n = 0
    for r in rows:
        why = "deleted" if r["live_id"] is None else r["watch_status"]
        reason = STOPPED_REASONS.get(why, f"closed: the watch is {why}")
        try:
            if await close_case(r["id"], reason, kind="watch_off", pool=pool,
                                note=f"The watch was {why}, so nothing checks this any more.",
                                ending=reason[0].upper() + reason[1:] + "."):
                n += 1
        except Exception:
            log.exception("closing watch case %s failed", r["id"])
    return n


async def close_case(case_id: int, reason: str, *, kind: str, pool=None,
                     note: str | None = None, ending: str | None = None,
                     fact: tuple | None = None, statuses: tuple = OPEN_STATUSES) -> bool:
    """Close a finding case through THE close path (triage._close_case: notify,
    distill), after attaching the closing note, the final fact and the summary."""
    pool = pool or await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                status = await conn.fetchval(
                    "SELECT status FROM cases WHERE id = $1 FOR UPDATE", case_id)
                if status not in statuses:
                    return False
                eid = None
                if note:
                    eid = await cases_util.add_case_note(
                        conn, case_id, note, kind="closing", meta={"close_kind": kind})
                if fact and fact[0] and fact[1] is not None:
                    await cases_util.record_case_fact(conn, case_id, fact[0], str(fact[1]),
                                                      source_event_id=eid)
                await write_summary(conn, case_id, ending=ending)
    except Exception:
        log.exception("closing note/summary for case %s failed; closing anyway", case_id)
    from . import triage   # lazy: triage pulls the LLM client
    closed = await triage._close_case(pool, case_id, reason, kind=kind, statuses=statuses)
    if closed:  # Quick 008
        try:
            title = await pool.fetchval("SELECT title FROM cases WHERE id = $1", case_id)
        except Exception:  # noqa: BLE001 - the name is cosmetic
            title = None
        await _record_run(case_id, title, f"Closed: {reason}", kind.replace("_", " "))
    return closed


async def mark_seen(pool, case_id: int) -> bool:
    """The owner acknowledged the alert: the gold card goes until the next fire."""
    return bool(await pool.fetchval(
        """UPDATE cases SET seen_at = now()
            WHERE id = $1 AND status = ANY($2::text[]) RETURNING id""",
        case_id, list(OPEN_STATUSES)))


# ── insight cases ──────────────────────────────────────────────────────────

# Owner change (2026-10-25): every warning or critical insight is its own case; info
# insights open none (they stay on the Insights page, stored on the shared
# exploration case, which is an internal log hidden from Cases).
INSIGHT_CASE_SEVERITIES = ("warning", "critical")
SPLIT_REASON = "split: each warning or critical finding now has its own case"


async def insight_case(conn, title: str, finding: str, severity: str = "warning",
                       same_as=None) -> tuple[int, bool]:
    """The case for a warning or critical insight. Same subject, same case: the open
    insight case the exploration agent named (same_as, validated here), else the open
    one with this normalised title, else a new one. Returns (case id, opened now).
    The caller holds the exploration advisory lock and the transaction."""
    try:
        named = int(same_as or 0)
    except (TypeError, ValueError):
        named = 0
    if named > 0:
        cid = await conn.fetchval(
            """SELECT id FROM cases WHERE id = $1 AND origin = 'insight' AND NOT sandbox
                  AND status = ANY($2::text[])""", named, list(OPEN_STATUSES))
        if cid:
            return cid, False
    ref = insight_ref(title)
    cid = await conn.fetchval(
        """SELECT id FROM cases WHERE origin_ref = $1 AND status = ANY($2::text[])
            ORDER BY id DESC LIMIT 1""", ref, list(OPEN_STATUSES))
    if cid:
        return cid, False
    sev = severity if severity in INSIGHT_CASE_SEVERITIES else "warning"
    cid = await conn.fetchval(
        """INSERT INTO cases (title, opened_reason, origin, origin_ref, summary, links)
           VALUES ($1, $2, 'insight', $3, $4, $5::jsonb)
           RETURNING id""",
        str(title)[:TITLE_CAP], f"{sev} insight from data exploration", ref,
        _excerpt(finding, 600),
        json.dumps({"insight_title": str(title)[:TITLE_CAP], "severity": sev}))
    await _record_run(cid, str(title)[:TITLE_CAP],  # Quick 008
                      f"Opened from a {sev} insight", "data exploration")
    return cid, True


async def link_insight(conn, case_id: int, event_id: int, severity: str = "warning") -> None:
    """Point the case at its latest insight. Severity only rises; a critical finding
    is new work for the owner, so it brings the gold card back (seen_at cleared)."""
    await conn.execute(
        """UPDATE cases SET
              links = links || jsonb_build_object(
                  'insight_key', $2::text,
                  'severity', CASE WHEN links->>'severity' = 'critical' OR $3::text = 'critical'
                                   THEN 'critical' ELSE 'warning' END),
              seen_at = CASE WHEN $3::text = 'critical' THEN NULL ELSE seen_at END
            WHERE id = $1 AND origin = 'insight'""", case_id, f"note:{event_id}", severity)


async def split_exploration_cases(pool=None, dry_run: bool = False) -> dict:
    """One-off and idempotent (owner change, 2026-10-25): before it, every insight was a
    note on one shared exploration case. Give each warning or critical note there its
    own insight case (the SAME note is linked to the new case as its first entry, so
    nothing is copied), then archive the shared case with a closing note naming the new
    cases. Its notes stay on it. Info notes are left alone (they live on the Insights
    page). A re-run finds nothing: archived cases are skipped and a note already linked
    to an insight case is not split again. No notice and no distill: this reorganises
    findings, it does not finish any work."""
    pool = pool or await get_pool()
    carriers = await pool.fetch(
        """SELECT id, title FROM cases
            WHERE (origin = 'exploration' OR (origin IS NULL AND opened_reason = 'exploration'))
              AND status = ANY($1::text[]) AND NOT sandbox ORDER BY id""", list(OPEN_STATUSES))
    report = {"dry_run": dry_run, "carriers": []}
    for car in carriers:
        if not dry_run:  # schema.sql backfills this on boot; a script run must not depend on it
            await pool.execute(
                "UPDATE cases SET origin = 'exploration' WHERE id = $1 AND origin IS NULL", car["id"])
        notes = await pool.fetch(
            """SELECT e.id, e.meta FROM case_events ce JOIN events e ON e.id = ce.event_id
                WHERE ce.case_id = $1 AND ce.note = 'exploration' ORDER BY e.id""", car["id"])
        made: list[dict] = []
        already = 0
        for n in notes:
            m = _obj(n["meta"])
            sev, title = str(m.get("severity") or "info"), str(m.get("title") or "").strip()
            if sev not in INSIGHT_CASE_SEVERITIES or not title:
                continue
            if await pool.fetchval(
                    """SELECT 1 FROM case_events ce JOIN cases c ON c.id = ce.case_id
                        WHERE ce.event_id = $1 AND c.origin = 'insight' LIMIT 1""", n["id"]):
                already += 1   # split by an earlier (interrupted) run
                continue
            if dry_run:
                made.append({"note": n["id"], "title": title, "severity": sev, "case": None})
                continue
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('ic:exploration-insights', 0))")
                    cid, _ = await insight_case(conn, title, str(m.get("finding") or ""), sev)
                    await conn.execute(
                        """INSERT INTO case_events (case_id, event_id, note)
                           VALUES ($1, $2, 'exploration') ON CONFLICT DO NOTHING""", cid, n["id"])
                    await conn.execute(
                        """UPDATE cases SET last_activity_at = now(),
                                  events_since_summary = events_since_summary + 1
                            WHERE id = $1""", cid)
                    await link_insight(conn, cid, n["id"], sev)
            made.append({"note": n["id"], "title": title, "severity": sev, "case": cid})
        archived = False
        if (made or already) and not dry_run:
            new_ids = ", ".join(f"#{x['case']}" for x in made) or "earlier"
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await cases_util.add_case_note(
                        conn, car["id"],
                        f"Split into one case per finding: {new_ids}. Info findings stay on "
                        "the Insights page. This case keeps its notes and is archived.",
                        kind="closing", meta={"close_kind": "split"})
                    archived = bool(await conn.fetchval(
                        """UPDATE cases SET status = 'closed', closed_at = now(),
                                  close_reason = $2
                            WHERE id = $1 AND status = ANY($3::text[]) RETURNING id""",
                        car["id"], SPLIT_REASON, list(OPEN_STATUSES)))
        report["carriers"].append({"case": car["id"], "split": made, "archived": archived})
    return report


# ── chat cases ─────────────────────────────────────────────────────────────

async def open_chat_case(*, title: str, finding: str, evidence_sql: str | None,
                         thread_id: str | None, links: dict, run_id: int | None,
                         pool=None) -> tuple[int, bool, int | None]:
    """Open a case the owner asked for in chat, or add the finding to the open case
    with the same normalised title. Writes the executed actions row (user command).
    Returns (case id, opened now, note event id)."""
    pool = pool or await get_pool()
    ref = chat_ref(title)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", ref)
            case_id = await conn.fetchval(
                """SELECT id FROM cases WHERE origin_ref = $1 AND status = ANY($2::text[])
                    ORDER BY id DESC LIMIT 1""", ref, list(OPEN_STATUSES))
            opened = case_id is None
            if opened:
                case_id = await conn.fetchval(
                    """INSERT INTO cases (title, opened_reason, origin, origin_ref, summary,
                                          links, origin_chat_jid, watch_id)
                       VALUES ($1, 'opened from chat at the owner''s request', 'chat', $2, $3,
                               $4::jsonb, $5, $6) RETURNING id""",
                    title[:TITLE_CAP], ref, _excerpt(finding, 600),
                    json.dumps({**links, "thread_id": thread_id}), thread_id,
                    links.get("watch_id"))
            content = f"{title}\n\n{finding}"
            if evidence_sql:
                content += f"\n\nEvidence SQL:\n{evidence_sql}"
            eid = await cases_util.add_case_note(
                conn, case_id, content, kind="chat", cap=6000,
                meta={"origin": "chat", "title": title, "finding": finding,
                      "evidence_sql": evidence_sql, "links": links, "task_run_id": run_id})
            await conn.execute(
                """INSERT INTO actions (case_id, task_run_id, kind, payload, tier, status, executed_at)
                   VALUES ($1, $2, 'open_case', $3::jsonb, 'user_command', 'executed', now())""",
                case_id, run_id, json.dumps({
                    "_summary": (f"Opened case: {title}" if opened
                                 else f"Added to case #{case_id}: {title}"),
                    "title": title, "thread_id": thread_id, "links": links,
                    "opened": opened}, default=str))
    if opened:  # Quick 008
        await _record_run(case_id, title[:TITLE_CAP], "Opened at the owner's request in chat", "chat")
    return case_id, opened, eid


# ── close notices (actionsvc.notify_case_closed calls these for finding cases) ──

def notice_text(case: dict, kind: str) -> str | None:
    """None when there is nothing to tell: the owner closed the case themselves."""
    title, cid = case.get("title") or "case", case.get("id")
    if kind == "resolved":
        msg = f"✅ Back to normal: “{title}” (case #{cid}) is closed."
    elif kind == "watch_off":
        msg = f"Closed case #{cid} “{title}”: its watch is no longer running."
    elif kind == "stale":
        msg = (f"Closing “{title}” (case #{cid}): nothing new for a while, so it has been "
               "archived. Open it on Cases if it still needs attention.")
    else:
        return None
    if case.get("summary"):
        msg += f"\n\n{str(case['summary'])[:400]}"
    return msg


def notice_thread(case: dict) -> str:
    """Where a finding case's notice goes: the thread the finding was reported to,
    else the owner's WhatsApp when the connector is set up, else the in-app chat."""
    thread = case.get("origin_chat_jid")
    if thread:
        return str(thread)
    s = get_settings()
    if s.owner_number and s.wa_api_base:
        return s.owner_number
    return appchannel.thread_for(None)


async def deliver_notice(thread: str, msg: str) -> bool:
    if appchannel.is_app_thread(thread):
        return await appchannel.deliver(thread, msg)
    from . import whatsapp
    return await whatsapp.send_message(thread, msg)
