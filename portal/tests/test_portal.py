"""Smoke tests for the combined front door."""

from fastapi.testclient import TestClient

from portal.app import app

client = TestClient(app)


def test_portal_serves_toggle_page() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "appSwitch" in response.text
    assert 'id="calendarFrame"' in response.text
    assert 'id="chatFrame"' in response.text


def test_both_apps_are_mounted() -> None:
    assert client.get("/health").json()["apps"] == ["calendar", "chatbot"]
    assert client.get("/calendar/health").status_code == 200
    assert client.get("/chat/health").status_code == 200


def test_calendar_assets_are_reachable_through_portal() -> None:
    response = client.get("/calendar/static/css/app.css")
    assert response.status_code == 200
    assert "Responsive" in response.text
