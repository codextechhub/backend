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


class ClassSeatsTests(StudentsFixture):
    """One aggregate for every class, so three pickers cannot disagree."""

    def test_every_visible_class_is_listed_with_its_load(self):
        student = self.student(branch=self.lekki, first="Ada", last="Seat")
        self.place(student, self.shared_class)

        rows = self.get(self.admin, "student-class-seats").data["data"]
        by_id = {r["id"]: r for r in rows}
        self.assertIn(self.shared_class.pk, by_id)
        self.assertEqual(by_id[self.shared_class.pk]["used"], 1)
        self.assertEqual(
            by_id[self.shared_class.pk]["capacity"], self.shared_class.capacity,
        )

    def test_a_class_with_no_capacity_is_listed_rather_than_dropped(self):
        """A class with no limit recorded is not the same as a full one."""
        from schools.vs_academics.models import SchoolClass

        loose = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.jss1, session=self.year,
            name="JSS1 Open", code="JSS1OPEN", arm="Open", capacity=None,
            branch=None,
        )
        rows = self.get(self.admin, "student-class-seats").data["data"]
        row = next(r for r in rows if r["id"] == loose.pk)
        self.assertIsNone(row["capacity"])
        self.assertIsNone(row["remaining"])

    def test_it_agrees_with_the_roster_it_is_meant_to_replace(self):
        """The whole point: the picker and the register cannot drift."""
        student = self.student(branch=self.lekki, first="Obi", last="Agree")
        self.place(student, self.shared_class)

        seats = next(
            r for r in self.get(self.admin, "student-class-seats").data["data"]
            if r["id"] == self.shared_class.pk
        )
        roster = self.get(
            self.admin, "student-class-roster", class_id=self.shared_class.pk,
        )
        self.assertEqual(seats["used"], roster.data["seats_used"])
        self.assertEqual(seats["capacity"], roster.data["capacity"])

    def test_another_year_s_classes_are_not_offered(self):
        """A school has one JSS1 A per year, and they are all called JSS1 A.

        Listing them together hands a picker two identical options, and the one
        from the year that ended is refused on save by
        assert_class_is_in_session - a name the registrar recognises and a
        refusal they cannot explain.
        """
        from schools.vs_academics.models import (
            AcademicSession,
            Level,
            SchoolClass,
            SessionStatus,
        )

        last_year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2024/2025",
            start_date=dt.date(2024, 9, 1), end_date=dt.date(2025, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        # Its own level, because a level belongs to a year as well and the
        # class name is unique per level - which is precisely what makes the
        # two JSS1 As indistinguishable on screen.
        stale_level = Level.all_objects.create(
            tenant=self.tenant, program=self.jss1.program, session=last_year,
            name="JSS1", code="JSS1-OLD", order_index=1,
        )
        stale = SchoolClass.all_objects.create(
            tenant=self.tenant, level=stale_level, session=last_year,
            name="JSS1 A", code="JSS1A-OLD", arm="A", capacity=30, branch=None,
        )

        ids = [r["id"] for r in self.get(self.admin, "student-class-seats").data["data"]]
        self.assertIn(self.shared_class.pk, ids)
        self.assertNotIn(stale.pk, ids)

    def test_it_narrows_with_the_branch_lens(self):
        rows = self.get(
            self.admin, "student-class-seats", params={"branch": self.ikeja.pk},
        ).data["data"]
        for row in rows:
            with self.subTest(cls=row["name"]):
                # That branch's classes, plus the school-wide ones.
                self.assertIn(row["branch"], (self.ikeja.pk, None))


class AdmissionSuggestionTests(StudentsFixture):
    """The next number, read from what the school already issues."""

    def suggestion(self):
        return self.get(
            self.admin, "student-admission-policy",
        ).data["data"]["suggestion"]

    def test_nothing_is_suggested_before_the_school_has_issued_anything(self):
        """There is no series to continue, and a guess would be invented."""
        self.assertEqual(self.suggestion(), "")

    def test_it_continues_the_school_s_own_format(self):
        self.student(
            branch=self.lekki, first="A", last="One", number="BFS/2025/0142",
        )
        self.assertEqual(self.suggestion(), "BFS/2025/0143")

    def test_it_continues_a_format_that_is_not_brightfield_s(self):
        """A regular expression cannot be inverted; the school's own rows can."""
        self.student(
            branch=self.lekki, first="B", last="Two", number="CSS-24-0117",
        )
        self.assertEqual(self.suggestion(), "CSS-24-0118")

    def test_zero_padding_survives_and_only_grows_on_a_real_overflow(self):
        self.student(
            branch=self.lekki, first="C", last="Three", number="BFS/2025/0099",
        )
        self.assertEqual(self.suggestion(), "BFS/2025/0100")

    def test_a_taken_successor_is_skipped(self):
        self.student(
            branch=self.lekki, first="D", last="Four", number="BFS/2025/0007",
        )
        self.student(
            branch=self.lekki, first="E", last="Five", number="BFS/2025/0008",
        )
        # The most recent is 0008, so the next free one is 0009.
        self.assertEqual(self.suggestion(), "BFS/2025/0009")

    def test_nothing_is_suggested_when_the_successor_breaks_the_school_s_rule(self):
        """A year inside the number is why this matters at a session boundary.

        Offering BFS/2025/0143 in the 2026 session would be a confident wrong
        answer, and an empty box beats a plausible one nobody checks.
        """
        from ..services.policy import write_policy

        write_policy(
            self.tenant, self.admin,
            required=False, pattern=r"BFS/2026/\d{4}", hint="BFS/2026/NNNN",
        )
        self.student(
            branch=self.lekki, first="F", last="Six", number="BFS/2025/0142",
        )
        self.assertEqual(self.suggestion(), "")

    def test_a_number_ending_in_no_digits_suggests_nothing(self):
        self.student(
            branch=self.lekki, first="G", last="Seven", number="BFS/2025/A",
        )
        self.assertEqual(self.suggestion(), "")


class RosterReadsItsOwnYearTests(StudentsFixture):
    """A register belongs to its class's year, not to whichever year is active.

    A class belongs to a year, so a school has one JSS1 A per session. Reading the
    roster against the ACTIVE year answered for the wrong one the moment the
    class was not this year's - an empty register and "0 of 30", with nothing
    saying which year had been looked in.
    """

    def setUp(self):
        super().setUp()
        from schools.vs_academics.models import (
            AcademicSession,
            Level,
            SchoolClass,
            SessionStatus,
        )

        self.next_year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2031/2032",
            start_date=dt.date(2031, 9, 1), end_date=dt.date(2032, 7, 31),
            status=SessionStatus.DRAFT,
        )
        # Levels are per-year too, which is what makes the class constraint on
        # (name, level) hold across years: next year's JSS1 A hangs off next
        # year's JSS1, not this one's.
        next_jss1 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS1", code="JSS1", order_index=1,
        )
        # Next year's JSS1 A: same name, a different year.
        self.next_class = SchoolClass.all_objects.create(
            tenant=self.tenant, level=next_jss1, session=self.next_year,
            name="JSS1 A", code="JSS1A32", arm="A", capacity=30, branch=None,
        )
        self.student_here = self.student(first="Ada", last="Thisyear")
        self.place(self.student_here, self.shared_class)
        self.student_next = self.student(first="Obi", last="Nextyear")
        self.place(
            self.student_next, self.next_class, session=self.next_year,
        )

    def roster(self, school_class):
        return self.get(
            self.admin, "student-class-roster", class_id=school_class.pk,
        )

    def test_next_years_class_reports_its_own_roll(self):
        """The defect: this answered 0 rows and 0 seats for a class of one."""
        response = self.roster(self.next_class)
        self.assertEqual(response.data["pagination"]["totalItems"], 1)
        self.assertEqual(response.data["seats_used"], 1)

    def test_this_years_class_is_unchanged(self):
        response = self.roster(self.shared_class)
        self.assertEqual(response.data["pagination"]["totalItems"], 1)
        self.assertEqual(response.data["seats_used"], 1)

    def test_two_years_of_the_same_class_do_not_bleed_into_each_other(self):
        """Both are called JSS1 A, which is exactly why this went unnoticed."""
        here = {r["full_name"] for r in self.roster(self.shared_class).data["data"]}
        nxt = {r["full_name"] for r in self.roster(self.next_class).data["data"]}
        self.assertEqual(here, {"Ada Thisyear"})
        self.assertEqual(nxt, {"Obi Nextyear"})


