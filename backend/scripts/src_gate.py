"""Phase 15 gate helper: deterministic tools-mode steps for 15-live-check.sh. Runs inside the app container. Prints JSON, never secrets.

Run inside the app container:  python -m scripts.src_gate <step> [options]

Every step prints exactly one JSON line on stdout and exits 0 on success, 1 on failure. Output goes
through secretbox.redact; no step prints a credential, a DSN or a password.
"""
import argparse
import asyncio
import json
import os
import sys
import time

STEPS = ("run-id", "probe", "test", "propose", "approve", "reject", "wait", "wait-running",
         "reconcile", "sync", "discover", "propose-column", "refusal", "status", "sql", "catalog",
         "measure", "payload")


# ---------------------------------------------------------------- pure helpers (unit-tested)
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts.src_gate", description=__doc__.splitlines()[0])
    p.add_argument("step", choices=STEPS)
    p.add_argument("--run-id", type=int, default=None, help="task run id used as the tool context")
    p.add_argument("--name", default="")
    p.add_argument("--target", default="")
    p.add_argument("--tables", default="", help="comma separated table names")
    p.add_argument("--json", dest="json_args", default="", help="JSON object with extra arguments")
    p.add_argument("--action-id", type=int, default=None)
    p.add_argument("--after", type=int, default=0, help="wait: only runs with an id above this")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--csv-file", default="")
    p.add_argument("--ref", default="")
    p.add_argument("--text", default="")
    p.add_argument("--sql", default="")
    p.add_argument("--kind", default="sync")
    return p


def parse_json_arg(raw: str) -> dict:
    """The --json value as a dict; empty means {}. Anything that is not an object is an error."""
    if not raw or not raw.strip():
        return {}
    val = json.loads(raw)
    if not isinstance(val, dict):
        raise ValueError("--json must be a JSON object")
    return val


def split_tables(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def classify(text) -> dict:
    """Shape a tool's text result: refused, tool_error and ok flags. A refusal is an expected
    outcome for some gate checks, so only a tool error counts as a failed step."""
    s = text if isinstance(text, str) else str(text)
    refused = s.startswith("REFUSED")
    tool_error = s.startswith("TOOL ERROR")
    return {"ok": not tool_error, "refused": refused, "tool_error": tool_error, "text": s}


def shape(obj) -> str:
    """One redacted JSON line. Redaction runs over the serialized text so nested values are covered."""
    try:
        from app import secretbox
        line = json.dumps(obj, default=str, ensure_ascii=False)
        return secretbox.redact(line) or line
    except Exception:  # redaction must never be the reason a result is lost
        return json.dumps(obj, default=str, ensure_ascii=False)


def finished(status) -> bool:
    return status in ("ok", "partial", "failed")


def exit_code(result: dict) -> int:
    return 0 if result.get("ok", True) else 1


def emit(result: dict) -> int:
    sys.stdout.write(shape(result) + "\n")
    sys.stdout.flush()
    return exit_code(result)


# ---------------------------------------------------------------- steps
def _ctx(args) -> None:
    from app.agents.graph import ctx_run_id
    if args.run_id is not None:
        ctx_run_id.set(args.run_id)


def thread_for_ctx(thread_id) -> str:
    """Phase 15 gap closure (15-21): steps run on the thread of their run (CR-04)."""
    return thread_id if isinstance(thread_id, str) and thread_id else "ui:gate"


async def _ctx_async(args) -> None:
    """Sets ctx_run_id and ctx_phone (the run's thread) so owner-thread rules apply as in chat."""
    _ctx(args)
    if args.run_id is None:
        return
    from app.agents.graph import ctx_phone
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        tid = await conn.fetchval("SELECT thread_id FROM task_runs WHERE id = $1", args.run_id)
    ctx_phone.set(thread_for_ctx(tid))


async def _source_id(name: str) -> int:
    from app import sourcesvc
    s = await sourcesvc.get_source(name)
    if s is None:
        raise ValueError(f"unknown source {name}")
    return int(s["id"])


async def step_run_id(args) -> dict:
    from app import sandbox
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO threads (thread_id, phone) VALUES ('ui:gate','ui:gate') "
                               "ON CONFLICT DO NOTHING")
            mid = await conn.fetchval("INSERT INTO messages (thread_id, role, content) "
                                      "VALUES ('ui:gate','user','[phase 15 gate]') RETURNING id")
            rid = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id, status, started_at, ended_at) "
                "VALUES ('ui:gate', $1, 'done', now(), now()) RETURNING id", mid)
    sandbox.scratch_dir(rid)
    return {"ok": True, "run_id": int(rid)}


