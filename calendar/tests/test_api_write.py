"""Write-path tests: booking, guards, holidays, admin."""

from __future__ import annotations

from tests.conftest import headers

DOCTOR = "dr-rajesh-sharma"


def _payload(day, time="09:00", phone="9000000001", **overrides):
    payload = {
        "doctor_id": DOCTOR,
        "date": day.isoformat(),
        "time": time,
        "patient_name": "Test Patient",
        "patient_phone": phone,
        "reason": "Fever",
    }
    payload.update(overrides)
    return payload


def test_receptionist_can_book(client, open_day):
    response = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    )
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["status"] == "BOOKED"
    assert data["doctor_id"] == DOCTOR
    assert data["patient_name"] == "Test Patient"
    assert data["reason"] == "Fever"
    assert data["token_number"] == 1
    assert data["source"] == "calendar"


def test_double_booking_is_rejected(client, open_day):
    first = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    )
    assert first.status_code == 201

    second = client.post(
        "/api/appointments",
        json=_payload(open_day, phone="9000000002"),
        headers=headers("receptionist"),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "slot_taken"


def test_patients_can_book(client, open_day):
    response = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("patient")
    )
    assert response.status_code == 201, response.text
    assert response.json()["patient_phone"] == "9000000001"


def test_clinic_wide_holidays_allow_booking(client, open_day):
    entries = (
        ("PUBLIC_HOLIDAY", "Test Public Holiday", "09:00", "9000000021"),
        ("CLOSED", "Test Closure", "09:30", "9000000022"),
        ("LEAVE", "Test Common Leave", "10:00", "9000000023"),
    )
    for holiday_type, name, time, phone in entries:
        holiday = client.post(
            "/api/holidays",
            json={
                "date": open_day.isoformat(),
                "name": name,
                "type": holiday_type,
            },
            headers=headers("admin"),
        )
        assert holiday.status_code == 201, holiday.text

        response = client.post(
            "/api/appointments",
            json=_payload(open_day, time=time, phone=phone),
            headers=headers("receptionist"),
        )
        assert response.status_code == 201, response.text

    grid = client.get(
        "/api/day-grid",
        params={"date": open_day.isoformat(), "doctor_id": DOCTOR},
        headers=headers("receptionist"),
    ).json()
    assert grid["doctors"][0]["holiday"]["scope"] == "CLINIC"
    assert any(
        slot["time"] == "10:30" and slot["status"] == "AVAILABLE"
        for slot in grid["doctors"][0]["slots"]
    )


def test_reschedule_moves_the_appointment(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    ).json()

    response = client.patch(
        f"/api/appointments/{created['id']}",
        json={"date": open_day.isoformat(), "time": "10:00"},
        headers=headers("receptionist"),
    )
    assert response.status_code == 200, response.text
    moved = response.json()

    assert moved["id"] != created["id"]
    assert moved["start"].endswith("10:00:00+05:30")

    old = client.get(
        f"/api/appointments/{created['id']}", headers=headers("receptionist")
    ).json()
    assert old["status"] == "RESCHEDULED"


def test_cancel_frees_the_slot(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    ).json()

    cancelled = client.post(
        f"/api/appointments/{created['id']}/cancel",
        json={"reason": "Patient called"},
        headers=headers("receptionist"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    rebooked = client.post(
        "/api/appointments",
        json=_payload(open_day, phone="9000000003"),
        headers=headers("receptionist"),
    )
    assert rebooked.status_code == 201


def test_doctor_can_only_check_in_own_patients(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    ).json()

    denied = client.post(
        f"/api/appointments/{created['id']}/check-in",
        headers=headers("doctor", "dr-meera-kulkarni"),
    )
    assert denied.status_code == 403

    allowed = client.post(
        f"/api/appointments/{created['id']}/check-in", headers=headers("doctor", DOCTOR)
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "CHECKED_IN"
    assert allowed.json()["arrived"] is True


def test_admin_can_add_and_remove_a_doctor(client):
    created = client.post(
        "/api/admin/doctors",
        json={
            "full_name": "Dr. Test Calendar",
            "specialization": "Test Specialist",
            "available_days": "Monday, Wednesday, Friday",
        },
        headers=headers("admin"),
    )
    assert created.status_code == 201, created.text
    doctor = created.json()

    assert doctor["id"] == "dr-test-calendar"
    assert {item["weekday"] for item in doctor["schedules"]} == {0, 2, 4}

    meta = client.get("/api/meta").json()
    assert len(meta["doctors"]) == 13

    deleted = client.delete(
        f"/api/admin/doctors/{doctor['id']}", headers=headers("admin")
    )
    assert deleted.status_code == 200
    assert len(client.get("/api/meta").json()["doctors"]) == 12


def test_non_admin_cannot_manage_doctors(client):
    response = client.post(
        "/api/admin/doctors",
        json={
            "full_name": "Dr. Nope",
            "specialization": "Nope",
            "available_days": "Monday",
        },
        headers=headers("receptionist"),
    )
    assert response.status_code == 403


def test_patient_lookup(client):
    response = client.get(
        "/api/patients/9999900000/appointments", headers=headers("patient")
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["patient"]["name"] == "Anuj Patil"
    assert payload["appointments"]


def test_patient_can_fetch_appointment_by_id(client):
    response = client.get("/api/appointments/2", headers=headers("patient"))
    assert response.status_code == 200
    assert response.json()["patient_name"]


def test_patient_can_reschedule_appointment(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("patient")
    ).json()

    moved = client.patch(
        f"/api/appointments/{created['id']}",
        json={"date": open_day.isoformat(), "time": "10:00"},
        headers=headers("patient"),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["start"].endswith("10:00:00+05:30")


def test_patient_can_cancel_appointment(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("patient")
    ).json()

    cancelled = client.post(
        f"/api/appointments/{created['id']}/cancel",
        json={"reason": "Cancelled by patient"},
        headers=headers("patient"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_patient_can_check_in(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("patient")
    ).json()

    checked_in = client.post(
        f"/api/appointments/{created['id']}/check-in", headers=headers("patient")
    )
    assert checked_in.status_code == 200
    assert checked_in.json()["status"] == "CHECKED_IN"


def test_common_leave_allows_booking_for_patient_and_receptionist(client, open_day):
    leave = client.post(
        "/api/holidays",
        json={
            "date": open_day.isoformat(),
            "name": "Clinic common leave",
            "type": "LEAVE",
        },
        headers=headers("admin"),
    )
    assert leave.status_code == 201, leave.text
    assert leave.json()["type"] == "LEAVE"

    for role, time, phone in (
        ("patient", "09:00", "9000000011"),
        ("receptionist", "09:30", "9000000012"),
    ):
        response = client.post(
            "/api/appointments",
            json=_payload(open_day, time=time, phone=phone),
            headers=headers(role),
        )
        assert response.status_code == 201, response.text

    grid = client.get(
        "/api/day-grid",
        params={"date": open_day.isoformat(), "doctor_id": DOCTOR},
        headers=headers("patient"),
    ).json()
    block = grid["doctors"][0]
    assert block["holiday"]["type"] == "LEAVE"
    assert any(
        slot["time"] == "10:00" and slot["status"] == "AVAILABLE"
        for slot in block["slots"]
    )
