"""Patient self-service lookups."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..deps import Actor, get_actor

router = APIRouter(prefix="/api", tags=["patients"])


@router.get("/patients/lookup")
def lookup_patient(
    phone: str = Query(min_length=4, max_length=20),
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    patient = source.lookup_patient(phone)
    if patient is None:
        raise HTTPException(status_code=404, detail="No patient found for that phone number.")
    return patient.to_dict()


@router.get("/patients/{phone}/appointments")
def patient_appointments(
    phone: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    patient = source.lookup_patient(phone)
    appointments = source.list_patient_appointments(phone)
    if patient is None and not appointments:
        raise HTTPException(status_code=404, detail="No patient found for that phone number.")
    return {
        "patient": patient.to_dict() if patient else None,
        "appointments": [appointment.to_dict() for appointment in appointments],
    }
