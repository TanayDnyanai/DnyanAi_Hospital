"""One front door for the calendar and the receptionist chatbot.

Both existing apps stay intact. They are mounted under ``/calendar`` and
``/chat``, and ``portal/index.html`` lets users switch between them without
changing URLs or logging in twice.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
CALENDAR_DIR = REPO_ROOT / "calendar"
CHATBOT_DIR = REPO_ROOT / "chatbot"
PORTAL_DIR = Path(__file__).resolve().parent

# The calendar exposes its code as the `backend` package; the chatbot exposes
# `main`, `database`, etc. Both directories must be importable here.
for _entry in (str(CALENDAR_DIR), str(CHATBOT_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from backend.app import app as calendar_app  # noqa: E402
from main import app as chatbot_app  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Starlette does not run lifespan hooks of mounted sub-apps automatically.
    async with calendar_app.router.lifespan_context(calendar_app):
        async with chatbot_app.router.lifespan_context(chatbot_app):
            yield


app = FastAPI(title="DnyanAI Hospital", lifespan=lifespan)

app.mount("/calendar", calendar_app)
app.mount("/chat", chatbot_app)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(PORTAL_DIR / "index.html")


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "apps": ["calendar", "chatbot"]}
