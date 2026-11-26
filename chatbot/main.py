
import os

from env_config import load_app_env

# Injected deployment secrets must win over any stray local .env file.
load_app_env()
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

import database
import notifications
import scheduler
from graph import graph, store
from memory import close_memory
from patient_memory import get_patient_memory

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Explicit schema check at startup (idempotent); DDL uses the direct DSN.
    database.init_db()
    scheduler.start_scheduler()
    yield
    close_memory()
    # SQLite's legacy backend has no pool to close; Neon does.
    close_pool = getattr(database, "close_pools", None)
    if close_pool:
        close_pool()


app = FastAPI(
    title="Medical Receptionist API",
    version="0.2.0",
    description="LangGraph-powered AI Medical Hospital Receptionist",
    lifespan=lifespan,
)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    # Voice and text must not replay each other's turns. Default keeps older
    # callers working; the dashboard always sends the explicit channel.
    channel: Literal["voice", "text"] = "text"


class ChatResponse(BaseModel):
    reply: str


def _thread_id(session_id: str, channel: str) -> str:
    """Keep channel histories separate while sharing one patient profile."""
    return f"{session_id}:{channel}"


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:

    config = {
        "configurable": {
            "thread_id": _thread_id(request.session_id, request.channel),
            "user_id": request.user_id,
            "channel": request.channel,
        }
    }

    try:
        result = graph.invoke(
            {"messages": [HumanMessage(content=request.message)]},
            config=config,
        )

        reply = result["messages"][-1].content
        if not isinstance(reply, str):
            # A model may return structured content blocks; flatten to text.
            reply = " ".join(
                str(block.get("text", ""))
                for block in reply
                if isinstance(block, dict)
            ).strip()
        return ChatResponse(reply=reply)

    except Exception:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="The receptionist could not process that request. Please try again.",
        )


# ==========================================
# Staff / Administrative Endpoints
# ==========================================

class NoShowRequest(BaseModel):
    reason: str = Field(default="Patient No-Show (10 min grace elapsed)")


@app.post("/appointments/{appointment_id}/no-show")
def mark_appointment_no_show(appointment_id: int, payload: NoShowRequest = NoShowRequest()) -> dict:
    """Staff endpoint: Marks an appointment as No-Show after the 10-minute grace period, immediately freeing the slot."""
    result = database.mark_no_show(appointment_id, reason=payload.reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class CancelRequest(BaseModel):
    reason: str = Field(default="Cancelled by staff/admin")


@app.post("/appointments/{appointment_id}/cancel")
def staff_cancel_appointment(appointment_id: int, payload: CancelRequest = CancelRequest()) -> dict:
    """Staff endpoint: Cancels an appointment and immediately releases the slot for other patients."""
    result = database.cancel_appointment(appointment_id, reason=payload.reason)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ==========================================
# Frontend & Schedule Inspection Endpoints
# ==========================================

@app.get("/")
def serve_ui():
    """Serves the Medical Receptionist web dashboard."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "ok", "message": "Dashboard UI not found in static folder."}


@app.get("/api/schedule")
def get_schedule(date: str = None) -> dict:
    """Returns real-time 14-slot schedule grid for all doctors for the specified date."""
    return database.get_daily_schedule(date)


@app.get("/api/patient-memory/{user_id}")
def get_patient_memory_endpoint(user_id: str) -> dict:
    """Inspects what long-term memories the agent currently holds for a patient."""
    mem = get_patient_memory(store, user_id)
    return {"user_id": user_id, "memory": mem or {}}


# ==========================================
# Notification / WhatsApp (scaffolding)
# ==========================================

@app.get("/api/notifications/due")
def get_due_notifications(date: str | None = None) -> dict[str, Any]:
    """Return due reminder/check-in actions with wa.me links (no automated send in link mode).

    Computed from schedule data; durable sent/reply state to be stored in Neon later.
    """
    schedule = database.get_daily_schedule(date)
    actions = notifications.build_notification_actions(schedule)
    return {
        "date": schedule.get("date"),
        "timezone": notifications.TIMEZONE,
        "mode": notifications.WHATSAPP_MODE,
        "count": len(actions),
        "actions": actions,
    }


@app.get("/api/test-whatsapp")
def test_whatsapp(phone: str = None) -> dict[str, Any]:
    """Test WhatsApp integration by sending a test message."""
    target_phone = phone or os.getenv("WHATSAPP_TEST_NUMBER", "")
    if not target_phone:
        return {"success": False, "error": "No phone number provided or set in WHATSAPP_TEST_NUMBER."}
    result = notifications.send_whatsapp_message(
        target_phone,
        "Hello from DnyanAI Hospital! This is a test notification via WhatsApp."
    )
    return {"target_phone": target_phone, "result": result}


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> dict[str, Any]:
    """Inbound WhatsApp webhook supporting both Twilio (form-urlencoded) and Meta (JSON)."""
    content_type = request.headers.get("content-type", "")
    payload: dict[str, Any] = {}
    from_phone = ""
    reply_text = ""

    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload = dict(form)
        from_phone = str(payload.get("From", "")).replace("whatsapp:", "").replace("+", "").strip()
        reply_text = str(payload.get("Body", "")).strip()
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            reply_text = str(payload.get("text") or payload.get("body") or "")

    parsed = notifications.parse_inbound_webhook(payload if isinstance(payload, dict) else {})
    reply = notifications.interpret_reply(reply_text)

    # Find appointment_id from payload, or resolve from phone number (e.g. Twilio)
    appt_id = None
    try:
        if isinstance(payload, dict) and payload.get("appointment_id"):
            appt_id = int(payload["appointment_id"])
    except (TypeError, ValueError):
        appt_id = None

    if not appt_id and from_phone:
        active = database.get_patient_appointments(from_phone)
        if active:
            appt_id = active[-1]["id"]

    if appt_id is not None and reply == "coming":
        database.update_notification_status(appt_id, "confirmed_coming")
        notifications.send_whatsapp_message(
            from_phone,
            "Thank you for confirming! We look forward to seeing you at DnyanAI Hospital."
        )
        return {"received": True, "appointment_id": appt_id, "reply": reply, "action": "confirmed_coming"}

    if appt_id is not None and reply == "not_coming":
        res = database.reschedule_appointment(appt_id)
        if res.get("success"):
            appt = notifications.AppointmentInfo(
                appointment_id=res["new_appointment_id"],
                patient_name=res["patient_name"],
                patient_phone=from_phone,
                doctor_name=res["doctor_name"],
                date=res["date"],
                time_slot=res["time_slot"],
                status="Confirmed",
            )
            msg_result = notifications.send_booking_confirmation(appt)
            return {
                "received": True,
                "parsed": parsed,
                "reply": reply,
                "action": "rescheduled",
                "result": res,
                "message": msg_result,
            }
        if res.get("no_slot"):
            link_msg = notifications.portal_reschedule_message(
                notifications.AppointmentInfo(
                    appointment_id=appt_id,
                    patient_name="",
                    patient_phone=from_phone,
                    doctor_name="",
                    date="",
                    time_slot="",
                    status="Confirmed",
                )
            )
            msg_result = notifications.send_whatsapp_message(from_phone, link_msg)
            return {
                "received": True,
                "parsed": parsed,
                "reply": reply,
                "action": "portal_link",
                "result": res,
                "message": msg_result,
            }

    return {"received": True, "parsed": parsed, "reply": reply}
