import re
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from clinic import CLINIC_INFO
import database


# Server-side guard rails for destructive actions. The prompt still drives the
# natural conversation, but the database is never changed unless these checks
# pass in code as well.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm|confirmed|correct|right|go ahead|do it|please do|sure|okay|ok)\b",
    re.IGNORECASE,
)


def _norm_phone(value) -> str:
    """Compare phone numbers ignoring country code and formatting."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:]


def _user_confirmed(runtime: ToolRuntime, appointment_id: int) -> tuple[bool, str]:
    """Require an explicit affirmative after a cancellation was offered.

    This keeps the user-facing flow unchanged (agent asks, patient says yes)
    while preventing a model hallucination or a stale memory entry from
    cancelling an appointment on its own.
    """
    messages = (getattr(runtime, "state", None) or {}).get("messages", []) or []
    last_human = None
    prior_agent_turns = []
    for message in messages:
        if isinstance(message, HumanMessage):
            last_human = message
        elif isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            prior_agent_turns.append(str(message.content or "").lower())

    if last_human is None:
        return False, "no user message"
    current = str(last_human.content or "")
    if not _CONFIRM_RE.search(current):
        return False, "the patient has not said yes yet"

    # Either the agent just offered this cancellation, or the patient named
    # the appointment themselves together with an affirmative.
    for prior in prior_agent_turns:
        if "cancel" in prior and (
            "?" in prior
            or "confirm" in prior
            or f"#{appointment_id}" in prior
            or str(appointment_id) in prior
        ):
            return True, ""
    if "cancel" in current.lower() and (
        f"#{appointment_id}" in current or str(appointment_id) in current
    ):
        return True, ""
    return False, "the cancellation was not confirmed for this appointment"


@tool
def get_clinic_info(
    topic: Literal["name", "address", "phone", "opening_hours", "services"],
) -> str:
    """Retrieve verified information about DnyanAI Hospital, such as name, address, phone number, opening hours, or available services."""
    if topic in CLINIC_INFO:
        data = CLINIC_INFO[topic]
        if isinstance(data, list):
            return f"{topic.replace('_', ' ').capitalize()}: {', '.join(data)}"
        return f"{topic.replace('_', ' ').capitalize()}: {data}"

    return f"Sorry, information for '{topic}' is not available."


@tool
def list_doctors() -> str:
    """List all available doctors at DnyanAI Hospital,
     their specialties, and working days."""
    doctors =  database.get_doctors()
    if not doctors:
        return "No doctors currently found in our clinic"
    lines = ["Available Doctors at DnyanAI Hospital:"]
    for doc in doctors:
        lines.append(
            f"-{doc['name']} ({doc['specialty']} | Days:{doc['available_days']})"
        )
    return "\n".join(lines)

@tool
def check_doctor_availability(doctor_name: str, date: str) -> str:
    """Check remaining open appointment slots for a doctor on 
    a specific date (format: YYYY-MM-DD)."""
    slots = database.get_available_slots(doctor_name, date)
    if not slots:
        return f"No open slots for {doctor_name} on {date}. The doctor may be fully booked or off duty"
    return f"Available slots for {doctor_name} on {date}: {', ' .join(slots)}"


import notifications
from notifications import AppointmentInfo


@tool
def book_appointment(
    doctor_name: str,
    patient_name: str,
    patient_contact: str,
    date: str,
    time_slot: str,
) -> str:
    """Book an appointment using patient_contact as the phone number.
    This wrapper aligns the tool signature with the LLM's expected argument name.
    """
    # Forward to the underlying database function, mapping contact to phone.
    result = database.book_appointment(
        patient_name=patient_name,
        patient_phone=patient_contact,
        doctor_name=doctor_name,
        date=date,
        time_slot=time_slot,
    )
    if result["success"]:
        appt = AppointmentInfo(
            appointment_id=result["appointment_id"],
            patient_name=result["patient_name"],
            patient_phone=patient_contact.strip(),
            doctor_name=result["doctor_name"],
            date=result["date"],
            time_slot=result["time_slot"],
            status="Confirmed",
        )
        msg_result = notifications.send_booking_confirmation(appt)
        database.update_notification_status(result["appointment_id"], "sent")
        msg_info = ""
        if msg_result.get("link"):
            msg_info = f" Confirmation message link: {msg_result['link']}"
        elif msg_result.get("success"):
            msg_info = " Confirmation message sent."
        else:
            msg_info = f" (WhatsApp send failed: {msg_result.get('error')})"
        return (
            f"Appointment confirmed successfully! "
            f"Appointment ID: #{result['appointment_id']}, "
            f"Patient: {result['patient_name']}, "
            f"Doctor: {result['doctor_name']}, "
            f"Date: {result['date']}, "
            f"Time: {result['time_slot']}."
            f"{msg_info}"
        )
    else:
        return f"Booking failed: {result['error']}"


@tool
def book_urgent_appointment(
    patient_name: str,
    patient_contact: str,
) -> str:
    """Book the earliest available appointment slot TODAY for an urgent or emergency patient with our General Physician (Dr. Rajesh Sharma).
    Use this when an emergency patient confirms they want an urgent slot immediately.
    """
    result = database.book_urgent_appointment(
        patient_name=patient_name,
        patient_phone=patient_contact,
        doctor_name="Dr. Rajesh Sharma",
    )
    if result["success"]:
        appt = AppointmentInfo(
            appointment_id=result["appointment_id"],
            patient_name=result["patient_name"],
            patient_phone=patient_contact.strip(),
            doctor_name=result["doctor_name"],
            date=result["date"],
            time_slot=result["time_slot"],
            status="Confirmed",
        )
        msg_result = notifications.send_booking_confirmation(appt)
        database.update_notification_status(result["appointment_id"], "sent")
        msg_info = ""
        if msg_result.get("link"):
            msg_info = f" Confirmation message link: {msg_result['link']}"
        elif msg_result.get("success"):
            msg_info = " Confirmation message sent."
        else:
            msg_info = f" (WhatsApp send failed: {msg_result.get('error')})"
        return (
            f"URGENT Appointment confirmed successfully! "
            f"Appointment ID: #{result['appointment_id']} (EMERGENCY PRIORITY), "
            f"Patient: {result['patient_name']}, "
            f"Doctor: {result['doctor_name']}, "
            f"Date: {result['date']}, "
            f"Time: {result['time_slot']}."
            f"{msg_info}"
        )
    else:
        return f"Urgent booking could not be completed: {result['error']}"


@tool
def get_patient_appointments(patient_phone: str) -> str:
    """Retrieve all active confirmed appointments for a patient using their phone number."""
    appointments = database.get_patient_appointments(patient_phone)
    if not appointments:
        return f"No active appointments found for phone number {patient_phone}."

    lines = [f"Active appointments for {patient_phone}:"]
    for appt in appointments:
        lines.append(
            f"- Appointment #{appt['id']}: with {appt['doctor_name']} on {appt['date']} at {appt['time_slot']}"
        )
    return "\n".join(lines)


@tool
def get_patient_profile(user_id: str) -> str:
    """Retrieve the stored patient profile (name, phone, preferences, medical history summary) for a user_id."""
    profile = database.get_patient_profile_by_user_id(user_id)
    if not profile:
        return f"No stored patient profile found for user_id '{user_id}'."
    name = profile.get("patient_name") or "unknown"
    phone = profile.get("patient_phone") or "unknown"
    doctor = profile.get("preferred_doctor") or "unknown"
    history = profile.get("medical_history", {}) if isinstance(profile, dict) else {}
    summary = profile.get("conversation_summary") or "none"
    return (
        f"Stored profile for {user_id}: name={name}, phone={phone}, preferred_doctor={doctor}, "
        f"conditions={history.get('conditions', [])}, allergies={history.get('allergies', [])}, "
        f"reported_symptoms={history.get('reported_symptoms', [])}, summary={summary}"
    )


@tool
def get_patient_appointments_by_user(user_id: str) -> str:
    """Retrieve active confirmed appointments for a patient using their user_id (resolves saved phone automatically)."""
    result = database.get_patient_appointments_by_user_id(user_id)
    if not result.get("success"):
        err = result.get("error", "Could not retrieve appointments.")
        name = result.get("patient_name")
        if name:
            return f"{err} Known patient name: {name}."
        return err

    appointments = result.get("appointments", [])
    phone = result.get("patient_phone")
    name = result.get("patient_name") or "Patient"
    if not appointments:
        return f"No active appointments found for {name} (phone {phone}, user_id {user_id})."

    lines = [f"Active appointments for {name} (phone {phone}, user_id {user_id}):"]
    for appt in appointments:
        lines.append(
            f"- Appointment #{appt['id']}: with {appt['doctor_name']} on {appt['date']} at {appt['time_slot']}"
        )
    return "\n".join(lines)


@tool
def cancel_appointment(appointment_id: int, runtime: ToolRuntime) -> str:
    """Cancel an existing clinic appointment using its unique appointment ID.

    Only appointments belonging to the current patient can be cancelled, and
    only after the patient has explicitly confirmed that exact appointment.
    """
    config = getattr(runtime, "config", None) or {}
    user_id = (config.get("configurable") or {}).get("user_id")
    if not user_id:
        return "Cancellation blocked: this chat has no patient session."

    profile = database.get_patient_profile_by_user_id(user_id) or {}
    phone = profile.get("patient_phone")
    if not phone:
        return (
            "Cancellation blocked: I do not have a confirmed phone number for "
            "this patient yet, so I cannot safely identify their appointments."
        )

    appointment = database.get_appointment_by_id(appointment_id)
    if not appointment:
        return f"Appointment #{appointment_id} was not found."

    if _norm_phone(appointment.get("patient_phone")) != _norm_phone(phone):
        return (
            "Cancellation blocked: that appointment does not belong to the "
            "current patient. I have not changed anything."
        )

    confirmed, reason = _user_confirmed(runtime, appointment_id)
    if not confirmed:
        return (
            f"I found appointment #{appointment_id} with {appointment['doctor_name']} "
            f"on {appointment['date']} at {appointment['time_slot']}. "
            f"Ask the patient to reply YES to confirm before cancelling ({reason}). "
            "Nothing has been changed yet."
        )

    result = database.cancel_appointment(appointment_id)
    if result["success"]:
        return (
            f"Appointment #{result['appointment_id']} for {result['patient_name']} "
            f"with {result['doctor_name']} on {result['date']} at {result['time_slot']} "
            f"has been successfully cancelled. The time slot is now open."
        )
    return f"Cancellation failed: {result['error']}"


# Export list of all tools for the agent
tools = [
    get_clinic_info,
    list_doctors,
    check_doctor_availability,
    book_appointment,
    book_urgent_appointment,
    get_patient_profile,
    get_patient_appointments,
    get_patient_appointments_by_user,
    cancel_appointment,
]

