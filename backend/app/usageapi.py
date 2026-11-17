"""Usage ledger API (Quick 005). Mounted with the ops key in main.py.

Read-only views over llm_usage (usage.py): totals and breakdowns for the Usage
page, a CSV export, and run / trace timelines (model calls from the ledger plus
tool calls from tool_calls) for System > Runs.
"""
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import usage
from .db import get_pool

router = APIRouter(tags=["usage"])

_INSTALL_RE = re.compile(r"^[A-Za-z0-9 ._:@+-]{1,60}$")
_TRACE_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,120}$")


def _period(period: str) -> str:
    if period not in usage.PERIODS:
        raise HTTPException(422, f"period must be one of {', '.join(usage.PERIODS)}")
    return period


def _install(install: str | None) -> str | None:
    if install in (None, ""):
        return None
    if not _INSTALL_RE.match(install):
        raise HTTPException(422, "install must be 1-60 letters, digits or . _ : @ + - and spaces")
    return install


@router.get("/api/usage/summary")
async def usage_summary(period: str = "month", install: str | None = None):
    pool = await get_pool()
    return await usage.summary(pool, _period(period), _install(install))


@router.get("/api/usage/export.csv")
async def usage_export(period: str = "month", install: str | None = None):
    period = _period(period)
    pool = await get_pool()
    body = await usage.export_csv(pool, period, _install(install))
    name = f"usage-{usage.install_label() if install is None else install}-{period}.csv"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/usage/trace")
async def usage_trace(trace: str):
    if not _TRACE_RE.match(trace or ""):
        raise HTTPException(422, "bad trace key")
    pool = await get_pool()
    return await usage.trace_by_key(pool, trace)


@router.get("/api/runs/{run_id}/trace")
async def run_trace(run_id: int):
    pool = await get_pool()
    return await usage.run_trace(pool, run_id)
