"""WhatsApp notification helpers (provider-agnostic).

No DB schema changes: pure helpers, message templates, wa.me link generation,
and stubs for future WhatsApp Business API integration.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo
from env_config import load_app_env

# Load configuration without overriding injected deployment secrets.
load_app_env()

from database import SLOT_DURATION_MINUTES, STANDARD_SLOTS

TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "http://localhost:8000").rstrip("/")
WHATSAPP_MODE = os.getenv("WHATSAPP_MODE", "link").strip().lower()  # link | api
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "").strip().lower()
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "").strip()
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0").strip()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID", "").strip()
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()

REMINDER_MINUTES_BEFORE = 10
CHECKIN_MINUTES_AFTER = 10


@dataclass
class AppointmentInfo:
    appointment_id: int
    patient_name: str
    patient_phone: str
    doctor_name: str
    date: str  # YYYY-MM-DD
    time_slot: str  # e.g. 10:00 AM
    status: str = "Confirmed"


def clinic_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def now_in_clinic_tz() -> datetime:
    return datetime.now(clinic_tz())


def parse_slot_datetime(date_str: str, time_slot: str, tz: ZoneInfo | None = None) -> datetime | None:
    tz = tz or clinic_tz()
    try:
        naive = datetime.strptime(f"{date_str.strip()} {time_slot.strip()}", "%Y-%m-%d %I:%M %p")
        return naive.replace(tzinfo=tz)
    except Exception:
        return None


def slot_start_end(date_str: str, time_slot: str) -> tuple[datetime | None, datetime | None]:
    start = parse_slot_datetime(date_str, time_slot)
    if not start:
        return None, None
    return start, start + timedelta(minutes=SLOT_DURATION_MINUTES)


def normalize_whatsapp_phone(phone: str, default_country_code: str = "91") -> str:
    """Best-effort normalization for wa.me links (digits only, with country code)."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        return default_country_code + digits
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def build_wa_link(phone: str, text: str) -> str:
    normalized = normalize_whatsapp_phone(phone)
    encoded = quote(text or "", safe="")
    if normalized:
        return f"https://wa.me/{normalized}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


# ---------- Message templates ----------

def reminder_message(appt: AppointmentInfo) -> str:
    return (
        f"Hello {appt.patient_name}, this is a reminder from DnyanAI Hospital. "
        f"Your appointment with {appt.doctor_name} is at {appt.time_slot} today ({appt.date}). "
        f"Please arrive on time."
    )


def send_booking_confirmation(appt: AppointmentInfo) -> dict[str, Any]:
    """Send (or generate link for) booking confirmation message."""
    text = (
        f"Hello {appt.patient_name}, your appointment with {appt.doctor_name} "
        f"on {appt.date} at {appt.time_slot} (#{appt.appointment_id}) is confirmed. "
        f"Please arrive on time. Reply YES or NO if asked about attendance."
    )
    return send_whatsapp_message(
        appt.patient_phone,
        text,
        content_variables={"1": f"{appt.doctor_name} ({appt.date})", "2": appt.time_slot},
    )


def checkin_message(appt: AppointmentInfo) -> str:
    return (
        f"Hello {appt.patient_name}, your appointment with {appt.doctor_name} "
        f"at {appt.time_slot} ({appt.date}) has started. "
        f"Will you be coming? Please reply YES or NO."
    )


def rescheduled_message(appt: AppointmentInfo, new_slot: str) -> str:
    return (
        f"Hello {appt.patient_name}, your appointment with {appt.doctor_name} "
        f"has been rescheduled to {new_slot} on {appt.date}. "
        f"Reply if you need further help."
    )


def portal_reschedule_message(appt: AppointmentInfo) -> str:
    return (
        f"Hello {appt.patient_name}, we could not find another slot today with {appt.doctor_name}. "
        f"Please reschedule here: {PORTAL_BASE_URL}/"
    )


# ---------- Pure scheduling/reschedule helpers ----------

