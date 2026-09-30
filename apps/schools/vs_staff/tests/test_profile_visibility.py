"""How much of a colleague's profile a reader sees, by how they stand to them.

Brightfield runs Lekki and Ikeja, and draws one school-wide chart::

    Principal (Okafor, Lekki)
    |- Head of Sciences (Bello, Lekki)
    |  |- Physics Teacher (Chika, Ikeja)
    |  |- Deputy Head of Sciences (Efe acts here; her own post is Librarian)
    |  '- Exams Officer (Femi, Ikeja) on a dotted line only
    '- Head of Arts (Dayo, Ikeja)

Every one of them holds the Member of Staff role: applying for leave and
reading the chart, and no staff key at all. That is what makes the grid the
only thing deciding what they read of each other. The teachers of the base
fixture hold what the seeder gives a teacher, which is no directory key
either; the administrators hold the keys, which is how the key path and the
grid are shown working together.

Sunrise is another school, and nobody here reaches it.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory

from schools.vs_staff.constants import EmploymentStatus, LeaveStatus
from schools.vs_staff.media_policies import _may_read_document, _may_read_photo
from schools.vs_staff.models import (
    LeaveRequest,
    StaffDocument,
    StaffMatrixReport,
    StaffOrgNode,
    StaffPosition,
    StaffPositionAssignment,
)
from schools.vs_staff.services import visibility
from vs_config.clock import branch_today
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_permission,
    make_role,
    make_role_permission,
    set_field_access,
)

from .base import StaffFixture

CHILD_ROUTES = (
    "staff-qualifications", "staff-documents", "staff-leave", "staff-history",
    "staff-teaching", "staff-roles",
)
ADMINISTRATION_KEYS = (
    "account", "account_status", "account_flag", "can_resend", "invited_at",
    "invitation_email_status", "created_by", "history_starts",
)


class ProfileVisibilityFixture(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        chart_key = make_permission("school.organogram.view", scope=PermissionScope.TENANT)
        make_role_permission(cls.teacher_role, chart_key)
        cls.member_role = make_role(cls.school, name="Member of Staff", key="member")
        make_role_permission(cls.member_role, cls.permissions["school.leave.apply"])
        make_role_permission(cls.member_role, chart_key)

        cls.unit = StaffOrgNode.all_objects.create(
            tenant=cls.tenant, name="Academics", code="ACAD", kind="DIVISION",
        )

        def post(title, code, reports_to=None, headcount=1):
            return StaffPosition.all_objects.create(
                tenant=cls.tenant, title=title, code=code, org_node=cls.unit,
                reports_to=reports_to, headcount=headcount,
            )

        cls.principal_post = post("Principal", "PRIN")
        cls.hod_post = post("Head of Sciences", "HSCI", cls.principal_post)
        cls.arts_post = post("Head of Arts", "HART", cls.principal_post)
        cls.physics_post = post("Physics Teacher", "PHY", cls.hod_post, headcount=3)
        cls.deputy_post = post("Deputy Head of Sciences", "DSCI", cls.hod_post)
        cls.exams_post = post("Exams Officer", "EXO")
        cls.library_post = post("Librarian", "LIB")
        StaffMatrixReport.all_objects.create(
            tenant=cls.tenant, position=cls.exams_post, reports_to=cls.hod_post,
        )

        def member(email, first, last, branch):
            return cls.make_staff(
                email, first, last, branch=branch, job_title=f"{first}'s post",
                staff_number=f"BFS/{last.upper()}", role=cls.member_role,
            )

        cls.okafor = member("okafor@brightfield.test", "Ngozi", "Okafor", cls.lekki)
        cls.bello = member("bello@brightfield.test", "Musa", "Bello", cls.lekki)
        cls.dayo = member("dayo@brightfield.test", "Dayo", "Ade", cls.ikeja)
        cls.chika = member("chika@brightfield.test", "Chika", "Obi", cls.ikeja)
        cls.efe = member("efe@brightfield.test", "Efe", "Ighalo", cls.ikeja)
        cls.femi = member("femi@brightfield.test", "Femi", "Lawal", cls.ikeja)

        def appoint(staff, position, **kwargs):
            kwargs.setdefault("start_date", branch_today(cls.tenant, staff.branch_id))
            StaffPositionAssignment.all_objects.create(
                tenant=cls.tenant, staff=staff, position=position, **kwargs,
            )

        appoint(cls.okafor, cls.principal_post)
        appoint(cls.bello, cls.hod_post)
        appoint(cls.dayo, cls.arts_post)
        appoint(cls.chika, cls.physics_post)
        appoint(cls.efe, cls.library_post)
        appoint(cls.efe, cls.deputy_post, is_primary=False, is_acting=True)
        appoint(cls.femi, cls.exams_post)

        cls.chika.date_of_birth = dt.date(1990, 3, 14)
        cls.chika.middle_name = "Amaka"
        cls.chika.save(update_fields=["date_of_birth", "middle_name"])
        cls.chika.user.phone = "08035550101"
        cls.chika.user.gender = "FEMALE"
        cls.chika.user.save(update_fields=["phone", "gender"])
        LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.chika, leave_type="SICK",
            start_date=dt.date(2025, 10, 6), end_date=dt.date(2025, 10, 8),
            days=3, status=LeaveStatus.APPROVED,
        )

    def record(self, reader, subject, params=None):
        response = self.get(reader.user if hasattr(reader, "user") else reader,
                            "staff-detail", params, pk=subject.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def save_policy(self, **audiences):
        policy = {
            audience: list(groups)
            for audience, groups in visibility.DEFAULT_POLICY.items()
        }
        policy.update(audiences)
        visibility.write_policy(self.tenant, self.admin, policy)


class AnotherSchoolTests(ProfileVisibilityFixture):
    def test_another_schools_person_is_404_on_every_route_whatever_the_reader_is(self):
        for reader in (self.okafor.user, self.eze.user, self.admin):
            for name in ("staff-detail",) + CHILD_ROUTES:
                with self.subTest(reader=reader.email, route=name):
                    response = self.get(reader, name, pk=self.solo_staff.pk)
                    self.assertEqual(response.status_code, 404, response.data)


class ColleagueTests(ProfileVisibilityFixture):
    """Anybody at the school reads the contact card, and nothing else by default."""

    def test_a_colleague_at_another_branch_reads_the_contact_card_only(self):
        data = self.record(self.dayo, self.bello)
        self.assertEqual(data["profile_view"], "restricted")
        self.assertEqual(data["visible_sections"], ["contact"])
        for key in ("full_name", "email", "phone", "photo_url", "organogram"):
            self.assertIn(key, data)
        self.assertEqual(data["organogram"]["position"]["title"], "Head of Sciences")
        for key in (
            "staff_number", "job_title", "hire_date", "employment_status",
            "tenure", "middle_name", "date_of_birth", "gender", "roles",
            "teaching_load", "counts", *ADMINISTRATION_KEYS,
        ):
            self.assertNotIn(key, data)
        self.assertFalse(data["can_manage"])

    def test_every_other_tab_is_refused_to_a_colleague(self):
        for name in CHILD_ROUTES:
            with self.subTest(route=name):
                response = self.get(self.dayo.user, name, pk=self.bello.pk)
                self.assertEqual(response.status_code, 403, response.data)

    def test_a_colleague_may_not_read_the_record_as_it_stood(self):
        response = self.get(
            self.dayo.user, "staff-detail", {"as_at": "2025-01-01"}, pk=self.bello.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_teacher_opens_another_branchs_colleague_as_a_contact_card(self):
        """Inside one school, a relationship crosses branches.

        Eze teaches at Lekki; Sule works at Ikeja. As colleagues, Eze reads
        Sule's contact card and nothing more.
        """
        data = self.record(self.eze, self.ikeja_teacher)
        self.assertEqual(data["profile_view"], "restricted")
        self.assertEqual(data["visible_sections"], ["contact"])
        self.assertNotIn("staff_number", data)

    def test_a_seeded_teacher_reads_a_same_branch_colleague_as_a_contact_card(self):
        """The Colleagues row decides for teachers inside their own branch too.

        A teacher holds no directory key, so a Lekki colleague is shown to Eze
        exactly as far as the school's policy shows colleagues.
        """
        colleague = self.make_staff(
            "ngozi@brightfield.test", "Ngozi", "Eke", branch=self.lekki,
            role=self.teacher_role, staff_number="BFS/EKE",
        )
        data = self.record(self.eze, colleague)
        self.assertEqual(data["profile_view"], "restricted")
        self.assertEqual(data["visible_sections"], ["contact"])
        for key in ("staff_number", "date_of_birth", "roles", "counts", "account"):
            self.assertNotIn(key, data)
        for name in CHILD_ROUTES:
            with self.subTest(route=name):
                self.assertEqual(self.get(self.eze.user, name, pk=colleague.pk).status_code, 403)

        self.save_policy(COLLEAGUE=["contact", "employment"])
        self.assertEqual(self.record(self.eze, colleague)["staff_number"], "BFS/EKE")

    def test_a_seeded_teacher_still_reads_their_own_record_and_tabs(self):
        data = self.record(self.eze, self.eze)
        self.assertEqual(data["profile_view"], "full")
        self.assertEqual(
            data["visible_sections"],
            ["contact", "employment", "personal", "records", "leave", "teaching",
             "history", "roles"],
        )
        self.assertIn("account", data)
        for name in CHILD_ROUTES:
            with self.subTest(route=name):
                response = self.get(self.eze.user, name, pk=self.eze.pk)
                self.assertEqual(response.status_code, 200, response.data)
        # A school that unticks them closes a person's own history and roles.
        self.save_policy(SELF=["contact", "employment", "personal", "records", "leave", "teaching"])
        for name in ("staff-history", "staff-roles"):
            with self.subTest(route=name, policy="closed"):
                self.assertEqual(self.get(self.eze.user, name, pk=self.eze.pk).status_code, 403)
        filed = self.post(self.eze.user, "staff-leave", {
            "leave_type": "ANNUAL", "start_date": "2026-11-02", "end_date": "2026-11-03",
        }, pk=self.eze.pk)
        self.assertNotEqual(filed.status_code, 403, filed.data)
        corrected = self.patch(
            self.eze.user, "staff-detail", {"phone": "08030000001"}, pk=self.eze.pk,
        )
        self.assertEqual(corrected.status_code, 200, corrected.data)

    def test_a_seeded_teacher_still_reads_the_chart(self):
        response = self.get(self.eze.user, "staff-org-tree")
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_card_names_the_branch_for_a_reader_who_works_in_one(self):
        """Where somebody works is the point of a card opened across branches.

        Dayo works at Ikeja only, so the directory recedes the branch for him.
        Opening Bello, posted to Lekki and also to Ikeja, from the chart still
        says so, and the school-wide registrar reads as school-wide.
        """
        self.bello.additional_postings.add(self.ikeja)
        data = self.record(self.dayo, self.bello)
        self.assertEqual(data["branch_name"], "Lekki, Ikeja")
        self.assertFalse(data["posted_school_wide"])
        self.assertEqual(data["posting_branches"], [
            {"id": self.lekki.pk, "name": "Lekki"},
            {"id": self.ikeja.pk, "name": "Ikeja"},
        ])
        registrar = self.record(self.dayo, self.registrar)
        self.assertEqual(registrar["branch_name"], "School-wide")
        self.assertTrue(registrar["posted_school_wide"])
        self.assertEqual(registrar["posting_branches"], [])

    def test_a_one_branch_school_names_no_branch_on_the_card(self):
        data = self.record(self.solo_admin, self.solo_staff)
        self.assertIsNone(data["branch_name"])
        self.assertIsNone(data["posted_school_wide"])
        self.assertIsNone(data["posting_branches"])

    def test_somebody_with_no_staff_record_and_no_chart_key_is_refused(self):
        response = self.get(self.nobody, "staff-detail", pk=self.bello.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_leaver_is_not_open_to_colleagues(self):
        gone = self.make_staff(
            "gone@brightfield.test", "Tunde", "Bakare", branch=self.lekki,
            status=EmploymentStatus.RESIGNED,
        )
        response = self.get(self.dayo.user, "staff-detail", pk=gone.pk)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.record(self.admin, gone)["profile_view"], "full")

    def test_the_contact_photograph_is_readable_by_a_colleague(self):
        request = APIRequestFactory().get("/")
        request.user = self.dayo.user
        request.tenant = self.tenant
        self.assertTrue(_may_read_photo(request, self.bello))


class LineManagerTests(ProfileVisibilityFixture):
    """Above somebody on the chart, at any depth, through acting posts and dotted lines."""

    def test_two_levels_down_across_branches_reads_employment_and_leave(self):
        data = self.record(self.okafor, self.chika)
        self.assertEqual(data["profile_view"], "restricted")
        self.assertEqual(
            data["visible_sections"], ["contact", "employment", "leave", "teaching"],
        )
        self.assertEqual(data["staff_number"], "BFS/OBI")
        self.assertIn("tenure", data)
        self.assertNotIn("date_of_birth", data)
        self.assertNotIn("middle_name", data)
        self.assertNotIn("account", data)
        self.assertEqual(set(data["counts"]), {"teaching_assignments", "leave_requests"})

        leave = self.get(self.okafor.user, "staff-leave", pk=self.chika.pk)
        self.assertEqual(leave.status_code, 200, leave.data)
        self.assertEqual(len(leave.data["data"]["leave"]), 1)
        teaching = self.get(self.okafor.user, "staff-teaching", pk=self.chika.pk)
        self.assertEqual(teaching.status_code, 200, teaching.data)
        for name in ("staff-documents", "staff-qualifications", "staff-history", "staff-roles"):
            with self.subTest(route=name):
                self.assertEqual(
                    self.get(self.okafor.user, name, pk=self.chika.pk).status_code, 403,
                )

    def test_the_subjects_acting_post_puts_them_under_its_manager(self):
        data = self.record(self.bello, self.efe)
        self.assertEqual(data["visible_sections"], ["contact", "employment", "leave", "teaching"])

    def test_the_readers_acting_post_puts_its_reports_under_them(self):
        acting = self.make_staff(
            "gbenga@brightfield.test", "Gbenga", "Ojo", branch=self.lekki,
            role=self.member_role,
        )
        StaffPositionAssignment.all_objects.create(
            tenant=self.tenant, staff=acting, position=self.hod_post,
            is_primary=False, is_acting=True,
            start_date=branch_today(self.tenant, acting.branch_id),
        )
        self.assertIn("leave", self.record(acting, self.chika)["visible_sections"])

    def test_a_dotted_line_puts_the_post_under_its_manager_and_theirs(self):
        self.assertIn("employment", self.record(self.bello, self.femi)["visible_sections"])
        self.assertIn("employment", self.record(self.okafor, self.femi)["visible_sections"])

    def test_a_peer_is_only_a_colleague(self):
        data = self.record(self.dayo, self.chika)
        self.assertEqual(data["visible_sections"], ["contact"])
        response = self.get(self.dayo.user, "staff-leave", pk=self.chika.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_report_is_not_above_their_manager(self):
        self.assertEqual(self.record(self.chika, self.okafor)["visible_sections"], ["contact"])

    def test_a_suspended_manager_still_reads_their_reports(self):
        self.bello.employment_status = EmploymentStatus.SUSPENDED
        self.bello.save(update_fields=["employment_status"])
        self.assertIn("leave", self.record(self.bello, self.efe)["visible_sections"])

    def test_a_line_manager_may_not_read_a_past_day(self):
        for name in ("staff-detail", "staff-leave", "staff-teaching"):
            with self.subTest(route=name):
                response = self.get(
                    self.okafor.user, name, {"as_at": "2025-12-01"}, pk=self.chika.pk,
                )
                self.assertEqual(response.status_code, 403, response.data)

    def test_a_document_opens_for_a_line_manager_once_records_are_shown_to_them(self):
        request = APIRequestFactory().get("/")
        request.user = self.okafor.user
        request.tenant = self.tenant
        document = StaffDocument(tenant=self.tenant, staff=self.chika)
        self.assertFalse(_may_read_document(request, document))

        self.save_policy(LINE=["contact", "records"])
        request = APIRequestFactory().get("/")
        request.user = self.okafor.user
        request.tenant = self.tenant
        self.assertTrue(_may_read_document(request, document))
        response = self.get(self.okafor.user, "staff-documents", pk=self.chika.pk)
        self.assertEqual(response.status_code, 200, response.data)


class TeacherLineManagerLeaveTests(ProfileVisibilityFixture):
    """The grid grants by relationship: leave for a teacher's reports, and nobody else's.

    Eze teaches at Lekki and holds neither ``school.teachers.view`` nor
    ``school.leave.view``. He heads Maths; Kemi teaches under him and Lola,
    at the same branch, does not.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        maths = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Head of Maths", code="HMTH", org_node=cls.unit,
        )
        maths_teacher = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Maths Teacher", code="MTH1", org_node=cls.unit,
            reports_to=maths,
        )
        cls.kemi = cls.make_staff(
            "kemi@brightfield.test", "Kemi", "Adebayo", branch=cls.lekki,
            role=cls.teacher_role,
        )
        cls.lola = cls.make_staff(
            "lola@brightfield.test", "Lola", "Shittu", branch=cls.lekki,
            role=cls.teacher_role,
        )
        StaffPositionAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.eze, position=maths,
            start_date=branch_today(cls.tenant, cls.eze.branch_id),
        )
        StaffPositionAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.kemi, position=maths_teacher,
            start_date=branch_today(cls.tenant, cls.kemi.branch_id),
        )

    def test_a_teacher_reads_leave_for_their_report_and_not_for_a_peer(self):
        mine = self.get(self.eze.user, "staff-leave", pk=self.kemi.pk)
        self.assertEqual(mine.status_code, 200, mine.data)
        peer = self.get(self.eze.user, "staff-leave", pk=self.lola.pk)
        self.assertEqual(peer.status_code, 403, peer.data)

        report = self.record(self.eze, self.kemi)
        self.assertEqual(report["profile_view"], "restricted")
        self.assertEqual(
            report["visible_sections"], ["contact", "employment", "leave", "teaching"],
        )
        self.assertIn("leave_requests", report["counts"])
        self.assertNotIn("leave", self.record(self.eze, self.lola)["visible_sections"])

    def test_the_school_can_take_leave_away_from_line_managers(self):
        self.save_policy(LINE=["contact", "employment"])
        response = self.get(self.eze.user, "staff-leave", pk=self.kemi.pk)
        self.assertEqual(response.status_code, 403, response.data)


class FieldAccessCeilingTests(ProfileVisibilityFixture):
    """A switch a reader's role has off beats the grid; a person reads their own."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.teachers")
        set_field_access(
            cls.member_role, "school.teachers.phone", "school.teachers.date_of_birth",
            read=False, write=False,
        )

    def test_a_switched_off_field_stays_hidden_whatever_the_grid_says(self):
        self.save_policy(LINE=["contact", "employment", "personal"])
        data = self.record(self.okafor, self.chika)
        self.assertIn("personal", data["visible_sections"])
        self.assertNotIn("phone", data)
        self.assertNotIn("date_of_birth", data)
        self.assertEqual(data["gender"], "FEMALE")
        self.assertEqual(data["middle_name"], "Amaka")

    def test_a_person_reads_their_own_phone_with_the_switch_off(self):
        data = self.record(self.chika, self.chika)
        self.assertEqual(data["profile_view"], "full")
        self.assertEqual(data["phone"], "08035550101")
        self.assertEqual(data["date_of_birth"], "1990-03-14")
        self.assertEqual(data["account"]["email"], "chika@brightfield.test")

    def test_a_person_corrects_their_own_phone_with_the_switch_off(self):
        response = self.patch(
            self.chika.user, "staff-detail", {"phone": "08035550202"}, pk=self.chika.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["phone"], "08035550202")
        refused = self.patch(
            self.chika.user, "staff-detail", {"hire_date": "2015-01-01"},
            pk=self.chika.pk,
        )
        self.assertEqual(refused.status_code, 422, refused.data)


class PolicyTests(ProfileVisibilityFixture):
    def test_the_default_applies_until_a_school_saves_one(self):
        policy = visibility.read_policy(self.tenant)
        for audience, groups in visibility.DEFAULT_POLICY.items():
            self.assertEqual(policy[audience], frozenset(groups))
        payload = visibility.policy_payload(self.tenant)
        self.assertEqual(payload["source"], "default")

    def test_one_schools_policy_does_not_reach_another(self):
        self.save_policy(COLLEAGUE=["contact", "employment"])
        self.assertEqual(
            visibility.read_policy(self.solo.tenant)["COLLEAGUE"], frozenset({"contact"}),
        )
        self.assertIn("employment", self.record(self.dayo, self.bello)["visible_sections"])

    def test_the_person_themselves_follows_the_policy_too(self):
        quiet = self.make_staff(
            "quiet@brightfield.test", "Quiet", "Person", branch=self.lekki,
        )
        self.assertEqual(
            self.get(quiet.user, "staff-documents", pk=quiet.pk).status_code, 200,
        )
        self.save_policy(SELF=["contact", "employment"])
        response = self.get(quiet.user, "staff-documents", pk=quiet.pk)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertNotIn("date_of_birth", self.record(quiet, quiet))

    def test_a_shape_that_is_not_a_policy_is_refused_on_every_write_path(self):
        from vs_config.exceptions import InvalidConfigurationValue
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        definition = ConfigurationDefinition.objects.get(key=visibility.POLICY_KEY)
        with self.assertRaises(InvalidConfigurationValue):
            set_value(
                definition=definition, value={"LINE": ["salary"]}, actor=self.admin,
                tenant=self.tenant,
            )


class QueryBudgetTests(ProfileVisibilityFixture):
    def _count(self):
        client = self.client_for(self.okafor.user)
        url = f"/v1/i/me/staff/{self.chika.pk}/?tenant={self.tenant.slug}"
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries)

    def test_the_line_check_does_not_grow_with_the_chart(self):
        before = self._count()
        top = self.principal_post
        for index in range(12):
            top = StaffPosition.all_objects.create(
                tenant=self.tenant, title=f"Layer {index}", code=f"L{index}",
                org_node=self.unit, reports_to=top,
            )
            StaffMatrixReport.all_objects.create(
                tenant=self.tenant, position=top, reports_to=self.arts_post,
            )
        self.assertEqual(self._count(), before)
        self.assertLessEqual(before, 40)
