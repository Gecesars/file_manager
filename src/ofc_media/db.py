from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings


_pool: ConnectionPool | None = None


def pool(settings: Settings | None = None) -> ConnectionPool:
    global _pool
    if _pool is None:
        selected = settings or Settings.from_env()
        _pool = ConnectionPool(
            selected.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Fecha explicitamente workers do pool em comandos de vida curta."""
    global _pool
    selected = _pool
    _pool = None
    if selected is not None:
        selected.close()


@contextmanager
def connection(settings: Settings | None = None) -> Iterator[Connection]:
    with pool(settings).connection() as database:
        yield database
