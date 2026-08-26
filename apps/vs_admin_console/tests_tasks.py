"""Tests for the admin-console task monitoring endpoints (BackgroundJob-backed).

The security-critical cases come first, because they are the ones this surface
got wrong: it was gated on ``is_staff`` (a Django-admin login flag every Codex
account carries), it served ``result``, ``error`` and ``traceback`` on every
list row, and it never filtered by tenant.
"""
import uuid

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import BackgroundJob, TaskDiagnostic
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_role_permission,
    make_assignment,
    make_school,
)
from vs_tenants.models import Tenant
from vs_user.models import User

PERM_VIEW = "platform.tasks.view"
PERM_VIEW_ALL = "platform.tasks.view_all"
PERM_VIEW_SENSITIVE = "platform.tasks.view_sensitive"


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    return codex_tenant()


def _cx_user(email="monitor@codexng.com", **extra):
    """A Codex account with no role assignments at all.

    This is Femi from the incident that prompted the change: a real member of
    staff, on the platform tenant, holding ``is_staff`` because
    ``UserCreationService`` grants it to everyone on that tenant, and holding
    no permission anybody chose to give him.
    """
    defaults = dict(
        status="ACTIVE", first_name="Mon", last_name="Itor", is_staff=True,
    )
    defaults.update(extra)
    return User.objects.create_user(
        tenant=_platform_tenant(), email=email, password="x", **defaults,
    )


def _grant(user, *keys):
    """Give *user* a platform role carrying *keys*."""
    role = make_platform_role(name=f"Ops {user.pk}")
    for key in keys:
        make_platform_role_permission(role, make_permission(key))
    make_platform_assignment(user, role)
    return role


def _job(name, job_status="SUCCEEDED", **extra):
    if "tenant" not in extra and "tenant_id" not in extra:
        extra["tenant"] = Tenant.objects.get(slug="codex")
    return BackgroundJob.objects.create(
        celery_task_id=str(uuid.uuid4()), task_name=name, status=job_status, **extra,
    )


