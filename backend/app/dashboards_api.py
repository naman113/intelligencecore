"""Phase 3 dashboards ops routes (DASH-04).

Operator/test seams with no agent involved: list, inspect, create a draft,
publish, roll back and rotate the view token. Auth is applied at include time in
main.py (`include_router(..., dependencies=[Depends(_require_ops_key)])`). This
module must never import the gateway module (circular).

Phase 18: the operator route is spec-only. New versions are chart specs, a body with code
is refused, and Phase 8's rule still holds (no UI editor posts versions; UI creation goes
through the agent).

Stored code is returned only inside JSON. No route here may serve it as HTML:
the ops key sits in localStorage on the gateway and UI origins, so agent-authored
HTML rendered there could steal it. Per Assumption A2, Phase 8 must not wire a UI
editor to POST /versions (UI creation goes through the agent).

Phase 6 adds the lint-gated preview mint (draft link only after dashboardlint passes).
Returning the token to an ops-key holder is intended: they can mint anyway.
"""

from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, HTTPException

from . import dashboardlint, dashboardrender, dashboards, dashboardspec, db

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


def _http(e: dashboards.DashboardError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="conflicting concurrent change; retry")


@router.get("")
async def api_list_dashboards(limit: int = 100):
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.list_dashboards(conn, limit=limit)
    except dashboards.DashboardError as e:
        raise _http(e)


@router.get("/{slug}")
async def api_get_dashboard(slug: str):
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.get_dashboard(conn, slug=slug)
    except dashboards.DashboardError as e:
        raise _http(e)


@router.get("/{slug}/versions/{n}")
async def api_get_dashboard_version(slug: str, n: int):
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.get_version(conn, slug=slug, version=n)
    except dashboards.DashboardError as e:
        raise _http(e)


@router.post("/{slug}/versions")
async def api_save_dashboard_version(slug: str, body: dict | None = None):
    """Phase 18: operator spec drafts only. A body with code is refused."""
    body = body or {}
    if "code" in body:
        raise HTTPException(
            status_code=422,
            detail="new dashboard versions are chart specs; send {spec, change_note}")
    if body.get("spec") is None:
        raise HTTPException(status_code=422, detail="spec is required: send {spec, change_note}")
    pool = await db.get_pool()
    try:
        prepared = await dashboards.prepare_spec(body["spec"])
        async with pool.acquire() as conn:
            res = await dashboards.save_spec_version(
                conn, slug=slug, stored=prepared["stored"], queries=prepared["queries"],
                title=prepared["title"], renderer=prepared.get("renderer") or dashboardrender.CURRENT_RENDERER,
                change_note=body.get("change_note"), author="operator", task_run_id=None)
    except dashboards.SpecRejected as e:
        raise HTTPException(status_code=422, detail={"findings": e.findings[:20]})
    except dashboards.DashboardError as e:
        raise _http(e)
    except asyncpg.exceptions.IntegrityConstraintViolationError:
        raise _conflict()
    return {k: res[k] for k in ("dashboard_id", "version_id", "version", "status",
                                "created", "path", "format")}


async def _set_version(slug: str, body: dict | None, op: str) -> dict:
    body = body or {}
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.operator_set_version(
                conn, slug=slug, version=body.get("version"), op=op,
                decided_by=body.get("decided_by"), note=body.get("note"))
    except dashboards.DashboardError as e:
        raise _http(e)
    except asyncpg.exceptions.IntegrityConstraintViolationError:
        raise _conflict()


@router.post("/{slug}/publish")
async def api_publish_dashboard(slug: str, body: dict | None = None):
    return await _set_version(slug, body, "publish")


@router.post("/{slug}/rollback")
async def api_rollback_dashboard(slug: str, body: dict | None = None):
    return await _set_version(slug, body, "rollback")


@router.post("/{slug}/layout")
async def api_layout_dashboard(slug: str, body: dict | None = None):
    """Phase 22 (DCAP-15): owner layout tweak saved as an operator draft plus an audit row."""
    body = body or {}
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.save_layout_tweak(
                conn, slug=slug, base_version=body.get("base_version"), changes=body,
                decided_by=body.get("decided_by"))
    except dashboardspec.SpecError as e:
        raise HTTPException(status_code=422, detail={"findings": e.findings[:20]})
    except dashboards.DashboardError as e:
        raise _http(e)
    except asyncpg.exceptions.IntegrityConstraintViolationError:
        raise _conflict()


@router.post("/{slug}/rotate-token")
async def api_rotate_dashboard_token(slug: str):
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await dashboards.rotate_token(conn, slug=slug)
    except dashboards.DashboardError as e:
        raise _http(e)


# ── Phase 6: lint-gated draft preview link (contract §5, D3) ──
@router.post("/{slug}/versions/{n}/preview")
async def api_mint_dashboard_preview(slug: str, n: int, body: dict | None = None):
    ttl = (body or {}).get("ttl_minutes", dashboards.PREVIEW_TTL_DEFAULT)
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            v = await dashboards.get_version(conn, slug=slug, version=n)
            if v["status"] != "draft":
                raise dashboards.DashboardError(409, "preview links are minted only for drafts")
            if v.get("format") == "spec":  # Phase 18: re-validate the stored spec, no lint
                try:
                    dashboardspec.parse(v["spec"])
                except dashboardspec.SpecError as e:
                    raise HTTPException(status_code=422,
                                        detail={"findings": e.findings[:20]})
            else:
                report = dashboardlint.lint(v["code"], v["queries"])
                if report["errors"]:
                    raise HTTPException(status_code=422,
                                        detail={"findings": report["errors"][:20]})
            out = await dashboards.mint_preview_token(
                conn, slug=slug, version=n, ttl_minutes=ttl)
    except dashboards.DashboardError as e:
        raise _http(e)
    out["url"] = out["path"] + "?p=" + out["preview_token"]
    created = v["created_at"]
    out["refreshing"] = bool(created and created > datetime.now(timezone.utc) - timedelta(hours=24))
    return out
