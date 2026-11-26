import json
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "clinic.db")

# Standard clinic appointment slots available per day
SLOT_DURATION_MINUTES = 30

STANDARD_SLOTS = [
    # Morning Session
    "09:00 AM", "09:30 AM",
    "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM",
    "12:00 PM", "12:30 PM",
    # (Lunch: 01:00 PM - 02:00 PM)
    # Afternoon Session
    "02:00 PM", "02:30 PM",
    "03:00 PM", "03:30 PM",
    "04:00 PM", "04:30 PM",
]

def get_connection():
    """Create a connection to the SQLite database with row factory for named column access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize tables and seed doctor data if empty."""
    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. Doctors table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                specialty TEXT NOT NULL,
                available_days TEXT NOT NULL
            )
            """
        )

        # 2. Appointments table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT NOT NULL,
                patient_phone TEXT NOT NULL,
                doctor_name TEXT NOT NULL,
                date TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Confirmed',
                is_emergency INTEGER NOT NULL DEFAULT 0,
                notification_status TEXT NOT NULL DEFAULT 'pending',
                arrived INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migration: ensure is_emergency exists on existing DB files
        cursor.execute("PRAGMA table_info(appointments)")
        existing_cols = [col["name"] for col in cursor.fetchall()]
        if "is_emergency" not in existing_cols:
            cursor.execute(
                "ALTER TABLE appointments ADD COLUMN is_emergency INTEGER NOT NULL DEFAULT 0"
            )

        # Migration: ensure notification_status exists on existing DB files
        cursor.execute("PRAGMA table_info(appointments)")
        existing_cols = [col["name"] for col in cursor.fetchall()]
        if "notification_status" not in existing_cols:
            cursor.execute(
                "ALTER TABLE appointments ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'pending'"
            )

        # Migration: ensure arrived flag exists (patient presence)
        cursor.execute("PRAGMA table_info(appointments)")
        existing_cols = [col["name"] for col in cursor.fetchall()]
        if "arrived" not in existing_cols:
            cursor.execute(
                "ALTER TABLE appointments ADD COLUMN arrived INTEGER NOT NULL DEFAULT 0"
            )

        # 3. Partial Unique Index: only enforce uniqueness for Confirmed appointments
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_appointment
            ON appointments(doctor_name, date, time_slot)
            WHERE status = 'Confirmed'
            """
        )

        # 4. Seed doctors (idempotent)
        sample_doctors = [
            ("Dr. Rajesh Sharma", "General Physician", "Monday to Saturday"),
            ("Dr. Meera Kulkarni", "General Physician", "Monday to Saturday"),
            ("Dr. Amit Deshmukh", "General Physician", "Monday to Friday"),
            ("Dr. Priya Patel", "Pediatrician (Child Specialist)", "Monday, Wednesday, Friday"),
            ("Dr. Ananya Iyer", "Dermatologist (Skin Specialist)", "Tuesday, Thursday, Saturday"),
            ("Dr. Suresh Nair", "Cardiologist", "Monday, Wednesday, Friday"),
            ("Dr. Kavita Joshi", "Orthopedic Surgeon", "Tuesday, Thursday, Saturday"),
            ("Dr. Rohan Mehta", "ENT Specialist", "Monday, Tuesday, Thursday, Friday"),
            ("Dr. Neha Kapoor", "Gynecologist", "Monday, Wednesday, Friday, Saturday"),
            ("Dr. Vikram Rao", "Neurologist", "Tuesday, Thursday"),
            ("Dr. Sanjay Gupta", "Psychiatrist", "Monday, Wednesday, Friday"),
            ("Dr. Pooja Bansal", "Ophthalmologist (Eye Specialist)", "Tuesday, Thursday, Saturday"),
        ]
        for name, specialty, available_days in sample_doctors:
            cursor.execute("SELECT 1 FROM doctors WHERE name = ?", (name,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO doctors (name, specialty, available_days) VALUES (?, ?, ?)",
                    (name, specialty, available_days),
                )
        #5. Patient Profiles table (Persistent Long-Term Memory)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS patient_profiles(
            user_id TEXT PRIMARY KEY,
            patient_name TEXT,
            patient_phone TEXT UNIQUE,
            preferred_doctor TEXT,
            medical_conditions TEXT,
            allergies TEXT,
            reported_symptoms TEXT,
            preferences TEXT,
            last_appointment_id INTEGER,
            conversation_summary TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(last_appointment_id) REFERENCES appointments(id)
            )
            """
        )
        cursor.execute("PRAGMA table_info(patient_profiles)")
        existing_profile_cols = [col["name"] for col in cursor.fetchall()]
        if "alias_of" not in existing_profile_cols:
            cursor.execute("ALTER TABLE patient_profiles ADD COLUMN alias_of TEXT")
        conn.commit()
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


def find_user_id_by_phone(patient_phone: str) -> str | None:
    """Return the canonical user_id that owns a given phone number, if any."""
    if not patient_phone:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM patient_profiles WHERE patient_phone = ?",
            (str(patient_phone).strip(),),
        )
        row = cursor.fetchone()
    return row["user_id"] if row else None


def set_profile_alias(user_id: str, canonical_user_id: str) -> None:
    """Mark user_id as an alias of canonical_user_id so lookups resolve to the same patient."""
    if not user_id or not canonical_user_id or user_id == canonical_user_id:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO patient_profiles (user_id, alias_of, patient_phone, updated_at)
            VALUES (?, ?, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                alias_of = excluded.alias_of,
                patient_phone = NULL,
                patient_name = NULL,
                preferred_doctor = NULL,
                medical_conditions = NULL,
                allergies = NULL,
                reported_symptoms = NULL,
                preferences = NULL,
                conversation_summary = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, canonical_user_id),
        )
        conn.commit()


def upsert_patient_profile(user_id: str, data: dict) -> None:
    """Insert or update a patient profile. Only overwrites fields that have new data."""
    history = data.get("medical_history", {}) if isinstance(data.get("medical_history"), dict) else {}
    medical_conditions = history.get("conditions", data.get("medical_conditions"))
    allergies = history.get("allergies", data.get("allergies"))
    reported_symptoms = history.get("reported_symptoms", data.get("reported_symptoms"))
    preferences = data.get("preferences")
    patient_phone = data.get("patient_phone")
    if patient_phone:
        patient_phone = str(patient_phone).strip() or None

    with get_connection() as conn:
        cursor = conn.cursor()
        # If this user_id is an alias, persist to canonical profile instead.
        cursor.execute("SELECT alias_of FROM patient_profiles WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row["alias_of"] and row["alias_of"] != user_id:
            user_id = row["alias_of"]

        # Never violate phone uniqueness: if phone belongs to another profile, persist there.
        if patient_phone:
            cursor.execute(
                "SELECT user_id FROM patient_profiles WHERE patient_phone = ? AND user_id != ?",
                (patient_phone, user_id),
            )
            owner = cursor.fetchone()
            if owner and owner["user_id"]:
                user_id = owner["user_id"]

        cursor.execute(
            """
            INSERT INTO patient_profiles (
                user_id, patient_name, patient_phone,
                preferred_doctor, medical_conditions,
                allergies, reported_symptoms,
                preferences, conversation_summary,
                alias_of, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                patient_name       = COALESCE(excluded.patient_name,       patient_name),
                patient_phone      = COALESCE(excluded.patient_phone,      patient_phone),
                preferred_doctor   = COALESCE(excluded.preferred_doctor,   preferred_doctor),
                medical_conditions = COALESCE(excluded.medical_conditions, medical_conditions),
                allergies          = COALESCE(excluded.allergies,          allergies),
                reported_symptoms  = COALESCE(excluded.reported_symptoms,  reported_symptoms),
                preferences        = COALESCE(excluded.preferences,        preferences),
                conversation_summary = COALESCE(excluded.conversation_summary, conversation_summary),
                alias_of           = NULL,
                updated_at         = CURRENT_TIMESTAMP
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
            )
        )
        conn.commit()

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


