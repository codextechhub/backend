"""Teaching duties, the lead, and the two kinds of gap.

FRD M12 v2.1, FR-011 and FR-017.
"""
from __future__ import annotations

from schools.vs_academics.models import SchoolClass
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import TeachingAssignment
from vs_audit.models import AuditActionType, AuditEvent

from .base import StaffFixture


class AssignmentTests(StaffFixture):
    def body(self, **overrides):
        payload = {
            "school_class": self.shared_class.pk,
            "subject": self.maths.pk,
            "part": "LEAD",
        }
        payload.update(overrides)
        return payload

    def test_assigning_records_a_lead(self):
        response = self.post(
            self.admin, "staff-teaching", self.body(), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["part"], TeachingPart.LEAD)

    def test_a_second_lead_on_one_pairing_is_refused_with_the_first_named(self):
        """Displacing a colleague is deliberate or it does not happen."""
        self.post(self.admin, "staff-teaching", self.body(), pk=self.eze.pk)
        response = self.post(
            self.admin, "staff-teaching", self.body(), pk=self.registrar.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "LEAD_ALREADY_SET")
        self.assertIn("Chukwuemeka Eze", response.data["message"])

    def test_a_second_assistant_on_one_pairing_is_fine(self):
        """A split class genuinely has several teachers."""
        self.post(self.admin, "staff-teaching", self.body(), pk=self.eze.pk)
        first = self.post(
            self.admin, "staff-teaching", self.body(part="ASSISTANT"),
            pk=self.registrar.pk,
        )
        second = self.post(
            self.admin, "staff-teaching", self.body(part="ASSISTANT"),
            pk=self.ikeja_teacher.pk,
        )
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)

    def test_the_same_teacher_twice_on_one_pairing_is_one_row(self):
        self.post(self.admin, "staff-teaching", self.body(), pk=self.eze.pk)
        self.post(self.admin, "staff-teaching", self.body(), pk=self.eze.pk)
        self.assertEqual(
            TeachingAssignment.all_objects.filter(
                staff=self.eze, school_class=self.shared_class,
                subject=self.maths,
            ).count(),
            1,
        )

    def test_stepping_back_frees_the_lead_for_somebody_else(self):
        created = self.post(
            self.admin, "staff-teaching", self.body(), pk=self.eze.pk,
        )
        assignment_id = created.data["data"]["id"]
        stepped = self.patch(
            self.admin, "staff-teaching-detail", {"part": "ASSISTANT"},
            pk=assignment_id,
        )
        self.assertEqual(stepped.status_code, 200, stepped.data)
        promoted = self.post(
            self.admin, "staff-teaching", self.body(), pk=self.registrar.pk,
        )
        self.assertEqual(promoted.status_code, 201, promoted.data)

    def test_promoting_where_a_lead_exists_is_refused(self):
        self.post(self.admin, "staff-teaching", self.body(), pk=self.eze.pk)
        assistant = self.post(
            self.admin, "staff-teaching", self.body(part="ASSISTANT"),
            pk=self.registrar.pk,
        )
        response = self.patch(
            self.admin, "staff-teaching-detail", {"part": "LEAD"},
            pk=assistant.data["data"]["id"],
        )
        self.assertEqual(response.status_code, 422, response.data)

    def test_an_archived_year_refuses_a_new_assignment(self):
        archived_class = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.jss1, session=self.archived_year,
            name="JSS1 OLD", code="JSS1OLD", arm="A", branch=None,
        )
        response = self.post(
            self.admin, "staff-teaching",
            self.body(
                school_class=archived_class.pk, session=self.archived_year.pk,
            ),
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "SESSION_ARCHIVED")

    def test_an_archived_year_still_returns_its_assignments(self):
        """Who taught what last year is the record a school will be asked for."""
        archived_class = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.jss1, session=self.archived_year,
            name="JSS1 OLD", code="JSS1OLD", arm="A", branch=None,
        )
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=archived_class,
            subject=self.maths, session=self.archived_year,
        )
        response = self.get(
            self.admin, "staff-teaching",
            {"session": self.archived_year.pk}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]["assignments"]), 1)

    def test_a_branch_admin_cannot_staff_another_branchs_class(self):
        response = self.post(
            self.lekki_head, "staff-teaching",
            self.body(school_class=self.ikeja_class.pk), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_assigning_and_removing_both_write_audit_events(self):
        created = self.post(
            self.admin, "staff-teaching", self.body(), pk=self.eze.pk,
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action_type=AuditActionType.STAFF_TEACHING_ASSIGNED,
            ).exists(),
        )
        self.delete(
            self.admin, "staff-teaching-detail", pk=created.data["data"]["id"],
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action_type=AuditActionType.STAFF_TEACHING_UNASSIGNED,
            ).exists(),
        )


