"""Migrate the receptionist SQLite database into Neon Postgres.

Idempotent: appointment and legacy doctor IDs are preserved, and re-running
the script skips rows that already exist. Safe to run against a Neon branch.

Usage:
    python scripts/migrate_sqlite_to_neon.py --dry-run
    python scripts/migrate_sqlite_to_neon.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

WORKSPACE = Path(__file__).resolve().parents[2]
OUTER_WORKSPACE = WORKSPACE.parent
CALENDAR_DIR = WORKSPACE / "calendar"
sys.path.insert(0, str(CALENDAR_DIR))

from backend import config  # noqa: E402
from backend.datasource.legacy import parse_available_days  # noqa: E402
from backend.schema import SCHEMA as EXTENDED_SCHEMA  # noqa: E402

DEFAULT_SQLITE = WORKSPACE / "chatbot" / "clinic.db"

# A previous run could have inserted duplicate schedule rows. Keep the lowest
# id before the canonical unique index is applied. This stays in the migration
# (not in startup DDL) because it deletes rows.
DEDUPE_SCHEDULES_SQL = """
-- Keep the lowest id for each distinct schedule.
DELETE FROM doctor_schedules a USING doctor_schedules b
WHERE a.id > b.id
  AND a.doctor_id = b.doctor_id
  AND a.weekday = b.weekday
  AND a.start_time = b.start_time
  AND a.end_time = b.end_time
  AND a.slot_minutes = b.slot_minutes;
