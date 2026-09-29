"""The school's organogram: who may read it, who may draw it, and what it keeps true.

Built on :class:`StaffFixture`, so every test runs against Brightfield's two
branches with Sunrise beside it as the other school.

The refusals come first, because they are what a mistake here would leak:
another school's chart, a teacher redrawing the school, a Lekki administrator
reaching into Ikeja, and an email address on a payload every colleague reads.
Then the rules the chart keeps, then the workflow climbs that read it.
"""
from __future__ import annotations

import datetime as dt
import json

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import (
    StaffMatrixReport,
    StaffOrgNode,
    StaffPosition,
    StaffPositionAssignment,
)
from schools.vs_staff.services import employment
from schools.vs_staff.services.organogram import StaffOrganogramService
from vs_config.clock import tenant_today
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role_permission,
    make_school_admin,
)

from .base import StaffFixture

ORG_KEYS = (
    "school.organogram.view",
    "school.organogram.create",
    "school.organogram.update",
    "school.organogram.delete",
    "school.organogram.assign",
)


class OrganogramFixture(StaffFixture):
    """Brightfield's chart: a school-wide division, a department per branch.

    Academics (school-wide division) is headed by the Principal post. Under it
    sit Lekki Sciences and Ikeja Sciences, each with a head post reporting to
    the Principal, and Ikeja has a teacher post under its head.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in ORG_KEYS:
            permission = make_permission(key, scope=PermissionScope.TENANT)
            make_role_permission(cls.role, permission)
            make_role_permission(cls.solo_role, permission)
            if key == "school.organogram.view":
                make_role_permission(cls.teacher_role, permission)

        # Pinned to Ikeja, holding the same administrator role as the Lekki head.
        cls.ikeja_head = make_school_admin(
            None, email="head@ikeja.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.ikeja_head, cls.role, branch=cls.ikeja)

        cls.academics = StaffOrgNode.all_objects.create(
            tenant=cls.tenant, name="Academics", code="ACAD", kind="DIVISION",
        )
        cls.lekki_sci = StaffOrgNode.all_objects.create(
            tenant=cls.tenant, name="Lekki Sciences", code="LSCI", kind="DEPARTMENT",
            parent=cls.academics, branch=cls.lekki,
        )
        cls.ikeja_sci = StaffOrgNode.all_objects.create(
            tenant=cls.tenant, name="Ikeja Sciences", code="ISCI", kind="DEPARTMENT",
            parent=cls.academics, branch=cls.ikeja,
        )
        cls.principal = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Principal", code="PRIN", org_node=cls.academics,
        )
        cls.academics.head_position = cls.principal
        cls.academics.save()
        cls.lekki_hod = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Head of Sciences, Lekki", code="LHOD",
            org_node=cls.lekki_sci, reports_to=cls.principal,
        )
        cls.ikeja_hod = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Head of Sciences, Ikeja", code="IHOD",
            org_node=cls.ikeja_sci, reports_to=cls.principal,
        )
        cls.ikeja_teacher_post = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Physics Teacher, Ikeja", code="IPHY",
            org_node=cls.ikeja_sci, reports_to=cls.ikeja_hod, headcount=2,
        )
        cls.ikeja_sci.head_position = cls.ikeja_hod
        cls.ikeja_sci.save()

        # Sunrise's chart, which nobody at Brightfield may reach.
        cls.solo_unit = StaffOrgNode.all_objects.create(
            tenant=cls.solo.tenant, name="Sunrise Academics", code="SACAD",
            kind="DIVISION",
        )
        cls.solo_post = StaffPosition.all_objects.create(
            tenant=cls.solo.tenant, title="Sunrise Head", code="SHEAD",
            org_node=cls.solo_unit,
        )
        cls.solo_appointment = StaffPositionAssignment.all_objects.create(
            tenant=cls.solo.tenant, staff=cls.solo_staff, position=cls.solo_post,
        )

    def appoint(self, staff, position, **kwargs):
        return StaffOrganogramService.assign_position(staff, position, **kwargs)


# ── Refusals ────────────────────────────────────────────────────────────────


class OrganogramTenantIsolationTests(OrganogramFixture):
    """Another school's unit, post, appointment or line is a 404, never a 403."""

    def test_another_schools_rows_are_404_on_read_and_write(self):
        cases = (
            ("get", "staff-org-node-detail", self.solo_unit.pk),
            ("patch", "staff-org-node-detail", self.solo_unit.pk),
            ("delete", "staff-org-node-detail", self.solo_unit.pk),
            ("get", "staff-org-position-detail", self.solo_post.pk),
            ("patch", "staff-org-position-detail", self.solo_post.pk),
            ("delete", "staff-org-position-detail", self.solo_post.pk),
            ("get", "staff-org-assignment-detail", self.solo_appointment.pk),
            ("post", "staff-org-assignment-close", self.solo_appointment.pk),
        )
        for method, name, pk in cases:
            with self.subTest(method=method, route=name):
                response = getattr(self, method)(self.admin, name, pk=pk)
                self.assertEqual(response.status_code, 404, response.content)

    def test_another_schools_post_is_not_a_tree_root(self):
        response = self.get(self.admin, "staff-org-tree", {"root": self.solo_post.pk})
        self.assertEqual(response.status_code, 404)

    def test_another_schools_ids_in_a_body_name_nothing(self):
        response = self.post(self.admin, "staff-org-positions", {
            "title": "Borrowed", "code": "BRW", "org_node_id": self.solo_unit.pk,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("org_node_id", response.json()["error"]["detail"])

    def test_lists_carry_only_this_schools_rows(self):
        codes = {
            row["code"] for row in
            self.get(self.admin, "staff-org-positions").json()["data"]
        }
        self.assertNotIn("SHEAD", codes)
        self.assertIn("PRIN", codes)


class OrganogramReadersTests(OrganogramFixture):
    """A teacher reads the whole chart and changes none of it."""

    def test_a_teacher_reads_the_chart(self):
        for name in (
            "staff-org-tree", "staff-org-nodes", "staff-org-positions",
            "staff-org-assignments-current", "staff-org-matrix-reports",
            "staff-org-assignments-mine",
        ):
            with self.subTest(route=name):
                self.assertEqual(self.get(self.eze.user, name).status_code, 200)

    def test_a_lekki_teacher_sees_ikeja_on_the_chart(self):
        titles = {
            row["title"] for row in
            self.get(self.eze.user, "staff-org-positions").json()["data"]
        }
        self.assertIn("Head of Sciences, Ikeja", titles)

    def test_a_teacher_is_refused_every_write(self):
        appointment = self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        cases = (
            ("post", "staff-org-nodes", {}, {"name": "X", "code": "X", "kind": "DIVISION"}),
            ("patch", "staff-org-node-detail", {"pk": self.academics.pk}, {"name": "Y"}),
            ("delete", "staff-org-node-detail", {"pk": self.lekki_sci.pk}, None),
            ("post", "staff-org-positions", {}, {
                "title": "T", "code": "T1", "org_node_id": self.lekki_sci.pk,
            }),
            ("patch", "staff-org-position-detail", {"pk": self.lekki_hod.pk}, {"title": "Z"}),
            ("delete", "staff-org-position-detail", {"pk": self.lekki_hod.pk}, None),
            ("post", "staff-org-assignments", {}, {
                "staff_id": self.eze.pk, "position_id": self.lekki_hod.pk,
            }),
            ("post", "staff-org-assignment-close", {"pk": appointment.pk}, {}),
            ("post", "staff-org-matrix-reports", {}, {
                "position_id": self.lekki_hod.pk, "reports_to_id": self.principal.pk,
            }),
        )
        for method, name, kwargs, body in cases:
            with self.subTest(method=method, route=name):
                call = getattr(self, method)
                response = (
                    call(self.eze.user, name, **kwargs) if body is None
                    else call(self.eze.user, name, body, **kwargs)
                )
                self.assertEqual(response.status_code, 403, response.content)

    def test_without_the_view_key_the_tree_is_refused(self):
        self.assertEqual(self.get(self.nobody, "staff-org-tree").status_code, 403)

    def test_a_teacher_is_refused_the_summary_the_vacancies_and_the_history(self):
        appointment = self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        for name, kwargs in (
            ("staff-org-summary", {}),
            ("staff-org-vacancies", {}),
            ("staff-org-assignments", {}),
            ("staff-org-assignment-detail", {"pk": appointment.pk}),
        ):
            with self.subTest(route=name):
                self.assertEqual(self.get(self.eze.user, name, **kwargs).status_code, 403)
        self.assertEqual(self.get(self.admin, "staff-org-summary").status_code, 200)

    def test_no_chart_payload_carries_an_email_address(self):
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        self.appoint(self.registrar, self.principal)
        StaffMatrixReport.all_objects.create(
            tenant=self.tenant, position=self.lekki_hod, reports_to=self.principal,
        )
        bodies = [
            self.get(self.admin, name).content.decode()
            for name in (
                "staff-org-tree", "staff-org-nodes", "staff-org-positions",
                "staff-org-assignments-current", "staff-org-matrix-reports",
                "staff-org-assignments", "staff-org-vacancies",
            )
        ]
        detail = self.get(self.admin, "staff-detail", pk=self.ikeja_teacher.pk).json()
        bodies.append(json.dumps(detail["data"]["organogram"]))
        for body in bodies:
            with self.subTest(body=body[:80]):
                self.assertNotIn("@", body)
                self.assertNotIn('"email"', body)


class BranchWriteTests(OrganogramFixture):
    """An Ikeja administrator draws Ikeja, and reads the rest."""

    def test_ikeja_cannot_create_a_lekki_or_school_wide_unit(self):
        for branch in (self.lekki.pk, None):
            with self.subTest(branch=branch):
                response = self.post(self.ikeja_head, "staff-org-nodes", {
                    "name": "Arts", "code": "ARTS", "kind": "DEPARTMENT",
                    "parent_id": self.academics.pk, "branch_id": branch,
                })
                self.assertEqual(response.status_code, 403, response.content)

    def test_ikeja_cannot_change_or_remove_a_lekki_or_school_wide_unit(self):
        for node in (self.lekki_sci, self.academics):
            with self.subTest(unit=node.name):
                self.assertEqual(self.patch(
                    self.ikeja_head, "staff-org-node-detail", {"description": "x"},
                    pk=node.pk,
                ).status_code, 403)
                self.assertEqual(self.delete(
                    self.ikeja_head, "staff-org-node-detail", pk=node.pk,
                ).status_code, 403)

    def test_ikeja_cannot_create_change_or_remove_a_lekki_or_school_wide_post(self):
        for unit, post in ((self.lekki_sci, self.lekki_hod), (self.academics, self.principal)):
            with self.subTest(unit=unit.name):
                self.assertEqual(self.post(self.ikeja_head, "staff-org-positions", {
                    "title": "Lab", "code": f"LAB{unit.pk}", "org_node_id": unit.pk,
                }).status_code, 403)
                self.assertEqual(self.patch(
                    self.ikeja_head, "staff-org-position-detail", {"title": "x"},
                    pk=post.pk,
                ).status_code, 403)
                self.assertEqual(self.delete(
                    self.ikeja_head, "staff-org-position-detail", pk=post.pk,
                ).status_code, 403)

    def test_ikeja_draws_its_own_branch(self):
        unit = self.post(self.ikeja_head, "staff-org-nodes", {
            "name": "Ikeja Arts", "code": "IARTS", "kind": "DEPARTMENT",
            "parent_id": self.academics.pk,
        })
        self.assertEqual(unit.status_code, 201, unit.content)
        self.assertEqual(unit.json()["data"]["branch"], {"id": self.ikeja.pk, "name": "Ikeja"})
        self.assertEqual(unit.json()["data"]["code"], "DT-IARTS")
        self.assertTrue(unit.json()["data"]["can_manage"])
        post = self.post(self.ikeja_head, "staff-org-positions", {
            "title": "Art Teacher", "code": "IART",
            "org_node_id": unit.json()["data"]["id"], "reports_to_id": self.principal.pk,
        })
        self.assertEqual(post.status_code, 201, post.content)
        self.assertEqual(self.patch(
            self.ikeja_head, "staff-org-node-detail", {"description": "Art and design"},
            pk=self.ikeja_sci.pk,
        ).status_code, 200)

    def test_ikeja_appoints_its_own_people_and_not_lekkis(self):
        refused = self.post(self.ikeja_head, "staff-org-assignments", {
            "staff_id": self.eze.pk, "position_id": self.ikeja_teacher_post.pk,
        })
        self.assertIn(refused.status_code, (403, 404))
        refused = self.post(self.ikeja_head, "staff-org-assignments", {
            "staff_id": self.eze.pk, "position_id": self.lekki_hod.pk,
        })
        self.assertIn(refused.status_code, (403, 404))
        self.assertFalse(
            StaffPositionAssignment.all_objects.filter(staff=self.eze).exists(),
        )
        made = self.post(self.ikeja_head, "staff-org-assignments", {
            "staff_id": self.ikeja_teacher.pk, "position_id": self.ikeja_teacher_post.pk,
        })
        self.assertEqual(made.status_code, 201, made.content)
        self.assertTrue(made.json()["data"]["can_manage"])

    def test_rows_say_whether_the_caller_may_change_them(self):
        rows = {
            row["code"]: row["can_manage"] for row in
            self.get(self.ikeja_head, "staff-org-nodes").json()["data"]
        }
        self.assertEqual(rows, {"DV-ACAD": False, "DT-LSCI": False, "DT-ISCI": True})
        whole = {
            row["code"]: row["can_manage"] for row in
            self.get(self.admin, "staff-org-nodes").json()["data"]
        }
        self.assertTrue(all(whole.values()))


# ── The rules the chart keeps ──────────────────────────────────────────────


class ChartInvariantTests(OrganogramFixture):

    def test_the_tier_rule(self):
        for body in (
            {"name": "Loose", "code": "LOOSE", "kind": "DEPARTMENT"},
            {"name": "Skip", "code": "SKIP", "kind": "TEAM", "parent_id": self.academics.pk},
            {"name": "Nested", "code": "NEST", "kind": "DIVISION", "parent_id": self.academics.pk},
        ):
            with self.subTest(body=body["name"]):
                response = self.post(self.admin, "staff-org-nodes", body)
                self.assertEqual(response.status_code, 400, response.content)

    def test_a_reporting_loop_is_refused(self):
        response = self.patch(
            self.admin, "staff-org-position-detail",
            {"reports_to_id": self.ikeja_teacher_post.pk}, pk=self.principal.pk,
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_child_unit_is_no_wider_than_its_parent(self):
        for branch in (self.ikeja.pk, None):
            with self.subTest(branch=branch):
                response = self.post(self.admin, "staff-org-nodes", {
                    "name": "Physics", "code": "PHY", "kind": "TEAM",
                    "parent_id": self.lekki_sci.pk, "branch_id": branch,
                })
                self.assertEqual(response.status_code, 400, response.content)
        # Left out, the child takes its parent's branch.
        inherited = self.post(self.admin, "staff-org-nodes", {
            "name": "Physics", "code": "PHY", "kind": "TEAM", "parent_id": self.lekki_sci.pk,
        })
        self.assertEqual(inherited.status_code, 201, inherited.content)
        self.assertEqual(inherited.json()["data"]["branch"]["id"], self.lekki.pk)

    def test_a_unit_moves_branch_only_where_its_lines_can_follow(self):
        # Ikeja Sciences answers only to the school-wide Principal, so it moves.
        moved = self.patch(
            self.admin, "staff-org-node-detail", {"branch_id": self.lekki.pk},
            pk=self.ikeja_sci.pk,
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.patch(
            self.admin, "staff-org-node-detail", {"branch_id": self.ikeja.pk},
            pk=self.ikeja_sci.pk,
        )
        # With Ikeja's own teacher appointed in it, it no longer can.
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        stranded = self.patch(
            self.admin, "staff-org-node-detail", {"branch_id": self.lekki.pk},
            pk=self.ikeja_sci.pk,
        )
        self.assertEqual(stranded.status_code, 400, stranded.content)
        self.assertIn("1 person appointed", json.dumps(stranded.json()))

        # Ikeja Arts has a team under it and a post reporting into Ikeja Sciences.
        arts = StaffOrgNode.all_objects.create(
            tenant=self.tenant, name="Ikeja Arts", code="IARTS", kind="DEPARTMENT",
            parent=self.academics, branch=self.ikeja,
        )
        StaffOrgNode.all_objects.create(
            tenant=self.tenant, name="Drama", code="DRAMA", kind="TEAM",
            parent=arts, branch=self.ikeja,
        )
        StaffPosition.all_objects.create(
            tenant=self.tenant, title="Art Teacher", code="IART",
            org_node=arts, reports_to=self.ikeja_hod,
        )
        refused = self.patch(
            self.admin, "staff-org-node-detail", {"branch_id": self.lekki.pk}, pk=arts.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.content)
        message = json.dumps(refused.json())
        self.assertIn("1 unit under it", message)
        self.assertIn("1 post in it", message)
        self.assertEqual(
            StaffOrgNode.all_objects.get(pk=arts.pk).branch_id, self.ikeja.pk,
        )

    def test_a_post_reports_to_its_own_branch_or_the_school(self):
        cross = self.post(self.admin, "staff-org-positions", {
            "title": "Lab Tech", "code": "LLAB", "org_node_id": self.lekki_sci.pk,
            "reports_to_id": self.ikeja_hod.pk,
        })
        self.assertEqual(cross.status_code, 400, cross.content)
        fine = self.post(self.admin, "staff-org-positions", {
            "title": "Lab Tech", "code": "LLAB", "org_node_id": self.lekki_sci.pk,
            "reports_to_id": self.principal.pk,
        })
        self.assertEqual(fine.status_code, 201, fine.content)
        upward = self.post(self.admin, "staff-org-positions", {
            "title": "Bursar", "code": "BURS", "org_node_id": self.academics.pk,
            "reports_to_id": self.lekki_hod.pk,
        })
        self.assertEqual(upward.status_code, 400, upward.content)

    def test_dotted_lines_keep_the_same_branch_rule(self):
        for target in (self.ikeja_hod, self.lekki_hod):
            with self.subTest(target=target.title):
                response = self.post(self.admin, "staff-org-matrix-reports", {
                    "position_id": self.lekki_hod.pk, "reports_to_id": target.pk,
                })
                self.assertEqual(response.status_code, 400, response.content)
        drawn = self.post(self.admin, "staff-org-matrix-reports", {
            "position_id": self.lekki_hod.pk, "reports_to_id": self.principal.pk,
            "relationship_label": "Exams",
        })
        self.assertEqual(drawn.status_code, 201, drawn.content)
        again = self.post(self.admin, "staff-org-matrix-reports", {
            "position_id": self.lekki_hod.pk, "reports_to_id": self.principal.pk,
        })
        self.assertEqual(again.status_code, 400)

    def test_eligibility_follows_the_posting(self):
        refused = self.post(self.admin, "staff-org-assignments", {
            "staff_id": self.eze.pk, "position_id": self.ikeja_hod.pk,
        })
        self.assertEqual(refused.status_code, 422, refused.content)
        for staff, post in (
            (self.registrar, self.ikeja_hod),
            (self.eze, self.principal),
        ):
            with self.subTest(staff=staff.user.first_name):
                response = self.post(self.admin, "staff-org-assignments", {
                    "staff_id": staff.pk, "position_id": post.pk,
                })
                self.assertEqual(response.status_code, 201, response.content)

    def test_a_new_primary_closes_the_old_and_one_primary_stays(self):
        first = self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        second = self.appoint(
            self.ikeja_teacher, self.ikeja_hod, start_date=tenant_today(self.tenant),
        )
        first.refresh_from_db()
        self.assertEqual(first.end_date, second.start_date)
        self.assertEqual(
            StaffPositionAssignment.all_objects.filter(
                staff=self.ikeja_teacher, is_primary=True, end_date__isnull=True,
            ).count(), 1,
        )
        clash = StaffPositionAssignment(
            tenant=self.tenant, staff=self.ikeja_teacher,
            position=self.ikeja_teacher_post, is_primary=True,
        )
        with self.assertRaises(ValidationError):
            clash.clean()

    def test_holders_leave_out_the_invited_and_the_departed(self):
        invited = self.make_staff(
            "new@brightfield.test", "Funke", "Adeyemi", branch=self.ikeja,
            status=EmploymentStatus.INVITED,
        )
        self.appoint(invited, self.ikeja_hod)
        post = self.get(self.admin, "staff-org-position-detail", pk=self.ikeja_hod.pk)
        self.assertTrue(post.json()["data"]["is_vacant"])

        gone = self.make_staff("gone@brightfield.test", "Tunde", "Bello", branch=self.ikeja)
        StaffPositionAssignment.all_objects.create(
            tenant=self.tenant, staff=gone, position=self.ikeja_teacher_post,
        )
        type(gone).all_objects.filter(pk=gone.pk).update(
            employment_status=EmploymentStatus.TERMINATED,
        )
        post = self.get(self.admin, "staff-org-position-detail", pk=self.ikeja_teacher_post.pk)
        self.assertEqual(post.json()["data"]["current_holders"], [])

    def test_a_suspended_person_keeps_their_post_marked_suspended(self):
        self.appoint(self.ikeja_teacher, self.ikeja_hod)
        employment.change_status(
            self.ikeja_teacher, to_status=EmploymentStatus.SUSPENDED, actor=self.admin,
            reason="Under review",
        )
        self.ikeja_teacher.user.refresh_from_db()
        self.assertFalse(self.ikeja_teacher.user.is_active)

        post = self.get(self.admin, "staff-org-position-detail", pk=self.ikeja_hod.pk)
        data = post.json()["data"]
        self.assertFalse(data["is_vacant"])
        self.assertEqual(data["open_seats"], 0)
        [holder] = data["current_holders"]
        self.assertEqual(holder["staff_id"], self.ikeja_teacher.pk)
        self.assertTrue(holder["is_suspended"])

        tree = self.get(self.admin, "staff-org-tree").json()["data"]
        [root] = tree
        [ikeja] = [n for n in root["direct_reports"] if n["id"] == self.ikeja_hod.pk]
        self.assertTrue(ikeja["holders"][0]["is_suspended"])
        self.assertNotIn(self.ikeja_hod.pk, [
            p["id"] for p in self.get(self.admin, "staff-org-vacancies").json()["data"]
        ])

    def test_a_holder_in_good_standing_is_not_marked_suspended(self):
        self.appoint(self.ikeja_teacher, self.ikeja_hod)
        post = self.get(self.admin, "staff-org-position-detail", pk=self.ikeja_hod.pk)
        self.assertFalse(post.json()["data"]["current_holders"][0]["is_suspended"])

    def test_resigning_ends_the_appointment_and_the_post_keeps_its_reports(self):
        appointment = self.appoint(self.ikeja_teacher, self.ikeja_hod)
        last_day = tenant_today(self.tenant) + dt.timedelta(days=14)
        employment.change_status(
            self.ikeja_teacher, to_status=EmploymentStatus.RESIGNED, actor=self.admin,
            reason="Relocating", last_working_day=last_day,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.end_date, last_day)
        post = self.get(self.admin, "staff-org-position-detail", pk=self.ikeja_hod.pk)
        self.assertTrue(post.json()["data"]["is_vacant"])
        self.assertEqual(
            list(StaffPosition.all_objects.filter(reports_to=self.ikeja_hod)),
            [self.ikeja_teacher_post],
        )

    def test_a_unit_or_post_in_use_is_not_deleted(self):
        unit = self.delete(self.admin, "staff-org-node-detail", pk=self.ikeja_sci.pk)
        self.assertEqual(unit.status_code, 409, unit.content)
        self.assertEqual(unit.json()["error"]["detail"]["positions"], 2)
        parent = self.delete(self.admin, "staff-org-node-detail", pk=self.academics.pk)
        self.assertEqual(parent.status_code, 409)

        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        post = self.delete(self.admin, "staff-org-position-detail", pk=self.ikeja_teacher_post.pk)
        self.assertEqual(post.status_code, 409, post.content)

        empty = StaffOrgNode.all_objects.create(
            tenant=self.tenant, name="Sports", code="SPORT", kind="DIVISION",
        )
        self.assertEqual(
            self.delete(self.admin, "staff-org-node-detail", pk=empty.pk).status_code, 200,
        )

    def test_the_tree_keeps_a_subtree_under_an_inactive_manager_post(self):
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        StaffPosition.all_objects.filter(pk=self.ikeja_hod.pk).update(is_active=False)
        tree = self.get(self.eze.user, "staff-org-tree").json()["data"]
        roots = {node["code"]: node for node in tree}
        self.assertIn("IPHY", roots)
        self.assertEqual(
            [holder["staff_id"] for holder in roots["IPHY"]["holders"]],
            [self.ikeja_teacher.pk],
        )
        self.assertEqual(roots["PRIN"]["branch"], None)

    def test_an_empty_chart_answers_empty_lists(self):
        StaffPositionAssignment.all_objects.filter(tenant=self.solo.tenant).delete()
        StaffPosition.all_objects.filter(tenant=self.solo.tenant).delete()
        for name in ("staff-org-tree", "staff-org-assignments-current"):
            with self.subTest(route=name):
                self.assertEqual(self.get(self.solo_admin, name).json()["data"], [])
        page = self.get(self.solo_admin, "staff-org-matrix-reports").json()
        self.assertEqual(page["data"], [])
        self.assertEqual(page["pagination"]["totalItems"], 0)
        self.assertEqual(
            self.get(self.solo_admin, "staff-org-vacancies").json()["data"], [],
        )

    def test_the_summary_counts_seats_post_by_post(self):
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post, is_acting=True)
        self.appoint(self.registrar, self.principal)
        data = self.get(self.admin, "staff-org-summary").json()["data"]
        self.assertEqual(data, {
            "active_staff": 3, "departments": 2, "positions": 4,
            "total_seats": 5, "filled_seats": 2, "vacant_seats": 3,
            "acting": 1, "on_leave": 0, "suspended": 0,
        })

    def test_the_summary_follows_the_readers_branches_and_the_chart_does_not(self):
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post, is_acting=True)
        self.appoint(self.registrar, self.principal)
        # Lekki's administrator counts Lekki and the school-wide rows only.
        data = self.get(self.lekki_head, "staff-org-summary").json()["data"]
        self.assertEqual(data, {
            "active_staff": 2, "departments": 1, "positions": 2,
            "total_seats": 2, "filled_seats": 1, "vacant_seats": 1,
            "acting": 0, "on_leave": 0, "suspended": 0,
        })
        # ...while the chart she reads still carries Ikeja's posts.
        tree = self.get(self.lekki_head, "staff-org-tree").json()["data"]
        [root] = tree
        self.assertIn(self.ikeja_hod.pk, [n["id"] for n in root["direct_reports"]])

    def test_the_record_names_the_post_and_the_line_manager(self):
        self.appoint(self.registrar, self.ikeja_hod)
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post, is_acting=True)
        block = self.get(
            self.admin, "staff-detail", pk=self.ikeja_teacher.pk,
        ).json()["data"]["organogram"]
        self.assertEqual(block["position"]["code"], "IPHY")
        self.assertEqual(block["org_node"]["code"], "DT-ISCI")
        self.assertEqual(block["line_manager"]["staff_id"], self.registrar.pk)
        self.assertTrue(block["is_acting"])
        self.assertIsNone(
            self.get(self.admin, "staff-detail", pk=self.eze.pk).json()["data"]["organogram"],
        )


class QueryCostTests(OrganogramFixture):
    """The tree and the lists cost the same whatever the size of the chart."""

    def _grow(self, start, stop):
        for index in range(start, stop):
            post = StaffPosition.all_objects.create(
                tenant=self.tenant, title=f"Clerk {index}", code=f"CLK{index}",
                org_node=self.academics, reports_to=self.principal,
            )
            person = self.make_staff(
                f"clerk{index}@brightfield.test", "Clerk", str(index), branch=None,
            )
            StaffPositionAssignment.all_objects.create(
                tenant=self.tenant, staff=person, position=post,
            )

    def _cost(self, name):
        with CaptureQueriesContext(connection) as captured:
            response = self.get(self.admin, name)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_the_tree_and_the_lists_are_bounded(self):
        routes = (
            "staff-org-tree", "staff-org-positions", "staff-org-nodes",
            "staff-org-assignments", "staff-org-assignments-current",
            "staff-org-summary", "staff-org-vacancies",
        )
        self._grow(0, 2)
        small = {name: self._cost(name) for name in routes}
        self._grow(2, 8)
        for name, before in small.items():
            with self.subTest(route=name):
                self.assertEqual(self._cost(name), before)


# ── The workflow climbs ────────────────────────────────────────────────────


class WorkflowClimbTests(OrganogramFixture):
    """An ORGANOGRAM stage climbs the school's own chart for a school requester."""

    def _instance(self, requester, tenant=None):
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        from vs_workflow.models import WorkflowInstance, WorkflowTemplate

        template = WorkflowTemplate.objects.create(
            document_type="ORG_SCHOOL_DOC", code=f"org-{requester.pk}",
            name="Organogram climb",
        )
        return template, WorkflowInstance.objects.create(
            tenant=tenant or requester.tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id="doc", document_type=template.document_type,
            status="IN_PROGRESS", requested_by=requester,
            submitted_at=timezone.now(),
        )

    def _stage(self, template, target, levels=1):
        from vs_workflow.models import WorkflowStage

        return WorkflowStage.objects.create(
            template=template, code=f"s-{target.lower()}", label=target,
            kind="APPROVAL", order=1, advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False, approver_source="ORGANOGRAM",
            approver_role_key="", organogram_target=target, organogram_levels=levels,
        )

    def _resolved(self, requester, target, levels=1, tenant=None):
        from vs_workflow.services.approvers import resolve_approvers

        template, instance = self._instance(requester, tenant)
        stage = self._stage(template, target, levels)
        return [approver.user.pk for approver in resolve_approvers(stage, instance)]

    def setUp(self):
        self.appoint(self.registrar, self.principal)
        self.hod = self.make_staff(
            "hod@brightfield.test", "Ngozi", "Okafor", branch=self.ikeja,
        )
        self.appoint(self.hod, self.ikeja_hod)
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)

    def test_direct_manager(self):
        self.assertEqual(
            self._resolved(self.ikeja_teacher.user, "DIRECT_MANAGER"),
            [self.hod.user.pk],
        )

    def test_n_levels_up_clamps_to_the_top(self):
        self.assertEqual(
            self._resolved(self.ikeja_teacher.user, "N_LEVELS_UP", levels=2),
            [self.registrar.user.pk],
        )
        self.assertEqual(
            self._resolved(self.ikeja_teacher.user, "N_LEVELS_UP", levels=9),
            [self.registrar.user.pk],
        )

    def test_department_head_walks_up_past_the_requesters_own_seat(self):
        self.assertEqual(
            self._resolved(self.ikeja_teacher.user, "DEPARTMENT_HEAD"),
            [self.hod.user.pk],
        )
        # The head of department herself reaches the division's head.
        self.assertEqual(
            self._resolved(self.hod.user, "DEPARTMENT_HEAD"), [self.registrar.user.pk],
        )

    def test_a_vacant_manager_post_parks_rather_than_reaching_further(self):
        StaffOrganogramService.close_for_exit(self.hod)
        self.assertEqual(self._resolved(self.ikeja_teacher.user, "DIRECT_MANAGER"), [])

    def test_a_suspended_manager_is_passed_over_as_an_approver(self):
        # She keeps her post on the chart but cannot sign in to decide anything.
        employment.change_status(
            self.hod, to_status=EmploymentStatus.SUSPENDED, actor=self.admin,
            reason="Under review",
        )
        self.assertEqual(self._resolved(self.ikeja_teacher.user, "DIRECT_MANAGER"), [])
        # The department head climb walks on to the next unit's head instead.
        self.assertEqual(
            self._resolved(self.ikeja_teacher.user, "DEPARTMENT_HEAD"),
            [self.registrar.user.pk],
        )

    def test_specific_position_reaches_the_named_posts_active_holder(self):
        template, instance = self._instance(self.ikeja_teacher.user)
        stage = self._stage(template, "SPECIFIC_POSITION")
        stage.organogram_tenant_position_id = self.principal.pk
        stage.save(update_fields=["organogram_tenant_position_id"])

        from vs_workflow.services.approvers import resolve_approvers

        self.assertEqual(
            [approver.user.pk for approver in resolve_approvers(stage, instance)],
            [self.registrar.user.pk],
        )

    def test_specific_position_naming_no_post_reaches_nobody(self):
        self.assertEqual(self._resolved(self.ikeja_teacher.user, "SPECIFIC_POSITION"), [])

    def test_a_climb_never_crosses_into_another_school(self):
        # Sunrise's requester holds a Sunrise post; asked inside Brightfield the
        # climb finds no record of them and reaches nobody.
        self.assertEqual(
            StaffOrganogramService.resolve_direct_manager(self.solo_staff.user, self.tenant),
            [],
        )
        self.assertEqual(
            self._resolved(self.solo_staff.user, "DIRECT_MANAGER", tenant=self.tenant), [],
        )


