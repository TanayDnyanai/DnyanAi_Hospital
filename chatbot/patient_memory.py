
import json
from langgraph.store.base import BaseStore
import database


def _empty_profile() -> dict:
    return {
        "patient_name": None,
        "patient_phone": None,
        "preferred_doctor": None,
        "medical_history": {
            "allergies": [],
            "conditions": [],
            "medications": [],
            "reported_symptoms": [],
        },
        "conversation_summary": "",
    }


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
        return [p.strip() for p in text.split(",") if p.strip()]
    return [str(value).strip()]


def normalize_profile(data: dict | None) -> dict:
    profile = _empty_profile()
    if not data:
        return profile

    profile["patient_name"] = data.get("patient_name") or profile["patient_name"]
    profile["patient_phone"] = data.get("patient_phone") or profile["patient_phone"]
    profile["preferred_doctor"] = data.get("preferred_doctor") or profile["preferred_doctor"]

    history = data.get("medical_history") if isinstance(data.get("medical_history"), dict) else {}
    profile["medical_history"]["allergies"] = _as_list(
        history.get("allergies", data.get("allergies"))
    )
    profile["medical_history"]["conditions"] = _as_list(
        history.get("conditions", data.get("medical_conditions"))
    )
    profile["medical_history"]["medications"] = _as_list(
        history.get("medications", data.get("medications"))
    )
    profile["medical_history"]["reported_symptoms"] = _as_list(
        history.get("reported_symptoms", data.get("reported_symptoms"))
    )

    summary = data.get("conversation_summary")
    profile["conversation_summary"] = str(summary).strip() if summary else ""
    return profile


def _merge_unique(existing: list, incoming) -> list:
    result = list(existing)
    for item in _as_list(incoming):
        if item not in result:
            result.append(item)
    return result


def _merge_summary(existing: str, incoming) -> str:
    entries = []
    for line in (existing or "").splitlines():
        line = line.strip().lstrip("-•\t ").strip()
        if line:
            entries.append(line)

    new_items = []
    if incoming is None:
        new_items = []
    elif isinstance(incoming, list):
        new_items = [str(x).strip().lstrip("-•\t ").strip() for x in incoming if str(x).strip()]
    else:
        for line in str(incoming).replace(" | ", "\n").splitlines():
            line = line.strip().lstrip("-•\t ").strip()
            if line:
                new_items.append(line)

    for item in new_items:
        if item not in entries:
            entries.append(item)

    entries = entries[-8:]
    result = "\n".join(entries)
    if len(result) > 500:
        while entries and len("\n".join(entries)) > 500:
            entries.pop(0)
        result = "\n".join(entries)
    return result


def _merge_profiles(base: dict | None, incoming_updates: dict) -> dict:
    merged = normalize_profile(base)
    for key in ("patient_name", "patient_phone", "preferred_doctor"):
        value = incoming_updates.get(key)
        if value:
            merged[key] = str(value).strip()

    if "conversation_summary" in incoming_updates and incoming_updates.get("conversation_summary"):
        merged["conversation_summary"] = _merge_summary(
            merged.get("conversation_summary", ""),
            incoming_updates.get("conversation_summary"),
        )

    history_updates = incoming_updates.get("medical_history", {}) if isinstance(incoming_updates.get("medical_history"), dict) else {}
    for key in ("allergies", "conditions", "medications", "reported_symptoms"):
        incoming = history_updates.get(key, incoming_updates.get(key))
        if incoming is not None:
            merged["medical_history"][key] = _merge_unique(
                merged["medical_history"].get(key, []), incoming
            )

    if "medical_conditions" in incoming_updates and "conditions" not in history_updates:
        merged["medical_history"]["conditions"] = _merge_unique(
            merged["medical_history"].get("conditions", []),
            incoming_updates.get("medical_conditions"),
        )
    return merged


def get_patient_memory(store: BaseStore, user_id: str) -> dict | None:
    """Retrieve long-term memory — RAM first, SQLite fallback."""
    namespace = ("patients", user_id)
    memory = store.get(namespace, "patient_profile")

    if memory is not None:
        normalized = normalize_profile(memory.value)
        if normalized != memory.value:
            store.put(namespace, "patient_profile", normalized)
        return normalized

    db_profile = database.load_patient_profile(user_id)
    if db_profile is None:
        return None

    normalized = normalize_profile(db_profile)
    store.put(namespace, "patient_profile", normalized)
    return normalized


def save_patient_memory(store: BaseStore, user_id: str, memory: dict) -> None:
    """Save patient memory to RAM; persist to SQLite only when identity is safely known."""
    namespace = ("patients", user_id)
    normalized = normalize_profile(memory)
    store.put(namespace, "patient_profile", normalized)

    phone = normalized.get("patient_phone")
    # Avoid creating DB rows for temporary/unidentified sessions.
    if not phone:
        try:
            existing_row = database.get_patient_profile_by_user_id(user_id)
            if existing_row and existing_row.get("patient_phone"):
                database.upsert_patient_profile(user_id, normalized)
        except Exception:
            pass
        return

    # If this phone is owned by a different canonical profile, persist to canonical only.
    canonical_user_id = database.find_user_id_by_phone(phone)
    if canonical_user_id and canonical_user_id != user_id:
        database.upsert_patient_profile(canonical_user_id, normalized)
        store.put(("patients", canonical_user_id), "patient_profile", normalized)
        store.put(namespace, "patient_profile", normalized)
        return

    database.upsert_patient_profile(user_id, normalized)


def get_patient_context(store: BaseStore, user_id: str, category: str = "all") -> str:
    memory = get_patient_memory(store, user_id)
    if not memory:
        return "No known patient profile for this user_id."

    history = memory.get("medical_history", {})
    identity = [
        f"name={memory.get('patient_name') or 'unknown'}",
        f"phone={memory.get('patient_phone') or 'unknown'}",
        f"preferred_doctor={memory.get('preferred_doctor') or 'unknown'}",
    ]
    medical = [
        f"allergies={history.get('allergies', [])}",
        f"conditions={history.get('conditions', [])}",
        f"medications={history.get('medications', [])}",
        f"reported_symptoms={history.get('reported_symptoms', [])}",
    ]
    summary = memory.get("conversation_summary") or "none"

    if category == "medical":
        return "Known patient medical context: " + "; ".join(medical)
    if category == "identity":
        return "Known patient identity: " + ", ".join(identity)

    return (
        "Known patient profile: "
        + ", ".join(identity)
        + ". Medical: "
        + "; ".join(medical)
        + f". Summary: {summary}"
    )


def update_patient_memory(store: BaseStore, user_id: str, updates: dict) -> None:
    """Update selected fields in a patient's existing memory."""
    phone = updates.get("patient_phone")
    canonical_user_id = database.find_user_id_by_phone(phone) if phone else None

    if canonical_user_id and canonical_user_id != user_id:
        # Same phone already belongs to another profile: treat as same patient identity.
        # Merge into canonical profile and keep this session's RAM linked, without creating a new DB patient row.
        current = get_patient_memory(store, user_id) or _empty_profile()
        incoming = _merge_profiles(current, updates)
        canonical_existing = database.load_patient_profile(canonical_user_id) or _empty_profile()
        merged = normalize_profile(_merge_profiles(canonical_existing, incoming))
        store.put(("patients", canonical_user_id), "patient_profile", merged)
        database.upsert_patient_profile(canonical_user_id, merged)
        store.put(("patients", user_id), "patient_profile", merged)
        return

    existing_memory = get_patient_memory(store, user_id)
    merged = _merge_profiles(existing_memory, updates)
    save_patient_memory(store, user_id, merged)
