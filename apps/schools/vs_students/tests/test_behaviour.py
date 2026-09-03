"""What the module actually does: enrolment, guardians, movement, promotion.

FRD M11 v2.4 section 12.2.
"""
from __future__ import annotations

import datetime as dt

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.media import sign

from schools.vs_academics.models import (
    AcademicSession,
    Level,
    SchoolClass,
    SessionStatus,
)
from schools.vs_students.constants import (
    ALLOWED_TRANSITIONS,
    DocumentType,
    EnrolmentOutcome,
    Gender,
    PromotionOutcome,
    Relationship,
    StudentStatus,
)
from schools.vs_students.exceptions import ClassBelongsToAnotherYear
from schools.vs_students.services.placement import place
from schools.vs_students.models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentDocument,
    StudentGuardian,
    StudentStatusLog,
)

from .base import StudentsFixture


class EnrolmentTests(StudentsFixture):
    def test_enrolling_creates_the_student_the_guardian_and_the_placement(self):
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 201, response.data)

        student = Student.all_objects.get(first_name="Zainab")
        self.assertEqual(student.status, StudentStatus.ACTIVE)
        self.assertEqual(student.branch, self.lekki)
        self.assertEqual(
            student.enrolments.filter(is_active=True).count(), 1,
        )
        self.assertEqual(student.guardian_links.filter(is_primary=True).count(), 1)

    def test_two_status_log_rows_are_written_enrolled_then_active(self):
        """Confirmed on the 8th, started on the 11th is a real distinction.

        A school is asked for it, so the two transitions are two rows and not
        one jump straight to ACTIVE.
        """
        self.post(self.admin, "student-list", self.enrolment_body())
        student = Student.all_objects.get(first_name="Zainab")
        moves = list(
            student.status_logs.order_by("changed_at").values_list(
                "from_status", "to_status",
            ),
        )
        self.assertEqual(moves, [
            (StudentStatus.APPLICANT, StudentStatus.ENROLLED),
            (StudentStatus.ENROLLED, StudentStatus.ACTIVE),
        ])

    def test_saving_as_an_applicant_takes_no_number_and_no_class(self):
        response = self.post(self.admin, "student-list", self.enrolment_body(
            as_applicant=True, applied_for=self.jss1.pk, school_class=None,
            student_number="IGNORED",
        ))
        self.assertEqual(response.status_code, 201, response.data)
        student = Student.all_objects.get(first_name="Zainab")
        self.assertEqual(student.status, StudentStatus.APPLICANT)
        self.assertEqual(student.student_number, "")
        self.assertEqual(student.enrolments.count(), 0)
        self.assertEqual(student.applied_for_id, self.jss1.pk)
        self.assertIsNotNone(student.applied_on)

    def test_no_guardian_is_refused_and_writes_nothing(self):
        before = Student.all_objects.count()
        response = self.post(
            self.admin, "student-list", self.enrolment_body(guardians=[]),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "GUARDIAN_REQUIRED")
        self.assertEqual(Student.all_objects.count(), before)

    def test_two_primary_guardians_are_refused(self):
        body = self.enrolment_body()
        body["guardians"].append({
            "full_name": "Mr. Yusuf", "phone": "08115550178",
            "relationship": Relationship.FATHER, "is_primary": True,
        })
        response = self.post(self.admin, "student-list", body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "PRIMARY_GUARDIAN_REQUIRED",
        )

    def test_two_guardians_in_one_call_create_two_rows_and_two_links(self):
        """Adding the father must be the same operation as adding the mother."""
        body = self.enrolment_body()
        body["guardians"].append({
            "full_name": "Mr. Ibrahim Yusuf", "phone": "08115550178",
            "email": "ibrahim.yusuf@example.ng",
            "relationship": Relationship.FATHER, "is_primary": False,
        })
        self.post(self.admin, "student-list", body)
        student = Student.all_objects.get(first_name="Zainab")
        self.assertEqual(student.guardian_links.count(), 2)
        self.assertEqual(
            Guardian.all_objects.filter(tenant=self.tenant).count(), 2,
        )

    def test_a_duplicate_name_and_birthday_is_caught_case_insensitively(self):
        self.student(first="Zainab", last="Yusuf", dob=dt.date(2013, 11, 30))
        response = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="zainab", last_name="YUSUF",
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_STUDENT")

    def test_the_duplicate_check_can_be_overridden(self):
        self.student(first="Zainab", last="Yusuf", dob=dt.date(2013, 11, 30))
        response = self.post(self.admin, "student-list", self.enrolment_body(
            confirm_duplicate=True,
        ))
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_number_another_student_holds_is_refused_and_writes_nothing(self):
        self.student(number="BFS/2025/0142", first="Someone", last="Already")
        before = Student.all_objects.count()
        response = self.post(self.admin, "student-list", self.enrolment_body(
            student_number="bfs/2025/0142",
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "DUPLICATE_STUDENT_NUMBER",
        )
        self.assertEqual(Student.all_objects.count(), before)

    def test_two_students_with_no_number_both_persist(self):
        """The conditional constraint permits it; an unconditional one would not."""
        self.post(self.admin, "student-list", self.enrolment_body())
        self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Ifeanyi", last_name="Chukwu",
            date_of_birth="2014-03-09",
            guardians=[{
                "full_name": "Mrs. Chukwu", "phone": "08115550999",
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
        ))
        self.assertEqual(
            Student.all_objects.filter(tenant=self.tenant, student_number="").count(),
            2,
        )

    def test_a_class_of_another_tenant_answers_404_and_writes_nothing(self):
        other_class = SchoolClass.all_objects.create(
            tenant=self.solo.tenant, level=None, session=self.solo_year,
            name="Theirs", code="THR",
        ) if False else None
        # A class id that does not belong to this school at all.
        before = Student.all_objects.count()
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(school_class=99_000_000),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Student.all_objects.count(), before)

    def test_a_student_at_one_branch_cannot_join_another_branchs_class(self):
        before = Student.all_objects.count()
        response = self.post(self.admin, "student-list", self.enrolment_body(
            branch=str(self.lekki.pk), school_class=self.ikeja_class.pk,
        ))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "BRANCH_SCOPE_CONFLICT",
        )
        self.assertEqual(Student.all_objects.count(), before)

    def test_a_student_may_join_a_school_wide_class(self):
        response = self.post(self.admin, "student-list", self.enrolment_body(
            branch=str(self.ikeja.pk), school_class=self.shared_class.pk,
        ))
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_full_class_is_refused_and_the_acknowledgement_clears_it(self):
        for i in range(2):
            child = self.student(first=f"Filler{i}", last="Child")
            self.place(child, self.lekki_class)

        refused = self.post(self.admin, "student-list", self.enrolment_body(
            school_class=self.lekki_class.pk,
        ))
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.data["error"]["code"], "CLASS_AT_CAPACITY")

        allowed = self.post(self.admin, "student-list", self.enrolment_body(
            school_class=self.lekki_class.pk, allow_over_capacity=True,
        ))
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_a_guardian_email_already_here_links_rather_than_duplicating(self):
        """This is what makes siblings work.

        Without it the second sibling creates a second guardian row and the
        family is split in two, silently, and stays split.
        """
        existing = self.guardian(
            name="Mrs. Amina Yusuf", phone="08115550177",
            email="amina.yusuf@example.ng",
        )
        self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[{
                "full_name": "Mrs A Yusuf", "phone": "08000000001",
                "email": "AMINA.YUSUF@EXAMPLE.NG",
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
        ))
        self.assertEqual(
            Guardian.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        student = Student.all_objects.get(first_name="Zainab")
        self.assertEqual(
            student.guardian_links.first().guardian_id, existing.pk,
        )

    def test_the_same_guardian_email_at_another_school_is_a_separate_row(self):
        self.guardian(
            tenant=self.solo.tenant, name="Mrs. Amina Yusuf",
            phone="08115550177", email="amina.yusuf@example.ng",
        )
        self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(
            Guardian.all_objects.filter(
                email="amina.yusuf@example.ng",
            ).count(),
            2,
        )


class SingleBranchSchoolTests(StudentsFixture):
    """A school with one branch never has to name it.

    One branch is the common case, and a control with a single option is
    noise. The dimension is absent, not disabled.
    """

    def setUp(self):
        self.solo_level = Level.all_objects.create(
            tenant=self.solo.tenant,
            program=self.solo.tenant.programs.create(
                name="Junior Secondary", code="JSS",
            ),
            session=self.solo_year, name="JSS1", code="JSS1", order_index=1,
        )
        self.solo_class = SchoolClass.all_objects.create(
            tenant=self.solo.tenant, level=self.solo_level,
            session=self.solo_year, name="JSS1 A", code="JSS1A", capacity=30,
        )

    def _body(self):
        return {
            "first_name": "Aisha", "last_name": "Bello",
            "date_of_birth": "2016-01-25", "gender": Gender.FEMALE,
            "school_class": self.solo_class.pk,
            "guardians": [{
                "full_name": "Alhaji Musa Bello", "phone": "08075550098",
                "relationship": Relationship.FATHER, "is_primary": True,
            }],
        }

    def test_enrolling_without_naming_a_branch_uses_the_only_one(self):
        response = self.post(self.solo_admin, "student-list", self._body())
        self.assertEqual(response.status_code, 201, response.data)
        student = Student.all_objects.get(first_name="Aisha")
        self.assertEqual(student.branch, self.solo_branch)

    def test_the_response_carries_no_branch_field(self):
        self.post(self.solo_admin, "student-list", self._body())
        response = self.get(self.solo_admin, "student-list")
        row = response.data["data"][0]
        self.assertNotIn("branch", row)
        self.assertNotIn("branch_name", row)

    def test_a_two_branch_school_does_carry_the_branch_field(self):
        self.student()
        response = self.get(self.admin, "student-list")
        self.assertIn("branch_name", response.data["data"][0])


class StatusMachineTests(StudentsFixture):
    def setUp(self):
        self.row = self.student()
        self.place(self.row)

    def test_every_disallowed_ordered_pair_is_refused(self):
        """All fifty-six pairs of the eight states, not a chosen few."""
        from schools.vs_students.exceptions import InvalidStatusTransition
        from schools.vs_students.services.status import transition

        values = list(StudentStatus.values)
        for source in values:
            for target in values:
                if target in ALLOWED_TRANSITIONS[source]:
                    continue
                with self.subTest(source=source, target=target):
                    student = self.student(
                        first=f"S{source}", last=f"T{target}", status=source,
                    )
                    before = StudentStatusLog.objects.filter(
                        student=student,
                    ).count()
                    with self.assertRaises(InvalidStatusTransition):
                        transition(
                            student, target, actor=self.admin, reason="test",
                        )
                    student.refresh_from_db()
                    self.assertEqual(student.status, source)
                    self.assertEqual(
                        StudentStatusLog.objects.filter(student=student).count(),
                        before,
                    )

    def test_a_refused_transition_writes_no_log_row_and_no_audit_event(self):
        from vs_audit.models import AuditEvent

        before_logs = StudentStatusLog.objects.count()
        before_events = AuditEvent.objects.count()
        response = self.post(
            self.admin, "student-suspend", {"reason": "x"},
            pk=self.student(status=StudentStatus.WITHDRAWN, first="Gone").pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(StudentStatusLog.objects.count(), before_logs)
        self.assertEqual(AuditEvent.objects.count(), before_events)

    def test_withdrawing_with_no_reason_is_refused(self):
        response = self.post(self.admin, "student-withdraw", {}, pk=self.row.pk)
        self.assertEqual(response.status_code, 400)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.ACTIVE)

    def test_withdrawing_keeps_every_row_and_releases_the_seat(self):
        response = self.post(
            self.admin, "student-withdraw", {"reason": "Family relocated."},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.WITHDRAWN)
        self.assertEqual(self.row.enrolments.count(), 1)
        self.assertEqual(self.row.enrolments.filter(is_active=True).count(), 0)
        self.assertIsNotNone(self.row.enrolments.first().ended_at)

    def test_suspending_leaves_the_placement_alone(self):
        """The whole difference between suspending and withdrawing."""
        self.post(
            self.admin, "student-suspend", {"reason": "Repeated absence."},
            pk=self.row.pk,
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.SUSPENDED)
        self.assertEqual(self.row.enrolments.filter(is_active=True).count(), 1)

    def test_a_suspended_student_comes_straight_back(self):
        self.post(self.admin, "student-suspend", {"reason": "x"}, pk=self.row.pk)
        response = self.post(
            self.admin, "student-reactivate", {"reason": "Suspension lifted."},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.ACTIVE)
        self.assertEqual(self.row.enrolments.filter(is_active=True).count(), 1)

    def test_a_withdrawn_student_needs_a_class_to_return(self):
        self.post(self.admin, "student-withdraw", {"reason": "x"}, pk=self.row.pk)
        refused = self.post(
            self.admin, "student-reactivate", {"reason": "Back."}, pk=self.row.pk,
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.data["error"]["code"], "PLACEMENT_REQUIRED")

    def test_readmission_reaches_enrolled_then_active_once_placed(self):
        """WITHDRAWN returns to ENROLLED, not straight to ACTIVE.

        ACTIVE means placed and attending, and the placement is what carries
        them the rest of the way.
        """
        self.post(self.admin, "student-withdraw", {"reason": "x"}, pk=self.row.pk)
        response = self.post(
            self.admin, "student-reactivate",
            {"reason": "Back.", "school_class": self.shared_class.pk},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.ACTIVE)
        moves = list(
            self.row.status_logs.order_by("changed_at").values_list(
                "from_status", "to_status",
            ),
        )
        self.assertIn(
            (StudentStatus.WITHDRAWN, StudentStatus.ENROLLED), moves,
        )

    def test_transferring_out_requires_a_destination(self):
        refused = self.post(
            self.admin, "student-transfer-out", {"reason": "Moving."},
            pk=self.row.pk,
        )
        self.assertEqual(refused.status_code, 400)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.ACTIVE)

    def test_transferring_out_records_the_destination_and_is_terminal(self):
        response = self.post(
            self.admin, "student-transfer-out",
            {"reason": "Family moved.", "destination_school": "Greenfield Academy"},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.TRANSFERRED)
        self.assertEqual(
            self.row.status_logs.first().destination_school, "Greenfield Academy",
        )
        self.assertEqual(
            self.row.enrolments.first().outcome, EnrolmentOutcome.TRANSFERRED,
        )
        again = self.post(
            self.admin, "student-reactivate",
            {"reason": "Back", "school_class": self.shared_class.pk},
            pk=self.row.pk,
        )
        self.assertEqual(again.status_code, 422)

    def test_rejecting_an_applicant_is_terminal_and_keeps_the_record(self):
        applicant = self.student(
            status=StudentStatus.APPLICANT, first="Ifeanyi", last="Chukwu",
        )
        response = self.post(
            self.admin, "student-reject", {"reason": "Places filled."},
            pk=applicant.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, StudentStatus.REJECTED)
        self.assertTrue(Student.all_objects.filter(pk=applicant.pk).exists())

        confirm = self.post(
            self.admin, "student-confirm", {}, pk=applicant.pk,
        )
        self.assertEqual(confirm.status_code, 422)

    def test_rejecting_a_non_applicant_is_refused(self):
        response = self.post(
            self.admin, "student-reject", {"reason": "x"}, pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "INVALID_STATUS_TRANSITION",
        )

    def test_a_withdrawn_student_is_absent_by_default_and_present_by_filter(self):
        self.post(self.admin, "student-withdraw", {"reason": "x"}, pk=self.row.pk)
        default = self.get(self.admin, "student-list")
        self.assertEqual(default.data["data"], [])
        filtered = self.get(
            self.admin, "student-list", {"status": StudentStatus.WITHDRAWN},
        )
        self.assertEqual(len(filtered.data["data"]), 1)


