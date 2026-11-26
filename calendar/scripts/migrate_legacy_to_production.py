"""Copy the legacy receptionist database into the production-shaped schema.

Usage
-----
    python scripts/migrate_legacy_to_production.py \
        --legacy-db ../chatbot/clinic.db \
        --production-db production.db

The script is idempotent for reference data (doctors/holidays are matched by
name) but refuses to append appointments to a non-empty target unless
``--force`` is passed, in which case the target tables are cleared first.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.datasource.legacy import LegacyDataSource  # noqa: E402
from backend.datasource.production import ProductionDataSource  # noqa: E402

WINDOW_START = date(2000, 1, 1)
WINDOW_END = date(2100, 1, 1)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _clear(conn: sqlite3.Connection) -> None:
    for table in (
        "appointments",
        "doctor_schedules",
        "clinic_holidays",
        "audit_log",
        "doctor_profiles",
        "patients",
        "users",
    ):
        conn.execute(f"DELETE FROM {table}")


def migrate(legacy_db: Path, production_db: Path, force: bool = False) -> dict:
    legacy = LegacyDataSource(
        db_path=legacy_db, receptionist_dir=config.RECEPTIONIST_DIR
    )
    production = ProductionDataSource(db_path=production_db)

    conn = _connect(production_db)
    counts = {"doctors": 0, "patients": 0, "appointments": 0, "holidays": 0}

    existing = conn.execute("SELECT COUNT(*) AS total FROM appointments").fetchone()[
        "total"
    ]
    if existing and not force:
        raise SystemExit(
            f"{production_db} already contains {existing} appointments. "
            "Re-run with --force to replace them."
        )
    if existing and force:
        _clear(conn)
        conn.commit()

    # --- doctors + schedules ---------------------------------------------
    doctor_map: dict[str, int] = {}
    for doctor in legacy.get_doctors():
        row = conn.execute(
            "SELECT id FROM users WHERE role = 'doctor' AND lower(name) = lower(?)",
            (doctor.full_name,),
        ).fetchone()
        if row:
            user_id = int(row["id"])
        else:
            cursor = conn.execute(
                "INSERT INTO users (name, role) VALUES (?, 'doctor')",
                (doctor.full_name,),
            )
            user_id = int(cursor.lastrowid)

        row = conn.execute(
            "SELECT id FROM doctor_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            doctor_id = int(row["id"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO doctor_profiles
                    (user_id, specialization, qualification, experience_years,
                     consultation_fee, room_number, available, color)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    doctor.specialization,
                    doctor.qualification,
                    doctor.experience_years,
                    float(doctor.consultation_fee)
                    if doctor.consultation_fee is not None
                    else None,
                    doctor.room_number,
                    1 if doctor.available else 0,
                    doctor.color,
                ),
            )
            doctor_id = int(cursor.lastrowid)
            counts["doctors"] += 1

        conn.execute(
            "DELETE FROM doctor_schedules WHERE doctor_id = ?", (doctor_id,)
        )
        for schedule in doctor.schedules:
            conn.execute(
                """
                INSERT INTO doctor_schedules
                    (doctor_id, weekday, start_time, end_time, slot_minutes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    doctor_id,
                    schedule.weekday,
                    schedule.start_time.strftime("%H:%M"),
                    schedule.end_time.strftime("%H:%M"),
                    schedule.slot_minutes,
                ),
            )
        doctor_map[doctor.id] = doctor_id

    # --- patients + appointments -----------------------------------------
    events = legacy.list_events(WINDOW_START, WINDOW_END)
    patient_cache: dict[str, int] = {}
    for event in events:
        doctor_id = doctor_map.get(event.doctor_id)
        if doctor_id is None:
            continue

        phone = event.patient_phone or f"unknown-{event.patient_id}"
        if phone in patient_cache:
            patient_id = patient_cache[phone]
        else:
            row = conn.execute(
                "SELECT id FROM patients WHERE phone = ?", (phone,)
            ).fetchone()
            if row:
                patient_id = int(row["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO patients (name, phone) VALUES (?, ?)",
                    (event.patient_name, phone),
                )
                patient_id = int(cursor.lastrowid)
                counts["patients"] += 1
            patient_cache[phone] = patient_id

        try:
            conn.execute(
                """
                INSERT INTO appointments
                    (id, patient_id, doctor_id, appointment_date, appointment_time,
                     reason, description, referred_by, token_number, status,
                     emergency, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(event.id),
                    patient_id,
                    doctor_id,
                    event.start.date().isoformat(),
                    event.start.strftime("%H:%M"),
                    event.reason,
                    event.description,
                    event.referred_by,
                    event.token_number,
                    event.status.value,
                    1 if event.emergency else 0,
                    event.source or "migration",
                    event.created_at.isoformat(sep=" ")
                    if event.created_at
                    else None,
                ),
            )
            counts["appointments"] += 1
        except sqlite3.IntegrityError as exc:
            print(f"  skipped appointment #{event.id}: {exc}")

    # --- holidays ---------------------------------------------------------
    for holiday in legacy.list_holidays(WINDOW_START, WINDOW_END):
        conn.execute(
            """
            INSERT INTO clinic_holidays
                (scope, doctor_id, holiday_date, end_date, name, type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                holiday.scope,
                doctor_map.get(holiday.doctor_id) if holiday.doctor_id else None,
                holiday.date.isoformat(),
                holiday.end_date.isoformat() if holiday.end_date else None,
                holiday.name,
                holiday.type,
            ),
        )
        counts["holidays"] += 1

    conn.commit()

    # --- audit trail (best effort) ---------------------------------------
    try:
        legacy_conn = sqlite3.connect(legacy_db)
        legacy_conn.row_factory = sqlite3.Row
        rows = legacy_conn.execute(
            "SELECT actor, action, entity, entity_id, detail, created_at "
            "FROM cal_audit ORDER BY id"
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO audit_log (actor, action, entity, entity_id, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["actor"],
                    row["action"],
                    row["entity"],
                    row["entity_id"],
                    row["detail"],
                    row["created_at"],
                ),
            )
        conn.commit()
        legacy_conn.close()
    except sqlite3.Error:
        pass

    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-db",
        type=Path,
        default=config.CLINIC_DB_PATH,
        help="Path to the receptionist clinic.db",
    )
    parser.add_argument(
        "--production-db",
        type=Path,
        default=config.PRODUCTION_DB_PATH,
        help="Path to the production-shaped database to create",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing appointments in the target database",
    )
    args = parser.parse_args()

    counts = migrate(args.legacy_db, args.production_db, force=args.force)
    print(f"Migrated into {args.production_db}:")
    for key, value in counts.items():
        print(f"  {key:12} {value}")


if __name__ == "__main__":
    main()
