"""Neon/Postgres backend for the medical receptionist.

Implements the same public function API as ``database.py`` so tools.py,
main.py, scheduler.py and patient_memory.py do not need to change. Enable it
with ``DATA_SOURCE=neon``; the switch is applied at the bottom of
``database.py``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import rows
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from env_config import load_app_env

load_app_env()

WORKSPACE = Path(__file__).resolve().parent.parent
CALENDAR_DIR = WORKSPACE / "calendar"
sys.path.insert(0, str(CALENDAR_DIR))
# Single source of truth for the shared clinic schema. Fail loudly if this
# import breaks: silently using an empty schema would mask a broken deployment.
from backend.schema import SCHEMA as _BASE_SCHEMA  # noqa: E402
from backend import config as _calendar_config  # noqa: E402

DSN = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_UNPOOLED")
if not DSN:
    raise RuntimeError("DATA_SOURCE=neon requires DATABASE_URL in .env.local")
# Schema changes must use the direct endpoint; PgBouncer transaction pooling
# does not support the session state DDL tools rely on.
DDL_DSN = os.getenv("DATABASE_URL_UNPOOLED") or DSN

SLOT_DURATION_MINUTES = 30
STANDARD_SLOTS = [
    "09:00 AM", "09:30 AM", "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM", "12:00 PM", "12:30 PM",
    "02:00 PM", "02:30 PM", "03:00 PM", "03:30 PM",
    "04:00 PM", "04:30 PM",
]
WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]

# Kept as an alias for any external caller. These tables are now part of the
# canonical schema, so there is no second definition to keep in sync.
EXTRA_SCHEMA = ""

_POOL: ConnectionPool | None = None
_WRITE_POOL: ConnectionPool | None = None


def _pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        # Reads use autocommit so they avoid an implicit BEGIN/COMMIT pair;
        # on Neon every round trip costs ~70ms.
        _POOL = ConnectionPool(
            conninfo=DSN,
            kwargs={"row_factory": rows.dict_row, "autocommit": True},
            min_size=2,
            max_size=8,
            timeout=10,
            open=True,
        )
    return _POOL


def _write_pool() -> ConnectionPool:
    global _WRITE_POOL
    if _WRITE_POOL is None:
        # Writes keep implicit transactions for multi-statement atomicity.
        _WRITE_POOL = ConnectionPool(
            conninfo=DSN,
            kwargs={"row_factory": rows.dict_row, "autocommit": False},
            min_size=1,
            max_size=4,
            timeout=10,
            open=True,
        )
    return _WRITE_POOL


def get_connection():
    """Return a read-only pooled connection context manager."""
    return _pool().connection()


def get_write_connection():
    """Return a transactional pooled connection for writes."""
    return _write_pool().connection()


def close_pools() -> None:
    global _POOL, _WRITE_POOL
    if _POOL is not None:
        _POOL.close()
    if _WRITE_POOL is not None:
        _WRITE_POOL.close()
    _POOL = None
    _WRITE_POOL = None


def init_db() -> None:
    """Ensure the shared schema exists (safe to call repeatedly).

    Called explicitly from the app lifespan, never on import. Uses the direct
    connection because DDL must not run through the pooled endpoint.
    """
    with psycopg.connect(DDL_DSN, autocommit=True) as conn:
        conn.execute(_BASE_SCHEMA)


# --- helpers -------------------------------------------------------------

def _to_json_list(value):
    if value is None or value == "":
        return None
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        return json.dumps(cleaned) if cleaned else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                cleaned = [str(v).strip() for v in parsed if str(v).strip()]
                return json.dumps(cleaned) if cleaned else None
        except Exception:
            pass
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return json.dumps(parts) if parts else text
    return json.dumps([str(value)])


def _from_json_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
        return [p.strip() for p in text.split(",") if p.strip()]
    return [str(value)]


def _pg_time_to_slot(value) -> str:
    if isinstance(value, str):
        value = datetime.strptime(value, "%H:%M:%S").time()
    return value.strftime("%I:%M %p")


def _slot_to_pg_time(slot: str) -> str:
    return datetime.strptime(str(slot).strip().upper(), "%I:%M %p").strftime("%H:%M:%S")


def _pg_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _legacy_status(value: str) -> str:
    return {
        "CONFIRMED": "Confirmed",
        "BOOKED": "Confirmed",
        "CHECKED_IN": "Confirmed",
        "CANCELLED": "Cancelled",
        "NO_SHOW": "No-Show",
    }.get(str(value).upper(), str(value))


def _format_available_days(days: list[int]) -> str:
    days = sorted(set(int(day) for day in days))
    names = [WEEKDAY_NAMES[day] for day in days]
    if not names:
        return "Monday to Saturday"
    if names == WEEKDAY_NAMES[:6]:
        return "Monday to Saturday"
    if len(names) == 7:
        return "Daily"
    return ", ".join(names)


def _weekday_map(conn) -> dict[int, list[int]]:
    """Load every doctor's schedule weekdays in one round trip."""
    rows_ = conn.execute(
        "SELECT doctor_id, weekday FROM doctor_schedules ORDER BY doctor_id, weekday"
    ).fetchall()
    result: dict[int, list[int]] = {}
    for row in rows_:
        result.setdefault(int(row["doctor_id"]), []).append(int(row["weekday"]))
    return result


