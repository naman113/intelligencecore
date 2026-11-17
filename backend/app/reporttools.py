"""Quick 047: propose_scheduled_report only proposes (actions gate). The approved
report runs each day as a cron playbook read and sent by fixed code (reports.py)."""

import time

from langchain_core.tools import tool

from . import reports
from .agents.graph import _log_tool, ctx_phone, ctx_run_id


@tool
async def propose_scheduled_report(title: str, at: str, items: list[dict],
                                   timezone: str = "") -> str:
    """Propose a report sent on WhatsApp EVERY DAY at a set time, for owner approval; nothing is sent until it is approved. Use it for 'send me X every day at 9 PM', 'daily report of X at 21:00', 'message me today's X each evening'. title: a short name. at: HH:MM in 24-hour time (9 PM is 21:00). timezone: only when the user named a zone for the send time (IST is Asia/Kolkata); empty means the business's own zone. 'today' is always the business day on the business's clock. items: 1 to 6, in message order, each ONE of:
- a governed number: {"label", "measure", "range", "filters", "by"}; measure is a name from get_model; range is today, yesterday, last_7_days, last_30_days, this_month, last_month, this_quarter, last_quarter, this_year, year_to_date or last_year; filters as in query_measure; by is at most 2 dimensions for a short breakdown.
- a details table: {"label", "sql"}; one read-only SELECT listing the rows (at most 20 are shown), columns aliased readably, ORDER BY set; write dates as {today}, {yesterday} or {tomorrow}, which become the business day's dates, for example WHERE received_at >= {today} AND received_at < {tomorrow}.
Use a measure for every number a measure covers and a table only for row details. Every item runs once now as a preview, and a failing item refuses the proposal. The report goes to the person asking, in their own WhatsApp chat, or to the owner. Each day it is read and sent by fixed code with no AI run. Reply with the action number, that it awaits the owner's approval on Approvals, and the preview."""
    start = time.monotonic()
    args = {"title": str(title)[:120], "at": str(at)[:20], "timezone": str(timezone)[:64],
            "items": len(items) if isinstance(items, list) else None}
    refused = False
    try:
        try:
            run_id = ctx_run_id.get()
        except LookupError:
            run_id = None
        result = await reports.propose(title, at, items, timezone,
                                       thread_id=ctx_phone.get(), run_id=run_id)
        refused = result.startswith("REFUSED")
    except Exception as e:
        result = f"TOOL ERROR: {type(e).__name__}: {e}"
    await _log_tool("propose_scheduled_report", args, result, start, refused=refused)
    return result
