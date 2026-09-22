"""A guardian's phone number is set freely when the guardian is added.

Every route that adds a guardian requires a phone number, so a role that may
not change one would otherwise never be able to add a guardian at all. The
phone is declared open on create: whoever adds the guardian sets it, through a
row of the enrol form or the link form on a pupil's record, and only
correcting it on an existing guardian asks the Write switch.

Two admissions officers per school: one who reads guardians' phone numbers but
may not change them, and one from whom the phone is hidden altogether. Both
are proven against Brightfield, with two branches, and Sunrise, with one. The
email address, home address and occupation are optional on every create route
and stay governed by their switches there.
"""
from __future__ import annotations

from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .test_field_write_paths import ENROLMENT_DATE_KEY, OFFICER_KEYS, _WritePathFixture

PHONE_KEY = "school.guardians.phone"
EMAIL_KEY = "school.guardians.email"


class _GuardianPhoneFixture(_WritePathFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.guardians")
        cls.phone_callers = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.phone_callers[label] = {
                "reads": cls._officer(school, label, "reads", read=True),
                "hidden": cls._officer(school, label, "hidden", read=False),
            }

    @classmethod
    def _officer(cls, school, label, name, *, read):
        """An officer who may enrol and correct records, but not a guardian's phone."""
        role = make_role(
            school, name=f"Phone {name} {label}", key=f"phone_{name}_{label}",
        )
        for key in OFFICER_KEYS + ("school.students.update",):
            make_role_permission(role, cls.permissions[key])
        set_field_access(role, ENROLMENT_DATE_KEY, read=True, write=False)
        set_field_access(role, PHONE_KEY, read=read, write=False)
        set_field_access(role, EMAIL_KEY, read=True, write=False)
        user = make_school_admin(
            None, email=f"phone.{name}.{label}@guardian-phone.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    def callers(self):
        for label, tenant, body in self.shapes():
            for kind, user in self.phone_callers[label].items():
                yield label, kind, tenant, body, user


class GuardianPhoneOnCreateTests(_GuardianPhoneFixture):
    def test_enrolling_with_a_new_guardians_phone_saves_it(self):
        for label, kind, tenant, body, user in self.callers():
            with self.subTest(school=label, caller=kind):
                phone = f"0803{label[:1]}{kind[:1]}000{len(kind)}"
                payload = body()
                payload["first_name"] = f"Pupil {kind}"
                payload["guardians"][0].update({
                    "phone": phone, "email": "", "full_name": f"Guardian {kind} {label}",
                })
                response = self.post(user, "student-list", payload)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertTrue(
                    Guardian.all_objects.filter(tenant=tenant, phone=phone).exists(),
                )

    def test_linking_a_new_guardian_with_a_phone_saves_it(self):
        for label, kind, tenant, _body, user in self.callers():
            with self.subTest(school=label, caller=kind):
                pupil = self.student(
                    tenant=tenant,
                    branch=self.lekki if label == "multi" else self.solo_branch,
                    first=f"Linked {kind}",
                )
                phone = f"0805{label[:1]}{kind[:1]}111{len(kind)}"
                response = self.post(user, "student-guardians", {
                    "full_name": f"Aunt {kind}", "phone": phone,
                    "relationship": Relationship.OTHER, "is_primary": False,
                }, pk=pupil.pk)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertTrue(
                    Guardian.all_objects.filter(tenant=tenant, phone=phone).exists(),
                )

    def test_correcting_that_phone_later_is_refused_and_nothing_changes(self):
        for label, kind, tenant, _body, user in self.callers():
            with self.subTest(school=label, caller=kind):
                guardian = self.guardian(
                    tenant=tenant, phone=f"0807{label[:1]}{kind[:1]}2222",
                    email=f"{kind}.{label}@guardian-phone.test",
                )
                response = self.patch(
                    user, "guardian-detail", {"phone": "08099999999"}, pk=guardian.pk,
                )
                self.assertFieldWriteDenied(response)
                guardian.refresh_from_db()
                self.assertNotEqual(guardian.phone, "08099999999")

    def test_a_new_guardians_email_without_its_switch_is_still_refused(self):
        """Only the phone is open on create; the email is optional and stays governed."""
        for label, kind, tenant, body, user in self.callers():
            with self.subTest(school=label, caller=kind):
                before = Guardian.all_objects.filter(tenant=tenant).count()
                payload = body()
                payload["guardians"][0]["email"] = f"new.{kind}.{label}@guardian-phone.test"
                response = self.post(user, "student-list", payload)
                self.assertFieldWriteDenied(response)
                self.assertEqual(self.refused_fields(response), {"email"})
                self.assertEqual(Guardian.all_objects.filter(tenant=tenant).count(), before)

    def test_the_map_offers_the_phone_to_the_add_form_for_both_callers(self):
        from vs_rbac.field_enforcement import field_access_payload

        for label, kind, tenant, _body, user in self.callers():
            with self.subTest(school=label, caller=kind):
                entry = field_access_payload(user, tenant)["school.guardians"]
                self.assertIn("phone", entry["open_on_create"])
                self.assertIn("phone", entry["read_only" if kind == "reads" else "hidden"])
                self.assertNotIn("email", entry["open_on_create"])
