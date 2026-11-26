"""Unit tests for the legacy mapping helpers."""

from __future__ import annotations

import sqlite3

import pytest

from backend.datasource.legacy import (
    LegacyDataSource,
    parse_available_days,
    slugify_doctor,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Monday to Saturday", {0, 1, 2, 3, 4, 5}),
        ("Monday to Friday", {0, 1, 2, 3, 4}),
        ("Monday, Wednesday, Friday", {0, 2, 4}),
        ("Tuesday, Thursday, Saturday", {1, 3, 5}),
        ("Mon-Fri", {0, 1, 2, 3, 4}),
        ("Tue, Thu", {1, 3}),
        ("Daily", {0, 1, 2, 3, 4, 5, 6}),
        ("Every day", {0, 1, 2, 3, 4, 5, 6}),
        ("", set()),
        ("Weekends", set()),
    ],
)
def test_parse_available_days(text: str, expected: set[int]) -> None:
    assert parse_available_days(text) == expected


def test_slugify_doctor() -> None:
    assert slugify_doctor("Dr. Rajesh Sharma") == "dr-rajesh-sharma"
    assert slugify_doctor("Dr. Ananya Iyer") == "dr-ananya-iyer"
    assert slugify_doctor("Dr.") == "dr-unknown"


def _row(**values):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(f"CREATE TABLE t ({', '.join(f'{c} TEXT' for c in values)})")
    conn.execute(f"INSERT INTO t ({columns}) VALUES ({placeholders})", tuple(values.values()))
    return conn.execute("SELECT * FROM t").fetchone()


@pytest.mark.parametrize(
    ("status", "notification", "arrived", "expected"),
    [
        ("Confirmed", "pending", 0, "BOOKED"),
        ("Confirmed", "sent", 0, "BOOKED"),
        ("Confirmed", "pending_reply", 0, "BOOKED"),
        ("Confirmed", "pending", 1, "CHECKED_IN"),
        ("Cancelled", "no_show", 0, "CANCELLED"),
        ("Cancelled", "rescheduled", 0, "RESCHEDULED"),
        ("No-Show", "pending", 0, "NO_SHOW"),
    ],
)
def test_status_mapping(status, notification, arrived, expected) -> None:
    row = _row(status=status, notification_status=notification, arrived=arrived)
    assert LegacyDataSource._map_status(row).value == expected
