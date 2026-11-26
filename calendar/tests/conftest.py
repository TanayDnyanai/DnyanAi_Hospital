"""Shared test fixtures.

Every test runs against a throwaway copy of ``clinic.db`` so the real
receptionist database is never modified.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.app import app
from backend.datasource.factory import reset_data_source, set_data_source
from backend.datasource.legacy import LegacyDataSource
from backend.seed_holidays import seed_default_holidays
from backend.services.calendar_service import today


@pytest.fixture()
def source(tmp_path):
    db_copy = tmp_path / "clinic.db"
    shutil.copy(config.CLINIC_DB_PATH, db_copy)
    data_source = LegacyDataSource(
        db_path=db_copy, receptionist_dir=config.RECEPTIONIST_DIR
    )
    seed_default_holidays(data_source)
    set_data_source(data_source)
    yield data_source
    reset_data_source()


@pytest.fixture()
def client(source):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def open_day(source):
    """A future Monday-Saturday date with no holiday."""
    cursor = today() + timedelta(days=10)
    for _ in range(60):
        if cursor.weekday() <= 5 and not source.list_holidays(cursor, cursor):
            return cursor
        cursor += timedelta(days=1)
    raise RuntimeError("No open day found in the next 60 days")


def headers(role: str, doctor_id: str | None = None) -> dict[str, str]:
    result = {"X-Role": role}
    if doctor_id:
        result["X-Doctor-Id"] = doctor_id
    return result
