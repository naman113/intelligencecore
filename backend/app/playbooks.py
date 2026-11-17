"""Playbook engine — runs a playbook as a task on the existing queue (plan §7.2).

A violated expectation (or a cron/erp trigger) enqueues a task_runs row with
kind='playbook'. The worker recognizes it and calls run_playbook_task(), which:

  * loads the playbook (goal, tier, tool grants),
  * assembles context (case summary + expectation + graph memory),
  * builds a ReAct agent restricted to the granted tools,
  * sets the tier contextvar so the tools enforce autonomy,
  * runs under a hard token budget,
  * records outcome on the task_run.

Tools do the side effects and enforce tiers (see playbook_tools). Reusing the
task queue gives us crash recovery, tool-call logging, and concurrency for free.
"""

import logging
import time
from datetime import datetime, timezone

from langgraph.prebuilt import create_react_agent

from . import llmclient
from . import playbook_tools as pt
from .agents import graph
from .budget import BudgetExceeded, TokenBudget
from .config import get_settings
from .db import get_pool
from .memoryservice import GLOBAL_GROUP, case_group, fact_line, memory_graph

log = logging.getLogger("playbooks")

PLAYBOOK_SYSTEM = (
    "You autonomously execute one operational playbook for an internal ops team. "
    "Follow the GOAL exactly, using ONLY your granted tools. Keep messages concise, "
    "specific, and in the case's tone. Your autonomy is enforced by the tools "
    "themselves — if a tool refuses on tier grounds, respect it and use an allowed "
    "path instead. When the goal is achieved, stop and briefly state what you did."
)


# TokenBudget / BudgetExceeded live in budget.py; imported above so existing
# `from app.playbooks import TokenBudget, BudgetExceeded` keeps working.

# ── tool registry (playbook tools + a few read tools from the graph) ───────

def _playbook_registry() -> dict:
    reg = dict(pt.PLAYBOOK_TOOL_REGISTRY)
    reg["search_business_context"] = graph.search_business_context
    reg["run_sql"] = graph.run_sql
    reg["send_progress"] = graph.send_progress
    # Read-only — they return information and write no actions row — so tiers,
    # which govern side effects, do not gate them. The grant IS the gate, and it
    # is a real one: unlike run_sql, the model chooses the destination, so a
    # granted playbook has model-controlled outbound egress at ANY tier (the URL
    # itself is data leaving the building). They stay off unless a playbook's
    # tool_grants name them.
    reg["web_research"] = graph.web_research
    reg["read_url"] = graph.read_url
    # Read-only (returns procedure text, writes nothing) — the grant is the gate.
    reg["use_skill"] = graph.use_skill
    # Phase 5: read-only KPI history; the grant is the gate (Phase 7 exploration grants it).
    reg["get_kpi_history"] = graph.get_kpi_history
    # Phase 7: compute + export for exploration. No side effect outside the run's
    # sandbox/scratch dir, so no tier gates them; the grant IS the gate (same as
    # get_kpi_history / use_skill).
    reg["run_python"] = graph.run_python
    reg["run_sql_to_csv"] = graph.run_sql_to_csv
    # Phase 17 (CONS-04): read-only model tools; the grant is the gate
    try:
        from . import modeltools
        reg["get_model"] = modeltools.get_model
        reg["query_measure"] = modeltools.query_measure
    except Exception as e:
        log.warning("model tools not registered: %s", type(e).__name__)
        pass
    # Phase 22 (DCAP-16): read-only dashboard digest from stored snapshots; the grant is the gate
    try:
        from . import dashdigesttools
        reg["dashboard_digest"] = dashdigesttools.dashboard_digest
    except Exception as e:
        log.warning("dashboard digest tool not registered: %s", type(e).__name__)
    try:
        from .investigation import investigate_case  # Phase 4 (optional)
        reg["investigate_case"] = investigate_case
    except Exception:
        pass
    return reg


# ── enqueue ────────────────────────────────────────────────────────────────

async def enqueue_playbook_run(playbook_id: int, case_id: int | None = None,
                               expectation_id: int | None = None) -> int | None:
    """Queue a playbook execution as a task. Returns the task_run id."""
    pool = await get_pool()
    thread_id = f"case-{case_id}" if case_id else "playbook-global"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO threads (thread_id, phone) VALUES ($1, $1) ON CONFLICT DO NOTHING",
                thread_id)
            msg_id = await conn.fetchval(
                """INSERT INTO messages (thread_id, role, content)
                   VALUES ($1, 'system', $2) RETURNING id""",
                thread_id, f"playbook run: playbook={playbook_id} case={case_id} exp={expectation_id}")
            run_id = await conn.fetchval(
                """INSERT INTO task_runs (thread_id, message_id, kind, playbook_id, case_id, expectation_id)
                   VALUES ($1, $2, 'playbook', $3, $4, $5) RETURNING id""",
                thread_id, msg_id, playbook_id, case_id, expectation_id)
    log.info("enqueued playbook %s run (task %s, case=%s, exp=%s)",
             playbook_id, run_id, case_id, expectation_id)
    return run_id


