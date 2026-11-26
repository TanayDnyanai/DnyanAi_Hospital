"""Appointment write endpoints."""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import config
from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..deps import Actor, get_actor
from ..domain import Appointment, BookingError
from ..services import booking_service
from ..validation import numeric_id

router = APIRouter(prefix="/api", tags=["appointments"])


class AddressInfo(BaseModel):
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    pincode: str | None = Field(default=None, max_length=10)
    country: str | None = Field(default=None, max_length=60)

    @field_validator("*")
    @classmethod
    def strip_fields(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("pincode")
    @classmethod
    def six_digit_pincode(cls, value: str | None) -> str | None:
        if not value:
            return None
        digits = "".join(char for char in value if char.isdigit())
        if len(digits) != 6:
            raise ValueError("PIN code must be exactly 6 digits")
        return digits


class AppointmentCreate(BaseModel):
    doctor_id: str = Field(min_length=1, max_length=80)
    date: date_type
    time: str = Field(min_length=4, max_length=10)
    patient_name: str = Field(min_length=1, max_length=120)
    patient_phone: str = Field(min_length=10, max_length=20)
    address: AddressInfo | None = None
    reason: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    referred_by: str | None = Field(default=None, max_length=200)
    emergency: bool = False

    @field_validator("patient_name")
    @classmethod
    def required_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("patient_phone")
    @classmethod
    def ten_digit_phone(cls, value: str) -> str:
        digits = "".join(char for char in value if char.isdigit())
        if len(digits) != 10:
            raise ValueError("phone number must be exactly 10 digits")
        return digits

    @field_validator("reason")
    @classmethod
    def required_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class AppointmentPatch(BaseModel):
    date: date_type | None = None
    time: str | None = None
    status: str | None = None


class CancelRequest(BaseModel):
    reason: str = Field(default="Cancelled from calendar", max_length=300)


def _can_view(actor: Actor, appointment: Appointment) -> bool:
    if actor.role == "doctor":
        return appointment.doctor_id == actor.doctor_id
    return actor.role in ("patient", "receptionist", "admin")


def _ensure_visible(actor: Actor, appointment: Appointment | None) -> Appointment:
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if not _can_view(actor, appointment):
        raise HTTPException(status_code=403, detail="Not allowed for this role.")
    return appointment


def _require_booking_manager(actor: Actor) -> None:
    if not actor.is_booking_manager:
        raise HTTPException(
            status_code=403, detail="This role cannot manage appointments."
        )


@router.get("/appointments/{appointment_id}")
def get_appointment(
    appointment_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    appointment = _ensure_visible(actor, source.get_appointment(numeric_id(appointment_id, label="Appointment")))
    return appointment.to_dict()


@router.post("/appointments", status_code=201)
def create_appointment(
    payload: AppointmentCreate,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    if actor.role not in ("patient", *config.STAFF_ROLES):
        raise HTTPException(
            status_code=403, detail="This role cannot create appointments."
        )
    if actor.role == "patient" and not config.ALLOW_PATIENT_BOOKING:
        raise HTTPException(
            status_code=403,
            detail="Patient self-booking is disabled. Please contact reception.",
        )
    appointment = booking_service.book(
        source,
        role=actor.role,
        actor=actor.label,
        doctor_id=payload.doctor_id,
        day=payload.date,
        start_time=payload.time,
        patient_name=payload.patient_name,
        patient_phone=payload.patient_phone,
        patient_address=payload.address.model_dump() if payload.address else None,
        reason=payload.reason,
        description=payload.description,
        referred_by=payload.referred_by,
        emergency=payload.emergency,
    )
    return appointment.to_dict()


@router.patch("/appointments/{appointment_id}")
def patch_appointment(
    appointment_id: str,
    payload: AppointmentPatch,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    _require_booking_manager(actor)
    appointment_id = numeric_id(appointment_id, label="Appointment")

    if payload.date or payload.time:
        if not (payload.date and payload.time):
            raise HTTPException(
                status_code=400,
                detail="Both 'date' and 'time' are required to reschedule.",
            )
        appointment = booking_service.reschedule(
            source,
            role=actor.role,
            actor=actor.label,
            appointment_id=appointment_id,
            day=payload.date,
            start_time=payload.time,
        )
        return appointment.to_dict()

    if payload.status:
        status = payload.status.strip().upper()
        if status == "CANCELLED":
            return source.cancel_appointment(
                appointment_id, reason="Cancelled from calendar", actor=actor.label
            ).to_dict()
        if status == "NO_SHOW":
            return source.mark_no_show(appointment_id, actor=actor.label).to_dict()
        if status == "CHECKED_IN":
            return source.check_in(appointment_id, actor=actor.label).to_dict()
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported status transition '{payload.status}'.",
        )

    raise HTTPException(status_code=400, detail="Nothing to update.")


@router.post("/appointments/{appointment_id}/cancel")
def cancel_appointment(
    appointment_id: str,
    payload: CancelRequest = CancelRequest(),
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    _require_booking_manager(actor)
    appointment_id = numeric_id(appointment_id, label="Appointment")
    return source.cancel_appointment(
        appointment_id, reason=payload.reason, actor=actor.label
    ).to_dict()


@router.post("/appointments/{appointment_id}/no-show")
def mark_no_show(
    appointment_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    appointment_id = numeric_id(appointment_id, label="Appointment")
    appointment = source.get_appointment(appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    allowed = actor.is_booking_manager or (
        actor.role == "doctor"
        and config.ALLOW_DOCTOR_STATUS_UPDATES
        and appointment.doctor_id == actor.doctor_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Not allowed for this role.")
    return source.mark_no_show(appointment_id, actor=actor.label).to_dict()


@router.post("/appointments/{appointment_id}/check-in")
def check_in(
    appointment_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    appointment_id = numeric_id(appointment_id, label="Appointment")
    appointment = source.get_appointment(appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    allowed = actor.is_booking_manager or (
        actor.role == "doctor"
        and config.ALLOW_DOCTOR_STATUS_UPDATES
        and appointment.doctor_id == actor.doctor_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Not allowed for this role.")
    return source.check_in(appointment_id, actor=actor.label).to_dict()
