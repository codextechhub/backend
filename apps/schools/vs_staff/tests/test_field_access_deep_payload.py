"""No staff detail reaches a caller who may read none of them, at any depth.

Every declared surface of ``school.teachers`` is rendered on a real member of
staff whose date of birth, gender, phone number and email address are all
filled in, as a caller whose role has every one of those switched off. The
whole payload is walked, so the account block nested in the record is held to
the same switch as the record's own top level.

Both shapes of school: Brightfield has two branches, so the record carries its
branch name, and Sunrise has one, so that column recedes. The switches do not
depend on the branch, and rendering both proves the recede rule does not open a
path either.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.models import StaffProfile
from schools.vs_staff.serializers import STAFF_LIST_PREFETCH
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .base import StaffFixture

_SURFACE = "schools.vs_staff.serializers."


class StaffDeepPayloadTests(DeepPayloadChecks, StaffFixture):
    resources = frozenset({"school.teachers"})
    covers = frozenset({
        _SURFACE + "StaffListSerializer",
        _SURFACE + "StaffDetailSerializer",
        _SURFACE + "StaffUpdateSerializer",
        _SURFACE + "StaffCreateSerializer",
        _SURFACE + "AccountStateSerializer",
        _SURFACE + "StaffSearchHitSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for staff in (cls.eze, cls.solo_staff):
            staff.date_of_birth = dt.date(1988, 4, 12)
            staff.save(update_fields=["date_of_birth"])
            staff.user.phone = "08031234567"
            staff.user.gender = "MALE"
            staff.user.save(update_fields=["phone", "gender"])

    def _shapes(self):
        return (
            (self.eze, self.tenant, True),
            (self.solo_staff, self.solo.tenant, False),
        )

    def samples(self):
        samples = []
        for staff, tenant, multi_branch in self._shapes():
            staff = StaffProfile.all_objects.select_related("user", "branch").get(pk=staff.pk)
            context = {"tenant": tenant, "multi_branch": multi_branch, "on_leave_ids": set()}
            rows = StaffProfile.all_objects.filter(pk=staff.pk).prefetch_related(
                *STAFF_LIST_PREFETCH,
            )
            user = staff.user
            samples += [
                Sample(_SURFACE + "StaffListSerializer", rows, context, many=True,
                       tenant=tenant),
                Sample(_SURFACE + "StaffDetailSerializer", staff, context, tenant=tenant),
                Sample(_SURFACE + "StaffUpdateSerializer", staff, context, tenant=tenant),
                Sample(_SURFACE + "AccountStateSerializer", user, context, tenant=tenant),
                Sample(_SURFACE + "StaffSearchHitSerializer", [staff], context,
                       many=True, tenant=tenant),
                # The Add form is never rendered from a record, so it is given
                # the values an Add form for this person would carry.
                Sample(_SURFACE + "StaffCreateSerializer", {
                    "first_name": user.first_name, "last_name": user.last_name,
                    "email": user.email, "phone": user.phone, "gender": user.gender,
                    "role": "teacher", "role_branch": None,
                    "staff_number": staff.staff_number, "job_title": staff.job_title,
                    "employment_type": staff.employment_type,
                    "hire_date": staff.hire_date, "branch": None,
                    "middle_name": staff.middle_name,
                    "date_of_birth": staff.date_of_birth, "photo": None,
                    "qualifications": [], "subjects": [], "classes": [],
                }, context, tenant=tenant),
            ]
        return samples


class LeaveDeepPayloadTests(DeepPayloadChecks, StaffFixture):
    """No leave detail reaches a caller who may read none of them, at any depth.

    Every declared surface of ``school.leave`` is rendered on a real request
    with a note, over its allowance and waiting with an approver, as a caller
    whose role has every leave switch off: the list row, the echo of a write,
    the bodies filing and correcting read, the hand-built page that carries the
    balance and days taken beside the requests, and the preview a form shows
    while dates are picked. Both shapes of school are rendered.
    """

    resources = frozenset({"school.leave"})
    covers = frozenset({
        _SURFACE + "LeaveSerializer",
        _SURFACE + "LeaveWriteSerializer",
        _SURFACE + "LeaveUpdateSerializer",
        _SURFACE + "StaffLeaveSerializer",
        _SURFACE + "LeavePreviewResultSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_staff.models import LeaveRequest

        cls.requests = {}
        for staff in (cls.eze, cls.solo_staff):
            cls.requests[staff.pk] = LeaveRequest.all_objects.create(
                tenant=staff.tenant, staff=staff, leave_type="SICK",
                start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 6), days=5,
                resumption_date=dt.date(2026, 3, 9), over_allowance_by=2,
                status="PENDING", note="Recovering from surgery.",
            )

    def samples(self):
        samples = []
        for staff, tenant in ((self.eze, self.tenant), (self.solo_staff, self.solo.tenant)):
            row = self.requests[staff.pk]
            context = {
                "leave_approvals": {row.pk: {
                    "instance_id": 1, "stage": "Line manager", "pending_with": ["Ada Reader"],
                }},
                "leave_changes": {},
            }
            body = {
                "leave_type": "SICK", "start_date": dt.date(2026, 3, 2),
                "end_date": dt.date(2026, 3, 6), "days": 5, "note": "Recovering.",
            }
            samples += [
                Sample(_SURFACE + "LeaveSerializer", [row], context, many=True, tenant=tenant),
                Sample(_SURFACE + "LeaveSerializer", row, context, tenant=tenant),
                Sample(_SURFACE + "LeaveWriteSerializer", body, tenant=tenant),
                Sample(_SURFACE + "LeaveUpdateSerializer", body, tenant=tenant),
                Sample(_SURFACE + "StaffLeaveSerializer", {
                    "leave": [row], "leave_group": {"id": "g1", "name": "Teaching staff"},
                    "days_taken": [{"leave_type": "SICK", "days": 5}],
                    "balances": [{
                        "leave_type": "SICK", "label": "Sick", "allowance": 10,
                        "taken": 5, "pending": 0, "remaining": 5,
                    }],
                    "balance_session": {
                        "id": 1, "name": "2025/2026",
                        "start_date": dt.date(2025, 9, 1), "end_date": dt.date(2026, 7, 31),
                    },
                    "balance_note": "Allowances are per academic session.",
                }, context, tenant=tenant),
                Sample(_SURFACE + "LeavePreviewResultSerializer", {
                    "days": 5, "resumption_date": dt.date(2026, 3, 9),
                    "leave_type": "SICK", "allowance": 10, "taken": 5, "pending": 0,
                    "remaining": 5, "remaining_after": 0, "over_allowance_by": 0,
                    "session": {"id": 1, "name": "2025/2026"},
                }, tenant=tenant),
            ]
        return samples
