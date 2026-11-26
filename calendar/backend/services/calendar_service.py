"""Read-side calendar logic: ranges, day grids and counters."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable

from .. import config
from ..domain import ACTIVE_STATUSES, Appointment, AppointmentStatus, BookingError, Doctor
from ..datasource.base import DataSource


def today() -> date:
    return datetime.now(config.CLINIC_TZ).date()


def _iter_days(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def resolve_doctor_filter(
    role: str, actor_doctor_id: str | None, requested: str | None
) -> str | None:
    """Doctors may only ever read their own column."""
    if role == "doctor":
        if not actor_doctor_id:
            raise BookingError(
                "The doctor role requires a doctor id.", code="doctor_required"
            )
        return actor_doctor_id
    return requested or None


def _holiday_for(holidays, day: date, doctor_id: str | None = None):
    """Return the first holiday that actually blocks booking."""
    for holiday in holidays:
        if holiday.blocks_booking and holiday.covers(day, doctor_id):
            return holiday
    return None


def _slots_for(doctor: Doctor, day: date) -> list[time]:
    slots: list[time] = []
    for schedule in doctor.schedules:
        if schedule.weekday == day.weekday():
            slots.extend(schedule.slots())
    return slots


def _available_count(
    doctors: list[Doctor],
    day: date,
    events: list[Appointment],
    holidays,
) -> int:
    booked = defaultdict(int)
    for event in events:
        if event.is_active:
            booked[event.doctor_id] += 1

    total = 0
    for doctor in doctors:
        if _holiday_for(holidays, day, doctor.id):
            continue
        total += max(0, len(_slots_for(doctor, day)) - booked.get(doctor.id, 0))
    return total


def get_calendar(
    source: DataSource,
    *,
    start: date,
    end: date,
    doctor_id: str | None = None,
    role: str = "patient",
    actor_doctor_id: str | None = None,
    statuses: set[AppointmentStatus] | None = None,
) -> dict:
    doctor_id = resolve_doctor_filter(role, actor_doctor_id, doctor_id)
    doctors = source.get_doctors()
    if doctor_id:
        doctors = [doctor for doctor in doctors if doctor.id == doctor_id]

    events = source.list_events(start, end, doctor_id)
    if statuses:
        events = [event for event in events if event.status in statuses]
    holidays = source.list_holidays(start, end)

    by_day: dict[date, list[Appointment]] = defaultdict(list)
    for event in events:
        by_day[event.start.date()].append(event)

    redact = False  # patients have the same access as receptionists
    current = today()

    days: list[dict] = []
    for day in _iter_days(start, end):
        day_events = sorted(by_day.get(day, []), key=lambda item: item.start)
        active = [event for event in day_events if event.is_active]
        clinic_holiday = next(
            (holiday for holiday in holidays if holiday.scope == "CLINIC" and holiday.covers(day)),
            None,
        )
        doctor_holiday = None
        if doctor_id:
            doctor_holiday = next(
                (
                    holiday
                    for holiday in holidays
                    if holiday.scope == "DOCTOR" and holiday.covers(day, doctor_id)
                ),
                None,
            )
        day_holiday = doctor_holiday or clinic_holiday

        counts: dict = {
            "total": len(active),
            "available": _available_count(doctors, day, active, holidays),
            "by_doctor": {doctor.id: 0 for doctor in doctors},
        }
        if not redact:
            counts["emergency"] = sum(1 for event in active if event.emergency)
        for event in active:
            if event.doctor_id in counts["by_doctor"]:
                counts["by_doctor"][event.doctor_id] += 1

        days.append(
            {
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "is_today": day == current,
                "is_holiday": day_holiday is not None,
                "holiday": day_holiday.to_dict() if day_holiday else None,
                "counts": counts,
                "events": [event.to_dict(redact=redact) for event in day_events],
            }
        )

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "role": role,
        "doctor_id": doctor_id,
        "doctors": [doctor.to_dict() for doctor in doctors],
        "holidays": [holiday.to_dict() for holiday in holidays],
        "days": days,
    }


def get_day(
    source: DataSource,
    *,
    day: date,
    doctor_id: str | None = None,
    role: str = "patient",
    actor_doctor_id: str | None = None,
) -> dict:
    doctor_id = resolve_doctor_filter(role, actor_doctor_id, doctor_id)
    grid = source.get_day_grid(day, doctor_id)
    redact = False  # patients have the same access as receptionists
    if redact:
        for doctor_block in grid["doctors"]:
            for slot in doctor_block["slots"]:
                if slot["appointment"]:
                    slot["appointment"] = {
                        key: slot["appointment"][key]
                        for key in (
                            "doctor_id",
                            "doctor_name",
                            "start",
                            "end",
                            "status",
                            "is_active",
                        )
                    }
    grid["role"] = role
    return grid


def get_stats(
    source: DataSource,
    *,
    start: date,
    end: date,
    doctor_id: str | None = None,
    role: str = "patient",
    actor_doctor_id: str | None = None,
) -> dict:
    doctor_id = resolve_doctor_filter(role, actor_doctor_id, doctor_id)
    doctors = source.get_doctors()
    if doctor_id:
        doctors = [doctor for doctor in doctors if doctor.id == doctor_id]

    events = [
        event
        for event in source.list_events(start, end, doctor_id)
        if event.is_active
    ]
    holidays = source.list_holidays(start, end)

    scheduled = 0
    for day in _iter_days(start, end):
        for doctor in doctors:
            if _holiday_for(holidays, day, doctor.id):
                continue
            scheduled += len(_slots_for(doctor, day))

    booked = len(events)
    per_doctor = []
    for doctor in doctors:
        doctor_booked = sum(1 for event in events if event.doctor_id == doctor.id)
        doctor_scheduled = sum(
            len(_slots_for(doctor, day))
            for day in _iter_days(start, end)
            if not _holiday_for(holidays, day, doctor.id)
        )
        per_doctor.append(
            {
                "doctor_id": doctor.id,
                "doctor_name": doctor.full_name,
                "color": doctor.color,
                "booked": doctor_booked,
                "scheduled": doctor_scheduled,
                "free": max(0, doctor_scheduled - doctor_booked),
            }
        )

    payload = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total_appointments": booked,
        "doctors": len(doctors),
        "scheduled_slots": scheduled,
        "free_slots": max(0, scheduled - booked),
        "occupancy": round(booked / scheduled, 4) if scheduled else 0.0,
        "per_doctor": per_doctor,
    }
    payload["emergency"] = sum(1 for event in events if event.emergency)
    return payload
