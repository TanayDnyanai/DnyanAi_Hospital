"""LangGraph persistence backend selection for the medical receptionist.

``MEMORY_BACKEND=neon`` uses PostgresSaver/PostgresStore backed by the same
Neon project as the application data. The default remains RAM-only so legacy
behaviour is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from env_config import load_app_env

RECEPTIONIST_DIR = Path(__file__).resolve().parent
WORKSPACE = RECEPTIONIST_DIR.parent

load_app_env()

MEMORY_BACKEND = os.getenv("MEMORY_BACKEND", "legacy").strip().lower()

_POOL: Any = None
_CHECKPOINTER: Any = None
_STORE: Any = None


def _database_url() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_UNPOOLED")
    if not dsn:
        raise RuntimeError("MEMORY_BACKEND=neon requires DATABASE_URL in .env.local")
    return dsn


def _pool() -> Any:
    global _POOL
    if _POOL is None:
        from psycopg import rows
        from psycopg_pool import ConnectionPool

        _POOL = ConnectionPool(
            conninfo=_database_url(),
            kwargs={
                "autocommit": True,
                "row_factory": rows.dict_row,
                "prepare_threshold": 0,
            },
            min_size=1,
            max_size=8,
            timeout=10,
            open=True,
        )
    return _POOL


def _serde() -> Any:
    """Use encrypted checkpoints when LANGGRAPH_AES_KEY is configured."""
    if not os.getenv("LANGGRAPH_AES_KEY"):
        return None
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

    return EncryptedSerializer.from_pycryptodome_aes()


def get_checkpointer() -> Any:
    """Return the configured LangGraph checkpointer."""
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    if MEMORY_BACKEND == "neon":
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = PostgresSaver(_pool(), serde=_serde())
        saver.setup()
        _CHECKPOINTER = saver
    elif MEMORY_BACKEND in {"legacy", "memory", "inmemory"}:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
    else:
        raise RuntimeError(
            f"Unknown MEMORY_BACKEND={MEMORY_BACKEND!r}; use 'legacy' or 'neon'"
        )
    return _CHECKPOINTER


def get_store() -> Any:
    """Return the configured LangGraph long-term store."""
    global _STORE
    if _STORE is not None:
        return _STORE

    if MEMORY_BACKEND == "neon":
        from langgraph.store.postgres import PostgresStore

        store = PostgresStore(_pool())
        store.setup()
        _STORE = store
    elif MEMORY_BACKEND in {"legacy", "memory", "inmemory"}:
        from langgraph.store.memory import InMemoryStore

        _STORE = InMemoryStore()
    else:
        raise RuntimeError(
            f"Unknown MEMORY_BACKEND={MEMORY_BACKEND!r}; use 'legacy' or 'neon'"
        )
    return _STORE


def close_memory() -> None:
    """Close the process-lifetime pool and reset cached backends."""
    global _POOL, _CHECKPOINTER, _STORE
    if _POOL is not None:
        _POOL.close()
    _POOL = None
    _CHECKPOINTER = None
    _STORE = None


__all__ = [
    "MEMORY_BACKEND",
    "get_checkpointer",
    "get_store",
    "close_memory",
]