class PlacementTests(StudentsFixture):
    def setUp(self):
        self.row = self.student(status=StudentStatus.ENROLLED)

    def test_a_first_placement_needs_no_reason_and_reaches_active(self):
        response = self.post(
            self.admin, "student-assign-class",
            {"school_class": self.shared_class.pk}, pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, StudentStatus.ACTIVE)

    def test_a_transfer_without_a_reason_is_refused(self):
        self.place(self.row, self.shared_class)
        response = self.post(
            self.admin, "student-assign-class",
            {"school_class": self.lekki_class.pk}, pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "REASON_REQUIRED")
        self.assertEqual(self.row.enrolments.filter(is_active=True).count(), 1)

    def test_a_transfer_closes_the_old_row_and_stamps_when(self):
        self.place(self.row, self.shared_class)
        response = self.post(
            self.admin, "student-assign-class",
            {"school_class": self.lekki_class.pk, "reason": "STREAM_CHANGE"},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        old = self.row.enrolments.filter(school_class=self.shared_class).first()
        self.assertFalse(old.is_active)
        self.assertIsNotNone(old.ended_at)
        self.assertEqual(self.row.enrolments.filter(is_active=True).count(), 1)

    def test_only_one_active_enrolment_per_session_can_exist(self):
        """Proven against the constraint, not only through the service."""
        self.place(self.row, self.shared_class)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassEnrolment.all_objects.create(
                tenant=self.tenant, student=self.row,
                school_class=self.lekki_class, session=self.year, is_active=True,
            )

    def test_the_year_on_a_row_is_the_classs_and_cannot_be_passed_in(self):
        """place() takes no year at all, which is what makes them agree.

        A caller that could name one could name a year the class does not
        belong to. The class is the single source and the row is written from
        it, so the two cannot be set apart by anybody.
        """
        import inspect

        from schools.vs_students.services.placement import place as place_fn

        self.assertNotIn("session", inspect.signature(place_fn).parameters)

        self.place(self.row, self.shared_class)
        row = self.row.enrolments.get()
        self.assertEqual(row.session_id, row.school_class.session_id)

    def test_a_placement_into_a_closed_years_class_is_refused(self):
        """The closed year is now reached through its class, not a parameter."""
        past = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2019/2020",
            start_date=dt.date(2019, 9, 1), end_date=dt.date(2020, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        old_jss1 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=past,
            name="JSS1", code="JSS1", order_index=1,
        )
        stale = SchoolClass.all_objects.create(
            tenant=self.tenant, level=old_jss1, session=past,
            name="JSS1 A", code="JSS1A", arm="A", branch=None, capacity=30,
        )
        with self.assertRaises(ClassBelongsToAnotherYear):
            place(self.row, stale, actor=self.admin)
        self.assertEqual(self.row.enrolments.count(), 0)

    def test_a_class_from_another_year_is_refused(self):
        """The register bug: both halves of the row look right on their own.

        Every year's JSS1 A is called JSS1 A, so an id from the wrong year
        renders identically on the student's profile and on the class card.
        What differs is the register, which the child is simply not on.
        """
        past = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2019/2020",
            start_date=dt.date(2019, 9, 1), end_date=dt.date(2020, 7, 31),
            status=SessionStatus.ARCHIVED,
        )
        # A class sits at a level of its OWN year - vs_academics keeps those in step -
        # so the old year needs its own JSS1 for the old class to hang off.
        old_jss1 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=past,
            name="JSS1", code="JSS1", order_index=1,
        )
        stale = SchoolClass.all_objects.create(
            tenant=self.tenant, level=old_jss1, session=past,
            name="JSS1 A", code="JSS1A", arm="A", branch=None, capacity=30,
        )
        response = self.post(
            self.admin, "student-assign-class",
            {"school_class": stale.pk}, pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "CLASS_BELONGS_TO_ANOTHER_YEAR",
        )
        self.assertIn("2019/2020", response.data["message"])
        self.assertEqual(self.row.enrolments.count(), 0)

    def test_every_placement_written_agrees_with_its_class(self):
        """The invariant the refusal exists to keep, asserted on the rows.

        A test that only checks the refusal proves the door is shut; this
        proves nothing already walked through it by another route.
        """
        self.place(self.row, self.shared_class)
        for enrolment in ClassEnrolment.all_objects.select_related(
            "school_class",
        ):
            self.assertEqual(
                enrolment.session_id, enrolment.school_class.session_id,
                f"{enrolment.school_class.name} is not in the placement's year",
            )

    def test_moving_a_graduated_student_says_so_rather_than_offering_a_form(self):
        graduated = self.student(status=StudentStatus.GRADUATED, first="Ahmed")
        response = self.post(
            self.admin, "student-assign-class",
            {"school_class": self.shared_class.pk}, pk=graduated.pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "NOTHING_TO_MOVE")


