"""Phase 13.1: ops API for forecast definitions (read, pause/resume/retire, run now). Creating a forecast is never a route: the owner asks the agent, which proposes one for approval."""

from fastapi import APIRouter, HTTPException

from . import forecasts

router = APIRouter(prefix="/api/forecasts", tags=["forecasts"])

MAX_ID = 2 ** 63 - 1


def _id(forecast_id: int) -> int:
    if not 1 <= forecast_id <= MAX_ID:
        raise HTTPException(status_code=404, detail="forecast not found")
    return forecast_id


@router.get("")
async def list_forecasts_route(limit: int = 50, status: str | None = None):
    limit = max(1, min(int(limit), 200))
    if status is not None and status != "all":
        raise HTTPException(status_code=422, detail="status must be 'all' or omitted")
    return await forecasts.list_brief(limit=limit, include_retired=status == "all")


@router.get("/{forecast_id}")
async def get_forecast_route(forecast_id: int):
    out = await forecasts.detail(_id(forecast_id))
    if out is None:
        raise HTTPException(status_code=404, detail="forecast not found")
    return out


@router.patch("/{forecast_id}")
async def patch_forecast_route(forecast_id: int, body: dict):
    status = body.get("status") if isinstance(body, dict) else None
    if set(body or {}) != {"status"} or status not in forecasts.STATUSES:
        raise HTTPException(status_code=422,
                            detail="body must be {'status': 'active'|'paused'|'retired'}")
    try:
        return await forecasts.set_status(_id(forecast_id), status)
    except LookupError:
        raise HTTPException(status_code=404, detail="forecast not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{forecast_id}/run")
async def run_forecast_route(forecast_id: int):
    try:
        return await forecasts.request_run(_id(forecast_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="forecast not found")
    except forecasts.RunTooSoon as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