async def enqueue_playbook_run_once(playbook_id: int) -> tuple[int, bool]:
    """Quick 023: queue a run unless one of this playbook is already queued or
    running. Returns (run_id, created); when not created, run_id is the run in
    flight. Race-safe: a transaction-scoped advisory lock per playbook
    serialises the check and the insert, so two clicks (or a click and the
    nightly cron) can never queue two runs. The lock key is playbook_id, so
    different playbooks never wait on each other."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('playbook_run'), $1)",
                               int(playbook_id))
            active = await conn.fetchval(
                """SELECT id FROM task_runs
                    WHERE kind = 'playbook' AND playbook_id = $1
                      AND status IN ('queued', 'running') ORDER BY id LIMIT 1""",
                playbook_id)
            if active is not None:
                return int(active), False
            await conn.execute(
                "INSERT INTO threads (thread_id, phone) VALUES ('playbook-global', 'playbook-global') "
                "ON CONFLICT DO NOTHING")
            msg_id = await conn.fetchval(
                """INSERT INTO messages (thread_id, role, content)
                   VALUES ('playbook-global', 'system', $1) RETURNING id""",
                f"playbook run: playbook={playbook_id} case=None exp=None")
            run_id = await conn.fetchval(
                """INSERT INTO task_runs (thread_id, message_id, kind, playbook_id)
                   VALUES ('playbook-global', $1, 'playbook', $2) RETURNING id""",
                msg_id, playbook_id)
    log.info("enqueued playbook %s run (task %s, once)", playbook_id, run_id)
    return int(run_id), True


# ── execute (called by the worker) ─────────────────────────────────────────

def _pb_int(pb, key: str) -> int | None:
    """Optional positive per-playbook limit (Phase 7); None when absent or unset."""
    try:
        v = pb[key]
    except (KeyError, IndexError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


async def run_playbook_task(task: dict) -> None:
    """Execute a claimed kind='playbook' task_run. Updates task_runs status."""
    run_id = task["id"]
    pool = await get_pool()
    pb = await pool.fetchrow("SELECT * FROM playbooks WHERE id = $1", task["playbook_id"])
    if not pb or pb["status"] != "active":
        await pool.execute(
            "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1",
            run_id, "playbook missing or not active")
        return

    # Quick 047: a scheduled report is read and sent by fixed code; no agent, no model call.
    from . import reports
    if reports.report_of(pb) is not None:
        await reports.run(task, pb)
        return

    context = await _assemble_context(task, pb)
    reg = _playbook_registry()
    tools = [reg[t] for t in pb["tool_grants"] if t in reg]

    # Tier + case context for the tool layer to enforce autonomy.
    graph.ctx_run_id.set(run_id)
    graph.ctx_phone.set(get_settings().owner_number or None)  # progress notes go to owner
    # Stamp this run's start alongside run_id/phone: contextvars persist across
    # the worker loop's iterations, so without a fresh set a value left by a
    # prior kind='user' run on the same consumer task would leak into any tool
    # that gates on it (send_document's provenance note, should a file-sending
    # tool ever be granted to playbooks).
    graph.ctx_run_started.set(datetime.now(timezone.utc))
    pt.ctx_tier.set(pb["autonomy_tier"])
    pt.ctx_case_id.set(task["case_id"])
    pt.ctx_playbook_id.set(pb["id"])
    pt.ctx_expectation_id.set(task["expectation_id"])

    model = _model()
    agent = create_react_agent(model, tools, prompt=_system_prompt(model))  # Quick 035
    budget = TokenBudget(_pb_int(pb, "token_budget") or get_settings().playbook_token_budget)
    cb = graph._callbacks()
    callbacks = [budget, *cb.get("callbacks", [])]
    try:
        out = await agent.ainvoke(
            {"messages": [("user", context)]},
            config={"recursion_limit": _pb_int(pb, "recursion_limit") or 25,
                    "callbacks": callbacks})
        # Phase 12 (CTX-09e): a refusal is a failed run, not an empty "nothing to report"
        if llmclient.is_refusal(out["messages"][-1]):
            await pool.execute(
                "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1",
                run_id, "model declined (refusal)")
            log.warning("playbook %s run %s: model declined (refusal)", pb["name"], run_id)
            return
        summary = out["messages"][-1].content
        if isinstance(summary, list):
            summary = " ".join(b.get("text", "") for b in summary if isinstance(b, dict))
        await pool.execute(
            "UPDATE task_runs SET status='done', ended_at=now(), result_summary=$2 WHERE id=$1",
            run_id, str(summary)[:500])
        log.info("playbook %s run %s done", pb["name"], run_id)
    except BudgetExceeded as e:
        await pool.execute(
            "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1",
            run_id, str(e))
        owner = get_settings().owner_number
        if owner:
            from . import whatsapp
            await whatsapp.send_message(owner, f"⚠️ Playbook '{pb['name']}' hit its token budget and was stopped.")
        log.warning("playbook %s run %s aborted: %s", pb["name"], run_id, e)
    except Exception as e:
        await pool.execute(
            "UPDATE task_runs SET status='failed', ended_at=now(), error=$2 WHERE id=$1",
            run_id, f"{type(e).__name__}: {e}")
        log.exception("playbook %s run %s failed", pb["name"], run_id)
    finally:
        from . import livebudget  # Phase 17 (CONS-04)
        livebudget.forget(run_id)  # Phase 17 (CONS-04)


async def _assemble_context(task: dict, pb) -> str:
    pool = await get_pool()
    parts = [f"PLAYBOOK: {pb['name']}", f"GOAL: {pb['goal']}"]
    if pb["escalation"]:
        parts.append(f"ESCALATION POLICY: {pb['escalation']}")

    case_id = task["case_id"]
    # Quick 014: a case lives once, in global (its close episode); a legacy
    # case_<id> graph is still read when it exists. recall skips missing graphs.
    scope = [case_group(case_id), GLOBAL_GROUP] if case_id else None
    if case_id:
        case = await pool.fetchrow("SELECT title, summary FROM cases WHERE id = $1", case_id)
        if case:
            parts.append(f"\nCASE #{case_id}: {case['title']}")
            if case["summary"]:
                parts.append(f"SUMMARY: {case['summary']}")

    if task["expectation_id"]:
        e = await pool.fetchrow(
            """SELECT e.description, e.due_at, e.reminder_count, e.satisfy_hint, p.name AS owner
               FROM expectations e LEFT JOIN people p ON p.id = e.owner_id WHERE e.id = $1""",
            task["expectation_id"])
        if e:
            parts.append(
                f"\nOVERDUE EXPECTATION: {e['description']}\n"
                f"  owner: {e['owner'] or 'UNASSIGNED'} | due: {e['due_at']} | "
                f"reminders sent so far: {e['reminder_count']} | satisfies when: {e['satisfy_hint'] or 'n/a'}")

    facts = await memory_graph.search(pb["goal"], scope=scope, limit=6)
    if facts:
        parts.append("\nRELEVANT MEMORY:\n" + "\n".join(f"- {fact_line(f)}" for f in facts))

    # Only when the playbook is actually granted use_skill — an ungranted
    # agent would just be teased with a tool it cannot call. Fail-soft: a
    # skills-table hiccup must not cost the run its context.
    if "use_skill" in (pb["tool_grants"] or []):
        try:
            from . import skillservice
            slugs = await skillservice.enabled_slugs()
            if slugs:
                parts.append("\nEnabled skills (load with use_skill): " + ", ".join(slugs))
        except Exception:
            pass

    # Phase 7: exploration context (approved tables, already recorded, already watched). Only when the playbook can record insights; fail-soft like the skills line, so a context hiccup never costs the run.
    grants = pb["tool_grants"] or []
    if "record_insight" in grants:
        try:
            from . import insights
            parts.append("\n" + await insights.exploration_context(include_kpis="propose_kpi" in grants))
        except Exception:
            pass

    parts.append("\nExecute the goal now using only your granted tools.")
    return "\n".join(parts)


def _model():
    settings = get_settings()
    if settings.anthropic_api_key:
        return llmclient.chat_anthropic(settings.supervisor_model, max_tokens=2000,
                                        temperature=0.2, api_key=settings.anthropic_api_key,
                                        role="supervisor", settings=settings,  # Phase 13 (GOLIVE-01)
                                        conversation_cache=llmclient.loop_cache_on(settings))  # Quick 035
    return llmclient.chat_openai(settings.openai_supervisor_model, temperature=0.2,
                                 api_key=settings.openai_api_key, role="supervisor")  # Quick 005


def _system_prompt(model):
    """Quick 035: PLAYBOOK_SYSTEM with the chat graph's system breakpoint, so the
    tools + system prefix (about 2.4k tokens for exploration's grants) is read from
    cache on every step after the first; beside the conversation cache _model()
    turns on, it is the chat graph's pair (graph._mk). Off (prompt_cache_loops) or
    on OpenAI: the plain string, as before. Tool order is the playbook's
    tool_grants order, fixed for the run."""
    if not llmclient.loop_cache_on(get_settings()):
        return PLAYBOOK_SYSTEM
    return graph._cached_prompt(PLAYBOOK_SYSTEM, model)
