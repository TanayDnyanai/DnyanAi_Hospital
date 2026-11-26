"""Calendar read endpoints: ranges, day grids, counters."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..deps import Actor, get_actor
from ..domain import AppointmentStatus
from ..services import calendar_service

router = APIRouter(prefix="/api", tags=["calendar"])

MAX_RANGE_DAYS = 62


def _validate_range(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(status_code=400, detail="'to' must be on or after 'from'.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Range too large. Maximum is {MAX_RANGE_DAYS} days.",
        )


def _parse_doctor_filter(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    # Legacy ids look like "dr-1"; production/Neon ids are numeric strings.
    if not text or not all(char.isalnum() or char in "-_" for char in text):
        raise HTTPException(status_code=400, detail="Invalid doctor_id.")
    return text


def _parse_statuses(value: str | None) -> set[AppointmentStatus] | None:
    if not value:
        return None
    statuses: set[AppointmentStatus] = set()
    for raw in value.split(","):
        item = raw.strip().upper()
        if not item:
            continue
        try:
            statuses.add(AppointmentStatus(item))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Unknown status '{raw.strip()}'."
            ) from exc
    return statuses or None


@router.get("/calendar")
def get_calendar(
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
    doctor_id: str | None = None,
    status: str | None = None,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    _validate_range(start, end)
    return calendar_service.get_calendar(
        source,
        start=start,
        end=end,
        doctor_id=_parse_doctor_filter(doctor_id),
        role=actor.role,
        actor_doctor_id=actor.doctor_id,
        statuses=_parse_statuses(status),
    )


@router.get("/day-grid")
def get_day_grid(
    day: date = Query(alias="date"),
    doctor_id: str | None = None,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    return calendar_service.get_day(
        source,
        day=day,
        doctor_id=_parse_doctor_filter(doctor_id),
        role=actor.role,
        actor_doctor_id=actor.doctor_id,
    )


@router.get("/stats")
def get_stats(
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    day: date | None = Query(default=None, alias="date"),
    doctor_id: str | None = None,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    if day is not None:
        start = end = day
    if start is None or end is None:
        start = end = calendar_service.today()
    if end < start:
        raise HTTPException(status_code=400, detail="'to' must be on or after 'from'.")
    return calendar_service.get_stats(
        source,
        start=start,
        end=end,
        doctor_id=_parse_doctor_filter(doctor_id),
        role=actor.role,
        actor_doctor_id=actor.doctor_id,
    )