class TaskMonitorAccessTests(TestCase):
    """Who may open the monitor at all."""

    def setUp(self):
        self.client = APIClient()
        _job("a.b.c")

    def test_unauthenticated_denied(self):
        self.assertEqual(
            self.client.get(reverse("tasks-list")).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_django_staff_without_the_key_is_denied(self):
        """The whole finding, as a test.

        Femi is on the Codex tenant and has ``is_staff``. Under the old gate
        that was the entire check and he saw every school's failures. He now
        holds no ``platform.tasks.view``, so he sees nothing.
        """
        self.client.force_authenticate(user=_cx_user())
        self.assertEqual(
            self.client.get(reverse("tasks-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_superuser_flag_alone_is_not_a_grant(self):
        """``is_superuser`` is a Django flag, not an RBAC assignment."""
        user = _cx_user(email="root@codexng.com", is_superuser=True)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self.client.get(reverse("tasks-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_school_cannot_even_be_granted_the_key(self):
        """The boundary is enforced a layer earlier than the endpoint.

        ``platform.tasks.*`` is PLATFORM-scoped, so
        ``assert_tenant_may_hold`` refuses the grant outright. A school admin
        who can mint roles therefore cannot name one "Task Monitor" and put
        the key in it - which is the attack the scope column exists to stop.
        """
        school = make_school(slug="corona", name="Corona Secondary School")
        role = make_role(school, name="Bursar")
        with self.assertRaises(ValidationError):
            make_role_permission(role, make_permission(PERM_VIEW))

    def test_school_user_is_refused_the_console(self):
        """Tenant kind, not the key, is what closes the console to a school."""
        school = make_school(slug="greenfield", name="Greenfield School")
        bursar = User.objects.create_user(
            tenant=school.tenant, email="bursar@greenfield.ng", password="x",
            status="ACTIVE", first_name="Ada", last_name="Bello",
        )
        self.client.force_authenticate(user=bursar)
        self.assertEqual(
            self.client.get(reverse("tasks-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_holder_of_the_key_may_list(self):
        user = _cx_user()
        _grant(user, PERM_VIEW)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self.client.get(reverse("tasks-list")).status_code,
            status.HTTP_200_OK,
        )


class TaskMonitorRedactionTests(TestCase):
    """What the surface is willing to show once you are through the door."""

    def setUp(self):
        self.client = APIClient()
        self.user = _cx_user()
        _grant(self.user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=self.user)
        self.job = _job(
            "vs_import_data.tasks.execute_import_batch_task",
            job_status="FAILED", kind="import",
            error="Key (email)=([redacted]) already exists.",
            traceback="File \"/app/x.py\", line 1",
        )

    def test_list_rows_carry_no_payload_fields(self):
        row = self.client.get(reverse("tasks-list")).data["data"][0]
        for field in ("result", "error", "traceback"):
            self.assertNotIn(field, row)
        # The list still says a raw record exists, without being it.
        self.assertIn("has_diagnostic", row)

    def test_detail_shows_redacted_error_but_never_the_traceback(self):
        resp = self.client.get(reverse("tasks-detail", args=[self.job.pk]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("error", resp.data["data"])
        self.assertNotIn("traceback", resp.data["data"])


class TaskDiagnosticAccessTests(TestCase):
    """The raw traceback: one row at a time, one key, one audit event."""

    def setUp(self):
        self.client = APIClient()
        self.job = _job("t.failing", job_status="FAILED")
        self.diagnostic = TaskDiagnostic.objects.create(
            job=self.job, tenant=self.job.tenant, task_name="t.failing",
            raw_error="Key (email)=(ada.okeye@gmail.com) already exists.",
            raw_traceback="Traceback (most recent call last): ...",
            expires_at=timezone.now() + timezone.timedelta(days=400),
        )

    def test_plain_view_key_cannot_read_diagnostics(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self.client.get(reverse("tasks-diagnostics", args=[self.job.pk])).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_sensitive_key_reads_the_raw_text(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL, PERM_VIEW_SENSITIVE)
        self.client.force_authenticate(user=user)
        resp = self.client.get(reverse("tasks-diagnostics", args=[self.job.pk]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("ada.okeye@gmail.com", resp.data["data"]["raw_error"])

    def test_reading_the_raw_text_is_audited_against_the_job_tenant(self):
        from vs_audit.models import AuditActionType, AuditEvent

        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL, PERM_VIEW_SENSITIVE)
        self.client.force_authenticate(user=user)
        self.client.get(reverse("tasks-diagnostics", args=[self.job.pk]))

        event = AuditEvent.objects.filter(
            action_type=AuditActionType.TASK_DIAGNOSTIC_VIEWED,
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_user_id, user.pk)
        self.assertEqual(event.entity_id, str(self.job.pk))
        self.assertEqual(event.tenant_id, self.diagnostic.tenant_id)

    def test_a_run_with_no_diagnostic_is_a_404_not_an_empty_body(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL, PERM_VIEW_SENSITIVE)
        self.client.force_authenticate(user=user)
        succeeded = _job("t.fine")
        self.assertEqual(
            self.client.get(reverse("tasks-diagnostics", args=[succeeded.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class TaskMonitorTenantScopeTests(TestCase):
    """Which customers' runs a given operator can see."""

    def setUp(self):
        self.client = APIClient()
        self.corona = make_school(slug="corona", name="Corona Secondary School")
        self.greenfield = make_school(slug="greenfield", name="Greenfield School")
        _job("t.codex")
        self.corona_job = _job("t.corona", tenant=self.corona.tenant)
        self.greenfield_job = _job("t.greenfield", tenant=self.greenfield.tenant)

    def _names(self, resp):
        return {row["task_name"] for row in resp.data["data"]}

    def test_view_all_sees_every_tenant(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self._names(self.client.get(reverse("tasks-list"))),
            {"t.codex", "t.corona", "t.greenfield"},
        )

    def test_without_view_all_no_customer_rows_are_visible(self):
        """An operator without the cross-tenant key sees Codex's runs only.

        Femi could previously page through Corona's and Greenfield's failures
        together. Now the platform-wide list is a key of its own, and without
        it he sees the platform's own system jobs and nothing belonging to a
        school.
        """
        user = _cx_user()
        _grant(user, PERM_VIEW)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self._names(self.client.get(reverse("tasks-list"))),
            {"t.codex"},
        )

    def test_tenant_filter_narrows_within_scope(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=user)
        resp = self.client.get(reverse("tasks-list") + "?tenant=corona")
        self.assertEqual(self._names(resp), {"t.corona"})

    def test_unknown_tenant_filter_returns_nothing_rather_than_everything(self):
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=user)
        resp = self.client.get(reverse("tasks-list") + "?tenant=does-not-exist")
        self.assertEqual(resp.data["data"], [])

    def test_out_of_scope_row_is_not_reachable_by_id(self):
        user = _cx_user()
        _grant(user, PERM_VIEW)
        self.client.force_authenticate(user=user)
        self.assertEqual(
            self.client.get(
                reverse("tasks-detail", args=[self.greenfield_job.pk]),
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_stats_are_scoped_too(self):
        """Counts must not leak the size of tenants the caller cannot list."""
        user = _cx_user()
        _grant(user, PERM_VIEW)
        self.client.force_authenticate(user=user)
        data = self.client.get(reverse("tasks-stats")).data["data"]
        self.assertEqual(data["total"], 1)


class TaskMonitorListingTests(TestCase):
    """The ordinary triage filters, unchanged in behaviour."""

    def setUp(self):
        self.client = APIClient()
        user = _cx_user()
        _grant(user, PERM_VIEW, PERM_VIEW_ALL)
        self.client.force_authenticate(user=user)

    def test_list_and_filters(self):
        _job("vs_import_data.tasks.execute_import_batch_task", kind="import")
        _job("vs_user.tasks.send_invitation_email_task", job_status="FAILED", kind="email")

        url = reverse("tasks-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 2)

        resp = self.client.get(url + "?status=FAILED")
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertIn("invitation", resp.data["data"][0]["task_name"])

        resp = self.client.get(url + "?task=import")
        self.assertEqual(len(resp.data["data"]), 1)

        resp = self.client.get(url + "?kind=email")
        self.assertEqual(len(resp.data["data"]), 1)

    def test_stats(self):
        _job("a.b.c")
        _job("a.b.c", job_status="FAILED", finished_at=timezone.now())

        resp = self.client.get(reverse("tasks-stats"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["by_status"].get("FAILED"), 1)
        self.assertEqual(len(data["recent_failures"]), 1)

    def test_status_counts_group_by_status_alone(self):
        """Guards a grouping bug the sibling endpoint already documents.

        ``BackgroundJob.Meta.ordering`` is ``-created_at``, and Django adds
        every ORDER BY column to the GROUP BY - so ``by_status`` silently
        counted one row per timestamp instead of one per status, and the
        cards read 1 where the table showed many. ``.order_by()`` clears it.
        Two failures at different moments is the smallest case that fails
        without the fix.
        """
        _job("a.b.c", job_status="FAILED", finished_at=timezone.now())
        _job("a.b.c", job_status="FAILED", finished_at=timezone.now())

        data = self.client.get(reverse("tasks-stats")).data["data"]
        self.assertEqual(data["by_status"].get("FAILED"), 2)

    def test_malformed_since_is_a_400_not_a_500(self):
        """``?since=yesterday`` used to reach the ORM and surface as a 500."""
        resp = self.client.get(reverse("tasks-list") + "?since=yesterday")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_well_formed_since_still_filters(self):
        _job("a.b.c")
        resp = self.client.get(reverse("tasks-list") + "?since=2000-01-01")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)

    def test_empty_list_stays_a_list(self):
        """``success_response`` coerces ``None`` to ``{}``; a list must survive."""
        resp = self.client.get(reverse("tasks-list") + "?status=CANCELLED")
        self.assertEqual(resp.data["data"], [])

    def test_schedule_lists_beat_entries(self):
        resp = self.client.get(reverse("tasks-schedule"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        names = {e["name"] for e in data["entries"]}
        self.assertIn("dispatch-pending-import-notifications", names)
        self.assertIn("prune-background-jobs", names)
        self.assertIn("prune-task-diagnostics", names)
        self.assertIn("eager_mode", data)