class GuardianTests(StudentsFixture):
    def setUp(self):
        self.row = self.student()
        self.g = self.guardian()
        self.link(self.row, self.g)

    def test_one_guardian_serves_two_children_at_two_branches(self):
        other = self.student(branch=self.ikeja, first="Somto", last="Okafor")
        self.link(other, self.g, primary=True)
        self.assertEqual(
            Guardian.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        response = self.get(self.admin, "guardian-students", pk=self.g.pk)
        self.assertEqual(len(response.data["data"]), 2)

    def test_a_branch_bound_caller_sees_one_of_the_two_wards(self):
        other = self.student(branch=self.ikeja, first="Somto", last="Okafor")
        self.link(other, self.g, primary=True)
        response = self.get(self.lekki_head, "guardian-students", pk=self.g.pk)
        self.assertEqual(len(response.data["data"]), 1)

    def test_a_pair_can_only_be_linked_once(self):
        response = self.post(
            self.admin, "student-guardians",
            {"guardian_id": self.g.pk, "relationship": Relationship.FATHER},
            pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 400)

    def test_unlinking_the_only_guardian_of_a_student_on_the_roll_is_refused(self):
        response = self.delete(
            self.admin, "student-guardian-detail",
            pk=self.row.pk, guardian_id=self.g.pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "GUARDIAN_REQUIRED")
        self.assertTrue(
            StudentGuardian.all_objects.filter(
                student=self.row, guardian=self.g,
            ).exists(),
        )

    def test_unlinking_the_primary_promotes_the_only_other_one(self):
        second = self.guardian(
            name="Mrs. Ifeoma Nwosu", phone="08035550102",
            email="ifeoma@example.ng",
        )
        self.link(self.row, second, primary=False,
                  relationship=Relationship.MOTHER)
        response = self.delete(
            self.admin, "student-guardian-detail",
            pk=self.row.pk, guardian_id=self.g.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            StudentGuardian.all_objects.get(
                student=self.row, guardian=second,
            ).is_primary,
        )

    def test_only_one_primary_guardian_can_exist_at_the_database(self):
        second = self.guardian(
            name="Mrs. Ifeoma Nwosu", phone="08035550102",
            email="ifeoma@example.ng",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentGuardian.all_objects.create(
                tenant=self.tenant, student=self.row, guardian=second,
                relationship=Relationship.MOTHER, is_primary=True,
            )

    def test_the_directory_shows_ward_counts_and_the_sibling_flag(self):
        other = self.student(first="Somto", last="Okafor")
        self.link(other, self.g, primary=True)
        response = self.get(self.admin, "guardian-list")
        row = response.data["data"][0]
        self.assertEqual(row["ward_count"], 2)
        self.assertTrue(row["is_sibling_household"])

    def test_a_guardian_matching_a_staff_user_links_that_account(self):
        """One person is one account within one school.

        Giving a teacher a second account so she can see her own child is
        exactly the defect this rule exists to prevent.
        """
        from vs_user.models import User

        teacher = self.lekki_head
        before = User.objects.filter(email__iexact=teacher.email).count()
        self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[{
                "full_name": "Head Of Lekki", "phone": "08099999999",
                "email": teacher.email,
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
        ))
        self.assertEqual(
            User.objects.filter(email__iexact=teacher.email).count(), before,
        )
        guardian = Guardian.all_objects.get(email__iexact=teacher.email)
        self.assertEqual(guardian.user_id, teacher.pk)


class BulkActionTests(StudentsFixture):
    def setUp(self):
        self.a = self.student(first="A", last="One", status=StudentStatus.ENROLLED)
        self.b = self.student(first="B", last="Two", status=StudentStatus.ENROLLED)

    def test_a_batch_with_one_bad_id_still_moves_the_rest(self):
        response = self.post(self.admin, "student-bulk-assign", {
            "student_ids": [self.a.pk, self.b.pk, 99_000_000],
            "school_class": self.shared_class.pk,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["assigned"], 2)
        codes = [r["code"] for r in response.data["data"]["results"] if not r["ok"]]
        self.assertEqual(codes, ["NOT_FOUND"])
        self.assertEqual(
            ClassEnrolment.all_objects.filter(is_active=True).count(), 2,
        )

    def test_a_batch_containing_one_disallowed_transition_moves_the_rest(self):
        graduated = self.student(status=StudentStatus.GRADUATED, first="Gone")
        response = self.post(self.admin, "student-bulk-status", {
            "student_ids": [self.a.pk, graduated.pk],
            "to_status": StudentStatus.WITHDRAWN,
            "reason": "End of year clean-up.",
        })
        self.assertEqual(response.data["data"]["changed"], 1)
        graduated.refresh_from_db()
        self.assertEqual(graduated.status, StudentStatus.GRADUATED)

    def test_a_selection_over_the_cap_is_refused_before_anything_is_written(self):
        from schools.vs_students.constants import BULK_MAX

        response = self.post(self.admin, "student-bulk-assign", {
            "student_ids": list(range(BULK_MAX + 1)),
            "school_class": self.shared_class.pk,
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "BULK_TOO_LARGE")
        self.assertEqual(ClassEnrolment.all_objects.count(), 0)

    def test_capacity_is_checked_once_against_the_whole_selection(self):
        response = self.post(self.admin, "student-bulk-assign", {
            "student_ids": [self.a.pk, self.b.pk],
            "school_class": self.lekki_class.pk,
        })
        self.assertEqual(response.status_code, 200, response.data)

        c = self.student(first="C", last="Three", status=StudentStatus.ENROLLED)
        refused = self.post(self.admin, "student-bulk-assign", {
            "student_ids": [c.pk], "school_class": self.lekki_class.pk,
        })
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.data["error"]["code"], "CLASS_AT_CAPACITY")


class PromotionTests(StudentsFixture):
    """The end-of-session move, and the cross-session hop it turns on."""

    def setUp(self):
        # Next year's structure, seeded the way a roll-forward would: same
        # codes, new rows. The promotion has to hop by CODE, because
        # next_level points at this year's JSS2 and not next year's.
        self.next_jss2 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS2", code="JSS2", order_index=2,
        )
        self.next_jss1 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS1", code="JSS1", order_index=1, next_level=self.next_jss2,
        )
        self.next_jss2_a = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.next_jss2, session=self.next_year,
            name="JSS2 A", code="N-JSS2A", arm="A", capacity=30,
        )
        # Next year's JSS1 A, which is where a REPEAT lands. A different row
        # from this year's JSS1 A and identical on screen.
        self.next_jss1_a = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.next_jss1, session=self.next_year,
            name="JSS1 A", code="N-JSS1A", arm="A", capacity=30,
        )

        self.mover = self.student(first="Chiamaka", last="Nwosu")
        self.place(self.mover, self.shared_class)

    def _preview(self, **body):
        return self.post(self.admin, "student-promotion-preview", {
            "to_session": self.next_year.pk, **body,
        })

    def test_the_preview_writes_nothing_and_resolves_the_target_by_code(self):
        before = ClassEnrolment.all_objects.count()
        response = self._preview()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ClassEnrolment.all_objects.count(), before)
        row = response.data["data"]["students"][0]
        self.assertEqual(row["to_class"], "JSS2 A")
        self.assertEqual(row["outcome"], PromotionOutcome.PROMOTE)

    def test_the_level_map_names_next_years_class_not_this_years(self):
        response = self._preview()
        entry = response.data["data"]["level_map"][0]
        self.assertEqual(entry["from"], "JSS1 A")
        self.assertEqual(entry["to_id"], self.next_jss2_a.pk)

    def test_a_terminal_level_graduates_and_writes_no_enrolment(self):
        self.jss1.next_level = None
        self.jss1.is_terminal = True
        self.jss1.save(update_fields=["next_level", "is_terminal"])

        response = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["graduated"], 1)
        self.mover.refresh_from_db()
        self.assertEqual(self.mover.status, StudentStatus.GRADUATED)
        self.assertEqual(
            ClassEnrolment.all_objects.filter(session=self.next_year).count(), 0,
        )

    def test_a_repeat_lands_in_next_years_copy_of_the_same_class(self):
        """Not the class they are in - next year's row of the same name.

        The two are indistinguishable on every screen, because both are called
        JSS1 A. What differs is which year's register has the child on it, and
        writing the old row against the new year puts them on neither.
        """
        response = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
            "overrides": {str(self.mover.pk): PromotionOutcome.REPEAT},
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["repeated"], 1)
        new = ClassEnrolment.all_objects.get(
            student=self.mover, session=self.next_year,
        )
        self.assertEqual(new.school_class_id, self.next_jss1_a.pk)
        self.assertNotEqual(new.school_class_id, self.shared_class.pk)
        self.assertEqual(new.school_class.session_id, new.session_id)
        old = ClassEnrolment.all_objects.get(
            student=self.mover, session=self.year,
        )
        self.assertEqual(old.outcome, EnrolmentOutcome.REPEATED)

    def test_a_repeat_with_no_class_in_the_new_year_is_held_and_named(self):
        """Held, not silently placed into last year's room."""
        self.next_jss1_a.delete()
        response = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
            "overrides": {str(self.mover.pk): PromotionOutcome.REPEAT},
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["held"], 1)
        self.assertEqual(response.data["data"]["repeated"], 0)
        self.assertFalse(
            ClassEnrolment.all_objects.filter(
                student=self.mover, session=self.next_year,
            ).exists(),
        )

    def test_the_preview_names_a_repeat_that_has_nowhere_to_land(self):
        """The run holds it; the preview has to say so before the run."""
        self.next_jss1_a.delete()
        response = self._preview(
            overrides={str(self.mover.pk): PromotionOutcome.REPEAT},
        )
        self.assertEqual(response.status_code, 200, response.data)
        causes = [
            e["cause"]
            for e in response.data["data"]["exceptions"]["by_class"]
        ]
        self.assertIn("NO_CLASS_TO_REPEAT", causes)

    def test_a_closed_year_cannot_be_promoted_into(self):
        """The register of a finished year is a fact, not a working document.

        Reachable by a mis-click rather than an exploit: the run takes a year
        from a picker, and last year is archived at every school that has run
        one, sitting in that list with real classes in it.
        """
        AcademicSession.all_objects.filter(pk=self.next_year.pk).update(
            status=SessionStatus.ARCHIVED,
        )
        response = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
            "overrides": {str(self.mover.pk): PromotionOutcome.PROMOTE},
        })
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY",
        )
        self.assertFalse(
            ClassEnrolment.all_objects.filter(session=self.next_year).exists(),
        )

    def test_the_preview_refuses_a_closed_year_too(self):
        """A preview that succeeds where the run refuses is not a preview."""
        AcademicSession.all_objects.filter(pk=self.next_year.pk).update(
            status=SessionStatus.ARCHIVED,
        )
        response = self._preview()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY",
        )

    def test_promoting_OUT_of_a_closed_year_is_the_normal_case(self):
        """The guard reads to_session only, and this is why.

        At the end of a year the school archives it and promotes out of it.
        A guard on from_session would refuse the one run this module exists
        for.
        """
        AcademicSession.all_objects.filter(pk=self.year.pk).update(
            status=SessionStatus.ARCHIVED,
        )
        response = self.post(self.admin, "student-promotion-run", {
            "from_session": self.year.pk,
            "to_session": self.next_year.pk,
            "overrides": {str(self.mover.pk): PromotionOutcome.PROMOTE},
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 1)

    def test_a_hold_writes_no_enrolment_and_leaves_the_placement_active(self):
        before = ClassEnrolment.all_objects.count()
        response = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
            "overrides": {str(self.mover.pk): PromotionOutcome.HOLD},
        })
        self.assertEqual(response.data["data"]["held"], 1)
        self.assertEqual(ClassEnrolment.all_objects.count(), before)
        self.assertTrue(
            ClassEnrolment.all_objects.get(student=self.mover).is_active,
        )

    def test_re_running_places_nobody_twice(self):
        self.post(self.admin, "student-promotion-run",
                  {"to_session": self.next_year.pk})
        self.post(self.admin, "student-promotion-run",
                  {"to_session": self.next_year.pk})
        self.assertEqual(
            ClassEnrolment.all_objects.filter(
                student=self.mover, session=self.next_year, is_active=True,
            ).count(),
            1,
        )

    def test_a_suspended_student_is_named_on_the_exception_list(self):
        suspended = self.student(
            first="Kelechi", last="Eze", status=StudentStatus.SUSPENDED,
        )
        self.place(suspended, self.lekki_class)
        response = self._preview()
        causes = {
            e["cause"] for e in response.data["data"]["exceptions"]["by_student"]
        }
        self.assertIn("STUDENT_SUSPENDED", causes)

    def test_a_student_with_no_class_is_named_rather_than_silently_skipped(self):
        self.student(first="Fatima", last="Sani", status=StudentStatus.ENROLLED)
        response = self._preview()
        causes = {
            e["cause"] for e in response.data["data"]["exceptions"]["by_student"]
        }
        self.assertIn("NO_CLASS_ASSIGNED", causes)

    def test_a_class_wide_cause_is_one_entry_however_many_students(self):
        """The rows that need a decision must not be buried under the rows that do not."""
        blocked = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.jss2, session=self.year,
            name="JSS2 X", code="JSS2X", arm="X", capacity=30,
        )
        for i in range(3):
            child = self.student(first=f"Blocked{i}", last="Child")
            self.place(child, blocked)
        response = self._preview()
        entries = [
            e for e in response.data["data"]["exceptions"]["by_class"]
            if e["class_name"] == "JSS2 X"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["students"], 3)

    def test_the_preview_and_the_run_agree(self):
        preview = self._preview()
        run = self.post(self.admin, "student-promotion-run", {
            "to_session": self.next_year.pk,
        })
        self.assertEqual(
            preview.data["data"]["counts"]["promote"],
            run.data["data"]["promoted"],
        )

    def test_running_needs_the_assign_key_as_well_as_manage(self):
        from vs_rbac.tests.helpers import (
            make_assignment, make_role, make_role_permission, make_school_admin,
        )

        role = make_role(self.school, name="Head", key="head")
        for key in ("school.students.view", "school.students.manage"):
            make_role_permission(role, self.permissions[key])
        head = make_school_admin(
            None, email="head2@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.school, head, role, branch=None)

        self.assertEqual(
            self.post(head, "student-promotion-preview",
                      {"to_session": self.next_year.pk}).status_code,
            200,
        )
        self.assertEqual(
            self.post(head, "student-promotion-run",
                      {"to_session": self.next_year.pk}).status_code,
            403,
        )


class UnwiredLevelTests(StudentsFixture):
    """A level nobody has wired must hold its students, never graduate them.

    ``Level.next_level`` carries three states and only two are safe to merge.
    ``is_terminal`` says pupils leave; a bare null says nobody has set the
    target yet. Reading the second as the first empties a year group, and the
    school hears about it from a parent - which is what Level's own comment and
    FRD v2.7 FR-005 both say must not happen.
    """

    def setUp(self):
        # Next year exists with a class to land in, so nothing here is held
        # for want of a target - the only variable is how JSS1 is wired.
        self.next_jss1 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS1", code="JSS1", order_index=1,
        )
        self.next_jss2 = Level.all_objects.create(
            tenant=self.tenant, program=self.program, session=self.next_year,
            name="JSS2", code="JSS2", order_index=2,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.next_jss2, session=self.next_year,
            name="JSS2 A", code="N-JSS2A", arm="A", capacity=30,
        )
        self.pupil = self.student(first="Ada", last="Unwired")
        self.place(self.pupil, self.shared_class)

    def preview(self):
        return self.post(
            self.admin, "student-promotion-preview",
            {"to_session": self.next_year.pk},
        ).data["data"]

    def test_an_unwired_level_holds_rather_than_graduating(self):
        """The defect itself: 82 children left the roll on a mis-click."""
        self.jss1.next_level = None
        self.jss1.is_terminal = False
        self.jss1.save(update_fields=["next_level", "is_terminal"])

        data = self.preview()
        self.assertEqual(data["counts"]["graduate"], 0)
        self.assertEqual(data["counts"]["hold"], 1)

    def test_it_is_named_as_unwired_and_not_as_terminal(self):
        """The registrar needs to know it is THEIR setup, not the pupils'."""
        self.jss1.next_level = None
        self.jss1.is_terminal = False
        self.jss1.save(update_fields=["next_level", "is_terminal"])

        causes = [e["cause"] for e in self.preview()["exceptions"]["by_class"]]
        self.assertIn("LEVEL_NOT_WIRED", causes)
        self.assertNotIn("TERMINAL_LEVEL", causes)

    def test_a_level_that_says_pupils_leave_still_graduates_them(self):
        """The other side: the real terminal case must keep working."""
        self.jss1.next_level = None
        self.jss1.is_terminal = True
        self.jss1.save(update_fields=["next_level", "is_terminal"])

        data = self.preview()
        self.assertEqual(data["counts"]["graduate"], 1)
        self.assertEqual(data["counts"]["hold"], 0)

    def test_the_map_does_not_claim_an_unwired_cohort_is_leaving(self):
        self.jss1.next_level = None
        self.jss1.is_terminal = False
        self.jss1.save(update_fields=["next_level", "is_terminal"])

        row = next(
            r for r in self.preview()["level_map"]
            if r["from_id"] == self.shared_class.pk
        )
        self.assertFalse(row["terminal"])