def _available_days_text(conn, doctor_id: int) -> str:
    return _format_available_days(_weekday_map(conn).get(int(doctor_id), []))


def find_doctor(cursor=None, query: str = "") -> str | None:
    """Fuzzy match a doctor name, mirroring the legacy helper."""
    clean = str(query or "").lower().replace("dr.", "").replace("dr", "").strip()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT legacy_name FROM doctor_legacy_map WHERE lower(legacy_name) LIKE lower(%s) "
            "ORDER BY doctor_id LIMIT 1",
            (f"%{clean}%",),
        ).fetchone()
        if row:
            return row["legacy_name"]
        for word in clean.split():
            if len(word) > 2:
                row = conn.execute(
                    "SELECT legacy_name FROM doctor_legacy_map WHERE lower(legacy_name) LIKE lower(%s) "
                    "ORDER BY doctor_id LIMIT 1",
                    (f"%{word}%",),
                ).fetchone()
                if row:
                    return row["legacy_name"]
    return None


def get_doctors() -> list[dict]:
    with get_connection() as conn:
        rows_ = conn.execute(
            """
            SELECT dp.id AS doctor_id, u.name AS name, dp.specialization AS specialty
            FROM doctor_profiles dp
            JOIN users u ON u.id = dp.user_id
            ORDER BY dp.id
            """
        ).fetchall()
        weekdays = _weekday_map(conn)
        return [
            {
                "name": row["name"],
                "specialty": row["specialty"],
                "available_days": _format_available_days(
                    weekdays.get(int(row["doctor_id"]), [])
                ),
            }
            for row in rows_
        ]


# --- patient profiles ----------------------------------------------------

def find_user_id_by_phone(patient_phone: str) -> str | None:
    if not patient_phone:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM patient_profiles WHERE patient_phone = %s",
            (str(patient_phone).strip(),),
        ).fetchone()
    return row["user_id"] if row else None


def set_profile_alias(user_id: str, canonical_user_id: str) -> None:
    if not user_id or not canonical_user_id or user_id == canonical_user_id:
        return
    with get_write_connection() as conn:
        conn.execute(
            """
            INSERT INTO patient_profiles (user_id, alias_of, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                alias_of = EXCLUDED.alias_of,
                patient_phone = NULL,
                patient_name = NULL,
                preferred_doctor = NULL,
                medical_conditions = NULL,
                allergies = NULL,
                reported_symptoms = NULL,
                preferences = NULL,
                conversation_summary = NULL,
                updated_at = now()
            """,
            (user_id, canonical_user_id),
        )