class SessionLensTests(StudentsFixture):
    """The roll and the class both belong to a year.

    A student in SSS1 A last year is in SSS2 A this year, and a student enrolled
    this year was not on last year's roll at all - so "which year" is a real
    question about students even though status carries no year.
    """

    def setUp(self):
        super().setUp()
        from schools.vs_academics.models import (
            AcademicSession,
            Level,
            SchoolClass,
            SessionStatus,
        )

        self.next_year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2041/2042",
            start_date=dt.date(2041, 9, 1), end_date=dt.date(2042, 7, 31),
            status=SessionStatus.DRAFT,
        )
        next_jss2 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS2", code="JSS2", order_index=2,
        )
        self.next_class = SchoolClass.all_objects.create(
            tenant=self.tenant, level=next_jss2, session=self.next_year,
            name="JSS2 A", code="JSS2A42", arm="A", capacity=30, branch=None,
        )
        # Moves up: this year in JSS1 A, next year in JSS2 A.
        self.mover = self.student(first="Ada", last="Mover")
        self.place(self.mover, self.shared_class)
        moved = self.place(self.mover, self.next_class, session=self.next_year)
        # A promotion leaves the OLD row inactive, which is the trap: filtering
        # on is_active answers "nobody was in JSS1 A last year".
        self.mover.enrolments.filter(session=self.year).update(is_active=False)
        self.assertTrue(moved)
        # Only ever on next year's roll.
        self.newcomer = self.student(first="Obi", last="Newcomer")
        self.place(self.newcomer, self.next_class, session=self.next_year)

    def rows(self, session=None):
        params = {"session": session.pk} if session else None
        return self.get(self.admin, "student-list", params=params).data

    def test_the_roll_differs_between_years(self):
        this_year = {r["full_name"] for r in self.rows(self.year)["data"]}
        next_year = {r["full_name"] for r in self.rows(self.next_year)["data"]}
        self.assertIn("Ada Mover", this_year)
        self.assertIn("Ada Mover", next_year)
        # The newcomer has no placement in the older year, so is not on its roll.
        self.assertNotIn("Obi Newcomer", this_year)
        self.assertIn("Obi Newcomer", next_year)

    def test_a_students_class_is_the_one_they_held_that_year(self):
        """The defect this exists to prevent: one class shown for every year."""
        def klass(session):
            row = next(
                r for r in self.rows(session)["data"] if r["full_name"] == "Ada Mover"
            )
            return r if False else row["class_name"]

        self.assertEqual(klass(self.year), "JSS1 A")
        self.assertEqual(klass(self.next_year), "JSS2 A")

    def test_a_superseded_placement_still_counts_as_having_been_on_the_roll(self):
        """is_active marks the CURRENT placement, not the fact of a placement."""
        names = {r["full_name"] for r in self.rows(self.year)["data"]}
        self.assertIn("Ada Mover", names)

    def test_the_summary_describes_the_same_roll_as_the_list(self):
        for session in (self.year, self.next_year):
            with self.subTest(session=session.name):
                listed = self.rows(session)["pagination"]["totalItems"]
                summary = self.get(
                    self.admin, "student-summary", params={"session": session.pk},
                ).data["data"]
                self.assertEqual(summary["on_roll"], listed)

    def test_the_summary_admits_that_status_has_no_year(self):
        plain = self.get(self.admin, "student-summary").data["data"]
        lensed = self.get(
            self.admin, "student-summary", params={"session": self.year.pk},
        ).data["data"]
        self.assertFalse(plain["status_is_current"])
        self.assertTrue(lensed["status_is_current"])

    def test_an_unknown_year_is_refused_rather_than_ignored(self):
        response = self.get(self.admin, "student-list", params={"session": 999999})
        self.assertEqual(response.status_code, 400)


