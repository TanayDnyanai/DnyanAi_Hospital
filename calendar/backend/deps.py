"""Request-scoped role handling.

The demo role switcher sends ``X-Role`` (and ``X-Doctor-Id`` for the doctor
role). Real authentication replaces this module later; everything else
already treats the actor as an opaque identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from . import config


@dataclass(slots=True)
class Actor:
    role: str
    doctor_id: str | None = None
    name: str = ""

    @property
    def is_staff(self) -> bool:
        return self.role in config.STAFF_ROLES

    @property
    def is_booking_manager(self) -> bool:
        """Patients and staff can both manage appointments."""
        return self.role in (*config.STAFF_ROLES, "patient")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.name or self.role.capitalize()


def get_actor(
    x_role: str | None = Header(default=None),
    x_doctor_id: str | None = Header(default=None),
    x_actor_name: str | None = Header(default=None),
) -> Actor:
    role = (x_role or "patient").strip().lower()
    if role not in config.ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown role '{role}'. Expected one of {', '.join(config.ROLES)}.",
        )
    if role == "doctor" and not x_doctor_id:
        raise HTTPException(
            status_code=400,
            detail="The doctor role requires the X-Doctor-Id header.",
        )
    return Actor(
        role=role,
        doctor_id=x_doctor_id if role == "doctor" else None,
        name=(x_actor_name or "").strip(),
    )


def require_staff(actor: Actor) -> None:
    if not actor.is_staff:
        raise HTTPException(
            status_code=403,
            detail="Only receptionists and admins can perform this action.",
        )


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only admins can perform this action.",
        )
