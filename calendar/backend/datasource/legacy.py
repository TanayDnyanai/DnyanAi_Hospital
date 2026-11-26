"""Legacy SQLite data source.

Reads ``chatbot/clinic.db`` directly and delegates all writes to
the receptionist's own ``database.py`` functions so slot validation, the
partial unique index and notification state stay identical between the
calendar and the voice agent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

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
from .sqlite_helpers import (
    DB_CONNECT_TIMEOUT_SECONDS,
    configure_connection,
    retry_sqlite_write,
)

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

WEEKDAY_ALIASES = {
    "mon": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}

_CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS cal_doctor_map (
    legacy_name   TEXT PRIMARY KEY,
    doctor_key    TEXT UNIQUE NOT NULL,
    color         TEXT NOT NULL,
    production_id INTEGER
);

CREATE TABLE IF NOT EXISTS cal_appointment_extra (
    appointment_id INTEGER PRIMARY KEY,
    reason         TEXT,
    description    TEXT,
    referred_by    TEXT,
    source         TEXT DEFAULT 'calendar',
    created_by     TEXT,
    address_json   TEXT
);

CREATE TABLE IF NOT EXISTS cal_patient_addresses (
    phone         TEXT PRIMARY KEY,
    address_json  TEXT NOT NULL,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cal_holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL DEFAULT 'CLINIC',
    doctor_key   TEXT,
    holiday_date TEXT NOT NULL,
    end_date     TEXT,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT 'PUBLIC_HOLIDAY',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cal_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT,
    action     TEXT,
    entity     TEXT,
    entity_id  TEXT,
    detail     TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_weekday_text(text: str) -> str:
    """Expand Mon/Tue/... abbreviations so range parsing is uniform."""
    if not text:
        return ""

    def replace(match: re.Match[str]) -> str:
        return WEEKDAY_ALIASES[match.group(0).lower()]

    return re.sub(
        r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
        replace,
        text.lower(),
    )


def parse_available_days(text: str) -> set[int]:
    """Parse the legacy free-text ``available_days`` column into weekdays.

    Handles "Monday to Saturday", "Monday, Wednesday, Friday",
    "Mon-Fri", "Daily", etc.
    """
    if not text or not str(text).strip():
        return set()

    clean = normalize_weekday_text(str(text))
    if any(word in clean for word in ("daily", "all days", "every day", "all week")):
        return set(range(7))

    found: set[int] = set()

    range_pattern = re.compile(
        r"\b(" + "|".join(WEEKDAYS) + r")\s*(?:to|through|until|-|–|—)\s*("
        + "|".join(WEEKDAYS)
        + r")\b"
    )
    for match in range_pattern.finditer(clean):
        start = WEEKDAYS.index(match.group(1))
        end = WEEKDAYS.index(match.group(2))
        if start <= end:
            found.update(range(start, end + 1))
        else:  # wrap-around, e.g. "Saturday to Monday"
            found.update(range(start, 7))
            found.update(range(0, end + 1))

    remainder = range_pattern.sub(" ", clean)
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", remainder):
            found.add(index)

    return found


def slugify_doctor(name: str) -> str:
    """Stable key for a legacy doctor name: 'Dr. Rajesh Sharma' -> 'dr-rajesh-sharma'."""
    cleaned = str(name).lower().replace("dr.", " ").replace("dr ", " ")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return f"dr-{slug}" if slug else "dr-unknown"


def patient_id_for_phone(phone: str) -> str:
    digest = hashlib.sha1(str(phone).strip().encode("utf-8")).hexdigest()[:12]
    return f"legacy:{digest}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


class _SharedConnection:
    """Let receptionist writes join a transaction opened by the calendar.

    The receptionist helpers open their own connection and commit after every
    statement. Proxying ``get_connection`` keeps their exact SQL and validation
    while allowing the calendar to commit the appointment together with its
    address, metadata and audit row.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def __enter__(self) -> "_SharedConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self._conn.rollback()
        return False

    def commit(self) -> None:
        # The calendar owns this transaction; the receptionist helper must not
        # commit it halfway through.
        return None

    def cursor(self) -> sqlite3.Cursor:
        return self._conn.cursor()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------