def load_patient_profile(user_id: str) -> dict | None:
    """Load a patient's profile from SQLite. Returns None if not found."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM patient_profiles WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
    if row is None:
        return None

    data = dict(row)
    alias_of = data.get("alias_of")
    if alias_of and alias_of != user_id:
        return load_patient_profile(alias_of)

    return {
        "patient_name": data.get("patient_name"),
        "patient_phone": data.get("patient_phone"),
        "preferred_doctor": data.get("preferred_doctor"),
        "medical_history": {
            "conditions": _from_json_list(data.get("medical_conditions")),
            "allergies": _from_json_list(data.get("allergies")),
            "reported_symptoms": _from_json_list(data.get("reported_symptoms")),
            "medications": [],
        },
        "conversation_summary": data.get("conversation_summary") or "",
        "last_appointment_id": data.get("last_appointment_id"),
    }

def get_doctors() -> list[dict]:
    """Return all doctors with their specialties and available days."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, specialty, available_days FROM doctors ORDER BY id"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def find_doctor(cursor, query: str) -> str | None:
    """Fuzzy match doctor name (e.g. 'Dr. Sharma', 'Sharma', 'Dr. Rajesh') to full DB name."""
    clean = query.lower().replace("dr.", "").replace("dr", "").strip()

    # 1. Try matching the cleaned query directly
    cursor.execute(
        "SELECT name FROM doctors WHERE LOWER(name) LIKE LOWER(?)",
        (f"%{clean}%",),
    )
    row = cursor.fetchone()
    if row:
        return row["name"]

    # 2. Try matching individual words (e.g., last name 'Sharma' or 'Patel')
    for word in clean.split():
        if len(word) > 2:
            cursor.execute(
                "SELECT name FROM doctors WHERE LOWER(name) LIKE LOWER(?)",
                (f"%{word}%",),
            )
            row = cursor.fetchone()
            if row:
                return row["name"]

    return None


