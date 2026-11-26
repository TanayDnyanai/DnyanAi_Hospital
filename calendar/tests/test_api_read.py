"""API contract tests: roles, access parity and read shapes."""

from __future__ import annotations

from datetime import date

from tests.conftest import headers


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_malformed_ids_do_not_raise_server_errors(client):
    assert client.get("/api/appointments/not-a-number", headers=headers("receptionist")).status_code == 404
    assert client.get("/api/calendar", params={"from": "2026-12-01", "to": "2026-12-02", "doctor_id": "!!"}, headers=headers("receptionist")).status_code == 400


def test_meta_lists_doctors_with_schedules(client):
    response = client.get("/api/meta")
    assert response.status_code == 200
    payload = response.json()

    assert len(payload["doctors"]) == 12
    assert payload["timezone"] == "Asia/Kolkata"
    assert payload["slot_minutes"] == 30

    first = payload["doctors"][0]
    assert first["id"].startswith("dr-")
    assert first["color"].startswith("#")
    assert first["schedules"], "doctors must expose working windows"
    assert {item["weekday"] for item in first["schedules"]} == {0, 1, 2, 3, 4, 5}


def test_calendar_returns_seeded_day(client):
    response = client.get(
        "/api/calendar",
        params={"from": "2026-11-18", "to": "2026-11-18"},
        headers=headers("receptionist"),
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["days"][0]["counts"]["total"] == 5
    assert len(payload["days"][0]["events"]) == 5
    assert "patient_name" in payload["days"][0]["events"][0]


def test_patient_payload_matches_receptionist(client):
    patient = client.get(
        "/api/calendar",
        params={"from": "2026-11-18", "to": "2026-11-18"},
        headers=headers("patient"),
    )
    receptionist = client.get(
        "/api/calendar",
        params={"from": "2026-11-18", "to": "2026-11-18"},
        headers=headers("receptionist"),
    )
    assert patient.status_code == 200
    assert receptionist.status_code == 200

    body = patient.text
    assert "patient_name" in body
    assert "patient_phone" in body
    assert "emergency" in body

    patient_event = patient.json()["days"][0]["events"][0]
    staff_event = receptionist.json()["days"][0]["events"][0]
    assert set(patient_event) == set(staff_event)
    assert patient_event["patient_name"] == staff_event["patient_name"]


def test_doctor_only_sees_own_appointments(client):
    response = client.get(
        "/api/calendar",
        params={"from": "2026-11-01", "to": "2026-11-30"},
        headers=headers("doctor", "dr-rajesh-sharma"),
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["doctor_id"] == "dr-rajesh-sharma"
    for day in payload["days"]:
        for event in day["events"]:
            assert event["doctor_id"] == "dr-rajesh-sharma"


def test_doctor_role_requires_doctor_header(client):
    response = client.get(
        "/api/calendar",
        params={"from": "2026-11-18", "to": "2026-11-18"},
        headers={"X-Role": "doctor"},
    )
    assert response.status_code == 400


def test_day_grid_shape(client):
    response = client.get(
        "/api/day-grid",
        params={"date": "2026-11-18"},
        headers=headers("receptionist"),
    )
    assert response.status_code == 200
    payload = response.json()

    assert len(payload["doctors"]) == 12
    booked = [
        slot
        for doctor in payload["doctors"]
        for slot in doctor["slots"]
        if slot["status"] == "BOOKED"
    ]
    assert len(booked) == 5


def test_stats_matches_day(client):
    response = client.get(
        "/api/stats",
        params={"date": "2026-11-18"},
        headers=headers("admin"),
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["total_appointments"] == 5
    assert payload["scheduled_slots"] > 0
    assert 0 <= payload["occupancy"] <= 1
    assert len(payload["per_doctor"]) == 12


def test_invalid_role_rejected(client):
    response = client.get(
        "/api/stats", params={"date": "2026-11-18"}, headers={"X-Role": "wizard"}
    )
    assert response.status_code == 400
