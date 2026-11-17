"""Quick 003: what a reactive run is doing right now, in words the owner reads.

The Ask panel shows one short status line while an answer runs ("Running query
2", "Reading the rules for DailyFuelReports", "Checking the dashboard draft").
`tool_calls` rows are written only when a tool finishes, so the live step comes
from a LangChain callback attached in `graph.run` (callbacks reach delegates,
the same way `TokenBudget` does) plus a few worker notes.

Held in this process only: the gateway is one process and the worker runs in
it. Plumbing, never behaviour: a step is a label for the UI and nothing reads it
back into a run. Labels carry no tool arguments except a table name, a skill
name, a query number or the agent's own send_progress note, and the callback
never raises into the run (`raise_error` stays False).
"""
import itertools
import re
import time
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler

FIRST_STEP = "Reading your question"
THINKING = "Thinking about the results"
REASK_STEP = "Double-checking the figures with a query"
MAX_LIVE = 256
NOTE_CHARS = 90
QUERY_TOOLS = frozenset({"run_sql", "run_sql_to_csv"})
RULES_REFUSAL = "RULES CHECK"

LABELS = {
    "search_business_context": "Looking up the business definitions",
    "discover_schema": "Reading the database schema",
    "get_kpi_history": "Reading the KPI history",
    "propose_forecast": "Proposing the forecast for your approval",  # Phase 13.1
    "get_forecast": "Reading the forecast",  # Phase 13.1
    "get_insight": "Reading the insight",
    "list_dashboard_proposals": "Checking the dashboard drafts waiting for you",
    "list_cases": "Checking the cases",  # Quick 021
    "delegate_to_data_agent": "Asking the data specialist",
    "delegate_to_analysis_agent": "Analysing the numbers",
    "delegate_to_document_agent": "Preparing the document",
    "delegate_to_drawing_agent": "Working on the drawing",
    "delegate_to_dashboard_agent": "Building the dashboard draft",
    "delegate_to_context_agent": "Reading the context pack",
    "get_dashboard": "Opening the current dashboard",
    "preview_dashboard": "Checking the dashboard draft",
    "save_dashboard_version": "Saving the dashboard draft for your approval",
    "create_watch": "Setting up the watch",
    "propose_kpi": "Proposing the KPI for your approval",
    "propose_scheduled_report": "Proposing the daily report for your approval",  # Quick 047
    "propose_context_change": "Proposing a description for your approval",
    "propose_context_changes": "Writing proposals for your approval",
    "propose_context_pack_items": "Writing proposals for your approval",
    "propose_hide_tables": "Writing proposals for your approval",
    "propose_kpi_candidates": "Writing KPI proposals for your approval",
    "read_context_pack": "Reading the context pack",
    "propose_next_action": "Proposing the next step for your approval",
    "run_python": "Running an analysis",
    "list_artifacts": "Looking at the workspace files",
    "web_research": "Searching the web",
    "read_url": "Reading a web page",
    "search_documents": "Searching the documents",
    "get_document": "Reading a document",
    "extract_document": "Reading the document",
    "remember_fact": "Noting that for later",
    "investigate_case": "Investigating the case",
    "send_message_to_person": "Sending the message",
    "send_document": "Sending the file",
    "skill_asset": "Reading a skill file",
    "create_pdf": "Writing the PDF",
    "create_docx": "Writing the document",
    "create_xlsx": "Writing the spreadsheet",
    "create_markdown": "Writing the document",
    "list_drawing_layers": "Reading the drawing layers",
    "deconstruct_drawing": "Splitting the drawing into layers",
    "reconstruct_drawing": "Rebuilding the drawing",
    "diff_drawings": "Comparing the drawings",
}