def get_patient_appointments(patient_phone: str) -> list[dict]:
    """Return all confirmed appointments for a patient by phone number."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, patient_name, doctor_name, date, time_slot, status, is_emergency
            FROM appointments
            WHERE patient_phone = ? AND status = 'Confirmed'
            ORDER BY date, time_slot
            """,
            (patient_phone.strip(),),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_appointment_by_id(appointment_id: int) -> dict | None:
    """Return one appointment (with patient phone) or None."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, patient_name, patient_phone, doctor_name, date, time_slot,
                   status, is_emergency
            FROM appointments
            WHERE id = ?
            """,
            (appointment_id,),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def get_patient_profile_by_user_id(user_id: str) -> dict | None:
    """Return stored patient profile for a user_id."""
    return load_patient_profile(user_id)


def get_patient_appointments_by_user_id(user_id: str) -> dict:
    """Return confirmed appointments for a patient by resolving phone from stored profile."""
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

    appointments = get_patient_appointments(phone)
    return {
        "success": True,
        "user_id": user_id,
        "patient_name": profile.get("patient_name"),
        "patient_phone": phone,
        "appointments": appointments,
    }


def get_available_slots(doctor_name: str, date: str) -> list[str]:
    """
    Return remaining free time slots for a specific doctor on a given date (YYYY-MM-DD).
    Subtracts already booked slots and filters out slots that have already passed for today.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # Check if the doctor exists
        matched_doctor_name = find_doctor(cursor, doctor_name)
        if not matched_doctor_name:
            return []

        clean_date = date.strip()

        # Fetch booked slots for that doctor on that date (only Confirmed ones)
        cursor.execute(
            """
            SELECT time_slot FROM appointments
            WHERE doctor_name = ? AND date = ? AND status = 'Confirmed'
            """,
            (matched_doctor_name, clean_date),
        )
        booked_slots = {row["time_slot"] for row in cursor.fetchall()}

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        free_slots = []
        for slot in STANDARD_SLOTS:
            if slot in booked_slots:
                continue

            # If date is today, exclude slots whose consultation window has ended
            if clean_date == today_str:
                try:
                    slot_start = datetime.strptime(f"{clean_date} {slot}", "%Y-%m-%d %I:%M %p")
                    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)
                    # The slot remains available within its window (e.g. for urgent/walk-ins after a no-show)
                    if slot_end <= now:
                        continue
                except ValueError:
                    pass
            elif clean_date < today_str:
                # Past dates have no available slots
                continue

            free_slots.append(slot)

        return free_slots


def normalize_time_slot(time_str: str) -> str | None:
    """Normalize user or LLM time string (e.g. '09:00', '9:00 am', '14:00', '2pm') to 'HH:MM AM/PM'."""
    clean = time_str.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(clean, fmt)
            return dt.strftime("%I:%M %p")
        except ValueError:
            continue
    return None


def book_appointment(
    patient_name: str,
    patient_phone: str,
    doctor_name: str,
    date: str,
    time_slot: str,
    is_emergency: int = 0,
) -> dict:
    """
    Book an appointment.
    Returns success confirmation with appointment ID, or failure reason.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # Find matching doctor
        resolved_doctor_name = find_doctor(cursor, doctor_name)
        if not resolved_doctor_name:
            return {
                "success": False,
                "error": f"Doctor '{doctor_name}' was not found in our clinic directory.",
            }

        # Normalize and validate time slot
        normalized_slot = normalize_time_slot(time_slot)
        if not normalized_slot:
            return {
                "success": False,
                "error": f"Invalid time format '{time_slot}'. Please use standard clinic slots (e.g., '09:00 AM', '02:00 PM').",
            }

        if normalized_slot not in STANDARD_SLOTS:
            return {
                "success": False,
                "error": f"'{time_slot}' is not a standard clinic appointment slot. Standard clinic slots are: {', '.join(STANDARD_SLOTS)}.",
            }

        # Validate that the date and slot are not in the past
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        clean_date = date.strip()

        if clean_date < today_str:
            return {
                "success": False,
                "error": f"Cannot book appointments for past dates ({clean_date}).",
            }
        elif clean_date == today_str:
            try:
                slot_start = datetime.strptime(f"{clean_date} {normalized_slot}", "%Y-%m-%d %I:%M %p")
                slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)
                if slot_end <= now:
                    return {
                        "success": False,
                        "error": f"The slot {normalized_slot} on {clean_date} has already ended. Please choose an upcoming time slot.",
                    }
            except ValueError:
                pass

        # Attempt insertion (guarded by the partial unique index)
        try:
            cursor.execute(
                """
                INSERT INTO appointments (
                    patient_name,
                    patient_phone,
                    doctor_name,
                    date,
                    time_slot,
                    status,
                    is_emergency,
                    notification_status
                )
                VALUES (?, ?, ?, ?, ?, 'Confirmed', ?, 'pending')
                """,
                (
                    patient_name.strip(),
                    patient_phone.strip(),
                    resolved_doctor_name,
                    clean_date,
                    normalized_slot,
                    is_emergency,
                ),
            )
            conn.commit()
            appointment_id = cursor.lastrowid
            return {
                "success": True,
                "appointment_id": appointment_id,
                "patient_name": patient_name,
                "doctor_name": resolved_doctor_name,
                "date": clean_date,
                "time_slot": normalized_slot,
                "status": "Confirmed",
                "is_emergency": bool(is_emergency),
                "notification_status": "pending",
            }
        except sqlite3.IntegrityError:
            # Triggered when slot already has an active Confirmed appointment
            return {
                "success": False,
                "error": f"The slot {time_slot} on {date} with {resolved_doctor_name} is already booked. Please choose another time.",
            }


