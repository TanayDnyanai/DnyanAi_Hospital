"""Medical calendar service.

Run with:  uvicorn backend.app:app --reload --port 8100
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .datasource.factory import get_data_source
from .domain import BookingError, DataSourceError
from .routers import admin, appointments, calendar, holidays, meta, patients
from .seed_holidays import seed_default_holidays

FRONTEND_DIR = config.BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        added = seed_default_holidays(get_data_source())
        if added:
            print(f"[calendar] seeded {added} holidays")
    except Exception as exc:  # pragma: no cover - never block startup
        print(f"[calendar] holiday seed skipped: {exc}")
    yield
    close = getattr(get_data_source(), "close", None)
    if close:
        close()


app = FastAPI(
    title="Medical Calendar API",
    version="0.1.0",
    description="Role-aware clinic calendar for DnyanAI Hospital.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_static_assets(request: Request, call_next):
    """Force browsers to revalidate static files after a deploy.

    Without this, a cached older api.js can be paired with a newer app.js and
    produce confusing "api.<method> is not a function" errors.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_STATUS_CODES = {
    "not_found": 404,
    "forbidden": 403,
    "patient_booking_disabled": 403,
    "unknown_doctor": 404,
    "duplicate_doctor": 409,
    "doctor_in_use": 409,
    "slot_taken": 409,
    "holiday": 409,
    "past_date": 409,
    "slot_ended": 409,
    "not_scheduled": 409,
    "invalid_slot": 409,
    "invalid_time": 400,
    "invalid_patient": 400,
    "invalid_reason": 400,
    "invalid_address": 400,
    "same_slot": 409,
    "inactive": 409,
    "db_busy": 503,
}


@app.exception_handler(BookingError)
async def booking_error_handler(request: Request, exc: BookingError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS_CODES.get(exc.code, 409),
        content={"detail": str(exc), "code": exc.code},
    )


@app.exception_handler(DataSourceError)
async def data_source_error_handler(
    request: Request, exc: DataSourceError
) -> JSONResponse:
    return JSONResponse(
        status_code=404 if exc.code == "not_found" else 500,
        content={"detail": str(exc), "code": exc.code},
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "data_source": config.DATA_SOURCE}


@app.get("/api", tags=["system"])
def api_index() -> dict:
    return {
        "name": "Medical Calendar API",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "/api/meta",
            "/api/calendar",
            "/api/day-grid",
            "/api/stats",
            "/api/appointments",
            "/api/patients/{phone}/appointments",
            "/api/holidays",
            "/api/doctor/leave",
            "/api/admin/doctors",
        ],
    }


app.include_router(meta.router)
app.include_router(calendar.router)
app.include_router(appointments.router)
app.include_router(patients.router)
app.include_router(holidays.router)
app.include_router(admin.router)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(FRONTEND_DIR) / "index.html")

    @app.get("/embed", include_in_schema=False)
    def embed_page() -> FileResponse:
        """Full-page app used inside the embed iframe."""
        return FileResponse(Path(FRONTEND_DIR) / "index.html")

    @app.get("/embed/widget.js", include_in_schema=False)
    def embed_widget() -> FileResponse:
        """One-line embed loader: <script src=".../embed/widget.js">."""
        return FileResponse(
            Path(FRONTEND_DIR) / "embed" / "widget.js",
            media_type="application/javascript",
        )
