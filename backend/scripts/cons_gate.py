"""Phase 17 gate helper: deterministic tools-mode steps for 17-live-check.sh. Runs inside the app container. Prints JSON, never secrets.

Run inside the app container:  python -m scripts.cons_gate <step> [options]

Every step prints exactly one JSON line on stdout and exits 0 on success, 1 on failure. Output goes
through secretbox.redact; no step prints a credential, a DSN or a password.
"""
import argparse
import asyncio
import json
import os
import math
import re
import sys
import time

THREAD = "ui:gate17"

STEPS = ("run-id", "measures", "propose-kpi", "approve", "force-check", "watch-state",
         "measure-value", "sql-watch", "backdate", "release", "snapshot", "refresh-now",
         "spec-dashboard", "propose-mode", "approve-mode", "advance", "view-def", "table-info",
         "budget", "insight", "grants", "retention", "sync-times", "run-sql", "approve-context")


# ---------------------------------------------------------------- pure helpers (unit-tested)
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts.cons_gate", description=__doc__.splitlines()[0])
    p.add_argument("step", choices=STEPS)
    p.add_argument("--run-id", type=int, default=None, help="task run id used as the tool context")
    p.add_argument("--name", default="")
    p.add_argument("--json", dest="json_args", default="", help="JSON object with extra arguments")
    p.add_argument("--action-id", type=int, default=None)
    p.add_argument("--watch", type=int, default=None)
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--query", default="")
    p.add_argument("--minutes", type=int, default=0)
    p.add_argument("--schema", default="")
    p.add_argument("--table", default="")
    p.add_argument("--budget", type=int, default=3)
    p.add_argument("--sqls", default="[]", help="JSON list of SQL strings")
    p.add_argument("--sql", default="")
    p.add_argument("--timeout", type=float, default=120.0)
    return p


def parse_json_arg(raw: str) -> dict:
    """The --json value as a dict; empty means {}. Anything that is not an object is an error."""
    if not raw or not raw.strip():
        return {}
    val = json.loads(raw)
    if not isinstance(val, dict):
        raise ValueError("--json must be a JSON object")
    return val


def parse_list_arg(raw: str) -> list:
    val = json.loads(raw or "[]")
    if not isinstance(val, list):
        raise ValueError("expected a JSON list")
    return val


def close_enough(a, b, rel: float = 1e-9) -> bool:
    """Float comparison with a relative tolerance of 1e-9 (absolute 1e-12 near zero)."""
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if math.isnan(x) or math.isnan(y):
        return False
    return abs(x - y) <= max(rel * max(abs(x), abs(y)), 1e-12)


def norm_signature(rows) -> list[str]:
    """(name, type) pairs as 'name:type' strings, in column order, whitespace and case folded."""
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            name, typ = r.get("name"), r.get("type")
        else:
            name, typ = r[0], r[1]
        typ = re.sub(r"\s+", " ", str(typ)).strip().lower()
        out.append(f"{str(name).strip().lower()}:{typ}")
    return out


def classify(text) -> dict:
    """Shape a tool's text result: refused, tool_error and ok flags."""
    s = text if isinstance(text, str) else str(text)
    return {"ok": not s.startswith("TOOL ERROR"), "refused": s.startswith("REFUSED"),
            "budget_spent": s.startswith("LIVE QUERY BUDGET SPENT"),
            "tool_error": s.startswith("TOOL ERROR"), "text": s}


def first_id(text, word: str):
    """The integer after '<word> #' in a tool result, or None."""
    m = re.search(re.escape(word) + r"\s*#(\d+)", text if isinstance(text, str) else "")
    return int(m.group(1)) if m else None


def first_number(rows):
    """First numeric value of the first row, or None."""
    for row in rows or []:
        for v in (row.values() if isinstance(row, dict) else row):
            if isinstance(v, bool):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        break
    return None


def shape(obj) -> str:
    """One redacted JSON line. Redaction runs over the serialized text so nested values are covered."""
    line = json.dumps(obj, default=str, ensure_ascii=False)
    try:
        from app import secretbox
        return secretbox.redact(line) or line
    except Exception:  # redaction must never be the reason a result is lost
        return line


