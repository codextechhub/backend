"""A parent's notices: the union of what each of their children is sent.

Mrs. Ngozi Pupil has Tunde (Lekki, JSS1 B) and Kemi (Ikeja, JSS1 C). Chiamaka
(Lekki, JSS1 A) is another family's child. A notice for JSS1 A is not in
Mrs. Pupil's feed; a school-wide one is, once, naming both her children.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_notices.models import NoticeRead
from schools.vs_notices.services import notices as service
from schools.vs_notices.tests import test_notices as notices_tests
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian
from vs_rbac.tests.helpers import make_school_admin

FEED = "parent-portal-notices"


class ParentNoticesFixture(notices_tests.NoticesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mother_user = make_school_admin(
            None, email="ngozi.pupil@example.ng", tenant=cls.tenant,
            first_name="Ngozi", last_name="Pupil",
        )
        mother = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ngozi", last_name="Pupil",
            phone="08110000010", user=cls.mother_user,
        )
        for pupil in (cls.tunde, cls.kemi):
            cls.link(cls, pupil, mother, primary=False, relationship=Relationship.MOTHER)

        cls.father_user = make_school_admin(
            None, email="emeka.pupil@example.ng", tenant=cls.tenant,
            first_name="Emeka", last_name="Pupil",
        )
        father = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Emeka", last_name="Pupil",
            phone="08110000011", user=cls.father_user,
        )
        cls.link(cls, cls.tunde, father, primary=True, relationship=Relationship.FATHER)

        cls.whole_school = cls.publish("Sports day", [{"kind": "SCHOOL"}])
        cls.for_jss1a = cls.publish("JSS1 A trip", [{"kind": "CLASS", "id": cls.shared_class.pk}])
        cls.for_jss1b = cls.publish("JSS1 B test", [{"kind": "CLASS", "id": cls.lekki_class.pk}])
        cls.for_ikeja = cls.publish("Ikeja assembly", [{"kind": "BRANCH", "id": cls.ikeja.pk}])
        cls.for_jss1 = cls.publish("JSS1 books", [{"kind": "LEVEL", "id": cls.jss1.pk}])

    @classmethod
    def publish(cls, title, audience):
        return service.create(
            cls.tenant, actor=cls.admin, title=title, body="Please read.",
            audience=audience, publish_now=True,
        )

    def feed_of(self, user):
        response = TenantAPIClient(user).get(reverse(FEED))
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def titles(self, user):
        return {row["title"] for row in self.feed_of(user).data["data"]}


class ParentFeedTests(ParentNoticesFixture):
    def test_a_notice_for_a_class_none_of_the_children_sit_in_is_absent(self):
        self.assertNotIn("JSS1 A trip", self.titles(self.mother_user))

    def test_the_feed_is_the_union_of_every_childs_audience(self):
        self.assertEqual(
            self.titles(self.mother_user),
            {"Sports day", "JSS1 B test", "Ikeja assembly", "JSS1 books"},
        )

    def test_a_school_wide_notice_appears_once_for_two_children(self):
        rows = [r for r in self.feed_of(self.mother_user).data["data"] if r["title"] == "Sports day"]
        self.assertEqual(len(rows), 1)

    def test_each_notice_names_the_children_it_is_for(self):
        by_title = {r["title"]: r["children"] for r in self.feed_of(self.mother_user).data["data"]}
        self.assertEqual(by_title["Sports day"], ["Tunde", "Kemi"])
        self.assertEqual(by_title["JSS1 B test"], ["Tunde"])
        self.assertEqual(by_title["Ikeja assembly"], ["Kemi"])
        self.assertEqual(by_title["JSS1 books"], ["Tunde", "Kemi"])

    def test_a_parent_of_one_child_sees_only_that_childs_notices(self):
        self.assertEqual(
            self.titles(self.father_user), {"Sports day", "JSS1 B test", "JSS1 books"},
        )

    def test_a_draft_and_a_withdrawn_notice_are_not_in_the_feed(self):
        service.create(
            self.tenant, actor=self.admin, title="Draft", body="x", audience=[{"kind": "SCHOOL"}],
        )
        gone = self.publish("Cancelled", [{"kind": "SCHOOL"}])
        service.withdraw(gone, actor=self.admin)
        titles = self.titles(self.mother_user)
        self.assertNotIn("Draft", titles)
        self.assertNotIn("Cancelled", titles)

    def test_a_feed_with_nothing_is_an_empty_list_with_a_zero_count(self):
        lone = make_school_admin(
            None, email="lone@example.ng", tenant=self.tenant, first_name="Lone", last_name="Parent",
        )
        guardian = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Lone", last_name="Parent", phone="08110000012", user=lone,
        )
        self.link(self.zainab_like(), guardian, relationship=Relationship.MOTHER)
        service.withdraw(self.whole_school, actor=self.admin)
        service.withdraw(self.for_jss1, actor=self.admin)
        service.withdraw(self.for_ikeja, actor=self.admin)
        response = self.feed_of(lone)
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["unread_count"], 0)

    def zainab_like(self):
        return self.student(first="Zed", last="Alone", number="BFS/9", branch=self.ikeja)

    def test_a_guardian_with_no_child_on_the_roll_gets_an_empty_feed(self):
        from schools.vs_students.constants import StudentStatus

        for pupil in (self.tunde, self.kemi):
            pupil.status = StudentStatus.WITHDRAWN
            pupil.save(update_fields=["status"])
        response = self.feed_of(self.mother_user)
        self.assertEqual(response.data["data"], [])
        self.assertEqual(response.data["unread_count"], 0)


class ParentReadTests(ParentNoticesFixture):
    def test_the_unread_count_follows_the_feed(self):
        self.assertEqual(self.feed_of(self.mother_user).data["unread_count"], 4)

    def test_opening_a_notice_does_not_mark_it_read(self):
        url = reverse("parent-portal-notice", kwargs={"pk": self.whole_school.pk})
        response = TenantAPIClient(self.mother_user).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["unread"])
        self.assertEqual(response.data["data"]["children"], ["Tunde", "Kemi"])

    def test_marking_read_belongs_to_the_parents_account(self):
        url = reverse("parent-portal-notice-read", kwargs={"pk": self.whole_school.pk})
        response = TenantAPIClient(self.mother_user).post(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["unread"])
        self.assertEqual(self.feed_of(self.mother_user).data["unread_count"], 3)
        self.assertEqual(self.feed_of(self.father_user).data["unread_count"], 3)
        self.assertTrue(
            NoticeRead.objects.filter(user=self.mother_user, notice=self.whole_school).exists(),
        )
        self.assertFalse(NoticeRead.objects.filter(user=self.father_user).exists())
        self.assertFalse(NoticeRead.objects.filter(user=self.tunde.user).exists())

    def test_read_all_marks_the_whole_feed_and_nothing_else(self):
        response = TenantAPIClient(self.mother_user).post(reverse("parent-portal-notices-read-all"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["marked"], 4)
        self.assertEqual(self.feed_of(self.mother_user).data["unread_count"], 0)
        self.assertFalse(
            NoticeRead.objects.filter(user=self.mother_user, notice=self.for_jss1a).exists(),
        )

    def test_a_notice_not_addressed_to_any_child_answers_404_to_open_and_to_mark(self):
        client = TenantAPIClient(self.mother_user)
        detail = client.get(reverse("parent-portal-notice", kwargs={"pk": self.for_jss1a.pk}))
        mark = client.post(reverse("parent-portal-notice-read", kwargs={"pk": self.for_jss1a.pk}))
        self.assertEqual((detail.status_code, mark.status_code), (404, 404))
        self.assertFalse(NoticeRead.objects.filter(notice=self.for_jss1a).exists())

    def test_a_staff_member_has_no_parent_feed(self):
        response = TenantAPIClient(self.admin).get(reverse(FEED))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "NOT_A_PARENT")