_live: dict[int, dict] = {}
# Event order (a clock can tie on Windows, where monotonic() ticks every ~15 ms).
_seq = itertools.count()
_current: ContextVar[int | None] = ContextVar("runstatus_current", default=None)
_NAME_RE = re.compile(r"^[A-Za-z0-9_ .$-]{1,63}$")
_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _clip(text, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def sql_tables(sql) -> list[str]:
    """Table names a query reads, in their written case, CTEs excluded; [] if
    it does not parse. Only names that look like identifiers are kept."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
        out: list[str] = []
        for tree in sqlglot.parse(str(sql or ""), dialect="postgres"):
            if tree is None:
                continue
            ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
            for t in tree.find_all(exp.Table):
                name = t.name
                if name and name.lower() not in ctes and name not in out and _NAME_RE.match(name):
                    out.append(name)
        return out
    except Exception:
        return []


def _names(tables: list[str], n: int = 3) -> str:
    shown = tables[:n]
    more = len(tables) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def describe(tool: str, args, query_no: int = 0) -> str:
    """The label for a tool that has just started."""
    a = args if isinstance(args, dict) else {}
    if tool in QUERY_TOOLS:
        return f"Running query {query_no}" if query_no else "Running a query"
    if tool == "send_progress":
        return _clip(a.get("text"), NOTE_CHARS) or THINKING
    if tool == "use_skill":
        name = str(a.get("name") or "").strip()
        return f"Reading the {name} skill" if _SKILL_RE.match(name) else "Reading a skill"
    return LABELS.get(tool) or ("Working: " + tool.replace("_", " ").strip())[:NOTE_CHARS]


# What the agent is doing once a tool has returned: most tools take milliseconds
# and the model then works on the result for seconds, so the finished tool's
# meaning stays on screen until the next tool starts.
AFTER = {
    "search_business_context": "Reading the business definitions",
    "discover_schema": "Reading the database schema",
    "get_dashboard": "Reading the current dashboard",
    "preview_dashboard": "Checking the dashboard draft",
    "save_dashboard_version": "Wrapping up the dashboard draft",
    "get_kpi_history": "Reading the KPI history",
    "get_forecast": "Reading the forecast",  # Phase 13.1
    "read_context_pack": "Reading the context pack",
    "run_python": "Reading the analysis results",
}


def after(tool: str, output, tables: list[str], query_no: int = 0, label: str = "") -> str | None:
    """A label that outlasts a finished tool, until the next tool starts; None
    falls back to the open delegate's label (or THINKING)."""
    text = str(getattr(output, "content", output) or "")
    if tool in QUERY_TOOLS:
        if text.startswith(RULES_REFUSAL):
            return f"Reading the rules for {_names(tables)}" if tables else "Reading the business rules"
        return f"Reading the results of query {query_no}" if query_no else "Reading the query results"
    if tool.startswith("delegate_to_"):
        return "Putting the answer together"
    if tool in ("send_progress", "use_skill"):
        return label or None
    return AFTER.get(tool)


# ── registry ─────────────────────────────────────────────────────────────

def start(run_id) -> None:
    """A consumer claimed run_id. The entry is dropped by release_current()
    when the same consumer finishes, so at most worker_concurrency entries
    outlive their run."""
    if run_id is None:
        return
    while len(_live) >= MAX_LIVE:
        _live.pop(next(iter(_live)))
    _live[run_id] = {"since": time.monotonic(), "queries": 0, "tools": 0,
                     "flight": {}, "after": None}
    _current.set(run_id)


def release_current() -> None:
    rid = _current.get()
    if rid is not None:
        _live.pop(rid, None)
        _current.set(None)


def clear(run_id) -> None:
    _live.pop(run_id, None)


def note(run_id, label: str) -> None:
    """A step the worker knows about (the grounding re-ask)."""
    e = _live.get(run_id)
    if e is not None:
        e["after"] = (next(_seq), label)


def step(run_id) -> str | None:
    """Current label, or None when this process holds nothing for run_id."""
    e = _live.get(run_id)
    if e is None:
        return None
    flight = e["flight"]
    top = max(flight.values(), key=lambda v: v[0]) if flight else None
    last = e["after"]
    if last is not None and (top is None or last[0] >= top[0]):
        if last[1]:
            return last[1]
        return top[1] if top else THINKING
    if top is not None:
        return top[1]
    return THINKING if e["tools"] else FIRST_STEP


class RunStatus(BaseCallbackHandler):
    """Feeds the registry from tool callbacks. Runs inline on the event loop
    (cheap dict updates); any error inside is swallowed by langchain-core."""

    run_inline = True
    raise_error = False

    def __init__(self, run_id: int):
        self.run_id = run_id

    def _entry(self):
        return _live.get(self.run_id)

    def on_tool_start(self, serialized, input_str, *, run_id=None, inputs=None, **kwargs):
        e = self._entry()
        if e is None:
            return
        tool = str((serialized or {}).get("name") or kwargs.get("name") or "tool")
        args = inputs if isinstance(inputs, dict) else {}
        tables: list[str] = []
        if tool in QUERY_TOOLS:
            e["queries"] += 1
            tables = sql_tables(args.get("sql"))
        e["tools"] += 1
        qn = e["queries"] if tool in QUERY_TOOLS else 0
        e["flight"][run_id] = (next(_seq), describe(tool, args, e["queries"]), tool, tables, qn)

    def on_tool_end(self, output, *, run_id=None, **kwargs):
        e = self._entry()
        if e is None:
            return
        f = e["flight"].pop(run_id, None)
        if f is not None:
            e["after"] = (next(_seq), after(f[2], output, f[3], f[4], f[1]))

    def on_tool_error(self, error, *, run_id=None, **kwargs):
        e = self._entry()
        if e is not None and e["flight"].pop(run_id, None) is not None:
            e["after"] = (next(_seq), None)
