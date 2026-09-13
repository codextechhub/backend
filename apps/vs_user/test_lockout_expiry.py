"""A lockout is a window, and it closes on its own.

The defect these tests close
----------------------------
A brute-force lockout was written in two places. ``AccountLockout.locked_until``
carried the moment it ended, and ``User.status`` was set to LOCKED beside it.
Only the first of those expires, so the fifteen minutes a school configured were
fifteen minutes and then forever: the sign-in got past the lockout check, which
had released the account, and was refused by the status check, which had not.

Concretely, and this is what the tests below reproduce: Mrs Okafor mistypes her
password five times on Tuesday morning at Brightfield. She waits the quarter of
an hour her school configured, and she still cannot sign in on Friday. An
administrator or a password reset is the only way back, for a control whose
whole purpose is to let go by itself.

``locked_until`` is the one source of truth now, and a lockout does not write the
status column at all. Two facts follow and both are asserted here: the window
still refuses for as long as it is open, and nothing but the clock is needed to
end it. Two more follow from the separation, and are asserted with them: an
administrator's own decisions about an account survive a lockout laid over them,
and a lockout no longer reaches through to a session somebody is already using.
"""
from __future__ import annotations

from datetime import timedelta
from importlib import import_module
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from core.test_utils import TenantAPIClient
from vs_rbac.permissions import IsAuthenticatedAndActive
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
)
from vs_user.models import AccountLockout, User
from vs_user.services.auth import LoginService
from vs_user.services.invitation import InvitationService
from vs_user.services.password import PasswordService
from vs_user.services.user import UserStatusService

PW = "Str0ng!pass123"
WRONG = "Wr0ng!pass123"
NEW_PW = "An0ther!pass99"

#: The product defaults, which are what a school gets until it configures
#: otherwise. Named here so the arithmetic in the tests reads as the policy it
#: is testing rather than as two bare numbers.
THRESHOLD = 5
WINDOW_MINUTES = 15


class _Fixture(TestCase):
    """One school, one teacher with a password, and the clock."""

    def setUp(self):
        self.school = make_school(slug="brightfield", name="Brightfield Schools")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school, name="Main Branch", is_main=True)
        self.okafor = self.person("okafor@brightfield.test", "Ngozi", "Okafor")

    # ── people ────────────────────────────────────────────────────────────

    def person(self, email, first="Test", last="Person", *,
               status=User.Status.ACTIVE, branch=True, password=PW):
        """An account. ``password=None`` leaves it unusable, as an invitation does."""
        return User.objects.create_user(
            email=email, password=password, status=status,
            first_name=first, last_name=last,
            tenant=self.tenant, branch=self.branch if branch else None,
        )

    def administrator(self):
        """Somebody who may suspend and unlock, with school-wide reach.

        Memoised: several tests want the same person, and a second Amaka would
        be a second account rather than a second fixture.
        """
        if getattr(self, "_admin", None) is None:
            admin = self.person("amaka@brightfield.test", "Amaka", "Obi", branch=False)
            role = make_role(self.tenant, name="Administrator")
            for key in ("platform.team.view", "platform.team.suspend",
                        "platform.team.reactivate"):
                make_role_permission(role, make_permission(key))
            make_assignment(self.tenant, admin, role)
            self._admin = admin
        return self._admin

    # ── signing in ────────────────────────────────────────────────────────

    def sign_in(self, user=None, password=PW):
        return LoginService.login(
            email=(user or self.okafor).email, password=password,
            tenant=self.tenant.slug,
        )

    def fail_sign_in(self, times=1, user=None):
        """Drive the real failed-sign-in path, which is what writes a lockout.

        Never by writing the row directly: the counter, the threshold and the
        window are the thing under test, and a fixture that set them would be
        asserting against itself.
        """
        for _ in range(times):
            with self.assertRaises(ValueError):
                self.sign_in(user, password=WRONG)

    def refusal_code(self, user=None, password=PW):
        with self.assertRaises(ValueError) as caught:
            self.sign_in(user, password)
        return caught.exception.args[0]["code"]

    @staticmethod
    def lockout_of(user):
        return AccountLockout.objects.get(user=user)

    @staticmethod
    def minutes_later(minutes):
        """Move the clock the lockout is judged against, and nothing else.

        ``is_locked_now`` is a comparison against ``timezone.now`` in
        ``vs_user.models``, so patching that one name is the whole of "time
        passed". No row is touched while it is patched, which is the point: the
        release has to happen with nobody acting.
        """
        return mock.patch(
            "vs_user.models.timezone.now",
            return_value=timezone.now() + timedelta(minutes=minutes),
        )


