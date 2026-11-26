"""Neon / Lakebase Postgres data source.

This is the real production adapter for the calendar. It speaks the same
schema as ``production.py`` (``users``, ``patients``, ``doctor_profiles``,
``appointments``, plus schedules, holidays and audit log) but uses Postgres
types, constraints and a connection pool instead of SQLite.

Enable with ``DATA_SOURCE=neon`` and ``DATABASE_URL`` (the pooled Neon URL).
``DATABASE_URL_UNPOOLED`` is used for schema setup when available.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timedelta
from typing import Any

import psycopg
from psycopg import rows
from psycopg.errors import DeadlockDetected, SerializationFailure, UniqueViolation
from psycopg_pool import ConnectionPool

from .. import config
from ..domain import (
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

# Canonical schema lives in one place so every backend shares the same contract.
# Re-exported here for compatibility with existing imports and tests.
from ..schema import SCHEMA  # noqa: F401,E402

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


def _parse_time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise DataSourceError(f"Invalid appointment_time '{value}'.", code="bad_time")


def _parse_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class PostgresDataSource(DataSource):
    def __init__(self, dsn: str | None = None, unpooled_dsn: str | None = None) -> None:
        self.dsn = dsn or config.NEON_DATABASE_URL
        if not self.dsn:
            raise DataSourceError(
                "DATABASE_URL is not set. Add the Neon connection string to .env.local.",
                code="bad_config",
            )
        self.unpooled_dsn = (
            unpooled_dsn or config.NEON_DATABASE_URL_UNPOOLED or self.dsn
        )
        # Reads use autocommit: on Neon every round trip costs ~70ms, so an
        # implicit BEGIN/COMMIT per read adds ~150ms per borrow. Writes keep
        # implicit transactions because they rely on pg_advisory_xact_lock and
        # need multi-statement atomicity.
        self._pool = ConnectionPool(
            conninfo=self.dsn,
            kwargs={"row_factory": rows.dict_row, "autocommit": True},
            min_size=2,
            max_size=8,
            timeout=10,
            open=True,
        )
        self._write_pool = ConnectionPool(
            conninfo=self.dsn,
            kwargs={"row_factory": rows.dict_row, "autocommit": False},
            min_size=1,
            max_size=4,
            timeout=10,
            open=True,
        )
        self._write_lock = threading.RLock()
        self._init_schema()

    # -- plumbing ----------------------------------------------------------

    def _connect(self) -> Any:
        """Read-only connection (no implicit BEGIN/COMMIT round trips)."""
        return self._pool.connection()

    def _write_connect(self) -> Any:
        """Transactional connection for writes and advisory locks."""
        return self._write_pool.connection()

    def close(self) -> None:
        """Release pool threads on shutdown (safe to call more than once)."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._pool.close()
        self._write_pool.close()

    def _init_schema(self) -> None:
        # DDL goes through the direct (unpooled) endpoint when configured.
        with psycopg.connect(
            self.unpooled_dsn, row_factory=rows.dict_row, autocommit=True
        ) as conn:
            conn.execute(SCHEMA)

    def _run_write(self, operation: Any) -> Any:
        """Serialize local writes and retry Postgres serialization conflicts."""
        with self._write_lock:
            for attempt in range(3):
                try:
                    return operation()
                except (SerializationFailure, DeadlockDetected):
                    if attempt == 2:
                        raise
        raise AssertionError("unreachable")

    def _audit(
        self,
        conn: Any,
        actor: str,
        action: str,
        entity: str,
        entity_id: str,
        detail: dict | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, detail) "
            "VALUES (%s, %s, %s, %s, %s)",
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
        # One round trip instead of two: join profiles and schedules. On Neon
        # each query costs ~70ms, so this halves the cost of every endpoint
        # that renders doctors.
        with self._connect() as conn:
            rows_ = conn.execute(
                """
                SELECT dp.id, dp.specialization, dp.qualification,
                       dp.experience_years, dp.consultation_fee, dp.room_number,
                       dp.available, dp.color, u.name AS full_name,
                       ds.weekday, ds.start_time, ds.end_time, ds.slot_minutes
                FROM doctor_profiles dp
                LEFT JOIN users u ON u.id = dp.user_id
                LEFT JOIN doctor_schedules ds ON ds.doctor_id = dp.id
                ORDER BY dp.id, ds.weekday, ds.start_time
                """
            ).fetchall()

        doctors: list[Doctor] = []
        by_id: dict[int, Doctor] = {}
        for row in rows_:
            doctor_id = int(row["id"])
            doctor = by_id.get(doctor_id)
            if doctor is None:
                doctor = Doctor(
                    id=str(doctor_id),
                    full_name=row["full_name"] or f"Doctor {doctor_id}",
                    specialization=row["specialization"],
                    qualification=row["qualification"],
                    experience_years=row["experience_years"],
                    consultation_fee=row["consultation_fee"],
                    room_number=row["room_number"],
                    available=bool(row["available"]),
                    color=row["color"]
                    or config.DOCTOR_COLORS[len(by_id) % len(config.DOCTOR_COLORS)],
                    schedules=[],
                )
                by_id[doctor_id] = doctor
                doctors.append(doctor)
            if row["weekday"] is not None:
                doctor.schedules.append(
                    Schedule(
                        weekday=int(row["weekday"]),
                        start_time=_parse_time(row["start_time"]),
                        end_time=_parse_time(row["end_time"]),
                        slot_minutes=int(row["slot_minutes"] or config.SLOT_MINUTES),
                    )
                )
        return doctors

    def get_doctor(self, doctor_id: str) -> Doctor | None:
        for doctor in self.get_doctors():
            if doctor.id == str(doctor_id):
                return doctor
        return None

    # -- appointments ------------------------------------------------------

    def _row_to_appointment(self, row: Any) -> Appointment:
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
            created_at=_parse_datetime(row["created_at"]),
            patient_address=address_from_json(row["patient_address"])
            or address_from_json(row["patient_profile_address"]),
        )

    def list_events(
        self, start: date, end: date, doctor_id: str | None = None
    ) -> list[Appointment]:
        query = _APPOINTMENT_SELECT + " WHERE a.appointment_date BETWEEN %s AND %s"
        params: list[Any] = [start, end]
        if doctor_id:
            try:
                identifier = int(doctor_id)
            except (TypeError, ValueError):
                return []
            query += " AND a.doctor_id = %s"
            params.append(identifier)
        query += " ORDER BY a.appointment_date, a.appointment_time, a.id"

        with self._connect() as conn:
            rows_ = conn.execute(query, params).fetchall()
        return [self._row_to_appointment(row) for row in rows_]

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        try:
            identifier = int(appointment_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                _APPOINTMENT_SELECT + " WHERE a.id = %s", (identifier,)
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
        conn: Any,
        name: str,
        phone: str,
        address_json: str | None = None,
    ) -> int:
        row = conn.execute(
            """
            INSERT INTO patients (name, phone, address)
            VALUES (%s, %s, %s)
            ON CONFLICT (phone) DO UPDATE
                SET name = COALESCE(EXCLUDED.name, patients.name),
                    address = COALESCE(EXCLUDED.address, patients.address)
            RETURNING id
            """,
            (name, phone, address_json),
        ).fetchone()
        return int(row["id"])

    def _next_token(self, conn: Any, doctor_id: int, day: date) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(token_number), 0) + 1 AS next FROM appointments "
            f"WHERE doctor_id = %s AND appointment_date = %s AND status IN {_ACTIVE_SQL}",
            (doctor_id, day),
        ).fetchone()
        return int(row["next"])

    def _insert_appointment(
        self,
        conn: Any,
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
            row = conn.execute(
                """
                INSERT INTO appointments
                    (patient_id, doctor_id, appointment_date, appointment_time,
                     reason, description, referred_by, token_number, status,
                     emergency, source, patient_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'BOOKED', %s, %s, %s)
                RETURNING id
                """,
                (
                    patient_id,
                    doctor_id,
                    day,
                    start_time,
                    reason,
                    description,
                    referred_by,
                    token,
                    emergency,
                    source,
                    address_json,
                ),
            ).fetchone()
        except UniqueViolation as exc:
            raise BookingError(
                "That slot is already booked.", code="slot_taken"
            ) from exc
        return int(row["id"])

    @staticmethod
    def _lock_slot(doctor_id: int, day: date) -> str:
        return f"dnyanai:appt:{doctor_id}:{day.isoformat()}"

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
            with self._write_connect() as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (self._lock_slot(int(doctor_id), day),),
                )
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
                return appointment_id

        appointment_id = self._run_write(operation)
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
            with self._write_connect() as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (self._lock_slot(int(current.doctor_id), day),),
                )
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
                    "UPDATE appointments SET status = 'RESCHEDULED' WHERE id = %s",
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
                return new_id

        new_id = self._run_write(operation)
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

        def operation() -> None:
            with self._write_connect() as conn:
                conn.execute(
                    "UPDATE appointments SET status = %s WHERE id = %s",
                    (status.value, int(appointment_id)),
                )
                self._audit(
                    conn, actor, action, "appointment", str(appointment_id), detail
                )

        self._run_write(operation)
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
                "SELECT * FROM patients WHERE phone = %s", (phone,)
            ).fetchone()
        if not row:
            return None
        dob = row["date_of_birth"]
        return Patient(
            id=str(row["id"]),
            name=row["name"],
            phone=row["phone"],
            gender=row["gender"],
            date_of_birth=_parse_date(dob) if dob else None,
            address=address_from_json(row["address"]),
        )

    def list_patient_appointments(self, phone: str) -> list[Appointment]:
        with self._connect() as conn:
            rows_ = conn.execute(
                _APPOINTMENT_SELECT
                + " WHERE p.phone = %s "
                + "ORDER BY a.appointment_date DESC, a.appointment_time DESC",
                (phone,),
            ).fetchall()
        return [self._row_to_appointment(row) for row in rows_]

    # -- holidays ----------------------------------------------------------

    def list_holidays(self, start: date, end: date) -> list[Holiday]:
        with self._connect() as conn:
            rows_ = conn.execute(
                """
                SELECT * FROM clinic_holidays
                WHERE holiday_date <= %s AND COALESCE(end_date, holiday_date) >= %s
                ORDER BY holiday_date
                """,
                (end, start),
            ).fetchall()
        return [
            Holiday(
                id=str(row["id"]),
                date=_parse_date(row["holiday_date"]),
                name=row["name"],
                scope=row["scope"],
                doctor_id=str(row["doctor_id"]) if row["doctor_id"] else None,
                end_date=_parse_date(row["end_date"]) if row["end_date"] else None,
                type=row["type"],
            )
            for row in rows_
        ]

    def get_holiday(self, holiday_id: str) -> Holiday | None:
        try:
            identifier = int(holiday_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clinic_holidays WHERE id = %s", (identifier,)
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
        def operation() -> int:
            with self._write_connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO clinic_holidays
                        (scope, doctor_id, holiday_date, end_date, name, type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        scope,
                        int(doctor_id) if doctor_id else None,
                        day,
                        end_date,
                        name,
                        holiday_type,
                    ),
                ).fetchone()
                holiday_id = int(row["id"])
                self._audit(
                    conn,
                    actor,
                    "holiday.create",
                    "holiday",
                    str(holiday_id),
                    {"date": day.isoformat(), "name": name},
                )
                return holiday_id

        holiday_id = self._run_write(operation)
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
        def operation() -> bool:
            with self._write_connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM clinic_holidays WHERE id = %s", (int(holiday_id),)
                )
                if cursor.rowcount:
                    self._audit(
                        conn, actor, "holiday.delete", "holiday", str(holiday_id)
                    )
                return bool(cursor.rowcount)

        return self._run_write(operation)

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

        def operation() -> int:
            with self._write_connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM users "
                    "WHERE role = 'doctor' AND lower(name) = lower(%s)",
                    (name,),
                ).fetchone()
                if exists:
                    raise BookingError(
                        f"{name} is already on the roster.", code="duplicate_doctor"
                    )
                user_row = conn.execute(
                    "INSERT INTO users (name, role) VALUES (%s, 'doctor') RETURNING id",
                    (name,),
                ).fetchone()
                user_id = int(user_row["id"])
                count_row = conn.execute(
                    "SELECT COUNT(*) AS total FROM doctor_profiles"
                ).fetchone()
                color = config.DOCTOR_COLORS[
                    int(count_row["total"]) % len(config.DOCTOR_COLORS)
                ]
                doctor_row = conn.execute(
                    "INSERT INTO doctor_profiles "
                    "(user_id, specialization, available, color) "
                    "VALUES (%s, %s, TRUE, %s) RETURNING id",
                    (user_id, specialization, color),
                ).fetchone()
                doctor_id = int(doctor_row["id"])

                for schedule in self._schedules_for(parse_available_days(available_days)):
                    conn.execute(
                        "INSERT INTO doctor_schedules "
                        "(doctor_id, weekday, start_time, end_time, slot_minutes) "
                        "VALUES (%s, %s, %s, %s, %s)",
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
                return doctor_id

        doctor_id = self._run_write(operation)
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

        def operation() -> None:
            with self._write_connect() as conn:
                if specialization:
                    conn.execute(
                        "UPDATE doctor_profiles SET specialization = %s WHERE id = %s",
                        (specialization, int(doctor_id)),
                    )
                if available_days:
                    conn.execute(
                        "DELETE FROM doctor_schedules WHERE doctor_id = %s",
                        (int(doctor_id),),
                    )
                    for schedule in self._schedules_for(
                        parse_available_days(available_days)
                    ):
                        conn.execute(
                            "INSERT INTO doctor_schedules "
                            "(doctor_id, weekday, start_time, end_time, slot_minutes) "
                            "VALUES (%s, %s, %s, %s, %s)",
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

        self._run_write(operation)
        updated = self.get_doctor(doctor_id)
        if updated is None:  # pragma: no cover - defensive
            raise DataSourceError("Doctor not found.", code="not_found")
        return updated

    def delete_doctor(self, doctor_id: str, *, actor: str = "admin") -> bool:
        def operation() -> None:
            with self._write_connect() as conn:
                count_row = conn.execute(
                    "SELECT COUNT(*) AS total FROM appointments WHERE doctor_id = %s",
                    (int(doctor_id),),
                ).fetchone()
                if int(count_row["total"]):
                    raise DataSourceError(
                        "This doctor has appointments and cannot be removed.",
                        code="doctor_in_use",
                    )
                row = conn.execute(
                    "SELECT user_id FROM doctor_profiles WHERE id = %s",
                    (int(doctor_id),),
                ).fetchone()
                if not row:
                    raise DataSourceError("Doctor not found.", code="not_found")
                conn.execute(
                    "DELETE FROM doctor_schedules WHERE doctor_id = %s",
                    (int(doctor_id),),
                )
                conn.execute(
                    "DELETE FROM doctor_profiles WHERE id = %s", (int(doctor_id),)
                )
                if row["user_id"]:
                    conn.execute(
                        "DELETE FROM users WHERE id = %s", (int(row["user_id"]),)
                    )
                self._audit(conn, actor, "doctor.delete", "doctor", str(doctor_id))

        self._run_write(operation)
        return True

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows_ = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "actor": row["actor"],
                "action": row["action"],
                "entity": row["entity"],
                "entity_id": row["entity_id"],
                "detail": json.loads(row["detail"]) if row["detail"] else None,
                "created_at": _parse_datetime(row["created_at"]),
            }
            for row in rows_
        ]
