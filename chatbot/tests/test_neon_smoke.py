"""Opt-in live smoke test for the Neon receptionist backend.

Run with:

    DATA_SOURCE=neon python -m pytest -q tests/test_neon_smoke.py

It creates a temporary appointment/profile and removes it afterwards.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest

RECEPTIONIST_DIR = Path(__file__).resolve().parents[1]
WORKSPACE = RECEPTIONIST_DIR.parent
sys.path.insert(0, str(RECEPTIONIST_DIR))

pytestmark = pytest.mark.skipif(
    os.getenv("DATA_SOURCE", "legacy").strip().lower() != "neon",
    reason="set DATA_SOURCE=neon to run the live Neon smoke test",
)


def test_neon_receptionist_round_trip() -> None:
    os.environ["DATA_SOURCE"] = "neon"

    import psycopg
    from env_config import load_app_env

    load_app_env()
    dsn = os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("no Neon DATABASE_URL in .env.local")

    # Other test modules may have imported database before dotenv set
    # DATA_SOURCE=neon. Reload so this live test really uses the Neon backend.
    import database

    database = importlib.reload(database)
    import database_neon  # noqa: F401 - registers the module for cleanup below

    phone = "9" + str(uuid.uuid4().int)[:9]
    user_id = f"patient_neon_test_{uuid.uuid4().hex[:8]}"
    appointment_id = None

    try:
        doctors = database.get_doctors()
        assert len(doctors) >= 1

        result = database.book_appointment(
            patient_name="Neon Test Patient",
            patient_phone=phone,
            doctor_name=doctors[0]["name"],
            date="2027-01-11",
            time_slot="09:00 AM",
        )
        assert result["success"] is True
        appointment_id = result["appointment_id"]

        duplicate = database.book_appointment(
            patient_name="Neon Duplicate Patient",
            patient_phone="9" + str(uuid.uuid4().int)[:9],
            doctor_name=doctors[0]["name"],
            date="2027-01-11",
            time_slot="09:00 AM",
        )
        assert duplicate["success"] is False

        database.upsert_patient_profile(
            user_id,
            {
                "patient_name": "Neon Test Profile",
                "patient_phone": phone,
                "medical_history": {"conditions": ["asthma"]},
                "conversation_summary": "smoke test",
            },
        )
        profile = database.load_patient_profile(user_id)
        assert profile["patient_name"] == "Neon Test Profile"
        assert profile["medical_history"]["conditions"] == ["asthma"]

        schedule = database.get_daily_schedule("2027-01-11")
        assert len(schedule["doctors"]) >= 1
        assert len(schedule["doctors"][0]["slots"]) == 14

        assert database.cancel_appointment(
            appointment_id, reason="pytest cleanup"
        )["success"] is True
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            if appointment_id:
                conn.execute(
                    "DELETE FROM appointments WHERE id = %s", (appointment_id,)
                )
            conn.execute(
                "DELETE FROM patient_profiles WHERE user_id = %s", (user_id,)
            )
            conn.execute("DELETE FROM patients WHERE phone = %s", (phone,))
        sys.modules["database_neon"]._pool().close()
