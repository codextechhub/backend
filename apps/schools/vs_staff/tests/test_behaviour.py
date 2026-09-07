"""What the module actually does: the lifecycle, the two statuses, posting, teaching.

FRD M12 v2.1 sections 12.2, 12.3 and 12.4.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from schools.vs_academics.models import SchoolClass
from schools.vs_staff.constants import EmploymentStatus, TeachingPart
from schools.vs_staff.models import StaffEmploymentEvent, StaffProfile, TeachingAssignment
from schools.vs_staff.services import employment
from vs_audit.models import AuditActionType, AuditEvent
from vs_user.models import User

from .base import StaffFixture


class CreationTests(StaffFixture):
    def test_inviting_writes_an_account_a_profile_and_one_event(self):
        response = self.post(self.admin, "staff-list", self.invite_body())
        self.assertEqual(response.status_code, 201, response.data)

        user = User.objects.get(tenant=self.tenant, email="funke@brightfield.test")
        self.assertEqual(user.status, User.Status.PENDING)
        profile = StaffProfile.all_objects.get(user=user)
        self.assertEqual(profile.employment_status, EmploymentStatus.INVITED)
        self.assertEqual(profile.employment_events.count(), 1)
        self.assertEqual(profile.employment_events.first().from_status, "")

    def test_a_refused_qualification_rolls_the_whole_person_back(self):
        """Six steps and one save, so a person is created whole or not at all."""
        body = self.invite_body(
            qualifications=[{"qualification": "B.Ed", "year_obtained": 3000}],
        )
        response = self.post(self.admin, "staff-list", body)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            User.objects.filter(
                tenant=self.tenant, email="funke@brightfield.test",
            ).exists(),
        )

    def test_a_duplicate_staff_number_is_reported_on_its_own_field(self):
        response = self.post(
            self.admin, "staff-list",
            self.invite_body(staff_number="BFS/STF/0012"),
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("staff_number", str(response.data))

    def test_creating_writes_an_audit_event(self):
        """Asserted to EXIST, never by the absence of an exception.

        ``emit_audit_event`` never raises, so a test that only checked the
        request succeeded would pass with an empty trail.
        """
        self.post(self.admin, "staff-list", self.invite_body())
        profile = StaffProfile.all_objects.get(user__email="funke@brightfield.test")
        self.assertTrue(
            AuditEvent.objects.filter(
                entity_type="StaffProfile", entity_id=str(profile.pk),
                action_type=AuditActionType.CREATE,
            ).exists(),
        )

    def test_qualifications_and_documents_ride_along_with_the_form(self):
        body = self.invite_body(
            qualifications=[
                {"qualification": "B.Sc. Mathematics", "institution": "UNN",
                 "year_obtained": 2010},
                {"qualification": "PGDE", "year_obtained": 2013},
            ],
        )
        response = self.post(self.admin, "staff-list", body)
        self.assertEqual(response.status_code, 201, response.data)
        profile = StaffProfile.all_objects.get(user__email="funke@brightfield.test")
        self.assertEqual(profile.qualifications.count(), 2)


class TwoStatusesTests(StaffFixture):
    """Employment and account are two facts, and each direction is asserted."""

    def test_suspending_an_account_does_not_change_employment(self):
        self.post(self.admin, "staff-account-suspend", pk=self.eze.pk)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.employment_status, EmploymentStatus.ACTIVE)
        self.assertEqual(self.eze.user.status, User.Status.SUSPENDED)

    def test_suspending_employment_does_suspend_the_account(self):
        """The other direction. Either one alone reads as correct."""
        response = self.post(
            self.admin, "staff-status",
            {"to_status": "SUSPENDED", "reason": "Internal review"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.eze.refresh_from_db()
        self.eze.user.refresh_from_db()
        self.assertEqual(self.eze.employment_status, EmploymentStatus.SUSPENDED)
        self.assertEqual(self.eze.user.status, User.Status.SUSPENDED)

    def test_a_locked_account_has_an_unchanged_employment_status(self):
        """Mrs. Okafor mistypes her password three times and is still employed."""
        self.eze.user.status = User.Status.LOCKED
        self.eze.user.save(update_fields=["status"])
        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)
        self.assertEqual(
            response.data["data"]["employment_status"], EmploymentStatus.ACTIVE,
        )
        self.assertEqual(response.data["data"]["account_flag"]["code"], "LOCKED")

    def test_locked_is_not_an_employment_transition(self):
        response = self.post(
            self.admin, "staff-status", {"to_status": "LOCKED"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_administrator_cannot_activate_somebody_else(self):
        """The design's "Mark accepted", refused.

        Mrs. Okonkwo pressing it would leave Mr. Adeyemo reading Active on every
        screen while his account stayed PENDING and he still could not sign in.
        """
        invited = self.make_staff(
            "adeyemo@brightfield.test", "Samuel", "Adeyemo",
            branch=self.ikeja, status=EmploymentStatus.INVITED,
        )
        response = self.post(
            self.admin, "staff-status", {"to_status": "ACTIVE"}, pk=invited.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "INVALID_STATUS_TRANSITION",
        )

    def test_accepting_an_invitation_promotes_the_employment_status(self):
        """The only place an account event drives employment."""
        invited = self.make_staff(
            "kola@brightfield.test", "Kola", "Ayanwale", branch=self.lekki,
            status=EmploymentStatus.INVITED,
        )
        employment.promote_on_activation(invited.user)
        invited.refresh_from_db()
        self.assertEqual(invited.employment_status, EmploymentStatus.ACTIVE)
        self.assertEqual(
            invited.employment_events.filter(
                from_status=EmploymentStatus.INVITED,
                to_status=EmploymentStatus.ACTIVE,
            ).count(),
            1,
        )

    def test_activating_an_account_with_no_profile_writes_nothing(self):
        """A school's first administrator predates this module and is that case."""
        before = StaffEmploymentEvent.all_objects.count()
        employment.promote_on_activation(self.admin)
        self.assertEqual(StaffEmploymentEvent.all_objects.count(), before)

    def test_on_leave_is_not_a_move_anybody_can_make(self):
        """Nobody is put on leave; leave puts them there.

        Offering the move would be a way in that nothing takes back out.
        Approval is an event and code hangs off it, but a leave ENDING is not
        one and there is no scheduler here to notice, so somebody set On Leave
        on 17 August would still read On Leave the following March.
        """
        response = self.post(
            self.admin, "staff-status", {"to_status": "ON_LEAVE"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "INVALID_STATUS_TRANSITION",
        )

        options = self.get(self.admin, "staff-status", pk=self.eze.pk).data["data"]
        self.assertNotIn(
            EmploymentStatus.ON_LEAVE,
            [row["value"] for row in options["options"]],
            "the drawer offers a move the service refuses",
        )

    def test_running_leave_is_what_reads_as_on_leave(self):
        """The derived status, in both directions, with no transition involved.

        Mr. Eze is employed throughout. The only thing that changes is whether
        an approved absence covers today, and the row follows it - which is the
        half a stored column could never do, because a leave ending fires
        nothing.
        """
        from schools.vs_staff.models import LeaveRequest

        row = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(row["display_employment_status"], EmploymentStatus.ACTIVE)

        today = dt.date.today()
        leave = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="STUDY",
            start_date=today - dt.timedelta(days=2),
            end_date=today + dt.timedelta(days=2), days=5, status="APPROVED",
        )
        row = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(
            row["display_employment_status"], EmploymentStatus.ON_LEAVE,
        )
        # And when he is back, because that is the next thing anybody asks.
        self.assertEqual(str(row["on_leave_until"]), str(leave.end_date))
        # The stored column never moved, which is the point: the history says
        # nothing happened to his employment, because nothing did.
        self.assertEqual(row["employment_status"], EmploymentStatus.ACTIVE)

        # And back, on the day it ends, with nobody doing anything.
        leave.end_date = today - dt.timedelta(days=1)
        leave.save(update_fields=["end_date"])
        row = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(row["display_employment_status"], EmploymentStatus.ACTIVE)
        # The date goes with the flag. An end date left behind would let a
        # screen say somebody was away until a day that had already passed.
        self.assertIsNone(row["on_leave_until"])

    def test_two_overlapping_absences_report_the_later_return(self):
        """Filing an overlap warns rather than refusing, so the case exists.

        The earlier date would say somebody was back while the second absence
        was still running, which is the one answer that is definitely wrong.
        """
        from schools.vs_staff.models import LeaveRequest

        today = dt.date.today()
        for days in (3, 9):
            LeaveRequest.all_objects.create(
                tenant=self.tenant, staff=self.eze, leave_type="SICK",
                start_date=today - dt.timedelta(days=1),
                end_date=today + dt.timedelta(days=days),
                days=days + 2, status="APPROVED",
            )
        row = self.get(self.admin, "staff-detail", pk=self.eze.pk).data["data"]
        self.assertEqual(
            str(row["on_leave_until"]), str(today + dt.timedelta(days=9)),
        )

    def test_the_on_leave_facet_and_its_count_agree_with_the_rows(self):
        """One expression behind the chip, the filter and the header figure.

        They were three separate readings once and the bar disagreed with the
        list it filtered to. Asserted together so a change to any one of them
        has to move the other two.
        """
        from schools.vs_staff.models import LeaveRequest

        today = dt.date.today()
        LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="STUDY",
            start_date=today, end_date=today + dt.timedelta(days=3),
            days=4, status="APPROVED",
        )

        page = self.get(self.admin, "staff-list").data
        counts = {
            row["value"]: row["count"] for row in page["counts"]["by_employment_status"]
        }
        self.assertEqual(counts.get(EmploymentStatus.ON_LEAVE), 1)

        away = self.get(
            self.admin, "staff-list", {"employment_status": "ON_LEAVE"},
        ).data
        self.assertEqual([r["id"] for r in away["data"]], [self.eze.pk])

        # And he is NOT among the Active ones, or the facets would overlap and
        # the bar would add up to more people than the school has.
        active = self.get(
            self.admin, "staff-list", {"employment_status": "ACTIVE"},
        ).data
        self.assertNotIn(self.eze.pk, [r["id"] for r in active["data"]])

    def test_a_terminal_status_is_terminal(self):
        self.post(
            self.admin, "staff-status",
            {
                "to_status": "RESIGNED", "reason": "Moving abroad",
                "last_working_day": "2025-12-18",
            },
            pk=self.eze.pk,
        )
        response = self.post(
            self.admin, "staff-status", {"to_status": "ACTIVE"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Resigned", response.data["message"])

    def test_resigning_leaves_the_account_open(self):
        """The last working day is set, and nothing closes the login.

        There is no periodic task registry, so a status that changed itself
        would be a promise nothing keeps. FRD section 3.4.
        """
        self.post(
            self.admin, "staff-status",
            {
                "to_status": "RESIGNED", "reason": "Moving abroad",
                "last_working_day": "2025-12-18",
            },
            pk=self.eze.pk,
        )
        self.eze.refresh_from_db()
        self.eze.user.refresh_from_db()
        self.assertEqual(self.eze.exit_date, dt.date(2025, 12, 18))
        self.assertEqual(self.eze.user.status, User.Status.ACTIVE)

    def test_terminating_deactivates_the_account(self):
        self.post(
            self.admin, "staff-status",
            {
                "to_status": "TERMINATED", "reason": "Gross misconduct",
                "last_working_day": "2025-08-29",
            },
            pk=self.eze.pk,
        )
        self.eze.user.refresh_from_db()
        self.assertEqual(self.eze.user.status, User.Status.DEACTIVATED)

    def test_a_reason_is_required_for_a_suspension(self):
        response = self.post(
            self.admin, "staff-status", {"to_status": "SUSPENDED"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "REASON_REQUIRED")

    def test_a_last_working_day_is_required_for_a_resignation(self):
        response = self.post(
            self.admin, "staff-status",
            {"to_status": "RESIGNED", "reason": "Moving abroad"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "LAST_WORKING_DAY_REQUIRED",
        )

    def test_the_cover_list_names_the_classes_rather_than_counting_them(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.shared_class,
            subject=self.maths, session=self.year, part=TeachingPart.LEAD,
        )
        response = self.post(
            self.admin, "staff-status",
            {
                "to_status": "TERMINATED", "reason": "Gross misconduct",
                "last_working_day": "2025-08-29",
            },
            pk=self.eze.pk,
        )
        self.assertEqual(
            response.data["data"]["assignments_needing_cover"],
            ["JSS1 A Mathematics"],
        )

    def test_a_transition_writes_an_audit_event(self):
        self.post(
            self.admin, "staff-status",
            {"to_status": "SUSPENDED", "reason": "Pending an internal review."},
            pk=self.eze.pk,
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                entity_type="StaffProfile", entity_id=str(self.eze.pk),
                action_type=AuditActionType.STAFF_EMPLOYMENT_STATUS_CHANGED,
            ).exists(),
        )

    def test_the_employment_log_refuses_an_edit(self):
        """Append-only means the row cannot be changed after it is written."""
        from django.core.exceptions import ValidationError

        event = self.eze.employment_events.create(
            tenant=self.tenant, to_status=EmploymentStatus.ACTIVE,
            effective_date=dt.date(2025, 1, 1),
        )
        event.reason = "rewritten"
        with self.assertRaises(ValidationError):
            event.save()

    def test_the_employment_log_refuses_a_delete(self):
        from django.core.exceptions import ValidationError

        event = self.eze.employment_events.create(
            tenant=self.tenant, to_status=EmploymentStatus.ACTIVE,
            effective_date=dt.date(2025, 1, 1),
        )
        with self.assertRaises(ValidationError):
            event.delete()


class InvitationRevokeTests(StaffFixture):
    def test_revoking_keeps_the_record_and_closes_the_account(self):
        invited = self.make_staff(
            "wrong@brightfield.test", "Wrong", "Person", branch=self.lekki,
            status=EmploymentStatus.INVITED,
        )
        invited.user.status = User.Status.PENDING
        invited.user.save(update_fields=["status"])

        response = self.post(
            self.admin, "staff-invitation-revoke",
            {"reason": "Wrong email address"}, pk=invited.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        invited.refresh_from_db()
        invited.user.refresh_from_db()
        self.assertEqual(invited.employment_status, EmploymentStatus.TERMINATED)
        self.assertEqual(invited.user.status, User.Status.DEACTIVATED)
        self.assertTrue(
            StaffProfile.all_objects.filter(pk=invited.pk).exists(),
            "the record survives, or the same mistake gets made twice",
        )

    def test_revoking_an_accepted_invitation_is_refused(self):
        response = self.post(
            self.admin, "staff-invitation-revoke",
            {"reason": "Changed our mind"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "INVITATION_ALREADY_ACCEPTED",
        )

    def test_revoking_needs_a_reason(self):
        invited = self.make_staff(
            "wrong2@brightfield.test", "Wrong", "Two", branch=self.lekki,
            status=EmploymentStatus.INVITED,
        )
        invited.user.status = User.Status.PENDING
        invited.user.save(update_fields=["status"])
        response = self.post(
            self.admin, "staff-invitation-revoke", {}, pk=invited.pk,
        )
        self.assertEqual(response.status_code, 400, response.data)


class PostingTests(StaffFixture):
    def test_moving_a_posting_leaves_every_grant_untouched(self):
        from vs_rbac.models import TenantUserRoleAssignment

        before = list(
            TenantUserRoleAssignment.objects.filter(user=self.eze.user)
            .values_list("pk", "role_id", "branch_id", "assignment_status")
            .order_by("pk"),
        )
        response = self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.eze.pk], "branch": self.ikeja.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["role_grants_touched"])
        after = list(
            TenantUserRoleAssignment.objects.filter(user=self.eze.user)
            .values_list("pk", "role_id", "branch_id", "assignment_status")
            .order_by("pk"),
        )
        self.assertEqual(before, after)

    def test_the_posting_is_written_to_the_account_as_well(self):
        self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.eze.pk], "branch": self.ikeja.pk},
        )
        self.eze.refresh_from_db()
        self.eze.user.refresh_from_db()
        self.assertEqual(self.eze.branch_id, self.ikeja.pk)
        self.assertEqual(self.eze.user.branch_id, self.ikeja.pk)

    def test_moving_somebody_with_assignments_warns_and_names_the_classes(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.eze, school_class=self.lekki_class,
            subject=self.maths, session=self.year, part=TeachingPart.LEAD,
        )
        response = self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.eze.pk], "branch": self.ikeja.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        warnings = response.data["data"]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("JSS1 B Mathematics", warnings[0]["classes"])

    def test_a_bulk_move_of_three_writes_three_audit_events(self):
        third = self.make_staff(
            "third@brightfield.test", "Third", "Person", branch=self.lekki,
        )
        self.post(
            self.admin, "staff-bulk-posting",
            {
                "staff_ids": [self.eze.pk, self.registrar.pk, third.pk],
                "branch": self.ikeja.pk,
            },
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action_type=AuditActionType.STAFF_POSTING_CHANGED,
            ).count(),
            3,
            "one event per person, or a bulk action nobody can audit per person",
        )

    def test_a_branch_that_is_not_in_service_is_refused(self):
        from vs_tenants.models import BranchStatus

        self.ikeja.status = BranchStatus.CLOSED
        self.ikeja.save(update_fields=["status"])
        response = self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.eze.pk], "branch": self.ikeja.pk},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "BRANCH_NOT_IN_SERVICE",
        )

    def test_the_roster_returns_three_labelled_groups(self):
        response = self.get(
            self.admin, "staff-roster", {"branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        groups = {group["key"]: group for group in response.data["data"]["groups"]}
        self.assertEqual(
            set(groups), {"posted_here", "reaching_here", "school_wide"},
        )
        self.assertTrue(groups["posted_here"]["movable"])
        self.assertFalse(groups["school_wide"]["movable"])

    def test_the_registrar_appears_as_school_wide_and_not_as_posted_here(self):
        """She is not at Lekki. She is at the school."""
        response = self.get(
            self.admin, "staff-roster", {"branch": self.lekki.pk},
        )
        groups = {group["key"]: group for group in response.data["data"]["groups"]}
        posted = {row["full_name"] for row in groups["posted_here"]["rows"]}
        shared = {row["full_name"] for row in groups["school_wide"]["rows"]}
        self.assertNotIn("Adaeze Nwankwo", posted)
        self.assertIn("Adaeze Nwankwo", shared)


class SingleBranchTests(StaffFixture):
    """A single-branch test proves nothing about a multi-branch one, and vice versa."""

    def test_the_posting_field_is_absent_at_a_one_branch_school(self):
        response = self.get(self.solo_admin, "staff-detail", pk=self.solo_staff.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["data"]["branch_name"])
        self.assertIsNone(response.data["data"]["posted_school_wide"])

    def test_the_posting_field_is_present_at_a_two_branch_school(self):
        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)
        self.assertEqual(response.data["data"]["branch_name"], "Lekki")

    def test_the_roster_is_not_offered_at_a_one_branch_school(self):
        response = self.get(
            self.solo_admin, "staff-roster", {"branch": self.solo_branch.pk},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_side_breakdown_is_by_role_at_a_one_branch_school(self):
        response = self.get(self.solo_admin, "staff-list")
        self.assertEqual(response.data["counts"]["breakdown_by"], "role")

    def test_the_side_breakdown_is_by_branch_at_a_two_branch_school(self):
        response = self.get(self.admin, "staff-list")
        self.assertEqual(response.data["counts"]["breakdown_by"], "branch")


class DirectoryTests(StaffFixture):
    def test_the_counts_carry_the_six_figures_the_header_draws(self):
        response = self.get(self.admin, "staff-list")
        counts = response.data["counts"]
        for key in (
            "total", "currently_employed", "by_employment_status",
            "with_teaching_duties", "locked_accounts", "breakdown",
        ):
            self.assertIn(key, counts)

    def test_currently_employed_differs_from_total_once_somebody_resigns(self):
        self.post(
            self.admin, "staff-status",
            {
                "to_status": "RESIGNED", "reason": "Moving abroad",
                "last_working_day": "2025-12-18",
            },
            pk=self.eze.pk,
        )
        counts = self.get(self.admin, "staff-list").data["counts"]
        self.assertEqual(counts["currently_employed"], counts["total"] - 1)

    def test_the_header_counts_people_and_not_groups(self):
        """Absolute figures, checked against the row count rather than each other.

        The header arrives from a queryset built for display: ordered by
        ``created_at`` and ``id``, and annotated with a teaching load. Django
        folds an ordering into the GROUP BY of a ``.values().annotate()``, so
        every row became its own group and a status held by three people
        reported one.

        It survived because the tests around it compared two figures from the
        same broken source. ``currently_employed == total - 1`` holds perfectly
        when both are wrong by the same amount, so this asserts each number
        against the number of staff rows that exist.
        """
        rows = StaffProfile.all_objects.filter(tenant=self.tenant).count()
        counts = self.get(self.admin, "staff-list").data["counts"]

        self.assertEqual(counts["total"], rows)
        self.assertEqual(
            sum(row["count"] for row in counts["by_employment_status"]), rows,
        )
        self.assertEqual(
            sum(row["count"] for row in counts["breakdown"]), rows,
        )
        active = next(
            row["count"] for row in counts["by_employment_status"]
            if row["value"] == EmploymentStatus.ACTIVE
        )
        self.assertEqual(active, rows, "a status held by several reported one")

    def test_a_teaching_duty_does_not_inflate_the_header(self):
        """The other half of the same defect, failing the other way.

        The ``teaching_load`` annotation joins the assignments table, so an
        undistinct ``Count("pk")`` counts a person once per duty they hold. Two
        duties for one person turned a school of three into a school of four,
        and the branch panel and the total disagreed by exactly the number of
        assignments nobody was looking at.
        """
        for subject in (self.maths, self.english):
            TeachingAssignment.all_objects.create(
                tenant=self.tenant, staff=self.eze,
                school_class=self.shared_class, subject=subject,
                session=self.year, part=TeachingPart.LEAD,
            )
        rows = StaffProfile.all_objects.filter(tenant=self.tenant).count()
        counts = self.get(self.admin, "staff-list").data["counts"]

        self.assertEqual(counts["total"], rows)
        self.assertEqual(sum(row["count"] for row in counts["breakdown"]), rows)
        # The one figure that SHOULD be a person count over a joined table, and
        # was already distinct. Asserted here so the fix to its neighbours
        # cannot quietly take it with them.
        self.assertEqual(counts["with_teaching_duties"], 1)

    def test_the_locked_count_is_an_account_count(self):
        self.eze.user.status = User.Status.LOCKED
        self.eze.user.save(update_fields=["status"])
        counts = self.get(self.admin, "staff-list").data["counts"]
        self.assertEqual(counts["locked_accounts"], 1)
        self.assertEqual(counts["currently_employed"], counts["total"])

    def test_the_teaching_filter_reads_assignments_and_not_a_job_title(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.registrar,
            school_class=self.shared_class, subject=self.maths,
            session=self.year, part=TeachingPart.LEAD,
        )
        response = self.get(self.admin, "staff-list", {"teaching": "true"})
        names = {row["full_name"] for row in response.data["data"]}
        self.assertEqual(names, {"Adaeze Nwankwo"})

    def test_the_employment_and_account_filters_are_separate(self):
        self.eze.user.status = User.Status.LOCKED
        self.eze.user.save(update_fields=["status"])
        by_account = self.get(
            self.admin, "staff-list", {"account_status": "LOCKED"},
        )
        self.assertEqual(len(by_account.data["data"]), 1)
        by_employment = self.get(
            self.admin, "staff-list", {"employment_status": "ACTIVE"},
        )
        self.assertGreater(len(by_employment.data["data"]), 1)

    def test_the_role_options_narrow_while_the_school_is_pending(self):
        from vs_tenants.models import Tenant

        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status"])
        response = self.get(self.admin, "staff-list")
        offered = {row["value"] for row in response.data["role_options"]}
        self.assertEqual(offered, {"school_admin"})

    def test_a_pending_school_refuses_a_role_the_dropdown_did_not_offer(self):
        """Enforced, not merely offered.

        A narrowed dropdown is a courtesy; without this a crafted request grants
        a bursar Payout Approver during onboarding, when there is nobody to
        review what was granted.
        """
        from vs_tenants.models import Tenant

        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status"])
        response = self.post(
            self.admin, "staff-list", self.invite_body(role="teacher"),
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("goes live", str(response.data).lower())

    def test_an_empty_directory_is_still_a_list(self):
        StaffProfile.all_objects.filter(tenant=self.solo.tenant).delete()
        response = self.get(self.solo_admin, "staff-list")
        self.assertEqual(response.data["data"], [])
