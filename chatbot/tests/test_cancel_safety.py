"""Cancellation must be ownership-checked and explicitly confirmed in code."""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

import tools
from tools import cancel_appointment, _user_confirmed


def _runtime(messages, user_id="patient-1"):
    return SimpleNamespace(
        config={"configurable": {"user_id": user_id}},
        state={"messages": messages},
    )


def _appointment(phone="9876543210"):
    return {
        "id": 5,
        "patient_name": "Test Patient",
        "patient_phone": phone,
        "doctor_name": "Dr. Sharma",
        "date": "2026-12-03",
        "time_slot": "10:30 AM",
        "status": "Confirmed",
    }


def test_confirmation_required_after_agent_offer():
    runtime = _runtime([
        HumanMessage(content="cancel my appointment"),
        AIMessage(content="Appointment #5 with Dr. Sharma on 2026-12-03 at 10:30 AM. Should I cancel it?"),
        HumanMessage(content="yes"),
    ])
    assert _user_confirmed(runtime, 5)[0] is True


def test_bare_cancel_request_is_not_enough():
    runtime = _runtime([HumanMessage(content="cancel appointment 5")])
    assert _user_confirmed(runtime, 5)[0] is False


def test_yes_without_a_pending_offer_is_not_enough():
    runtime = _runtime([
        HumanMessage(content="hello"),
        AIMessage(content="Hi! How can I help?"),
        HumanMessage(content="yes"),
    ])
    assert _user_confirmed(runtime, 5)[0] is False


def test_cross_patient_cancel_is_blocked(monkeypatch):
    writes = []
    monkeypatch.setattr(
        tools.database, "get_patient_profile_by_user_id",
        lambda user_id: {"patient_phone": "9999999999"},
    )
    monkeypatch.setattr(
        tools.database, "get_appointment_by_id", lambda appt_id: _appointment()
    )
    monkeypatch.setattr(
        tools.database,
        "cancel_appointment",
        lambda appt_id: writes.append(appt_id) or {"success": True},
    )
    runtime = _runtime([
        AIMessage(content="Should I cancel appointment #5?"),
        HumanMessage(content="yes"),
    ])

    result = cancel_appointment.func(5, runtime)

    assert "blocked" in result.lower()
    assert writes == []


def test_confirmed_owner_cancel_succeeds(monkeypatch):
    writes = []
    monkeypatch.setattr(
        tools.database, "get_patient_profile_by_user_id",
        lambda user_id: {"patient_phone": "+91 98765 43210"},
    )
    monkeypatch.setattr(
        tools.database, "get_appointment_by_id", lambda appt_id: _appointment()
    )
    monkeypatch.setattr(
        tools.database,
        "cancel_appointment",
        lambda appt_id: writes.append(appt_id) or {
            "success": True,
            "appointment_id": appt_id,
            "patient_name": "Test Patient",
            "doctor_name": "Dr. Sharma",
            "date": "2026-12-03",
            "time_slot": "10:30 AM",
        },
    )
    runtime = _runtime([
        AIMessage(content="Should I cancel appointment #5?"),
        HumanMessage(content="yes, please"),
    ])

    result = cancel_appointment.func(5, runtime)

    assert "successfully cancelled" in result
    assert writes == [5]
