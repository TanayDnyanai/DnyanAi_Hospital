"""SQLite locking helpers shared by the local data sources."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_BUSY_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)

DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 10_000
DB_WRITE_ATTEMPTS = 4
DB_RETRY_BASE_DELAY_SECONDS = 0.02


def is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    """Return True when SQLite could not obtain a write lock."""
    message = str(exc).lower()
    return any(fragment in message for fragment in _BUSY_MESSAGES)


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply the same lock-wait policy to every calendar connection."""
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def retry_sqlite_write(
    operation: Callable[[], T],
    *,
    attempts: int = DB_WRITE_ATTEMPTS,
    base_delay: float = DB_RETRY_BASE_DELAY_SECONDS,
) -> T:
    """Retry a short SQLite write when another connection holds the lock."""
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover
