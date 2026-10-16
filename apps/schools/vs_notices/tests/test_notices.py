"""Notices: who may post where, who reads what, and what a pupil sees.

Brightfield has two branches. Chiamaka is at Lekki in JSS1 A, a class shared by
both branches; Tunde is at Lekki in JSS1 B, a Lekki class; Kemi is at Ikeja in
JSS1 C. The school admin reaches everything, the Lekki head reaches Lekki, and
Mrs. Funke Adeyemi teaches and leads JSS1 B only. Sunrise has one branch and is
also the other tenant.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from schools.vs_notices.constants import NoticeStatus
from schools.vs_notices.models import Notice
from schools.vs_notices.services import notices as service
from schools.vs_notices.tasks import dispatch_published
from schools.vs_students.services import portal_access
from schools.vs_students.tests.base import StudentsFixture
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)
from vs_user.models import User

VIEW, POST, POST_CLASS = (
    "school.notices.view", "school.notices.post", "school.class_notices.post",
)


class NoticesFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_staff.models import StaffProfile

        keys = {k: make_permission(k, scope=PermissionScope.TENANT) for k in (VIEW, POST, POST_CLASS)}
        for key in (VIEW, POST):
            make_role_permission(cls.role, keys[key])
            make_role_permission(cls.solo_role, keys[key])

        cls.teacher_role = make_role(cls.school, name="Teacher", key="teacher")
        for key in (VIEW, POST_CLASS):
            make_role_permission(cls.teacher_role, keys[key])
        cls.teacher = make_school_admin(None, email="funke@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.teacher, cls.teacher_role, branch=cls.lekki)
        staff = StaffProfile.all_objects.create(
            tenant=cls.tenant, user=cls.teacher, branch=cls.lekki,
            job_title="Teacher", hire_date=dt.date(2021, 9, 6),
        )
        cls.lekki_class.class_teacher = staff
        cls.lekki_class.save(update_fields=["class_teacher"])

        cls.chiamaka = cls._pupil("Chiamaka", "0142", cls.lekki, cls.shared_class)
        cls.tunde = cls._pupil("Tunde", "0143", cls.lekki, cls.lekki_class)
        cls.kemi = cls._pupil("Kemi", "0200", cls.ikeja, cls.ikeja_class)

    @classmethod
    def _pupil(cls, first, number, branch, school_class):
        pupil = cls.student(
            cls, first=first, last="Pupil", number=f"BFS/2025/{number}", branch=branch,
            email=f"{first.lower()}@example.ng",
        )
        cls.place(cls, pupil, school_class)
        portal_access.issue(pupil, actor=cls.admin)
        pupil.refresh_from_db()
        User.objects.filter(pk=pupil.user_id).update(status=User.Status.ACTIVE, is_active=True)
        pupil.user.refresh_from_db()
        return pupil

    def post_notice(self, user, audience, *, publish=True, title="PTA meeting Saturday"):
        return self.post(user, "notice-list", {
            "title": title, "body": "All parents are welcome.",
            "audience": audience, "publish": publish,
        })

    def feed(self, pupil):
        return self.get(pupil.user, "student-portal-notices")


class PostingTests(NoticesFixture):
    def test_without_a_posting_key_nothing_is_posted(self):
        response = self.post_notice(self.nobody, [{"kind": "SCHOOL"}])
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Notice.all_objects.exists())

    def test_without_the_view_key_the_list_is_refused(self):
        self.assertEqual(self.get(self.nobody, "notice-list").status_code, 403)

    def test_a_whole_school_notice_reaches_every_pupil(self):
        response = self.post_notice(self.admin, [{"kind": "SCHOOL"}])
        self.assertEqual(response.status_code, 201, response.data)
        notice = Notice.all_objects.get()
        self.assertEqual(notice.status, NoticeStatus.PUBLISHED)
        with mock.patch("vs_notifications.notify.send_notification") as send:
            self.assertEqual(dispatch_published(notice.pk), 3)
        sent_to = {u.pk for call in send.call_args_list for u in call.kwargs["recipients"]}
        self.assertEqual(sent_to, {p.user_id for p in (self.chiamaka, self.tunde, self.kemi)})
        for pupil in (self.chiamaka, self.tunde, self.kemi):
            self.assertEqual(self.feed(pupil).data["pagination"]["totalItems"], 1)

    def test_a_branch_head_cannot_post_to_the_whole_school(self):
        response = self.post_notice(self.lekki_head, [{"kind": "SCHOOL"}])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "NOTICE_AUDIENCE_OUT_OF_REACH")

    def test_a_branch_head_cannot_post_to_another_branchs_class(self):
        response = self.post_notice(self.lekki_head, [{"kind": "CLASS", "id": self.ikeja_class.pk}])
        self.assertEqual(response.status_code, 403)

    def test_a_branch_head_posts_to_their_branch(self):
        response = self.post_notice(self.lekki_head, [{"kind": "BRANCH", "id": self.lekki.pk}])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.feed(self.tunde).data["pagination"]["totalItems"], 1)
        self.assertEqual(self.feed(self.kemi).data["pagination"]["totalItems"], 0)

    def test_a_teacher_posts_to_the_class_they_lead_and_no_other(self):
        ok = self.post_notice(self.teacher, [{"kind": "CLASS", "id": self.lekki_class.pk}])
        self.assertEqual(ok.status_code, 201, ok.data)
        refused = self.post_notice(self.teacher, [{"kind": "CLASS", "id": self.ikeja_class.pk}])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.feed(self.tunde).data["pagination"]["totalItems"], 1)
        self.assertEqual(self.feed(self.chiamaka).data["pagination"]["totalItems"], 0)

    def test_a_teacher_cannot_withdraw_someone_elses_notice(self):
        self.post_notice(self.admin, [{"kind": "CLASS", "id": self.lekki_class.pk}])
        notice = Notice.all_objects.get()
        response = self.post(self.teacher, "notice-withdraw", pk=notice.pk)
        self.assertEqual(response.status_code, 403)

    def test_another_schools_class_is_out_of_reach(self):
        from schools.vs_academics.models import Level, Program, SchoolClass

        program = Program.all_objects.create(tenant=self.solo.tenant, name="Primary", code="PRY")
        level = Level.all_objects.create(
            tenant=self.solo.tenant, program=program, session=self.solo_year,
            name="Basic 1", code="B1", order_index=1,
        )
        other = SchoolClass.all_objects.create(
            tenant=self.solo.tenant, level=level, session=self.solo_year,
            name="Basic 1", code="B1", arm="", capacity=30, branch=None,
        )
        response = self.post_notice(self.admin, [{"kind": "CLASS", "id": other.pk}])
        self.assertEqual(response.status_code, 403)

    def test_a_level_notice_reaches_the_pupils_in_that_level(self):
        self.post_notice(self.admin, [{"kind": "LEVEL", "id": self.jss1.pk}])
        for pupil in (self.chiamaka, self.tunde, self.kemi):
            self.assertEqual(self.feed(pupil).data["pagination"]["totalItems"], 1)

    def test_a_notice_without_an_audience_cannot_be_published(self):
        response = self.post_notice(self.admin, [], publish=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "NOTICE_AUDIENCE_REQUIRED")


class VisibilityTests(NoticesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_notice = service.create(
            cls.tenant, actor=cls.admin, title="Ikeja sports day",
            body="Bring your kit.",
            audience=[{"kind": "CLASS", "id": cls.ikeja_class.pk}], publish_now=True,
        )

    def test_a_branch_head_does_not_see_another_branchs_notice(self):
        self.assertEqual(
            self.get(self.lekki_head, "notice-detail", pk=self.ikeja_notice.pk).status_code, 404,
        )
        listed = [row["id"] for row in self.get(self.lekki_head, "notice-list").data["data"]]
        self.assertNotIn(self.ikeja_notice.pk, listed)

    def test_another_tenant_cannot_open_the_notice(self):
        response = self.get(self.solo_admin, "notice-detail", pk=self.ikeja_notice.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_pupil_cannot_open_a_notice_not_addressed_to_them(self):
        response = self.get(self.chiamaka.user, "student-portal-notice", pk=self.ikeja_notice.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_draft_never_reaches_a_pupil(self):
        draft = service.create(
            self.tenant, actor=self.admin, title="Draft", body="Not yet.",
            audience=[{"kind": "SCHOOL"}],
        )
        response = self.get(self.chiamaka.user, "student-portal-notice", pk=draft.pk)
        self.assertEqual(response.status_code, 404)

    def test_staff_cannot_use_the_pupil_feed(self):
        self.assertEqual(self.get(self.admin, "student-portal-notices").status_code, 404)


class LifecycleTests(NoticesFixture):
    def setUp(self):
        self.notice = service.create(
            self.tenant, actor=self.admin, title="PTA meeting Saturday",
            body="All parents are welcome.",
            audience=[{"kind": "CLASS", "id": self.shared_class.pk}], publish_now=True,
        )

    def test_correcting_a_published_notice_marks_it_edited(self):
        response = self.patch(self.admin, "notice-detail", {"body": "Starts at 10am."}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.notice.refresh_from_db()
        self.assertIsNotNone(self.notice.edited_at)
        self.assertEqual(self.notice.body, "Starts at 10am.")

    def test_the_audience_of_a_published_notice_cannot_change(self):
        response = self.patch(
            self.admin, "notice-detail", {"audience": [{"kind": "SCHOOL"}]}, pk=self.notice.pk,
        )
        self.assertEqual(response.status_code, 409)

    def test_withdrawing_takes_it_out_of_the_feed(self):
        self.assertEqual(self.feed(self.chiamaka).data["pagination"]["totalItems"], 1)
        response = self.post(self.admin, "notice-withdraw", pk=self.notice.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.feed(self.chiamaka).data["pagination"]["totalItems"], 0)

    def test_reading_counts_against_the_audience(self):
        self.post(self.chiamaka.user, "student-portal-notice-read", pk=self.notice.pk)
        data = self.get(self.admin, "notice-detail", pk=self.notice.pk).data["data"]
        self.assertEqual(data["read_count"], 1)
        self.assertEqual(data["audience_count"], 1)

    def test_a_withdrawn_notice_tells_nobody(self):
        service.withdraw(self.notice, actor=self.admin)
        with mock.patch("vs_notifications.notify.send_notification") as send:
            self.assertEqual(dispatch_published(self.notice.pk), 0)
        send.assert_not_called()


class PortalFeedTests(NoticesFixture):
    def setUp(self):
        self.first = service.create(
            self.tenant, actor=self.admin, title="Mid-term break begins Oct 25",
            body="School closes Friday.", audience=[{"kind": "SCHOOL"}], publish_now=True,
        )
        self.second = service.create(
            self.tenant, actor=self.admin, title="Inter-house sports postponed",
            body="New date to follow.", audience=[{"kind": "SCHOOL"}], publish_now=True,
        )

    def test_the_feed_counts_unread_notices(self):
        data = self.feed(self.chiamaka).data
        self.assertEqual(data["unread_count"], 2)
        self.assertTrue(all(row["unread"] for row in data["data"]))

    def test_marking_one_read(self):
        response = self.post(self.chiamaka.user, "student-portal-notice-read", pk=self.first.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["unread"])
        self.assertEqual(self.feed(self.chiamaka).data["unread_count"], 1)

    def test_marking_all_read(self):
        response = self.post(self.chiamaka.user, "student-portal-notices-read-all")
        self.assertEqual(response.data["data"]["marked"], 2)
        self.assertEqual(self.feed(self.chiamaka).data["unread_count"], 0)

    def test_one_pupils_reads_do_not_mark_anothers(self):
        self.post(self.chiamaka.user, "student-portal-notices-read-all")
        self.assertEqual(self.feed(self.tunde).data["unread_count"], 2)

    def test_the_pupil_sees_no_staff_detail(self):
        row = self.feed(self.chiamaka).data["data"][0]
        for hidden in ("created_by", "published_by", "read_count", "status"):
            self.assertNotIn(hidden, row)


class AudienceOptionTests(NoticesFixture):
    def test_a_two_branch_admin_is_offered_branches_and_levels(self):
        data = self.get(self.admin, "notice-audiences").data["data"]
        self.assertTrue(data["school"])
        self.assertEqual(len(data["branches"]), 2)
        self.assertTrue(data["levels"])

    def test_a_one_branch_school_is_offered_no_branch(self):
        data = self.get(self.solo_admin, "notice-audiences").data["data"]
        self.assertTrue(data["school"])
        self.assertEqual(data["branches"], [])

    def test_a_teacher_is_offered_only_their_classes(self):
        data = self.get(self.teacher, "notice-audiences").data["data"]
        self.assertFalse(data["school"])
        self.assertEqual([c["id"] for c in data["classes"]], [self.lekki_class.pk])


class FieldAccessTests(NoticesFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.notices")
        cls.notice = service.create(
            cls.tenant, actor=cls.admin, title="PTA meeting Saturday",
            body="All parents are welcome.", audience=[{"kind": "SCHOOL"}], publish_now=True,
        )

    def test_a_hidden_read_count_leaves_detail_and_list(self):
        set_field_access(self.role, "school.notices.reads", read=False, write=False)
        detail = self.get(self.admin, "notice-detail", pk=self.notice.pk).data["data"]
        self.assertNotIn("read_count", detail)
        self.assertNotIn("audience_count", detail)
        row = self.get(self.admin, "notice-list").data["data"][0]
        self.assertNotIn("read_count", row)

    def test_a_hidden_audience_leaves_both_its_names(self):
        set_field_access(self.role, "school.notices.audience", read=False, write=False)
        detail = self.get(self.admin, "notice-detail", pk=self.notice.pk).data["data"]
        self.assertNotIn("audience", detail)
        self.assertNotIn("audience_label", detail)

    def test_a_title_the_role_may_not_write_is_refused(self):
        set_field_access(self.role, "school.notices.title", read=True, write=False)
        response = self.patch(self.admin, "notice-detail", {"title": "Changed"}, pk=self.notice.pk)
        self.assertEqual(response.status_code, 403)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.title, "PTA meeting Saturday")

    def test_switches_are_open_by_default(self):
        detail = self.get(self.admin, "notice-detail", pk=self.notice.pk).data["data"]
        for name in ("title", "body", "audience", "read_count", "published_at"):
            self.assertIn(name, detail)