class LockoutOpensAndClosesTests(_Fixture):
    """The window itself: it still shuts, and it still opens again."""

    def test_the_threshold_still_locks_the_account(self):
        self.fail_sign_in(times=THRESHOLD - 1)
        self.assertFalse(self.lockout_of(self.okafor).is_locked_now())

        self.fail_sign_in()

        lockout = self.lockout_of(self.okafor)
        self.assertTrue(lockout.is_locked_now())
        self.assertEqual(lockout.locked_reason, "BRUTE_FORCE_THRESHOLD")

    def test_inside_the_window_the_right_password_is_refused_as_locked(self):
        """And told so, because the caller has proved they know it."""
        self.fail_sign_in(times=THRESHOLD)
        self.assertEqual(self.refusal_code(), "ACCOUNT_LOCKED")

    def test_the_window_passing_is_the_whole_of_the_release(self):
        """Mrs Okafor waits a quarter of an hour and signs in. That is all.

        No administrator, no password reset, and nothing written to any row in
        between: the only thing that changes between the refusal above and the
        sign-in below is the time.
        """
        self.fail_sign_in(times=THRESHOLD)
        self.assertEqual(self.refusal_code(), "ACCOUNT_LOCKED")

        with self.minutes_later(WINDOW_MINUTES + 1):
            result = self.sign_in()

        self.assertIn("access", result)
        self.assertIsNotNone(result["session_id"])

    def test_a_further_wrong_password_does_not_push_the_window_back(self):
        """Otherwise anybody who knows an address can hold its owner out.

        A stranger who keeps guessing at okafor@brightfield.test every ten
        minutes would renew her lockout every time, and she would never get back
        in however long she waited. Attempts made inside a window are counted as
        evidence and change nothing else.
        """
        self.fail_sign_in(times=THRESHOLD)
        opened_until = self.lockout_of(self.okafor).locked_until

        with self.minutes_later(WINDOW_MINUTES - 5):
            self.fail_sign_in(times=3)

        lockout = self.lockout_of(self.okafor)
        self.assertEqual(lockout.locked_until, opened_until)
        self.assertEqual(lockout.failure_count, THRESHOLD + 3)

        with self.minutes_later(WINDOW_MINUTES + 1):
            self.assertIn("access", self.sign_in())

    def test_a_spent_window_gives_back_the_whole_allowance(self):
        """The counter does not outlive the lockout it caused.

        Left standing at five, the first typo after the window passed would be
        the fifth again, and every single mistake from then on would cost Mrs
        Okafor another quarter of an hour. A window that has closed is spent.
        """
        self.fail_sign_in(times=THRESHOLD)

        with self.minutes_later(WINDOW_MINUTES + 1):
            self.fail_sign_in()
            lockout = self.lockout_of(self.okafor)
            self.assertEqual(lockout.failure_count, 1)
            self.assertFalse(lockout.is_locked_now())
            self.assertIn("access", self.sign_in())


class ALockoutDoesNotTouchTheAccountTests(_Fixture):
    """The status column belongs to the administrator, not to the lockout."""

    def test_the_status_is_left_exactly_where_it_was(self):
        self.fail_sign_in(times=THRESHOLD)

        fresh = User.objects.get(pk=self.okafor.pk)
        self.assertEqual(fresh.status, User.Status.ACTIVE)
        self.assertTrue(fresh.is_active)
        self.assertTrue(fresh.is_locked)

    def test_a_suspension_survives_being_guessed_at(self):
        """The worst of what one column for two facts did.

        Bright Star suspends Emeka's account while it investigates him. Somebody
        then guesses at his password five times, which used to overwrite his
        status with LOCKED - and the unlock that followed set him ACTIVE. His
        suspension was lifted by an attacker's failed attempts and an
        administrator tidying up a lockout list.
        """
        emeka = self.person("emeka@brightfield.test", status=User.Status.SUSPENDED)

        self.fail_sign_in(times=THRESHOLD, user=emeka)
        self.assertEqual(
            User.objects.get(pk=emeka.pk).status, User.Status.SUSPENDED,
        )

        UserStatusService.unlock(emeka, self.administrator())

        fresh = User.objects.get(pk=emeka.pk)
        self.assertEqual(fresh.status, User.Status.SUSPENDED)
        self.assertFalse(fresh.is_active)
        self.assertFalse(fresh.is_locked)
        self.assertEqual(self.refusal_code(emeka), "ACCOUNT_SUSPENDED")


