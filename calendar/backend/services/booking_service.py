"""Write-side rules for the calendar.

All guards live here so the routers stay thin and the same checks apply to
every entry point (calendar UI, future voice-agent tools, scripts).
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta

from .. import config
from ..datasource.base import DataSource
from ..domain import Appointment, BookingError, Doctor, normalize_address
from .calendar_service import today

# Serialises the read-check/write sequence inside one server process. Multiple
# workers still rely on the database's active-slot unique index.
_WRITE_LOCK = threading.RLock()


def parse_time(value: str) -> time:
    clean = str(value).strip().upper()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return datetime.strptime(clean, fmt).time()
        except ValueError:
            continue
    raise BookingError(
        f"'{value}' is not a valid time. Use HH:MM (24-hour) or hh:mm AM/PM.",
        code="invalid_time",
    )


def _guard(
    source: DataSource,
    doctor: Doctor,
    day: date,
    slot: time,
    *,
    ignore_appointment_id: str | None = None,
) -> None:
    current_day = today()

    if day < current_day:
        raise BookingError(
            f"{day.isoformat()} is in the past.", code="past_date"
        )

    if day == current_day:
        slot_start = datetime.combine(day, slot, tzinfo=config.CLINIC_TZ)
        slot_end = slot_start + timedelta(minutes=config.SLOT_MINUTES)
        if slot_end <= datetime.now(config.CLINIC_TZ):
            raise BookingError(
                "That slot has already ended today.", code="slot_ended"
            )

    for holiday in source.list_holidays(day, day):
        # Common (clinic-wide) leave is informational only — both patients
        # and staff may still book on it. Doctor leave and closures block slots.
        if holiday.blocks_booking and holiday.covers(day, doctor.id):
            raise BookingError(
                f"{day.strftime('%d %b')} is unavailable: {holiday.name}.",
                code="holiday",
            )

    windows = [item for item in doctor.schedules if item.weekday == day.weekday()]
    if not windows:
        raise BookingError(
            f"{doctor.full_name} does not consult on {day.strftime('%A')}s.",
            code="not_scheduled",
        )

    valid_slots = {item for window in windows for item in window.slots()}
    if slot not in valid_slots:
        raise BookingError(
            f"{slot.strftime('%H:%M')} is not a consultation slot for {doctor.full_name}.",
            code="invalid_slot",
        )

    for event in source.list_events(day, day, doctor.id):
        if not event.is_active:
            continue
        if event.start.time() != slot:
            continue
        if ignore_appointment_id and event.id == str(ignore_appointment_id):
            continue
        raise BookingError(
            f"That slot is already booked (appointment #{event.id}).",
            code="slot_taken",
        )


def book(
    source: DataSource,
    *,
    role: str,
    actor: str,
    doctor_id: str,
    day: date,
    start_time: str,
    patient_name: str,
    patient_phone: str,
    patient_address: dict[str, str] | None = None,
    reason: str | None = None,
    description: str | None = None,
    referred_by: str | None = None,
    emergency: bool = False,
) -> Appointment:
    if role == "patient" and not config.ALLOW_PATIENT_BOOKING:
        raise BookingError(
            "Patient self-booking is disabled. Please contact reception.",
            code="patient_booking_disabled",
        )

    name = (patient_name or "").strip()
    phone = (patient_phone or "").strip()
    if not name:
        raise BookingError("Patient name is required.", code="invalid_patient")
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) != 10:
        raise BookingError(
            "Phone number must be exactly 10 digits.", code="invalid_patient"
        )
    phone = digits

    reason = (reason or "").strip()
    if not reason:
        raise BookingError("A reason / symptoms is required.", code="invalid_reason")

    address = normalize_address(patient_address)
    if address and address.get("pincode") and len(address["pincode"]) != 6:
        raise BookingError(
            "PIN code must be exactly 6 digits.", code="invalid_address"
        )

    doctor = source.get_doctor(doctor_id)
    if doctor is None:
        raise BookingError(f"Unknown doctor '{doctor_id}'.", code="unknown_doctor")

    slot = parse_time(start_time)
    with _WRITE_LOCK:
        _guard(source, doctor, day, slot)

        return source.create_appointment(
            patient_name=name,
            patient_phone=phone,
            doctor_id=doctor_id,
            day=day,
            start_time=slot.strftime("%H:%M"),
            patient_address=address,
            reason=reason,
            description=description,
            referred_by=referred_by,
            emergency=emergency,
            source="calendar",
            actor=actor,
        )


def reschedule(
    source: DataSource,
    *,
    role: str,
    actor: str,
    appointment_id: str,
    day: date,
    start_time: str,
) -> Appointment:
    if role not in (*config.STAFF_ROLES, "patient"):
        raise BookingError(
            "This role cannot reschedule appointments.",
            code="forbidden",
        )

    appointment = source.get_appointment(appointment_id)
    if appointment is None:
        raise BookingError(
            f"Appointment #{appointment_id} was not found.", code="not_found"
        )
    if not appointment.is_active:
        raise BookingError(
            f"Appointment #{appointment_id} is {appointment.status.value}.",
            code="inactive",
        )

    doctor = source.get_doctor(appointment.doctor_id)
    if doctor is None:  # pragma: no cover - defensive
        raise BookingError("The appointment's doctor no longer exists.", code="unknown_doctor")

    slot = parse_time(start_time)
    with _WRITE_LOCK:
        _guard(
            source,
            doctor,
            day,
            slot,
            ignore_appointment_id=appointment_id,
        )

        return source.reschedule_appointment(
            appointment_id,
            day=day,
            start_time=slot.strftime("%H:%M"),
            actor=actor,
        )
