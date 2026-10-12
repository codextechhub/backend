"""Brightfield and Sunrise with people to put on a staff register.

Builds on :class:`AttendanceFixture` (Monday 13 October 2025, Brightfield with
Lekki and Ikeja, Sunrise with one branch). Brightfield's staff, by home branch:

* Lekki: Mr Eze (leads JSS1 A), Ms Bello (leads JSS1 B), Funke Adeyemi (Bursar)
  and Peter Achebe (English teacher);
* Ikeja: Emeka Nnamdi (leads SSS2 Science) and Tunde Bakare;
* no home branch: Adaeze Nwankwo, the registrar.

Sunrise's staff: Bola Ade (leads Basic 3), Halima Danjuma at Main and Kola
Ayanwale with no branch, who at a one-branch school is Main's.

The people who read and mark the registers are separate accounts so a test about
who is on a register never depends on who is looking at it: a whole-school
registrar, a Lekki and an Ikeja branch admin, one who may only view, one who may
only mark, and the usual stranger. Sunrise has its own whole-school registrar.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_staff.constants import PERM_LEAVE_VIEW
from schools.vs_staff.models import LeaveRequest, StaffProfile
from vs_rbac.tests.helpers import make_assignment, make_school_admin

from ..constants import PERM_STAFF_MARK, PERM_STAFF_VIEW
from ..models import StaffMark
from .base import TODAY, AttendanceFixture, make_key_role

BOTH = (PERM_STAFF_VIEW, PERM_STAFF_MARK)
WITH_LEAVE = (*BOTH, PERM_LEAVE_VIEW)


class StaffFixture(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        both = cls.staff_role = make_key_role(
            cls.tenant, "Staff register", "staff_register", WITH_LEAVE,
        )
        viewer = make_key_role(cls.tenant, "Staff viewer", "staff_viewer", (PERM_STAFF_VIEW,))
        marker = make_key_role(cls.tenant, "Staff marker", "staff_marker", (PERM_STAFF_MARK,))

        cls.registrar = cls.make_actor(None, "registrar@brightfield.test", "Rita", "Okon", both)
        cls.lekki_hr = cls.make_actor(cls.lekki, "hr@lekki.test", "Lola", "Ade", both)
        cls.ikeja_hr = cls.make_actor(cls.ikeja, "hr@ikeja.test", "Ike", "Obi", both)
        cls.viewer = cls.make_actor(None, "viewer@brightfield.test", "Vera", "Look", viewer)
        cls.marker = cls.make_actor(None, "marker@brightfield.test", "Mark", "Ing", marker)

        cls.funke = cls.make_person("Funke", "Adeyemi", cls.lekki, title="Bursar")
        cls.peter = cls.make_person("Peter", "Achebe", cls.lekki, title="English Teacher")
        cls.emeka_n = cls.make_person("Emeka", "Nnamdi", cls.ikeja, title="Teacher")
        cls.tunde_b = cls.make_person("Tunde", "Bakare", cls.ikeja, title="Economics Teacher")
        cls.adaeze = cls.make_person("Adaeze", "Nwankwo", None, title="Registrar")
        cls.sss2.class_teacher = cls.emeka_n
        cls.sss2.save(update_fields=["class_teacher"])

        solo_both = make_key_role(cls.solo, "Staff register", "staff_register", WITH_LEAVE)
        cls.solo_registrar = make_school_admin(
            None, email="registrar@sunrise.test", tenant=cls.solo,
            first_name="Soji", last_name="Eze",
        )
        make_assignment(cls.solo, cls.solo_registrar, solo_both, branch=None)
        cls.halima = cls.make_person(
            "Halima", "Danjuma", cls.solo_branch, tenant=cls.solo, title="Finance Officer",
        )
        cls.kola = cls.make_person(
            "Kola", "Ayanwale", None, tenant=cls.solo, title="Mathematics Teacher",
        )

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def make_actor(cls, branch, email, first, last, role):
        user = make_school_admin(
            branch, email=email, first_name=first, last_name=last,
            **({"tenant": cls.tenant} if branch is None else {}),
        )
        make_assignment(cls.tenant, user, role, branch=branch)
        return user

    @classmethod
    def make_person(cls, first, last, branch, *, tenant=None, title="", **fields):
        tenant = tenant or cls.tenant
        user = make_school_admin(
            branch, email=f"{first}.{last}@{tenant.slug}.staff.test".lower(),
            first_name=first, last_name=last,
            **({"tenant": tenant} if branch is None else {}),
        )
        fields.setdefault("employment_status", "ACTIVE")
        return StaffProfile.all_objects.create(
            tenant=tenant, user=user, branch=branch, job_title=title, **fields,
        )

    @classmethod
    def approve_leave(cls, person, start, end, leave_type="MATERNITY"):
        return LeaveRequest.all_objects.create(
            tenant=person.tenant, staff=person, leave_type=leave_type,
            start_date=start, end_date=end, days=5, status="APPROVED",
        )

    # ── HTTP ──────────────────────────────────────────────────────────────

    def staff_url(self, name, branch, on_date=TODAY, **extra):
        return reverse(name, kwargs={"branch_id": branch.pk, "on_date": str(on_date), **extra})

    def staff_day(self, user, on_date=TODAY, **params):
        return TenantAPIClient(user).get(
            reverse("attendance-staff-registers", kwargs={"on_date": str(on_date)}), params,
        )

    def mark_staff(self, user, branch, person, mark, on_date=TODAY, **body):
        return TenantAPIClient(user).put(
            self.staff_url("attendance-staff-mark", branch, on_date, staff_id=person.pk),
            {"mark": mark, **body}, format="json",
        )

    def fill_staff(self, user, branch, on_date=TODAY):
        return TenantAPIClient(user).post(
            self.staff_url("attendance-staff-fill-present", branch, on_date), {}, format="json",
        )

    def submit_staff(self, user, branch, on_date=TODAY):
        return TenantAPIClient(user).post(
            self.staff_url("attendance-staff-submit", branch, on_date), {}, format="json",
        )

    def group(self, response, branch):
        """The group for *branch* in a day read, or in a write's re-read body."""
        body = self.data(response)
        if "groups" not in body:
            return body
        return next(g for g in body["groups"] if g["branch"]["id"] == branch.pk)

    def staff_rows(self, response, branch):
        return {row["staff"]["id"]: row for row in self.group(response, branch)["rows"]}

    def lekki_everyone_marked(self, **marks):
        """Mark every markable Lekki person Present, with *marks* (by last name, lower case) overriding."""
        for person in (self.peter, self.funke, self.bello_staff, self.eze_staff):
            kind = marks.get(person.user.last_name.lower(), "P")
            body = {"arrived_at": "08:25"} if kind == "L" else {}
            response = self.mark_staff(self.registrar, self.lekki, person, kind, **body)
            assert response.status_code == 200, response.data

    def stored_mark(self, person, on_date=TODAY):
        return StaffMark.objects.get(staff=person, date=on_date)

    @staticmethod
    def days_ago(count):
        return TODAY - dt.timedelta(days=count)
