"""Regression coverage for request-bound account-action credentials."""

from __future__ import annotations

import threading
from datetime import timedelta
from unittest import mock

from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
from django.utils import timezone

from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin
from vs_user.action_tokens import (
    invitation_token_digest,
    password_reset_token_digest,
)
from vs_user.models import PasswordResetRequest, User
from vs_user.services.invitation import InvitationService
from vs_user.services.password import PasswordService


NEW_PASSWORD = "An0ther!pass99"


class ActionTokenIsolationTests(TestCase):
    """Every emailed token authorizes one row in one credential family."""

    def setUp(self):
        self.school = make_school(slug="token-school", name="Token School")
        self.branch = make_branch(self.school, name="Main", is_main=True)
        self.user = make_school_admin(
            self.branch,
            email="ada@token-school.test",
            password="Str0ng!pass123",
        )

    def _request_reset(self):
        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                PasswordService.request_reset(
                    email=self.user.email,
                    tenant=self.school.tenant.slug,
                )
        token = delay.call_args.kwargs["token"]
        request_id = delay.call_args.kwargs["reset_request_id"]
        return PasswordResetRequest.objects.get(pk=request_id), token

    def test_old_link_does_not_revive_when_a_new_reset_is_requested(self):
        monday_request, monday_token = self._request_reset()
        friday_request, friday_token = self._request_reset()

        monday_request.refresh_from_db()
        self.assertIsNotNone(monday_request.used_at)
        self.assertIsNone(friday_request.used_at)
        self.assertNotEqual(monday_token, friday_token)

        self.assertIsNone(PasswordService.valid_reset_for_token(monday_token))
        with self.assertRaises(ValueError) as caught:
            PasswordService.confirm_reset(
                token=monday_token,
                new_password=NEW_PASSWORD,
            )
        self.assertEqual(caught.exception.args[0]["error_code"], "RESET_KEY_INVALID")

        PasswordService.confirm_reset(
            token=friday_token,
            new_password=NEW_PASSWORD,
        )
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(NEW_PASSWORD))

    def test_database_stores_the_reset_digest_not_the_raw_token(self):
        reset_request, token = self._request_reset()

        self.assertEqual(
            reset_request.token_hash,
            password_reset_token_digest(token),
        )
        self.assertNotEqual(reset_request.token_hash, token)

    def test_a_consumed_reset_token_cannot_be_replayed(self):
        reset_request, token = self._request_reset()
        PasswordService.confirm_reset(token=token, new_password=NEW_PASSWORD)

        with self.assertRaises(ValueError) as caught:
            PasswordService.confirm_reset(
                token=token,
                new_password="Y3tAnother!pass",
            )

        self.assertEqual(caught.exception.args[0]["error_code"], "RESET_KEY_INVALID")
        reset_request.refresh_from_db()
        self.assertIsNotNone(reset_request.used_at)

    def test_invitation_and_reset_tokens_cannot_cross_endpoints(self):
        pending = User.objects.create_user(
            email="pending@token-school.test",
            password=None,
            first_name="Ngozi",
            last_name="Okafor",
            tenant=self.school.tenant,
            branch=self.branch,
            status=User.Status.PENDING,
            is_active=False,
        )
        invitation, invitation_token = InvitationService.create(
            user=pending,
            invited_by=self.user,
        )
        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                PasswordService.request_reset(
                    email=pending.email,
                    tenant=self.school.tenant.slug,
                )
        reset_token = delay.call_args.kwargs["token"]

        self.assertEqual(
            invitation.token_hash,
            invitation_token_digest(invitation_token),
        )
        with self.assertRaises(ValueError) as reset_error:
            PasswordService.confirm_reset(
                token=invitation_token,
                new_password=NEW_PASSWORD,
            )
        self.assertEqual(
            reset_error.exception.args[0]["error_code"],
            "RESET_KEY_INVALID",
        )

        with self.assertRaises(ValueError) as invitation_error:
            InvitationService.activate(
                token=reset_token,
                password=NEW_PASSWORD,
            )
        self.assertEqual(
            invitation_error.exception.args[0]["error_code"],
            "INVITATION_NOT_FOUND",
        )
        self.assertEqual(User.objects.get(pk=pending.pk).status, User.Status.PENDING)

    def test_resending_an_invitation_rotates_and_kills_the_old_url(self):
        pending = User.objects.create_user(
            email="resend@token-school.test",
            password=None,
            first_name="Bola",
            last_name="Adeniyi",
            tenant=self.school.tenant,
            branch=self.branch,
            status=User.Status.PENDING,
            is_active=False,
        )
        invitation, old_token = InvitationService.create(
            user=pending,
            invited_by=self.user,
        )

        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                InvitationService.resend(
                    user=pending,
                    requested_by=self.user,
                )
        new_token = delay.call_args.kwargs["token"]

        invitation.refresh_from_db()
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(
            invitation.token_hash,
            invitation_token_digest(new_token),
        )
        with self.assertRaises(ValueError):
            InvitationService.get_valid_invitation(old_token)
        self.assertEqual(
            InvitationService.get_valid_invitation(new_token).pk,
            invitation.pk,
        )