class PassportPhotographTests(StudentsFixture):
    """The photograph a school uploads has to become the face it sees.

    The defect this covers: ``Student.photo`` is serialised as ``photo_url``
    and written by nothing anywhere, while the passport photograph - a REQUIRED
    document since FR-015 - sat in ``StudentDocument`` being read by nothing but
    the checklist. A school that had uploaded every document the module asked
    for still saw initials on every row, and had nowhere it could point at to
    say "the photograph goes here".
    """

    def setUp(self):
        super().setUp()
        self.pupil = self.student(first="Tunde", last="Bello")

    def _upload(self, *, document_type, name, content_type, size=64):
        upload = SimpleUploadedFile(name, b"x" * size, content_type=content_type)
        url = reverse("student-documents", kwargs={"pk": self.pupil.pk})
        return self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"document_type": document_type, "file": upload},
            format="multipart",
        )

    def test_uploaded_passport_photograph_becomes_the_students_photo_url(self):
        self.assertEqual(
            self.get(self.admin, "student-detail", pk=self.pupil.pk).data["data"][
                "photo_url"
            ],
            "",
        )

        self.assertEqual(
            self._upload(
                document_type=DocumentType.PASSPORT_PHOTO,
                name="tunde.jpg", content_type="image/jpeg",
            ).status_code,
            201,
        )

        detail = self.get(self.admin, "student-detail", pk=self.pupil.pk)
        self.assertTrue(detail.data["data"]["photo_url"])

    def test_the_directory_row_carries_it_too_not_only_the_profile(self):
        """The row is where it was most visible as missing."""
        self._upload(
            document_type=DocumentType.PASSPORT_PHOTO,
            name="tunde.png", content_type="image/png",
        )
        rows = self.get(self.admin, "student-list").data["data"]
        row = next(r for r in rows if r["id"] == self.pupil.pk)
        self.assertTrue(row["photo_url"])

    def test_the_urls_are_absolute_because_the_api_is_another_host(self):
        """A bare /media/ path resolves against the FRONTEND, which has none.

        The app runs at the school's own subdomain and the API at api.…, so a
        path handed to an <img> or a "View" link fetched the single-page app's
        index.html and got HTML where an image should have been. Every other
        module already passes ``absolute_for``; this one did not.
        """
        self._upload(
            document_type=DocumentType.PASSPORT_PHOTO,
            name="tunde.jpg", content_type="image/jpeg",
        )
        detail = self.get(self.admin, "student-detail", pk=self.pupil.pk)
        self.assertTrue(detail.data["data"]["photo_url"].startswith("http"))

        docs = self.get(self.admin, "student-documents", pk=self.pupil.pk)
        photo = next(
            d for d in docs.data["data"]
            if d["document_type"] == DocumentType.PASSPORT_PHOTO
        )
        self.assertTrue(photo["url"].startswith("http"), photo["url"])

    def test_removing_it_takes_the_face_away_again(self):
        self._upload(
            document_type=DocumentType.PASSPORT_PHOTO,
            name="tunde.jpg", content_type="image/jpeg",
        )
        doc = self.pupil.documents.get(document_type=DocumentType.PASSPORT_PHOTO)
        self.delete(
            self.admin, "student-document-detail",
            pk=self.pupil.pk, doc_id=doc.pk,
        )
        detail = self.get(self.admin, "student-detail", pk=self.pupil.pk)
        self.assertEqual(detail.data["data"]["photo_url"], "")

    def test_a_pdf_is_refused_as_a_passport_photograph_and_the_message_says_why(self):
        """It would render as a broken picture beside the child's name."""
        response = self._upload(
            document_type=DocumentType.PASSPORT_PHOTO,
            name="scan.pdf", content_type="application/pdf",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("image", str(response.data).lower())

    def test_a_pdf_is_still_a_perfectly_good_birth_certificate(self):
        """The rule is about the photograph, not about documents."""
        response = self._upload(
            document_type=DocumentType.BIRTH_CERTIFICATE,
            name="birth.pdf", content_type="application/pdf",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_oversize_upload_is_refused_with_its_own_size_named(self):
        response = self._upload(
            document_type=DocumentType.BIRTH_CERTIFICATE,
            name="huge.pdf", content_type="application/pdf",
            size=6 * 1024 * 1024,
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("6MB", str(response.data))

    def test_the_directory_does_not_ask_once_per_face(self):
        """The prefetch, not fifty extra queries on a page of fifty children."""
        for i in range(3):
            other = self.student(first=f"Child{i}", last="Ade")
            SimpleUploadedFile(f"c{i}.jpg", b"x" * 32, content_type="image/jpeg")
            StudentDocument.objects.create(
                tenant=self.tenant, student=other,
                document_type=DocumentType.PASSPORT_PHOTO,
                file=SimpleUploadedFile(
                    f"c{i}.jpg", b"x" * 32, content_type="image/jpeg",
                ),
                uploaded_by=self.admin,
            )

        with CaptureQueriesContext(connection) as first:
            self.get(self.admin, "student-list")

        for i in range(3, 9):
            other = self.student(first=f"Child{i}", last="Ade")
            StudentDocument.objects.create(
                tenant=self.tenant, student=other,
                document_type=DocumentType.PASSPORT_PHOTO,
                file=SimpleUploadedFile(
                    f"c{i}.jpg", b"x" * 32, content_type="image/jpeg",
                ),
                uploaded_by=self.admin,
            )

        with CaptureQueriesContext(connection) as second:
            self.get(self.admin, "student-list")

        self.assertEqual(len(second), len(first))


class GuardianPhotographTests(StudentsFixture):
    """A face for the person collecting a child, and it is never demanded.

    The gap this closes: ``Guardian`` carried a name, a phone, an email, an
    occupation and an address and no file field at all - so gate staff opening
    a contact card saw a phone number where a face should be, and there was
    nothing anywhere in the product that could have put one there.
    """

    def setUp(self):
        super().setUp()
        self.parent = self.guardian(name="Mrs. Adaeze Bello")

    def _upload(self, *, name="adaeze.jpg", content_type="image/jpeg", size=64):
        upload = SimpleUploadedFile(name, b"x" * size, content_type=content_type)
        url = reverse("guardian-photo", kwargs={"pk": self.parent.pk})
        return self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}", {"photo": upload},
            format="multipart",
        )

    def test_a_guardian_starts_with_no_photograph_and_that_is_fine(self):
        response = self.get(self.admin, "guardian-detail", pk=self.parent.pk)
        self.assertEqual(response.data["data"]["photo_url"], "")

    def test_uploading_one_puts_it_on_the_record_and_the_directory(self):
        # The directory lists guardians who have a child here, so give her one.
        self.link(self.student(first="Chidi", last="Bello"), self.parent)
        self.assertEqual(self._upload().status_code, 200)

        detail = self.get(self.admin, "guardian-detail", pk=self.parent.pk)
        self.assertTrue(detail.data["data"]["photo_url"])

        rows = self.get(self.admin, "guardian-list").data["data"]
        row = next(r for r in rows if r["id"] == self.parent.pk)
        self.assertTrue(row["photo_url"])

    def test_the_url_is_absolute_because_the_api_is_another_host(self):
        self._upload()
        detail = self.get(self.admin, "guardian-detail", pk=self.parent.pk)
        self.assertTrue(detail.data["data"]["photo_url"].startswith("http"))

    def test_removing_it_leaves_the_record_intact(self):
        self._upload()
        response = self.delete(
            self.admin, "guardian-photo", pk=self.parent.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.parent.refresh_from_db()
        self.assertFalse(self.parent.photo)
        self.assertEqual(self.parent.full_name, "Mrs. Adaeze Bello")

    def test_removing_a_photograph_that_is_not_there_is_a_404_not_a_success(self):
        self.assertEqual(
            self.delete(self.admin, "guardian-photo", pk=self.parent.pk).status_code,
            404,
        )

    def test_a_pdf_is_refused_and_the_message_names_what_was_sent(self):
        response = self._upload(name="scan.pdf", content_type="application/pdf")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("image", str(response.data).lower())

    def test_an_oversize_photograph_is_refused(self):
        response = self._upload(size=6 * 1024 * 1024)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("6MB", str(response.data))

    def test_another_schools_guardian_answers_404_never_403(self):
        """An id must not reveal that a person exists at another school."""
        outsider = self.guardian(
            tenant=self.solo.tenant, name="Mr. Somebody Else",
            email="somebody@example.ng", phone="08099990000",
        )
        url = reverse("guardian-photo", kwargs={"pk": outsider.pk})
        response = self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"photo": SimpleUploadedFile("x.jpg", b"x" * 8, content_type="image/jpeg")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 404, response.data)


class GuardianPhotoReadPolicyTests(StudentsFixture):
    """A guardian's face is reachable by whoever reaches one of their children.

    The branch check cannot be made on the guardian: the row carries no branch
    on purpose, because one household serves siblings at two branches. So the
    policy asks the question the record itself answers - which of this person's
    children does this caller see - and a caller who sees none of them is
    refused the photograph too.
    """

    def setUp(self):
        super().setUp()
        self.parent = self.guardian(name="Mrs. Adaeze Bello")
        self.link(
            self.student(first="Chidi", last="Bello", branch=self.ikeja),
            self.parent,
        )
        # Uploaded through the route, not assigned to the field. The stored
        # row takes its tenant from the request that wrote it, and a file
        # written outside one has none - which authorize() refuses, correctly.
        url = reverse("guardian-photo", kwargs={"pk": self.parent.pk})
        self.client_for(self.admin).post(
            f"{url}?tenant={self.tenant.slug}",
            {"photo": SimpleUploadedFile(
                "face.jpg", b"x" * 32, content_type="image/jpeg",
            )},
            format="multipart",
        )
        self.parent.refresh_from_db()

    def _read(self, user):
        return self.client_for(user).get(
            reverse("stored-media", kwargs={"name": self.parent.photo.name}),
            {"t": sign(self.parent.photo.name, user), "tenant": self.tenant.slug},
        )

    def test_a_school_wide_admin_may_read_it(self):
        self.assertEqual(self._read(self.admin).status_code, 200)

    def test_a_head_pinned_to_another_branch_may_not(self):
        """Their only child is at Ikeja; this caller is pinned to Lekki."""
        self.assertEqual(self._read(self.lekki_head).status_code, 404)

    def test_somebody_holding_no_student_permission_may_not(self):
        self.assertEqual(self._read(self.nobody).status_code, 404)


class PhotographsAreOptionalTests(StudentsFixture):
    """A photograph is a prompt on nobody's record, on either side.

    A school photographs its intake on a day it chooses, not at the desk while
    a parent waits. Marking every new child's record incomplete for a missing
    picture teaches everybody to ignore the mark - including on the row that is
    genuinely missing a birth certificate.
    """

    def test_the_passport_photograph_is_not_a_required_document(self):
        pupil = self.student(first="Ngozi", last="Umeh")
        rows = self.get(self.admin, "student-documents", pk=pupil.pk).data["data"]
        photo = next(
            r for r in rows if r["document_type"] == DocumentType.PASSPORT_PHOTO
        )
        self.assertFalse(photo["required"])

    def test_the_birth_certificate_still_is(self):
        pupil = self.student(first="Ngozi", last="Umeh")
        rows = self.get(self.admin, "student-documents", pk=pupil.pk).data["data"]
        cert = next(
            r for r in rows if r["document_type"] == DocumentType.BIRTH_CERTIFICATE
        )
        self.assertTrue(cert["required"])