class SessionLensReachesEveryListTests(SessionLensTests):
    """The year narrows the whole section, not the directory alone.

    A section where the directory answers about one year and the guardian list
    beside it answers about another is the same screen contradicting itself one
    click apart - the defect the branch lens already had once.
    """

    def test_the_guardian_directory_follows_the_year(self):
        """A guardian carries no year, so the narrowing is on their wards."""
        self.link(self.newcomer, self.guardian(
            name="Mrs. Only Next Year", phone="08035550999",
            email="nextyear@example.ng",
        ))
        def names(session):
            return {
                g["full_name"]
                for g in self.get(
                    self.admin, "guardian-list", params={"session": session.pk},
                ).data["data"]
            }

        self.assertNotIn("Mrs. Only Next Year", names(self.year))
        self.assertIn("Mrs. Only Next Year", names(self.next_year))

    def test_class_seats_follow_the_year(self):
        """A class belongs to a year, so last year's had last year's loads."""
        def used(session, class_id):
            rows = self.get(
                self.admin, "student-class-seats", params={"session": session.pk},
            ).data["data"]
            return next((r["used"] for r in rows if r["id"] == class_id), None)

        # The mover sits in this year's JSS1 A and next year's JSS2 A.
        self.assertEqual(used(self.year, self.shared_class.pk), 1)
        self.assertEqual(used(self.next_year, self.next_class.pk), 2)

    def test_the_unplaced_worklist_stays_on_the_running_year(self):
        """Deliberate: placing happens now, and a closed year refuses writes."""
        plain = self.get(self.admin, "student-unplaced").data["pagination"]["totalItems"]
        asked = self.get(
            self.admin, "student-unplaced", params={"session": self.next_year.pk},
        ).data["pagination"]["totalItems"]
        self.assertEqual(plain, asked)

    def test_the_guardian_directory_is_ordered_so_pages_are_stable(self):
        """Postgres gives an unordered query no stable order between pages."""
        rows = self.get(self.admin, "guardian-list").data["data"]
        names = [g["full_name"] for g in rows]
        self.assertEqual(names, sorted(names))


