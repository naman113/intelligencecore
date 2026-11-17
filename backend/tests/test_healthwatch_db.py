"""Real-Postgres check for health_alerts (GOLIVE-05).

Skipped unless GOLIVE_TEST_DATABASE_URL is set and points at a loopback host.
"""
import asyncio
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _URL or (urlsplit(_URL).hostname or "") not in ("127.0.0.1", "localhost"),
    reason="GOLIVE_TEST_DATABASE_URL not set or not loopback",
)

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


def _ddl() -> str:
    m = re.search(r"-- Phase 11: GOLIVE-05 health alerts.*?-- End Phase 11: health alerts",
                  SCHEMA.read_text(encoding="utf-8"), re.S)
    assert m
    return m.group(0)


async def test_one_open_alert_per_component():
    import asyncpg

    from app.healthwatch import OPEN_SQL, RESOLVE_SQL

    name = "ic_hw_" + secrets.token_hex(4)
    p = urlsplit(_URL)
    admin = await asyncpg.connect(_URL)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        pool = await asyncpg.create_pool(
            urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment)),
            min_size=2, max_size=4)
        await pool.execute(_ddl())
        await pool.execute(_ddl())  # idempotent
        res = await asyncio.gather(*(pool.fetchval(OPEN_SQL, "worker", "x") for _ in range(5)))
        assert len([r for r in res if r is not None]) == 1
        assert await pool.fetchval(
            "SELECT count(*) FROM health_alerts WHERE resolved_at IS NULL") == 1
        assert await pool.fetchval(RESOLVE_SQL, "worker") is not None
        assert await pool.fetchval(RESOLVE_SQL, "worker") is None
        assert await pool.fetchval(OPEN_SQL, "worker", "y") is not None
        assert await pool.fetchval("SELECT count(*) FROM health_alerts") == 2
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
