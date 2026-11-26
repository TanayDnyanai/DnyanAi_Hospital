"""Storage-agnostic domain model.

These objects mirror the production schema (appointments / doctor_profiles)
rather than the local SQLite layout, so swapping data sources never leaks
into the API or the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum


ADDRESS_FIELDS = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "pincode",
    "country",
)


def normalize_address(value: object) -> dict[str, str] | None:
    """Return a trimmed address dict, or None when every field is blank."""
    if value is None:
        return None
    raw = dict(value) if isinstance(value, dict) else {}
    address = {
        key: str(raw.get(key) or "").strip()[:120]
        for key in ADDRESS_FIELDS
    }
    return address if any(address.values()) else None


def address_to_json(address: dict[str, str] | None) -> str | None:
    normalized = normalize_address(address)
    return json.dumps(normalized) if normalized else None


def address_from_json(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return normalize_address(parsed)


class AppointmentStatus(str, Enum):
    BOOKED = "BOOKED"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    RESCHEDULED = "RESCHEDULED"


ACTIVE_STATUSES = frozenset(
    {
        AppointmentStatus.BOOKED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CHECKED_IN,
    }
)

HOLIDAY_TYPES = ("PUBLIC_HOLIDAY", "CLOSED", "LEAVE")


class DataSourceError(Exception):
    """Raised when a storage operation cannot be completed."""

    def __init__(self, message: str, code: str = "data_error") -> None:
        super().__init__(message)
        self.code = code


class BookingError(Exception):
    """Raised when a booking violates a clinic rule."""

    def __init__(self, message: str, code: str = "booking_error") -> None:
        super().__init__(message)
        self.code = code


def _iso(value: datetime | date | time | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value.strftime("%H:%M")


@dataclass(slots=True)
class Schedule:
    weekday: int  # 0 = Monday
    start_time: time
    end_time: time
    slot_minutes: int = 30

    def to_dict(self) -> dict:
        return {
            "weekday": self.weekday,
            "start_time": _iso(self.start_time),
            "end_time": _iso(self.end_time),
            "slot_minutes": self.slot_minutes,
        }

    def slots(self) -> list[time]:
        """Return every slot start inside this window."""
        result: list[time] = []
        cursor = datetime.combine(date.min, self.start_time)
        limit = datetime.combine(date.min, self.end_time)
        while cursor < limit:
            result.append(cursor.time())
            cursor += timedelta(minutes=self.slot_minutes)
        return result


@dataclass(slots=True)
class Doctor:
    id: str
    full_name: str
    specialization: str
    qualification: str | None = None
    experience_years: int | None = None
    consultation_fee: Decimal | None = None
    room_number: str | None = None
    available: bool = True
    color: str = "#1a73e8"
    schedules: list[Schedule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "specialization": self.specialization,
            "qualification": self.qualification,
            "experience_years": self.experience_years,
            "consultation_fee": (
                float(self.consultation_fee) if self.consultation_fee is not None else None
            ),
            "room_number": self.room_number,
            "available": self.available,
            "color": self.color,
            "schedules": [schedule.to_dict() for schedule in self.schedules],
        }


@dataclass(slots=True)
class Patient:
    id: str
    name: str
    phone: str
    gender: str | None = None
    date_of_birth: date | None = None
    address: dict[str, str] | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "phone": self.phone,
            "gender": self.gender,
            "date_of_birth": _iso(self.date_of_birth),
            "address": self.address,
        }


@dataclass(slots=True)
class Appointment:
    id: str
    patient_id: str
    patient_name: str
    patient_phone: str
    doctor_id: str
    doctor_name: str
    start: datetime
    end: datetime
    status: AppointmentStatus = AppointmentStatus.BOOKED
    emergency: bool = False
    arrived: bool = False
    reason: str | None = None
    description: str | None = None
    referred_by: str | None = None
    token_number: int | None = None
    source: str = "calendar"
    created_at: datetime | None = None
    patient_address: dict[str, str] | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_dict(self, redact: bool = False) -> dict:
        if redact:
            # Patient-facing payload: no identity, no clinical notes, no token.
            return {
                "doctor_id": self.doctor_id,
                "doctor_name": self.doctor_name,
                "start": _iso(self.start),
                "end": _iso(self.end),
                "status": self.status.value,
                "is_active": self.is_active,
            }
        return {
            "id": self.id,
            "doctor_id": self.doctor_id,
            "doctor_name": self.doctor_name,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "status": self.status.value,
            "emergency": self.emergency,
            "token_number": self.token_number,
            "source": self.source,
            "reason": self.reason,
            "is_active": self.is_active,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "patient_phone": self.patient_phone,
            "patient_address": self.patient_address,
            "description": self.description,
            "referred_by": self.referred_by,
            "arrived": self.arrived,
            "created_at": _iso(self.created_at),
        }


@dataclass(slots=True)
class Holiday:
    id: str
    date: date
    name: str
    scope: str = "CLINIC"  # CLINIC | DOCTOR
    doctor_id: str | None = None
    end_date: date | None = None
    type: str = "PUBLIC_HOLIDAY"

    def covers(self, day: date, doctor_id: str | None = None) -> bool:
        last = self.end_date or self.date
        if not (self.date <= day <= last):
            return False
        if self.scope == "CLINIC":
            return True
        return doctor_id is not None and self.doctor_id == doctor_id

    @property
    def blocks_booking(self) -> bool:
        """Only doctor-specific leave blocks slots; clinic-wide days stay open."""
        return self.scope == "DOCTOR"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "date": _iso(self.date),
            "end_date": _iso(self.end_date),
            "name": self.name,
            "scope": self.scope,
            "doctor_id": self.doctor_id,
            "type": self.type,
        }
