import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global _pool
    conninfo = os.environ["CLAUSTRUM_DB_URL"]
    _pool = AsyncConnectionPool(conninfo=conninfo, min_size=2, max_size=10, open=False)
    await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call open_pool() in lifespan.")
    return _pool


@asynccontextmanager
async def conn() -> AsyncIterator:
    async with pool().connection() as c:
        yield c