def upsert_patient_profile(user_id: str, data: dict) -> None:
    history = data.get("medical_history", {}) if isinstance(data.get("medical_history"), dict) else {}
    medical_conditions = history.get("conditions", data.get("medical_conditions"))
    allergies = history.get("allergies", data.get("allergies"))
    reported_symptoms = history.get("reported_symptoms", data.get("reported_symptoms"))
    preferences = data.get("preferences")
    patient_phone = data.get("patient_phone")
    if patient_phone:
        patient_phone = str(patient_phone).strip() or None

    with get_write_connection() as conn:
        row = conn.execute(
            "SELECT alias_of FROM patient_profiles WHERE user_id = %s", (user_id,)
        ).fetchone()
        if row and row["alias_of"] and row["alias_of"] != user_id:
            user_id = row["alias_of"]
        if patient_phone:
            owner = conn.execute(
                "SELECT user_id FROM patient_profiles "
                "WHERE patient_phone = %s AND user_id <> %s",
                (patient_phone, user_id),
            ).fetchone()
            if owner:
                user_id = owner["user_id"]
        conn.execute(
            """
            INSERT INTO patient_profiles (
                user_id, patient_name, patient_phone, preferred_doctor,
                medical_conditions, allergies, reported_symptoms,
                preferences, conversation_summary, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                patient_name = COALESCE(EXCLUDED.patient_name, patient_profiles.patient_name),
                patient_phone = COALESCE(EXCLUDED.patient_phone, patient_profiles.patient_phone),
                preferred_doctor = COALESCE(EXCLUDED.preferred_doctor, patient_profiles.preferred_doctor),
                medical_conditions = COALESCE(EXCLUDED.medical_conditions, patient_profiles.medical_conditions),
                allergies = COALESCE(EXCLUDED.allergies, patient_profiles.allergies),
                reported_symptoms = COALESCE(EXCLUDED.reported_symptoms, patient_profiles.reported_symptoms),
                preferences = COALESCE(EXCLUDED.preferences, patient_profiles.preferences),
                conversation_summary = COALESCE(EXCLUDED.conversation_summary, patient_profiles.conversation_summary),
                alias_of = NULL,
                updated_at = now()
            """,
            (
                user_id,
                data.get("patient_name"),
                patient_phone,
                data.get("preferred_doctor"),
                _to_json_list(medical_conditions),
                _to_json_list(allergies),
                _to_json_list(reported_symptoms),
                _to_json_list(preferences) if isinstance(preferences, (list, dict)) else preferences,
                data.get("conversation_summary"),
            ),
        )


def load_patient_profile(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM patient_profiles WHERE user_id = %s", (user_id,)
        ).fetchone()
    if row is None:
        return None
    alias_of = row["alias_of"]
    if alias_of and alias_of != user_id:
        return load_patient_profile(alias_of)
    return {
        "patient_name": row["patient_name"],
        "patient_phone": row["patient_phone"],
        "preferred_doctor": row["preferred_doctor"],
        "medical_history": {
            "conditions": _from_json_list(row["medical_conditions"]),
            "allergies": _from_json_list(row["allergies"]),
            "reported_symptoms": _from_json_list(row["reported_symptoms"]),
            "medications": [],
        },
        "conversation_summary": row["conversation_summary"] or "",
        "last_appointment_id": row["last_appointment_id"],
    }


def get_patient_profile_by_user_id(user_id: str) -> dict | None:
    return load_patient_profile(user_id)


# --- appointments --------------------------------------------------------

def _appointment_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "patient_name": row["patient_name"],
        "patient_phone": row["patient_phone"],
        "doctor_name": row["doctor_name"],
        "date": _pg_date(row["appointment_date"]).isoformat(),
        "time_slot": _pg_time_to_slot(row["appointment_time"]),
        "status": _legacy_status(row["status"]),
        "is_emergency": bool(row["is_emergency"]),
    }


