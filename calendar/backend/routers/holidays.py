"""Holiday and clinic-closure endpoints."""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..deps import Actor, get_actor, require_admin
from ..domain import HOLIDAY_TYPES
from ..services.calendar_service import today
from ..validation import numeric_id

router = APIRouter(prefix="/api", tags=["holidays"])


class HolidayCreate(BaseModel):
    date: date_type
    name: str = Field(min_length=1, max_length=120)
    scope: str = Field(default="CLINIC", max_length=20)
    doctor_id: str | None = Field(default=None, max_length=80)
    end_date: date_type | None = None
    type: str = Field(default="PUBLIC_HOLIDAY", max_length=30)


class LeaveCreate(BaseModel):
    date: date_type
    reason: str = Field(min_length=1, max_length=120)
    confirm_conflicts: bool = False


def _require_doctor(actor: Actor) -> str:
    if actor.role != "doctor" or not actor.doctor_id:
        raise HTTPException(
            status_code=403,
            detail="Only a doctor can manage their own leave.",
        )
    return actor.doctor_id


def _active_appointment_count(source: DataSource, doctor_id: str, day: date_type) -> int:
    return sum(
        1 for event in source.list_events(day, day, doctor_id) if event.is_active
    )


@router.get("/holidays")
def list_holidays(
    start: date_type | None = None,
    end: date_type | None = None,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    start = start or date_type.today()
    end = end or date_type(start.year + 1, 12, 31)
    return {
        "holidays": [
            holiday.to_dict() for holiday in source.list_holidays(start, end)
        ]
    }


@router.post("/holidays", status_code=201)
def create_holiday(
    payload: HolidayCreate,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    scope = payload.scope.strip().upper()
    if scope not in ("CLINIC", "DOCTOR"):
        raise HTTPException(status_code=400, detail="scope must be CLINIC or DOCTOR.")
    if scope == "DOCTOR" and not payload.doctor_id:
        raise HTTPException(status_code=400, detail="doctor_id is required for DOCTOR scope.")
    if payload.type not in HOLIDAY_TYPES:
        raise HTTPException(
            status_code=400, detail=f"type must be one of {', '.join(HOLIDAY_TYPES)}."
        )
    if payload.end_date and payload.end_date < payload.date:
        raise HTTPException(status_code=400, detail="end_date must be on or after date.")

    holiday = source.create_holiday(
        day=payload.date,
        name=payload.name,
        scope=scope,
        doctor_id=payload.doctor_id,
        end_date=payload.end_date,
        holiday_type=payload.type,
        actor=actor.label,
    )
    return holiday.to_dict()


@router.delete("/holidays/{holiday_id}")
def delete_holiday(
    holiday_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    holiday_id = numeric_id(holiday_id, label="Holiday")
    if not source.delete_holiday(holiday_id, actor=actor.label):
        raise HTTPException(status_code=404, detail="Holiday not found.")
    return {"deleted": True, "id": holiday_id}


# --- Doctor self-service leave --------------------------------------------


@router.get("/doctor/leave/conflicts")
def leave_conflicts(
    date: date_type,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    """Preview how many active appointments a leave day would overlap."""
    doctor_id = _require_doctor(actor)
    return {
        "date": date.isoformat(),
        "count": _active_appointment_count(source, doctor_id, date),
    }


@router.post("/doctor/leave", status_code=201, response_model=None)
def apply_leave(
    payload: LeaveCreate,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
):
    doctor_id = _require_doctor(actor)
    day = payload.date

    if day < today():
        raise HTTPException(
            status_code=400, detail="Leave cannot be applied for a past date."
        )

    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required.")

    for holiday in source.list_holidays(day, day):
        if (
            holiday.scope == "DOCTOR"
            and holiday.doctor_id == doctor_id
            and holiday.type == "LEAVE"
            and holiday.covers(day, doctor_id)
        ):
            raise HTTPException(
                status_code=409, detail="You already have leave on this day."
            )

    conflicts = _active_appointment_count(source, doctor_id, day)
    if conflicts and not payload.confirm_conflicts:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"{conflicts} appointment(s) are already booked on "
                    f"{day.strftime('%d %b %Y')}. They will not be cancelled."
                ),
                "code": "leave_conflicts",
                "conflicts": conflicts,
            },
        )

    holiday = source.create_holiday(
        day=day,
        name=reason,
        scope="DOCTOR",
        doctor_id=doctor_id,
        holiday_type="LEAVE",
        actor=actor.label,
    )
    return {"leave": holiday.to_dict(), "conflicts": conflicts}


@router.delete("/doctor/leave/{holiday_id}")
def cancel_leave(
    holiday_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    doctor_id = _require_doctor(actor)
    holiday_id = numeric_id(holiday_id, label="Leave")
    holiday = source.get_holiday(holiday_id)
    if (
        holiday is None
        or holiday.scope != "DOCTOR"
        or holiday.type != "LEAVE"
        or holiday.doctor_id != doctor_id
    ):
        raise HTTPException(status_code=404, detail="Leave not found.")

    source.delete_holiday(holiday_id, actor=actor.label)
    return {"deleted": True, "id": holiday_id}