async def _tool(fn, payload: dict) -> dict:
    return classify(await fn.ainvoke(payload))


async def step_probe(args) -> dict:
    from app import sourcetools
    return await _tool(sourcetools.probe_source, {"target": args.target})


async def step_test(args) -> dict:
    from app import sourcetools
    return await _tool(sourcetools.test_source, {"name": args.name, "target": args.target,
                                                 "tables": split_tables(args.tables)})


async def step_propose(args) -> dict:
    from app import sourcetools
    extra = parse_json_arg(args.json_args)
    payload = {"name": args.name, "source_config": extra.get("source_config") or {}}
    for k in ("schedule", "modes"):
        if extra.get(k) is not None:
            payload[k] = extra[k]
    out = await _tool(sourcetools.propose_source, payload)
    import re
    m = re.search(r"action #(\d+)", out["text"])
    out["action_id"] = int(m.group(1)) if m else None
    return out


async def _newest_action(name: str, status: str = "proposed"):
    from app.db import get_pool
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT id, status, payload FROM actions WHERE kind='source_change' AND payload->>'name'=$1 "
        "AND ($2='' OR status=$2) ORDER BY id DESC LIMIT 1", name, status)


async def _resolve_action(args) -> int:
    if args.action_id:
        return args.action_id
    row = await _newest_action(args.name)
    if row is None:
        raise ValueError(f"no proposed source_change for {args.name}")
    return int(row["id"])


async def step_approve(args) -> dict:
    from app import actionsvc
    res = await actionsvc.approve(await _resolve_action(args))
    return {"ok": "error" not in res, **res}


async def step_reject(args) -> dict:
    from app import actionsvc
    res = await actionsvc.reject(await _resolve_action(args))
    return {"ok": "error" not in res, **res}


async def step_payload(args) -> dict:
    row = await _newest_action(args.name, "")
    if row is None:
        return {"ok": False, "error": f"no source_change for {args.name}"}
    p = row["payload"]
    return {"ok": True, "action_id": int(row["id"]), "status": row["status"],
            "payload": json.loads(p) if isinstance(p, str) else p}


async def _newest_run(source_id: int):
    from app.db import get_pool
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT id, kind, status, rows_loaded, tables, error, peak_memory_mb, duration_ms "
        "FROM sync_runs WHERE source_id=$1 ORDER BY id DESC LIMIT 1", source_id)


def _run_view(r) -> dict:
    t = r["tables"]
    return {"run_id": int(r["id"]), "kind": r["kind"], "status": r["status"],
            "rows_loaded": int(r["rows_loaded"] or 0),
            "tables": json.loads(t) if isinstance(t, str) else (t or []), "error": r["error"],
            "peak_memory_mb": r["peak_memory_mb"], "duration_ms": r["duration_ms"]}


async def step_wait(args) -> dict:
    sid = await _source_id(args.name)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        r = await _newest_run(sid)
        if r is not None and int(r["id"]) > args.after and finished(r["status"]):
            return {"ok": True, **_run_view(r)}
        await asyncio.sleep(2)
    r = await _newest_run(sid)
    return {"ok": False, "error": "timeout waiting for a finished run",
            **(_run_view(r) if r is not None else {})}


async def _sync_containers() -> int:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "label=ic.sync=1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return len((out or b"").split())


async def step_wait_running(args) -> dict:
    sid = await _source_id(args.name)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        r = await _newest_run(sid)
        if r is not None and int(r["id"]) > args.after and r["status"] == "running":
            n = await _sync_containers()
            if n:
                return {"ok": True, "containers": n, **_run_view(r)}
        await asyncio.sleep(0.5)
    return {"ok": False, "error": "timeout waiting for a running sync container"}


async def step_reconcile(args) -> dict:
    from app import syncsched
    rid = await syncsched.enqueue_now(await _source_id(args.name), "reconcile")
    return {"ok": rid is not None, "run_id": rid}


async def step_sync(args) -> dict:
    from app import sourcetools
    payload = {"name": args.name}
    if args.csv_file:
        payload["csv_file"] = args.csv_file
    out = await _tool(sourcetools.request_sync, payload)
    import re
    m = re.search(r"run #(\d+)", out["text"])
    out["run_id"] = int(m.group(1)) if m else None
    return out


async def step_discover(args) -> dict:
    from app import contexttools
    return await _tool(contexttools.discover_schema, {"source": args.name or "business_db"})


