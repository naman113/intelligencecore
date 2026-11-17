"""Phase 17 (CONS-04): per-run budget of queries on live tables for unattended runs.
Enforced in tool code; chat runs are not limited. Fails closed: an unresolved
statement counts as live.

``check`` compares and reserves with no await in between, so the budget holds inside
one parallel tool batch (one event loop).
"""
from collections import OrderedDict

from . import freshness
from .config import get_settings

REFUSAL = (
    "LIVE QUERY BUDGET SPENT: this unattended run has used its {n} queries on live tables "
    "(they read the client's production database). Continue with imported tables or governed "
    "measures over them, record what you have, and list these live tables in your final reply "
    "as candidates to import (the owner can ask in chat to switch them): {tables}")
UNRESOLVED = "(unresolved statement)"
MAX_RUNS = 256

_used: "OrderedDict[int, int]" = OrderedDict()


async def check(sql: str) -> tuple[str | None, list[str]]:
    """Returns (refusal | None, live tables read). Counts only inside a playbook run."""
    from . import playbook_tools
    from .agents import graph
    run_id = graph.ctx_run_id.get()
    if playbook_tools.ctx_playbook_id.get() is None or run_id is None:
        return None, []
    f = await freshness.for_sql(sql)
    live = list(f.live_reads)
    if f.failed and not live:
        live = [UNRESOLVED]          # never fail open
    if not live:
        return None, []
    # No await below: read and reserve are one atomic step.
    budget = max(0, int(get_settings().playbook_live_query_budget))
    used = _used.get(run_id, 0)
    if used >= budget:
        return REFUSAL.format(n=budget, tables=", ".join(live)), live
    _used[run_id] = used + 1
    _used.move_to_end(run_id)
    while len(_used) > MAX_RUNS:
        _used.popitem(last=False)
    return None, live


def forget(run_id) -> None:
    _used.pop(run_id, None)
