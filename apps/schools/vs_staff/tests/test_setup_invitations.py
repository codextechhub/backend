"""Staff imported while a school is set up are invited when it goes live.

Brightfield is still onboarding. Adaeze imports Funke and Sade from the staff
template; neither is emailed, both start as Teacher and read "Invited at
go-live". When CodeX approves Brightfield's go-live request, both invitations go
out, once, and a second approval of the same school sends nothing more. A
person whose invitation cannot be created is written down and skipped, and
Brightfield is live regardless.
"""
from __future__ import annotations

from unittest import mock

from django.utils import timezone

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.imports import create_staff_from_row, resolve_row
from schools.vs_staff.models import StaffProfile
from schools.vs_staff.services.setup_invitations import release_all
from vs_rbac.models import TenantUserRoleAssignment
from vs_user.models import User

from .test_imports import _ImportFixture

SEND = "vs_user.tasks.send_invitation_email_task.delay"


class _SetupFixture(_ImportFixture):
    def setUp(self):
        super().setUp()
        self.tenant.status = "PENDING"
        self.tenant.save(update_fields=["status"])

    def import_person(self, first, email, **extra):
        row = resolve_row(
            {"first_name": first, "last_name": "Adeyemi", "email": email, **extra},
            tenant=self.tenant, actor=self.admin,
        )
        self.assertTrue(row.ok, row.issues)
        with mock.patch(SEND) as delay, self.captureOnCommitCallbacks(execute=True):
            profile = create_staff_from_row(row, tenant=self.tenant, created_by=self.admin)
        self.assertEqual(delay.call_count, 0)
        return profile

    def release(self):
        with mock.patch(SEND) as delay, self.captureOnCommitCallbacks(execute=True):
            outcome = release_all(self.tenant, actor=self.admin)
        return outcome, delay


class SetupImportTests(_SetupFixture):
    def test_a_setup_import_emails_nobody_and_records_the_starting_role(self):
        profile = self.import_person("Funke", "funke@brightfield.test", role="school_admin")

        self.assertEqual(profile.employment_status, EmploymentStatus.AWAITING_GO_LIVE)
        self.assertEqual(profile.get_employment_status_display(), "Invited at go-live")
        self.assertEqual(profile.user.status, User.Status.PENDING_APPROVAL)
        self.assertFalse(hasattr(profile.user, "invitation"))
        self.assertEqual(
            [g.role.key for g in TenantUserRoleAssignment.objects.filter(
                user=profile.user, assignment_status="ACTIVE",
            )],
            ["teacher"],
        )

    def test_the_list_shows_them_as_invited_at_go_live(self):
        profile = self.import_person("Funke", "funke@brightfield.test")

        response = self.get(self.admin, "staff-list", {"employment_status": "AWAITING_GO_LIVE"})

        self.assertEqual([row["id"] for row in response.data["data"]], [profile.pk])
        self.assertEqual(response.data["data"][0]["employment_status_label"], "Invited at go-live")
        self.assertFalse(response.data["data"][0]["can_resend"])

    def test_resend_during_setup_is_refused(self):
        profile = self.import_person("Funke", "funke@brightfield.test")

        response = self.post(self.admin, "staff-resend", pk=profile.pk)

        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "INVITATION_HELD_FOR_GO_LIVE")
        self.assertEqual(
            response.data["message"],
            "Invitations for staff imported during setup go out when the school goes live.",
        )

    def test_revoking_a_held_invitation_during_setup_closes_it_and_sends_nothing(self):
        profile = self.import_person("Funke", "funke@brightfield.test")

        response = self.post(
            self.admin, "staff-invitation-revoke", {"reason": "Left before we opened."},
            pk=profile.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["message"],
            "Invitation withdrawn before it was sent. Nothing was sent to them.",
        )
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, EmploymentStatus.TERMINATED)
        self.assertEqual(profile.user.status, User.Status.REJECTED)
        outcome, delay = self.release()
        self.assertEqual((outcome["sent"], delay.call_count), ([], 0))


