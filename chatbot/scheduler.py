"""Scheduler skeleton for appointment notifications.

Uses APScheduler if available; otherwise a simple background thread loop.
Designed to be idempotent and safe for single-worker local usage.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _HAS_APSCHEDULER = True
except Exception:  # pragma: no cover
    _HAS_APSCHEDULER = False

import database
import notifications


def _process_due_checkins() -> dict[str, Any]:
    """Process due T+10 check-ins: send check-in message, mark status.

    In link mode this only generates links; in api mode it would send via provider.
    """
    from datetime import datetime
    now = datetime.now()
    due = database.get_due_checkins(now)
    results = []
    for appt in due:
        appt_info = notifications.AppointmentInfo(
            appointment_id=appt["id"],
            patient_name=appt["patient_name"],
            patient_phone=appt["patient_phone"],
            doctor_name=appt["doctor_name"],
            date=appt["date"],
            time_slot=appt["time_slot"],
            status="Confirmed",
        )
        text = notifications.checkin_message(appt_info)
        msg_result = notifications.send_whatsapp_message(appt_info.patient_phone, text)
        database.update_notification_status(appt["id"], "pending_reply")
        results.append({"appointment_id": appt["id"], "message": msg_result})
    return {"processed": len(results), "results": results}


def start_scheduler() -> None:
    """Start the notification scheduler (single-worker)."""
    if _HAS_APSCHEDULER:
        scheduler = BackgroundScheduler()
        scheduler.add_job(_process_due_checkins, "interval", minutes=1, id="checkin_job", replace_existing=True)
        scheduler.start()
        print("[scheduler] APScheduler started (1-min interval)")
    else:
        def _loop():
            while True:
                try:
                    _process_due_checkins()
                except Exception as e:
                    print(f"[scheduler] error: {e}")
                time.sleep(60)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[scheduler] background thread started (1-min interval)")


if __name__ == "__main__":
    start_scheduler()
    import time as _t
    _t.sleep(3600)
