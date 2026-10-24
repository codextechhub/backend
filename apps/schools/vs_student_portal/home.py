"""The pupil portal's home: one read that gathers a block from each module.

Home is the first thing a pupil sees after signing in, so it must never fail
as a whole because one module cannot answer. Each block is read from the module
that owns it, through the same function its own portal route uses, inside a
savepoint of its own: a block that raises is logged and answered as
``{"available": false, "message": ...}``, and the others still arrive.

**Unavailable is never zero.** A pupil told they owe ₦0.00 because the books
could not be read, or that they have no notices because the notice table could
not be queried, has been told something false. The screen shows the block's
sentence instead, and a pupil who needs the figure opens its own page.

Every available block carries ``"available": true`` beside its own fields.
Assignments and messages have no block: neither is built, and a block that
always answered "unavailable" would read as a fault rather than a feature not
yet offered.
"""
from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

UNAVAILABLE = {
    "today": "Today's classes cannot be shown right now. Open Timetable to try again.",
    "attendance": "Your attendance cannot be shown right now. Open Attendance to try again.",
    "fees": "Your fees cannot be shown right now. Open Fees to try again.",
    "notices": "Notices cannot be shown right now. Open Notices to try again.",
    "notifications": "Your notifications cannot be counted right now.",
}


def _block(name, read):
    """Run one block's read in its own savepoint; a failure is unavailable."""
    try:
        with transaction.atomic():
            return {"available": True, **read()}
    except Exception:  # noqa: BLE001 - one block never fails the whole home
        logger.exception("Pupil portal home: the %s block could not be read.", name)
        return {"available": False, "message": UNAVAILABLE[name]}


def today_block(student) -> dict:
    """Today's periods with done / now / next / later, from the published grid."""
    from .services import timetable

    grid = timetable(student)
    today = grid["today"]
    return {
        "published": grid["published"],
        "date": today["date"] if today else None,
        "periods": today["periods"] if today else [],
    }


def attendance_block(student) -> dict:
    """Today's mark, or not taken yet, and the term's attendance rate so far."""
    from schools.vs_attendance.constants import Mark
    from schools.vs_attendance.services.student_tab import pupil_attendance

    body = pupil_attendance(student.tenant, student)
    term = body["term"]
    today = body["today"]
    if today is not None:
        today = {
            **today,
            "mark_label": Mark(today["mark"]).label if today["mark"] else None,
        }
    return {
        "term": (
            {"id": term["id"], "name": term["name"], "session_name": term["session_name"]}
            if term else None
        ),
        "today": today,
        "rate": body["counts"]["rate"],
    }


def _fees(student) -> dict:
    """What is owed, the credit held, the status, and the invoice falling due first."""
    from .fees import fee_summary, position_status

    summary = fee_summary(student)
    open_invoices = sorted(
        (row for row in summary["invoices"] if row["balance"] > 0),
        key=lambda row: (row["due_date"] is None, row["due_date"] or row["invoice_date"]),
    )
    first = open_invoices[0] if open_invoices else None
    return {
        "owing": summary["owing"],
        "credit": summary["credit"],
        "balance": summary["balance"],
        "status": position_status(summary["owing"], summary["credit"]),
        "open_invoice": (
            {
                key: first[key]
                for key in ("invoice_ref", "number", "title", "due_date", "balance",
                            "status", "status_label")
            }
            if first else None
        ),
        "open_invoice_count": len(open_invoices),
        "can_pay": summary["can_pay"],
    }


def _notices(student, user) -> dict:
    """The newest notice addressed to the pupil, and how many they have not opened."""
    from schools.vs_notices.models import NoticeRead
    from schools.vs_notices.serializers import PortalNoticeSerializer
    from schools.vs_notices.services.notices import pupil_notices

    notices = pupil_notices(student)
    latest = notices.first()
    latest_data = None
    if latest is not None:
        read = NoticeRead.objects.filter(user=user, notice=latest).exists()
        latest_data = PortalNoticeSerializer(
            latest, context={"read_ids": {latest.pk} if read else set()},
        ).data
    return {
        "latest": latest_data,
        "unread_count": notices.exclude(reads__user=user).count(),
    }


def _notifications(user) -> dict:
    from vs_notifications.services.inbox import unread_in_app_count

    return {"unread_count": unread_in_app_count(user)}


def home(student, *, user) -> dict:
    """Every home block for the signed-in pupil, each available or not on its own."""
    from vs_config.clock import branch_today

    return {
        "date": branch_today(student.tenant, student.branch),
        "today": _block("today", lambda: today_block(student)),
        "attendance": _block("attendance", lambda: attendance_block(student)),
        "fees": _block("fees", lambda: _fees(student)),
        "notices": _block("notices", lambda: _notices(student, user)),
        "notifications": _block("notifications", lambda: _notifications(user)),
    }