_APPOINTMENT_JOIN = """
SELECT a.id, a.appointment_date, a.appointment_time, a.status,
       a.emergency AS is_emergency,
       a.notification_status, p.name AS patient_name, p.phone AS patient_phone,
       m.legacy_name AS doctor_name
FROM appointments a
JOIN patients p ON p.id = a.patient_id
JOIN doctor_legacy_map m ON m.doctor_id = a.doctor_id
"""


def get_patient_appointments(patient_phone: str) -> list[dict]:
    with get_connection() as conn:
        rows_ = conn.execute(
            _APPOINTMENT_JOIN
            + " WHERE p.phone = %s AND a.status = 'CONFIRMED' "
            + "ORDER BY a.appointment_date, a.appointment_time",
            (str(patient_phone).strip(),),
        ).fetchall()
    return [_appointment_dict(row) for row in rows_]


def get_appointment_by_id(appointment_id: int) -> dict | None:
    """Return one appointment (with patient phone) or None.

    Used to verify ownership before a destructive chat-initiated change.
    """
    with get_connection() as conn:
        row = conn.execute(
            _APPOINTMENT_JOIN + " WHERE a.id = %s", (int(appointment_id),)
        ).fetchone()
    return _appointment_dict(row) if row else None


def get_patient_appointments_by_user_id(user_id: str) -> dict:
    profile = load_patient_profile(user_id)
    if not profile:
        return {"success": False, "error": "No patient profile found for this user_id.", "appointments": []}
    phone = profile.get("patient_phone")
    if not phone:
        return {
            "success": False,
            "error": "Patient phone number is not saved for this user_id yet.",
            "patient_name": profile.get("patient_name"),
            "appointments": [],
        }
    return {
        "success": True,
        "user_id": user_id,
        "patient_name": profile.get("patient_name"),
        "patient_phone": phone,
        "appointments": get_patient_appointments(phone),
    }


def normalize_time_slot(time_str: str) -> str | None:
    clean = str(time_str).strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt).strftime("%I:%M %p")
        except ValueError:
            continue
    return None


def get_available_slots(doctor_name: str, date: str) -> list[str]:
    resolved = find_doctor(None, doctor_name)
    if not resolved:
        return []
    clean_date = str(date).strip()
    with get_connection() as conn:
        booked = {
            _pg_time_to_slot(row["appointment_time"])
            for row in conn.execute(
                """
                SELECT a.appointment_time FROM appointments a
                JOIN doctor_legacy_map m ON m.doctor_id = a.doctor_id
                WHERE m.legacy_name = %s AND a.appointment_date = %s
                  AND a.status = 'CONFIRMED'
                """,
                (resolved, clean_date),
            )
        }
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    free = []
    for slot in STANDARD_SLOTS:
        if slot in booked:
            continue
        if clean_date == today_str:
            try:
                start = datetime.strptime(f"{clean_date} {slot}", "%Y-%m-%d %I:%M %p")
                if start + timedelta(minutes=SLOT_DURATION_MINUTES) <= now:
                    continue
            except ValueError:
                pass
        elif clean_date < today_str:
            continue
        free.append(slot)
    return free


