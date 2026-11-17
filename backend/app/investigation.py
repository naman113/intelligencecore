"""Investigation — federated search across all sources, exposed as a tool.

`investigate_case` answers a question by searching everything we know:
  * events  — full-text search over WhatsApp/email/ERP history (tsvector index),
  * memory  — the Graphiti knowledge graph (durable facts),
  * tenant  — read-only SQL against the business DB (via the guardrail path).

It writes a sourced findings note back onto the case as a synthetic `system`
event, so the investigation is preserved in the case timeline. This is the
"conduct investigation from all sources" requirement as a supervisor tool, not a
separate pipeline (plan §8.4).
"""

import json
import logging
import time

from langchain_core.tools import tool

from .agents.graph import _log_tool, ctx_run_id
from .db import get_pool
from .guardrails import SQLGuardrailError, run_readonly_sql
from .memoryservice import fact_line, memory_graph
from .playbook_tools import ctx_case_id

log = logging.getLogger("investigation")


async def _search_events(query: str, limit: int = 12) -> list[dict]:
    """Full-text search over event content (english tsvector). Falls back to a
    trigram-ish ILIKE if FTS yields nothing.

    Test-thread events are excluded (same `test_threads` setting the triage
    sandbox uses): they are provenance='external' like any chat message, so
    without the filter a real user's question could surface fictional E2E
    messages as sourced "history hits" — and investigate_case writes its
    findings back onto the REAL current case."""
    from .triage import _test_threads   # lazy: triage pulls heavy deps at call time
    excluded = sorted(_test_threads())
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT e.id, e.source, e.occurred_at, e.content,
                  COALESCE(p.name, e.actor_raw) AS actor
           FROM events e LEFT JOIN people p ON p.id = e.actor_id
           WHERE e.provenance = 'external'
             AND (e.thread_ref IS NULL OR e.thread_ref <> ALL($3::text[]))
             AND to_tsvector('english', e.content) @@ plainto_tsquery('english', $1)
           ORDER BY ts_rank(to_tsvector('english', e.content), plainto_tsquery('english', $1)) DESC,
                    e.occurred_at DESC
           LIMIT $2""", query, limit, excluded)
    if not rows:
        rows = await pool.fetch(
            """SELECT e.id, e.source, e.occurred_at, e.content,
                      COALESCE(p.name, e.actor_raw) AS actor
               FROM events e LEFT JOIN people p ON p.id = e.actor_id
               WHERE e.provenance='external' AND e.content ILIKE '%'||$1||'%'
                 AND (e.thread_ref IS NULL OR e.thread_ref <> ALL($3::text[]))
               ORDER BY e.occurred_at DESC LIMIT $2""", query, limit, excluded)
    return [dict(r) for r in rows]


@tool
async def investigate_case(question: str, sql: str = "") -> str:
    """Investigate a question across ALL sources: message/email/ERP history
    (full-text), the knowledge graph, and — if you pass a read-only SELECT in the
    `sql` argument — the tenant business database. Returns a sourced findings
    summary and records it on the current case. Use for questions like
    'who hasn't submitted their expenses this quarter?'."""
    start = time.monotonic()
    findings: dict = {"question": question, "events": [], "memory": [], "sql": None}

    events = await _search_events(question)
    findings["events"] = [
        {"event": e["id"], "source": e["source"], "actor": e["actor"],
         "when": e["occurred_at"].isoformat() if e["occurred_at"] else None,
         "excerpt": (e["content"] or "")[:200]}
        for e in events]

    findings["memory"] = await memory_graph.search(question, limit=8)

    if sql.strip():
        try:
            rows = await run_readonly_sql(sql)
            findings["sql"] = {"query": sql, "rows": rows[:50]}
        except SQLGuardrailError as e:
            findings["sql"] = {"query": sql, "error": f"guardrail rejected: {e}"}
        except Exception as e:
            findings["sql"] = {"query": sql, "error": f"{type(e).__name__}: {e}"}

    note = _render(findings)
    await _record_finding(note)
    result = note[:6000]
    await _log_tool("investigate_case", {"question": question, "sql": sql[:300]}, result, start)
    return result


def _render(f: dict) -> str:
    lines = [f"INVESTIGATION: {f['question']}", ""]
    if f["events"]:
        lines.append(f"From message/email/ERP history ({len(f['events'])} hits):")
        for e in f["events"]:
            lines.append(f"  • [{e['source']}] {e['actor']} @ {e['when']}: {e['excerpt']}")
    else:
        lines.append("From history: no matching events.")
    if f["memory"]:
        lines.append("\nFrom knowledge graph:")
        lines += [f"  • {fact_line(m)}" for m in f["memory"]]
    if f["sql"] is not None:
        lines.append("\nFrom business DB:")
        if "error" in f["sql"]:
            lines.append(f"  • SQL error: {f['sql']['error']}")
        else:
            lines.append(f"  • {len(f['sql']['rows'])} row(s): "
                         + json.dumps(f["sql"]["rows"][:10], default=str)[:1500])
    return "\n".join(lines)


async def _record_finding(note: str) -> None:
    """Attach the findings to the current case as a synthetic system event.

    Delegates to cases_util.add_case_note — the single sanctioned write path
    for case knowledge (findings now also count toward the rolling-summary
    refresh, so an investigated case resummarizes with its findings folded in).
    """
    case_id = ctx_case_id.get()
    if not case_id:
        return
    from . import cases_util
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await cases_util.add_case_note(conn, case_id, note,
                                           kind="investigation", cap=8000)
