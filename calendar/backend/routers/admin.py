"""Admin endpoints: doctors, audit log."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..datasource.base import DataSource
from ..datasource.factory import get_data_source
from ..deps import Actor, get_actor, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


class DoctorCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    specialization: str = Field(min_length=2, max_length=120)
    available_days: str = Field(default="Monday to Saturday", max_length=200)


class DoctorUpdate(BaseModel):
    specialization: str | None = Field(default=None, max_length=120)
    available_days: str | None = Field(default=None, max_length=200)


@router.get("/doctors")
def list_doctors(
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    return {"doctors": [doctor.to_dict() for doctor in source.get_doctors()]}


@router.post("/doctors", status_code=201)
def create_doctor(
    payload: DoctorCreate,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    doctor = source.create_doctor(
        full_name=payload.full_name,
        specialization=payload.specialization,
        available_days=payload.available_days,
        actor=actor.label,
    )
    return doctor.to_dict()


@router.patch("/doctors/{doctor_id}")
def update_doctor(
    doctor_id: str,
    payload: DoctorUpdate,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    if payload.specialization is None and payload.available_days is None:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    doctor = source.update_doctor(
        doctor_id,
        specialization=payload.specialization,
        available_days=payload.available_days,
        actor=actor.label,
    )
    return doctor.to_dict()


@router.delete("/doctors/{doctor_id}")
def delete_doctor(
    doctor_id: str,
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    source.delete_doctor(doctor_id, actor=actor.label)
    return {"deleted": True, "id": doctor_id}


@router.get("/audit")
def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(get_actor),
    source: DataSource = Depends(get_data_source),
) -> dict:
    require_admin(actor)
    return {"entries": source.list_audit(limit)}
