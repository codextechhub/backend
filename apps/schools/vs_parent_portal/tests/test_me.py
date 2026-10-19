"""The parent's shell and one child's record.

Brightfield has three branches, so each child shows the branch they attend and
a parent with children at two of them sees both; Sunrise has one, so the
branch is absent from every payload. The record never carries another
guardian's contact details.
"""
from __future__ import annotations

import json

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Relationship

from .base import HUSBAND_ADDRESS, HUSBAND_EMAIL, HUSBAND_PHONE, ParentFixture

ME, CHILD = "parent-portal-me", "parent-portal-child"


class MeTests(ParentFixture):
    def me(self, user):
        return TenantAPIClient(user).get(reverse(ME)).data["data"]

    def test_a_parent_sees_their_children_in_the_order_of_age(self):
        children = self.me(self.nwosu_user)["children"]
        self.assertEqual([c["first_name"] for c in children], ["Chiamaka", "Tobi", "Folu"])
        first = children[0]
        self.assertEqual(first["full_name"], "Chiamaka Nwosu")
        self.assertEqual(first["initials"], "CN")
        self.assertEqual(first["admission_number"], "BFS/001")
        self.assertEqual(first["class_name"], "JSS1 A")
        self.assertEqual(first["relationship"], "Mother")
        self.assertEqual(children[2]["class_name"], "")

    def test_children_at_two_branches_each_show_their_own(self):
        data = self.me(self.nwosu_user)
        self.assertTrue(data["show_branch"])
        branches = {c["first_name"]: c["branch"]["name"] for c in data["children"]}
        self.assertEqual(branches, {"Chiamaka": "Lekki", "Tobi": "Ikeja", "Folu": "Epe"})

    def test_a_one_branch_school_has_no_branch_anywhere(self):
        data = self.me(self.ade_user)
        self.assertFalse(data["show_branch"])
        self.assertEqual([c["first_name"] for c in data["children"]], ["Bola"])
        self.assertNotIn("branch", data["children"][0])
        record = TenantAPIClient(self.ade_user).get(
            reverse(CHILD, kwargs={"child_id": self.bola.pk}),
        ).data["data"]
        self.assertNotIn("branch", record)

    def test_the_relationship_is_the_one_on_each_link(self):
        guardian = self.nwosu
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(guardian=guardian, student=self.tobi).update(
            relationship=Relationship.OTHER, relationship_detail="Sponsor",
        )
        labels = {c["first_name"]: c["relationship"] for c in self.me(self.nwosu_user)["children"]}
        self.assertEqual(labels["Tobi"], "Sponsor")
        self.assertEqual(labels["Chiamaka"], "Mother")

    def test_me_costs_the_same_for_one_child_as_for_three(self):
        one, three = TenantAPIClient(self.yusuf_user), TenantAPIClient(self.nwosu_user)
        one.get(reverse(ME))
        three.get(reverse(ME))
        with CaptureQueriesContext(connection) as single:
            self.assertEqual(len(one.get(reverse(ME)).data["data"]["children"]), 1)
        with CaptureQueriesContext(connection) as triple:
            self.assertEqual(len(three.get(reverse(ME)).data["data"]["children"]), 3)
        self.assertEqual(len(single), len(triple))


class ChildRecordTests(ParentFixture):
    def record(self, pupil, user=None):
        response = TenantAPIClient(user or self.nwosu_user).get(
            reverse(CHILD, kwargs={"child_id": pupil.pk}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_a_parent_reads_their_childs_record(self):
        data = self.record(self.chiamaka).json()["data"]
        self.assertEqual(data["full_name"], "Chiamaka Nwosu")
        self.assertEqual(data["admission_number"], "BFS/001")
        self.assertEqual(data["class_name"], "JSS1 A")
        self.assertEqual(data["level_name"], "JSS1")
        self.assertEqual(data["session"]["name"], "2025/2026")
        self.assertEqual(data["date_of_birth"], "2013-04-18")
        self.assertEqual(data["branch"]["name"], "Lekki")
        self.assertFalse(data["class_is_next_year"])

    def test_other_guardians_appear_by_name_and_relationship_only(self):
        data = self.record(self.chiamaka).data["data"]
        self.assertEqual(
            data["other_guardians"],
            [{"name": "Chukwudi Nwosu", "relationship": "Father"}],
        )
        body = self.record(self.chiamaka).content.decode()
        for private in (HUSBAND_PHONE, HUSBAND_EMAIL, HUSBAND_ADDRESS):
            self.assertNotIn(private, body)

    def test_a_child_with_no_other_guardian_lists_none(self):
        self.assertEqual(self.record(self.folu).data["data"]["other_guardians"], [])

    def test_no_medical_or_staff_field_is_carried(self):
        keys = json.dumps(self.record(self.chiamaka).json()["data"]).lower()
        for hidden in ("medical", "allergy", "blood", "note", "phone", "email", "address"):
            self.assertNotIn(hidden, keys)
