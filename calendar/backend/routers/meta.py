"""Reference data for the frontend: doctors, holidays, flags."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from .. import config
from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..services.calendar_service import today

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/meta")
def get_meta(source: DataSource = Depends(get_data_source)) -> dict:
    current = today()
    doctors = source.get_doctors()
    holidays = source.list_holidays(current, current + timedelta(days=365))

    return {
        "clinic": source.get_clinic_info(),
        "timezone": config.CLINIC_TIMEZONE_LABEL,
        "today": current.isoformat(),
        "slot_minutes": config.SLOT_MINUTES,
        "sessions": [
            {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
            for start, end in config.CLINIC_SESSIONS
        ],
        "open_weekdays": list(config.CLINIC_OPEN_WEEKDAYS),
        "roles": list(config.ROLES),
        "doctors": [doctor.to_dict() for doctor in doctors],
        "specialties": sorted({doctor.specialization for doctor in doctors}),
        "holidays": [holiday.to_dict() for holiday in holidays],
        "flags": {
            "allow_patient_booking": config.ALLOW_PATIENT_BOOKING,
            "allow_doctor_status_updates": config.ALLOW_DOCTOR_STATUS_UPDATES,
            "data_source": config.DATA_SOURCE,
        },
    }
