"""Configuration for the medical calendar service.

Everything environment-specific lives here so the rest of the codebase
can stay storage-agnostic.
"""

from __future__ import annotations

import os
from datetime import time, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # calendar/
WORKSPACE_DIR = BASE_DIR.parent  # repo root (main/)
OUTER_WORKSPACE = WORKSPACE_DIR.parent  # local workspace holding .env.local

try:  # .env files are optional
    from dotenv import load_dotenv

    # App-local config wins; Neon writes the connection strings to the
    # workspace-level .env.local when `neon deploy` / `neon env pull` runs.
    load_dotenv(OUTER_WORKSPACE / ".env.local", override=False)
    load_dotenv(OUTER_WORKSPACE / ".env", override=False)
    load_dotenv(BASE_DIR / ".env", override=False)
    load_dotenv(WORKSPACE_DIR / ".env.local", override=False)
    load_dotenv(WORKSPACE_DIR / ".env", override=False)
except Exception:  # pragma: no cover - dotenv is a soft dependency
    pass


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (BASE_DIR / candidate).resolve()
    return candidate


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# --- Storage -------------------------------------------------------------

DATA_SOURCE = os.getenv("DATA_SOURCE", "legacy").strip().lower()

RECEPTIONIST_DIR = _path(
    os.getenv("RECEPTIONIST_DIR"),
    WORKSPACE_DIR / "chatbot",
)
CLINIC_DB_PATH = _path(
    os.getenv("CLINIC_DB_PATH"),
    RECEPTIONIST_DIR / "clinic.db",
)
# Only used when DATA_SOURCE=production (the reference adapter uses SQLite).
PRODUCTION_DB_PATH = _path(
    os.getenv("PRODUCTION_DB_PATH"),
    BASE_DIR / "production.db",
)
# Written to the workspace .env.local by `neon deploy`. The pooled URL serves
# app traffic; the unpooled URL is used for DDL/schema setup.
NEON_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
NEON_DATABASE_URL_UNPOOLED = os.getenv("DATABASE_URL_UNPOOLED", "").strip()

# --- Scheduling ----------------------------------------------------------

SLOT_MINUTES = _int(os.getenv("CLINIC_SLOT_MINUTES"), 30)

# Morning and afternoon consultation windows (end is exclusive of the next
# slot start, i.e. 09:00-13:00 yields the 12:30 slot).
CLINIC_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(13, 0)),
    (time(14, 0), time(17, 0)),
)

# Monday=0 ... Sunday=6. Legacy clinics run Monday to Saturday.
CLINIC_OPEN_WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5)

_OFFSET_MINUTES = _int(os.getenv("CLINIC_TIMEZONE_OFFSET_MINUTES"), 330)
CLINIC_TZ = timezone(timedelta(minutes=_OFFSET_MINUTES), "IST")
CLINIC_TIMEZONE_LABEL = "Asia/Kolkata"

# --- Feature flags -------------------------------------------------------

ALLOW_PATIENT_BOOKING = _bool(os.getenv("ALLOW_PATIENT_BOOKING"), True)
ALLOW_DOCTOR_STATUS_UPDATES = _bool(os.getenv("ALLOW_DOCTOR_STATUS_UPDATES"), True)

# --- Server --------------------------------------------------------------

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = _int(os.getenv("APP_PORT"), 8100)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

ROLES = ("patient", "receptionist", "doctor", "admin")
STAFF_ROLES = ("receptionist", "admin")

DOCTOR_COLORS = (
    "#1a73e8",
    "#d93025",
    "#188038",
    "#f9ab00",
    "#9334e6",
    "#e8710a",
    "#12a4af",
    "#c5221f",
    "#7cb342",
    "#5f6368",
    "#a142f4",
    "#f29900",
)