class LegacyDataSource(DataSource):
    def __init__(
        self,
        db_path: Path | str | None = None,
        receptionist_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path or config.CLINIC_DB_PATH).resolve()
        self.receptionist_dir = Path(
            receptionist_dir or config.RECEPTIONIST_DIR
        ).resolve()
        self._write_lock = threading.RLock()

        if not self.db_path.exists():
            raise DataSourceError(
                f"Clinic database not found at {self.db_path}. "
                "Set CLINIC_DB_PATH or run the receptionist app once.",
                code="db_missing",
            )

        self._db = self._load_receptionist_database()
        self._ensure_calendar_tables()
        self._seed_doctor_map()

    # -- bootstrap ---------------------------------------------------------

    def _load_receptionist_database(self):
        module_path = self.receptionist_dir / "database.py"
        if not module_path.exists():
            raise DataSourceError(
                f"Receptionist database module not found at {module_path}",
                code="receptionist_missing",
            )
        spec = importlib.util.spec_from_file_location(
            "receptionist_database", module_path
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise DataSourceError("Could not load receptionist database module")
        module = importlib.util.module_from_spec(spec)
        # database.py swaps to its Neon backend at import time when
        # DATA_SOURCE=neon. This datasource is explicitly the legacy SQLite
        # implementation, so force legacy mode while loading it regardless of
        # the ambient environment (e.g. workspace .env.local in production).
        previous_data_source = os.environ.get("DATA_SOURCE")
        os.environ["DATA_SOURCE"] = "legacy"
        try:
            spec.loader.exec_module(module)
        finally:
            if previous_data_source is None:
                os.environ.pop("DATA_SOURCE", None)
            else:
                os.environ["DATA_SOURCE"] = previous_data_source
        # Point the module at *our* database file before initialising. Its own
        # get_connection() is replaced here (never on disk) so calendar writes
        # wait for busy locks instead of failing immediately.
        module.DB_PATH = str(self.db_path)

        def get_connection() -> sqlite3.Connection:
            conn = sqlite3.connect(
                self.db_path, timeout=DB_CONNECT_TIMEOUT_SECONDS
            )
            conn.row_factory = sqlite3.Row
            return configure_connection(conn)

        module.get_connection = get_connection
        module.init_db()
        return module

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_CONNECT_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        return configure_connection(conn)

    @contextmanager
    def _shared_connection(self, conn: sqlite3.Connection):
        """Temporarily route receptionist writes into ``conn``."""
        original = self._db.get_connection
        self._db.get_connection = lambda: _SharedConnection(conn)
        try:
            yield
        finally:
            self._db.get_connection = original

    @staticmethod
    def _db_busy_error(exc: sqlite3.OperationalError) -> BookingError:
        return BookingError(
            "The clinic database is busy. Please try again shortly.",
            code="db_busy",
        )

    def _ensure_calendar_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(_CALENDAR_SCHEMA)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(cal_appointment_extra)")
            }
            if "address_json" not in columns:
                conn.execute(
                    "ALTER TABLE cal_appointment_extra ADD COLUMN address_json TEXT"
                )
            conn.commit()

    def _seed_doctor_map(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM doctors ORDER BY id").fetchall()
            existing = {
                row["legacy_name"]
                for row in conn.execute("SELECT legacy_name FROM cal_doctor_map")
            }
            next_color = len(existing) % len(config.DOCTOR_COLORS)
            for row in rows:
                name = row["name"]
                if name in existing:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO cal_doctor_map "
                    "(legacy_name, doctor_key, color) VALUES (?, ?, ?)",
                    (
                        name,
                        slugify_doctor(name),
                        config.DOCTOR_COLORS[next_color % len(config.DOCTOR_COLORS)],
                    ),
                )
                next_color += 1
            conn.commit()

    def _doctor_map(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT legacy_name, doctor_key, color FROM cal_doctor_map"
            ).fetchall()
        return {
            row["legacy_name"]: {
                "key": row["doctor_key"],
                "color": row["color"],
            }
            for row in rows
        }

    def _register_doctor(self, name: str) -> dict[str, Any]:
        """Ensure a doctor discovered in the DB has a key and a colour."""
        mapping = self._doctor_map()
        if name in mapping:
            return mapping[name]
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM cal_doctor_map").fetchone()[0]
            color = config.DOCTOR_COLORS[count % len(config.DOCTOR_COLORS)]
            conn.execute(
                "INSERT OR IGNORE INTO cal_doctor_map "
                "(legacy_name, doctor_key, color) VALUES (?, ?, ?)",
                (name, slugify_doctor(name), color),
            )
            conn.commit()
        return {"key": slugify_doctor(name), "color": color}

    # -- reference data ----------------------------------------------------

    def get_clinic_info(self) -> dict[str, Any]:
        info = {
            "name": "DnyanAI Hospital",
            "address": "Pune, Maharashtra",
            "phone": "020-12345678",
            "opening_hours": "Monday to Saturday, 9 AM to 6 PM",
        }
        clinic_path = self.receptionist_dir / "clinic.py"
        if clinic_path.exists():
            try:
                spec = importlib.util.spec_from_file_location(
                    "receptionist_clinic", clinic_path
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    info.update(getattr(module, "CLINIC_INFO", {}))
            except Exception:  # pragma: no cover - cosmetic only
                pass
        return info

    def get_doctors(self) -> list[Doctor]:
        mapping = self._doctor_map()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, specialty, available_days FROM doctors ORDER BY id"
            ).fetchall()

        doctors: list[Doctor] = []
        for row in rows:
            meta = mapping.get(row["name"]) or self._register_doctor(row["name"])
            weekdays = parse_available_days(row["available_days"])
            doctors.append(
                Doctor(
                    id=meta["key"],
                    full_name=row["name"],
                    specialization=row["specialty"],
                    available=True,
                    color=meta["color"],
                    schedules=self._schedules_for(weekdays),
                )
            )
        return doctors

    def get_doctor(self, doctor_id: str) -> Doctor | None:
        for doctor in self.get_doctors():
            if doctor.id == doctor_id:
                return doctor
        return None

    def _doctor_by_name(self, name: str) -> Doctor | None:
        for doctor in self.get_doctors():
            if doctor.full_name == name:
                return doctor
        return None

    @staticmethod
    def _schedules_for(weekdays: set[int]) -> list[Schedule]:
        schedules: list[Schedule] = []
        for weekday in sorted(weekdays):
            for start, end in config.CLINIC_SESSIONS:
                schedules.append(
                    Schedule(
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                        slot_minutes=config.SLOT_MINUTES,
                    )
                )
        return schedules

    # -- appointment reads -------------------------------------------------

    def _fetch_rows(
        self, start: date | None = None, end: date | None = None
    ) -> list[sqlite3.Row]:
        query = """
            SELECT a.id, a.patient_name, a.patient_phone, a.doctor_name,
                   a.date, a.time_slot, a.status, a.is_emergency, a.arrived,
                   a.notification_status, a.created_at,
                   x.reason, x.description, x.referred_by, x.source,
                   x.address_json
            FROM appointments a
            LEFT JOIN cal_appointment_extra x ON x.appointment_id = a.id
        """
        params: list[Any] = []
        if start and end:
            query += " WHERE a.date >= ? AND a.date <= ?"
            params = [start.isoformat(), end.isoformat()]
        query += " ORDER BY a.date, a.time_slot, a.id"

        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def _row_to_appointment(self, row: sqlite3.Row) -> Appointment:
        start = datetime.strptime(
            f"{row['date']} {row['time_slot']}", "%Y-%m-%d %I:%M %p"
        ).replace(tzinfo=config.CLINIC_TZ)
        doctor_key = self._register_doctor(row["doctor_name"])["key"]
        status = self._map_status(row)
        return Appointment(
            id=str(row["id"]),
            patient_id=patient_id_for_phone(row["patient_phone"]),
            patient_name=row["patient_name"],
            patient_phone=row["patient_phone"],
            doctor_id=doctor_key,
            doctor_name=row["doctor_name"],
            start=start,
            end=start + timedelta(minutes=config.SLOT_MINUTES),
            status=status,
            emergency=bool(row["is_emergency"]),
            arrived=bool(row["arrived"]),
            reason=row["reason"],
            description=row["description"],
            referred_by=row["referred_by"],
            source=row["source"] or "voice",
            created_at=_parse_datetime(row["created_at"]),
            patient_address=address_from_json(row["address_json"]),
        )

    @staticmethod
    def _map_status(row: sqlite3.Row) -> AppointmentStatus:
        status = (row["status"] or "").strip().lower()
        notification = (row["notification_status"] or "").strip().lower()
        arrived = str(row["arrived"]).strip().lower() in {"1", "true", "yes", "t"}
        if status in {"cancelled", "canceled"}:
            return (
                AppointmentStatus.RESCHEDULED
                if notification == "rescheduled"
                else AppointmentStatus.CANCELLED
            )
        if status in {"no-show", "no show", "noshow"}:
            return AppointmentStatus.NO_SHOW
        if arrived:
            return AppointmentStatus.CHECKED_IN
        return AppointmentStatus.BOOKED

    def _assign_tokens(self, appointments: list[Appointment]) -> None:
        grouped: dict[tuple[str, date], list[Appointment]] = {}
        for appointment in appointments:
            if appointment.is_active:
                grouped.setdefault(
                    (appointment.doctor_id, appointment.start.date()), []
                ).append(appointment)
        for group in grouped.values():
            group.sort(key=lambda item: item.start)
            for index, appointment in enumerate(group, start=1):
                appointment.token_number = index

    def list_events(
        self, start: date, end: date, doctor_id: str | None = None
    ) -> list[Appointment]:
        appointments = [
            self._row_to_appointment(row) for row in self._fetch_rows(start, end)
        ]
        if doctor_id:
            appointments = [a for a in appointments if a.doctor_id == doctor_id]
        self._assign_tokens(appointments)
        return appointments

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        try:
            identifier = int(appointment_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT a.id, a.patient_name, a.patient_phone, a.doctor_name,
                       a.date, a.time_slot, a.status, a.is_emergency, a.arrived,
                       a.notification_status, a.created_at,
                       x.reason, x.description, x.referred_by, x.source,
                       x.address_json
                FROM appointments a
                LEFT JOIN cal_appointment_extra x ON x.appointment_id = a.id
                WHERE a.id = ?
                """,
                (identifier,),
            ).fetchone()
        if not row:
            return None
        appointment = self._row_to_appointment(row)
        same_day = self.list_events(
            appointment.start.date(), appointment.start.date()
        )
        for item in same_day:
            if item.id == appointment.id:
                return item
        return appointment

    def get_day_grid(self, day: date, doctor_id: str | None = None) -> dict[str, Any]:
        doctors = self.get_doctors()
        if doctor_id:
            doctors = [doctor for doctor in doctors if doctor.id == doctor_id]
        holidays = [holiday for holiday in self.list_holidays(day, day)]
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
            weekday = day.weekday()
            windows = [
                schedule for schedule in doctor.schedules if schedule.weekday == weekday
            ]
            slots: list[dict[str, Any]] = []
            for window in windows:
                for slot_time in window.slots():
                    appointment = by_slot.get((doctor.id, slot_time))
                    if blocking_holiday:
                        state = "HOLIDAY"
                    elif appointment:
                        state = "BOOKED"
                    else:
                        state = "AVAILABLE"
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

            # Appointments outside the doctor's posted hours must still be
            # visible; otherwise they silently vanish from the day view.
            seen_times = {slot["time"] for slot in slots}
            for event in events:
                if event.doctor_id != doctor.id:
                    continue
                hhmm = event.start.strftime("%H:%M")
                if hhmm in seen_times:
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
                seen_times.add(hhmm)
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

    def _write_extra_conn(
        self,
        conn: sqlite3.Connection,
        appointment_id: int,
        *,
        reason: str | None,
        description: str | None,
        referred_by: str | None,
        source: str,
        actor: str,
        patient_address: dict[str, str] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO cal_appointment_extra
                (appointment_id, reason, description, referred_by, source,
                 created_by, address_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appointment_id) DO UPDATE SET
                reason = COALESCE(excluded.reason, reason),
                description = COALESCE(excluded.description, description),
                referred_by = COALESCE(excluded.referred_by, referred_by),
                source = excluded.source,
                created_by = excluded.created_by,
                address_json = COALESCE(excluded.address_json, address_json)
            """,
            (
                appointment_id,
                reason,
                description,
                referred_by,
                source,
                actor,
                address_to_json(patient_address),
            ),
        )

    def _write_extra(
        self,
        appointment_id: int,
        *,
        reason: str | None,
        description: str | None,
        referred_by: str | None,
        source: str,
        actor: str,
        patient_address: dict[str, str] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        kwargs = dict(
            reason=reason,
            description=description,
            referred_by=referred_by,
            source=source,
            actor=actor,
            patient_address=patient_address,
        )
        if conn is None:
            with self._connect() as conn:
                self._write_extra_conn(conn, appointment_id, **kwargs)
        else:
            self._write_extra_conn(conn, appointment_id, **kwargs)

    def _save_patient_address_conn(
        self,
        conn: sqlite3.Connection,
        phone: str,
        address: dict[str, str] | None,
    ) -> None:
        address_json = address_to_json(address)
        if not address_json:
            return
        conn.execute(
            """
            INSERT INTO cal_patient_addresses (phone, address_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
                address_json = excluded.address_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(phone).strip(), address_json),
        )

    def _save_patient_address(
        self, phone: str, address: dict[str, str] | None
    ) -> None:
        with self._connect() as conn:
            self._save_patient_address_conn(conn, phone, address)

    def _audit_conn(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        entity: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO cal_audit (actor, action, entity, entity_id, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, entity, str(entity_id), json.dumps(detail or {})),
        )

    def _audit(
        self,
        actor: str,
        action: str,
        entity: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            self._audit_conn(conn, actor, action, entity, entity_id, detail)

    def _book_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        doctor: Doctor,
        slot: str,
        day: date,
        patient_name: str,
        patient_phone: str,
        patient_address: dict[str, str] | None,
        reason: str | None,
        description: str | None,
        referred_by: str | None,
        emergency: bool,
        source: str,
        actor: str,
    ) -> int:
        result = self._db.book_appointment(
            patient_name=patient_name,
            patient_phone=patient_phone,
            doctor_name=doctor.full_name,
            date=day.isoformat(),
            time_slot=slot,
            is_emergency=int(emergency),
        )
        if not result.get("success"):
            raise BookingError(result.get("error", "Booking failed."), code="slot_taken")

        appointment_id = int(result["appointment_id"])
        self._write_extra_conn(
            conn,
            appointment_id,
            reason=reason,
            description=description,
            referred_by=referred_by,
            source=source,
            actor=actor,
            patient_address=patient_address,
        )
        self._save_patient_address_conn(conn, patient_phone, patient_address)
        self._audit_conn(
            conn,
            actor,
            "appointment.create",
            "appointment",
            str(appointment_id),
            {"doctor": doctor.full_name, "date": day.isoformat(), "time": slot},
        )
        return appointment_id

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
        with self._write_lock:
            doctor = self.get_doctor(doctor_id)
            if doctor is None:
                raise BookingError(
                    f"Unknown doctor '{doctor_id}'.", code="unknown_doctor"
                )

            slot = self._db.normalize_time_slot(start_time)
            if slot is None:
                raise BookingError(
                    f"'{start_time}' is not a valid time.", code="invalid_time"
                )

            details = dict(
                doctor=doctor,
                slot=slot,
                day=day,
                patient_name=patient_name,
                patient_phone=patient_phone,
                patient_address=patient_address,
                reason=reason,
                description=description,
                referred_by=referred_by,
                emergency=emergency,
                source=source,
                actor=actor,
            )

            def operation() -> int:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    with self._shared_connection(conn):
                        return self._book_in_transaction(conn, **details)

            try:
                appointment_id = retry_sqlite_write(operation)
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc

        appointment = self.get_appointment(str(appointment_id))
        if appointment is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment was created but could not be read back.")
        return appointment

    def reschedule_appointment(
        self,
        appointment_id: str,
        *,
        day: date,
        start_time: str,
        actor: str = "receptionist",
    ) -> Appointment:
        with self._write_lock:
            current = self.get_appointment(appointment_id)
            if current is None:
                raise DataSourceError(
                    f"Appointment #{appointment_id} was not found.", code="not_found"
                )
            if not current.is_active:
                raise BookingError(
                    f"Appointment #{appointment_id} is {current.status.value} and cannot be moved.",
                    code="inactive",
                )

            slot = self._db.normalize_time_slot(start_time)
            if slot is None:
                raise BookingError(
                    f"'{start_time}' is not a valid time.", code="invalid_time"
                )

            if (
                current.start.date() == day
                and current.start.strftime("%I:%M %p") == slot
            ):
                raise BookingError("Appointment is already at that slot.", code="same_slot")

            doctor = self.get_doctor(current.doctor_id)
            if doctor is None:  # pragma: no cover - defensive
                raise BookingError(
                    f"Unknown doctor '{current.doctor_id}'.", code="unknown_doctor"
                )

            def operation() -> int:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    with self._shared_connection(conn):
                        result = self._db.book_appointment(
                            patient_name=current.patient_name,
                            patient_phone=current.patient_phone,
                            doctor_name=doctor.full_name,
                            date=day.isoformat(),
                            time_slot=slot,
                            is_emergency=int(current.emergency),
                        )
                        if not result.get("success"):
                            raise BookingError(
                                result.get("error", "Booking failed."),
                                code="slot_taken",
                            )
                        new_id = int(result["appointment_id"])
                        self._write_extra_conn(
                            conn,
                            new_id,
                            reason=current.reason,
                            description=current.description,
                            referred_by=current.referred_by,
                            source=current.source,
                            actor=actor,
                            patient_address=current.patient_address,
                        )
                        self._save_patient_address_conn(
                            conn, current.patient_phone, current.patient_address
                        )
                        cancelled = self._db.cancel_appointment(
                            int(appointment_id), reason=f"Rescheduled to #{new_id}"
                        )
                        if not cancelled.get("success"):
                            raise DataSourceError(
                                cancelled.get(
                                    "error", "Could not release the original slot."
                                ),
                                code="reschedule_failed",
                            )
                        # Mark the freed row as rescheduled so it is distinct
                        # from a plain cancellation.
                        conn.execute(
                            "UPDATE appointments SET notification_status = 'rescheduled' "
                            "WHERE id = ?",
                            (int(appointment_id),),
                        )
                        self._audit_conn(
                            conn,
                            actor,
                            "appointment.reschedule",
                            "appointment",
                            str(appointment_id),
                            {"to": new_id},
                        )
                        return new_id

            try:
                new_appointment_id = retry_sqlite_write(operation)
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc

        moved = self.get_appointment(str(new_appointment_id))
        if moved is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment was not saved.", code="write_failed")
        return moved

    def cancel_appointment(
        self, appointment_id: str, *, reason: str, actor: str = "receptionist"
    ) -> Appointment:
        with self._write_lock:
            try:
                result = retry_sqlite_write(
                    lambda: self._db.cancel_appointment(int(appointment_id), reason=reason)
                )
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc
        if not result.get("success"):
            raise BookingError(result.get("error", "Cancellation failed."), code="cancel_failed")
        self._audit(
            actor, "appointment.cancel", "appointment", appointment_id, {"reason": reason}
        )
        appointment = self.get_appointment(appointment_id)
        if appointment is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment vanished after cancellation.")
        return appointment

    def mark_no_show(self, appointment_id: str, *, actor: str = "receptionist") -> Appointment:
        with self._write_lock:
            try:
                result = retry_sqlite_write(
                    lambda: self._db.mark_no_show(int(appointment_id))
                )
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc
        if not result.get("success"):
            raise BookingError(result.get("error", "Could not mark no-show."), code="no_show_failed")
        self._audit(actor, "appointment.no_show", "appointment", appointment_id, {})
        appointment = self.get_appointment(appointment_id)
        if appointment is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment vanished after no-show.")
        return appointment

    def check_in(self, appointment_id: str, *, actor: str = "receptionist") -> Appointment:
        with self._write_lock:
            try:
                updated = retry_sqlite_write(
                    lambda: self._db.mark_appointment_arrived(int(appointment_id))
                )
            except sqlite3.OperationalError as exc:
                raise self._db_busy_error(exc) from exc
        if not updated:
            raise BookingError("Appointment not found.", code="not_found")
        self._audit(actor, "appointment.check_in", "appointment", appointment_id, {})
        appointment = self.get_appointment(appointment_id)
        if appointment is None:  # pragma: no cover - defensive
            raise DataSourceError("Appointment vanished after check-in.")
        return appointment

    # -- patients ----------------------------------------------------------

    def lookup_patient(self, phone: str) -> Patient | None:
        clean = str(phone).strip()
        if not clean:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT patient_name, patient_phone FROM patient_profiles "
                "WHERE patient_phone = ?",
                (clean,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT patient_name, patient_phone FROM appointments "
                    "WHERE patient_phone = ? ORDER BY id DESC LIMIT 1",
                    (clean,),
                ).fetchone()
            address_row = conn.execute(
                "SELECT address_json FROM cal_patient_addresses WHERE phone = ?",
                (clean,),
            ).fetchone()
            if not address_row:
                address_row = conn.execute(
                    "SELECT x.address_json FROM appointments a "
                    "LEFT JOIN cal_appointment_extra x ON x.appointment_id = a.id "
                    "WHERE a.patient_phone = ? AND x.address_json IS NOT NULL "
                    "ORDER BY a.id DESC LIMIT 1",
                    (clean,),
                ).fetchone()
        if not row:
            return None
        return Patient(
            id=patient_id_for_phone(row["patient_phone"]),
            name=row["patient_name"],
            phone=row["patient_phone"],
            address=address_from_json(address_row["address_json"])
            if address_row
            else None,
        )

    def list_patient_appointments(self, phone: str) -> list[Appointment]:
        clean = str(phone).strip()
        if not clean:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.patient_name, a.patient_phone, a.doctor_name,
                       a.date, a.time_slot, a.status, a.is_emergency, a.arrived,
                       a.notification_status, a.created_at,
                       x.reason, x.description, x.referred_by, x.source,
                       x.address_json
                FROM appointments a
                LEFT JOIN cal_appointment_extra x ON x.appointment_id = a.id
                WHERE a.patient_phone = ?
                ORDER BY a.date, a.time_slot
                """,
                (clean,),
            ).fetchall()
        appointments = [self._row_to_appointment(row) for row in rows]
        self._assign_tokens(appointments)
        return appointments

    # -- holidays ----------------------------------------------------------

    def list_holidays(self, start: date, end: date) -> list[Holiday]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, scope, doctor_key, holiday_date, end_date, name, type
                FROM cal_holidays
                WHERE holiday_date <= ? AND COALESCE(end_date, holiday_date) >= ?
                ORDER BY holiday_date
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return [
            Holiday(
                id=str(row["id"]),
                date=date.fromisoformat(row["holiday_date"]),
                end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
                name=row["name"],
                scope=row["scope"],
                doctor_id=row["doctor_key"],
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
                """
                SELECT id, scope, doctor_key, holiday_date, end_date, name, type
                FROM cal_holidays
                WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return Holiday(
            id=str(row["id"]),
            date=date.fromisoformat(row["holiday_date"]),
            end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
            name=row["name"],
            scope=row["scope"],
            doctor_id=row["doctor_key"],
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
                INSERT INTO cal_holidays
                    (scope, doctor_key, holiday_date, end_date, name, type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    doctor_id,
                    day.isoformat(),
                    end_date.isoformat() if end_date else None,
                    name,
                    holiday_type,
                ),
            )
            conn.commit()
            holiday_id = cursor.lastrowid
        self._audit(actor, "holiday.create", "holiday", holiday_id, {"name": name})
        return Holiday(
            id=str(holiday_id),
            date=day,
            end_date=end_date,
            name=name,
            scope=scope,
            doctor_id=doctor_id,
            type=holiday_type,
        )

    def delete_holiday(self, holiday_id: str, *, actor: str = "admin") -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM cal_holidays WHERE id = ?", (int(holiday_id),))
            conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self._audit(actor, "holiday.delete", "holiday", holiday_id, {})
        return deleted

    # -- admin -------------------------------------------------------------

    def create_doctor(
        self,
        *,
        full_name: str,
        specialization: str,
        available_days: str,
        actor: str = "admin",
    ) -> Doctor:
        name = full_name.strip()
        if not name:
            raise BookingError("Doctor name is required.", code="invalid_name")
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM doctors WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()
            if exists:
                raise BookingError(
                    f"A doctor named '{name}' already exists.", code="duplicate_doctor"
                )
            conn.execute(
                "INSERT INTO doctors (name, specialty, available_days) VALUES (?, ?, ?)",
                (name, specialization.strip(), available_days.strip()),
            )
            conn.commit()
        self._register_doctor(name)
        self._audit(actor, "doctor.create", "doctor", name, {"specialty": specialization})
        doctor = self._doctor_by_name(name)
        if doctor is None:  # pragma: no cover - defensive
            raise DataSourceError("Doctor was created but could not be read back.")
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
            raise BookingError(f"Unknown doctor '{doctor_id}'.", code="unknown_doctor")

        updates: list[str] = []
        params: list[Any] = []
        if specialization is not None:
            updates.append("specialty = ?")
            params.append(specialization.strip())
        if available_days is not None:
            updates.append("available_days = ?")
            params.append(available_days.strip())
        if not updates:
            return doctor

        params.append(doctor.full_name)
        with self._connect() as conn:
            conn.execute(f"UPDATE doctors SET {', '.join(updates)} WHERE name = ?", params)
            conn.commit()
        self._audit(actor, "doctor.update", "doctor", doctor_id, {"fields": updates})
        updated = self._doctor_by_name(doctor.full_name)
        if updated is None:  # pragma: no cover - defensive
            raise DataSourceError("Doctor was updated but could not be read back.")
        return updated

    def delete_doctor(self, doctor_id: str, *, actor: str = "admin") -> bool:
        doctor = self.get_doctor(doctor_id)
        if doctor is None:
            raise BookingError(f"Unknown doctor '{doctor_id}'.", code="unknown_doctor")
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM appointments WHERE doctor_name = ?",
                (doctor.full_name,),
            ).fetchone()[0]
            if count:
                raise BookingError(
                    f"{doctor.full_name} has {count} appointment(s) on record and cannot be deleted.",
                    code="doctor_in_use",
                )
            conn.execute("DELETE FROM doctors WHERE name = ?", (doctor.full_name,))
            conn.execute("DELETE FROM cal_doctor_map WHERE doctor_key = ?", (doctor_id,))
            conn.commit()
        self._audit(actor, "doctor.delete", "doctor", doctor_id, {})
        return True

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT actor, action, entity, entity_id, detail, created_at "
                "FROM cal_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "actor": row["actor"],
                "action": row["action"],
                "entity": row["entity"],
                "entity_id": row["entity_id"],
                "detail": json.loads(row["detail"]) if row["detail"] else {},
                "created_at": row["created_at"],
            }
            for row in rows
        ]
