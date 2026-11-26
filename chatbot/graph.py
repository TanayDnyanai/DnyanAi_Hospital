import os
import re
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

from langchain_core.messages import SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.store.base import BaseStore
from langchain_core.runnables import RunnableConfig


# Import specialized tools
from tools import (
    get_clinic_info,
    list_doctors,
    check_doctor_availability,
    book_appointment,
    book_urgent_appointment,
    get_patient_profile,
    get_patient_appointments,
    get_patient_appointments_by_user,
    cancel_appointment,
)
from patient_memory import (
    get_patient_context,
    update_patient_memory
)

# Load environment variables without overriding injected deployment secrets.
from env_config import load_app_env

load_app_env()


def _channel_instruction(config: RunnableConfig) -> str:
    """Append a channel-specific response style to a worker prompt."""
    channel = (config.get("configurable") or {}).get("channel", "text")
    if channel == "voice":
        return (
            "\n\nCHANNEL: VOICE. Reply in at most two short spoken sentences. "
            "Plain words only: no markdown, bullet points, links or raw appointment IDs. "
            "Read back a full phone number only when the patient is confirming it."
        )
    return (
        "\n\nCHANNEL: TEXT. Short paragraphs are fine; you may include appointment "
        "IDs and confirmation links where useful. Avoid unnecessary detail."
    )

from memory import get_checkpointer, get_store

# ==========================================
# PART 1: State Definition & Tool Grouping
# ==========================================

class AgentState(MessagesState, total=False):
    """Conversation messages + supervisor routing decision."""
    next_worker: str
    memory_updates: dict


# Group tools by specialist responsibility
triage_tools = [
    list_doctors,
    book_urgent_appointment,
]

clinic_info_tools = [
    get_clinic_info,
]

scheduling_tools = [
    list_doctors,
    check_doctor_availability,
    book_appointment,
    book_urgent_appointment,
    get_patient_profile,
    get_patient_appointments,
    get_patient_appointments_by_user,
    cancel_appointment,
]

# Initialize Groq LLM
model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

llm = ChatGroq(
    model=model_name,
    temperature=0.2,
)

# ==========================================
# PART 2: Supervisor / Router Definition
# ==========================================

class RouterDecision(BaseModel):
    """Structured decision returned by the supervisor."""

    next_worker: Literal[
        "triage",
        "scheduling",
        "clinic_info",
        "general",
    ] = Field(
        description="The specialist best suited to handle this user message."
    )

    reason: str = Field(
        description="Brief explanation of why this specialist was chosen."
    )


SUPERVISOR_SYSTEM_PROMPT = """You are the Front Desk Manager at DnyanAI Hospital.

Your sole responsibility is to analyze the conversation and route
the patient to the single best specialist.

ROUTING CRITERIA:

1. 'triage': The patient mentions physical symptoms, illness, pain,
   feeling sick, or asks which doctor can treat their condition.

2. 'scheduling': The patient wants to check appointment availability,
   book a visit (routine or urgent/emergency), cancel an appointment, look up existing appointments,
   or asks to list/know available doctors, specialists, or doctor names.

3. 'clinic_info': The patient asks about hospital address, location,
   opening hours, phone number, or general services.

4. 'general': Greetings, pleasantries, thanks, or goodbyes.

Always select the most specific specialist for the user's latest message.
"""


router_llm = llm.with_structured_output(RouterDecision)


def supervisor_node(state: AgentState) -> dict:
    """Inspect conversation and choose the next specialist."""

    messages = [
        SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT)
    ] + state["messages"]

    try:
        decision: RouterDecision = router_llm.invoke(messages)
        worker = decision.next_worker

    except Exception:
        # Fallback to scheduling if routing fails, as scheduling handles appointments
        worker = "scheduling"

    return {"next_worker": worker}


# ==========================================
# PART 3: Specialist Prompts & Worker Nodes
# ==========================================

