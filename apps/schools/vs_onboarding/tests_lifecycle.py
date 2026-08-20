"""Abandoned onboarding, reinstatement, and the retention of go-live history.

Three product decisions are proved here, and one test matters more than the
rest: :meth:`ExpirySweepTests.test_a_reinstated_school_is_not_expired_again`.
Everything else in the expiry work is arithmetic; that test is the one that
fails if the sweep ever goes back to measuring from ``Tenant.created_at``,
which is the mistake this design exists to prevent.

Two habits carried over from ``tests.py``. Audit is asserted by reading the row
back, never by "nothing raised" - ``emit_audit_event`` swallows an unregistered
action type in silence. And every tenancy claim uses more than one school, so
the row that must not be touched exists for its stillness to mean anything.
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from schools.vs_schools.models import School, SchoolStatus
from vs_audit.models import AuditActionType, AuditEvent, AuditModuleKey
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from .constants import (
    GO_LIVE_HISTORY_RETENTION_DAYS,
    GoLiveStatus,
    ONBOARDING_EXPIRY_DAYS,
    ONBOARDING_EXPIRY_WARNING_DAYS,
    PERM_GO_LIVE_APPROVE,
    PERM_PROGRESS_REACTIVATE,
    PERM_PROGRESS_VIEW,
    PERM_TASK_UPDATE,
    ReadinessState,
    STALE_ONBOARDING_AFTER_DAYS,
    TaskKey,
    TaskStatus,
)
from .models import GoLiveRequest, OnboardingProgress
from .services.lifecycle import (
    expire_stale_onboarding,
    reinstate_school,
    run_sweep,
    stale_onboarding_report,
    warn_expiring_onboarding,
)
from .services.retention import purge_go_live_history
from .tests import grant_platform, grant_school_admin


def days_ago(n):
    return timezone.now() - timezone.timedelta(days=n)


def pending_school(slug, name, *, pending_days=0, **kwargs):
    """A PENDING school whose tenant entered PENDING ``pending_days`` ago."""
    school = make_school(slug=slug, name=name, status="PENDING", **kwargs)
    Tenant.objects.filter(pk=school.tenant_id).update(
        pending_since=days_ago(pending_days),
        created_at=days_ago(pending_days),
    )
    school.tenant.refresh_from_db()
    return school


def statuses(school):
    """The pair that must never disagree: (school status, tenant status)."""
    school.refresh_from_db()
    tenant = Tenant.objects.get(pk=school.tenant_id)
    return school.status, tenant.status


# ══════════════════════════════════════════════════════════════════════════
# The pending clock
# ══════════════════════════════════════════════════════════════════════════

class PendingSinceTests(TestCase):
    """``Tenant.pending_since`` describes the spell the tenant is in now."""

    def test_a_new_school_is_pending_from_creation(self):
        school = make_school(slug="fresh-pending", name="Fresh", status="PENDING")

        self.assertIsNotNone(school.tenant.pending_since)

    def test_an_ordinary_edit_does_not_restart_the_clock(self):
        """The difference between "became pending" and "was already pending"."""
        school = pending_school("editable", "Editable", pending_days=40)
        original = Tenant.objects.get(pk=school.tenant_id).pending_since

        school.name = "Editable, Renamed"
        school.save()

        self.assertEqual(
            Tenant.objects.get(pk=school.tenant_id).pending_since, original,
        )

    def test_going_live_clears_it(self):
        school = pending_school("goes-live", "Goes Live", pending_days=5)

        school.status = SchoolStatus.ACTIVE
        school.activated_at = timezone.now()
        school.save()

        tenant = Tenant.objects.get(pk=school.tenant_id)
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertIsNone(tenant.pending_since)


# ══════════════════════════════════════════════════════════════════════════
# FR-013a  The 90-day expiry sweep
# ══════════════════════════════════════════════════════════════════════════

class ExpirySweepTests(TestCase):
    def setUp(self):
        self.stale = pending_school(
            "stale-school", "Stale School", pending_days=ONBOARDING_EXPIRY_DAYS + 1,
        )
        self.young = pending_school(
            "young-school", "Young School", pending_days=ONBOARDING_EXPIRY_DAYS - 1,
        )

    def sweep(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return expire_stale_onboarding(**kwargs)

    def test_a_ninety_day_old_pending_school_is_suspended(self):
        result = self.sweep()

        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            statuses(self.stale), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )
        self.assertIsNotNone(School.objects.get(pk=self.stale.pk).deactivated_at)

    def test_a_younger_school_is_left_alone(self):
        self.sweep()

        self.assertEqual(
            statuses(self.young), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )
        self.assertIsNotNone(
            Tenant.objects.get(pk=self.young.tenant_id).pending_since,
        )

    def test_an_active_school_is_never_swept(self):
        """A live school is old too; only PENDING is a candidate."""
        live = make_school(slug="live-school", name="Live", status="ACTIVE")
        Tenant.objects.filter(pk=live.tenant_id).update(created_at=days_ago(400))

        self.sweep()

        self.assertEqual(
            statuses(live), (SchoolStatus.ACTIVE, Tenant.Status.ACTIVE),
        )

    def test_the_school_and_its_tenant_never_disagree(self):
        """Written through School.save(), so the mirror is what moves the tenant.

        The follow-up edit is the part that matters: a tenant suspended beside
        its school would be silently returned to PENDING by the next ordinary
        school save.
        """
        self.sweep()

        school = School.objects.get(pk=self.stale.pk)
        school.motto = "Still suspended."
        school.save()

        self.assertEqual(
            statuses(school), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )

    def test_the_sweep_is_idempotent(self):
        first = self.sweep()
        second = self.sweep()

        self.assertEqual(first["expired_count"], 1)
        self.assertEqual(second["expired_count"], 0)

    def test_dry_run_writes_nothing(self):
        result = self.sweep(dry_run=True)

        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            statuses(self.stale), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )

    def test_each_expiry_writes_one_audit_row(self):
        AuditEvent.objects.all().delete()

        self.sweep()

        rows = AuditEvent.objects.filter(
            module_key=AuditModuleKey.ONBOARDING,
            action_type=AuditActionType.ONBOARDING_EXPIRED,
        )
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertEqual(row.tenant_id, self.stale.tenant_id)
        self.assertEqual(row.entity_type, "Tenant")
        self.assertEqual(row.severity, "WARNING")

    def test_a_school_kind_tenant_with_no_school_profile_is_still_expired(self):
        """Written on the tenant, because there is no school to write through."""
        bare = Tenant.objects.create(
            name="Bare", slug="bare-tenant", kind=Tenant.Kind.SCHOOL,
            status=Tenant.Status.PENDING, pending_since=days_ago(200),
        )

        result = self.sweep()

        bare.refresh_from_db()
        self.assertEqual(bare.status, Tenant.Status.SUSPENDED)
        self.assertIsNone(bare.pending_since)
        self.assertEqual(result["without_school_profile"], 1)

    def test_a_platform_or_organization_tenant_is_out_of_scope(self):
        other = Tenant.objects.create(
            name="Clinic", slug="clinic-tenant", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.PENDING, pending_since=days_ago(400),
        )

        self.sweep()

        other.refresh_from_db()
        self.assertEqual(other.status, Tenant.Status.PENDING)

    # ── the one that proves the timestamp ──────────────────────────────────

    def test_a_reinstated_school_is_not_expired_again(self):
        """The whole point of ``pending_since``.

        The school is old: it was created long before the window and its
        creation date says so for ever. Reinstating it starts a new pending
        spell, and the very next sweep must leave it alone. A sweep measuring
        from ``created_at`` re-suspends it here, which is the failure this
        column exists to make impossible.
        """
        self.sweep()
        self.assertEqual(statuses(self.stale)[1], Tenant.Status.SUSPENDED)

        tenant = Tenant.objects.get(pk=self.stale.tenant_id)
        with self.captureOnCommitCallbacks(execute=True):
            reinstate_school(tenant)

        self.assertEqual(
            statuses(self.stale), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )
        # Still an old row by every other measure.
        self.assertLess(
            Tenant.objects.get(pk=self.stale.tenant_id).created_at,
            days_ago(ONBOARDING_EXPIRY_DAYS),
        )

        again = self.sweep()

        self.assertEqual(again["expired_count"], 0)
        self.assertEqual(
            statuses(self.stale), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )


class ExpirySweepCommandTests(TestCase):
    def setUp(self):
        self.stale = pending_school(
            "command-stale", "Command Stale",
            pending_days=ONBOARDING_EXPIRY_DAYS + 5,
        )

    def _run(self, *args):
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command(
                "expire_stale_onboarding", *args, stdout=out, stderr=StringIO(),
            )
        return out.getvalue()

    def test_the_command_suspends_and_reports(self):
        output = self._run()

        self.assertIn("command-stale", output)
        self.assertIn("suspended 1 school(s)", output)
        self.assertEqual(statuses(self.stale)[1], Tenant.Status.SUSPENDED)

    def test_dry_run_reports_and_writes_nothing(self):
        output = self._run("--dry-run")

        self.assertIn("would suspend 1 school(s)", output)
        self.assertEqual(statuses(self.stale)[1], Tenant.Status.PENDING)


# ══════════════════════════════════════════════════════════════════════════
# The 14-day warning
# ══════════════════════════════════════════════════════════════════════════

WARN_AT_DAYS = ONBOARDING_EXPIRY_DAYS - ONBOARDING_EXPIRY_WARNING_DAYS


class ExpiryWarningTests(TestCase):
    """Once per pending spell, and never a precondition for expiry."""

    def setUp(self):
        self.school = pending_school(
            "warned-school", "Warned School", pending_days=WARN_AT_DAYS,
        )
        self.young = pending_school(
            "young-warn", "Young Warn", pending_days=WARN_AT_DAYS - 10,
        )
        self.admin = make_school_admin(
            None, email="warned-admin@test.com", tenant=self.school.tenant,
        )
        grant_school_admin(
            self.school.tenant, self.admin, PERM_PROGRESS_VIEW, PERM_TASK_UPDATE,
        )

    def warnings_for(self, send, school=None):
        """The warning calls about one school, by slug.

        Filtered rather than counted wholesale: the fixture holds a second
        school that ages into the window part way through the daily loop, and
        its own single warning is correct.
        """
        slug = (school or self.school).slug
        return [
            call for call in send.call_args_list
            if call.args[0] == "onboarding.expiry_warning"
            and call.args[1]["school_slug"] == slug
        ]

    def sweep(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return run_sweep(**kwargs)

    def warned_at(self, school=None):
        school = school or self.school
        return Tenant.objects.get(pk=school.tenant_id).expiry_warned_at

    # ── the trap ──────────────────────────────────────────────────────────

    def test_a_daily_sweep_warns_once_not_once_a_day(self):
        """Fourteen consecutive daily runs, one warning.

        "Is this school within fourteen days of expiry?" is true on every one
        of those days, so a sweep without a recorded stamp mails the school
        fourteen times. This is the test that fails if the stamp goes.
        """
        base = timezone.now()

        with patch("schools.vs_onboarding.services.effects._send") as send:
            for day in range(ONBOARDING_EXPIRY_WARNING_DAYS):
                self.sweep(now=base + timezone.timedelta(days=day))

        self.assertEqual(len(self.warnings_for(send)), 1)
        # And the school that aged into the window on the way through was
        # warned exactly once too, not once a day from the moment it arrived.
        self.assertEqual(len(self.warnings_for(send, self.young)), 1)
        self.assertIsNotNone(self.warned_at())
        self.assertIsNotNone(self.warned_at(self.young))

    def test_a_school_short_of_the_window_is_not_warned(self):
        with patch("schools.vs_onboarding.services.effects._send") as send:
            result = self.sweep()

        self.assertEqual(
            {row["slug"] for row in result["warned"]}, {self.school.tenant.slug},
        )
        self.assertIsNone(self.warned_at(self.young))
        self.assertEqual(len(send.call_args_list), 1)

    def test_a_reinstated_school_is_warned_again_in_its_new_cycle(self):
        """The stamp belongs to the spell, not to the school."""
        with patch("schools.vs_onboarding.services.effects._send"):
            self.sweep()
        self.assertIsNotNone(self.warned_at())

        # Run it out to expiry, then put it back.
        Tenant.objects.filter(pk=self.school.tenant_id).update(
            pending_since=days_ago(ONBOARDING_EXPIRY_DAYS + 1),
        )
        with self.captureOnCommitCallbacks(execute=True):
            expire_stale_onboarding()
            reinstate_school(Tenant.objects.get(pk=self.school.tenant_id))

        self.assertIsNone(self.warned_at(), "reinstatement must clear the stamp")

        # A new spell, aged into the window again.
        Tenant.objects.filter(pk=self.school.tenant_id).update(
            pending_since=days_ago(WARN_AT_DAYS),
        )
        with patch("schools.vs_onboarding.services.effects._send") as send:
            self.sweep()

        self.assertEqual(len(self.warnings_for(send)), 1)
        self.assertIsNotNone(self.warned_at())

    # ── the warning is never a gate on expiry ─────────────────────────────

    def test_a_school_past_the_deadline_is_expired_even_though_it_was_warned(self):
        with patch("schools.vs_onboarding.services.effects._send"):
            self.sweep()

        Tenant.objects.filter(pk=self.school.tenant_id).update(
            pending_since=days_ago(ONBOARDING_EXPIRY_DAYS + 1),
        )
        with patch("schools.vs_onboarding.services.effects._send"):
            result = self.sweep()

        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            statuses(self.school), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )

    def test_a_school_that_was_never_warned_still_expires_on_time(self):
        """No sweep ever ran during its window: it expires all the same."""
        never = pending_school(
            "never-warned", "Never Warned",
            pending_days=ONBOARDING_EXPIRY_DAYS + 3,
        )

        with patch("schools.vs_onboarding.services.effects._send"):
            result = self.sweep()

        self.assertIsNone(self.warned_at(never))
        self.assertIn(
            never.tenant.slug, {row["slug"] for row in result["expired"]},
        )
        self.assertEqual(
            statuses(never), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )

    def test_a_school_past_the_deadline_is_expired_rather_than_warned(self):
        """Order of the two steps: nobody is warned about a date already passed."""
        overdue = pending_school(
            "overdue-school", "Overdue School",
            pending_days=ONBOARDING_EXPIRY_DAYS + 5,
        )

        with patch("schools.vs_onboarding.services.effects._send"):
            result = self.sweep()

        self.assertNotIn(
            overdue.tenant.slug, {row["slug"] for row in result["warned"]},
        )
        self.assertIsNone(self.warned_at(overdue))

    def test_suspension_clears_the_warning_stamp(self):
        with patch("schools.vs_onboarding.services.effects._send"):
            self.sweep()
        Tenant.objects.filter(pk=self.school.tenant_id).update(
            pending_since=days_ago(ONBOARDING_EXPIRY_DAYS + 1),
        )

        with self.captureOnCommitCallbacks(execute=True):
            expire_stale_onboarding()

        self.assertIsNone(self.warned_at())

    # ── who hears it, and what it says ────────────────────────────────────

    def test_the_warning_goes_to_the_school_and_carries_the_date(self):
        with patch("schools.vs_onboarding.services.effects._send") as send:
            self.sweep()

        event, context, recipients, tenant = send.call_args.args
        self.assertEqual(event, "onboarding.expiry_warning")
        self.assertEqual({user.pk for user in recipients}, {self.admin.pk})
        self.assertEqual(tenant.pk, self.school.tenant_id)
        self.assertEqual(context["school_name"], "Warned School")
        self.assertEqual(context["days_remaining"], ONBOARDING_EXPIRY_WARNING_DAYS)
        self.assertTrue(context["expires_on"])

    def test_dry_run_neither_stamps_nor_sends(self):
        with patch("schools.vs_onboarding.services.effects._send") as send:
            result = warn_expiring_onboarding(dry_run=True)

        self.assertEqual(result["warned_count"], 1)
        send.assert_not_called()
        self.assertIsNone(self.warned_at())

    # ── the owner's other decision ────────────────────────────────────────

    def test_activity_does_not_reset_the_clock(self):
        """Completing a step on day 80 does not buy the school more time.

        A product decision, and one a future reader is likely to assume the
        other way round, so it is pinned rather than left implied.
        """
        from .services.provisioning import provision_onboarding
        from .services.tasks import transition_task

        with self.captureOnCommitCallbacks(execute=True):
            provision_onboarding(self.school.tenant)
        before = Tenant.objects.get(pk=self.school.tenant_id).pending_since

        with self.captureOnCommitCallbacks(execute=True):
            transition_task(
                self.school.tenant, TaskKey.ACADEMIC_STRUCTURE, TaskStatus.DONE,
                actor=self.admin,
            )

        self.assertEqual(
            Tenant.objects.get(pk=self.school.tenant_id).pending_since, before,
        )

        Tenant.objects.filter(pk=self.school.tenant_id).update(
            pending_since=days_ago(ONBOARDING_EXPIRY_DAYS + 1),
        )
        with patch("schools.vs_onboarding.services.effects._send"):
            result = self.sweep()

        self.assertEqual(result["expired_count"], 1)


class ExpiryWarningTemplateTests(TestCase):
    """The seeded templates render the context the warning actually sends."""

    def setUp(self):
        from vs_notifications.services.seed import (
            seed_notification_templates, seed_platform_settings,
        )

        self.school = pending_school(
            "template-warned", "Template Warned", pending_days=WARN_AT_DAYS + 2,
        )
        self.admin = make_school_admin(
            None, email="template-warned-admin@test.com", tenant=self.school.tenant,
        )
        grant_school_admin(self.school.tenant, self.admin, PERM_PROGRESS_VIEW)
        seed_notification_templates()
        seed_platform_settings()

    def test_the_warning_renders_without_leaving_a_placeholder(self):
        from vs_notifications.models import Notification

        warn_expiring_onboarding()

        rows = Notification.objects.filter(
            event_type__key="onboarding.expiry_warning",
        )
        self.assertTrue(rows.exists())
        body = rows.first().body
        self.assertNotIn("{{", body)
        self.assertIn("Template Warned", body)


# ══════════════════════════════════════════════════════════════════════════
# The expiry window on the control room payload
# ══════════════════════════════════════════════════════════════════════════

class StateExpiryPayloadTests(TestCase):
    """The dates the countdown renders, and the one authority behind them."""

    def setUp(self):
        from .services.provisioning import provision_onboarding

        self.school = pending_school(
            "countdown-school", "Countdown School", pending_days=WARN_AT_DAYS,
        )
        self.admin = make_school_admin(
            None, email="countdown-admin@test.com", tenant=self.school.tenant,
        )
        grant_school_admin(self.school.tenant, self.admin, PERM_PROGRESS_VIEW)
        with self.captureOnCommitCallbacks(execute=True):
            provision_onboarding(self.school.tenant)

    def state(self, school=None, user=None):
        school = school or self.school
        token = str(CodeXRefreshToken.for_user(user or self.admin).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(
            f"{reverse('onboarding-state')}?tenant={school.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["expiry"]

    def test_a_pending_school_gets_its_window_and_its_countdown(self):
        expiry = self.state()

        tenant = Tenant.objects.get(pk=self.school.tenant_id)
        self.assertTrue(expiry["applies"])
        self.assertEqual(expiry["pending_since"], tenant.pending_since)
        self.assertEqual(
            expiry["expires_at"],
            tenant.pending_since + timezone.timedelta(days=ONBOARDING_EXPIRY_DAYS),
        )
        self.assertEqual(expiry["days_remaining"], ONBOARDING_EXPIRY_WARNING_DAYS)
        self.assertEqual(expiry["expiry_days"], ONBOARDING_EXPIRY_DAYS)
        self.assertEqual(expiry["warning_days"], ONBOARDING_EXPIRY_WARNING_DAYS)
        self.assertFalse(expiry["warning_sent"])
        self.assertIsNone(expiry["warning_sent_at"])

    def test_a_warned_school_shows_the_warning_as_sent(self):
        with patch("schools.vs_onboarding.services.effects._send"):
            warn_expiring_onboarding()

        expiry = self.state()

        self.assertTrue(expiry["warning_sent"])
        self.assertEqual(
            expiry["warning_sent_at"],
            Tenant.objects.get(pk=self.school.tenant_id).expiry_warned_at,
        )

    def test_the_endpoint_and_the_warning_name_the_same_date(self):
        """The point of sharing the authority, asserted rather than assumed."""
        with patch("schools.vs_onboarding.services.effects._send") as send:
            warn_expiring_onboarding()

        context = send.call_args.args[1]
        expiry = self.state()

        self.assertEqual(
            context["expires_on"],
            timezone.localtime(expiry["expires_at"]).date().isoformat(),
        )
        self.assertEqual(context["days_remaining"], expiry["days_remaining"])

    def test_a_live_school_has_no_expiry_rather_than_a_stale_one(self):
        """Going live must not leave a countdown behind on the screen."""
        from .services.provisioning import provision_onboarding

        live = make_school(slug="live-countdown", name="Live Countdown", status="ACTIVE")
        admin = make_school_admin(
            None, email="live-countdown-admin@test.com", tenant=live.tenant,
        )
        grant_school_admin(live.tenant, admin, PERM_PROGRESS_VIEW)
        with self.captureOnCommitCallbacks(execute=True):
            provision_onboarding(live.tenant)

        expiry = self.state(school=live, user=admin)

        self.assertFalse(expiry["applies"])
        self.assertIsNone(expiry["pending_since"])
        self.assertIsNone(expiry["expires_at"])
        self.assertIsNone(expiry["days_remaining"])
        self.assertFalse(expiry["warning_sent"])

    def test_a_school_that_goes_live_loses_its_countdown(self):
        """The same school, before and after: no leftover date survives."""
        self.assertTrue(self.state()["applies"])

        school = School.objects.get(pk=self.school.pk)
        school.status = SchoolStatus.ACTIVE
        school.activated_at = timezone.now()
        school.save()

        expiry = self.state()
        self.assertFalse(expiry["applies"])
        self.assertIsNone(expiry["expires_at"])


# ══════════════════════════════════════════════════════════════════════════
# FR-013c  Reinstatement
# ══════════════════════════════════════════════════════════════════════════

class ReinstateServiceTests(TestCase):
    def setUp(self):
        self.school = pending_school(
            "suspended-school", "Suspended School",
            pending_days=ONBOARDING_EXPIRY_DAYS + 2,
        )
        with self.captureOnCommitCallbacks(execute=True):
            expire_stale_onboarding()
        self.tenant = Tenant.objects.get(pk=self.school.tenant_id)

    def test_it_returns_the_school_to_pending_with_a_fresh_window(self):
        before = timezone.now()

        with self.captureOnCommitCallbacks(execute=True):
            tenant = reinstate_school(self.tenant)

        self.assertEqual(
            statuses(self.school), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )
        self.assertGreaterEqual(tenant.pending_since, before)
        self.assertIsNone(School.objects.get(pk=self.school.pk).deactivated_at)

    def test_it_writes_one_audit_row(self):
        AuditEvent.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            reinstate_school(self.tenant)

        rows = AuditEvent.objects.filter(
            module_key=AuditModuleKey.ONBOARDING,
            action_type=AuditActionType.ONBOARDING_REINSTATED,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().tenant_id, self.tenant.pk)

    def test_a_school_that_is_not_suspended_is_refused(self):
        from .exceptions import OnboardingNotSuspended

        with self.captureOnCommitCallbacks(execute=True):
            reinstate_school(self.tenant)

        with self.assertRaises(OnboardingNotSuspended) as raised:
            reinstate_school(Tenant.objects.get(pk=self.tenant.pk))

        self.assertEqual(raised.exception.error_code, "ONBOARDING_NOT_SUSPENDED")
        self.assertEqual(raised.exception.http_status, 409)

    def test_the_school_keeps_its_checklist(self):
        """Expiry suspends a school; it does not throw its work away."""
        from .services.provisioning import provision_onboarding

        with self.captureOnCommitCallbacks(execute=True):
            progress = provision_onboarding(self.tenant)
            reinstate_school(Tenant.objects.get(pk=self.tenant.pk))

        self.assertEqual(
            OnboardingProgress.all_objects.get(pk=progress.pk).readiness_state,
            ReadinessState.NOT_READY,
        )


class ReinstateEndpointTests(TestCase):
    """Platform staff only, and refused to a school-tenant caller."""

    def setUp(self):
        self.school = pending_school(
            "endpoint-school", "Endpoint School",
            pending_days=ONBOARDING_EXPIRY_DAYS + 3,
        )
        self.admin = make_school_admin(
            None, email="endpoint-admin@test.com", tenant=self.school.tenant,
        )
        # A second school, still pending, whose admin is a school-tenant caller
        # that can actually authenticate (a suspended tenant cannot).
        self.other = pending_school("other-school", "Other School", pending_days=1)
        self.other_admin = make_school_admin(
            None, email="other-admin@test.com", tenant=self.other.tenant,
        )

        self.operator = make_vision_user(email="operator@codex.test")
        grant_platform(self.operator, PERM_PROGRESS_REACTIVATE)

        with self.captureOnCommitCallbacks(execute=True):
            expire_stale_onboarding()

    def client_for(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def url(self, slug, *, as_tenant):
        return (
            f"{reverse('onboarding-reinstate', args=[slug])}"
            f"?tenant={as_tenant.slug}"
        )

    def test_platform_staff_may_reinstate(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.operator).post(
                self.url(self.school.slug, as_tenant=codex_tenant()),
                {}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], Tenant.Status.PENDING)
        self.assertEqual(
            statuses(self.school), (SchoolStatus.PENDING, Tenant.Status.PENDING),
        )

    def test_a_school_tenant_caller_is_refused(self):
        """No key, so the gate answers before the view's own platform check."""
        response = self.client_for(self.other_admin).post(
            self.url(self.school.slug, as_tenant=self.other.tenant),
            {}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            statuses(self.school), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )

    def test_a_school_caller_holding_the_key_is_still_refused(self):
        """The second gate: the asserted tenant must be the platform tenant."""
        from .tests import grant_extra

        grant_extra(self.other.tenant, self.other_admin, PERM_PROGRESS_REACTIVATE)

        response = self.client_for(self.other_admin).post(
            self.url(self.school.slug, as_tenant=self.other.tenant),
            {}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            statuses(self.school), (SchoolStatus.SUSPENDED, Tenant.Status.SUSPENDED),
        )

    def test_the_suspended_school_cannot_be_addressed_as_the_asserted_tenant(self):
        """Why the slug is in the path: a suspended tenant is not authenticable."""
        response = self.client_for(self.operator).post(
            self.url(self.school.slug, as_tenant=self.school.tenant),
            {}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_an_unknown_school_is_404(self):
        response = self.client_for(self.operator).post(
            self.url("no-such-school", as_tenant=codex_tenant()), {}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_reinstating_a_school_that_is_not_suspended_is_a_conflict(self):
        response = self.client_for(self.operator).post(
            self.url(self.other.slug, as_tenant=codex_tenant()), {}, format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "ONBOARDING_NOT_SUSPENDED",
        )


# ══════════════════════════════════════════════════════════════════════════
# FR-013b  The two-weekly operator list
# ══════════════════════════════════════════════════════════════════════════

class StaleReportTests(TestCase):
    def setUp(self):
        self.ageing = pending_school(
            "ageing-school", "Ageing School",
            pending_days=STALE_ONBOARDING_AFTER_DAYS + 5,
        )
        self.fresh = pending_school("fresh-school", "Fresh School", pending_days=2)
        self.operator = make_vision_user(email="report-operator@codex.test")
        grant_platform(self.operator, PERM_GO_LIVE_APPROVE)

    def test_it_lists_the_ageing_school_and_not_the_fresh_one(self):
        with patch("schools.vs_onboarding.services.effects._send") as send:
            result = stale_onboarding_report()

        slugs = {row["slug"] for row in result["ageing"]}
        self.assertEqual(slugs, {"ageing-school"})
        self.assertTrue(result["dispatched"])
        self.assertEqual(send.call_args.args[0], "onboarding.stale_report")
        self.assertIn("Ageing School", send.call_args.args[1]["ageing_list"])
        self.assertNotIn("Fresh School", send.call_args.args[1]["ageing_list"])

    def test_the_recipients_are_the_platform_operators(self):
        outsider = make_school_admin(
            None, email="not-an-operator@test.com", tenant=self.fresh.tenant,
        )

        with patch("schools.vs_onboarding.services.effects._send") as send:
            stale_onboarding_report()

        recipients = {user.pk for user in send.call_args.args[2]}
        self.assertEqual(recipients, {self.operator.pk})
        self.assertNotIn(outsider.pk, recipients)

    def test_a_recently_expired_school_appears_on_the_list(self):
        expired = pending_school(
            "expired-school", "Expired School",
            pending_days=ONBOARDING_EXPIRY_DAYS + 10,
        )
        with self.captureOnCommitCallbacks(execute=True):
            expire_stale_onboarding()

        with patch("schools.vs_onboarding.services.effects._send") as send:
            result = stale_onboarding_report()

        self.assertEqual(
            {row["slug"] for row in result["expired"]}, {expired.tenant.slug},
        )
        self.assertIn("Expired School", send.call_args.args[1]["expired_list"])
        # And it is no longer counted as ageing: it is gone, not going.
        self.assertNotIn(
            "expired-school", {row["slug"] for row in result["ageing"]},
        )

    def test_a_clean_fortnight_sends_nothing(self):
        """Nothing ageing and nothing expired means no message at all."""
        Tenant.objects.filter(pk=self.ageing.tenant_id).update(
            pending_since=timezone.now(),
        )

        with patch("schools.vs_onboarding.services.effects._send") as send:
            result = stale_onboarding_report()

        self.assertFalse(result["dispatched"])
        send.assert_not_called()

    def test_the_command_dry_run_dispatches_nothing(self):
        out = StringIO()

        with patch("schools.vs_onboarding.services.effects._send") as send:
            call_command(
                "report_stale_onboarding", "--dry-run",
                stdout=out, stderr=StringIO(),
            )

        send.assert_not_called()
        self.assertIn("Ageing School", out.getvalue())
        self.assertIn("Nothing was dispatched", out.getvalue())

    def test_the_command_dispatches(self):
        out = StringIO()

        with patch("schools.vs_onboarding.services.effects._send") as send:
            call_command("report_stale_onboarding", stdout=out, stderr=StringIO())

        self.assertEqual(send.call_count, 1)
        self.assertIn("Reported to 1 platform operator(s)", out.getvalue())


class StaleReportTemplateTests(TestCase):
    """The seeded templates render the context this module actually sends."""

    def setUp(self):
        from vs_notifications.services.seed import (
            seed_notification_templates, seed_platform_settings,
        )

        self.ageing = pending_school(
            "template-school", "Template School",
            pending_days=STALE_ONBOARDING_AFTER_DAYS + 1,
        )
        self.operator = make_vision_user(email="template-operator@codex.test")
        grant_platform(self.operator, PERM_GO_LIVE_APPROVE)
        seed_notification_templates()
        seed_platform_settings()

    def test_the_report_renders_without_leaving_a_placeholder(self):
        from vs_notifications.models import Notification

        stale_onboarding_report()

        rows = Notification.objects.filter(
            event_type__key="onboarding.stale_report",
        )
        self.assertTrue(rows.exists())
        body = rows.first().body
        self.assertNotIn("{{", body)
        self.assertIn("1", body)


# ══════════════════════════════════════════════════════════════════════════
# Go-live history retention
# ══════════════════════════════════════════════════════════════════════════

class GoLiveRetentionTests(TestCase):
    """A year of history, minus the row that records a school going live."""

    def setUp(self):
        self.live = make_school(slug="retained-live", name="Retained", status="ACTIVE")
        self.other = make_school(slug="retained-two", name="Retained Two", status="PENDING")
        self.went_live_at = days_ago(400)

        OnboardingProgress.all_objects.create(
            tenant=self.live.tenant,
            readiness_state=ReadinessState.LIVE,
            go_live_at=self.went_live_at,
        )
        # The row that took this school live: old, ACTIVATED, and matching the
        # progress row's go_live_at exactly, as activation writes it.
        self.activating = self._request(
            self.live.tenant, GoLiveStatus.ACTIVATED,
            created=days_ago(401), reviewed=self.went_live_at,
        )
        self.old_rejected = self._request(
            self.live.tenant, GoLiveStatus.REJECTED, created=days_ago(500),
        )
        self.superseded = self._request(
            self.live.tenant, GoLiveStatus.ACTIVATED,
            created=days_ago(380), reviewed=days_ago(380),
        )
        self.old_stale_pending = self._request(
            self.other.tenant, GoLiveStatus.PENDING, created=days_ago(500),
        )
        self.recent_failed = self._request(
            self.other.tenant, GoLiveStatus.FAILED, created=days_ago(10),
        )

    def _request(self, tenant, status, *, created, reviewed=None):
        row = GoLiveRequest.all_objects.create(
            tenant=tenant,
            preferred_go_live_at=created,
            acknowledged=True,
            status=status,
            reviewed_at=reviewed,
            rejection_reason="no" if status == GoLiveStatus.REJECTED else "",
        )
        GoLiveRequest.all_objects.filter(pk=row.pk).update(created_at=created)
        row.refresh_from_db()
        return row

    def surviving(self):
        return set(GoLiveRequest.all_objects.values_list("pk", flat=True))

    def test_it_keeps_the_request_that_activated_the_school(self):
        result = purge_go_live_history()

        self.assertIn(self.activating.pk, self.surviving())
        self.assertEqual(result["kept_activating"], 1)

    def test_it_removes_the_old_rejected_pending_and_superseded_rows(self):
        result = purge_go_live_history()

        survivors = self.surviving()
        self.assertNotIn(self.old_rejected.pk, survivors)
        self.assertNotIn(self.old_stale_pending.pk, survivors)
        self.assertNotIn(self.superseded.pk, survivors)
        self.assertEqual(result["removed"], 3)
        self.assertEqual(
            result["by_status"],
            {
                GoLiveStatus.ACTIVATED.value: 1,
                GoLiveStatus.PENDING.value: 1,
                GoLiveStatus.REJECTED.value: 1,
            },
        )

    def test_a_row_inside_the_window_is_untouched(self):
        purge_go_live_history()

        self.assertIn(self.recent_failed.pk, self.surviving())

    def test_it_is_idempotent(self):
        first = purge_go_live_history()
        after_first = self.surviving()

        second = purge_go_live_history()

        self.assertEqual(first["removed"], 3)
        self.assertEqual(second["removed"], 0)
        self.assertEqual(self.surviving(), after_first)

    def test_dry_run_reports_the_same_rows_and_deletes_nothing(self):
        before = self.surviving()

        result = purge_go_live_history(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["removed"], 3)
        self.assertEqual(result["kept_activating"], 1)
        self.assertEqual(self.surviving(), before)

    def test_the_window_is_a_year(self):
        self.assertEqual(GO_LIVE_HISTORY_RETENTION_DAYS, 365)

        edge = self._request(
            self.other.tenant, GoLiveStatus.REJECTED,
            created=days_ago(GO_LIVE_HISTORY_RETENTION_DAYS - 1),
        )
        purge_go_live_history()

        self.assertIn(edge.pk, self.surviving())

    def test_a_schools_history_is_never_purged_from_another_schools_run(self):
        """Two tenants, so the sets it keeps and drops are actually separated."""
        purge_go_live_history()

        survivors = self.surviving()
        self.assertIn(self.activating.pk, survivors)
        self.assertIn(self.recent_failed.pk, survivors)

    def test_the_command_reports_what_it_removed(self):
        out = StringIO()

        call_command("purge_go_live_history", stdout=out, stderr=StringIO())

        output = out.getvalue()
        self.assertIn("removed 3 go-live request(s)", output)
        self.assertIn("kept 1 activating request(s)", output)

    def test_the_command_dry_run_deletes_nothing(self):
        out = StringIO()
        before = self.surviving()

        call_command(
            "purge_go_live_history", "--dry-run", stdout=out, stderr=StringIO(),
        )

        self.assertIn("would remove 3 go-live request(s)", out.getvalue())
        self.assertEqual(self.surviving(), before)


class RetentionScopeTests(TestCase):
    """The activating row is found even where the exact stamp is gone."""

    def test_the_earliest_activated_row_is_kept_when_no_stamp_matches(self):
        school = make_school(slug="stampless", name="Stampless", status="ACTIVE")
        OnboardingProgress.all_objects.create(
            tenant=school.tenant, readiness_state=ReadinessState.LIVE,
        )
        first = GoLiveRequest.all_objects.create(
            tenant=school.tenant, preferred_go_live_at=days_ago(500),
            acknowledged=True, status=GoLiveStatus.ACTIVATED,
            reviewed_at=days_ago(500),
        )
        second = GoLiveRequest.all_objects.create(
            tenant=school.tenant, preferred_go_live_at=days_ago(400),
            acknowledged=True, status=GoLiveStatus.ACTIVATED,
            reviewed_at=days_ago(400),
        )
        GoLiveRequest.all_objects.filter(pk=first.pk).update(created_at=days_ago(500))
        GoLiveRequest.all_objects.filter(pk=second.pk).update(created_at=days_ago(400))

        purge_go_live_history()

        survivors = set(GoLiveRequest.all_objects.values_list("pk", flat=True))
        self.assertEqual(survivors, {first.pk})
