"""Production-shaped SQLite data source.

This adapter speaks the client's schema (`users`, `patients`, `doctor_profiles`,
`appointments`) plus the four additions proposed in the plan
(`doctor_schedules`, `clinic_holidays`, the active-slot unique index and
`audit_log`). It is the reference implementation for the eventual
PostgreSQL/MySQL adapter: only this file changes when the real database lands.

Enable with ``DATA_SOURCE=production`` and ``PRODUCTION_DB_PATH=...``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .. import config
from ..domain import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentStatus,
    BookingError,
    DataSourceError,
    Doctor,
    Holiday,
    Patient,
    Schedule,
    address_from_json,
    address_to_json,
)
from .base import DataSource
from .legacy import parse_available_days
from .sqlite_helpers import (
    DB_CONNECT_TIMEOUT_SECONDS,
    configure_connection,
    retry_sqlite_write,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    role          TEXT NOT NULL DEFAULT 'receptionist',
    password_hash TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    gender        TEXT,
    date_of_birth TEXT,
    address       TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctor_profiles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER REFERENCES users(id),
    specialization   TEXT NOT NULL,
    qualification    TEXT,
    experience_years INTEGER,
    consultation_fee REAL,
    room_number      TEXT,
    available        INTEGER NOT NULL DEFAULT 1,
    color            TEXT
);

CREATE TABLE IF NOT EXISTS doctor_schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id    INTEGER NOT NULL REFERENCES doctor_profiles(id) ON DELETE CASCADE,
    weekday      INTEGER NOT NULL,
    start_time   TEXT NOT NULL,
    end_time     TEXT NOT NULL,
    slot_minutes INTEGER NOT NULL DEFAULT 30
);

CREATE TABLE IF NOT EXISTS clinic_holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'CLINIC',
    doctor_id    INTEGER REFERENCES doctor_profiles(id) ON DELETE CASCADE,
    holiday_date TEXT NOT NULL,
    end_date     TEXT,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT 'PUBLIC_HOLIDAY'
);

CREATE TABLE IF NOT EXISTS appointments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id          INTEGER NOT NULL REFERENCES patients(id),
    doctor_id           INTEGER NOT NULL REFERENCES doctor_profiles(id),
    appointment_date    TEXT NOT NULL,
    appointment_time    TEXT NOT NULL,
    reason              TEXT,
    description         TEXT,
    referred_by         TEXT,
    token_number        INTEGER,
    status              TEXT NOT NULL DEFAULT 'BOOKED',
    emergency           INTEGER NOT NULL DEFAULT 0,
    source              TEXT DEFAULT 'calendar',
    patient_address     TEXT,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    notification_status TEXT NOT NULL DEFAULT 'pending',
    arrived             INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot
    ON appointments (doctor_id, appointment_date, appointment_time)
    WHERE status IN ('BOOKED', 'CONFIRMED', 'CHECKED_IN');

CREATE INDEX IF NOT EXISTS ix_appointments_date
    ON appointments (appointment_date);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_ACTIVE_SQL = "('BOOKED', 'CONFIRMED', 'CHECKED_IN')"

_APPOINTMENT_SELECT = """
SELECT a.id, a.patient_id, a.doctor_id, a.appointment_date, a.appointment_time,
       a.reason, a.description, a.referred_by, a.token_number, a.status,
       a.emergency, a.source, a.created_at,
       p.name AS patient_name, p.phone AS patient_phone,
       p.address AS patient_profile_address, a.patient_address,
       u.name AS doctor_name, dp.color AS doctor_color