TRIAGE_PROMPT = """Role: Hospital Intake & Triage Specialist at DnyanAI Hospital. Date: {today}.

DOCTORS (guide; use 'list_doctors' to verify current availability/days):
- General Physicians: Dr. Rajesh Sharma, Dr. Meera Kulkarni, Dr. Amit Deshmukh — fever, cough, cold, headache, dizziness, fatigue, body aches, stomach pain, infections, checkups, general concerns.
- Dr. Priya Patel (Pediatrician): Children, infants, pediatric care.
- Dr. Ananya Iyer (Dermatologist): Rashes, acne, itching, skin issues, allergies, hair/scalp.
- Dr. Suresh Nair (Cardiologist): Heart/chest-related concerns (non-emergency evaluation), palpitations, BP follow-ups.
- Dr. Kavita Joshi (Orthopedic Surgeon): Bone, joint, muscle, back pain, injuries, sprains.
- Dr. Rohan Mehta (ENT Specialist): Ear, nose, throat, sinus, hearing issues.
- Dr. Neha Kapoor (Gynecologist): Women’s health, menstrual, pregnancy-related consultations.
- Dr. Vikram Rao (Neurologist): Neurological concerns, migraines, numbness, seizures follow-up.
- Dr. Sanjay Gupta (Psychiatrist): Mental health, anxiety, depression, stress, sleep concerns.
- Dr. Pooja Bansal (Ophthalmologist): Eye/vision issues, irritation, eye checkups.

VOICE RESPONSE STYLE:
- Max 2-3 short, spoken sentences. Direct, empathetic, and urgent.
- No medical diagnoses or prescriptions.

CORE INSTRUCTIONS:
1. EMERGENCY (Severe chest pain, breathing difficulty, paralysis, slurred speech, heavy bleeding, unconsciousness):
   - Direct to call 112/local ER instantly (safety guidance comes first; do not delay this for details).
   - Offer urgent slot: "If you're safe and need an immediate visit today, I can book an urgent slot with Dr. Rajesh Sharma. Should I book that?"
   - Call 'book_urgent_appointment' ONLY if name and complete 10-digit phone number are known from PATIENT PROFILE/CONTEXT or provided and confirmed in this conversation. If missing, ask for the missing detail(s) only.
2. ROUTINE INTAKE:
   - Call 'list_doctors' if doctor info needs live verification or the patient asks which doctors/specialists are available.
   - Match symptoms to the most suitable doctor/specialist and ask immediately to book.
   - Never claim doctor names are unavailable without calling 'list_doctors'.
3. Use PATIENT PROFILE/CONTEXT when available. Do not ask again for name or phone if already known.
4. PATIENT IDENTITY (voice-safe): Phone number is the primary identifier. Use/request the complete 10-digit phone number only; never ask for or accept last few/partial digits. When a phone number is provided, read it back clearly and ask the user to confirm it is correct before booking or using it for records. If it differs from the number on file, do not replace the saved number automatically—confirm which complete 10-digit number is correct. If the user provides partial digits, ask for the full 10-digit number.

Example:
"I'm sorry you're dealing with that headache and dizziness. Dr. Sharma, our General Physician, can evaluate that today. Would you like me to check available slots?"
"""


