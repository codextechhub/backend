"""Which account statuses may sign in, and which may hold a password.

The defect these tests close
----------------------------
Every gate on this question used to be written as a list of the statuses it
wanted to REFUSE, with everything else falling through to permitted:

* ``LoginService._check_status`` was a dict of error payloads consulted with
  ``.get()``. PENDING, LOCKED, SUSPENDED and DEACTIVATED were in it. DRAFT,
  PENDING_APPROVAL and REJECTED were not, so all three signed in and got tokens,
  a session row and their effective permission set.
* ``AdminPasswordResetView`` checked nothing at all, so an administrator could
  put a working password on an account in any state whatever.
* ``PasswordService.confirm_reset`` promoted LOCKED and PENDING to ACTIVE and
  left every other status alone - so a rejected hire came out of it still
  REJECTED and now holding a live credential.
* ``vs_rbac.permissions.IsAuthenticatedAndActive`` compared against three
  string literals, so the same three statuses passed the per-request gate too.

Concretely, and this is what the tests below reproduce: Bright Star asks Codex
for a finance officer seat for Emeka. The hire is entered, the Finance Officer
role is written onto his account so the approver can see what they are agreeing
to, and Amaka rejects it - he failed his reference check. His account goes to
REJECTED. Then an administrator, tidying up a list of accounts with no
password, clicks "reset password" on his row. Emeka gets the email, sets a
password, signs in, and is a finance officer at Bright Star.

Every gate is written the other way round now: ``User.SIGN_IN_STATUSES`` and
``User.PASSWORD_STATUSES`` enumerate what is PERMITTED, and the properties
``may_sign_in`` / ``may_hold_password`` are the only things the call sites read.
So the tests are a matrix over ``User.Status.values`` rather than a list of
cases, and :class:`StatusModelIsTotalTests` fails if a status is ever added to
the enum without an entry - which is exactly how the three above got in.
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.permissions import IsAuthenticatedAndActive
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
)
from vs_user.models import AccountLockout, PasswordResetRequest, User
from vs_user.services.auth import LoginService
from vs_user.services.password import PasswordService


PW = "Str0ng!pass123"
NEW_PW = "An0ther!pass99"

#: The whole status model in one table: may this status sign in, and may it hold
#: a usable password? Everything below is driven from here, so the expectations
#: live in one place instead of being restated per test.
#:
#: The second column is wider than the first, and deliberately so. PENDING is
#: the ordinary invited-but-not-yet-activated account and the invitation link
#: exists precisely to set its first password; LOCKED is unlocked BY a reset;
#: SUSPENDED may have a suspect credential replaced without that being a
#: reinstatement. None of the three may sign in while they are in that state.
EXPECTED = {
    #  status                      may_sign_in  may_hold_password
    User.Status.DRAFT:            (False,       False),
    User.Status.PENDING_APPROVAL: (False,       False),
    User.Status.REJECTED:         (False,       False),
    User.Status.PENDING:          (False,       True),
    User.Status.ACTIVE:           (True,        True),
    User.Status.SUSPENDED:        (False,       True),
    User.Status.LOCKED:           (False,       True),
    User.Status.DEACTIVATED:      (False,       False),
}

#: The three the defect was about: never approved into existence, so a sign-in
#: must not even confirm they are accounts.
NEVER_APPROVED = (
    User.Status.DRAFT, User.Status.PENDING_APPROVAL, User.Status.REJECTED,
)


class _Fixture(TestCase):
    """One school, and an account in whatever status a test asks for."""

    def setUp(self):
        self.school = make_school(slug="bright-star", name="Bright Star School")
        self.branch = make_branch(self.school, name="Main Branch", is_main=True)
        self.tenant = self.school.tenant
        self._n = 0
        self._admin = None

    def user(self, status, *, email=None, password=PW):
        self._n += 1
        user = User.objects.create_user(
            email=email or f"person{self._n}@bright-star.test",
            password=password,
            status=status,
            first_name="Test",
            last_name=f"Person{self._n}",
            tenant=self.tenant,
            branch=self.branch,
        )
        if status == User.Status.LOCKED:
            # A LOCKED account without a lockout row would be refused by the
            # status gate but never reach the lockout branch above it; the row
            # keeps the fixture honest about which gate is doing the work.
            AccountLockout.objects.create(user=user, failure_count=5)
        return User.objects.get(pk=user.pk)

    def admin(self):
        """An account that may administer the others, with the reset key.

        Memoised: several tests loop over the status table and would otherwise
        try to create Amaka once per iteration.
        """
        if getattr(self, "_admin", None) is None:
            user = self.user(User.Status.ACTIVE, email="amaka@bright-star.test")
            role = make_role(self.tenant, name="Administrator")
            # update -> the admin reset; create -> the invitation resend. Both
            # are held, so a refusal in these tests is never about the key.
            for key in ("platform.team.view", "platform.team.create",
                        "platform.team.update"):
                make_role_permission(role, make_permission(key))
            make_assignment(self.tenant, user, role)
            self._admin = user
        return self._admin

    def login(self, user, password=PW):
        """Sign in over HTTP, exactly as the frontend does."""
        return self.login_as(user.email, password)

    def login_as(self, email, password=PW):
        # ``LoginView`` carries throttle_scope='login' and these tests attempt a
        # sign-in per status in a single method, which trips it. The counter
        # lives in the default cache, so clearing it isolates each attempt
        # without disabling the throttle - which is itself a security control
        # and should stay switched on in the code under test.
        cache.clear()
        return APIClient().post(
            "/v1/user/auth/login/",
            {"email": email, "password": password, "tenant": self.tenant.slug},
            format="json",
        )


# ─────────────────────────────────────────────────────────────────────────────
# The status model itself
# ─────────────────────────────────────────────────────────────────────────────

class StatusModelIsTotalTests(_Fixture):
    """The predicate must have an answer for every status that exists.

    This is the regression test for the shape of the bug rather than for any
    one instance of it. A status added to ``User.Status`` without a decision
    about sign-in and passwords fails here, loudly, instead of quietly
    inheriting a working login the way DRAFT, PENDING_APPROVAL and REJECTED
    each did on the day they were added.
    """

    def test_every_status_has_a_decision(self):
        self.assertEqual(
            set(User.Status.values), {str(s) for s in EXPECTED},
            "A status was added to User.Status without deciding whether it may "
            "sign in or hold a password. Add it to EXPECTED and to "
            "User.SIGN_IN_STATUSES / User.PASSWORD_STATUSES as appropriate - "
            "the default is refusal, and that must stay a deliberate choice.",
        )

    def test_permitted_sets_match_the_table(self):
        self.assertEqual(
            {str(s) for s in User.SIGN_IN_STATUSES},
            {str(s) for s, (signin, _) in EXPECTED.items() if signin},
        )
        self.assertEqual(
            {str(s) for s in User.PASSWORD_STATUSES},
            {str(s) for s, (_, pw) in EXPECTED.items() if pw},
        )

    def test_sign_in_is_a_subset_of_password(self):
        """Anything that may sign in must be able to hold the password it
        signs in with. The reverse is not true and is the point of two sets."""
        self.assertTrue(User.SIGN_IN_STATUSES <= User.PASSWORD_STATUSES)

    def test_properties_answer_from_the_sets(self):
        for status, (signin, password) in EXPECTED.items():
            with self.subTest(status=status):
                user = self.user(status)
                self.assertIs(user.may_sign_in, signin)
                self.assertIs(user.may_hold_password, password)


class IsActiveIsDerivedTests(_Fixture):
    """``is_active`` is a cache of ``status``, not a second opinion about it."""

    def test_is_active_is_true_only_for_active(self):
        for status in EXPECTED:
            if status == User.Status.LOCKED:
                continue  # deliberate exception, covered below
            with self.subTest(status=status):
                user = self.user(status)
                self.assertIs(user.is_active, status == User.Status.ACTIVE)

    def test_draft_is_not_active(self):
        """The hole that made the whole chain exploitable.

        ``_sync_is_active`` listed five statuses that meant False and left
        DRAFT alone, so a draft could keep an ``is_active=True`` flag - and
        ``confirm_reset`` set exactly that flag by hand. A parked draft came
        out of a password reset with a credential AND a session SimpleJWT
        accepted, because ``JWTAuthentication.get_user`` reads ``is_active``.
        """
        user = self.user(User.Status.DRAFT)
        self.assertFalse(user.is_active)

        user.is_active = True
        user.save(update_fields=["is_active", "updated_at"])
        self.assertFalse(User.objects.get(pk=user.pk).is_active)

    def test_locked_keeps_its_flag_so_unlocking_restores_the_account(self):
        user = self.user(User.Status.ACTIVE)
        self.assertTrue(user.is_active)
        user.status = User.Status.LOCKED
        user.save(update_fields=["status", "updated_at"])
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

    def test_status_change_writes_is_active_even_when_not_asked_for(self):
        user = self.user(User.Status.ACTIVE)
        user.status = User.Status.DEACTIVATED
        user.save(update_fields=["status", "updated_at"])
        self.assertFalse(User.objects.get(pk=user.pk).is_active)


# ─────────────────────────────────────────────────────────────────────────────
# Sign-in
# ─────────────────────────────────────────────────────────────────────────────

class SignInStatusGateTests(_Fixture):
    """Call site 1: ``LoginService._check_status``."""

    def test_only_active_signs_in(self):
        for status, (may, _) in EXPECTED.items():
            with self.subTest(status=status):
                user = self.user(status)
                response = self.login(user)
                if may:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("access", response.json()["data"])
                else:
                    self.assertIn(response.status_code, (401, 403))
                    self.assertNotIn("access", response.json().get("data") or {})

    def test_never_approved_accounts_are_refused(self):
        for status in NEVER_APPROVED:
            with self.subTest(status=status):
                user = self.user(status)
                with self.assertRaises(ValueError) as caught:
                    LoginService.login(
                        email=user.email, password=PW, tenant=self.tenant.slug,
                    )
                self.assertEqual(caught.exception.args[0]["code"],
                                 "INVALID_CREDENTIALS")

    def test_refusal_is_indistinguishable_from_a_wrong_password(self):
        """A rejected hire must not learn from this endpoint that they exist.

        Unlike PENDING or LOCKED there is nobody legitimate to inform: no
        invitation was ever sent for these accounts. So the answer has to be
        the answer an unknown address gets - the same status code, the same
        body, the same error code - and not merely a different 4xx.
        """
        unknown = self.login_as("nobody@bright-star.test")
        active = self.user(User.Status.ACTIVE, email="real@bright-star.test")
        wrong_password = self.login(active, password="Wr0ng!pass123")

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_password.json(), unknown.json())

        for status in NEVER_APPROVED:
            with self.subTest(status=status):
                user = self.user(status)
                refused = self.login(user)
                self.assertEqual(refused.status_code, 401)
                self.assertEqual(refused.json(), unknown.json())

    def test_the_four_named_statuses_keep_their_own_messages(self):
        """PENDING especially - the normal invited-but-not-activated path.

        These four have a legitimate holder who needs to be told what to do,
        and the frontend routes on the code (403 vs 401), so none of this may
        change while the three above become generic.
        """
        expected = {
            User.Status.PENDING:     "ACCOUNT_NOT_ACTIVATED",
            User.Status.LOCKED:      "ACCOUNT_LOCKED",
            User.Status.SUSPENDED:   "ACCOUNT_SUSPENDED",
            User.Status.DEACTIVATED: "ACCOUNT_DEACTIVATED",
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                user = self.user(status)
                response = self.login(user)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], code)

    def test_active_signs_in_and_gets_a_session(self):
        user = self.user(User.Status.ACTIVE)
        result = LoginService.login(
            email=user.email, password=PW, tenant=self.tenant.slug,
        )
        self.assertIn("access", result)
        self.assertIsNotNone(result["session_id"])


class RequestGateTests(_Fixture):
    """Call site 2: ``IsAuthenticatedAndActive`` in vs_rbac.

    The per-request gate has to agree with the sign-in gate, or a token issued
    by one is honoured or refused by the other for different reasons. Both read
    ``may_sign_in`` now.
    """

    def _check(self, user):
        request = type("R", (), {"user": user, "method": "GET"})()
        return IsAuthenticatedAndActive().has_permission(request, None)

    def test_gate_matches_may_sign_in_for_every_status(self):
        for status, (may, _) in EXPECTED.items():
            with self.subTest(status=status):
                user = self.user(status)
                if may:
                    self.assertTrue(self._check(user))
                else:
                    with self.assertRaises(PermissionDenied):
                        self._check(user)

    def test_a_principal_without_the_predicate_is_refused(self):
        """A ``request.user`` that is not a real account must be refused, not
        waved through by the attribute simply being absent."""
        stranger = type("U", (), {"is_authenticated": True, "status": "ACTIVE"})()
        with self.assertRaises(PermissionDenied):
            self._check(stranger)


# ─────────────────────────────────────────────────────────────────────────────
# Passwords
# ─────────────────────────────────────────────────────────────────────────────

class AdminPasswordResetStatusTests(_Fixture):
    """Call site 3: the admin-initiated reset."""

    def setUp(self):
        super().setUp()
        self.amaka = self.admin()
        self.client = TenantAPIClient(self.amaka)

    def post(self, target):
        return self.client.post(
            f"/v1/user/{target.pk}/password-reset/", {}, format="json",
        )

    def test_reset_follows_may_hold_password_for_every_status(self):
        for status, (_, may) in EXPECTED.items():
            with self.subTest(status=status):
                target = self.user(status)
                response = self.post(target)
                if may:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(PasswordResetRequest.objects.filter(
                        user=target, used_at__isnull=True).exists())
                else:
                    self.assertEqual(response.status_code, 422)
                    self.assertFalse(PasswordResetRequest.objects.filter(
                        user=target).exists())

    def test_never_approved_accounts_are_refused(self):
        for status in NEVER_APPROVED:
            with self.subTest(status=status):
                target = self.user(status)
                response = self.post(target)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["error"]["error_code"], "ACCOUNT_NOT_ELIGIBLE",
                )

    def test_deactivated_is_refused_here_as_it_already_was_self_service(self):
        """The two resets disagreed: self-service refused DEACTIVATED and the
        admin route did not. They read the same predicate now."""
        target = self.user(User.Status.DEACTIVATED)
        self.assertEqual(self.post(target).status_code, 422)

    def test_active_pending_and_locked_still_work(self):
        for status in (User.Status.ACTIVE, User.Status.PENDING, User.Status.LOCKED):
            with self.subTest(status=status):
                target = self.user(status)
                self.assertEqual(self.post(target).status_code, 200)

    def test_the_check_is_in_the_service_not_only_the_view(self):
        """Any future caller of the service is covered, not just this URL."""
        target = self.user(User.Status.REJECTED)
        with self.assertRaises(ValueError) as caught:
            PasswordService.admin_reset(
                target_user=target, requesting_user=self.amaka,
            )
        self.assertEqual(
            caught.exception.args[0]["error_code"], "ACCOUNT_NOT_ELIGIBLE",
        )


class SelfServiceResetStatusTests(_Fixture):
    """Call sites 4 and 5: ``request_reset`` and ``confirm_reset``."""

    def test_request_is_silent_but_sends_nothing_to_an_ineligible_account(self):
        for status, (_, may) in EXPECTED.items():
            with self.subTest(status=status):
                user = self.user(status)
                PasswordService.request_reset(
                    email=user.email, tenant=self.tenant.slug,
                )
                self.assertIs(
                    PasswordResetRequest.objects.filter(user=user).exists(), may,
                )

    def test_confirm_refuses_an_account_closed_since_the_link_was_issued(self):
        """The state can change between the email going out and the click.

        A hire is approved, invited, and the workflow is then withdrawn - which
        runs the same rejection path and drives them to REJECTED - while their
        reset link sits unused and unexpired in an inbox.
        """
        user = self.user(User.Status.PENDING)
        admin = self.admin()
        PasswordService.admin_reset(target_user=user, requesting_user=admin)

        user.status = User.Status.REJECTED
        user.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ValueError) as caught:
            PasswordService.confirm_reset(
                user=User.objects.get(pk=user.pk), new_password=NEW_PW,
            )
        self.assertEqual(
            caught.exception.args[0]["error_code"], "ACCOUNT_NOT_ELIGIBLE",
        )
        self.assertFalse(
            User.objects.get(pk=user.pk).check_password(NEW_PW),
        )

    def test_confirm_promotes_locked_and_pending_to_active(self):
        for status in (User.Status.LOCKED, User.Status.PENDING):
            with self.subTest(status=status):
                user = self.user(status)
                PasswordService.admin_reset(
                    target_user=user, requesting_user=self.admin(),
                )
                PasswordService.confirm_reset(user=user, new_password=NEW_PW)
                fresh = User.objects.get(pk=user.pk)
                self.assertEqual(fresh.status, User.Status.ACTIVE)
                self.assertTrue(fresh.is_active)
                self.assertTrue(fresh.check_password(NEW_PW))
                if status == User.Status.LOCKED:
                    lockout = AccountLockout.objects.get(user=fresh)
                    self.assertEqual(lockout.failure_count, 0)

    def test_confirm_does_not_reinstate_a_suspended_account(self):
        """A new password is not a reinstatement - and the account still may
        not sign in with it."""
        user = self.user(User.Status.SUSPENDED)
        PasswordService.admin_reset(target_user=user, requesting_user=self.admin())
        PasswordService.confirm_reset(user=user, new_password=NEW_PW)
        fresh = User.objects.get(pk=user.pk)
        self.assertEqual(fresh.status, User.Status.SUSPENDED)
        self.assertFalse(fresh.is_active)
        self.assertEqual(self.login(fresh, password=NEW_PW).status_code, 403)

    def test_no_status_leaves_confirm_with_a_credential_it_cannot_use(self):
        """The invariant part 2 of the defect broke.

        Every status that reaches ``confirm_reset`` either becomes ACTIVE or
        was already unable to sign in for a reason the reset does not touch.
        None may end up as a status that is refused at sign-in while quietly
        holding a brand-new working password it was just handed - which is
        exactly what REJECTED did.
        """
        for status, (_, may) in EXPECTED.items():
            if not may:
                continue
            with self.subTest(status=status):
                user = self.user(status)
                PasswordService.admin_reset(
                    target_user=user, requesting_user=self.admin(),
                )
                PasswordService.confirm_reset(user=user, new_password=NEW_PW)
                fresh = User.objects.get(pk=user.pk)
                if status in (User.Status.LOCKED, User.Status.PENDING,
                              User.Status.ACTIVE):
                    self.assertTrue(fresh.may_sign_in)
                else:
                    self.assertEqual(fresh.status, status)
                    self.assertFalse(fresh.may_sign_in)


class LoggedInChangeStatusTests(_Fixture):
    """Call site 6: the logged-in password change reads the same predicate."""

    def test_change_refuses_an_account_that_may_not_hold_a_password(self):
        user = self.user(User.Status.REJECTED)
        with self.assertRaises(ValueError) as caught:
            PasswordService.change(user=user, new_password=NEW_PW)
        self.assertEqual(
            caught.exception.args[0]["error_code"], "ACCOUNT_NOT_ELIGIBLE",
        )

    def test_change_still_works_for_an_active_account(self):
        user = self.user(User.Status.ACTIVE)
        PasswordService.change(user=user, new_password=NEW_PW)
        self.assertTrue(User.objects.get(pk=user.pk).check_password(NEW_PW))


class InvitationActivationStatusTests(_Fixture):
    """Call site 7: a live invitation link must not revive a closed account."""

    def test_a_rejected_account_cannot_activate_through_its_old_link(self):
        from vs_user.services.invitation import InvitationService

        user = self.user(User.Status.PENDING)
        InvitationService.create(user=user, invited_by=self.admin())
        key = str(User.objects.get(pk=user.pk).activation_key)

        user.status = User.Status.REJECTED
        user.save(update_fields=["status", "updated_at"])

        with self.assertRaises(ValueError) as caught:
            InvitationService.activate(activation_key=key, password=NEW_PW)
        self.assertEqual(
            caught.exception.args[0]["error_code"], "INVITATION_NOT_ACTIONABLE",
        )
        fresh = User.objects.get(pk=user.pk)
        self.assertEqual(fresh.status, User.Status.REJECTED)
        self.assertFalse(fresh.check_password(NEW_PW))

    def test_a_pending_account_activates_normally(self):
        from vs_user.services.invitation import InvitationService

        user = self.user(User.Status.PENDING)
        InvitationService.create(user=user, invited_by=self.admin())
        key = str(User.objects.get(pk=user.pk).activation_key)

        InvitationService.activate(activation_key=key, password=NEW_PW)
        fresh = User.objects.get(pk=user.pk)
        self.assertEqual(fresh.status, User.Status.ACTIVE)
        self.assertTrue(fresh.is_active)
        self.assertTrue(fresh.check_password(NEW_PW))


# ─────────────────────────────────────────────────────────────────────────────
# The grant that outlives the account
# ─────────────────────────────────────────────────────────────────────────────

class RejectedHireLosesTheRoleTests(_Fixture):
    """Rejection vacates the role, not only the seat.

    ``create_pending`` writes the role assignment before an approver has seen
    the request - it must, because the role is what the approval card shows.
    Rejecting the hire used to vacate the organogram position and leave that
    grant ACTIVE, so the permissions the approver declined to hand over stayed
    on the account and ``get_effective_permissions`` still returned them.
    """

    def _reject(self, user):
        """Drive the real handler, not a hand-written status change."""
        from vs_user.workflow_handlers import UserCreationWorkflowHandler

        instance = type("I", (), {
            "document_object_id": user.pk,
            "requested_by": None,
        })()
        UserCreationWorkflowHandler().on_rejected(instance, {})

    def _hire_with_a_role(self):
        user = self.user(User.Status.PENDING_APPROVAL)
        role = make_role(self.tenant, name="Finance Officer")
        make_role_permission(role, make_permission("finance.invoice.view"))
        make_assignment(self.tenant, user, role)
        return user

    def test_rejection_revokes_the_grant(self):
        user = self._hire_with_a_role()
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                user=user, assignment_status="ACTIVE").count(), 1,
        )

        self._reject(user)

        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                user=user, assignment_status="ACTIVE").count(), 0,
        )

    def test_the_row_survives_for_audit(self):
        """Revoked, not deleted - what was asked for and refused stays
        readable, with a timestamp and a reason."""
        user = self._hire_with_a_role()
        self._reject(user)

        grant = TenantUserRoleAssignment.objects.get(user=user)
        self.assertEqual(grant.assignment_status, "REVOKED")
        self.assertIsNotNone(grant.revoked_at)
        self.assertIn("not approved", grant.reason_note)

    def test_the_permissions_are_gone(self):
        from vs_rbac.evaluator import get_effective_permissions

        user = self._hire_with_a_role()
        self.assertIn(
            "finance.invoice.view",
            get_effective_permissions(user, tenant=self.tenant),
        )

        self._reject(user)

        self.assertNotIn(
            "finance.invoice.view",
            get_effective_permissions(
                User.objects.get(pk=user.pk), tenant=self.tenant,
            ),
        )

    def test_rejection_still_sets_the_status_and_clears_is_active(self):
        user = self._hire_with_a_role()
        self._reject(user)
        fresh = User.objects.get(pk=user.pk)
        self.assertEqual(fresh.status, User.Status.REJECTED)
        self.assertFalse(fresh.is_active)


class RejectedAccountHasNoWayBackTests(_Fixture):
    """The routes that could revive a rejected account, checked together.

    Each is closed by a different guard, so they are asserted in one place:
    closing one and leaving another open would leave the grant reachable again.
    """

    def setUp(self):
        super().setUp()
        self.amaka = self.admin()
        self.client = TenantAPIClient(self.amaka)
        self.target = self.user(User.Status.REJECTED)

    def test_cannot_sign_in(self):
        self.assertEqual(self.login(self.target).status_code, 401)

    def test_cannot_be_given_a_password_by_an_admin(self):
        self.assertEqual(
            self.client.post(
                f"/v1/user/{self.target.pk}/password-reset/", {}, format="json",
            ).status_code, 422,
        )

    def test_cannot_request_a_reset_for_themselves(self):
        PasswordService.request_reset(
            email=self.target.email, tenant=self.tenant.slug,
        )
        self.assertFalse(
            PasswordResetRequest.objects.filter(user=self.target).exists(),
        )

    def test_cannot_be_reinvited(self):
        response = self.client.post(
            f"/v1/user/{self.target.pk}/invite/resend/", {}, format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_cannot_be_reactivated(self):
        from vs_user.services.user import UserStatusService

        with self.assertRaises(ValueError):
            UserStatusService.reactivate(self.target, self.amaka)
