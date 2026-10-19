"""The parent portal's home: one read that gathers a block from each module.

Home is the first thing a parent sees after signing in, so it must never fail as
a whole because one module cannot answer. Each block is read from the module
that owns it, through the same function its own portal route uses, inside a
savepoint of its own: a block that raises is logged and answered as
``{"available": false, "message": ...}``, and the others still arrive.

**Unavailable is never zero.** A parent told a child owes ₦0.00 because the
books could not be read has been told something false. The screen shows the
block's sentence instead, and the parent opens the page that has the figure.

There is a block per child for attendance, fees, results and the next lesson,
and family blocks for fees, notices and notifications. Each child's blocks are
read separately, so one child's unreadable books leave a sibling's blocks
standing. Assignments and messages have no block: neither is built, and a block
that always answered "unavailable" would read as a fault rather than a feature
not yet offered.

The figures come from the pupil portal's own functions, called with the child's
record, so a parent and a pupil are never told two different things. Each
child's fees are read once and shared by that child's block and the family
block, and the family blocks read the feed once for the whole family.
"""
from __future__ import annotations

import logging

from django.db import transaction

from schools.vs_student_portal.exceptions import FinanceUnavailable

from . import fees as parent_fees

logger = logging.getLogger(__name__)

NOTICES_UNAVAILABLE = "Notices cannot be shown right now. Open Notices to try again."
NOTIFICATIONS_UNAVAILABLE = "Your notifications cannot be counted right now."
FAMILY_FEES_UNAVAILABLE = (
    "The family's fees cannot be shown right now. Open Fees to try again."
)


def _block(name, message, read):
    """Run one block's read in its own savepoint; a failure is unavailable."""
    try:
        with transaction.atomic():
            return {"available": True, **read()}
    except Exception:  # noqa: BLE001 - one block never fails the whole home
        logger.exception("Parent portal home: the %s block could not be read.", name)
        return {"available": False, "message": message}


class _FeeReads:
    """Each child's fees, read at most once per home."""

    def __init__(self, children):
        self.children = children
        self._entries: dict = {}

    def entry(self, child) -> dict:
        if child.pk not in self._entries:
            self._entries[child.pk] = parent_fees.read_child(child)
        return self._entries[child.pk]

    def entries(self) -> list:
        return [self.entry(child) for child in self.children]


def _today(child) -> dict:
    """The lesson in progress or next up today, from the published grid."""
    from schools.vs_student_portal.home import today_block

    body = today_block(child)
    lesson = next(
        (
            period for period in body["periods"]
            if period["kind"] == "LESSON" and period["state"] in ("now", "next")
        ),
        None,
    )
    return {"published": body["published"], "date": body["date"], "next_lesson": lesson}


def _fees(child, reads: _FeeReads) -> dict:
    """The child's balance, whether it is owed, and how many invoices are open."""
    entry = reads.entry(child)
    if not entry["available"]:
        raise FinanceUnavailable()
    balance = entry["balance"]
    return {
        "balance": balance,
        "status": "OWING" if balance > 0 else ("IN_CREDIT" if balance < 0 else "CLEAR"),
        "open_invoice_count": len(parent_fees.open_invoices(entry)),
    }


def _results(child) -> dict:
    """The latest term released to the family and its overall average, or none."""
    from schools.vs_student_portal.results import RELEASED, term_result

    body = term_result(child)
    if body["state"] != RELEASED:
        return {"released": False}
    return {
        "released": True,
        "term": body["term"],
        "overall_average": body["summary"]["overall_average"],
    }


def _child(child, enrolment, reads: _FeeReads) -> dict:
    from schools.vs_student_portal.home import attendance_block

    name = child.first_name
    return {
        "id": child.pk,
        "first_name": name,
        "class_name": enrolment.school_class.name if enrolment else "",
        "attendance": _block(
            "attendance", f"{name}'s attendance cannot be shown right now.",
            lambda: attendance_block(child),
        ),
        "fees": _block(
            "fees", parent_fees.unavailable_sentence(child), lambda: _fees(child, reads),
        ),
        "results": _block(
            "results", f"{name}'s results cannot be shown right now.", lambda: _results(child),
        ),
        "today": _block(
            "today", f"{name}'s classes today cannot be shown right now.", lambda: _today(child),
        ),
    }


def _family_fees(reads: _FeeReads) -> dict:
    """The family's outstanding balance and the total of its pay-all steps."""
    entries = reads.entries()
    total = parent_fees.combined(entries)
    if not total["available"]:
        raise FinanceUnavailable()
    steps = parent_fees.pay_all(reads.children, entries)
    return {"outstanding": total["outstanding"], "pay_all_total": steps["total"]}


def _notices(children, user) -> dict:
    """The newest notice addressed to any child, and how many the parent has not opened."""
    from schools.vs_notices.models import NoticeRead
    from schools.vs_notices.services.notices import (
        children_addressed,
        guardian_notices,
        pupil_placements,
    )

    from .serializers import ParentNoticeSerializer

    placements = pupil_placements(children)
    notices = guardian_notices(children, placements=placements)
    latest = notices.first()
    latest_data = None
    if latest is not None:
        context = {
            "read_ids": (
                {latest.pk} if NoticeRead.objects.filter(user=user, notice=latest).exists()
                else set()
            ),
            "children_for": {
                latest.pk: [c.first_name for c in children_addressed(latest, children, placements)],
            },
        }
        latest_data = ParentNoticeSerializer(latest, context=context).data
    return {
        "latest": latest_data,
        "unread_count": notices.exclude(reads__user=user).count(),
    }


def _notifications(user) -> dict:
    from vs_notifications.services.inbox import unread_in_app_count

    return {"unread_count": unread_in_app_count(user)}


def home(guardian, children, *, user) -> dict:
    """Every home block for the signed-in parent, each available or not on its own."""
    from vs_config.clock import branch_today

    from .services import child_enrolments

    placed = child_enrolments(children)
    reads = _FeeReads(children)
    return {
        "date": branch_today(guardian.tenant, None),
        "children": [_child(child, placed.get(child.pk), reads) for child in children],
        "fees": _block("fees", FAMILY_FEES_UNAVAILABLE, lambda: _family_fees(reads)),
        "notices": _block("notices", NOTICES_UNAVAILABLE, lambda: _notices(children, user)),
        "notifications": _block(
            "notifications", NOTIFICATIONS_UNAVAILABLE, lambda: _notifications(user),
        ),
    }