def book_appointment(
    patient_name: str,
    patient_phone: str,
    doctor_name: str,
    date: str,
    time_slot: str,
    is_emergency: int = 0,
) -> dict:
    resolved = find_doctor(None, doctor_name)
    if not resolved:
        return {"success": False, "error": f"Doctor '{doctor_name}' was not found in our clinic directory."}
    normalized = normalize_time_slot(time_slot)
    if not normalized:
        return {
            "success": False,
            "error": f"Invalid time format '{time_slot}'. Please use standard clinic slots (e.g., '09:00 AM', '02:00 PM').",
        }
    if normalized not in STANDARD_SLOTS:
        return {
            "success": False,
            "error": f"'{time_slot}' is not a standard clinic appointment slot. Standard clinic slots are: {', '.join(STANDARD_SLOTS)}.",
        }
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    clean_date = str(date).strip()
    if clean_date < today_str:
        return {"success": False, "error": f"Cannot book appointments for past dates ({clean_date})."}
    if clean_date == today_str:
        try:
            start = datetime.strptime(f"{clean_date} {normalized}", "%Y-%m-%d %I:%M %p")
            if start + timedelta(minutes=SLOT_DURATION_MINUTES) <= now:
                return {
                    "success": False,
                    "error": f"The slot {normalized} on {clean_date} has already ended. Please choose an upcoming time slot.",
                }
        except ValueError:
            pass

    try:
        with get_write_connection() as conn:
            patient_id = int(
                conn.execute(
                    "INSERT INTO patients (name, phone) VALUES (%s, %s) "
                    "ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                    (str(patient_name).strip(), str(patient_phone).strip()),
                ).fetchone()["id"]
            )
            doctor_id = int(
                conn.execute(
                    "SELECT doctor_id FROM doctor_legacy_map WHERE legacy_name = %s",
                    (resolved,),
                ).fetchone()["doctor_id"]
            )
            appointment_id = int(
                conn.execute(
                    """
                    INSERT INTO appointments (
                        patient_id, doctor_id, appointment_date, appointment_time,
                        status, emergency, notification_status, source
                    ) VALUES (%s, %s, %s, %s, 'CONFIRMED', %s, 'pending', 'voice-agent')
                    RETURNING id
                    """,
                    (
                        patient_id,
                        doctor_id,
                        datetime.strptime(clean_date, "%Y-%m-%d").date(),
                        _slot_to_pg_time(normalized),
                        bool(is_emergency),
                    ),
                ).fetchone()["id"]
            )
    except UniqueViolation:
        return {
            "success": False,
            "error": f"The slot {time_slot} on {date} with {resolved} is already booked. Please choose another time.",
        }
    return {
        "success": True,
        "appointment_id": appointment_id,
        "patient_name": str(patient_name).strip(),
        "doctor_name": resolved,
        "date": clean_date,
        "time_slot": normalized,
        "status": "Confirmed",
        "is_emergency": bool(is_emergency),
        "notification_status": "pending",
    }


def cancel_appointment(appointment_id: int, reason: str = "Cancelled by staff/patient") -> dict:
    with get_write_connection() as conn:
        row = conn.execute(
            _APPOINTMENT_JOIN + " WHERE a.id = %s", (int(appointment_id),)
        ).fetchone()
        if not row:
            return {"success": False, "error": f"Appointment #{appointment_id} was not found."}
        current = _appointment_dict(row)
        if current["status"] in ("Cancelled", "No-Show"):
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} has already been marked as {current['status']}.",
            }
        conn.execute(
            "UPDATE appointments SET status = 'CANCELLED' WHERE id = %s", (int(appointment_id),)
        )
    return {
        "success": True,
        "appointment_id": int(appointment_id),
        "patient_name": current["patient_name"],
        "doctor_name": current["doctor_name"],
        "date": current["date"],
        "time_slot": current["time_slot"],
        "status": "Cancelled",
        "reason": reason,
    }


def update_notification_status(appointment_id: int, status: str) -> bool:
    with get_write_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET notification_status = %s WHERE id = %s",
            (status, int(appointment_id)),
        )
        return cursor.rowcount > 0


def mark_appointment_arrived(appointment_id: int) -> bool:
    with get_write_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET arrived = TRUE WHERE id = %s", (int(appointment_id),)
        )
        return cursor.rowcount > 0


def mark_no_show(appointment_id: int, reason: str = "No-Show after 10 min grace") -> dict:
    with get_write_connection() as conn:
        row = conn.execute(
            _APPOINTMENT_JOIN + " WHERE a.id = %s", (int(appointment_id),)
        ).fetchone()
        if not row:
            return {"success": False, "error": f"Appointment #{appointment_id} was not found."}
        current = _appointment_dict(row)
        if current["status"] in ("Cancelled", "No-Show"):
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} is already marked as {current['status']}.",
            }
        conn.execute(
            "UPDATE appointments SET status = 'NO_SHOW' WHERE id = %s", (int(appointment_id),)
        )
    return {
        "success": True,
        "appointment_id": int(appointment_id),
        "patient_name": current["patient_name"],
        "doctor_name": current["doctor_name"],
        "date": current["date"],
        "time_slot": current["time_slot"],
        "status": "No-Show",
        "reason": reason,
        "message": f"Appointment #{appointment_id} marked as No-Show. Slot {current['time_slot']} on {current['date']} is now immediately open for other patients.",
    }