async def step_propose_column(args) -> dict:
    from app import contexttools
    return await _tool(contexttools.propose_context_change, {
        "kind": "column", "ref": args.ref, "text": args.text or "Gate check description."})


async def step_refusal(args) -> dict:
    """propose_source with a DSN in the config: must be refused, and the tool_calls row must not
    hold the DSN."""
    from app import sourcetools
    from app.db import get_pool
    dsn = "mysql://gate_user:gate_pw_x1@db.example.invalid:3306/ops"
    out = await _tool(sourcetools.propose_source, {
        "name": args.name or "gate_refusal",
        "source_config": {"target": dsn, "tables": [{"table": "t"}]}})
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT args::text AS a, result_digest FROM tool_calls WHERE tool='propose_source' "
        "ORDER BY id DESC LIMIT 1")
    a = (row["a"] if row else "") or ""
    out["tool_call_args"] = a
    out["dsn_in_args"] = ("gate_pw_x1" in a) or ("db.example.invalid" in a)
    out["ok"] = out["refused"] and not out["dsn_in_args"]
    return out


async def step_status(args) -> dict:
    from app import sourcetools
    return await _tool(sourcetools.get_sync_status, {"name": args.name})


async def step_sql(args) -> dict:
    from app import guardrails
    rows = await guardrails.run_readonly_sql(args.sql)
    return {"ok": True, "rows": rows, "count": len(rows)}


async def step_catalog(args) -> dict:
    from app import sourcesvc, warehouse
    s = await sourcesvc.get_source(args.name)
    if s is None:
        return {"ok": False, "error": f"unknown source {args.name}"}
    registered = sorted(t["warehouse_table"] for t in s.get("tables") or [])
    conn = await warehouse.connect("reader")
    try:
        rows = await conn.fetch("SELECT table_name FROM ic_meta.source_catalog WHERE source=$1 "
                                "ORDER BY table_name", args.name)
    finally:
        await conn.close()
    cat = [r["table_name"] for r in rows]
    return {"ok": True, "registered": registered, "catalog": cat, "match": registered == sorted(cat)}


async def step_measure(args) -> dict:
    """One syncrunner.run of an import job. The source and warehouse come from the IC_SRC_* and
    IC_WH_* variables of this process (passed in by the shell); nothing secret is printed."""
    from app import syncrunner
    extra = parse_json_arg(args.json_args)
    names = ("IC_SRC_HOST", "IC_SRC_PORT", "IC_SRC_DB", "IC_SRC_USER", "IC_SRC_PASSWORD",
             "IC_WH_HOST", "IC_WH_PORT", "IC_WH_DB", "IC_WH_USER", "IC_WH_PASSWORD")
    env = {k: os.environ[k] for k in names if os.environ.get(k)}
    missing = [k for k in names if k not in env]
    if missing:
        return {"ok": False, "error": "missing environment: " + ",".join(missing)}
    name = args.name or "measure"
    job = {"v": 1, "source": name, "kind": extra.get("kind", "mysql"),
           "remote_schema": extra.get("remote_schema"),
           "dataset": f"ic_imp_{name}", "pipeline": f"ic_{name}",
           "tables": extra["tables"], "reconcile": False,
           "caps": extra.get("caps") or {"max_rows": 50_000_000, "max_seconds": 5400,
                                         "chunk_size": 50_000}}
    t0 = time.monotonic()
    res = await syncrunner.run("sync", job, env, name=f"gate-{name}", network=extra.get("network"),
                               timeout=float(args.timeout))
    tabs = res.get("tables") or []
    return {"ok": res.get("status") in ("ok", "partial") and bool(tabs),
            "status": res.get("status"), "peak_memory_mb": res.get("peak_memory_mb"),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "rows": [{"table": t.get("table"), "rows": t.get("rows"), "status": t.get("status")}
                     for t in tabs],
            "error": res.get("error")}


HANDLERS = {
    "run-id": step_run_id, "probe": step_probe, "test": step_test, "propose": step_propose,
    "approve": step_approve, "reject": step_reject, "wait": step_wait,
    "wait-running": step_wait_running, "reconcile": step_reconcile, "sync": step_sync,
    "discover": step_discover, "propose-column": step_propose_column, "refusal": step_refusal,
    "status": step_status, "sql": step_sql, "catalog": step_catalog, "measure": step_measure,
    "payload": step_payload,
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
    try:
        result = asyncio.run(_amain(args))
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
