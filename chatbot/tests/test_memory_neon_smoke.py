"""Opt-in live smoke test for Postgres-backed LangGraph memory.

Run with:

    MEMORY_BACKEND=neon python -m pytest -q tests/test_memory_neon_smoke.py

It simulates a process restart by closing the memory pool, rebuilding the
backends, and verifying that checkpoints and store items survived.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

RECEPTIONIST_DIR = Path(__file__).resolve().parents[1]
WORKSPACE = RECEPTIONIST_DIR.parent
sys.path.insert(0, str(RECEPTIONIST_DIR))

pytestmark = pytest.mark.skipif(
    os.getenv("MEMORY_BACKEND", "legacy").strip().lower() != "neon",
    reason="set MEMORY_BACKEND=neon to run the live Neon memory smoke test",
)


def test_postgres_memory_survives_restart() -> None:
    import psycopg
    from env_config import load_app_env
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    import memory

    load_app_env()
    dsn = os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("no Neon DATABASE_URL in .env.local")

    thread_id = f"memory-smoke-{uuid.uuid4().hex[:12]}"
    namespace = ("tests", thread_id)

    builder = StateGraph(MessagesState)

    def echo_node(state: MessagesState) -> dict:
        return {"messages": [AIMessage(content="persisted-reply")]}

    builder.add_node("echo", echo_node)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)

    try:
        graph_before = builder.compile(
            checkpointer=memory.get_checkpointer(),
            store=memory.get_store(),
        )
        graph_before.invoke(
            {"messages": [HumanMessage(content="persist me")]},
            config={"configurable": {"thread_id": thread_id}},
        )
        memory.get_store().put(namespace, "marker", {"ok": True})

        # Simulate a process restart: drop cached backends and the pool.
        memory.close_memory()

        graph_after = builder.compile(
            checkpointer=memory.get_checkpointer(),
            store=memory.get_store(),
        )
        snapshot = graph_after.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        contents = [
            getattr(message, "content", "")
            for message in snapshot.values.get("messages", [])
        ]
        assert any("persisted-reply" in str(content) for content in contents)

        item = memory.get_store().get(namespace, "marker")
        assert item is not None
        assert item.value["ok"] is True
    finally:
        memory.close_memory()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,)
            )
            conn.execute(
                "DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,)
            )
            conn.execute(
                "DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,)
            )
            conn.execute(
                "DELETE FROM store WHERE prefix = %s", (".".join(namespace),)
            )
