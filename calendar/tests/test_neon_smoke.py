"""End-to-end smoke test against the real Neon/Postgres adapter.

Runs only when ``DATA_SOURCE=neon`` and Neon has written ``DATABASE_URL``
into the workspace .env.local. It creates temporary rows on the selected
branch and removes them at the end.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

from backend import config
from backend.datasource.postgres import PostgresDataSource
from backend.domain import BookingError


pytestmark = pytest.mark.skipif(
    os.getenv("DATA_SOURCE", "").lower() != "neon" or not config.NEON_DATABASE_URL,
    reason="set DATA_SOURCE=neon and provide DATABASE_URL to run the Neon smoke test",
)


def _next_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def test_neon_round_trip() -> None:
    source = PostgresDataSource()
    token = uuid.uuid4().hex[:8]
    doctor_name = f"Dr. Smoke {token}"
    phone = f"99{token[:8]}"
    day = _next_monday()

    try:
        doctor = source.create_doctor(
            full_name=doctor_name,
            specialization="Smoke Test",
            available_days="Monday",
            actor="smoke",
        )
        assert doctor.id.isdigit()
        assert any(schedule.weekday == 0 for schedule in doctor.schedules)

        appointment = source.create_appointment(
            patient_name="Smoke Patient",
            patient_phone=phone,
            doctor_id=doctor.id,
            day=day,
            start_time="09:00",
            reason="Neon smoke test",
            actor="smoke",
        )
        assert appointment.id.isdigit()
        assert appointment.status.value == "BOOKED"
        assert appointment.token_number == 1
        assert appointment.start.strftime("%H:%M") == "09:00"

        fetched = source.get_appointment(appointment.id)
        assert fetched is not None
        assert fetched.patient_phone == phone

        with pytest.raises(BookingError) as conflict:
            source.create_appointment(
                patient_name="Second Patient",
                patient_phone=f"{phone}1",
                doctor_id=doctor.id,
                day=day,
                start_time="09:00",
                actor="smoke",
            )
        assert conflict.value.code == "slot_taken"

        grid = source.get_day_grid(day, doctor.id)
        slots = [slot for slot in grid["doctors"][0]["slots"] if slot["time"] == "09:00"]
        assert slots and slots[0]["status"] == "BOOKED"

        history = source.list_patient_appointments(phone)
        assert [item.id for item in history] == [appointment.id]

        cancelled = source.cancel_appointment(
            appointment.id, reason="Smoke cleanup", actor="smoke"
        )
        assert cancelled.status.value == "CANCELLED"

        rebooked = source.create_appointment(
            patient_name="Smoke Patient",
            patient_phone=phone,
            doctor_id=doctor.id,
            day=day,
            start_time="09:00",
            actor="smoke",
        )
        assert rebooked.id != appointment.id

        audit = [entry for entry in source.list_audit(20) if entry["actor"] == "smoke"]
        assert any(entry["action"] == "appointment.create" for entry in audit)
        assert any(entry["action"] == "appointment.cancel" for entry in audit)
    finally:
        with source._connect() as conn:
            conn.execute(
                "DELETE FROM appointments WHERE doctor_id IN "
                "(SELECT id FROM doctor_profiles WHERE user_id IN "
                "(SELECT id FROM users WHERE name = %s))",
                (doctor_name,),
            )
            conn.execute(
                "DELETE FROM doctor_schedules WHERE doctor_id IN "
                "(SELECT id FROM doctor_profiles WHERE user_id IN "
                "(SELECT id FROM users WHERE name = %s))",
                (doctor_name,),
            )
            conn.execute(
                "DELETE FROM doctor_profiles WHERE user_id IN "
                "(SELECT id FROM users WHERE name = %s)",
                (doctor_name,),
            )
            conn.execute("DELETE FROM users WHERE name = %s", (doctor_name,))
            conn.execute(
                "DELETE FROM patients WHERE phone IN (%s, %s)", (phone, f"{phone}1")
            )
            conn.execute("DELETE FROM audit_log WHERE actor = 'smoke'")
        source._pool.close()
