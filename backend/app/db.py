"""Working-memory database (local Postgres + pgvector)."""

from pathlib import Path

import asyncpg

from .config import get_settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            get_settings().memory_database_url,
            min_size=1, max_size=10,
            # Railway closes idle connections; recycle them before they go stale
            # so long-lived loops (worker/sentinel) don't hit "connection closed".
            max_inactive_connection_lifetime=120,
        )
    return _pool


async def init_schema() -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
