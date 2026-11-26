"""Doctor self-service leave: apply, conflicts, cancellation and guards."""

from __future__ import annotations

from datetime import timedelta

from backend.services.calendar_service import today
from tests.conftest import headers

DOCTOR = "dr-rajesh-sharma"


def _payload(day, time="09:00", phone="9000000001", **overrides):
    payload = {
        "doctor_id": DOCTOR,
        "date": day.isoformat(),
        "time": time,
        "patient_name": "Leave Test Patient",
        "patient_phone": phone,
        "reason": "Fever",
    }
    payload.update(overrides)
    return payload


def _leave(day, reason="Fever", confirm_conflicts=False):
    return {
        "date": day.isoformat(),
        "reason": reason,
        "confirm_conflicts": confirm_conflicts,
    }


def test_doctor_can_apply_leave_and_booking_is_blocked(client, open_day):
    response = client.post(
        "/api/doctor/leave",
        json=_leave(open_day, "Family function"),
        headers=headers("doctor", DOCTOR),
    )
    assert response.status_code == 201, response.text

    leave = response.json()["leave"]
    assert leave["scope"] == "DOCTOR"
    assert leave["doctor_id"] == DOCTOR
    assert leave["type"] == "LEAVE"
    assert leave["name"] == "Family function"
    assert leave["date"] == open_day.isoformat()

    listed = client.get(
        "/api/holidays",
        params={"start": open_day.isoformat(), "end": open_day.isoformat()},
        headers=headers("receptionist"),
    ).json()["holidays"]
    assert any(item["id"] == leave["id"] for item in listed)

    booking = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    )
    assert booking.status_code == 409
    assert booking.json()["code"] == "holiday"


def test_only_a_doctor_can_apply_for_leave(client, open_day):
    for role in ("receptionist", "admin", "patient"):
        response = client.post(
            "/api/doctor/leave",
            json=_leave(open_day),
            headers=headers(role),
        )
        assert response.status_code == 403, (role, response.text)

    missing_doctor = client.post(
        "/api/doctor/leave",
        json=_leave(open_day),
        headers=headers("doctor"),
    )
    assert missing_doctor.status_code == 400


def test_leave_requires_a_reason_and_a_future_date(client, open_day):
    blank = client.post(
        "/api/doctor/leave",
        json=_leave(open_day, "   "),
        headers=headers("doctor", DOCTOR),
    )
    assert blank.status_code == 400

    past_day = today() - timedelta(days=1)
    past = client.post(
        "/api/doctor/leave",
        json=_leave(past_day),
        headers=headers("doctor", DOCTOR),
    )
    assert past.status_code == 400


def test_duplicate_leave_is_rejected(client, open_day):
    first = client.post(
        "/api/doctor/leave",
        json=_leave(open_day),
        headers=headers("doctor", DOCTOR),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/doctor/leave",
        json=_leave(open_day, "Another reason"),
        headers=headers("doctor", DOCTOR),
    )
    assert second.status_code == 409


def test_existing_appointments_need_explicit_confirmation(client, open_day):
    created = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    )
    assert created.status_code == 201, created.text
    appointment = created.json()

    preview = client.get(
        "/api/doctor/leave/conflicts",
        params={"date": open_day.isoformat()},
        headers=headers("doctor", DOCTOR),
    )
    assert preview.status_code == 200
    assert preview.json()["count"] == 1

    blocked = client.post(
        "/api/doctor/leave",
        json=_leave(open_day),
        headers=headers("doctor", DOCTOR),
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "leave_conflicts"
    assert blocked.json()["conflicts"] == 1

    confirmed = client.post(
        "/api/doctor/leave",
        json=_leave(open_day, confirm_conflicts=True),
        headers=headers("doctor", DOCTOR),
    )
    assert confirmed.status_code == 201, confirmed.text

    kept = client.get(
        f"/api/appointments/{appointment['id']}",
        headers=headers("receptionist"),
    ).json()
    assert kept["status"] == "BOOKED"

    moved = client.patch(
        f"/api/appointments/{appointment['id']}",
        json={"date": open_day.isoformat(), "time": "10:00"},
        headers=headers("receptionist"),
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "holiday"


def test_doctor_can_cancel_own_leave(client, open_day):
    leave = client.post(
        "/api/doctor/leave",
        json=_leave(open_day),
        headers=headers("doctor", DOCTOR),
    ).json()["leave"]

    removed = client.delete(
        f"/api/doctor/leave/{leave['id']}",
        headers=headers("doctor", DOCTOR),
    )
    assert removed.status_code == 200

    booking = client.post(
        "/api/appointments", json=_payload(open_day), headers=headers("receptionist")
    )
    assert booking.status_code == 201, booking.text


def test_filtered_calendar_marks_the_leave_day(client, open_day):
    created = client.post(
        "/api/doctor/leave",
        json=_leave(open_day, "Conference"),
        headers=headers("doctor", DOCTOR),
    )
    assert created.status_code == 201, created.text

    params = {"from": open_day.isoformat(), "to": open_day.isoformat()}
    filtered = client.get(
        "/api/calendar",
        params={**params, "doctor_id": DOCTOR},
        headers=headers("receptionist"),
    ).json()["days"][0]
    assert filtered["is_holiday"] is True
    assert filtered["holiday"]["type"] == "LEAVE"
    assert filtered["holiday"]["name"] == "Conference"
    assert filtered["counts"]["available"] == 0

    unfiltered = client.get(
        "/api/calendar", params=params, headers=headers("receptionist")
    ).json()["days"][0]
    assert unfiltered["is_holiday"] is False


def test_doctor_cannot_cancel_another_doctors_leave(client, source, open_day):
    other = next(
        doctor.id for doctor in source.get_doctors() if doctor.id != DOCTOR
    )
    leave = client.post(
        "/api/doctor/leave",
        json=_leave(open_day),
        headers=headers("doctor", other),
    )
    assert leave.status_code == 201, leave.text
    leave_id = leave.json()["leave"]["id"]

    denied = client.delete(
        f"/api/doctor/leave/{leave_id}",
        headers=headers("doctor", DOCTOR),
    )
    assert denied.status_code == 404

    admin = client.delete(
        f"/api/holidays/{leave_id}", headers=headers("admin")
    )
    assert admin.status_code == 200