SCHEDULING_PROMPT = """You are the Appointment Scheduling Specialist
at DnyanAI Hospital.

Today's date is {today}.
Current user_id: {user_id}

YOUR OPERATIONAL PROTOCOL:

1. Use PATIENT PROFILE/CONTEXT and Current user_id when available.
   - If patient name or complete 10-digit phone number is already known, do not ask for it again; reuse it.
   - To look up this patient's profile or active appointments, prefer 'get_patient_profile' and 'get_patient_appointments_by_user' with Current user_id.
   - If those tools show phone is missing, ask once for the complete 10-digit phone number, then use 'get_patient_appointments' if needed.
   - Patient identification uses the complete 10-digit phone number only. Never ask for or accept last few/partial digits. If the user provides partial digits, ask for the full 10-digit number.
   - Voice-safe verification: when a phone number is provided (or before booking/lookup with a number not already confirmed), read it back clearly and ask the user to confirm it is correct. If the provided number differs from the known number on file, do not replace the saved number automatically; confirm which complete 10-digit number is correct and proceed only with the confirmed number.
   - Do not skip required patient details (name, complete 10-digit phone number) for booking, urgent booking, lookup, or cancellation.

2. Urgent / Emergency Bookings:
   If the patient is in an urgent situation or confirms they want the urgent appointment offered by triage:
   - Use 'book_urgent_appointment' with the patient's name and complete 10-digit phone number.
   - You only need their name and complete 10-digit phone number (do NOT ask for doctor, date, or time slot, as this tool automatically reserves the earliest slot today with Dr. Rajesh Sharma).
   - If name or complete 10-digit phone number is missing and not in context, ask for the missing detail(s) only.
   - Once confirmed, provide the Appointment ID, date, and time slot, and remind them that if symptoms worsen, they must seek emergency services at 112 immediately.

3. Checking Availability:
   When a patient asks for a doctor or date, use
   'check_doctor_availability' with date format YYYY-MM-DD.

4. Booking:
   Collect patient name, complete 10-digit phone number, doctor name, date,
   and time slot.

   Once the patient confirms, call 'book_appointment'.

   Confirm the appointment using the returned Appointment ID.

5. Cancelling:
   Use known identity/context and Current user_id to find the right appointment first (prefer 'get_patient_appointments_by_user').
   Ask for the complete 10-digit phone number only if it cannot be resolved from tools/context.
   Never ask for last few/partial digits.
   Read back the appointment details and ask "Should I cancel this? Please reply yes."
   Call 'cancel_appointment' ONLY after the patient replies with an explicit yes.
   If the tool says the cancellation is blocked, repeat its question to the patient
   and do NOT call it again until they answer.

6. Doctor Directory:
   - If the patient asks about clinic doctors, available doctors, specialists, or doctor names, ALWAYS call 'list_doctors' and answer from its result.
   - Never say doctor names are not listed/available in the system without calling 'list_doctors'.

7. Keep answers concise, clear, and professional.

8. Never assume the patient wants an appointment booked
   merely because a doctor was recommended.
"""


CLINIC_INFO_PROMPT = """Role: Hospital Information Specialist at DnyanAI Hospital.

VOICE STYLE:
- Direct, clear, and professional. 1-2 short sentences max for rapid TTS response.

INSTRUCTIONS:
1. Retrieval: Always call 'get_clinic_info' to answer questions about address, hours, phone, location, or available services.
2. Accuracy: Never guess or fabricate information. If data is missing from tool output, state it concisely.

Example:
"DnyanAI Hospital is open Monday through Saturday from 8 AM to 8 PM. Is there a specific service or doctor schedule you'd like to check?"
"""

GENERAL_PROMPT = """Role: Front Desk Greeter at DnyanAI Hospital.

VOICE STYLE:
- Warm, professional, and crisp. 1-2 short sentences max.
- Immediate response—no unnecessary preamble.

INSTRUCTIONS:
1. Greet: Welcome the caller warmly to DnyanAI Hospital.
2. Use PATIENT CONTEXT if available: if the patient's name is known, greet them by name. Do not ask for name or phone again if already known.
3. If the user asks to confirm identity or access records and details are not known, request the complete 10-digit phone number (and name if needed). Never ask for or accept last few/partial digits. When a number is provided, read it back clearly and ask for confirmation (voice-safe).
4. Prompt: Directly ask how you can help (e.g., booking an appointment, seeing a doctor, or general clinic info).

Example:
"Welcome to DnyanAI Hospital. How may I assist you today?"
"""


# ==========================================
# Model bindings per specialist
# ==========================================

triage_llm = llm.bind_tools(triage_tools)

scheduling_llm = llm.bind_tools(scheduling_tools)

clinic_info_llm = llm.bind_tools(clinic_info_tools)

general_llm = llm.bind_tools([])


# ==========================================
# Worker Nodes
# ==========================================