def get_patient_profile_by_user_id(user_id: str) -> dict | None:
    """Return stored patient profile for a user_id."""
    return load_patient_profile(user_id)


def get_patient_appointments_by_user_id(user_id: str) -> dict:
    """Return confirmed appointments for a patient by resolving phone from stored profile."""
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

    appointments = get_patient_appointments(phone)
    return {
        "success": True,
        "user_id": user_id,
        "patient_name": profile.get("patient_name"),
        "patient_phone": phone,
        "appointments": appointments,
    }


def cancel_appointment(appointment_id: int, reason: str = "Cancelled by staff/patient") -> dict:
    """
    Cancel an appointment by its ID.
    Marks status as 'Cancelled', which immediately frees the slot.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # Check if the appointment exists
        cursor.execute(
            """
            SELECT id, patient_name, doctor_name, date, time_slot, status, is_emergency
            FROM appointments
            WHERE id = ?
            """,
            (appointment_id,),
        )
        row = cursor.fetchone()

        if not row:
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} was not found.",
            }

        if row["status"] in ("Cancelled", "No-Show"):
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} has already been marked as {row['status']}.",
            }

        # Update status to Cancelled
        cursor.execute(
            "UPDATE appointments SET status = 'Cancelled' WHERE id = ?",
            (appointment_id,),
        )
        conn.commit()

        return {
            "success": True,
            "appointment_id": appointment_id,
            "patient_name": row["patient_name"],
            "doctor_name": row["doctor_name"],
            "date": row["date"],
            "time_slot": row["time_slot"],
            "status": "Cancelled",
            "reason": reason,
        }


def update_notification_status(appointment_id: int, status: str) -> bool:
    """Update notification_status for an appointment."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE appointments SET notification_status = ? WHERE id = ?",
            (status, appointment_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_appointment_arrived(appointment_id: int) -> bool:
    """Mark a patient as arrived; suppresses T+10 check-in."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE appointments SET arrived = 1 WHERE id = ?", (appointment_id,))
        conn.commit()
        return cursor.rowcount > 0


def mark_no_show(appointment_id: int, reason: str = "Patient No-Show") -> dict:
    """Mark appointment as no-show, free slot, set status Cancelled."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM appointments WHERE id = ?", (appointment_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Appointment not found."}
        if row["status"] != "Confirmed":
            return {"success": False, "error": "Appointment is not confirmed."}
        cursor.execute(
            "UPDATE appointments SET status = 'Cancelled', notification_status = 'no_show' WHERE id = ?",
            (appointment_id,),
        )
        conn.commit()
        return {"success": True, "appointment_id": appointment_id, "reason": reason}


def get_due_checkins(now: datetime | None = None) -> list[dict]:
    """Return Confirmed appointments due for T+10 check-in (notification_status='sent')."""
    if now is None:
        try:
            from notifications import clinic_tz, CHECKIN_MINUTES_AFTER
            now = datetime.now(clinic_tz())
        except Exception:
            now = datetime.now()
            CHECKIN_MINUTES_AFTER = 10
    else:
        try:
            from notifications import CHECKIN_MINUTES_AFTER
        except Exception:
            CHECKIN_MINUTES_AFTER = 10
    # Strip tz info for comparison with naive slot datetimes
    now_naive = now.replace(tzinfo=None) if hasattr(now, 'tzinfo') and now.tzinfo else now
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, patient_name, patient_phone, doctor_name, date, time_slot, is_emergency
            FROM appointments
            WHERE status = 'Confirmed' AND notification_status = 'sent' AND COALESCE(arrived,0)=0
            """,
        )
        rows = [dict(r) for r in cursor.fetchall()]
    due = []
    for row in rows:
        try:
            slot_start = datetime.strptime(f"{row['date']} {row['time_slot']}", "%Y-%m-%d %I:%M %p")
        except ValueError:
            continue
        checkin_time = slot_start + timedelta(minutes=CHECKIN_MINUTES_AFTER)
        if now_naive >= checkin_time:
            due.append(row)
    return due


def reschedule_appointment(appointment_id: int) -> dict:
    """Reschedule a confirmed appointment to the next available same-doctor/same-day slot.

    Frees the current slot (marks Cancelled) and books the next available slot
    for the same patient/doctor/date. Returns success with new details or failure.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, patient_name, patient_phone, doctor_name, date, time_slot, status, is_emergency "
            "FROM appointments WHERE id = ?",
            (appointment_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": f"Appointment #{appointment_id} not found."}
        row = dict(row)
        if row["status"] not in ("Confirmed", "pending_reply", "confirmed_not_coming"):
            return {"success": False, "error": f"Appointment #{appointment_id} is {row['status']}, cannot reschedule."}

        doctor = row["doctor_name"]
        appt_date = row["date"]
        current_slot = row["time_slot"]
        patient_name = row["patient_name"]
        patient_phone = row["patient_phone"]
        is_emergency = row["is_emergency"]

        # find next available slot for same doctor, same day
        available = get_available_slots(doctor, appt_date)
        from notifications import find_next_available_same_day_slot
        new_slot = find_next_available_same_day_slot(current_slot, available)
        if not new_slot:
            return {"success": False, "error": "No available slots remaining for this doctor today.", "no_slot": True}

        # free current slot
        cursor.execute("UPDATE appointments SET status = 'Cancelled', notification_status = 'rescheduled' WHERE id = ?", (appointment_id,))
        # book new slot
        try:
            cursor.execute(
                "INSERT INTO appointments (patient_name, patient_phone, doctor_name, date, time_slot, status, is_emergency, notification_status) "
                "VALUES (?, ?, ?, ?, ?, 'Confirmed', ?, 'pending')",
                (patient_name, patient_phone, doctor, appt_date, new_slot, is_emergency),
            )
            conn.commit()
            new_id = cursor.lastrowid
            return {
                "success": True,
                "old_appointment_id": appointment_id,
                "new_appointment_id": new_id,
                "patient_name": patient_name,
                "doctor_name": doctor,
                "date": appt_date,
                "time_slot": new_slot,
            }
        except sqlite3.IntegrityError:
            conn.rollback()
            # slot taken between check and book - retry not available, return failure
            return {"success": False, "error": f"Slot {new_slot} was just taken. Please try again or reschedule via the portal.", "no_slot": True}


def mark_no_show(appointment_id: int, reason: str = "No-Show after 10 min grace") -> dict:
    """
    Mark an appointment as No-Show by clinic staff.
    Marks status as 'No-Show', which immediately frees the slot in SQLite
    because the unique index only enforces uniqueness for 'Confirmed' appointments.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, patient_name, doctor_name, date, time_slot, status, is_emergency
            FROM appointments
            WHERE id = ?
            """,
            (appointment_id,),
        )
        row = cursor.fetchone()

        if not row:
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} was not found.",
            }

        if row["status"] in ("Cancelled", "No-Show"):
            return {
                "success": False,
                "error": f"Appointment #{appointment_id} is already marked as {row['status']}.",
            }

        cursor.execute(
            "UPDATE appointments SET status = 'No-Show' WHERE id = ?",
            (appointment_id,),
        )
        conn.commit()

        return {
            "success": True,
            "appointment_id": appointment_id,
            "patient_name": row["patient_name"],
            "doctor_name": row["doctor_name"],
            "date": row["date"],
            "time_slot": row["time_slot"],
            "status": "No-Show",
            "reason": reason,
            "message": f"Appointment #{appointment_id} marked as No-Show. Slot {row['time_slot']} on {row['date']} is now immediately open for other patients.",
        }