# ── A named post, in approval steps and approver groups ────────────────────


class WorkflowNamedPostTests(OrganogramFixture):
    """A school's approval step or approver group names one post on its own chart.

    The code a person types is looked up on the chart the owning tenant uses:
    Brightfield's own for Brightfield, the CX chart for a central template. A
    code that chart does not have is refused naming it, whether it exists on the
    other chart, on another school's, or nowhere. What is stored is the post's
    id, and every read shows the post's current code and title.
    """

    WORKFLOW_KEYS = (
        "workflow.template.view", "workflow.template.publish",
        "workflow.template.update",
        "workflow.group.view", "workflow.group.create", "workflow.group.update",
    )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in cls.WORKFLOW_KEYS:
            make_role_permission(cls.role, make_permission(key, scope=PermissionScope.TENANT))

    def setUp(self):
        self.appoint(self.registrar, self.principal)
        self.hod = self.make_staff(
            "hod@brightfield.test", "Ngozi", "Okafor", branch=self.ikeja,
        )
        self.appoint(self.hod, self.ikeja_hod)

    # ── builders ───────────────────────────────────────────────────────────

    def publish(self, code, tenant="own", **stage):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            tenant=self.tenant if tenant == "own" else tenant,
            document_type="NAMED_POST_DOC", code="named-post", name="Named post",
            stages_payload=[{
                "code": "post", "label": "Named post", "kind": "APPROVAL",
                "order": 1, "approver_source": "ORGANOGRAM",
                "organogram_target": "SPECIFIC_POSITION",
                "organogram_position_code": code, **stage,
            }],
        )

    def instance_for(self, template, requester, tenant=None):
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        from vs_workflow.models import WorkflowInstance, WorkflowTemplate

        return WorkflowInstance.objects.create(
            tenant=tenant or self.tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id="doc", document_type=template.document_type,
            status="IN_PROGRESS", requested_by=requester, submitted_at=timezone.now(),
        )

    def group_naming(self, position, tenant=None, code="post-holders"):
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        group = WorkflowApproverGroup.all_objects.create(
            tenant=tenant or self.tenant, code=code, name="Post holders",
        )
        WorkflowApproverGroupMember.objects.create(
            group=group, kind="POSITION", tenant_position_id=position.pk,
        )
        return group

    @staticmethod
    def body(response):
        """The payload, inside the success envelope where the renderer adds one."""
        payload = response.json()
        return payload["data"] if isinstance(payload, dict) and "data" in payload else payload

    def cx_seat(self, code="CX-AUDIT"):
        from vs_user.models import OrgNode, Position

        node = OrgNode.objects.create(code="DV-NAMED", name="Audit", kind="DIVISION")
        return Position.objects.create(title="Group Auditor", code=code, org_node=node)

    # ── binding a code ─────────────────────────────────────────────────────

    def test_publishing_a_school_post_stores_its_id(self):
        stage = self.publish("ihod").stages.get()
        self.assertEqual(stage.organogram_tenant_position_id, self.ikeja_hod.pk)
        self.assertIsNone(stage.organogram_position_id)

    def test_publishing_over_the_api_refuses_an_unknown_code_with_400(self):
        body = {
            "document_type": "NAMED_POST_DOC", "code": "named-post", "name": "Named post",
            "stages": [{
                "code": "post", "label": "Named post", "approver_source": "ORGANOGRAM",
                "organogram_target": "SPECIFIC_POSITION",
                "organogram_position_code": "NOPE",
            }],
        }
        response = self.post(self.admin, "workflow-template-publish", body)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("NOPE", response.json()["message"])
        self.assertEqual(response.json()["error"]["code"], "UNKNOWN_POSITION")

        body["stages"][0]["organogram_position_code"] = "IHOD"
        response = self.post(self.admin, "workflow-template-publish", body)
        self.assertEqual(response.status_code, 201, response.content)
        [stage] = self.body(response)["stages"]
        self.assertEqual(stage["organogram_position_code"], "IHOD")

    def test_an_unknown_code_is_refused_and_nothing_is_published(self):
        from vs_workflow.exceptions import UnknownPositionError
        from vs_workflow.models import WorkflowTemplate

        with self.assertRaises(UnknownPositionError) as refused:
            self.publish("NOPE")
        self.assertIn("'NOPE'", refused.exception.message)
        self.assertEqual(refused.exception.http_status, 400)
        self.assertFalse(
            WorkflowTemplate.all_objects.filter(document_type="NAMED_POST_DOC").exists(),
        )

    def test_a_school_cannot_name_a_platform_seat(self):
        from vs_workflow.exceptions import UnknownPositionError

        self.cx_seat()
        with self.assertRaises(UnknownPositionError):
            self.publish("CX-AUDIT")

    def test_a_central_template_cannot_name_a_school_post(self):
        from vs_workflow.exceptions import UnknownPositionError

        with self.assertRaises(UnknownPositionError):
            self.publish("IHOD", tenant=None)
        seat = self.cx_seat()
        stage = self.publish("CX-AUDIT", tenant=None).stages.get()
        self.assertEqual(
            (stage.organogram_position_id, stage.organogram_tenant_position_id),
            (seat.pk, None),
        )

    def test_another_schools_post_code_names_nothing(self):
        from vs_workflow.exceptions import UnknownPositionError
        from vs_workflow.models import WorkflowStage

        with self.assertRaises(UnknownPositionError):
            self.publish("SHEAD")
        self.assertFalse(
            WorkflowStage.objects.filter(
                organogram_tenant_position_id=self.solo_post.pk,
            ).exists(),
        )

    def test_a_group_member_binds_a_school_post_and_not_another_schools(self):
        from vs_workflow.models import WorkflowApproverGroup

        group = WorkflowApproverGroup.all_objects.create(
            tenant=self.tenant, code="heads", name="Heads",
        )
        refused = self.post(
            self.admin, "workflow-approver-group-add-member",
            {"kind": "POSITION", "position_code": "SHEAD"}, pk=group.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.content)
        self.cx_seat()
        refused = self.post(
            self.admin, "workflow-approver-group-add-member",
            {"kind": "POSITION", "position_code": "CX-AUDIT"}, pk=group.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertFalse(group.members.exists())

        added = self.post(
            self.admin, "workflow-approver-group-add-member",
            {"kind": "POSITION", "position_code": "PRIN"}, pk=group.pk,
        )
        self.assertEqual(added.status_code, 201, added.content)
        member = group.members.get()
        self.assertEqual((member.position_id, member.tenant_position_id),
                         (None, self.principal.pk))
        again = self.post(
            self.admin, "workflow-approver-group-add-member",
            {"kind": "POSITION", "position_code": "PRIN"}, pk=group.pk,
        )
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(group.members.count(), 1)

    # ── resolving it ───────────────────────────────────────────────────────

    def test_the_builders_preview_names_the_posts_holder(self):
        body = {
            "requester": str(self.ikeja_teacher.user.pk),
            "approver_source": "ORGANOGRAM", "organogram_target": "SPECIFIC_POSITION",
            "organogram_position_code": "IHOD",
        }
        response = self.post(self.admin, "workflow-template-preview-approvers", body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [row["user"]["id"] for row in self.body(response)["approvers"]],
            [str(self.hod.user.pk)],
        )
        body["organogram_position_code"] = "SHEAD"
        response = self.post(self.admin, "workflow-template-preview-approvers", body)
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_published_step_reaches_the_posts_holder_and_never_the_requester(self):
        from vs_workflow.services.approvers import resolve_approvers

        template = self.publish("IHOD")
        stage = template.stages.get()
        resolved = resolve_approvers(stage, self.instance_for(template, self.ikeja_teacher.user))
        self.assertEqual([a.user.pk for a in resolved], [self.hod.user.pk])
        # Raised by the holder herself, the post has nobody else to decide it.
        self.assertEqual(resolve_approvers(stage, self.instance_for(template, self.hod.user)), [])

    def test_a_group_naming_a_post_resolves_its_holders_able_to_act(self):
        from vs_workflow.services.approvers import resolve_group_users

        group = self.group_naming(self.ikeja_hod)
        self.assertEqual(
            [u.pk for u in resolve_group_users(group, self.tenant)], [self.hod.user.pk],
        )
        # Suspended, she keeps the post on the chart and is passed over here.
        employment.change_status(
            self.hod, to_status=EmploymentStatus.SUSPENDED, actor=self.admin,
            reason="Under review",
        )
        self.assertEqual(resolve_group_users(group, self.tenant), [])

    def test_a_post_id_from_another_school_reaches_nobody(self):
        from vs_workflow.services.approvers import resolve_approvers, resolve_group_users

        # Sunrise's group holding Brightfield's post id, however it got there.
        group = self.group_naming(self.ikeja_hod, tenant=self.solo.tenant)
        self.assertEqual(resolve_group_users(group, self.solo.tenant), [])
        # Brightfield's step resolved inside Sunrise finds nobody either.
        template = self.publish("IHOD")
        instance = self.instance_for(template, self.solo_staff.user, tenant=self.solo.tenant)
        self.assertEqual(resolve_approvers(template.stages.get(), instance), [])
        self.assertEqual(
            StaffOrganogramService.resolve_position_holders(self.ikeja_hod.pk, self.solo.tenant),
            [],
        )
        self.assertIsNone(StaffOrganogramService.find_position("IHOD", self.solo.tenant))
        self.assertEqual(
            StaffOrganogramService.describe_positions([self.ikeja_hod.pk], self.solo.tenant), {},
        )

    # ── reading it back ────────────────────────────────────────────────────

    def test_reads_show_the_posts_current_code_and_title(self):
        from vs_workflow.services.approvers import describe_group_members

        group = self.group_naming(self.ikeja_hod)
        self.publish("IHOD")
        # Codes are editable on the chart; the stored id follows the post.
        self.ikeja_hod.code = "IKHOD"
        self.ikeja_hod.save(update_fields=["code"])

        [row] = describe_group_members(group, self.tenant)
        self.assertEqual(
            (row["label"], row["target_code"], row["resolved_count"]),
            ("Head of Sciences, Ikeja", "IKHOD", 1),
        )
        data = self.body(self.get(self.admin, "workflow-approver-group-detail", pk=group.pk))
        [member] = data["members"]
        self.assertEqual(
            (member["position_code"], member["position_title"]),
            ("IKHOD", "Head of Sciences, Ikeja"),
        )
        resolved = self.body(self.get(self.admin, "workflow-approver-group-resolve", pk=group.pk))
        self.assertEqual(resolved["members"][0]["target_code"], "IKHOD")

        listed = self.body(self.get(self.admin, "workflow-template-list"))
        rows = listed["results"] if isinstance(listed, dict) else listed
        [template] = [t for t in rows if t["document_type"] == "NAMED_POST_DOC"]
        self.assertEqual(template["stages"][0]["organogram_position_code"], "IKHOD")

    def test_a_page_of_groups_describes_its_posts_in_one_lookup(self):
        from vs_workflow.models import WorkflowApproverGroupMember

        for i in range(4):
            group = self.group_naming(self.ikeja_hod, code=f"g{i}")
            WorkflowApproverGroupMember.objects.create(
                group=group, kind="POSITION", tenant_position_id=self.principal.pk,
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.get(self.admin, "workflow-approver-group-list")
        self.assertEqual(response.status_code, 200, response.content)
        post_lookups = [
            q for q in queries.captured_queries
            if 'FROM "vs_staff_staffposition"' in q["sql"]
        ]
        self.assertEqual(len(post_lookups), 1, [q["sql"] for q in post_lookups])

    def test_the_parked_sentence_names_the_post(self):
        from vs_workflow.services.release import stage_requirement

        stage = self.publish("IHOD").stages.get()
        self.assertEqual(
            stage_requirement(stage),
            "put somebody in the Head of Sciences, Ikeja position",
        )

    def test_a_comparison_shows_a_changed_post(self):
        from vs_workflow.services.comparison import compare_templates

        self.cx_seat()
        base = self.publish("CX-AUDIT", tenant=None)
        other = self.publish("IHOD")
        [changed] = compare_templates(base, other)["stages"]["changed"]
        [field] = [f for f in changed["fields"] if f["field"] == "organogram_position_code"]
        self.assertEqual((field["base"], field["other"]), ("CX-AUDIT", "IHOD"))

        same = self.publish("IHOD", tenant=self.tenant)
        self.assertEqual(compare_templates(other, same)["stages"]["changed"], [])

    # ── deleting a post something names ───────────────────────────────────

    def test_a_post_a_step_or_a_group_names_is_not_deleted(self):
        spare = StaffPosition.all_objects.create(
            tenant=self.tenant, title="Bursar", code="BURS", org_node=self.academics,
        )
        self.publish("BURS")
        response = self.delete(self.admin, "staff-org-position-detail", pk=spare.pk)
        self.assertEqual(response.status_code, 409, response.content)
        detail = response.json()["error"]["detail"]
        self.assertEqual((detail["workflow_stages"], detail["approver_group_members"]), (1, 0))
        self.assertIn("1 approval step naming it", response.json()["message"])

        self.group_naming(spare)
        detail = self.delete(
            self.admin, "staff-org-position-detail", pk=spare.pk,
        ).json()["error"]["detail"]
        self.assertEqual((detail["workflow_stages"], detail["approver_group_members"]), (1, 1))
        self.assertTrue(StaffPosition.all_objects.filter(pk=spare.pk).exists())

    def test_a_post_nothing_names_is_deleted_and_another_schools_use_is_not_counted(self):
        spare = StaffPosition.all_objects.create(
            tenant=self.tenant, title="Bursar", code="BURS", org_node=self.academics,
        )
        # Sunrise's group carrying the same id is Sunrise's business, not a use here.
        self.group_naming(spare, tenant=self.solo.tenant)
        response = self.delete(self.admin, "staff-org-position-detail", pk=spare.pk)
        self.assertEqual(response.status_code, 200, response.content)