class ALockoutDoesNotEndALiveSessionTests(_Fixture):
    """Whether a lockout reaches a session somebody is already using.

    It does not, and that is deliberate. A lockout exists to slow somebody
    guessing a password; the person guessing holds no session, so ending one
    only ever reaches the legitimate holder. Made to end sessions, the control
    is a denial of service anybody can aim at anybody: five wrong passwords for
    the principal's address, every quarter of an hour, and she is thrown out of
    her own school all day.

    Ending a session on purpose is an administrator's act and has its own
    endpoints - suspend, and force-logout - which are asserted below so that the
    difference between the two is held rather than assumed.
    """

    @staticmethod
    def gate(user):
        request = type("R", (), {"user": user, "method": "GET"})()
        return IsAuthenticatedAndActive().has_permission(request, None)

    def test_the_signed_in_teacher_keeps_working(self):
        self.fail_sign_in(times=THRESHOLD)
        self.assertTrue(self.gate(User.objects.get(pk=self.okafor.pk)))

    def test_the_gate_costs_no_query(self):
        """The reason the request gate does not consult the lockout row.

        It runs on every authenticated request, and the account's own status is
        already loaded with the user. Asking the lockout table here would be a
        query per request for a question that has no bearing on a session that
        already exists.
        """
        fresh = User.objects.get(pk=self.okafor.pk)
        with self.assertNumQueries(0):
            self.assertTrue(fresh.may_sign_in)

    def test_an_administrator_closing_the_account_does_end_it(self):
        UserStatusService.suspend(self.okafor, self.administrator())

        with self.assertRaises(PermissionDenied):
            self.gate(User.objects.get(pk=self.okafor.pk))


