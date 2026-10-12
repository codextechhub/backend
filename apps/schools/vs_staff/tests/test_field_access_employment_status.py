"""A member of staff's employment status under one Field Access switch.

Brightfield (two branches) and Sunrise (one) each have a registrar who holds
every staff key with the employment status switched off entirely, and a
deputy who may read it but not change it. The status is one fact under many
names (the stored value, its label, what the row reads as, on the roll or
left, the lifecycle strip, an employment event's from and to), so the hidden
registrar must meet none of them anywhere: on the directory row, the record,
the palette hit, the branch roster, the history, the header counts or the
status filter. The lifecycle endpoint and the invitation revoke, the two
routes that write the status, ask the Write switch as well as the key.
"""
from __future__ import annotations

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.field_access import EMPLOYMENT_STATUS_FIELD, EMPLOYMENT_STATUS_NAMES
from vs_rbac.tests.deep_payload import walk
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
)

from .base import ALL_KEYS, StaffFixture

NAMES = set(EMPLOYMENT_STATUS_NAMES)


def _status_keys(payload):
    return [path for path, key, _ in walk(payload) if key in NAMES]


class EmploymentStatusFieldAccessTests(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_rbac.tests.helpers import set_field_access

        install_declared_fields("school.teachers")
        cls.hidden = {}
        cls.read_only = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            for bucket, read in ((cls.hidden, False), (cls.read_only, True)):
                key = f"{'hidden' if not read else 'readonly'}_{label}"
                role = make_role(school, name=key, key=key)
                for perm in ALL_KEYS:
                    make_role_permission(role, cls.permissions[perm])
                set_field_access(role, EMPLOYMENT_STATUS_FIELD, read=read, write=False)
                user = make_school_admin(None, email=f"{key}@status.test", tenant=school.tenant)
                make_assignment(school, user, role, branch=None)
                bucket[label] = user
        cls.staff_for = {"multi": cls.eze, "single": cls.solo_staff}

    # -- default access -------------------------------------------------

    def test_an_admin_with_the_switch_open_reads_every_form_and_filters(self):
        response = self.get(self.admin, "staff-list", {"employment_status": "ACTIVE"})
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data["data"] if r["id"] == self.eze.pk)
        self.assertEqual(row["employment_status"], EmploymentStatus.ACTIVE)
        self.assertTrue(row["on_roll"])
        self.assertIn("by_employment_status", response.data["counts"])
        self.assertIn("currently_employed", response.data["counts"])
        detail = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(detail["lifecycle"]["current"], EmploymentStatus.ACTIVE)

    # -- hidden: every read path ---------------------------------------

    def test_the_directory_carries_no_status_and_no_breakdown(self):
        for label, user in self.hidden.items():
            with self.subTest(school=label):
                response = self.get(user, "staff-list")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data["data"])
                self.assertEqual(_status_keys(response.data["data"]), [])
                counts = response.data["counts"]
                self.assertNotIn("by_employment_status", counts)
                self.assertNotIn("currently_employed", counts)
                self.assertIn("total", counts)

    def test_the_status_filter_is_refused(self):
        for label, user in self.hidden.items():
            with self.subTest(school=label):
                response = self.get(user, "staff-list", {"employment_status": "TERMINATED"})
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_read_denied")

    def test_the_record_carries_the_status_nowhere(self):
        for label, user in self.hidden.items():
            with self.subTest(school=label):
                response = self.get(user, "staff-detail", pk=self.staff_for[label].pk)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(_status_keys(response.data["data"]), [])

    def test_the_palette_hit_carries_no_status(self):
        for label, user in self.hidden.items():
            staff = self.staff_for[label]
            with self.subTest(school=label):
                response = self.get(user, "staff-search", {"q": staff.user.first_name})
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data["data"])
                self.assertEqual(_status_keys(response.data["data"]), [])

    def test_the_branch_roster_carries_no_status(self):
        response = self.get(self.hidden["multi"], "staff-roster", {"branch": self.lekki.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(_status_keys(response.data["data"]), [])

    def test_the_history_has_no_employment_half(self):
        self.post(
            self.admin, "staff-status",
            {"to_status": "SUSPENDED", "reason": "Internal review"}, pk=self.eze.pk,
        )
        admin_entries = self.get(self.admin, "staff-history", pk=self.eze.pk).data["data"]["entries"]
        self.assertIn("employment", {row["kind"] for row in admin_entries})
        entries = self.get(
            self.hidden["multi"], "staff-history", pk=self.eze.pk,
        ).data["data"]["entries"]
        self.assertNotIn("employment", {row["kind"] for row in entries})
        self.assertEqual(_status_keys(entries), [])

    def test_the_status_drawer_is_refused(self):
        for label, user in self.hidden.items():
            with self.subTest(school=label):
                response = self.get(user, "staff-status", pk=self.staff_for[label].pk)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_read_denied")

    def test_a_suspended_account_is_flagged_without_comparing_employment(self):
        from vs_user.models import User

        user = self.eze.user
        user.status = User.Status.SUSPENDED
        user.save(update_fields=["status"])
        row = next(
            r for r in self.get(self.hidden["multi"], "staff-list").data["data"]
            if r["id"] == self.eze.pk
        )
        self.assertEqual(row["account_flag"]["code"], "ACCOUNT_SUSPENDED")
        self.assertNotIn("work here", row["account_flag"]["note"])

    # -- write paths ----------------------------------------------------

    def test_a_status_change_is_refused_without_the_write_switch(self):
        for bucket in (self.hidden, self.read_only):
            for label, user in bucket.items():
                staff = self.staff_for[label]
                with self.subTest(school=label, user=user.email):
                    response = self.post(
                        user, "staff-status",
                        {"to_status": "SUSPENDED", "reason": "Internal review"}, pk=staff.pk,
                    )
                    self.assertEqual(response.status_code, 403, response.data)
                    self.assertEqual(response.data["error"]["code"], "field_write_denied")
                    staff.refresh_from_db()
                    self.assertEqual(staff.employment_status, EmploymentStatus.ACTIVE)

    def test_a_read_only_reader_still_opens_the_drawer(self):
        response = self.get(self.read_only["multi"], "staff-status", pk=self.eze.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["employment_status"], EmploymentStatus.ACTIVE)

    def test_revoking_an_invitation_is_refused_without_the_write_switch(self):
        invited = self.make_staff(
            "invited@brightfield.test", "Tola", "Bello", branch=self.lekki,
            status=EmploymentStatus.INVITED,
        )
        response = self.post(
            self.read_only["multi"], "staff-invitation-revoke",
            {"reason": "Wrong email address"}, pk=invited.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        invited.refresh_from_db()
        self.assertEqual(invited.employment_status, EmploymentStatus.INVITED)