def next_slot_label(current_slot: str) -> str | None:
    try:
        idx = STANDARD_SLOTS.index(current_slot)
    except ValueError:
        return None
    if idx + 1 >= len(STANDARD_SLOTS):
        return None
    return STANDARD_SLOTS[idx + 1]


def find_next_available_same_day_slot(current_slot: str, available_slots: list[str]) -> str | None:
    """Return earliest available slot after current_slot from a same-doctor same-day availability list."""
    try:
        idx = STANDARD_SLOTS.index(current_slot)
    except ValueError:
        return None
    ordered = [s for s in STANDARD_SLOTS[idx + 1 :] if s in set(available_slots or [])]
    return ordered[0] if ordered else None


def reschedule_decision(current_slot: str, available_slots: list[str], portal_url: str | None = None) -> dict[str, Any]:
    """Pure decision: reschedule to next available same-doctor/same-day slot or provide portal link."""
    new_slot = find_next_available_same_day_slot(current_slot, available_slots)
    if new_slot:
        return {"action": "reschedule", "new_time_slot": new_slot}
    return {"action": "portal_link", "portal_url": (portal_url or PORTAL_BASE_URL)}


def is_reminder_due(date_str: str, time_slot: str, now: datetime | None = None) -> bool:
    start, _ = slot_start_end(date_str, time_slot)
    if not start:
        return False
    now = now or now_in_clinic_tz()
    return now >= (start - timedelta(minutes=REMINDER_MINUTES_BEFORE))


def is_checkin_due(date_str: str, time_slot: str, now: datetime | None = None) -> bool:
    start, _ = slot_start_end(date_str, time_slot)
    if not start:
        return False
    now = now or now_in_clinic_tz()
    return now >= (start + timedelta(minutes=CHECKIN_MINUTES_AFTER))


