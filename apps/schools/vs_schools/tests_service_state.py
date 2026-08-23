"""Taking a school out of service, and bringing it back.

The action the branch lifecycle has been pointing at since it was written.
``LastBranchCannotLeaveService`` refuses to take a school's only branch out of
service and tells the operator to deactivate the school instead - advice that
could not be followed, because no endpoint did that. This is that endpoint.

The rule worth stating once: the status column is not what stops people
signing in. ``School.save()`` mirrors it onto the tenant, and
``Tenant.AUTHENTICABLE_STATUSES`` admits only ACTIVE and PENDING, so INACTIVE
locks out every account at the school. The tenant assertions below are
therefore the ones that matter most - a test that only checked the school row
would pass while everybody could still sign in.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_audit.models import AuditActionType, AuditEvent
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_tenants.models import BranchStatus, Tenant

from .models import School, SchoolStatus

MANAGE_KEY = "platform.schools.manage"


class SchoolServiceStateTests(TestCase):
    """``POST /v1/i/<slug>/service-state/``."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="service-state@example.com", super_admin=True,
        )

    def setUp(self):
        self.school = make_school(slug="bright-star", name="Bright Star School")
        self.school.status = SchoolStatus.ACTIVE
        self.school.save()
        self.branch = make_branch(
            self.school, name="Main Branch", status=BranchStatus.ACTIVE,
        )

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.vision_user)
        return client

    def _url(self, school=None):
        return reverse(
            "school-service-state", kwargs={"slug": (school or self.school).slug},
        )

    def _post(self, payload, *, expect, user=None, school=None):
        response = self._client(user).post(self._url(school), payload, format="json")
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def _tenant(self):
        return Tenant.objects.get(pk=self.school.tenant_id)

    # ── who may do it at all ────────────────────────────────────────────────

    def test_a_school_account_cannot_switch_a_school_off(self):
        """The key is namespaced ``platform.`` but nothing stops it being put in
        a school's own role, so IsVisionStaff is the boundary, not the name."""
        school_admin = make_school_admin(
            None, email="admin@bright-star.test", tenant=self.school.tenant,
        )
        role = make_platform_role(name="Sneaky")
        make_platform_role_permission(role, make_permission(MANAGE_KEY))
        make_platform_assignment(school_admin, role)

        self._post(
            {"to_state": "INACTIVE", "reason": "trying it on"},
            expect=403, user=school_admin,
        )
        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)

    def test_a_caller_without_the_key_is_refused(self):
        nobody = make_vision_user(email="nobody-service-state@example.com")

        self._post(
            {"to_state": "INACTIVE", "reason": "no key"}, expect=403, user=nobody,
        )
        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)

    # ── the thing itself ────────────────────────────────────────────────────

    def test_taking_a_school_out_of_service_stops_its_users_signing_in(self):
        self._post(
            {"to_state": "INACTIVE", "reason": "Contract ended 31 August."},
            expect=200,
        )

        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.INACTIVE)
        self.assertIsNotNone(self.school.deactivated_at)
        # The half that actually takes access away.
        self.assertEqual(self._tenant().status, Tenant.Status.INACTIVE)
        self.assertNotIn(self._tenant().status, Tenant.AUTHENTICABLE_STATUSES)

    def test_returning_it_to_service_lets_them_back_in(self):
        self.school.status = SchoolStatus.INACTIVE
        self.school.save()

        self._post({"to_state": "ACTIVE"}, expect=200)

        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)
        self.assertIsNone(self.school.deactivated_at)
        self.assertIn(self._tenant().status, Tenant.AUTHENTICABLE_STATUSES)

    def test_the_branches_are_left_exactly_as_they_were(self):
        """Their statuses are the record of which sites were trading, and
        returning the school to service has to restore that arrangement."""
        self._post(
            {"to_state": "INACTIVE", "reason": "Contract ended."}, expect=200,
        )

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.ACTIVE)
        self.assertTrue(self.branch.is_main)

    # ── the refusals ────────────────────────────────────────────────────────

    def test_a_reason_is_required_on_the_way_out(self):
        response = self._post({"to_state": "INACTIVE"}, expect=409)

        self.assertEqual(
            response.data["error"]["code"], "SCHOOL_DEACTIVATION_REASON_REQUIRED",
        )
        self.school.refresh_from_db()
        self.assertEqual(self.school.status, SchoolStatus.ACTIVE)

    def test_no_reason_is_needed_on_the_way_back(self):
        self.school.status = SchoolStatus.INACTIVE
        self.school.save()

        self._post({"to_state": "ACTIVE"}, expect=200)

    def test_asking_for_the_state_it_is_already_in_is_refused(self):
        response = self._post(
            {"to_state": "ACTIVE", "reason": "already"}, expect=409,
        )

        self.assertEqual(response.data["error"]["code"], "SCHOOL_ALREADY_IN_STATE")

    def test_a_school_still_onboarding_cannot_be_deactivated_here(self):
        """PENDING belongs to onboarding. Going live, the 90-day sweep and
        reinstatement each do more than move a column, so this endpoint must
        not be a second way in."""
        pending = make_school(slug="greenfield", name="Greenfield Academy")
        # make_school hands back an ACTIVE row, so the state under test is set
        # explicitly rather than assumed from the helper's default.
        pending.status = SchoolStatus.PENDING
        pending.save()

        response = self._post(
            {"to_state": "INACTIVE", "reason": "wrong door"},
            expect=409, school=pending,
        )

        self.assertEqual(response.data["error"]["code"], "INVALID_SCHOOL_TRANSITION")
        pending.refresh_from_db()
        self.assertEqual(pending.status, SchoolStatus.PENDING)

    def test_a_suspended_school_is_reinstated_not_reactivated_here(self):
        """SUSPENDED means the 90-day sweep gave up on its onboarding. Letting
        this endpoint set it ACTIVE would take a school live that never went
        live, skipping the go-live decision entirely."""
        self.school.status = SchoolStatus.SUSPENDED
        self.school.save()

        response = self._post({"to_state": "ACTIVE"}, expect=409)

        self.assertEqual(response.data["error"]["code"], "INVALID_SCHOOL_TRANSITION")

    def test_a_state_this_endpoint_does_not_own_is_refused_as_a_field_error(self):
        """PENDING and SUSPENDED are not in the choices at all, so the refusal
        arrives before any state is read."""
        self._post({"to_state": "SUSPENDED", "reason": "no"}, expect=400)
        self._post({"to_state": "PENDING", "reason": "no"}, expect=400)

    # ── the record it leaves ────────────────────────────────────────────────

    def test_the_reason_is_written_into_the_audit_trail(self):
        self._post(
            {"to_state": "INACTIVE", "reason": "Contract ended 31 August."},
            expect=200,
        )

        event = AuditEvent.objects.filter(
            entity_type="School", entity_id=str(self.school.pk),
            action_type=AuditActionType.UPDATE,
        ).latest("event_at")
        self.assertIn("Contract ended 31 August.", event.summary)
        self.assertEqual(event.tenant_id, self.school.tenant_id)

    def test_the_trail_records_coming_back_too(self):
        self.school.status = SchoolStatus.INACTIVE
        self.school.save()

        self._post({"to_state": "ACTIVE"}, expect=200)

        event = AuditEvent.objects.filter(
            entity_type="School", entity_id=str(self.school.pk),
        ).latest("event_at")
        self.assertIn("returned to service", event.summary)