def book_urgent_appointment(
    patient_name: str,
    patient_phone: str,
    doctor_name: str = "Dr. Rajesh Sharma",
) -> dict:
    """
    Automatically book the earliest available slot today for an urgent/emergency patient.
    Defaults to General Physician (Dr. Rajesh Sharma).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    available_slots = get_available_slots(doctor_name, today)

    if not available_slots:
        return {
            "success": False,
            "error": f"No remaining regular slots available with {doctor_name} for today ({today}). Please direct the patient to the emergency walk-in desk or nearest ER immediately.",
            "date": today,
            "doctor_name": doctor_name,
        }

    earliest_slot = available_slots[0]
    return book_appointment(
        patient_name=patient_name,
        patient_phone=patient_phone,
        doctor_name=doctor_name,
        date=today,
        time_slot=earliest_slot,
        is_emergency=1,
    )


def get_daily_schedule(date: str = None) -> dict:
    """Return live schedule grid of all doctors and their 14 standard slots for a given date."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    clean_date = date.strip()
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    doctors = get_doctors()

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, patient_name, patient_phone, doctor_name, date, time_slot, status, is_emergency
            FROM appointments
            WHERE date = ?
            ORDER BY id DESC
            """,
            (clean_date,),
        )
        all_appts = [dict(row) for row in cursor.fetchall()]

    schedule_by_doc = []
    for doc in doctors:
        doc_name = doc["name"]
        doc_appts = {
            appt["time_slot"]: appt
            for appt in all_appts
            if appt["doctor_name"] == doc_name and appt["status"] == "Confirmed"
        }
        recent_cancelled = {
            appt["time_slot"]: appt
            for appt in all_appts
            if appt["doctor_name"] == doc_name and appt["status"] in ("Cancelled", "No-Show")
        }

        slots_status = []
        for slot in STANDARD_SLOTS:
            is_expired = False
            if clean_date == today_str:
                try:
                    slot_start = datetime.strptime(f"{clean_date} {slot}", "%Y-%m-%d %I:%M %p")
                    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)
                    if slot_end <= now:
                        is_expired = True
                except ValueError:
                    pass
            elif clean_date < today_str:
                is_expired = True

            if slot in doc_appts:
                appt = doc_appts[slot]
                slots_status.append({
                    "time_slot": slot,
                    "status": "Confirmed",
                    "appointment_id": appt["id"],
                    "patient_name": appt["patient_name"],
                    "patient_phone": appt["patient_phone"],
                    "is_emergency": bool(appt.get("is_emergency")),
                    "notification_status": appt.get("notification_status", "pending"),
                    "is_expired": is_expired,
                })
            else:
                last_event = recent_cancelled.get(slot, {}).get("status")
                slots_status.append({
                    "time_slot": slot,
                    "status": "Available",
                    "last_event": last_event,
                    "is_expired": is_expired,
                })

        schedule_by_doc.append({
            "doctor_name": doc_name,
            "specialty": doc["specialty"],
            "available_days": doc["available_days"],
            "slots": slots_status,
        })

    return {
        "date": clean_date,
        "is_today": clean_date == today_str,
        "doctors": schedule_by_doc,
    }



# Auto-initialize the active backend on first load. In Neon mode all public
# functions above are replaced by their Postgres implementations.
if os.getenv("DATA_SOURCE", "legacy").strip().lower() == "neon":
    # This module can be loaded by file path (e.g. the calendar test fixtures
    # use importlib.util.spec_from_file_location), in which case its own
    # directory is not on sys.path. Make the sibling import work in both the
    # normal app run and that importlib scenario.
    import sys as _sys
    from pathlib import Path as _Path

    _self_dir = str(_Path(__file__).resolve().parent)
    _added_path = _self_dir not in _sys.path
    if _added_path:
        _sys.path.insert(0, _self_dir)
    try:
        from database_neon import *  # noqa: F401,F403
    finally:
        if _added_path:
            _sys.path.remove(_self_dir)
else:
    init_db()