class GoLiveReleaseTests(_SetupFixture):
    def test_going_live_sends_every_held_invitation_once(self):
        funke = self.import_person("Funke", "funke@brightfield.test")
        sade = self.import_person("Sade", "sade@brightfield.test")

        first, delay = self.release()
        again, delay_again = self.release()

        self.assertEqual(sorted(first["sent"]), sorted([funke.pk, sade.pk]))
        self.assertEqual(delay.call_count, 2)
        self.assertEqual((again, delay_again.call_count), ({"sent": [], "failed": []}, 0))
        for profile in (funke, sade):
            profile.refresh_from_db()
            self.assertEqual(profile.employment_status, EmploymentStatus.INVITED)
            self.assertEqual(profile.user.status, User.Status.PENDING)

    def test_a_row_that_said_no_gets_its_invitation_left_unsent(self):
        profile = self.import_person("Funke", "funke@brightfield.test", send_invitation="No")

        outcome, delay = self.release()

        self.assertEqual(outcome["sent"], [profile.pk])
        self.assertEqual(delay.call_count, 0)
        profile.refresh_from_db()
        self.assertEqual(profile.user.invitation.email_status, "PENDING")

    def test_one_failure_does_not_block_the_others(self):
        from vs_user.services.invitation import InvitationService

        funke = self.import_person("Funke", "funke@brightfield.test")
        sade = self.import_person("Sade", "sade@brightfield.test")
        real = InvitationService.create

        def create(*, user, invited_by):
            if user.email == "funke@brightfield.test":
                raise RuntimeError("The mail provider refused this address.")
            return real(user=user, invited_by=invited_by)

        with mock.patch.object(InvitationService, "create", side_effect=create):
            outcome, delay = self.release()

        self.assertEqual((outcome["sent"], outcome["failed"]), ([sade.pk], [funke.pk]))
        self.assertEqual(delay.call_count, 1)
        funke.refresh_from_db()
        self.assertEqual(funke.employment_status, EmploymentStatus.AWAITING_GO_LIVE)
        self.assertEqual(funke.user.status, User.Status.PENDING_APPROVAL)

        # Once live, the ordinary resend sends the one left behind.
        self.tenant.status = "ACTIVE"
        self.tenant.save(update_fields=["status"])
        with mock.patch(SEND) as resend, self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.admin, "staff-resend", pk=funke.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(resend.call_count, 1)

    def test_hire_approval_does_not_hold_the_setup_list(self):
        """Going live is the school approving the list it loaded."""
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        profile = self.import_person("Funke", "funke@brightfield.test")
        set_value(
            definition=ConfigurationDefinition.objects.get(key="staff.hire.requires_approval"),
            value=True, actor=self.admin, tenant=self.tenant,
        )

        outcome, delay = self.release()

        self.assertEqual((outcome["sent"], delay.call_count), ([profile.pk], 1))

    def test_approving_go_live_releases_them(self):
        from schools.vs_onboarding.constants import GoLiveStatus, ReadinessState
        from schools.vs_onboarding.models import GoLiveRequest, OnboardingProgress
        from schools.vs_onboarding.services.go_live import approve_go_live

        profile = self.import_person("Funke", "funke@brightfield.test")
        OnboardingProgress.all_objects.create(
            tenant=self.tenant, readiness_state=ReadinessState.PENDING_APPROVAL,
        )
        request = GoLiveRequest.all_objects.create(
            tenant=self.tenant, preferred_go_live_at=timezone.now(),
            acknowledged=True, status=GoLiveStatus.PENDING,
        )

        with mock.patch(SEND) as delay, self.captureOnCommitCallbacks(execute=True):
            approve_go_live(self.tenant, request.pk, actor=self.admin)

        self.assertEqual(delay.call_count, 1)
        self.assertEqual(
            StaffProfile.all_objects.get(pk=profile.pk).employment_status,
            EmploymentStatus.INVITED,
        )


class LiveImportUnchangedTests(_ImportFixture):
    def test_an_import_at_a_live_school_invites_at_once(self):
        row = resolve_row(
            {"first_name": "Funke", "last_name": "Adeyemi", "email": "funke@brightfield.test"},
            tenant=self.tenant, actor=self.admin,
        )
        with mock.patch(SEND) as delay, self.captureOnCommitCallbacks(execute=True):
            profile = create_staff_from_row(row, tenant=self.tenant, created_by=self.admin)

        self.assertEqual(delay.call_count, 1)
        self.assertEqual(profile.employment_status, EmploymentStatus.INVITED)