"""

STATUS_MAP = {
    "Confirmed": "CONFIRMED",
    "Cancelled": "CANCELLED",
    "No-Show": "NO_SHOW",
}


def _pg_time(slot: str) -> str:
    return datetime.strptime(str(slot).strip().upper(), "%I:%M %p").strftime("%H:%M:%S")


def _pg_timestamp(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(str(value)).replace(tzinfo=config.CLINIC_TZ)


def _json_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _reset_sequence(conn, table: str) -> None:
    conn.execute(
        "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
        "COALESCE((SELECT MAX(id) FROM " + table + "), 1))",
        (table,),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(OUTER_WORKSPACE / ".env.local", override=False)
    load_dotenv(OUTER_WORKSPACE / ".env", override=False)
    load_dotenv(WORKSPACE / ".env.local", override=False)
    load_dotenv(WORKSPACE / ".env", override=False)
    dsn = args.dsn or os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: no DATABASE_URL found. Run `neon deploy` or pass --dsn.")
        return 2
    if not args.sqlite.exists():
        print(f"ERROR: SQLite database not found: {args.sqlite}")
        return 2

    src = sqlite3.connect(args.sqlite)
    src.row_factory = sqlite3.Row

    def table_counts() -> dict[str, int]:
        return {
            name: src.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "doctors",
                "appointments",
                "patient_profiles",
                "cal_doctor_map",
                "cal_appointment_extra",
                "cal_holidays",
                "cal_audit",
                "cal_patient_addresses",
            )
        }

    counts = table_counts()
    print("SQLite source counts:")
    for name, value in counts.items():
        print(f"  {name:24} {value}")
    if args.dry_run:
        print("Dry run — no changes written.")
        return 0

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute(DEDUPE_SCHEDULES_SQL)
        conn.execute(EXTENDED_SCHEMA)
        stats = {key: 0 for key in ("doctors", "patients", "appointments", "profiles", "holidays", "audit")}

        # --- doctors, schedules, legacy mapping -------------------------
        colors = {
            row["legacy_name"]: row["color"]
            for row in src.execute("SELECT legacy_name, color FROM cal_doctor_map")
        }
        keys = {
            row["legacy_name"]: row["doctor_key"]
            for row in src.execute("SELECT legacy_name, doctor_key FROM cal_doctor_map")
        }
        for doctor in src.execute("SELECT * FROM doctors ORDER BY id"):
            name = doctor["name"]
            conn.execute(
                "INSERT INTO doctor_profiles "
                "(id, specialization, available, color) VALUES (%s, %s, TRUE, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    int(doctor["id"]),
                    doctor["specialty"],
                    colors.get(name, config.DOCTOR_COLORS[int(doctor["id"]) % len(config.DOCTOR_COLORS)]),
                ),
            )
            existing_user = conn.execute(
                "SELECT id FROM users WHERE role = 'doctor' AND lower(name) = lower(%s)",
                (name,),
            ).fetchone()
            if existing_user:
                user_id = int(existing_user["id"])
            else:
                user_id = int(
                    conn.execute(
                        "INSERT INTO users (name, role) VALUES (%s, 'doctor') RETURNING id",
                        (name,),
                    ).fetchone()["id"]
                )
            conn.execute(
                "UPDATE doctor_profiles SET user_id = %s WHERE id = %s",
                (user_id, int(doctor["id"])),
            )
            weekdays = parse_available_days(doctor["available_days"])
            for weekday in sorted(weekdays):
                for start, end in config.CLINIC_SESSIONS:
                    conn.execute(
                        "INSERT INTO doctor_schedules "
                        "(doctor_id, weekday, start_time, end_time, slot_minutes) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            int(doctor["id"]),
                            weekday,
                            start.strftime("%H:%M"),
                            end.strftime("%H:%M"),
                            config.SLOT_MINUTES,
                        ),
                    )
            conn.execute(
                "INSERT INTO doctor_legacy_map (doctor_id, legacy_name, doctor_key, color) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (doctor_id) DO NOTHING",
                (int(doctor["id"]), name, keys.get(name, f"dr-{doctor['id']}"), colors.get(name, "#1a73e8")),
            )
            stats["doctors"] += 1

        doctor_ids = {
            row["legacy_name"]: int(row["doctor_id"])
            for row in conn.execute("SELECT legacy_name, doctor_id FROM doctor_legacy_map")
        }

        # --- patients ---------------------------------------------------
        phones: set[str] = set()
        for row in src.execute("SELECT DISTINCT patient_phone FROM appointments"):
            if row["patient_phone"]:
                phones.add(str(row["patient_phone"]).strip())
        for row in src.execute("SELECT patient_phone FROM patient_profiles WHERE patient_phone IS NOT NULL"):
            phones.add(str(row["patient_phone"]).strip())
        names: dict[str, str] = {}
        for row in src.execute("SELECT patient_phone, patient_name FROM appointments ORDER BY id"):
            phone = str(row["patient_phone"]).strip()
            if phone and phone not in names:
                names[phone] = row["patient_name"]
        for phone in sorted(phones):
            conn.execute(
                "INSERT INTO patients (name, phone) VALUES (%s, %s) "
                "ON CONFLICT (phone) DO NOTHING",
                (names.get(phone, "Unknown Patient"), phone),
            )
            stats["patients"] += 1
        for row in src.execute("SELECT phone, address_json FROM cal_patient_addresses"):
            conn.execute(
                "UPDATE patients SET address = %s WHERE phone = %s AND address IS NULL",
                (row["address_json"], row["phone"]),
            )
        patient_ids = {
            row["phone"]: int(row["id"])
            for row in conn.execute("SELECT id, phone FROM patients")
        }

        # --- appointments ----------------------------------------------
        extras = {
            int(row["appointment_id"]): row
            for row in src.execute("SELECT * FROM cal_appointment_extra")
        }
        for appt in src.execute("SELECT * FROM appointments ORDER BY id"):
            phone = str(appt["patient_phone"]).strip()
            extra = extras.get(int(appt["id"]))
            conn.execute(
                """
                INSERT INTO appointments (
                    id, patient_id, doctor_id, appointment_date, appointment_time,
                    reason, description, referred_by, token_number, status,
                    emergency, source, patient_address, notification_status,
                    arrived, created_by, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    int(appt["id"]),
                    patient_ids.get(phone),
                    doctor_ids.get(appt["doctor_name"]),
                    datetime.strptime(appt["date"], "%Y-%m-%d").date(),
                    _pg_time(appt["time_slot"]),
                    extra["reason"] if extra else None,
                    extra["description"] if extra else None,
                    extra["referred_by"] if extra else None,
                    None,
                    STATUS_MAP.get(appt["status"], str(appt["status"]).upper()),
                    bool(appt["is_emergency"]),
                    (extra["source"] if extra else None) or "calendar",
                    extra["address_json"] if extra else None,
                    appt["notification_status"] or "pending",
                    bool(appt["arrived"]),
                    extra["created_by"] if extra else None,
                    _pg_timestamp(appt["created_at"]),
                ),
            )
            stats["appointments"] += 1

        # --- patient profiles -------------------------------------------
        for profile in src.execute("SELECT * FROM patient_profiles ORDER BY user_id"):
            phone = profile["patient_phone"]
            if phone and conn.execute(
                "SELECT 1 FROM patient_profiles WHERE patient_phone = %s AND user_id <> %s",
                (phone, profile["user_id"]),
            ).fetchone():
                print(f"  skip profile {profile['user_id']}: phone owned by another profile")
                continue
            conn.execute(
                """
                INSERT INTO patient_profiles (
                    user_id, patient_name, patient_phone, preferred_doctor,
                    medical_conditions, allergies, reported_symptoms, preferences,
                    last_appointment_id, conversation_summary, alias_of, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (
                    profile["user_id"],
                    profile["patient_name"],
                    phone,
                    profile["preferred_doctor"],
                    profile["medical_conditions"],
                    profile["allergies"],
                    profile["reported_symptoms"],
                    _json_value(profile["preferences"]),
                    profile["last_appointment_id"],
                    profile["conversation_summary"],
                    profile["alias_of"],
                    _pg_timestamp(profile["updated_at"]),
                ),
            )
            stats["profiles"] += 1

        # --- holidays ----------------------------------------------------
        doctor_keys = {
            row["doctor_key"]: int(row["doctor_id"])
            for row in conn.execute("SELECT doctor_key, doctor_id FROM doctor_legacy_map")
        }
        for holiday in src.execute("SELECT * FROM cal_holidays ORDER BY id"):
            doctor_id = doctor_keys.get(holiday["doctor_key"])
            day = datetime.strptime(holiday["holiday_date"], "%Y-%m-%d").date()
            end_day = (
                datetime.strptime(holiday["end_date"], "%Y-%m-%d").date()
                if holiday["end_date"]
                else None
            )
            exists = conn.execute(
                """
                SELECT 1 FROM clinic_holidays
                WHERE holiday_date = %s AND name = %s AND scope = %s
                  AND doctor_id IS NOT DISTINCT FROM %s
                """,
                (day, holiday["name"], holiday["scope"], doctor_id),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO clinic_holidays
                    (scope, doctor_id, holiday_date, end_date, name, type)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    holiday["scope"],
                    doctor_id,
                    day,
                    end_day,
                    holiday["name"],
                    holiday["type"],
                ),
            )
            stats["holidays"] += 1

        # --- audit -------------------------------------------------------
        for entry in src.execute("SELECT * FROM cal_audit ORDER BY id"):
            conn.execute(
                """
                INSERT INTO audit_log (id, actor, action, entity, entity_id, detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    int(entry["id"]),
                    entry["actor"],
                    entry["action"],
                    entry["entity"],
                    entry["entity_id"],
                    entry["detail"],
                    _pg_timestamp(entry["created_at"]),
                ),
            )
            stats["audit"] += 1

        for table in (
            "users",
            "patients",
            "doctor_profiles",
            "doctor_schedules",
            "clinic_holidays",
            "appointments",
            "audit_log",
        ):
            _reset_sequence(conn, table)

    print("\nMigration stats:")
    for name, value in stats.items():
        print(f"  {name:16} {value}")

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as verify:
        for table in (
            "users",
            "doctor_profiles",
            "doctor_schedules",
            "doctor_legacy_map",
            "patients",
            "appointments",
            "patient_profiles",
            "clinic_holidays",
            "audit_log",
        ):
            count = verify.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"  Neon {table:20} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
