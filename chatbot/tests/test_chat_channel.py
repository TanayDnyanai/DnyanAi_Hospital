"""Channel-boundary contract tests for /chat."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from graph import _current_turn_messages
from main import ChatRequest, _thread_id


def test_voice_and_text_use_separate_threads():
    assert _thread_id("session-1", "voice") != _thread_id("session-1", "text")
    assert _thread_id("session-1", "voice") == "session-1:voice"


def test_chat_request_defaults_to_text():
    request = ChatRequest(
        session_id="session-1", user_id="patient-1", message="hello"
    )
    assert request.channel == "text"


def test_chat_request_rejects_unknown_channel():
    with pytest.raises(ValidationError):
        ChatRequest(
            session_id="session-1",
            user_id="patient-1",
            message="hello",
            channel="carrier-pigeon",
        )


def test_memory_only_reads_the_current_turn():
    old_booking = AIMessage(content="Appointment confirmed #99 with Dr. Sharma")
    state = {
        "messages": [
            HumanMessage(content="book an appointment"),
            old_booking,
            HumanMessage(content="thanks"),
            AIMessage(content="You're welcome."),
        ]
    }
    current = _current_turn_messages(state)
    assert old_booking not in current
    assert current[0].content == "thanks"