def triage_node(state: AgentState, config:RunnableConfig,* , store: BaseStore) -> dict:
    """Evaluate symptoms and recommend an appropriate doctor."""

    today_str = datetime.now().strftime("%Y-%m-%d (%A)")

    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        raise ValueError("user_id is required")

    patient_context = get_patient_context(
        store,
        user_id,
        category="all"
    )
    prompt = f"""{TRIAGE_PROMPT.format(today=today_str)}

Current user_id: {user_id}
PATIENT PROFILE/CONTEXT:
{patient_context}
"""
    prompt += _channel_instruction(config)

    messages = [
        SystemMessage(content=prompt)
    ] + state["messages"]

    response = triage_llm.invoke(messages)

    # Track reported symptoms into memory_updates so they persist across sessions
    last_user_msg = next((m for m in reversed(state["messages"]) if getattr(m, "type", "") == "human"), None)
    updates = {}
    if last_user_msg is not None:
        text = str(getattr(last_user_msg, "content", "")).strip()
        lower = text.lower()
        if 3 < len(text) <= 140:
            skip_keywords = (
                "book", "appointment", "cancel", "resched", "slot", "available", "schedule",
                "hours", "address", "location", "phone number", "contact", "my number is",
                "my phone", "thank", "thanks", "hello", "hi", "bye", "good morning",
            )
            symptom_hints = (
                "pain", "ache", "fever", "cough", "cold", "rash", "itch", "dizz", "nause",
                "vomit", "bleed", "swell", "allerg", "headache", "fatigue", "tired",
                "breath", "injur", "hurt", "sore", "infection", "burn", "acne", "skin",
            )
            if not any(k in lower for k in skip_keywords) and (any(k in lower for k in symptom_hints) or len(text.split()) >= 3):
                updates = {
                    "medical_history": {
                        "reported_symptoms": [text[:100]]
                    }
                }

    return {"messages": [response], "memory_updates": updates}


def scheduling_node(state: AgentState, config:RunnableConfig,* , store: BaseStore) -> dict:
    """Manage appointments and database operations."""

    today_str = datetime.now().strftime("%Y-%m-%d (%A)")

    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        raise ValueError("user_id is required")

    patient_context = get_patient_context(
        store,
        user_id,
        category="all"
    )

    prompt = f"""
    {SCHEDULING_PROMPT.format(today=today_str, user_id=user_id)}

    PATIENT PROFILE/CONTEXT:
    {patient_context}
    """
    prompt += _channel_instruction(config)

    messages = [
        SystemMessage(content=prompt)
    ] + state["messages"]

    response = scheduling_llm.invoke(messages)

    return {"messages": [response]}


def clinic_info_node(state: AgentState, config: RunnableConfig) -> dict:
    """Answer clinic FAQs using clinic tools."""

    messages = [
        SystemMessage(content=CLINIC_INFO_PROMPT + _channel_instruction(config))
    ] + state["messages"]

    response = clinic_info_llm.invoke(messages)

    return {"messages": [response]}


def general_node(
    state: AgentState,
    config: RunnableConfig,
    *,
    store: BaseStore
) -> dict:
    """Handle greetings and general conversation using long-term memory."""

    # 1. Extract patient ID from config
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        raise ValueError("user_id is required")

    patient_context = get_patient_context(
        store,
        user_id,
        category="all"
    )
    prompt = f"""{GENERAL_PROMPT}

Current user_id: {user_id}
PATIENT PROFILE/CONTEXT:
{patient_context}
"""
    prompt += _channel_instruction(config)

    messages = [
        SystemMessage(content=prompt)
    ] + state["messages"]

    # 5. Generate response
    response = general_llm.invoke(messages)

    return {"messages": [response]}


def _current_turn_messages(state: AgentState) -> list:
    """Only the latest user message and everything produced in response to it.

    Booking/cancellation notes must come from this turn. Scanning the whole
    thread re-imports old confirmations on every later message, which made the
    assistant keep "reading its own messages" from previous conversations.
    """
    messages = state.get("messages", []) or []
    last_human = max(
        (i for i, msg in enumerate(messages) if getattr(msg, "type", "") == "human"),
        default=-1,
    )
    return messages[last_human:] if last_human >= 0 else []


