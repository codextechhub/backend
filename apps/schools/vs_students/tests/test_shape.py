"""Response shape, query cost, and the constraints that must bite.

FRD M11 v2.4 sections 12.3 and 12.4.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, connection, transaction

from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentDocument,
)

from .base import StudentsFixture


class EmptyShapeTests(StudentsFixture):
    """``success_response`` coerces a genuinely absent payload to ``{}``.

    An empty LIST must stay a list, or every caller doing ``data.map(...)``
    crashes on the empty case - which is the shape bug this assertion exists
    to pin.
    """

    def test_every_list_endpoint_answers_an_empty_list_not_an_object(self):
        for name, kwargs in (
            ("student-list", {}),
            ("student-unplaced", {}),
            ("guardian-list", {}),
        ):
            with self.subTest(endpoint=name):
                response = self.get(self.admin, name, **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data["success"])
                self.assertEqual(response.data["data"], [])

    def test_every_list_endpoint_returns_the_pagination_block(self):
        for name in ("student-list", "student-unplaced", "guardian-list"):
            with self.subTest(endpoint=name):
                response = self.get(self.admin, name)
                self.assertIn("pagination", response.data)
                self.assertIn("totalItems", response.data["pagination"])

    def test_the_summary_of_an_empty_school_is_zeros_and_not_an_error(self):
        response = self.get(self.admin, "student-summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["total"], 0)
        self.assertEqual(response.data["data"]["unassigned"], 0)

    def test_a_student_with_no_class_gets_empty_lists_not_a_404(self):
        """Having no class is an ordinary state, not a missing page."""
        row = self.student(status=StudentStatus.ENROLLED)
        for name in ("student-subjects", "student-class-history"):
            with self.subTest(endpoint=name):
                response = self.get(self.admin, name, pk=row.pk)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"], [])


class QueryCostTests(StudentsFixture):
    """The query count must not grow with the page size.

    Without the prefetches each row fetches its own class and its own
    guardians, and a page of fifty students costs a hundred and fifty queries.
    """

    def _fill(self, n):
        for i in range(n):
            child = self.student(first=f"Child{i}", last="Filler")
            self.place(child)
            g = self.guardian(
                name=f"Parent {i}", phone=f"0800000{i:04d}",
                email=f"parent{i}@example.ng",
            )
            self.link(child, g)

    def test_a_page_of_ten_and_a_page_of_forty_cost_the_same(self):
        self._fill(40)
        client = self.client_for(self.admin)

        with self.assertNumQueries(0):
            pass  # warm nothing; the counts below are what matter.

        def cost(page_size):
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                response = client.get(
                    "/v1/students/",
                    {"tenant": self.tenant.slug, "page_size": page_size},
                )
                self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        small = cost(10)
        large = cost(40)
        self.assertEqual(
            small, large,
            f"{small} queries for 10 rows and {large} for 40 - the prefetch is "
            f"not doing its job.",
        )

    def test_a_roster_of_two_hundred_costs_the_same_as_one_of_ten(self):
        self._fill(200)
        client = self.client_for(self.admin)

        def cost(page_size):
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                response = client.get(
                    f"/v1/students/classes/{self.shared_class.pk}/roster/",
                    {"tenant": self.tenant.slug, "page_size": page_size},
                )
                self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        self.assertEqual(cost(10), cost(100))

    def test_the_summary_does_not_grow_with_the_roll(self):
        from django.test.utils import CaptureQueriesContext

        client = self.client_for(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            client.get("/v1/students/summary/", {"tenant": self.tenant.slug})
        empty = len(ctx.captured_queries)

        self._fill(60)
        with CaptureQueriesContext(connection) as ctx:
            client.get("/v1/students/summary/", {"tenant": self.tenant.slug})
        full = len(ctx.captured_queries)
        self.assertEqual(empty, full)


class ConstraintTests(StudentsFixture):
    """The constraints, proven at the database and not only in a service."""

    def test_the_student_number_is_unique_per_tenant_case_insensitively(self):
        self.student(number="BFS/2025/0142", first="First", last="Child")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Student.all_objects.create(
                tenant=self.tenant, branch=self.lekki,
                first_name="Second", last_name="Child",
                date_of_birth=dt.date(2013, 1, 1), gender=Gender.MALE,
                student_number="bfs/2025/0142",
            )

    def test_two_schools_may_hold_the_same_student_number(self):
        self.student(number="0001")
        other = Student.all_objects.create(
            tenant=self.solo.tenant, branch=self.solo_branch,
            first_name="Theirs", last_name="Own",
            date_of_birth=dt.date(2013, 1, 1), gender=Gender.MALE,
            student_number="0001",
        )
        self.assertIsNotNone(other.pk)

    def test_a_guardian_email_is_unique_per_tenant_case_insensitively(self):
        self.guardian(email="shared@example.ng")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Guardian.all_objects.create(
                tenant=self.tenant, full_name="Someone Else",
                phone="08000000000", email="SHARED@EXAMPLE.NG",
            )

    def test_two_schools_may_hold_a_guardian_on_the_same_email(self):
        self.guardian(email="shared@example.ng")
        other = Guardian.all_objects.create(
            tenant=self.solo.tenant, full_name="Same Person",
            phone="08000000000", email="shared@example.ng",
        )
        self.assertIsNotNone(other.pk)

    def test_two_guardians_cannot_point_at_one_user_in_one_tenant(self):
        Guardian.all_objects.create(
            tenant=self.tenant, full_name="A", phone="1", email="a@example.ng",
            user=self.lekki_head,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Guardian.all_objects.create(
                tenant=self.tenant, full_name="B", phone="2",
                email="b@example.ng", user=self.lekki_head,
            )

    def test_a_student_holds_at_most_one_document_of_each_type(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        row = self.student()
        for _ in range(2):
            response = self.client_for(self.admin).post(
                f"/v1/students/{row.pk}/documents/?tenant={self.tenant.slug}",
                {
                    "document_type": "BIRTH_CERTIFICATE",
                    "file": SimpleUploadedFile("b.pdf", b"x", "application/pdf"),
                },
                format="multipart",
            )
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            StudentDocument.all_objects.filter(
                student=row, document_type="BIRTH_CERTIFICATE",
            ).count(),
            1,
        )

    def test_the_declared_indexes_exist_on_the_columns_they_name(self):
        """Checked column by column, not by table name.

        A test that only asserts the table appears somewhere in pg_indexes
        passes on the primary key alone and would not notice if every index in
        section 7 had been dropped.
        """
        expected = [
            ("vs_students_student", ["tenant_id", "branch_id", "status"]),
            ("vs_students_student", ["tenant_id", "status"]),
            ("vs_students_student", ["tenant_id", "last_name", "first_name"]),
            ("vs_students_classenrolment", ["school_class_id", "is_active"]),
            ("vs_students_classenrolment", ["tenant_id", "session_id", "is_active"]),
            ("vs_students_guardian", ["tenant_id", "phone"]),
            ("vs_students_studentstatuslog", ["student_id", "changed_at"]),
            ("vs_students_studentdocument", ["tenant_id", "student_id"]),
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname='public'"
                if connection.vendor == "postgresql"
                else "SELECT tbl_name, sql FROM sqlite_master WHERE type='index'",
            )
            rows = [(t, (d or "")) for t, d in cursor.fetchall()]

        for table, columns in expected:
            with self.subTest(table=table, columns=columns):
                found = any(
                    t == table and all(c in definition for c in columns)
                    for t, definition in rows
                )
                self.assertTrue(
                    found, f"No index on {table} covering {columns}.",
                )


class MultiShapeTenancyTests(StudentsFixture):
    """Two tenants, and two shapes of school. One of each proves nothing."""

    def test_two_tenants_enrolling_at_once_do_not_see_each_other(self):
        self.post(self.admin, "student-list", self.enrolment_body())
        response = self.get(self.solo_admin, "student-list")
        self.assertEqual(response.data["data"], [])

    def test_a_caller_pinned_to_two_of_three_branches_sees_both_and_no_third(self):
        from vs_rbac.tests.helpers import (
            make_assignment, make_branch, make_school_admin,
        )

        third = make_branch(self.school, name="Yaba", is_main=False)
        lekki_child = self.student(branch=self.lekki, first="L", last="One")
        ikeja_child = self.student(branch=self.ikeja, first="I", last="Two")
        yaba_child = self.student(branch=third, first="Y", last="Three")

        user = make_school_admin(
            None, email="two@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.school, user, self.role, branch=self.lekki)
        make_assignment(self.school, user, self.role, branch=self.ikeja)

        response = self.get(user, "student-list")
        ids = {row["id"] for row in response.data["data"]}
        self.assertEqual(ids, {lekki_child.pk, ikeja_child.pk})
        self.assertNotIn(yaba_child.pk, ids)

    def test_a_shared_classs_roster_is_narrowed_but_the_class_is_not(self):
        """The one place a row is visible while part of its content is not.

        It follows from a school-wide class holding branch-bound children.
        """
        lekki_child = self.student(branch=self.lekki, first="L", last="One")
        ikeja_child = self.student(branch=self.ikeja, first="I", last="Two")
        self.place(lekki_child, self.shared_class)
        self.place(ikeja_child, self.shared_class)

        whole = self.get(
            self.admin, "student-class-roster", class_id=self.shared_class.pk,
        )
        self.assertEqual(len(whole.data["data"]), 2)

        pinned = self.get(
            self.lekki_head, "student-class-roster",
            class_id=self.shared_class.pk,
        )
        self.assertEqual(len(pinned.data["data"]), 1)
        # The seat count is the class's own fact and is NOT narrowed: a branch
        # admin who was shown one of two seats used would fill a class that is
        # already full.
        self.assertEqual(pinned.data["seats_used"], 2)
        self.assertEqual(whole.data["seats_used"], 2)
        self.assertEqual(pinned.data["capacity"], self.shared_class.capacity)

    def test_no_student_in_this_module_is_ever_unbranched(self):
        """There is no shared row here, so there is nothing to leak."""
        self.post(self.admin, "student-list", self.enrolment_body())
        self.assertFalse(
            Student.all_objects.filter(branch__isnull=True).exists(),
        )


class HistoryTabTests(StudentsFixture):
    """The profile's History tab, which answered 500 for every student.

    Two faults, one wrong assumption each, and the 403 test that already
    covered this route never reached either of them because permission is
    checked before the handler runs.

    1. ``AuditEvent`` stamps ``event_at``; the view ordered and read
       ``created_at``, the name the other models in this repo use.
    2. ``StudentHistoryView`` is a plain ``APIView`` and called
       ``paginate_queryset``, which only exists on ``GenericAPIView`` - the
       same trap ``base.get_serializer_context`` already documents.
    """

    def setUp(self):
        super().setUp()
        self.row = self.student()

    def _emit(self, summary="Home address updated.", action_type="UPDATE"):
        from vs_audit.models import AuditModuleKey
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key=AuditModuleKey.STUDENT, action_type=action_type,
            entity_type="Student", entity_id=str(self.row.pk),
            entity_label=self.row.full_name, tenant=self.tenant,
            actor_user=self.admin, summary=summary,
        )

    def test_the_history_tab_answers_with_an_audit_event_present(self):
        self._emit()
        response = self.get(self.admin, "student-history", pk=self.row.pk)
        self.assertEqual(response.status_code, 200)
        texts = [e["text"] for e in response.data["data"]]
        self.assertIn("Home address updated.", texts)

    def test_the_history_tab_paginates_from_a_plain_apiview(self):
        for i in range(3):
            self._emit(summary=f"Edit {i}.")
        response = self.get(self.admin, "student-history", pk=self.row.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("pagination", response.data)
        self.assertIn("totalItems", response.data["pagination"])

    def test_entries_are_newest_first(self):
        self._emit(summary="Older.")
        self._emit(summary="Newer.")
        response = self.get(self.admin, "student-history", pk=self.row.pk)
        whens = [e["when"] for e in response.data["data"]]
        self.assertEqual(whens, sorted(whens, reverse=True))

    def test_a_student_with_no_history_gets_an_empty_list_not_a_500(self):
        response = self.get(self.admin, "student-history", pk=self.row.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], [])


class BranchLensAgreementTests(StudentsFixture):
    """Every read narrows to the same branch, or the screen contradicts itself.

    ``?branch=`` lived in the student list and nowhere else. The directory's
    table narrowed while the four tiles above it kept answering for the whole
    school, so a registrar looking at one site read the other's totals - and
    nothing on the page said which number was which.
    """

    def setUp(self):
        super().setUp()
        self.here = self.student(branch=self.lekki, first="Ada", last="Here")
        self.place(self.here, self.shared_class)
        self.there = self.student(branch=self.ikeja, first="Obi", last="There")
        self.place(self.there, self.ikeja_class)
        # On the roll with no class, so `unplaced` has something at ONE branch.
        self.stray = self.student(
            branch=self.lekki, first="Ngozi", last="Stray",
            status=StudentStatus.ENROLLED,
        )
        for row in (self.here, self.there, self.stray):
            self.link(row, self.guardian(
                name=f"Guardian of {row.first_name}",
                phone=f"0803555{row.pk:04d}",
                email=f"{row.first_name.lower()}@example.ng",
            ))

    def totals(self, branch):
        params = {"branch": branch.pk} if branch else None
        listed = self.get(self.admin, "student-list", params=params)
        summary = self.get(self.admin, "student-summary", params=params)
        unplaced = self.get(self.admin, "student-unplaced", params=params)
        guardians = self.get(self.admin, "guardian-list", params=params)
        return {
            "list": listed.data["pagination"]["totalItems"],
            "summary": summary.data["data"]["total"],
            "unplaced": unplaced.data["pagination"]["totalItems"],
            "guardians": guardians.data["pagination"]["totalItems"],
        }

    def test_the_summary_narrows_with_the_list(self):
        """The defect itself: the table moved and the tiles did not."""
        lekki = self.totals(self.lekki)
        ikeja = self.totals(self.ikeja)
        self.assertNotEqual(lekki["summary"], ikeja["summary"])
        self.assertEqual(lekki["summary"], lekki["list"])
        self.assertEqual(ikeja["summary"], ikeja["list"])

    def test_the_branches_sum_to_the_whole_school(self):
        whole = self.totals(None)
        parts = self.totals(self.lekki), self.totals(self.ikeja)
        for key in ("list", "summary", "unplaced"):
            with self.subTest(endpoint=key):
                self.assertEqual(
                    parts[0][key] + parts[1][key], whole[key],
                    f"{key} does not split cleanly across the two branches",
                )

    def test_the_nav_badge_narrows_too(self):
        """A whole-school count beside one branch's roll sends somebody hunting."""
        self.assertEqual(self.totals(self.lekki)["unplaced"], 1)
        self.assertEqual(self.totals(self.ikeja)["unplaced"], 0)

    def test_guardians_narrow_by_the_children_they_stand_for(self):
        """A guardian carries no branch, so the wards are what narrows."""
        self.assertNotEqual(
            self.totals(self.lekki)["guardians"],
            self.totals(self.ikeja)["guardians"],
        )

    def test_a_single_branch_school_ignores_the_parameter(self):
        """The dimension has receded there; a branch filter is meaningless."""
        plain = self.get(self.solo_admin, "student-summary")
        asked = self.get(
            self.solo_admin, "student-summary",
            params={"branch": self.solo_branch.pk},
        )
        self.assertEqual(asked.status_code, 200)
        self.assertEqual(asked.data["data"]["total"], plain.data["data"]["total"])

    def test_an_unknown_branch_is_refused_rather_than_ignored(self):
        """A filter that quietly does nothing is worse than one that refuses."""
        response = self.get(
            self.admin, "student-summary", params={"branch": 999999},
        )
        self.assertEqual(response.status_code, 400)
