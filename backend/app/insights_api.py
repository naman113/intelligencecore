"""Read-only insights routes for the operator UIs. Approving KPI proposals stays
on /api/actions; this route never runs tenant SQL, it shows stored evidence."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from . import insights

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _check(value: str, allowed: set, name: str) -> None:
    if value not in allowed:
        raise HTTPException(status_code=422,
                            detail=f"{name} must be one of {sorted(allowed)}")


def _clamp(n: int, hi: int = 1000) -> int:
    return max(1, min(int(n), hi))


@router.get("")
async def list_insights_route(origin: str = "exploration", limit: int = 50,
                              before: str | None = None, severity: str | None = None,
                              case: str | None = None):
    _check(origin, insights.ORIGINS, "origin")
    if severity is not None:
        _check(severity, insights.SEVERITY_FILTERS, "severity")
    if case is not None:  # Quick 006: insights whose case is open or closed
        _check(case, insights.CASE_FILTERS, "case")
    when = None
    if before:
        try:
            when = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="before must be an ISO-8601 timestamp")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    return await insights.list_insights(origin=origin, limit=_clamp(limit, insights.MAX_LIST),
                                        before=when, severity=severity, case=case)


@router.get("/summary")
async def insights_summary_route(days: int = 30):
    """Quick 006: counts by severity, open insight cases and the last exploration run,
    for the Insights page summary strip."""
    return await insights.insights_summary(days=max(1, min(int(days), 400)))
