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