def build_notification_actions(schedule: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Compute due reminder/check-in actions from /api/schedule-like data.

    No DB writes. For WHATSAPP_MODE=link this produces wa.me links staff can click.
    For WHATSAPP_MODE=api these are intents to send via provider adapter (future).
    """
    now = now or now_in_clinic_tz()
    actions: list[dict[str, Any]] = []
    date_str = (schedule or {}).get("date")
    if not date_str:
        return actions

    for doc in (schedule or {}).get("doctors", []):
        doctor_name = doc.get("doctor_name")
        for slot in doc.get("slots", []):
            if slot.get("status") != "Confirmed":
                continue
            appt = AppointmentInfo(
                appointment_id=int(slot.get("appointment_id")),
                patient_name=str(slot.get("patient_name") or "Patient"),
                patient_phone=str(slot.get("patient_phone") or ""),
                doctor_name=str(doctor_name or ""),
                date=date_str,
                time_slot=str(slot.get("time_slot") or ""),
                status="Confirmed",
            )

            # T-10 reminder (future step 1) - include as due action intent
            if is_reminder_due(appt.date, appt.time_slot, now) and not is_checkin_due(appt.date, appt.time_slot, now):
                text = reminder_message(appt)
                actions.append(
                    {
                        "type": "reminder",
                        "appointment_id": appt.appointment_id,
                        "patient_phone": appt.patient_phone,
                        "doctor_name": appt.doctor_name,
                        "date": appt.date,
                        "time_slot": appt.time_slot,
                        "text": text,
                        "whatsapp_link": build_wa_link(appt.patient_phone, text),
                    }
                )

            # T+10 check-in (step 2)
            if is_checkin_due(appt.date, appt.time_slot, now):
                text = checkin_message(appt)
                actions.append(
                    {
                        "type": "checkin",
                        "appointment_id": appt.appointment_id,
                        "patient_phone": appt.patient_phone,
                        "doctor_name": appt.doctor_name,
                        "date": appt.date,
                        "time_slot": appt.time_slot,
                        "text": text,
                        "whatsapp_link": build_wa_link(appt.patient_phone, text),
                    }
                )
    return actions


# ---------- Provider stubs (future) ----------

def send_whatsapp_message(
    phone: str,
    text: str,
    content_sid: str | None = None,
    content_variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send WhatsApp message. link mode returns a wa.me link; api mode calls Twilio or Meta Cloud API."""
    mode = os.getenv("WHATSAPP_MODE", WHATSAPP_MODE).strip().lower()
    provider = os.getenv("WHATSAPP_PROVIDER", WHATSAPP_PROVIDER).strip().lower()

    if mode == "link":
        return {
            "success": False,
            "mode": "link",
            "error": "Automated sending disabled in link mode.",
            "link": build_wa_link(phone, text),
        }
    if provider == "twilio":
        sid = os.getenv("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID).strip()
        token = os.getenv("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN).strip()
        from_num = os.getenv("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER).strip()
        if TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET:
            sid = TWILIO_API_KEY_SID
            token = TWILIO_API_KEY_SECRET
        if not sid or not token or not from_num:
            return {"success": False, "mode": mode, "error": "Twilio not configured (SID, token, or from_number missing)."}
        to = normalize_whatsapp_phone(phone)
        if not to:
            return {"success": False, "mode": mode, "error": "Invalid phone number."}
        if not to.startswith("whatsapp:"):
            to = "whatsapp:+" + to
        if not from_num.startswith("whatsapp:"):
            from_num = "whatsapp:" + from_num

        content_sid = os.getenv("TWILIO_CONTENT_SID", "").strip()
        if content_sid:
            payload_dict = {
                "From": from_num,
                "To": to,
                "ContentSid": content_sid,
                "ContentVariables": json.dumps({"1": text}),
            }
        else:
            payload_dict = {
                "From": from_num,
                "To": to,
                "Body": text,
            }

        url = f"https://api.twilio.com/2010-04-01/Accounts/{os.getenv('TWILIO_ACCOUNT_SID', TWILIO_ACCOUNT_SID).strip()}/Messages.json"
        data = urllib.parse.urlencode(payload_dict).encode("utf-8")
        creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {creds}"}, method="POST")
        try:
            print(f"[whatsapp] Sending via Twilio From={from_num} To={to}...")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            print(f"[whatsapp] Sent successfully. SID={body.get('sid')}")
            return {"success": True, "mode": "api", "provider": "twilio", "response": body}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = ""
            print(f"[whatsapp] Twilio HTTP error {e.code}: {body or e.reason}")
            return {"success": False, "mode": "api", "provider": "twilio", "error": f"HTTP {e.code}: {body or e.reason}"}
        except Exception as e:
            print(f"[whatsapp] Twilio error: {e}")
            return {"success": False, "mode": "api", "provider": "twilio", "error": str(e)}

    if WHATSAPP_PROVIDER != "meta" or not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return {"success": False, "mode": WHATSAPP_MODE, "error": "WhatsApp API provider not configured."}
    to = normalize_whatsapp_phone(phone)
    if not to:
        return {"success": False, "mode": WHATSAPP_MODE, "error": "Invalid phone number."}
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"success": True, "mode": "api", "provider": "meta", "response": body}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return {"success": False, "mode": "api", "provider": "meta", "error": f"HTTP {e.code}: {body or e.reason}"}
    except Exception as e:
        return {"success": False, "mode": "api", "provider": "meta", "error": str(e)}


def parse_inbound_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Meta Cloud API inbound webhook payload."""
    try:
        entry = (payload or {}).get("entry") or []
        messages = []
        for e in entry:
            for change in e.get("changes", []):
                value = change.get("value", {}) or {}
                for m in value.get("messages", []) or []:
                    text = (m.get("text") or {}).get("body")
                    messages.append({
                        "from": m.get("from"),
                        "id": m.get("id"),
                        "timestamp": m.get("timestamp"),
                        "type": m.get("type"),
                        "text": text,
                    })
        return {"ok": True, "messages": messages}
    except Exception as e:
        return {"ok": False, "error": str(e), "raw_keys": list((payload or {}).keys())}


def interpret_reply(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t in {"yes", "y", "haan", "ha", "ho", "coming", "on my way", "aa raha hu", "aa rahi hu"}:
        return "coming"
    if t in {"no", "n", "nahi", "nahin", "not coming", "can't come", "cant come", "cancel"}:
        return "not_coming"
    return None
