import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"],
            min_size=2,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0,  # required for pgbouncer transaction-mode pooling
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def fetchrow(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def get_user_by_token(user_token: str) -> asyncpg.Record | None:
    return await fetchrow(
        "SELECT * FROM users WHERE user_token = $1",
        user_token,
    )


async def get_sites_for_user(user_id: str) -> list[asyncpg.Record]:
    return await fetch(
        "SELECT * FROM sites WHERE user_id = $1 ORDER BY created_at",
        user_id,
    )