def memory_update_node(
    state: AgentState,
    config: RunnableConfig,
    *,
    store: BaseStore
) -> dict:
    """Save explicitly validated patient memory updates."""

    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return {"memory_updates": {}}

    updates = dict(state.get("memory_updates") or {})
    existing_profile = None
    try:
        from patient_memory import get_patient_memory
        existing_profile = get_patient_memory(store, user_id) or {}
    except Exception:
        existing_profile = {}

    today_str = datetime.now().strftime("%Y-%m-%d")
    summary_events = []
    existing_history = existing_profile.get("medical_history", {}) if isinstance(existing_profile, dict) else {}

    # Auto-extract patient identity info from user messages
    for msg in state.get("messages", []):
        if getattr(msg, "type", "") != "human":
            continue
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue

        if not existing_profile.get("patient_name") and "patient_name" not in updates:
            # Prefer explicit name statements from user messages
            for m in state.get("messages", []):
                if getattr(m, "type", "") != "human":
                    continue
                mcontent = getattr(m, "content", "")
                if not isinstance(mcontent, str):
                    continue

                name_match = re.search(
                    r"\b(?:my name is|name is)\s+([A-Za-z][A-Za-z]+(?:\s+[A-Za-z][A-Za-z]+){0,2})",
                    mcontent,
                    re.IGNORECASE,
                )
                candidate = name_match.group(1).strip(" .,!?:;") if name_match else None

                if not candidate:
                    m2 = re.search(
                        r"\b(?:i am|i'm|im|this is)\s+([A-Za-z][A-Za-z]+(?:\s+[A-Za-z][A-Za-z]+){0,2})",
                        mcontent,
                        re.IGNORECASE,
                    )
                    if m2:
                        cand2 = m2.group(1).strip(" .,!?:;")
                        tokens = [t.lower() for t in cand2.split() if t]
                        bad_first = {
                            "having", "feeling", "allergic", "here", "calling", "looking", "suffering",
                            "experiencing", "booking", "trying", "not", "a", "an", "in", "on", "with",
                            "at", "to", "for", "from", "also", "very", "so", "just", "able", "unable",
                            "going", "coming", "back", "waiting", "cancelling", "canceling",
                            "doing", "getting", "facing", "down", "up", "fine", "good",
                            "okay", "ok", "sick", "ill", "unwell",
                        }
                        symptom_like = re.search(
                            r"\b(pain|ache|fever|cough|cold|rash|itch|dizz|nause|vomit|bleed|swell|hurt|sore|tired|sick|ill|injur|infection|breath|allergy|allergies)\b",
                            cand2,
                            re.IGNORECASE,
                        )
                        if tokens and tokens[0] not in bad_first and not symptom_like and not re.fullmatch(r"\d+", cand2):
                            candidate = cand2

                if candidate:
                    cleaned = candidate.strip(" .,!?:;")
                    if len(cleaned) >= 2 and cleaned.lower() not in {"booking", "calling", "here", "there"}:
                        updates["patient_name"] = cleaned.title()
                        summary_events.append(f"{today_str}: confirmed name")
                        break

            # Fallback: if assistant asked for name and user replied with name-only text
            if "patient_name" not in updates:
                msgs = state.get("messages", [])
                for i in range(len(msgs) - 1, -1, -1):
                    m = msgs[i]
                    if getattr(m, "type", "") != "human":
                        continue
                    utext = getattr(m, "content", "")
                    if not isinstance(utext, str):
                        break
                    prev = msgs[i - 1] if i - 1 >= 0 else None
                    if prev and getattr(prev, "type", "") in ("ai", "assistant"):
                        ptext = getattr(prev, "content", "")
                        asked_name = isinstance(ptext, str) and re.search(
                            r"\b(your name|patient'?s?\s+name|may i have your name|can i have your name|could i have your name|what(?:'s| is) your name|please provide your name)\b",
                            ptext,
                            re.IGNORECASE,
                        )
                        if asked_name:
                            cand = utext.strip().strip(" .,!?:;")
                            if re.fullmatch(r"[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2}", cand):
                                low = cand.lower()
                                non_names = {
                                    "yes", "no", "ok", "okay", "sure", "thanks", "thank", "bye",
                                    "hi", "hello", "cancel", "book", "appointment", "none", "na",
                                    "correct", "right", "wrong", "nope", "yep", "yeah", "unknown",
                                }
                                if 2 <= len(cand) <= 40 and low not in non_names and not re.search(r"\d", cand):
                                    updates["patient_name"] = " ".join(w.capitalize() for w in cand.split())
                                    summary_events.append(f"{today_str}: confirmed name")
                    break

        if "patient_phone" not in updates:
            phone_match = re.search(r"\b(\d{10})\b", content)
            if phone_match:
                candidate_phone = phone_match.group(1)
                existing_phone = existing_profile.get("patient_phone")
                # Only auto-capture when unknown. If different, do not overwrite (may be voice/typo).
                if not existing_phone:
                    updates["patient_phone"] = candidate_phone
                    summary_events.append(f"{today_str}: confirmed phone")
                elif str(existing_phone).strip() != candidate_phone:
                    # Potential correction/misheard number: leave for agent to confirm explicitly.
                    pass

        allergy_match = re.search(
            r"\b(?:allergic to|allergy to|have (?:an? )?allergy to|allergies(?:\s+are|\s+include|:))\s+([^.\n;]{1,80})",
            content,
            re.IGNORECASE,
        )
        if allergy_match:
            raw = allergy_match.group(1)
            items = []
            for part in re.split(r",|/|\band\b", raw, flags=re.IGNORECASE):
                item = part.strip(" .,!?:;-\"'()[]").lower()
                item = re.sub(r"\s+", " ", item).strip()
                if not item:
                    continue
                if item in {"none", "no", "nil", "n/a", "na", "not sure", "unknown", "nothing", "no allergies", "no allergy", "no known allergies", "none known"}:
                    continue
                item = re.sub(r"\b(only|etc|i guess|i think)\b.*$", "", item).strip(" .,!?:;-")
                if 2 <= len(item) <= 40 and re.search(r"[a-z]", item):
                    items.append(item)
            if items:
                existing_allergies = {str(a).strip().lower() for a in (existing_history.get("allergies") or [])}
                updates.setdefault("medical_history", {}).setdefault("allergies", [])
                new_items = []
                for item in items:
                    if item not in updates["medical_history"]["allergies"]:
                        updates["medical_history"]["allergies"].append(item)
                    if item.lower() not in existing_allergies:
                        new_items.append(item)
                if new_items:
                    summary_events.append(f"{today_str}: reported allergy {', '.join(new_items[:2])}")

    # Log newly captured symptoms from triage updates (if any)
    sym_updates = (updates.get("medical_history") or {}).get("reported_symptoms") if isinstance(updates.get("medical_history"), dict) else None
    if sym_updates:
        existing_syms = {str(s).strip().lower() for s in (existing_history.get("reported_symptoms") or [])}
        new_syms = [s for s in sym_updates if str(s).strip().lower() not in existing_syms]
        if new_syms:
            short = str(new_syms[0]).strip().replace("\n", " ")
            if len(short) > 50:
                short = short[:47] + "..."
            summary_events.append(f"{today_str}: reported symptom {short}")

    # Clean empty update containers before persisting
    mh = updates.get("medical_history")
    if isinstance(mh, dict):
        cleaned_mh = {k: v for k, v in mh.items() if v}
        if cleaned_mh:
            updates["medical_history"] = cleaned_mh
        else:
            updates.pop("medical_history", None)

    # Extract confirmed booking/cancellation details from THIS turn only.
    for msg in reversed(_current_turn_messages(state)):
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue
        lower = content.lower()
        if "appointment confirmed" in lower or "urgent appointment confirmed" in lower:
            appt_id = re.search(r"Appointment ID:\s*#?(\d+)", content, re.IGNORECASE)
            doctor = re.search(r"Doctor:\s*([^,\n]+)", content)
            appt_date = re.search(r"Date:\s*([^,\n]+)", content)
            appt_time = re.search(r"Time:\s*([^.\n]+)", content)
            parts = []
            if appt_id:
                parts.append(f"booked appt #{appt_id.group(1)}")
            else:
                parts.append("booked appointment")
            if doctor:
                parts.append(f"with {doctor.group(1).strip()}")
            if appt_date:
                parts.append(f"on {appt_date.group(1).strip()}")
            if appt_time:
                parts.append(f"at {appt_time.group(1).strip().rstrip('.')}")
            summary_events.append(f"{today_str}: " + " ".join(parts))

            if "patient_name" not in updates and not existing_profile.get("patient_name"):
                m = re.search(r"Patient:\s*([^,\n]+)", content)
                if m:
                    updates["patient_name"] = m.group(1).strip()
            if "patient_phone" not in updates and not existing_profile.get("patient_phone"):
                m = re.search(r"\b(\d{10})\b", content)
                if m:
                    updates["patient_phone"] = m.group(1)
            elif "patient_phone" not in updates and existing_profile.get("patient_phone"):
                pass
            if "preferred_doctor" not in updates and not existing_profile.get("preferred_doctor"):
                if doctor:
                    updates["preferred_doctor"] = doctor.group(1).strip()
            break

    for msg in reversed(_current_turn_messages(state)):
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue
        if "has been successfully cancelled" in content.lower() or "cancellation" in content.lower():
            appt_id = re.search(r"Appointment\s*#?(\d+)", content, re.IGNORECASE)
            if appt_id and "cancelled" in content.lower():
                summary_events.append(f"{today_str}: cancelled appt #{appt_id.group(1)}")
                break

    if summary_events:
        # preserve order, dedupe within this turn
        seen = set()
        deduped = []
        for e in summary_events:
            e = str(e).strip()
            if e and e not in seen:
                seen.add(e)
                deduped.append(e)
        if deduped:
            updates["conversation_summary"] = deduped

    if updates:
        update_patient_memory(store, user_id, updates)

    return {"memory_updates": {}}



