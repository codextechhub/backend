"""A guardian's name in parts, and the review flag on a name split from one line.

A name typed on one line is split by a rule and flagged, because only somebody
who knows the family can say which word is the surname. The one-line name is
kept exactly as typed until a person confirms the parts; confirming composes it
from them.
"""
from __future__ import annotations

from schools.vs_students.models import Guardian
from schools.vs_students.names import split_full_name
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import StudentsFixture


class SplitRuleTests(StudentsFixture):
    def test_honorifics_are_set_aside_and_the_middle_words_kept(self):
        self.assertEqual(split_full_name("Mrs Ngozi Adaeze Okafor"), ("Ngozi", "Adaeze", "Okafor"))
        self.assertEqual(split_full_name("Chief Dr. Emeka Obi"), ("Emeka", "", "Obi"))
        self.assertEqual(split_full_name("Bisi"), ("Bisi", "", ""))
        self.assertEqual(split_full_name("  "), ("", "", ""))


class GuardianNameTests(StudentsFixture):
    def test_a_one_line_name_is_split_flagged_and_kept_as_typed(self):
        guardian = self.guardian(name="Mrs Ngozi Adaeze Okafor")
        guardian.refresh_from_db()
        self.assertEqual((guardian.first_name, guardian.middle_name, guardian.last_name),
                         ("Ngozi", "Adaeze", "Okafor"))
        self.assertTrue(guardian.name_needs_review)
        self.assertEqual(guardian.full_name, "Mrs Ngozi Adaeze Okafor")

    def test_a_name_given_in_parts_is_composed_and_not_flagged(self):
        guardian = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Ngozi", last_name="Okafor", phone="0803",
        )
        self.assertFalse(guardian.name_needs_review)
        self.assertEqual(guardian.full_name, "Ngozi Okafor")

    def test_sending_the_parts_unchanged_confirms_the_split(self):
        guardian = self.guardian(name="Mrs Ngozi Adaeze Okafor")
        response = self.patch(self.admin, "guardian-detail", {
            "first_name": "Ngozi", "middle_name": "Adaeze", "last_name": "Okafor",
        }, pk=guardian.pk)
        self.assertEqual(response.status_code, 200, response.data)
        guardian.refresh_from_db()
        self.assertFalse(guardian.name_needs_review)
        self.assertEqual(guardian.full_name, "Ngozi Adaeze Okafor")
        self.assertIn("confirmed", response.data["message"])

    def test_a_blank_last_name_is_refused(self):
        guardian = self.guardian()
        response = self.patch(self.admin, "guardian-detail", {"last_name": " "}, pk=guardian.pk)
        self.assertEqual(response.status_code, 400)

    def test_the_directory_lists_only_names_awaiting_review_when_asked(self):
        pupil = self.student()
        flagged = self.guardian(name="Mrs Ngozi Okafor", phone="0801", email="a@x.ng")
        confirmed = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Emeka", last_name="Obi", phone="0802",
        )
        self.link(pupil, flagged)
        self.link(pupil, confirmed, primary=False)
        rows = self.get(self.admin, "guardian-list", {"name_review": "true"}).data["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([row["id"] for row in rows], [flagged.pk])
        self.assertTrue(rows[0]["name_needs_review"])

    def test_enrolment_accepts_a_guardian_in_parts(self):
        body = self.enrolment_body(guardians=[{
            "first_name": "Amina", "last_name": "Yusuf", "phone": "08115550199",
            "relationship": "MOTHER", "is_primary": True,
        }])
        response = self.post(self.admin, "student-list", body)
        self.assertEqual(response.status_code, 201, response.data)
        guardian = Guardian.all_objects.get(phone="08115550199")
        self.assertEqual(guardian.full_name, "Amina Yusuf")
        self.assertFalse(guardian.name_needs_review)


class GuardianNameSwitchTests(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.guardians")
        cls.clerk_role = make_role(cls.school, name="Clerk", key="clerk")
        for key in ("school.students.view", "school.students.update"):
            make_role_permission(cls.clerk_role, cls.permissions[key])
        set_field_access(cls.clerk_role, "school.guardians.last_name", read=True, write=False)
        set_field_access(cls.clerk_role, "school.guardians.photo", read=True, write=False)
        cls.clerk = make_school_admin(None, email="clerk@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.clerk, cls.clerk_role, branch=None)

    def test_changing_a_read_only_name_part_is_refused(self):
        guardian = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Ngozi", last_name="Okafor", phone="0803",
        )
        response = self.patch(self.clerk, "guardian-detail", {"last_name": "Obi"}, pk=guardian.pk)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_a_photograph_is_refused_without_its_write_switch(self):
        guardian = self.guardian()
        response = self.delete(self.clerk, "guardian-photo", pk=guardian.pk)
        self.assertEqual(response.status_code, 403)