class GuardianEditTests(StudentsFixture):
    """A guardian's own details can be corrected.

    Until this route existed they could not be, anywhere: the create path was
    the only writer, and linking an EXISTING guardian passes their id and drops
    every other field. A phone number mistyped at enrolment was permanent, and
    the only workaround was a second record for the same parent - which splits
    the household and breaks the sibling link.
    """

    def setUp(self):
        super().setUp()
        self.g = self.guardian(
            name="Mrs. Patricia Okafor", phone="08065550130",
            email="patricia@example.ng",
        )
        self.child = self.student(first="Tobi", last="Okafor")
        self.link(self.child, self.g)

    def test_a_mistyped_number_can_be_corrected(self):
        response = self.patch(
            self.admin, "guardian-detail", {"phone": "08065550131"}, pk=self.g.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.g.refresh_from_db()
        self.assertEqual(self.g.phone, "08065550131")

    def test_only_the_named_fields_move(self):
        self.patch(self.admin, "guardian-detail", {"phone": "0806"}, pk=self.g.pk)
        self.g.refresh_from_db()
        self.assertEqual(self.g.full_name, "Mrs. Patricia Okafor")
        self.assertEqual(self.g.email, "patricia@example.ng")

    def test_an_unchanged_save_is_not_an_edit(self):
        """Or the history fills with entries for edits nobody made."""
        response = self.patch(
            self.admin, "guardian-detail",
            {"phone": self.g.phone}, pk=self.g.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing to change", response.data["message"])

    def test_an_email_another_guardian_holds_is_refused_by_name(self):
        """Email is unique per school, so this must not surface as a 500."""
        other = self.guardian(
            name="Mr. Emeka Obi", phone="08065550999", email="emeka@example.ng",
        )
        response = self.patch(
            self.admin, "guardian-detail", {"email": other.email}, pk=self.g.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Mr. Emeka Obi", str(response.data))

    def test_a_blank_name_is_refused(self):
        """A guardian with no name cannot be picked out of a ward list."""
        response = self.patch(
            self.admin, "guardian-detail", {"full_name": "   "}, pk=self.g.pk,
        )
        self.assertEqual(response.status_code, 400)

    def test_reading_needs_view_but_writing_needs_update(self):
        from vs_rbac.tests.helpers import (
            make_assignment, make_role, make_role_permission, make_school_admin,
        )

        role = make_role(self.school, name="Reader", key="guardian_reader")
        make_role_permission(role, self.permissions["school.students.view"])
        viewer = make_school_admin(
            None, email="guardian-reader@test.example", tenant=self.tenant,
        )
        make_assignment(self.school, viewer, role, branch=None)
        self.assertEqual(
            self.get(viewer, "guardian-detail", pk=self.g.pk).status_code, 200,
        )
        self.assertEqual(
            self.patch(
                viewer, "guardian-detail", {"phone": "0806"}, pk=self.g.pk,
            ).status_code,
            403,
        )

    def test_another_schools_guardian_is_404_not_403(self):
        """An id must not reveal that a person exists at another school."""
        theirs = self.guardian(
            tenant=self.solo.tenant, name="Mrs. Elsewhere",
            phone="08065550777", email="elsewhere@example.ng",
        )
        response = self.patch(
            self.admin, "guardian-detail", {"phone": "0806"}, pk=theirs.pk,
        )
        self.assertEqual(response.status_code, 404)