class ClassTeacherTests(StaffFixture):
    def test_designating_writes_it_to_the_class(self):
        response = self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": self.eze.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.shared_class.refresh_from_db()
        self.assertEqual(self.shared_class.class_teacher_id, self.eze.pk)

    def test_replacing_audits_both_the_old_and_the_new(self):
        self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": self.eze.pk},
        )
        self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": self.registrar.pk},
        )
        event = AuditEvent.objects.filter(
            entity_type="SchoolClass", entity_id=str(self.shared_class.pk),
        ).order_by("-event_at", "-id").first()
        self.assertEqual(event.metadata["from_staff_id"], self.eze.pk)
        self.assertEqual(event.metadata["to_staff_id"], self.registrar.pk)

    def test_it_can_be_cleared(self):
        """A class whose teacher has left and whom nobody has replaced."""
        self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": self.eze.pk},
        )
        response = self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": None},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.shared_class.refresh_from_db()
        self.assertIsNone(self.shared_class.class_teacher_id)

    def test_it_points_at_the_staff_record_and_not_the_account(self):
        """A class teacher who has left still holds the class otherwise.

        Pointing at the employment record is what lets a school ask whether the
        person owning a class still works there; pointing at the login only
        answers whether somebody remembered to deactivate it.
        """
        field = SchoolClass._meta.get_field("class_teacher")
        self.assertEqual(field.related_model.__name__, "StaffProfile")

    def test_the_designation_can_be_read_back(self):
        """Set by this endpoint and read from the class list.

        It was written by one endpoint and exposed by nothing, so the screen
        that sets a class teacher could not show what it had set. An id and a
        display name, and never an email: this rides on a list a whole school
        reads.
        """
        self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.shared_class.pk, "staff": self.eze.pk},
        )
        response = self.client_for(self.admin).get(
            "/v1/academics/classes/",
            {"tenant": self.tenant.slug, "session": self.year.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item for item in response.data["data"]
            if item["id"] == self.shared_class.pk
        )
        self.assertEqual(row["class_teacher"]["staff_id"], self.eze.pk)
        self.assertEqual(row["class_teacher"]["name"], "Chukwuemeka Eze")
        self.assertNotIn("email", row["class_teacher"])

    def test_a_class_with_nobody_reads_null_rather_than_a_blank_person(self):
        """The common case, and it must not be an object with empty strings.

        A screen testing truthiness on the object would draw a nameless chip on
        every class in the school.
        """
        response = self.client_for(self.admin).get(
            "/v1/academics/classes/",
            {"tenant": self.tenant.slug, "session": self.year.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item for item in response.data["data"]
            if item["id"] == self.shared_class.pk
        )
        self.assertIsNone(row["class_teacher"])


class CoverageTests(StaffFixture):
    def test_an_uncovered_pairing_is_a_coverage_gap(self):
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertEqual(response.status_code, 200, response.data)
        # Three classes x two offered subjects, none staffed.
        self.assertEqual(response.data["coverage_gaps"], 6)
        self.assertEqual(response.data["lead_gaps"], 0)

    def test_a_pairing_with_assistants_and_no_lead_is_a_lead_gap(self):
        """Being taught and unowned is a different problem from nobody teaching it."""
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.shared_class,
            subject=self.maths, session=self.year, part=TeachingPart.ASSISTANT,
        )
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertEqual(response.data["coverage_gaps"], 5)
        self.assertEqual(response.data["lead_gaps"], 1)

    def test_a_lead_closes_both_kinds_of_gap_for_its_pairing(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.shared_class,
            subject=self.maths, session=self.year, part=TeachingPart.LEAD,
        )
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertEqual(response.data["coverage_gaps"], 5)
        self.assertEqual(response.data["lead_gaps"], 0)

    def test_the_headline_names_both_counts(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.shared_class,
            subject=self.maths, session=self.year, part=TeachingPart.ASSISTANT,
        )
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertIn("subjects have no teacher at all", response.data["headline"])
        self.assertIn("taught with no main teacher", response.data["headline"])

    def test_removing_an_assignment_makes_the_pairing_a_gap_again(self):
        row = TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.shared_class,
            subject=self.maths, session=self.year, part=TeachingPart.LEAD,
        )
        before = self.get(self.admin, "staff-teaching-coverage")
        self.delete(self.admin, "staff-teaching-detail", pk=row.pk)
        after = self.get(self.admin, "staff-teaching-coverage")
        self.assertEqual(
            after.data["coverage_gaps"], before.data["coverage_gaps"] + 1,
        )

    def test_the_universe_is_offerings_and_not_every_class_crossed_with_every_subject(self):
        """A Primary 4 class is not missing a Physics teacher.

        Crossing everything with everything would invent gaps for subjects the
        school does not teach at that level, which is a warning nobody can act
        on and one that would swamp the real ones.
        """
        from schools.vs_academics.models import Subject

        Subject.all_objects.create(
            tenant=self.tenant, name="Further Maths", code="FMTH",
        )
        response = self.get(self.admin, "staff-teaching-coverage")
        # Still two subjects offered at JSS1, not three.
        self.assertEqual(response.data["coverage_gaps"], 6)

    def test_a_branch_admin_sees_only_their_own_and_the_shared_classes(self):
        response = self.get(self.lekki_head, "staff-teaching-coverage")
        classes = {row["class_name"] for row in response.data["data"]}
        self.assertIn("JSS1 A", classes, "a shared class belongs to every branch")
        self.assertIn("JSS1 B", classes)
        self.assertNotIn("JSS1 C", classes)

    def test_an_archived_year_has_no_coverage_question(self):
        response = self.get(
            self.admin, "staff-teaching-coverage",
            {"session": self.archived_year.pk},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "SESSION_ARCHIVED")

    def test_another_schools_year_is_404(self):
        from schools.vs_academics.models import AcademicSession
        import datetime as dt

        theirs = AcademicSession.all_objects.create(
            tenant=self.solo.tenant, name="2025/2026",
            start_date=dt.date(2025, 9, 1), end_date=dt.date(2026, 7, 31),
            status="DRAFT",
        )
        response = self.get(
            self.admin, "staff-teaching-coverage", {"session": theirs.pk},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_coverage_response_is_paginated(self):
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertIn("pagination", response.data)
