"""Patient address capture, prefill and required-field tests."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import headers

DOCTOR = "dr-rajesh-sharma"
PHONE = "9000000099"

ADDRESS_A = {
    "address_line1": "12 Shivaji Nagar",
    "address_line2": "Near Metro",
    "city": "Pune",
    "state": "Maharashtra",
    "pincode": "411016",
    "country": "India",
}
ADDRESS_B = {
    "address_line1": "8 MG Road",
    "address_line2": "",
    "city": "Mumbai",
    "state": "Maharashtra",
    "pincode": "400001",
    "country": "India",
}


def _payload(day, **overrides):
    payload = {
        "doctor_id": DOCTOR,
        "date": day.isoformat(),
        "time": "09:00",
        "patient_name": "Anuj Patil",
        "patient_phone": PHONE,
        "reason": "Follow-up",
    }
    payload.update(overrides)
    return payload


def test_booking_stores_address_snapshot_and_profile(client, open_day):
    response = client.post(
        "/api/appointments",
        json=_payload(open_day, address=ADDRESS_A),
        headers=headers("receptionist"),
    )
    assert response.status_code == 201, response.text
    appointment = response.json()
    assert appointment["patient_address"] == ADDRESS_A

    detail = client.get(
        f"/api/appointments/{appointment['id']}", headers=headers("receptionist")
    ).json()
    assert detail["patient_address"] == ADDRESS_A

    patient = client.get(
        "/api/patients/lookup",
        params={"phone": PHONE},
        headers=headers("patient"),
    ).json()
    assert patient["address"] == ADDRESS_A


def test_latest_booking_updates_profile_but_not_old_appointment(client, open_day):
    first = client.post(
        "/api/appointments",
        json=_payload(open_day, address=ADDRESS_A),
        headers=headers("receptionist"),
    ).json()
    second_day = open_day + timedelta(days=7)
    second = client.post(
        "/api/appointments",
        json=_payload(second_day, time="09:30", address=ADDRESS_B),
        headers=headers("receptionist"),
    )
    assert second.status_code == 201, second.text

    patient = client.get(
        "/api/patients/lookup",
        params={"phone": PHONE},
        headers=headers("receptionist"),
    ).json()
    assert patient["address"] == ADDRESS_B

    old = client.get(
        f"/api/appointments/{first['id']}", headers=headers("receptionist")
    ).json()
    assert old["patient_address"] == ADDRESS_A


def test_blank_address_keeps_previous_profile(client, open_day):
    client.post(
        "/api/appointments",
        json=_payload(open_day, address=ADDRESS_A),
        headers=headers("receptionist"),
    )
    response = client.post(
        "/api/appointments",
        json=_payload(
            open_day + timedelta(days=7),
            time="09:30",
            address={"address_line1": "", "city": "", "country": ""},
        ),
        headers=headers("receptionist"),
    )
    assert response.status_code == 201, response.text

    patient = client.get(
        "/api/patients/lookup",
        params={"phone": PHONE},
        headers=headers("receptionist"),
    ).json()
    assert patient["address"] == ADDRESS_A


def test_blank_required_fields_are_rejected(client, open_day):
    for field in ("patient_name", "patient_phone", "reason"):
        response = client.post(
            "/api/appointments",
            json=_payload(open_day, **{field: "   "}),
            headers=headers("receptionist"),
        )
        assert response.status_code in (400, 422), response.text


def test_missing_reason_is_rejected(client, open_day):
    payload = _payload(open_day)
    payload.pop("reason")
    response = client.post(
        "/api/appointments",
        json=payload,
        headers=headers("receptionist"),
    )
    assert response.status_code in (400, 422), response.text


def test_phone_must_be_exactly_ten_digits(client, open_day):
    for phone in ("900000000", "90000000011", "abcdefghij", "12345"):
        response = client.post(
            "/api/appointments",
            json=_payload(open_day, patient_phone=phone),
            headers=headers("receptionist"),
        )
        assert response.status_code in (400, 422), response.text


def test_phone_is_normalized_to_digits(client, open_day):
    response = client.post(
        "/api/appointments",
        json=_payload(open_day, patient_phone="90000-00099"),
        headers=headers("receptionist"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["patient_phone"] == "9000000099"


def test_pincode_must_be_six_digits(client, open_day):
    for pincode in ("12345", "1234567", "abcdef"):
        response = client.post(
            "/api/appointments",
            json=_payload(open_day, address={**ADDRESS_A, "pincode": pincode}),
            headers=headers("receptionist"),
        )
        assert response.status_code in (400, 422), response.text


def test_pincode_is_optional_and_normalized(client, open_day):
    response = client.post(
        "/api/appointments",
        json=_payload(open_day, address={**ADDRESS_A, "pincode": "411 016"}),
        headers=headers("receptionist"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["patient_address"]["pincode"] == "411016"

    no_pin = client.post(
        "/api/appointments",
        json=_payload(
            open_day,
            time="09:30",
            address={**ADDRESS_A, "pincode": ""},
        ),
        headers=headers("receptionist"),
    )
    assert no_pin.status_code == 201, no_pin.text
    assert no_pin.json()["patient_address"]["pincode"] == ""


def test_address_is_optional(client, open_day):
    response = client.post(
        "/api/appointments",
        json=_payload(open_day),
        headers=headers("receptionist"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["patient_address"] is None


def test_patient_can_book_with_address(client, open_day):
    response = client.post(
        "/api/appointments",
        json=_payload(open_day, address=ADDRESS_A),
        headers=headers("patient"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["patient_address"] == ADDRESS_A
