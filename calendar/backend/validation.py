"""Shared request validation helpers.

IDs in the public API are opaque strings, but every data source ultimately
stores them as integers. Validate at the edge so malformed input becomes a
clean 404/400 instead of an unhandled ``ValueError`` (HTTP 500).
"""

from __future__ import annotations

from fastapi import HTTPException


def numeric_id(value: str, *, label: str = "ID") -> str:
    """Return a normalized numeric id, or raise a 404 for malformed input."""
    text = str(value).strip()
    if not text.isdigit():
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    return str(int(text))


__all__ = ["numeric_id"]
