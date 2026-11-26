"""Storage interface.

Every concrete data source must produce the domain objects in
``backend.domain``. Nothing above this layer knows about tables.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..domain import Appointment, Doctor, Holiday, Patient


class DataSource(ABC):
    """Read/write contract for the calendar."""

    # --- Reference data ---------------------------------------------------

    @abstractmethod
    def get_doctors(self) -> list[Doctor]:
        ...

    @abstractmethod
    def get_doctor(self, doctor_id: str) -> Doctor | None:
        ...

    @abstractmethod
    def get_clinic_info(self) -> dict[str, Any]:
        ...

    # --- Appointments -----------------------------------------------------

    @abstractmethod
    def list_events(
        self, start: date, end: date, doctor_id: str | None = None
    ) -> list[Appointment]:
        ...

    @abstractmethod
    def get_appointment(self, appointment_id: str) -> Appointment | None:
        ...

    @abstractmethod
    def get_day_grid(
        self, day: date, doctor_id: str | None = None
    ) -> dict[str, Any]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def reschedule_appointment(
        self,
        appointment_id: str,
        *,
        day: date,
        start_time: str,
        actor: str = "receptionist",
    ) -> Appointment:
        ...

    @abstractmethod
    def cancel_appointment(
        self, appointment_id: str, *, reason: str, actor: str = "receptionist"
    ) -> Appointment:
        ...

    @abstractmethod
    def mark_no_show(
        self, appointment_id: str, *, actor: str = "receptionist"
    ) -> Appointment:
        ...

    @abstractmethod
    def check_in(self, appointment_id: str, *, actor: str = "receptionist") -> Appointment:
        ...

    # --- Patients ---------------------------------------------------------

    @abstractmethod
    def lookup_patient(self, phone: str) -> Patient | None:
        ...

    @abstractmethod
    def list_patient_appointments(self, phone: str) -> list[Appointment]:
        ...

    # --- Holidays ---------------------------------------------------------

    @abstractmethod
    def list_holidays(self, start: date, end: date) -> list[Holiday]:
        ...

    @abstractmethod
    def get_holiday(self, holiday_id: str) -> Holiday | None:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def delete_holiday(self, holiday_id: str, *, actor: str = "admin") -> bool:
        ...

    # --- Admin ------------------------------------------------------------

    @abstractmethod
    def create_doctor(
        self,
        *,
        full_name: str,
        specialization: str,
        available_days: str,
        actor: str = "admin",
    ) -> Doctor:
        ...

    @abstractmethod
    def update_doctor(
        self,
        doctor_id: str,
        *,
        specialization: str | None = None,
        available_days: str | None = None,
        actor: str = "admin",
    ) -> Doctor:
        ...

    @abstractmethod
    def delete_doctor(self, doctor_id: str, *, actor: str = "admin") -> bool:
        ...

    @abstractmethod
    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        ...
