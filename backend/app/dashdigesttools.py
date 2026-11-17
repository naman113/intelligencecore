"""dashboard_digest tool (Phase 22, DCAP-16): a dashboard's stat tiles as plain text.

Read-only and memory-DB only: the published version's spec and its stored snapshots. It never
queries business data (the viewer-path invariant) and writes no actions row. The text is meant to
be sent unchanged with notify_owner by the seeded, owner-activated playbook.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from langchain_core.tools import tool

from . import dashboarddigest, dashboards, dashboardspec, dashsettings, kpihistory
from .agents.graph import _log_tool
from .db import get_pool

log = logging.getLogger(__name__)

MAX_DASHBOARDS = 3
MAX_CHARS = 3000

_PUBLISHED_ONE = (
    "SELECT d.slug, d.title, v.id AS version_id, v.spec, v.queries FROM dashboards d "
    "JOIN dashboard_versions v ON v.id = d.current_version_id WHERE d.slug = $1")
_PUBLISHED_ALL = (
    "SELECT d.slug, d.title, v.id AS version_id, v.spec, v.queries FROM dashboards d "
    "JOIN dashboard_versions v ON v.id = d.current_version_id "
    "WHERE v.spec IS NOT NULL ORDER BY v.published_at DESC NULLS LAST, d.id DESC LIMIT 50")
_SNAPSHOTS = (
    "SELECT query_id, data, data_as_of, day_timezone, error, refreshed_at "
    "FROM dashboard_snapshots WHERE version_id = $1")


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


async def _digest_one(pool, row, now_utc: datetime) -> list:
    spec = dashboards._decode_json(row["spec"])
    if not isinstance(spec, dict):
        return []
    queries = dashboards._decode_json(row["queries"]) or []
    form = dashboardspec.render_spec(spec, queries)
    if form.get("render_version") == 1 and not form.get("number_locale"):
        form["number_locale"] = dashsettings.number_locale()  # a v1 form carries none (C-15)
    snaps = {}
    for s in await pool.fetch(_SNAPSHOTS, row["version_id"]):
        data = dashboards._decode_json(s["data"]) or []
        snaps[s["query_id"]] = {"rows": data, "data_as_of": _iso(s["data_as_of"] or s["refreshed_at"]),
                                "timezone": s["day_timezone"], "error": s["error"],
                                "refreshed_at": _iso(s["refreshed_at"])}
    return dashboarddigest.digest_lines(form, snaps, now_utc, default_tz=kpihistory._tz())


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    cut = len(text) - MAX_CHARS
    return text[:MAX_CHARS].rstrip() + f"\n(cut: {cut} more characters)"


@tool
async def dashboard_digest(slug: str | None = None) -> str:
    """Plain text of a published dashboard's stat tiles (the numbers, with change versus the previous period), read from stored snapshots only. Give a slug for one dashboard, or none for up to 3 published dashboards. Send the result unchanged with notify_owner for a WhatsApp digest."""
    start = time.monotonic()
    args = {"slug": slug}
    try:
        pool = await get_pool()
        now = datetime.now(timezone.utc)
        if slug:
            rows = await pool.fetch(_PUBLISHED_ONE, slug)
            if not rows:
                result = f"NOT FOUND: no published dashboard {slug}"
            else:
                lines = await _digest_one(pool, rows[0], now)
                result = _cap("\n".join(lines)) if lines else f"NOT FOUND: {slug} has no stat tiles"
        else:
            blocks = []
            for row in await pool.fetch(_PUBLISHED_ALL):
                try:
                    lines = await _digest_one(pool, row, now)
                except Exception as e:  # one bad dashboard must not stop the others
                    log.warning("dashboard_digest skipped %s: %s", row["slug"], type(e).__name__)
                    continue
                if lines:
                    blocks.append("\n".join(lines))
                if len(blocks) >= MAX_DASHBOARDS:
                    break
            result = _cap("\n\n".join(blocks)) if blocks else "NOT FOUND: no published dashboard with stat tiles"
    except Exception as e:
        log.warning("dashboard_digest failed: %s", type(e).__name__)
        result = f"TOOL ERROR: {type(e).__name__}"
    await _log_tool("dashboard_digest", args, result[:1000], start,
                    refused=result.startswith("NOT FOUND"))
    return result