class SchoolServiceStateModelTests(TestCase):
    """The same rules, reached without an HTTP request.

    The endpoint is not the only door: a shell, a data migration and a
    management command all write this model. The refusals live on
    ``change_service_state`` so every one of them is held to the same rules.
    """

    def setUp(self):
        self.school = make_school(slug="corona", name="Corona Secondary")
        self.school.status = SchoolStatus.ACTIVE
        self.school.save()

    def test_the_model_refuses_a_missing_reason(self):
        from vs_tenants.exceptions import SchoolDeactivationReasonRequired

        with self.assertRaises(SchoolDeactivationReasonRequired):
            self.school.change_service_state(to_state=SchoolStatus.INACTIVE)

    def test_the_model_refuses_an_edge_it_does_not_own(self):
        from vs_tenants.exceptions import InvalidSchoolTransition

        self.school.status = SchoolStatus.PENDING
        self.school.save()

        with self.assertRaises(InvalidSchoolTransition):
            self.school.change_service_state(
                to_state=SchoolStatus.INACTIVE, reason="no",
            )

    def test_a_refused_call_writes_nothing(self):
        with self.assertRaises(Exception):
            self.school.change_service_state(to_state=SchoolStatus.INACTIVE)

        self.assertEqual(
            School.objects.get(pk=self.school.pk).status, SchoolStatus.ACTIVE,
        )