class AnAdministratorStillDecidesTests(_Fixture):
    """Deliberate lock and deliberate release, and neither confused with the other."""

    def setUp(self):
        super().setUp()
        self.client = TenantAPIClient(self.administrator())

    def test_an_administrator_can_still_close_an_account(self):
        response = self.client.post(
            f"/v1/user/{self.okafor.pk}/suspend/", {}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            User.objects.get(pk=self.okafor.pk).status, User.Status.SUSPENDED,
        )
        self.assertEqual(self.refusal_code(), "ACCOUNT_SUSPENDED")

    def test_an_administrator_can_release_a_lockout_early(self):
        """She does not have to wait out the window she is shortening."""
        self.fail_sign_in(times=THRESHOLD)

        response = self.client.post(
            f"/v1/user/{self.okafor.pk}/unlock/", {}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        lockout = self.lockout_of(self.okafor)
        self.assertIsNone(lockout.locked_until)
        self.assertEqual(lockout.failure_count, 0)
        self.assertIn("access", self.sign_in())

    def test_the_lockout_console_releases_the_same_state(self):
        """Two routes, one implementation, so they cannot answer differently."""
        self.fail_sign_in(times=THRESHOLD)

        response = self.client.post(
            "/v1/user/account-lockouts/unlock/",
            {"user_id": self.okafor.pk}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.lockout_of(self.okafor).has_state())
        self.assertIn("access", self.sign_in())

    def test_unlocking_an_account_with_nothing_to_clear_is_refused(self):
        """Honestly, rather than reporting a release that released nothing."""
        response = self.client.post(
            f"/v1/user/{self.okafor.pk}/unlock/", {}, format="json",
        )

        self.assertEqual(response.status_code, 422, response.data)

    def test_clearing_a_spent_counter_is_a_real_act(self):
        """Four failures and no lock is still something to clear.

        The fifth typo would reach the threshold, so an administrator clearing
        the count after somebody has been struggling with a new password is
        doing something, and must not be told there is nothing to do.
        """
        self.fail_sign_in(times=THRESHOLD - 1)

        response = self.client.post(
            f"/v1/user/{self.okafor.pk}/unlock/", {}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.lockout_of(self.okafor).failure_count, 0)


class APasswordResetEndsALockoutTests(_Fixture):
    """The credential being guessed no longer exists, so the window has no job."""

    def _reset_token(self, user):
        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                PasswordService.admin_reset(
                    target_user=user, requesting_user=self.administrator(),
                )
        return delay.call_args.kwargs["token"]

    def test_completing_a_reset_clears_the_lockout(self):
        """The branch that never ran once the status stopped moving.

        The clearing used to sit inside ``if user.status == LOCKED``. A lockout
        does not move the status, so Mrs Okafor could reset her own password and
        still be refused for the rest of her window, holding a brand-new
        credential she was not allowed to use.
        """
        self.fail_sign_in(times=THRESHOLD)
        token = self._reset_token(self.okafor)

        PasswordService.confirm_reset(token=token, new_password=NEW_PW)

        lockout = self.lockout_of(self.okafor)
        self.assertFalse(lockout.is_locked_now())
        self.assertEqual(lockout.failure_count, 0)
        self.assertIn("access", self.sign_in(password=NEW_PW))


class TheMigrationFreesTheAccountsAlreadyStuckTests(_Fixture):
    """Migration 0012, run against the rows the old code left behind.

    The data step is called directly rather than through a rebuilt migration
    graph. What is under test is which account becomes what, and the real model
    answers that exactly as the historical one does - it reads two columns and
    writes one.
    """

    def _run_the_migration(self):
        from django.apps import apps as registry

        module = import_module("vs_user.migrations.0012_a_lockout_is_not_a_status")
        module.clear_the_locked_status(registry, None)

    def _make_it_locked(self, user, *, is_active, minutes_left=-60):
        """Put a row into the state the old code left behind.

        Written round ``save()`` on purpose. ``_sync_is_active`` would derive
        the flag from the status now, and these rows exist precisely because it
        used to leave it alone.
        """
        User.objects.filter(pk=user.pk).update(
            status=User.Status.LOCKED, is_active=is_active,
        )
        AccountLockout.objects.create(
            user=user, failure_count=5,
            locked_until=timezone.now() + timedelta(minutes=minutes_left),
        )
        return User.objects.get(pk=user.pk)

    def _stuck(self, email, *, is_active, minutes_left=-60):
        """Somebody who already had a password when they were locked."""
        return self._make_it_locked(
            self.person(email), is_active=is_active, minutes_left=minutes_left,
        )

    def _stuck_invited(self, email):
        """An invited teacher who never activated, locked and left that way.

        Built through the real invitation service, because the row that service
        writes is exactly what the migration reads.
        """
        user = self.person(email, status=User.Status.PENDING, password=None)
        _invitation, token = InvitationService.create(
            user=user, invited_by=self.administrator(),
        )
        return self._make_it_locked(user, is_active=False), token

    def _stuck_never_invited(self, email):
        """A hire refused before any invitation was sent, then guessed at."""
        user = self.person(email, status=User.Status.REJECTED, password=None)
        return self._make_it_locked(user, is_active=False)

    def test_an_account_whose_window_has_passed_comes_back(self):
        stuck = self._stuck("stuck@brightfield.test", is_active=True)
        self.assertEqual(self.refusal_code(stuck), "ACCOUNT_LOCKED")

        self._run_the_migration()

        fresh = User.objects.get(pk=stuck.pk)
        self.assertEqual(fresh.status, User.Status.ACTIVE)
        self.assertIn("access", self.sign_in(fresh))

    def test_an_account_still_inside_its_window_stays_refused(self):
        """Moved like the rest, and still locked - by the row that says so."""
        inside = self._stuck(
            "inside@brightfield.test", is_active=True, minutes_left=10,
        )

        self._run_the_migration()

        fresh = User.objects.get(pk=inside.pk)
        self.assertEqual(fresh.status, User.Status.ACTIVE)
        self.assertEqual(self.refusal_code(fresh), "ACCOUNT_LOCKED")

    def test_an_account_that_could_not_sign_in_before_is_left_for_a_person(self):
        """A password was set here once, so this is not an invited account.

        Which it was - suspended, deactivated, or something else that could not
        sign in - is unrecoverable once LOCKED has been written over it, so
        nothing is handed back. SUSPENDED is the state that means an
        administrator has to decide, and deciding takes one action; guessing
        ACTIVE would give a working sign-in back to somebody who was closed out.
        """
        unclear = self._stuck("unclear@brightfield.test", is_active=False)

        self._run_the_migration()

        fresh = User.objects.get(pk=unclear.pk)
        self.assertEqual(fresh.status, User.Status.SUSPENDED)
        self.assertFalse(fresh.is_active)
        self.assertEqual(self.refusal_code(fresh), "ACCOUNT_SUSPENDED")

    def test_an_invited_teacher_gets_her_invitation_back(self):
        """Four marks together say invited and never used, so PENDING comes back.

        Funke is invited to Brightfield on the Monday. On the Tuesday she goes
        to the sign-in page instead of using her link, guesses at a password
        five times, and is locked. She has an unused invitation, has never
        signed in, and has never had a password set. Sent to SUSPENDED she would
        need an administrator before she could use the link her school had
        already emailed her; restored to PENDING the link works, which is what
        it was sent for.

        The one shape this cannot tell from hers is an account deactivated after
        it was invited and before it activated. That comes back invited too, and
        its link works for whatever is left of its expiry. The migration's
        docstring says so: the token still has to be in somebody's inbox, the
        expiry still applies, and an administrator can withdraw the invitation.
        """
        funke, token = self._stuck_invited("funke@brightfield.test")
        # What being stuck costs her: activation refuses anything that is not
        # PENDING, so the link in her inbox is dead. She is never shown the
        # lockout refusal itself - that is given only to somebody who proves
        # they know the password, and she has never had one.
        self.assertFalse(funke.may_sign_in)
        with self.assertRaises(ValueError) as caught:
            InvitationService.activate(token=token, password=NEW_PW)
        self.assertEqual(
            caught.exception.args[0]["error_code"], "INVITATION_NOT_ACTIONABLE",
        )

        self._run_the_migration()

        fresh = User.objects.get(pk=funke.pk)
        self.assertEqual(fresh.status, User.Status.PENDING)
        self.assertFalse(fresh.is_active)

        # The proof that "invited" was restored and not merely written down.
        InvitationService.activate(token=token, password=NEW_PW)
        self.assertEqual(
            User.objects.get(pk=funke.pk).status, User.Status.ACTIVE,
        )

    def test_a_hire_who_was_never_invited_does_not_come_back_invited(self):
        """The branch that must not reach a rejected hire.

        Emeka failed his reference check and his account was refused before any
        invitation was sent, so there is no invitation row - which is what stops
        the rule above touching him. Restored to PENDING he would be one
        activation link away from being a finance officer at Bright Star, which
        is the defect an earlier change closed and this one must not reopen.
        """
        emeka = self._stuck_never_invited("refused@brightfield.test")

        self._run_the_migration()

        fresh = User.objects.get(pk=emeka.pk)
        self.assertNotEqual(fresh.status, User.Status.PENDING)
        self.assertEqual(fresh.status, User.Status.SUSPENDED)
        self.assertFalse(fresh.may_sign_in)

    def test_a_genuinely_suspended_account_is_not_touched(self):
        emeka = self.person("emeka@brightfield.test", status=User.Status.SUSPENDED)

        self._run_the_migration()

        fresh = User.objects.get(pk=emeka.pk)
        self.assertEqual(fresh.status, User.Status.SUSPENDED)
        self.assertFalse(fresh.is_active)

    def test_nothing_is_left_carrying_the_status(self):
        self._stuck("one@brightfield.test", is_active=True)
        self._stuck("two@brightfield.test", is_active=False)

        self._run_the_migration()

        self.assertFalse(
            User.objects.filter(status=User.Status.LOCKED).exists(),
        )

    def test_the_reverse_is_a_no_op_and_says_why(self):
        """Named here so that removing the reason removes a test with it."""
        from django.db import migrations

        module = import_module("vs_user.migrations.0012_a_lockout_is_not_a_status")
        reverse = module.Migration.operations[0].reverse_code
        self.assertIs(reverse, migrations.RunPython.noop)
        self.assertIn("no reverse", module.__doc__)