FROM appointments a
JOIN patients p ON p.id = a.patient_id
JOIN doctor_profiles dp ON dp.id = a.doctor_id
LEFT JOIN users u ON u.id = dp.user_id
"""


def _parse_time(value: str) -> time:
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise DataSourceError(f"Invalid appointment_time '{value}'.", code="bad_time")


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


class ProductionDataSource(DataSource):
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or config.PRODUCTION_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._init_schema()

    # -- plumbing ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_CONNECT_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return configure_connection(conn)

    @staticmethod
    def _db_busy_error(exc: sqlite3.OperationalError) -> BookingError:
        return BookingError(
            "The clinic database is busy. Please try again shortly.",
            code="db_busy",
        )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(appointments)")
            }
            if "patient_address" not in columns:
                conn.execute(
                    "ALTER TABLE appointments ADD COLUMN patient_address TEXT"
                )
            conn.commit()

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        entity: str,
        entity_id: str,
        detail: dict | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, entity, str(entity_id), json.dumps(detail or {})),
        )

    # -- reference data ----------------------------------------------------

    def get_clinic_info(self) -> dict[str, Any]:
        return {
            "name": "DnyanAI Hospital",
            "address": "Pune, Maharashtra",
            "phone": "020-12345678",
            "opening_hours": "Monday to Saturday, 9 AM to 6 PM",
        }

    def get_doctors(self) -> list[Doctor]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT dp.id, dp.specialization, dp.qualification,
                       dp.experience_years, dp.consultation_fee, dp.room_number,
                       dp.available, dp.color, u.name AS full_name
                FROM doctor_profiles dp
                LEFT JOIN users u ON u.id = dp.user_id
                ORDER BY dp.id
                """
            ).fetchall()
            schedule_rows = conn.execute(
                "SELECT * FROM doctor_schedules ORDER BY doctor_id, weekday, start_time"
            ).fetchall()

        schedules: dict[int, list[Schedule]] = {}
        for row in schedule_rows:
            schedules.setdefault(row["doctor_id"], []).append(
                Schedule(
                    weekday=row["weekday"],
                    start_time=_parse_time(row["start_time"]),
                    end_time=_parse_time(row["end_time"]),
                    slot_minutes=row["slot_minutes"] or config.SLOT_MINUTES,
                )
            )

        doctors: list[Doctor] = []
        for index, row in enumerate(rows):
            doctors.append(
                Doctor(
                    id=str(row["id"]),
                    full_name=row["full_name"] or f"Doctor {row['id']}",
                    specialization=row["specialization"],
                    qualification=row["qualification"],
                    experience_years=row["experience_years"],
                    consultation_fee=row["consultation_fee"],
                    room_number=row["room_number"],
                    available=bool(row["available"]),
                    color=row["color"]
                    or config.DOCTOR_COLORS[index % len(config.DOCTOR_COLORS)],
                    schedules=schedules.get(row["id"], []),
                )
            )
        return doctors

    def get_doctor(self, doctor_id: str) -> Doctor | None:
        for doctor in self.get_doctors():
            if doctor.id == str(doctor_id):
                return doctor
        return None

    # -- appointments ------------------------------------------------------

    def _row_to_appointment(self, row: sqlite3.Row) -> Appointment:
        day = _parse_date(row["appointment_date"])
        start_time = _parse_time(row["appointment_time"])
        start = datetime.combine(day, start_time, tzinfo=config.CLINIC_TZ)
        end = start + timedelta(minutes=config.SLOT_MINUTES)
        try:
            status = AppointmentStatus(str(row["status"]).upper())
        except ValueError:
            status = AppointmentStatus.BOOKED
        return Appointment(
            id=str(row["id"]),
            patient_id=str(row["patient_id"]),
            patient_name=row["patient_name"],
            patient_phone=row["patient_phone"],
            doctor_id=str(row["doctor_id"]),
            doctor_name=row["doctor_name"] or f"Doctor {row['doctor_id']}",
            start=start,
            end=end,
            status=status,
            emergency=bool(row["emergency"]),
            arrived=status == AppointmentStatus.CHECKED_IN,
            reason=row["reason"],
            description=row["description"],
            referred_by=row["referred_by"],
            token_number=row["token_number"],
            source=row["source"] or "calendar",
            created_at=(
                datetime.fromisoformat(str(row["created_at"]))
                if row["created_at"]
                else None
            ),
            patient_address=address_from_json(row["patient_address"])
            or address_from_json(row["patient_profile_address"]),
        )

    def list_events(
        self, start: date, end: date, doctor_id: str | None = None
    ) -> list[Appointment]:
        query = (
            _APPOINTMENT_SELECT
            + " WHERE a.appointment_date BETWEEN ? AND ?"
        )
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if doctor_id:
            try:
                identifier = int(doctor_id)
            except (TypeError, ValueError):
                return []
            query += " AND a.doctor_id = ?"
            params.append(identifier)
        query += " ORDER BY a.appointment_date, a.appointment_time, a.id"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_appointment(row) for row in rows]

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        try:
            identifier = int(appointment_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                _APPOINTMENT_SELECT + " WHERE a.id = ?", (identifier,)
            ).fetchone()
        return self._row_to_appointment(row) if row else None

    def get_day_grid(
        self, day: date, doctor_id: str | None = None
    ) -> dict[str, Any]:
        doctors = self.get_doctors()
        if doctor_id:
            doctors = [doctor for doctor in doctors if doctor.id == str(doctor_id)]
        holidays = self.list_holidays(day, day)
        events = [event for event in self.list_events(day, day, doctor_id) if event.is_active]
        by_slot = {(event.doctor_id, event.start.time()): event for event in events}

        grid: list[dict[str, Any]] = []
        for doctor in doctors:
            holiday = next(
                (item for item in holidays if item.covers(day, doctor.id)), None
            )
            blocking_holiday = next(
                (
                    item
                    for item in holidays
                    if item.blocks_booking and item.covers(day, doctor.id)
                ),
                None,
            )
            windows = [
                schedule
                for schedule in doctor.schedules
                if schedule.weekday == day.weekday()
            ]
            slots: list[dict[str, Any]] = []
            for window in windows:
                for slot_time in window.slots():
                    appointment = by_slot.get((doctor.id, slot_time))
                    state = (
                        "HOLIDAY"
                        if blocking_holiday
                        else "BOOKED"
                        if appointment
                        else "AVAILABLE"
                    )
                    slots.append(
                        {
                            "time": slot_time.strftime("%H:%M"),
                            "label": slot_time.strftime("%I:%M %p").lstrip("0"),
                            "status": state,
                            "appointment": (
                                appointment.to_dict() if appointment else None
                            ),
                        }
                    )

            seen = {slot["time"] for slot in slots}
            for event in events:
                if event.doctor_id != doctor.id:
                    continue
                hhmm = event.start.strftime("%H:%M")
                if hhmm in seen:
                    continue
                slots.append(
                    {
                        "time": hhmm,
                        "label": event.start.strftime("%I:%M %p").lstrip("0"),
                        "status": "BOOKED",
                        "appointment": event.to_dict(),
                        "off_schedule": True,
                    }
                )
                seen.add(hhmm)
            slots.sort(key=lambda item: item["time"])
            grid.append(
                {
                    "doctor": doctor.to_dict(),
                    "scheduled": bool(windows),
                    "holiday": (blocking_holiday or holiday).to_dict()
                    if (blocking_holiday or holiday)
                    else None,
                    "slots": slots,
                }
            )

        return {
            "date": day.isoformat(),
            "weekday": day.weekday(),
            "is_holiday": any(item.scope == "CLINIC" for item in holidays),
            "holidays": [holiday.to_dict() for holiday in holidays],
            "doctors": grid,
        }

    # -- writes ------------------------------------------------------------

    def _ensure_patient(
        self,
        conn: sqlite3.Connection,
        name: str,
        phone: str,
        address_json: str | None = None,
    ) -> int:
        row = conn.execute(
            "SELECT id, name, address FROM patients WHERE phone = ?", (phone,)
        ).fetchone()
        if row:
            if address_json:
                conn.execute(
                    "UPDATE patients SET address = ? WHERE id = ?",
                    (address_json, row["id"]),
                )
            if name and name != row["name"]:
                conn.execute(
                    "UPDATE patients SET name = ? WHERE id = ?", (name, row["id"])
                )
            return int(row["id"])
        cursor = conn.execute(
            "INSERT INTO patients (name, phone, address) VALUES (?, ?, ?)",
            (name, phone, address_json),
        )
        return int(cursor.lastrowid)

    def _next_token(
        self, conn: sqlite3.Connection, doctor_id: int, day: date
    ) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(token_number), 0) + 1 AS next FROM appointments "
            f"WHERE doctor_id = ? AND appointment_date = ? AND status IN {_ACTIVE_SQL}",
            (doctor_id, day.isoformat()),
        ).fetchone()
        return int(row["next"])

    def _insert_appointment(
        self,
        conn: sqlite3.Connection,
        *,
        patient_id: int,
        doctor_id: int,
        day: date,
        start_time: str,
        reason: str | None,
        description: str | None,
        referred_by: str | None,
        emergency: bool,
        source: str,
        address_json: str | None = None,
    ) -> int:
        token = self._next_token(conn, doctor_id, day)
        try:
            cursor = conn.execute(
                """
                INSERT INTO appointments
                    (patient_id, doctor_id, appointment_date, appointment_time,
                     reason, description, referred_by, token_number, status,
                     emergency, source, patient_address)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'BOOKED', ?, ?, ?)
                """,
                (
                    patient_id,
                    doctor_id,
                    day.isoformat(),
                    start_time,
                    reason,
                    description,
                    referred_by,
                    token,
                    1 if emergency else 0,
                    source,
                    address_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BookingError(
                "That slot is already booked.", code="slot_taken"
            ) from exc
        return int(cursor.lastrowid)

    def create_appointment(
        self,
        *,
        patient_name: str,
        patient_phone: str,
        doctor_id: str,
        day: date,
        start_time: str,
        patient_address: dict[str, str] | None = None,
        reason: str | None = None,
        description: str | None = None,
        referred_by: str | None = None,
        emergency: bool = False,
        source: str = "calendar",
        actor: str = "receptionist",
    ) -> Appointment:
        address_json = address_to_json(patient_address)

        def operation() -> int:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                patient_id = self._ensure_patient(
                    conn, patient_name, patient_phone, address_json
                )
                appointment_id = self._insert_appointment(
                    conn,
                    patient_id=patient_id,
                    doctor_id=int(doctor_id),
                    day=day,
                    start_time=start_time,
                    address_json=address_json,
                    reason=reason,
                    description=description,
                    referred_by=referred_by,
                    emergency=emergency,
                    source=source,
                )
                self._audit(
                    conn,
                    actor,
                    "appointment.create",
                    "appointment",
                    str(appointment_id),
                    {"doctor_id": doctor_id, "date": day.isoformat(), "time": start_time},
                )
                conn.commit()
                return appointment_id

        with self._write_lock:
            try:
                appointment_id = retry_sqlite_write(operation)
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc

        appointment = self.get_appointment(str(appointment_id))
        if appointment is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment was not saved.", code="write_failed")
        return appointment

    def reschedule_appointment(
        self,
        appointment_id: str,
        *,
        day: date,
        start_time: str,
        actor: str = "receptionist",
    ) -> Appointment:
        current = self.get_appointment(appointment_id)
        if current is None:
            raise DataSourceError("Appointment not found.", code="not_found")
        if not current.is_active:
            raise BookingError(
                f"Appointment #{appointment_id} is {current.status.value}.",
                code="inactive",
            )

        def operation() -> int:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                new_id = self._insert_appointment(
                    conn,
                    patient_id=int(current.patient_id),
                    doctor_id=int(current.doctor_id),
                    day=day,
                    start_time=start_time,
                    reason=current.reason,
                    description=current.description,
                    referred_by=current.referred_by,
                    emergency=current.emergency,
                    source="reschedule",
                )
                conn.execute(
                    "UPDATE appointments SET status = 'RESCHEDULED' WHERE id = ?",
                    (int(appointment_id),),
                )
                self._audit(
                    conn,
                    actor,
                    "appointment.reschedule",
                    "appointment",
                    str(appointment_id),
                    {"new_id": new_id, "date": day.isoformat(), "time": start_time},
                )
                conn.commit()
                return new_id

        with self._write_lock:
            try:
                new_id = retry_sqlite_write(operation)
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc

        moved = self.get_appointment(str(new_id))
        if moved is None:  # pragma: no cover - defensive
            raise DataSourceError("Reschedule failed.", code="write_failed")
        return moved

    def _set_status(
        self,
        appointment_id: str,
        status: AppointmentStatus,
        *,
        actor: str,
        action: str,
        detail: dict | None = None,
    ) -> Appointment:
        current = self.get_appointment(appointment_id)
        if current is None:
            raise DataSourceError("Appointment not found.", code="not_found")

        with self._connect() as conn:
            conn.execute(
                "UPDATE appointments SET status = ? WHERE id = ?",
                (status.value, int(appointment_id)),
            )
            self._audit(
                conn,
                actor,
                action,
                "appointment",
                str(appointment_id),
                detail,
            )
            conn.commit()

        updated = self.get_appointment(appointment_id)
        if updated is None:  # pragma: no cover - defensive
            raise DataSourceError("Update failed.", code="write_failed")
        return updated

    def cancel_appointment(
        self, appointment_id: str, *, reason: str, actor: str = "receptionist"
    ) -> Appointment:
        return self._set_status(
            appointment_id,
            AppointmentStatus.CANCELLED,
            actor=actor,
            action="appointment.cancel",
            detail={"reason": reason},
        )

    def mark_no_show(
        self, appointment_id: str, *, actor: str = "receptionist"
    ) -> Appointment:
        return self._set_status(
            appointment_id,
            AppointmentStatus.NO_SHOW,
            actor=actor,
            action="appointment.no_show",
        )

    def check_in(
        self, appointment_id: str, *, actor: str = "receptionist"
    ) -> Appointment:
        return self._set_status(
            appointment_id,
            AppointmentStatus.CHECKED_IN,
            actor=actor,
            action="appointment.check_in",
        )

    # -- patients ----------------------------------------------------------

    def lookup_patient(self, phone: str) -> Patient | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM patients WHERE phone = ?", (phone,)
            ).fetchone()
        if not row:
            return None
        return Patient(
            id=str(row["id"]),
            name=row["name"],
            phone=row["phone"],
            gender=row["gender"],
            date_of_birth=(
                date.fromisoformat(row["date_of_birth"])
                if row["date_of_birth"]
                else None
            ),
            address=address_from_json(row["address"]),
        )

    def list_patient_appointments(self, phone: str) -> list[Appointment]:
        with self._connect() as conn:
            rows = conn.execute(
                _APPOINTMENT_SELECT
                + " WHERE p.phone = ? ORDER BY a.appointment_date DESC, a.appointment_time DESC",
                (phone,),
            ).fetchall()
        return [self._row_to_appointment(row) for row in rows]

    # -- holidays ----------------------------------------------------------

    def list_holidays(self, start: date, end: date) -> list[Holiday]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM clinic_holidays
                WHERE holiday_date <= ? AND COALESCE(end_date, holiday_date) >= ?
                ORDER BY holiday_date
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return [
            Holiday(
                id=str(row["id"]),
                date=_parse_date(row["holiday_date"]),
                name=row["name"],
                scope=row["scope"],
                doctor_id=str(row["doctor_id"]) if row["doctor_id"] else None,
                end_date=(
                    _parse_date(row["end_date"]) if row["end_date"] else None
                ),
                type=row["type"],
            )
            for row in rows
        ]

    def get_holiday(self, holiday_id: str) -> Holiday | None:
        try:
            identifier = int(holiday_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clinic_holidays WHERE id = ?", (identifier,)
            ).fetchone()
        if row is None:
            return None
        return Holiday(
            id=str(row["id"]),
            date=_parse_date(row["holiday_date"]),
            name=row["name"],
            scope=row["scope"],
            doctor_id=str(row["doctor_id"]) if row["doctor_id"] else None,
            end_date=_parse_date(row["end_date"]) if row["end_date"] else None,
            type=row["type"],
        )

    def create_holiday(
        self,
        *,
        day: date,
        name: str,
        scope: str = "CLINIC",
        doctor_id: str | None = None,
        end_date: date | None = None,
        holiday_type: str = "PUBLIC_HOLIDAY",
        actor: str = "admin",
    ) -> Holiday:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO clinic_holidays
                    (scope, doctor_id, holiday_date, end_date, name, type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    int(doctor_id) if doctor_id else None,
                    day.isoformat(),
                    end_date.isoformat() if end_date else None,
                    name,
                    holiday_type,
                ),
            )
            holiday_id = int(cursor.lastrowid)
            self._audit(
                conn,
                actor,
                "holiday.create",
                "holiday",
                str(holiday_id),
                {"date": day.isoformat(), "name": name},
            )
            conn.commit()

        return Holiday(
            id=str(holiday_id),
            date=day,
            name=name,
            scope=scope,
            doctor_id=doctor_id,
            end_date=end_date,
            type=holiday_type,
        )

    def delete_holiday(self, holiday_id: str, *, actor: str = "admin") -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM clinic_holidays WHERE id = ?", (int(holiday_id),)
            )
            if cursor.rowcount:
                self._audit(
                    conn, actor, "holiday.delete", "holiday", str(holiday_id)
                )
            conn.commit()
        return bool(cursor.rowcount)

    # -- admin -------------------------------------------------------------

    def _schedules_for(self, weekdays: set[int]) -> list[Schedule]:
        return [
            Schedule(
                weekday=weekday,
                start_time=start,
                end_time=end,
                slot_minutes=config.SLOT_MINUTES,
            )
            for weekday in sorted(weekdays)
            for start, end in config.CLINIC_SESSIONS
        ]

    def create_doctor(
        self,
        *,
        full_name: str,
        specialization: str,
        available_days: str,
        actor: str = "admin",
    ) -> Doctor:
        name = (full_name or "").strip()
        if not name:
            raise BookingError("Doctor name is required.", code="invalid_doctor")

        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE role = 'doctor' AND lower(name) = lower(?)",
                (name,),
            ).fetchone()
            if exists:
                raise BookingError(
                    f"{name} is already on the roster.", code="duplicate_doctor"
                )

            cursor = conn.execute(
                "INSERT INTO users (name, role) VALUES (?, 'doctor')", (name,)
            )
            user_id = int(cursor.lastrowid)
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM doctor_profiles"
            ).fetchone()["total"]
            color = config.DOCTOR_COLORS[int(count) % len(config.DOCTOR_COLORS)]
            cursor = conn.execute(
                "INSERT INTO doctor_profiles (user_id, specialization, available, color) "
                "VALUES (?, ?, 1, ?)",
                (user_id, specialization, color),
            )
            doctor_id = int(cursor.lastrowid)

            for schedule in self._schedules_for(parse_available_days(available_days)):
                conn.execute(
                    "INSERT INTO doctor_schedules "
                    "(doctor_id, weekday, start_time, end_time, slot_minutes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        doctor_id,
                        schedule.weekday,
                        schedule.start_time.strftime("%H:%M"),
                        schedule.end_time.strftime("%H:%M"),
                        schedule.slot_minutes,
                    ),
                )

            self._audit(
                conn, actor, "doctor.create", "doctor", str(doctor_id), {"name": name}
            )
            conn.commit()

        doctor = self.get_doctor(str(doctor_id))
        if doctor is None:  # pragma: no cover - defensive
            raise DataSourceError("Doctor was not saved.", code="write_failed")
        return doctor

    def update_doctor(
        self,
        doctor_id: str,
        *,
        specialization: str | None = None,
        available_days: str | None = None,
        actor: str = "admin",
    ) -> Doctor:
        doctor = self.get_doctor(doctor_id)
        if doctor is None:
            raise DataSourceError("Doctor not found.", code="not_found")

        with self._connect() as conn:
            if specialization:
                conn.execute(
                    "UPDATE doctor_profiles SET specialization = ? WHERE id = ?",
                    (specialization, int(doctor_id)),
                )
            if available_days:
                conn.execute(
                    "DELETE FROM doctor_schedules WHERE doctor_id = ?",
                    (int(doctor_id),),
                )
                for schedule in self._schedules_for(parse_available_days(available_days)):
                    conn.execute(
                        "INSERT INTO doctor_schedules "
                        "(doctor_id, weekday, start_time, end_time, slot_minutes) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            int(doctor_id),
                            schedule.weekday,
                            schedule.start_time.strftime("%H:%M"),
                            schedule.end_time.strftime("%H:%M"),
                            schedule.slot_minutes,
                        ),
                    )
            self._audit(
                conn,
                actor,
                "doctor.update",
                "doctor",
                str(doctor_id),
                {"specialization": specialization, "available_days": available_days},
            )
            conn.commit()

        updated = self.get_doctor(doctor_id)
        if updated is None:  # pragma: no cover - defensive
            raise DataSourceError("Doctor not found.", code="not_found")
        return updated

    def delete_doctor(self, doctor_id: str, *, actor: str = "admin") -> bool:
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM appointments WHERE doctor_id = ?",
                (int(doctor_id),),
            ).fetchone()["total"]
            if count:
                raise DataSourceError(
                    "This doctor has appointments and cannot be removed.",
                    code="doctor_in_use",
                )
            row = conn.execute(
                "SELECT user_id FROM doctor_profiles WHERE id = ?",
                (int(doctor_id),),
            ).fetchone()
            if not row:
                raise DataSourceError("Doctor not found.", code="not_found")
            conn.execute(
                "DELETE FROM doctor_schedules WHERE doctor_id = ?", (int(doctor_id),)
            )
            conn.execute(
                "DELETE FROM doctor_profiles WHERE id = ?", (int(doctor_id),)
            )
            if row["user_id"]:
                conn.execute("DELETE FROM users WHERE id = ?", (row["user_id"],))
            self._audit(
                conn, actor, "doctor.delete", "doctor", str(doctor_id)
            )
            conn.commit()
        return True

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "actor": row["actor"],
                "action": row["action"],
                "entity": row["entity"],
                "entity_id": row["entity_id"],
                "detail": json.loads(row["detail"]) if row["detail"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]