# ==========================================
# PART 4: State Graph Construction & Wiring
# ==========================================

builder = StateGraph(AgentState)

# 1. Register specialist nodes
builder.add_node("supervisor", supervisor_node)
builder.add_node("triage", triage_node)
builder.add_node("scheduling", scheduling_node)
builder.add_node("clinic_info", clinic_info_node)
builder.add_node("general", general_node)
builder.add_node("memory_update", memory_update_node)

# 2. Register tool execution nodes
builder.add_node("triage_tools", ToolNode(triage_tools))
builder.add_node("scheduling_tools", ToolNode(scheduling_tools))
builder.add_node("clinic_info_tools", ToolNode(clinic_info_tools))

# 3. Start at supervisor
builder.add_edge(START, "supervisor")

# 4. Supervisor routing
builder.add_conditional_edges(
    "supervisor",
    lambda state: state.get("next_worker", "general"),
    {
        "triage": "triage",
        "scheduling": "scheduling",
        "clinic_info": "clinic_info",
        "general": "general",
    },
)

# 5. Triage tool loop
builder.add_conditional_edges(
    "triage",
    tools_condition,
    {
        "tools": "triage_tools",
        END: "memory_update",
    },
)

builder.add_edge("triage_tools", "triage")

# 6. Scheduling tool loop
builder.add_conditional_edges(
    "scheduling",
    tools_condition,
    {
        "tools": "scheduling_tools",
        END: "memory_update",
    },
)

builder.add_edge("scheduling_tools", "scheduling")

# 7. Clinic information tool loop
builder.add_conditional_edges(
    "clinic_info",
    tools_condition,
    {
        "tools": "clinic_info_tools",
        END: "memory_update",
    },
)

builder.add_edge("clinic_info_tools", "clinic_info")

# 8. General response
builder.add_edge("general", "memory_update")

# 9. Memory Updates after execution
builder.add_edge("memory_update", END)

# ==========================================
# PART 5: Compile Graph & Enable Memory
# ==========================================

memory = get_checkpointer()
store = get_store()

graph = builder.compile(
    checkpointer=memory,
    store=store
)