def get_due_checkins(now: datetime | None = None) -> list[dict]:
    try:
        from notifications import CHECKIN_MINUTES_AFTER
    except Exception:
        CHECKIN_MINUTES_AFTER = 10
    if now is None:
        try:
            from notifications import clinic_tz
            now = datetime.now(clinic_tz())
        except Exception:
            now = datetime.now()
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    # Only check in appointments for the current clinic day. This prevents
    # imported historical rows with notification_status='sent' from replaying
    # old WhatsApp check-ins on startup.
    today = now_naive.date()
    with get_connection() as conn:
        rows_ = conn.execute(
            _APPOINTMENT_JOIN
            + " WHERE a.status = 'CONFIRMED' AND a.notification_status = 'sent' "
            + "AND COALESCE(a.arrived, FALSE) = FALSE "
            + "AND a.appointment_date = %s",
            (today,),
        ).fetchall()
    due = []
    for row in rows_:
        slot = datetime.strptime(
            f"{_pg_date(row['appointment_date']).isoformat()} {_pg_time_to_slot(row['appointment_time'])}",
            "%Y-%m-%d %I:%M %p",
        )
        if now_naive >= slot + timedelta(minutes=CHECKIN_MINUTES_AFTER):
            due.append(_appointment_dict(row))
    return due


def reschedule_appointment(appointment_id: int) -> dict:
    with get_write_connection() as conn:
        row = conn.execute(
            _APPOINTMENT_JOIN + " WHERE a.id = %s", (int(appointment_id),)
        ).fetchone()
        if not row:
            return {"success": False, "error": f"Appointment #{appointment_id} not found."}
        current = _appointment_dict(row)
        if current["status"] not in ("Confirmed", "pending_reply", "confirmed_not_coming"):
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} is {current['status']}, cannot reschedule.",
            }
        doctor = current["doctor_name"]
        appt_date = current["date"]
        available = get_available_slots(doctor, appt_date)
        try:
            from notifications import find_next_available_same_day_slot
            new_slot = find_next_available_same_day_slot(current["time_slot"], available)
        except Exception:
            new_slot = available[0] if available else None
        if not new_slot:
            return {
                "success": False,
                "error": "No available slots remaining for this doctor today.",
                "no_slot": True,
            }
        try:
            conn.execute(
                "UPDATE appointments SET status = 'CANCELLED', notification_status = 'rescheduled' "
                "WHERE id = %s",
                (int(appointment_id),),
            )
            new_id = int(
                conn.execute(
                    """
                    INSERT INTO appointments (
                        patient_id, doctor_id, appointment_date, appointment_time,
                        status, emergency, notification_status, source
                    )
                    SELECT a.patient_id, a.doctor_id, a.appointment_date, %s,
                           'CONFIRMED', a.emergency, 'pending', 'reschedule'
                    FROM appointments a WHERE a.id = %s
                    RETURNING id
                    """,
                    (_slot_to_pg_time(new_slot), int(appointment_id)),
                ).fetchone()["id"]
            )
        except UniqueViolation:
            return {
                "success": False,
                "error": f"Slot {new_slot} was just taken. Please try again or reschedule via the portal.",
                "no_slot": True,
            }
    return {
        "success": True,
        "old_appointment_id": int(appointment_id),
        "new_appointment_id": new_id,
        "patient_name": current["patient_name"],
        "doctor_name": doctor,
        "date": appt_date,
        "time_slot": new_slot,
    }


