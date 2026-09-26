"""A staff member's email under Field Access, on the Add form and on the record.

The sign-in address is declared open on create: every new account needs one,
so a role that may add staff sends it whatever its switch says. On an existing
record the Read switch decides, and it decides for every copy of the address
the record carries, the account block nested inside it included.

A registrar at each school may add and read staff, with the email switched off
entirely. Brightfield has two branches and Sunrise one.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.tests.deep_payload import walk
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)
from vs_user.models import User

from .base import ALL_KEYS, StaffFixture

EMAIL_KEY = "school.teachers.email"


class StaffEmailFieldAccessTests(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.teachers")
        cls.registrars = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            role = make_role(school, name=f"Registrar {label}", key=f"registrar_{label}")
            for key in ALL_KEYS:
                make_role_permission(role, cls.permissions[key])
            set_field_access(role, EMAIL_KEY, read=False, write=False)
            user = make_school_admin(
                None, email=f"registrar.{label}@staff-email.test", tenant=school.tenant,
            )
            make_assignment(school, user, role, branch=None)
            cls.registrars[label] = (school, user)

    def _domain(self, school):
        return "brightfield.test" if school.pk == self.school.pk else "sunrise.test"

    def test_a_registrar_with_the_email_hidden_still_adds_a_member_of_staff(self):
        for label, (school, registrar) in self.registrars.items():
            with self.subTest(school=label):
                email = f"funke.{label}@{self._domain(school)}"
                response = self.post(registrar, "staff-list", self.invite_body(
                    email=email, staff_number=f"STF/{label}/9",
                ))
                self.assertEqual(response.status_code, 201, response.data)
                self.assertTrue(User.objects.filter(tenant=school.tenant, email=email).exists())
                self.assertNotIn(email, str(response.data))

    def test_the_record_carries_the_address_nowhere_for_that_registrar(self):
        """Neither at the top of the record nor in the account block inside it."""
        for label, (school, registrar) in self.registrars.items():
            with self.subTest(school=label):
                staff = self.eze if label == "multi" else self.solo_staff
                response = self.get(registrar, "staff-detail", pk=staff.pk)
                self.assertEqual(response.status_code, 200, response.data)
                data = response.data["data"]
                self.assertIn("account", data)
                self.assertEqual(
                    [path for path, key, _ in walk(data) if key == "email"], [],
                )
                self.assertNotIn(staff.user.email, str(data))

    def test_an_admin_with_the_email_open_reads_both_copies(self):
        install_declared_fields("school.teachers")
        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)
        data = response.data["data"]
        self.assertEqual(data["email"], self.eze.user.email)
        self.assertEqual(data["account"]["email"], self.eze.user.email)

    def test_the_map_offers_the_hidden_email_to_the_add_form(self):
        for label, (school, registrar) in self.registrars.items():
            with self.subTest(school=label):
                entry = field_access_payload(registrar, school.tenant)["school.teachers"]
                self.assertEqual(entry["hidden"], ["email"])
                self.assertEqual(entry["open_on_create"], ["email"])
                self.assertEqual(entry["read_only"], [])
