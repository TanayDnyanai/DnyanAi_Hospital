"""Seed the Indian / Maharashtra public holiday list.

These dates are a starting point for the clinic; admins can edit or remove
any entry from the calendar UI. Lunar festivals move every year, so verify
against the official gazette before relying on them for closures.
"""

from __future__ import annotations

from datetime import date

from .datasource.base import DataSource

# (ISO date, display name)
HOLIDAYS: tuple[tuple[str, str], ...] = (
    # 2026
    ("2026-01-26", "Republic Day"),
    ("2026-03-04", "Holi"),
    ("2026-03-19", "Gudi Padwa"),
    ("2026-03-20", "Eid al-Fitr"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-08-15", "Independence Day"),
    ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-02", "Gandhi Jayanti"),
    ("2026-10-20", "Dussehra"),
    ("2026-11-08", "Diwali"),
    ("2026-11-24", "Guru Nanak Jayanti"),
    ("2026-12-25", "Christmas"),
    # 2027
    ("2027-01-26", "Republic Day"),
    ("2027-03-10", "Eid al-Fitr"),
    ("2027-03-22", "Holi"),
    ("2027-03-26", "Good Friday"),
    ("2027-04-07", "Gudi Padwa"),
    ("2027-04-14", "Dr. Ambedkar Jayanti"),
    ("2027-05-01", "Maharashtra Day"),
    ("2027-08-15", "Independence Day"),
    ("2027-09-04", "Ganesh Chaturthi"),
    ("2027-10-02", "Gandhi Jayanti"),
    ("2027-10-09", "Dussehra"),
    ("2027-10-29", "Diwali"),
    ("2027-12-25", "Christmas"),
)


def seed_default_holidays(source: DataSource) -> int:
    """Insert missing seed holidays. Returns the number of rows added."""
    window_start = date(2026, 1, 1)
    window_end = date(2027, 12, 31)
    existing = {
        (holiday.date.isoformat(), holiday.name)
        for holiday in source.list_holidays(window_start, window_end)
        if holiday.scope == "CLINIC"
    }

    added = 0
    for iso_date, name in HOLIDAYS:
        if (iso_date, name) in existing:
            continue
        source.create_holiday(
            day=date.fromisoformat(iso_date),
            name=name,
            scope="CLINIC",
            holiday_type="PUBLIC_HOLIDAY",
            actor="seed",
        )
        added += 1
    return added


if __name__ == "__main__":  # pragma: no cover
    from .datasource.factory import get_data_source

    print(f"Seeded {seed_default_holidays(get_data_source())} holidays.")
