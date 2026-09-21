"""One pool, one transaction helper. Every request is exactly one transaction."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool(dsn: str) -> ConnectionPool:
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = ConnectionPool(dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


@contextmanager
def tx() -> Iterator[Cursor]:
    """Yields a cursor inside a transaction. Commit on clean exit, rollback on exception."""
    with pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur
