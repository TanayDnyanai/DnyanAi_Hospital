"""Concurrent booking tests for the legacy datasource."""

from __future__ import annotations

import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import headers

DOCTOR = "dr-rajesh-sharma"
ATTEMPTS = 50
SLOTS = (
    "09:00",
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "12:00",
    "12:30",
    "14:00",
    "14:30",
    "15:00",
    "15:30",
    "16:00",
    "16:30",
)


def _payload(day, index: int, *, time: str = "09:00") -> dict:
    return {
        "doctor_id": DOCTOR,
        "date": day.isoformat(),
        "time": time,
        "patient_name": f"Race Patient {index}",
        "patient_phone": f"9000000{index:03d}",
        "reason": "Concurrency test",
    }


def test_same_slot_simultaneous_bookings_have_one_winner(client, open_day):
    def book(index: int):
        return client.post(
            "/api/appointments",
            json=_payload(open_day, index),
            headers=headers("patient"),
        )

    with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
        responses = list(pool.map(book, range(ATTEMPTS)))

    statuses = Counter(response.status_code for response in responses)
    assert statuses[201] == 1, [response.text for response in responses]
    assert statuses[409] == ATTEMPTS - 1, [response.text for response in responses]
    assert not any(status >= 500 for status in statuses), [
        response.text for response in responses
    ]

    grid = client.get(
        "/api/day-grid",
        params={"date": open_day.isoformat(), "doctor_id": DOCTOR},
        headers=headers("receptionist"),
    ).json()
    booked = [
        slot
        for slot in grid["doctors"][0]["slots"]
        if slot["time"] == "09:00" and slot["status"] == "BOOKED"
    ]
    assert len(booked) == 1


def test_different_slots_simultaneous_bookings_all_succeed(client, open_day):
    def book(index: int):
        return client.post(
            "/api/appointments",
            json=_payload(open_day, index, time=SLOTS[index]),
            headers=headers("patient"),
        )

    with ThreadPoolExecutor(max_workers=len(SLOTS)) as pool:
        responses = list(pool.map(book, range(len(SLOTS))))

    statuses = Counter(response.status_code for response in responses)
    assert statuses[201] == len(SLOTS), [response.text for response in responses]
    assert not any(status >= 500 for status in statuses), [
        response.text for response in responses
    ]


def test_booking_retries_transient_sqlite_busy(client, open_day, source, monkeypatch):
    original = source._db.book_appointment
    attempts: list[int] = []

    def flaky_book(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(**kwargs)

    monkeypatch.setattr(source._db, "book_appointment", flaky_book)

    response = client.post(
        "/api/appointments",
        json=_payload(open_day, 90),
        headers=headers("patient"),
    )
    assert response.status_code == 201, response.text
    assert len(attempts) >= 2


def test_persistent_sqlite_busy_returns_503(client, open_day, source, monkeypatch):
    def locked_book(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(source._db, "book_appointment", locked_book)

    response = client.post(
        "/api/appointments",
        json=_payload(open_day, 91),
        headers=headers("patient"),
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "db_busy"


def test_failed_metadata_write_rolls_back_appointment(client, open_day, source, monkeypatch):
    def fail_extra(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(source, "_write_extra_conn", fail_extra)

    response = client.post(
        "/api/appointments",
        json=_payload(open_day, 92),
        headers=headers("patient"),
    )
    assert response.status_code == 503, response.text
    assert source.list_patient_appointments("9000000092") == []