def book_urgent_appointment(
    patient_name: str,
    patient_phone: str,
    doctor_name: str = "Dr. Rajesh Sharma",
) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    slots = get_available_slots(doctor_name, today)
    if not slots:
        return {
            "success": False,
            "error": f"No remaining regular slots available with {doctor_name} for today ({today}). Please direct the patient to the emergency walk-in desk or nearest ER immediately.",
            "date": today,
            "doctor_name": doctor_name,
        }
    return book_appointment(
        patient_name=patient_name,
        patient_phone=patient_phone,
        doctor_name=doctor_name,
        date=today,
        time_slot=slots[0],
        is_emergency=1,
    )


def get_daily_schedule(date: str = None) -> dict:
    clean_date = (date or datetime.now().strftime("%Y-%m-%d")).strip()
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    with get_connection() as conn:
        all_appts = conn.execute(
            _APPOINTMENT_JOIN + " WHERE a.appointment_date = %s ORDER BY a.id DESC",
            (datetime.strptime(clean_date, "%Y-%m-%d").date(),),
        ).fetchall()
        doctors = conn.execute(
            """
            SELECT m.legacy_name AS name, dp.specialization AS specialty,
                   dp.id AS doctor_id
            FROM doctor_legacy_map m
            JOIN doctor_profiles dp ON dp.id = m.doctor_id
            ORDER BY dp.id
            """
        ).fetchall()
        weekdays = _weekday_map(conn)
        result_doctors = []
        for doctor in doctors:
            name = doctor["name"]
            by_slot = {
                _pg_time_to_slot(row["appointment_time"]): row
                for row in all_appts
                if row["doctor_name"] == name and row["status"] == "CONFIRMED"
            }
            recent = {
                _pg_time_to_slot(row["appointment_time"]): row
                for row in all_appts
                if row["doctor_name"] == name and row["status"] in ("CANCELLED", "NO_SHOW")
            }
            slots = []
            for slot in STANDARD_SLOTS:
                expired = False
                if clean_date == today_str:
                    try:
                        start = datetime.strptime(f"{clean_date} {slot}", "%Y-%m-%d %I:%M %p")
                        expired = start + timedelta(minutes=SLOT_DURATION_MINUTES) <= now
                    except ValueError:
                        pass
                elif clean_date < today_str:
                    expired = True
                if slot in by_slot:
                    appt = by_slot[slot]
                    slots.append({
                        "time_slot": slot,
                        "status": "Confirmed",
                        "appointment_id": int(appt["id"]),
                        "patient_name": appt["patient_name"],
                        "patient_phone": appt["patient_phone"],
                        "is_emergency": bool(appt["is_emergency"]),
                        "notification_status": appt["notification_status"],
                        "is_expired": expired,
                    })
                else:
                    last = recent.get(slot)
                    slots.append({
                        "time_slot": slot,
                        "status": "Available",
                        "last_event": _legacy_status(last["status"]) if last else None,
                        "is_expired": expired,
                    })
            result_doctors.append({
                "doctor_name": name,
                "specialty": doctor["specialty"],
                "available_days": _format_available_days(
                    weekdays.get(int(doctor["doctor_id"]), [])
                ),
                "slots": slots,
            })
    return {
        "date": clean_date,
        "is_today": clean_date == today_str,
        "doctors": result_doctors,
    }


__all__ = [
    "SLOT_DURATION_MINUTES",
    "STANDARD_SLOTS",
    "get_connection",
    "init_db",
    "find_user_id_by_phone",
    "set_profile_alias",
    "upsert_patient_profile",
    "load_patient_profile",
    "get_patient_profile_by_user_id",
    "get_doctors",
    "find_doctor",
    "get_patient_appointments",
    "get_patient_appointments_by_user_id",
    "get_appointment_by_id",
    "get_available_slots",
    "normalize_time_slot",
    "book_appointment",
    "cancel_appointment",
    "update_notification_status",
    "mark_appointment_arrived",
    "mark_no_show",
    "get_due_checkins",
    "reschedule_appointment",
    "book_urgent_appointment",
    "get_daily_schedule",
]