class PasswordResetConsumptionRaceTests(TransactionTestCase):
    """Two simultaneous submissions may consume one reset request only once."""

    serialized_rollback = True

    def test_only_one_concurrent_confirmation_can_set_the_password(self):
        school = make_school(slug="reset-race", name="Reset Race")
        branch = make_branch(school, name="Main", is_main=True)
        user = make_school_admin(
            branch,
            email="race@reset.test",
            password="Str0ng!pass123",
        )
        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            PasswordService.request_reset(
                email=user.email,
                tenant=school.tenant.slug,
            )
        token = delay.call_args.kwargs["token"]

        first_validating = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_done = threading.Event()
        validation_calls = 0
        validation_guard = threading.Lock()
        outcomes = {}

        def gated_validation(*args, **kwargs):
            nonlocal validation_calls
            with validation_guard:
                validation_calls += 1
                call_number = validation_calls
            if call_number == 1:
                first_validating.set()
                if not release_first.wait(5):
                    raise TimeoutError("reset race did not release the first caller")

        def worker(name, password, *, started=None, done=None):
            close_old_connections()
            try:
                if started is not None:
                    started.set()
                PasswordService.confirm_reset(
                    token=token,
                    new_password=password,
                )
                outcomes[name] = "success"
            except Exception as exc:
                outcomes[f"{name}_error"] = exc
            finally:
                close_old_connections()
                if done is not None:
                    done.set()

        first = threading.Thread(
            target=worker,
            args=("first", "F1rstWinner!99"),
            daemon=True,
        )
        second = threading.Thread(
            target=worker,
            args=("second", "S3condWinner!99"),
            kwargs={"started": second_started, "done": second_done},
            daemon=True,
        )

        with mock.patch(
            "vs_user.services.password.validate_password",
            side_effect=gated_validation,
        ):
            first.start()
            try:
                self.assertTrue(first_validating.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second_done.wait(0.25))
            finally:
                release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(outcomes.get("first"), "success")
        self.assertNotIn("first_error", outcomes)
        self.assertNotIn("second", outcomes)
        self.assertIsInstance(outcomes.get("second_error"), ValueError)
        self.assertEqual(
            outcomes["second_error"].args[0]["error_code"],
            "RESET_KEY_INVALID",
        )
        self.assertEqual(validation_calls, 1)
        self.assertTrue(
            User.objects.get(pk=user.pk).check_password("F1rstWinner!99"),
        )


@tag("slow")
class ActionTokenMigrationTests(TransactionTestCase):
    """Deployment preserves invitations and closes unbound legacy resets."""

    serialized_rollback = True

    APP = "vs_user"
    BEFORE = "0009_drop_user_type"
    AFTER = "0010_request_bound_action_tokens"

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(self.APP, target)])
        executor.loader.build_graph()
        return executor

    def setUp(self):
        executor = self._migrate(self.BEFORE)
        historical = executor.loader.project_state((self.APP, self.BEFORE)).apps
        HistoricalUser = historical.get_model("vs_user", "User")
        HistoricalInvitation = historical.get_model("vs_user", "UserInvitation")
        HistoricalReset = historical.get_model("vs_user", "PasswordResetRequest")
        HistoricalTenant = historical.get_model("vs_tenants", "Tenant")

        tenant = HistoricalTenant.objects.get(slug="codex")
        user = HistoricalUser.objects.create(
            email="migration-token@codex.test",
            first_name="Ada",
            last_name="Okoye",
            status="PENDING",
            tenant=tenant,
            is_active=False,
            is_staff=False,
            is_superuser=False,
        )
        self.user_id = user.pk
        self.legacy_invitation_token = str(user.activation_key)
        self.invitation_id = HistoricalInvitation.objects.create(
            user=user,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
            is_used=False,
        ).pk
        self.reset_id = HistoricalReset.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=1),
            used_at=None,
            requested_by="SELF",
        ).pk

        self._migrate(self.AFTER)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        executor.loader.build_graph()
        super().tearDown()

    def test_forward_migration_preserves_only_the_invitation_credential(self):
        from vs_user.models import UserInvitation

        invitation = UserInvitation.objects.get(pk=self.invitation_id)
        reset = PasswordResetRequest.objects.get(pk=self.reset_id)

        self.assertEqual(
            invitation.token_hash,
            invitation_token_digest(self.legacy_invitation_token),
        )
        self.assertEqual(
            InvitationService.get_valid_invitation(
                self.legacy_invitation_token,
            ).pk,
            invitation.pk,
        )
        self.assertIsNotNone(reset.used_at)
        self.assertIsNone(
            PasswordService.valid_reset_for_token(
                self.legacy_invitation_token,
            ),
        )
        self.assertNotIn("activation_key", {field.name for field in User._meta.fields})
