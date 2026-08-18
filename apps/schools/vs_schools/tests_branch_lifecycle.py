"""
Branch lifecycle transitions and the BranchLifecycle audit trail.

`Branch.transition()` used to save a `deleted_at` field that does not exist on
the model, so every transition raised ValueError before the audit row was
written and the endpoint that drives it returned a 500. These tests pin both
the persisted timestamps and the history row for each transition.
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_school_admin,
    make_vision_user,
)

from vs_tenants.exceptions import InvalidBranchTransition
from vs_tenants.models import Branch, BranchLifecycle, BranchStatus


class BranchTransitionModelTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="lifecycle-school", name="Lifecycle School")
        # The subject is an ordinary campus, and the school keeps a main branch
        # beside it. These tests are about what each edge does to the
        # timestamps and the history row; a *main* branch is refused every
        # out-of-service edge outright (Branch._assert_may_leave_service), so
        # driving them through one would test the guard instead - which
        # ``MainBranchLifecycleGuardTests`` in ``vs_tenants`` already does.
        self.head_office = make_branch(self.school, name="Head Office")
        self.branch = make_branch(
            self.school,
            name="Lekki Campus",
            is_main=False,
            status=BranchStatus.PENDING,
        )

    def _events(self, branch=None):
        return list(
            BranchLifecycle.objects.filter(branch=branch or self.branch)
            .order_by("occurred_at", "pk")
        )

    # --- the regression itself -------------------------------------------

    def test_transition_persists_status_and_writes_history(self):
        self.branch.mark_active(actor_id="admin@example.com", reason="Opened")

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.ACTIVE)

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].from_state, BranchStatus.PENDING)
        self.assertEqual(events[0].to_state, BranchStatus.ACTIVE)
        self.assertEqual(events[0].actor_id, "admin@example.com")
        self.assertEqual(events[0].reason, "Opened")

    # --- per-transition timestamp rules ----------------------------------

    def test_mark_active_stamps_activated_at(self):
        self.assertIsNone(self.branch.activated_at)

        self.branch.mark_active(actor_id="actor")

        self.branch.refresh_from_db()
        self.assertIsNotNone(self.branch.activated_at)
        self.assertIsNone(self.branch.deactivated_at)

    def test_suspend_stamps_deactivated_at(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.suspend(actor_id="actor", reason="Unpaid invoices")

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.SUSPENDED)
        self.assertIsNotNone(self.branch.deactivated_at)

        events = self._events()
        self.assertEqual(
            [(e.from_state, e.to_state) for e in events],
            [
                (BranchStatus.PENDING, BranchStatus.ACTIVE),
                (BranchStatus.ACTIVE, BranchStatus.SUSPENDED),
            ],
        )
        self.assertEqual(events[-1].reason, "Unpaid invoices")

    def test_mark_inactive_stamps_deactivated_at(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.mark_inactive(actor_id="actor", reason="Campus merged")

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.INACTIVE)
        self.assertIsNotNone(self.branch.deactivated_at)
        self.assertEqual(self._events()[-1].to_state, BranchStatus.INACTIVE)

    def test_reactivate_clears_deactivated_at_and_keeps_first_activation(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.refresh_from_db()
        first_activation = self.branch.activated_at

        self.branch.suspend(actor_id="actor", reason="Temporary")
        self.branch.reactivate(actor_id="actor", reason="Resolved")

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.ACTIVE)
        self.assertIsNone(self.branch.deactivated_at)
        # activated_at records the first activation and is never rewritten.
        self.assertEqual(self.branch.activated_at, first_activation)
        self.assertEqual(len(self._events()), 3)

    def test_close_stamps_closed_at_and_deactivated_at(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.transition(
            to_state=BranchStatus.CLOSED,
            actor_id="actor",
            reason="Campus shut down",
        )

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatus.CLOSED)
        self.assertIsNotNone(self.branch.closed_at)
        self.assertIsNotNone(self.branch.deactivated_at)
        self.assertEqual(self._events()[-1].to_state, BranchStatus.CLOSED)

    # --- the allowed-transition matrix ------------------------------------

    def test_closed_is_terminal(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.transition(to_state=BranchStatus.CLOSED, actor_id="actor")

        for target in (
            BranchStatus.ACTIVE,
            BranchStatus.SUSPENDED,
            BranchStatus.INACTIVE,
            BranchStatus.PENDING,
        ):
            with self.subTest(target=target):
                with self.assertRaises(InvalidBranchTransition):
                    self.branch.transition(to_state=target, actor_id="actor")

    def test_pending_cannot_be_suspended(self):
        # Nothing to suspend: the branch has never traded.
        with self.assertRaises(InvalidBranchTransition):
            self.branch.suspend(actor_id="actor", reason="Too early")

    def test_nothing_can_return_to_pending(self):
        self.branch.mark_active(actor_id="actor")

        with self.assertRaises(InvalidBranchTransition):
            self.branch.transition(to_state=BranchStatus.PENDING, actor_id="actor")

    def test_inactive_cannot_be_suspended_directly(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.mark_inactive(actor_id="actor", reason="Shelved")

        with self.assertRaises(InvalidBranchTransition):
            self.branch.suspend(actor_id="actor", reason="Wrong door")

    def test_refused_transition_changes_nothing(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.transition(to_state=BranchStatus.CLOSED, actor_id="actor")
        history_before = len(self._events())

        with self.assertRaises(InvalidBranchTransition):
            self.branch.transition(to_state=BranchStatus.ACTIVE, actor_id="actor")

        self.assertEqual(
            Branch.all_objects.get(pk=self.branch.pk).status, BranchStatus.CLOSED
        )
        self.assertEqual(len(self._events()), history_before)

    def test_the_matrix_covers_every_status(self):
        # A new BranchStatus must be given explicit edges, not silently become
        # a dead end via .get(..., frozenset()).
        self.assertEqual(
            set(Branch.ALLOWED_TRANSITIONS), set(BranchStatus.values)
        )

    # --- guards -----------------------------------------------------------

    def test_no_op_transition_writes_no_history(self):
        self.branch.mark_active(actor_id="actor")
        self.branch.mark_active(actor_id="actor")

        self.assertEqual(len(self._events()), 1)

    def test_status_and_history_are_written_as_one_unit(self):
        self.branch.mark_active(actor_id="actor")

        with mock.patch.object(
            BranchLifecycle.objects, "create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.branch.suspend(actor_id="actor", reason="Should roll back")

        # The status write must not survive the failed audit row.
        self.assertEqual(
            Branch.all_objects.get(pk=self.branch.pk).status,
            BranchStatus.ACTIVE,
        )

    def test_user_actor_is_coerced_to_the_char_field(self):
        actor = make_vision_user(email="lifecycle-actor@example.com")

        self.branch.mark_active(actor_id=actor)

        self.assertEqual(self._events()[0].actor_id, str(actor))

    def test_history_row_defaults_reason_to_empty_string(self):
        # The column is NOT NULL; omitting reason used to hit an IntegrityError.
        event = BranchLifecycle.objects.create(
            branch=self.branch,
            from_state=BranchStatus.PENDING,
            to_state=BranchStatus.ACTIVE,
            actor_id="actor",
        )

        event.refresh_from_db()
        self.assertEqual(event.reason, "")


class BranchTransitionEndpointTests(TestCase):
    """The POST endpoint is the only production path that changes branch status."""

    def setUp(self):
        # super_admin=True carries the RBAC grant for platform.branches.manage.
        self.vision_user = make_vision_user(
            email="lifecycle-vision@example.com", super_admin=True
        )

        self.school_a = make_school(slug="branch-tx-a", name="Tx School A")
        self.school_b = make_school(slug="branch-tx-b", name="Tx School B")
        # Each school keeps its main branch and the endpoint is driven against
        # an ordinary campus: the main branch of a school may not be suspended,
        # deactivated or closed at all, which is a different test.
        make_branch(self.school_a, name="A Main")
        make_branch(self.school_b, name="B Main")
        # Codes are allocated per tenant, so both of these are branch 2.
        self.branch_a = make_branch(
            self.school_a, name="A Annex", is_main=False,
            status=BranchStatus.PENDING,
        )
        self.branch_b = make_branch(
            self.school_b, name="B Annex", is_main=False,
            status=BranchStatus.PENDING,
        )

    def _url(self, school, branch):
        return reverse(
            "branch-transition",
            kwargs={"slug": school.slug, "code": branch.code},
        )

    def _vision_client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def test_transition_endpoint_activates_branch(self):
        response = self._vision_client().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE", "reason": "Ready to trade"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], BranchStatus.ACTIVE)

        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.ACTIVE)
        self.assertEqual(
            BranchLifecycle.objects.filter(branch=self.branch_a).count(), 1
        )

    def test_transition_endpoint_suspends_branch(self):
        self.branch_a.mark_active(actor_id="setup")

        response = self._vision_client().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "SUSPENDED", "reason": "Compliance hold"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.SUSPENDED)
        self.assertIsNotNone(self.branch_a.deactivated_at)

        last = BranchLifecycle.objects.filter(branch=self.branch_a).last()
        self.assertEqual(last.to_state, BranchStatus.SUSPENDED)
        self.assertEqual(last.reason, "Compliance hold")

    def test_transition_resolves_the_branch_within_its_own_school(self):
        response = self._vision_client().post(
            self._url(self.school_b, self.branch_b),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.branch_b.refresh_from_db()
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_b.status, BranchStatus.ACTIVE)
        # The other school's branch 1 must be untouched.
        self.assertEqual(self.branch_a.status, BranchStatus.PENDING)
        self.assertFalse(
            BranchLifecycle.objects.filter(branch=self.branch_a).exists()
        )

    def test_unknown_branch_for_this_school_is_404(self):
        response = self._vision_client().post(
            reverse(
                "branch-transition",
                kwargs={"slug": self.school_a.slug, "code": 999},
            ),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)

    def test_repeating_the_current_state_is_rejected(self):
        self.branch_a.mark_active(actor_id="setup")

        response = self._vision_client().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "BRANCH_ALREADY_IN_STATE"
        )
        self.assertEqual(
            BranchLifecycle.objects.filter(branch=self.branch_a).count(), 1
        )

    def test_reopening_a_closed_branch_is_refused(self):
        self.branch_a.mark_active(actor_id="setup")
        self.branch_a.transition(to_state=BranchStatus.CLOSED, actor_id="setup")

        response = self._vision_client().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE", "reason": "Changed our minds"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["error"]["code"], "INVALID_BRANCH_TRANSITION"
        )
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.CLOSED)

    def test_pending_is_not_an_offered_target(self):
        response = self._vision_client().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "PENDING"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.PENDING)

    def test_platform_user_without_the_permission_key_is_denied(self):
        # Vision staff, but holding no role that grants platform.branches.manage.
        plain_staff = make_vision_user(email="lifecycle-nokey@example.com")
        client = APIClient()
        client.force_authenticate(user=plain_staff)

        response = client.post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.PENDING)

    def test_non_vision_user_cannot_transition_a_branch(self):
        school_admin = make_school_admin(
            self.branch_a, email="lifecycle-admin@example.com"
        )
        client = APIClient()
        client.force_authenticate(user=school_admin)

        response = client.post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.PENDING)

    def test_anonymous_user_cannot_transition_a_branch(self):
        response = APIClient().post(
            self._url(self.school_a, self.branch_a),
            {"to_state": "ACTIVE"},
            format="json",
        )

        self.assertIn(response.status_code, (401, 403))
        self.branch_a.refresh_from_db()
        self.assertEqual(self.branch_a.status, BranchStatus.PENDING)