def exit_code(result: dict) -> int:
    return 0 if result.get("ok", True) else 1


def emit(result: dict) -> int:
    sys.stdout.write(shape(result) + "\n")
    sys.stdout.flush()
    return exit_code(result)


def iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def jval(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# ---------------------------------------------------------------- context
async def _ctx_async(args) -> None:
    """ctx_run_id and ctx_phone (the run's thread) so owner-thread rules apply as in chat."""
    from app.agents.graph import ctx_phone, ctx_run_id
    ctx_phone.set(THREAD)
    if args.run_id is None:
        return
    ctx_run_id.set(args.run_id)
    from app.db import get_pool
    pool = await get_pool()
    tid = await pool.fetchval("SELECT thread_id FROM task_runs WHERE id = $1", args.run_id)
    ctx_phone.set(tid or THREAD)


async def _new_run(conn=None) -> int:
    from app import sandbox
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("INSERT INTO threads (thread_id, phone) VALUES ($1,$1) "
                            "ON CONFLICT DO NOTHING", THREAD)
            mid = await c.fetchval("INSERT INTO messages (thread_id, role, content) "
                                   "VALUES ($1,'user','[phase 17 gate]') RETURNING id", THREAD)
            rid = await c.fetchval(
                "INSERT INTO task_runs (thread_id, message_id, status, started_at, ended_at) "
                "VALUES ($1, $2, 'done', now(), now()) RETURNING id", THREAD, mid)
    sandbox.scratch_dir(rid)
    return int(rid)


async def _pool():
    from app.db import get_pool
    return await get_pool()


# ---------------------------------------------------------------- steps: run, model, watches
async def step_run_id(args) -> dict:
    return {"ok": True, "run_id": await _new_run()}


async def step_measures(args) -> dict:
    """Active measure names; time_dimension tells whether a measure can take a time range."""
    from app import semanticmodel
    model = await semanticmodel.load_model()
    out = []
    for m in model.measures:
        d = m.__dict__ if hasattr(m, "__dict__") else {}
        out.append({"name": m.name, "label": getattr(m, "label", None),
                    "time_dimension": d.get("time_dimension") or d.get("time") or None})
    return {"ok": True, "measures": out, "names": [m["name"] for m in out]}


async def step_propose_kpi(args) -> dict:
    from app import insights
    j = parse_json_arg(args.json_args)
    res = await insights.propose_kpi_action(
        j["title"], j.get("condition", ""), j.get("cadence", "15"), j.get("rationale", ""),
        origin="chat", thread_id=THREAD, run_id=args.run_id, cap=50,
        measure=j.get("measure"), filters=j.get("filters"), range=j.get("range"),
        alert=j.get("alert"))
    out = classify(res)
    out["action_id"] = first_id(res, "action")
    out["ok"] = out["action_id"] is not None
    return out


async def step_approve(args) -> dict:
    """Approve an action (default: the newest proposed create_watch); reports the watch id."""
    from app import actionsvc
    pool = await _pool()
    aid = args.action_id
    if aid is None:
        aid = await pool.fetchval("SELECT max(id) FROM actions WHERE kind='create_watch' "
                                  "AND status='proposed'")
    if aid is None:
        return {"ok": False, "error": "no proposed action"}
    row = await pool.fetchrow("SELECT kind, payload FROM actions WHERE id=$1", aid)
    res = await actionsvc.approve(int(aid))
    out = {"ok": "error" not in res, "action_id": int(aid), **{k: v for k, v in res.items()}}
    if row and row["kind"] == "create_watch":
        p = jval(row["payload"]) or {}
        out["watch_id"] = await pool.fetchval(
            "SELECT id FROM watches WHERE origin_ref = $1", p.get("proposal_key"))
    return out


async def _watch_counts(pool, wid: int) -> tuple:
    h = await pool.fetchval("SELECT coalesce(max(id),0) FROM metric_history WHERE watch_id=$1", wid)
    e = await pool.fetchval("SELECT coalesce(max(id),0) FROM watch_events WHERE watch_id=$1", wid)
    return int(h), int(e)


def _hist_view(r) -> dict:
    obs = jval(r["observation"]) or {}
    return {"id": int(r["id"]), "value": r["value"], "stale": bool(r["stale"]),
            "as_of": obs.get("as_of") if isinstance(obs, dict) else None,
            "observed_at": iso(r["observed_at"])}


async def step_force_check(args) -> dict:
    pool = await _pool()
    wid = args.watch
    h0, e0 = await _watch_counts(pool, wid)
    await pool.execute("UPDATE watches SET last_checked_at = NULL WHERE id = $1", wid)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        h1, e1 = await _watch_counts(pool, wid)
        if h1 > h0 or e1 > e0:
            await asyncio.sleep(3)  # the event and the marker row are separate writes
            break
        await asyncio.sleep(2)
    else:
        w = await pool.fetchrow("SELECT compile_error, status FROM watches WHERE id=$1", wid)
        return {"ok": False, "error": "timeout waiting for a check",
                "compile_error": w["compile_error"] if w else None}
    hist = await pool.fetch("SELECT * FROM metric_history WHERE watch_id=$1 AND id > $2 "
                            "ORDER BY id", wid, h0)
    ev = await pool.fetch("SELECT id, kind, severity, summary FROM watch_events "
                          "WHERE watch_id=$1 AND id > $2 ORDER BY id", wid, e0)
    return {"ok": True, "history": [_hist_view(r) for r in hist],
            "events": [{"id": int(r["id"]), "kind": r["kind"], "summary": r["summary"]} for r in ev]}


async def step_watch_state(args) -> dict:
    pool = await _pool()
    w = await pool.fetchrow("SELECT compiled_sql, compile_error, stale_since, data_as_of, status, "
                            "measure_ref FROM watches WHERE id=$1", args.watch)
    if w is None:
        return {"ok": False, "error": "unknown watch"}
    ev = await pool.fetch("SELECT kind, summary FROM watch_events WHERE watch_id=$1 "
                          "ORDER BY id DESC LIMIT 3", args.watch)
    hist = await pool.fetch("SELECT * FROM metric_history WHERE watch_id=$1 ORDER BY id DESC "
                            "LIMIT 3", args.watch)
    return {"ok": True, "compiled_sql": w["compiled_sql"], "compile_error": w["compile_error"],
            "stale_since": iso(w["stale_since"]), "data_as_of": iso(w["data_as_of"]),
            "status": w["status"], "events": [dict(kind=r["kind"], summary=r["summary"]) for r in ev],
            "history": [_hist_view(r) for r in hist]}


async def step_measure_value(args) -> dict:
    from app import modeltools
    j = parse_json_arg(args.json_args)
    payload = {"measure": j["measure"]}
    for k in ("by", "grain", "filters", "range"):
        if j.get(k) is not None:
            payload[k] = j[k]
    text = await modeltools.query_measure.ainvoke(payload)
    try:
        doc = json.loads(text.split("\n\nUNVERIFIED")[0])
    except ValueError:
        return {"ok": False, "error": text[:300]}
    return {"ok": True, "sql": doc.get("sql"), "value": first_number(doc.get("rows")),
            "row_count": doc.get("row_count")}


async def step_sql_watch(args) -> dict:
    """A kpi watch with a draft SQL and trigger hint, created through the create_watch executor
    path so it compiles without an LLM."""
    import uuid
    from app import insights
    j = parse_json_arg(args.json_args)
    key = uuid.uuid4().hex
    payload = {"title": j["title"], "condition": j.get("condition") or j["title"] + " (gate watch)",
               "cadence_seconds": int(j.get("cadence_seconds", 900)), "origin": "chat",
               "thread_id": THREAD, "proposal_key": key, "draft_sql": j["sql"],
               "trigger_hint": j.get("trigger") or {"type": "threshold", "column": "n",
                                                    "op": ">", "value": 1e12}}
    ok = await insights.create_watch_from_proposal(payload)
    pool = await _pool()
    wid = await pool.fetchval("SELECT id FROM watches WHERE origin_ref=$1", key)
    return {"ok": bool(ok and wid), "watch_id": wid}


# ---------------------------------------------------------------- steps: sync clocks, dashboards
async def step_backdate(args) -> dict:
    """Hold back a source's sync: its import tables look minutes old, next_sync_at a day away."""
    pool = await _pool()
    n = await pool.fetchval(
        "WITH u AS (UPDATE source_tables t SET last_success_at = now() - make_interval(mins => $2) "
        "FROM sources s WHERE s.id = t.source_id AND s.name = $1 AND t.mode = 'import' "
        "RETURNING t.id) SELECT count(*) FROM u", args.name, args.minutes)
    await pool.execute("UPDATE sources SET next_sync_at = now() + interval '1 day' WHERE name=$1",
                       args.name)
    return {"ok": n > 0, "tables": int(n)}


async def step_release(args) -> dict:
    pool = await _pool()
    await pool.execute("UPDATE sources SET next_sync_at = now() WHERE name=$1", args.name)
    return {"ok": True}


async def step_sync_times(args) -> dict:
    pool = await _pool()
    rows = await pool.fetch(
        "SELECT t.warehouse_schema||'.'||t.warehouse_table AS t, t.last_success_at, t.mode, "
        "s.schedule FROM source_tables t JOIN sources s ON s.id=t.source_id WHERE s.name=$1 "
        "ORDER BY 1", args.name)
    return {"ok": True, "tables": {r["t"]: iso(r["last_success_at"]) for r in rows},
            "interval_minutes": (jval(rows[0]["schedule"]) or {}).get("interval_minutes") if rows else None}


async def step_snapshot(args) -> dict:
    pool = await _pool()
    r = await pool.fetchrow(
        "SELECT s.refreshed_at, s.attempted_at, s.data_as_of, s.sync_aligned, s.reads, "
        "s.inputs_synced_at, s.row_count, s.error, s.next_refresh_at, md5(s.data::text) AS h "
        "FROM dashboard_snapshots s JOIN dashboard_versions v ON v.id = s.version_id "
        "WHERE s.version_id = $1 AND s.query_id = $2", args.version, args.query)
    if r is None:
        return {"ok": False, "error": "no snapshot row"}
    return {"ok": True, "refreshed_at": iso(r["refreshed_at"]), "attempted_at": iso(r["attempted_at"]),
            "data_as_of": iso(r["data_as_of"]), "sync_aligned": r["sync_aligned"],
            "reads": list(r["reads"] or []), "inputs_synced_at": iso(r["inputs_synced_at"]),
            "row_count": r["row_count"], "error": r["error"], "data_hash": r["h"],
            "next_refresh_at": iso(r["next_refresh_at"])}


async def step_refresh_now(args) -> dict:
    pool = await _pool()
    await pool.execute("UPDATE dashboard_snapshots SET next_refresh_at = now() "
                       "WHERE version_id = $1 AND query_id = $2", args.version, args.query)
    return {"ok": True}


async def step_spec_dashboard(args) -> dict:
    """Save a spec through the Phase 18 tool and publish it through the approval executor."""
    from app import actionsvc, dashboardtools
    j = parse_json_arg(args.json_args)
    text = await dashboardtools.save_dashboard_version.ainvoke(
        {"slug": j["slug"], "spec": j["spec"], "change_note": j.get("change_note", "gate 17")})
    out = classify(text)
    aid = first_id(text, "action")
    if aid is None:
        out["ok"] = False
        return out
    res = await actionsvc.approve(aid)
    pool = await _pool()
    d = await pool.fetchrow(
        "SELECT d.slug, d.view_token, d.current_version_id, v.version FROM dashboards d "
        "LEFT JOIN dashboard_versions v ON v.id = d.current_version_id WHERE d.slug = $1", j["slug"])
    return {"ok": "error" not in res and d is not None and d["current_version_id"] is not None,
            "action_id": aid, "slug": j["slug"], "version_id": d["current_version_id"] if d else None,
            "version": d["version"] if d else None, "view_token": d["view_token"] if d else None}


# ---------------------------------------------------------------- steps: mode flips
async def step_propose_mode(args) -> dict:
    from app import sourcetools
    j = parse_json_arg(args.json_args)
    text = await sourcetools.propose_mode_change.ainvoke(
        {"source": j["source"], "tables": j["tables"]})
    out = classify(text)
    out["action_id"] = first_id(text, "action")
    out["ok"] = out["action_id"] is not None
    return out


async def step_approve_mode(args) -> dict:
    from app import actionsvc
    pool = await _pool()
    aid = args.action_id or await pool.fetchval(
        "SELECT max(id) FROM actions WHERE kind='source_change' AND status='proposed' "
        "AND payload->>'name' = $1", args.name)
    if aid is None:
        return {"ok": False, "error": f"no proposed source_change for {args.name}"}
    res = await actionsvc.approve(int(aid))
    return {"ok": "error" not in res, "action_id": int(aid), **res}


async def step_advance(args) -> dict:
    from app import modeflip
    res = await modeflip.advance()
    return {"ok": True, **res}


async def step_table_info(args) -> dict:
    pool = await _pool()
    r = await pool.fetchrow(
        "SELECT t.mode, t.pending_mode, t.backfill_done, t.last_success_at, t.import_reset, t.status "
        "FROM source_tables t JOIN sources s ON s.id=t.source_id "
        "WHERE s.name=$1 AND t.warehouse_schema=$2 AND t.warehouse_table=$3",
        args.name, args.schema, args.table)
    if r is None:
        return {"ok": False, "error": "no such table"}
    return {"ok": True, "mode": r["mode"], "pending_mode": r["pending_mode"],
            "backfill_done": r["backfill_done"], "last_success_at": iso(r["last_success_at"]),
            "import_reset": r["import_reset"], "status": r["status"]}


async def step_view_def(args) -> dict:
    from app import warehouse
    from app.warehouse import DEFAULT_ROLES
    conn = await warehouse.connect("owner", roles=DEFAULT_ROLES, timeout=15.0)
    try:
        qual = f'"{args.schema}"."{args.table}"'
        d = await conn.fetchval("SELECT pg_get_viewdef($1::regclass, true)", qual)
        cols = await conn.fetch(
            "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type "
            "FROM pg_attribute a WHERE a.attrelid = $1::regclass AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY a.attnum", qual)
    finally:
        await conn.close()
    sig = norm_signature([dict(c) for c in cols])
    return {"ok": d is not None, "viewdef": d, "signature": sig,
            "reads_import": "ic_imp_" in (d or ""), "reads_foreign": "ic_fdw" in (d or "")}


async def step_run_sql(args) -> dict:
    from app import guardrails
    rows = await guardrails.run_readonly_sql(args.sql)
    return {"ok": True, "rows": rows, "count": len(rows),
            "value": first_number(rows)}


# ---------------------------------------------------------------- steps: exploration, retention
async def _explore_playbook_id() -> int:
    pool = await _pool()
    pid = await pool.fetchval("SELECT id FROM playbooks WHERE name='explore_data'")
    if pid is None:
        raise ValueError("explore_data playbook missing")
    return int(pid)


def _set_budget(n: int) -> None:
    from app.config import get_settings
    s = get_settings()
    try:
        s.playbook_live_query_budget = n
    except Exception:
        object.__setattr__(s, "playbook_live_query_budget", n)


async def step_budget(args) -> dict:
    from app import livebudget
    from app.agents import graph
    from app import playbook_tools
    rid = await _new_run()
    graph.ctx_run_id.set(rid)
    playbook_tools.ctx_playbook_id.set(await _explore_playbook_id())
    playbook_tools.ctx_tier.set("draft")
    _set_budget(args.budget)
    results = []
    try:
        for sql in parse_list_arg(args.sqls):
            text = await graph.run_sql.ainvoke({"sql": sql})
            c = classify(text)
            results.append({"sql": sql[:80], "refused": c["budget_spent"],
                            "text": c["text"][:240]})
    finally:
        livebudget.forget(rid)
    return {"ok": True, "run_id": rid, "results": results,
            "refused_indexes": [i for i, r in enumerate(results) if r["refused"]]}


async def step_insight(args) -> dict:
    from app import playbook_tools
    from app.agents import graph
    j = parse_json_arg(args.json_args)
    rid = await _new_run()
    graph.ctx_run_id.set(rid)
    playbook_tools.ctx_playbook_id.set(await _explore_playbook_id())
    playbook_tools.ctx_tier.set("draft")
    payload = {"title": j["title"], "finding": j["finding"], "severity": j.get("severity", "info")}
    if j.get("evidence_sql"):
        payload["evidence_sql"] = j["evidence_sql"]
    if j.get("evidence_measure"):
        payload["evidence_measure"] = j["evidence_measure"]
    text = await playbook_tools.record_insight.ainvoke(payload)
    out = classify(text)
    eid = first_id(text, "note event")
    out["event_id"] = eid
    if eid is not None:
        pool = await _pool()
        meta = jval(await pool.fetchval("SELECT meta FROM events WHERE id=$1", eid)) or {}
        out.update(lane=meta.get("lane"), verified=meta.get("verified"),
                   stored_finding=meta.get("finding"))
    out["ok"] = eid is not None
    return out


async def step_grants(args) -> dict:
    pool = await _pool()
    r = await pool.fetchrow("SELECT tool_grants, goal FROM playbooks WHERE name='explore_data'")
    grants = jval(r["tool_grants"]) if r else None
    return {"ok": r is not None, "tool_grants": grants,
            "has_get_model": "get_model" in (grants or []),
            "has_query_measure": "query_measure" in (grants or [])}


async def step_retention(args) -> dict:
    from app import consolidation
    res = await consolidation.sync_retention()
    return {"ok": True, **res}


async def step_approve_context(args) -> dict:
    """Approve table descriptions for the demo tables (insight evidence needs approved tables).
    Goes through the same propose and approve path as chat; already approved tables are skipped."""
    from app import actionsvc, contexttools
    j = parse_json_arg(args.json_args)
    tables = j.get("tables") or ["regions", "sales_reps", "customers", "products", "sales_orders",
                                 "order_lines", "invoices", "payments", "inventory_snapshots"]
    pool = await _pool()
    await contexttools.discover_schema.ainvoke({"source": "business_db"})
    before = int(await pool.fetchval("SELECT coalesce(max(id),0) FROM actions") or 0)
    items = [{"kind": "table", "ref": f"business_db.public.{t}",
              "text": f"The {t} table of the synthetic Northwind Supply demo business."} for t in tables]
    text = await contexttools.propose_context_changes.ainvoke({"items": items})
    ids = [int(r["id"]) for r in await pool.fetch(
        "SELECT id FROM actions WHERE id > $1 AND kind = 'context_change' AND status = 'proposed' "
        "ORDER BY id", before)]
    done = 0
    for aid in ids:
        res = await actionsvc.approve(aid)
        done += 0 if "error" in res else 1
    n = int(await pool.fetchval("SELECT count(*) FROM context_tables WHERE status='active' "
                                "AND description IS NOT NULL") or 0)
    return {"ok": n >= 1, "approved_now": done, "approved_total": n, "note": text[:200]}


HANDLERS = {
    "run-id": step_run_id, "measures": step_measures, "propose-kpi": step_propose_kpi,
    "approve": step_approve, "force-check": step_force_check, "watch-state": step_watch_state,
    "measure-value": step_measure_value, "sql-watch": step_sql_watch, "backdate": step_backdate,
    "release": step_release, "snapshot": step_snapshot, "refresh-now": step_refresh_now,
    "spec-dashboard": step_spec_dashboard, "propose-mode": step_propose_mode,
    "approve-mode": step_approve_mode, "advance": step_advance, "view-def": step_view_def,
    "table-info": step_table_info, "budget": step_budget, "insight": step_insight,
    "grants": step_grants, "retention": step_retention, "sync-times": step_sync_times,
    "run-sql": step_run_sql, "approve-context": step_approve_context,
}


async def _amain(args) -> dict:
    from app import configservice
    try:
        await configservice.load_overrides()
    except Exception:
        pass
    await _ctx_async(args)
    return await HANDLERS[args.step](args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # WR-05: this gate approves actions and writes synthetic data; demo stacks only
    if os.environ.get("SCENARIO_DEMO_STACK") != "1":
        return emit({"ok": False, "error": "demo stacks only (set SCENARIO_DEMO_STACK=1)"})
    try:
        result = asyncio.run(_amain(args))
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
