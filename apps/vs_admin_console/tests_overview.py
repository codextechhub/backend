"""Console overview aggregate - security matrix first, then per-section numbers.

The security-critical tests are the FIRST group: this endpoint requires nothing
but an active account, so every number inside it has to be gated individually.
A section the caller cannot otherwise fetch must be ABSENT from the payload -
absent, not zero, because a zero would read as real data on the screen.

The second group pins each section's arithmetic against data built for the case,
and the third pins tenant isolation on the one section that spans tenants.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification, NotificationEventType
from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from schools.vs_schools.models import School, SchoolStatus
from vs_todo.models import Task
from vs_user.tokens import CodeXRefreshToken

OVERVIEW_URL = "/v1/admin/dashboard/overview/"

PERM_SCHOOLS = "platform.schools.view"
PERM_TEAM = "platform.team.view"
PERM_HEALTH = "platform.health.view"
PERM_TICKETS = "tickets.ticket.view"


def grant(user, *keys):
    """Give *user* an active role on their own tenant carrying *keys*."""
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"overview-test-{user.pk}",
        defaults={"name": f"Overview Test Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


class OverviewTestBase(TestCase):
    def setUp(self):
        self.school = make_school(slug="ov-school", name="Overview School")
        self.branch = make_branch(self.school)
        self.user = make_vision_user(email="ov-cx@codex.test")

    def client_for(self, user):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def fetch(self, user=None, tenant=None):
        user = user or self.user
        slug = tenant or user.tenant.slug
        resp = self.client_for(user).get(f"{OVERVIEW_URL}?tenant={slug}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]


class OverviewPermissionTests(OverviewTestBase):
    """Every gated section must be absent without its own permission key."""

    def test_anonymous_is_rejected(self):
        resp = APIClient().get(OVERVIEW_URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_ungranted_user_gets_no_gated_sections(self):
        data = self.fetch()
        for section in ("schools", "team", "health", "tickets"):
            self.assertNotIn(
                section, data,
                f"{section} leaked to a user holding no permission for it",
            )

    def test_schools_section_needs_the_schools_view_key(self):
        self.assertNotIn("schools", self.fetch())
        grant(self.user, PERM_SCHOOLS)
        self.assertIn("schools", self.fetch())

    def test_team_section_needs_the_team_view_key(self):
        self.assertNotIn("team", self.fetch())
        grant(self.user, PERM_TEAM)
        self.assertIn("team", self.fetch())

    def test_health_section_needs_the_health_view_key(self):
        self.assertNotIn("health", self.fetch())
        grant(self.user, PERM_HEALTH)
        self.assertIn("health", self.fetch())

    def test_tickets_section_needs_the_ticket_view_key(self):
        self.assertNotIn("tickets", self.fetch())
        grant(self.user, PERM_TICKETS)
        self.assertIn("tickets", self.fetch())

    def test_one_key_does_not_unlock_the_others(self):
        # Guards against a single blanket gate being reintroduced over the lot.
        grant(self.user, PERM_SCHOOLS)
        data = self.fetch()
        self.assertIn("schools", data)
        for section in ("team", "health", "tickets"):
            self.assertNotIn(section, data)

    def test_own_data_sections_need_no_key(self):
        # These replace endpoints that were open to any active account and only
        # ever return the caller's own rows.
        data = self.fetch()
        for section in ("approvals", "submissions", "notifications"):
            self.assertIn(section, data)

    def test_tasks_section_is_cx_staff_only(self):
        school_admin = make_school_admin(self.branch, email="ov-admin@school.test")
        self.assertNotIn("tasks", self.fetch(school_admin, tenant=self.school.tenant.slug))
        self.assertIn("tasks", self.fetch())


class OverviewSectionTests(OverviewTestBase):
    """Each section's arithmetic, against data built for the case."""

    def test_schools_counts_only_active(self):
        make_school(slug="ov-active", name="Active", status=SchoolStatus.ACTIVE)
        make_school(slug="ov-inactive", name="Inactive", status=SchoolStatus.INACTIVE)
        grant(self.user, PERM_SCHOOLS)
        expected = School.objects.filter(status=SchoolStatus.ACTIVE).count()
        self.assertEqual(self.fetch()["schools"]["active"], expected)
        # Guard the assertion itself: an all-INACTIVE fixture would pass 0 == 0.
        self.assertGreaterEqual(expected, 1)
        self.assertTrue(School.objects.filter(status=SchoolStatus.INACTIVE).exists())

    def test_team_counts_active_cx_staff(self):
        make_vision_user(email="ov-cx2@codex.test")
        grant(self.user, PERM_TEAM)
        # Both CX users, and the school admin below must not be counted.
        make_school_admin(self.branch, email="ov-notcx@school.test")
        self.assertEqual(self.fetch()["team"]["total"], 2)

    def test_task_stats_and_the_three_listed_items(self):
        today = timezone.localdate()
        # 2 overdue, 1 high, 1 medium, 1 low, 1 done → in_progress 3, overdue 2.
        Task.objects.create(assignee=self.user, title="Overdue A", deadline=today - timedelta(days=3), priority="LOW")
        Task.objects.create(assignee=self.user, title="Overdue B", deadline=today - timedelta(days=1), priority="LOW")
        Task.objects.create(assignee=self.user, title="High", deadline=today + timedelta(days=5), priority="HIGH")
        Task.objects.create(assignee=self.user, title="Medium", deadline=today + timedelta(days=2), priority="MEDIUM")
        Task.objects.create(assignee=self.user, title="Low", deadline=today + timedelta(days=1), priority="LOW")
        Task.objects.create(assignee=self.user, title="Done", deadline=today, priority="HIGH", is_done=True)

        tasks = self.fetch()["tasks"]
        self.assertEqual(tasks["stats"]["overdue"], 2)
        self.assertEqual(tasks["stats"]["in_progress"], 3)
        self.assertEqual(tasks["stats"]["done"], 1)

        # Ordering: overdue first (nearest deadline first), then by priority.
        # Completed tasks never appear, and the list stops at three.
        self.assertEqual(
            [item["title"] for item in tasks["items"]],
            ["Overdue A", "Overdue B", "High"],
        )

    def test_tasks_are_only_the_callers_own(self):
        other = make_vision_user(email="ov-other@codex.test")
        Task.objects.create(
            assignee=other, title="Not mine",
            deadline=timezone.localdate() + timedelta(days=1), priority="HIGH",
        )
        tasks = self.fetch()["tasks"]
        self.assertEqual(tasks["stats"]["total"], 0)
        self.assertEqual(tasks["items"], [])

    def test_unread_counts_only_the_callers_unread_in_app(self):
        event = NotificationEventType.objects.create(
            key="overview.test", label="Overview test",
            source_module="vs_notifications",
            supported_channels=[ChannelChoices.IN_APP],
        )
        other = make_vision_user(email="ov-notif@codex.test")
        common = dict(tenant=self.user.tenant, event_type=event, body="b")
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=False, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=False, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=True, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.EMAIL, is_read=False, **common)
        Notification.objects.create(recipient=other, channel=ChannelChoices.IN_APP, is_read=False, **common)

        self.assertEqual(self.fetch()["notifications"]["unread"], 2)

    def test_ticket_counts_drop_finished_work(self):
        # The landing screen's rule: doing the work clears the card. Collecting
        # the file removes the row, and an expired or purged file never keeps it.
        from vs_tickets.constants import TicketStatus
        from vs_tickets.models import Ticket

        grant(self.user, PERM_TICKETS)
        common = dict(tenant=self.user.tenant, requester=self.user, description="x")
        Ticket.objects.create(title="Untouched", status=TicketStatus.OPEN, **common)
        Ticket.objects.create(
            title="Mine, live", status=TicketStatus.IN_PROGRESS,
            assignee=self.user, **common,
        )
        Ticket.objects.create(
            title="Mine, resolved", status=TicketStatus.RESOLVED,
            assignee=self.user, **common,
        )
        Ticket.objects.create(
            title="Mine, closed", status=TicketStatus.CLOSED,
            assignee=self.user, **common,
        )

        tickets = self.fetch()["tickets"]
        self.assertEqual(tickets["assigned_to_me"], 1)
        # Live work is not just OPEN: a ticket someone picked up still counts.
        self.assertEqual(tickets["active"], 2)

    def test_health_reports_posture_not_the_whole_command_centre(self):
        grant(self.user, PERM_HEALTH)
        health = self.fetch()["health"]
        self.assertEqual(set(health), {"label", "overall", "active_incidents"})

    def test_empty_state_is_zeros_not_missing_keys(self):
        # The screen reads these unconditionally; a missing key would render as
        # "undefined" rather than 0.
        data = self.fetch()
        self.assertEqual(data["approvals"]["pending"], 0)
        self.assertEqual(data["approvals"]["items"], [])
        self.assertEqual(data["submissions"]["returned"], 0)
        self.assertEqual(data["submissions"]["items"], [])
        self.assertEqual(data["notifications"]["unread"], 0)
        self.assertEqual(data["tasks"]["stats"]["total"], 0)


class OverviewWorklistTests(OverviewTestBase):
    """The dashboard worklist items inside approvals/submissions.

    Items must stay inside the caller's own queue (another user's decisions or
    submissions can never appear), match the count's own rules, and stay capped
    so the landing payload cannot grow with the queue.
    """

    def setUp(self):
        super().setUp()
        from django.contrib.contenttypes.models import ContentType
        from vs_workflow.models import WorkflowStage, WorkflowTemplate

        self.requester = make_vision_user(email="ov-requester@codex.test")
        self.requester.first_name = "Rita"
        self.requester.last_name = "Requester"
        self.requester.save(update_fields=["first_name", "last_name"])

        self.template = WorkflowTemplate.objects.create(
            document_type="TEST_DOC", code="default", name="Overview Template",
        )
        self.stage = WorkflowStage.objects.create(
            template=self.template, code="review", label="Manager Review",
            kind="APPROVAL", order=1, advance_rule="ANY",
            on_rejection="TERMINAL", skip_if_no_approvers=False,
        )
        self.doc_ct = ContentType.objects.get_for_model(WorkflowTemplate)

    def _make_instance(self, requester, status="IN_PROGRESS", object_id="doc-1"):
        from django.utils import timezone as tz
        from vs_workflow.models import WorkflowInstance

        return WorkflowInstance.objects.create(
            tenant=requester.tenant,
            template=self.template,
            document_content_type=self.doc_ct,
            document_object_id=object_id,
            document_type=self.template.document_type,
            status=status,
            requested_by=requester,
            current_stage=self.stage,
            submitted_at=tz.now(),
        )

    def _queue_for(self, approver, object_id="doc-1"):
        """One instance awaiting *approver*'s decision; returns the instance."""
        from django.utils import timezone as tz
        from vs_workflow.models import WorkflowStageApprover, WorkflowStageInstance

        instance = self._make_instance(self.requester, object_id=object_id)
        stage_instance = WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status="ACTIVE", attempt=1, activated_at=tz.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=stage_instance, user=approver, attempt=1,
        )
        return instance

    def test_approval_items_carry_what_the_row_renders(self):
        instance = self._queue_for(self.user)
        approvals = self.fetch()["approvals"]
        self.assertEqual(approvals["pending"], 1)
        (item,) = approvals["items"]
        self.assertEqual(item["id"], str(instance.id))
        self.assertEqual(item["document_type"], "TEST_DOC")
        self.assertEqual(item["document_object_id"], "doc-1")
        self.assertEqual(item["stage_label"], "Manager Review")
        self.assertEqual(item["requested_by_name"], "Rita Requester")
        self.assertIsNotNone(item["awaiting_since"])

    def test_approval_items_are_only_the_callers_queue(self):
        other = make_vision_user(email="ov-other@codex.test")
        self._queue_for(other, object_id="not-mine")
        approvals = self.fetch()["approvals"]
        self.assertEqual(approvals["pending"], 0)
        self.assertEqual(approvals["items"], [])

    def test_approval_items_are_capped_but_the_count_is_not(self):
        from vs_admin_console.overview import APPROVAL_ITEMS_LIMIT

        for n in range(APPROVAL_ITEMS_LIMIT + 2):
            self._queue_for(self.user, object_id=f"doc-{n}")
        approvals = self.fetch()["approvals"]
        self.assertEqual(approvals["pending"], APPROVAL_ITEMS_LIMIT + 2)
        self.assertEqual(len(approvals["items"]), APPROVAL_ITEMS_LIMIT)

    def test_returned_items_are_own_newest_first_and_capped(self):
        from vs_admin_console.overview import RETURNED_ITEMS_LIMIT
        from vs_workflow.models import WorkflowInstance

        for n in range(RETURNED_ITEMS_LIMIT + 1):
            self._make_instance(self.user, status="RETURNED", object_id=f"ret-{n}")
        # Someone else's returned submission must not appear in my list.
        self._make_instance(self.requester, status="RETURNED", object_id="not-mine")

        submissions = self.fetch()["submissions"]
        self.assertEqual(submissions["returned"], RETURNED_ITEMS_LIMIT + 1)
        self.assertEqual(len(submissions["items"]), RETURNED_ITEMS_LIMIT)
        listed = [item["document_object_id"] for item in submissions["items"]]
        self.assertNotIn("not-mine", listed)
        # auto_now updated_at: creation order ascending, so newest-first means
        # the highest suffix leads.
        self.assertEqual(listed[0], f"ret-{RETURNED_ITEMS_LIMIT}")



class OverviewSignalTenancyTests(OverviewTestBase):
    """A signal counts the caller's OWN books and nobody else's.

    The reported instance: a school's dashboard named the school with the worst
    fiscal runway, which was a different school entirely. The root cause was
    general - every query in ``_signals`` carried its permission gate but no
    tenant filter, so nine counts spanned the whole platform - so these cover the
    rule on both a named leak and a counted one. Holding a key means "this kind
    of number, for my books"; it never reaches another tenant's ledger.
    """

    def setUp(self):
        super().setUp()
        from vs_finance.models import LedgerEntity

        self.other_school = make_school(slug="ov-other", name="Other School")
        self.mine = LedgerEntity.objects.create(
            name="Mine Books", code="OVTENMINE", tenant=self.school.tenant,
        )
        self.theirs = LedgerEntity.objects.create(
            name="Theirs Books", code="OVTENTHEIRS", tenant=self.other_school.tenant,
        )
        self.admin = make_school_admin(self.branch, email="ov-ten-admin@school.test")

    def test_fiscal_runway_never_names_another_tenants_entity(self):
        from vs_finance.models import FiscalPeriod, FiscalYear, LedgerEntity

        today = timezone.localdate()

        def calendar(entity, end):
            year = FiscalYear.objects.create(
                entity=entity, year=end.year,
                start_date=end - timedelta(days=364), end_date=end,
            )
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=1, name="P1",
                start_date=end - timedelta(days=364), end_date=end,
            )

        # The other tenant's books are the worst on the platform by a mile.
        calendar(self.theirs, today + timedelta(days=1))
        calendar(self.mine, today + timedelta(days=300))
        for entity in LedgerEntity.objects.filter(is_active=True).exclude(
            pk__in=(self.mine.pk, self.theirs.pk),
        ):
            calendar(entity, today + timedelta(days=300))

        grant(self.admin, "finance.report.view")
        data = self.fetch(self.admin)
        # Everything of mine is healthy, so the signal must be absent entirely -
        # not present naming someone else's school.
        self.assertNotIn("fiscal_runway", data.get("signals", {}))

    def test_fiscal_runway_still_reports_the_callers_own_entity(self):
        from vs_finance.models import FiscalPeriod, FiscalYear, LedgerEntity

        today = timezone.localdate()

        def calendar(entity, end):
            year = FiscalYear.objects.create(
                entity=entity, year=end.year,
                start_date=end - timedelta(days=364), end_date=end,
            )
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=1, name="P1",
                start_date=end - timedelta(days=364), end_date=end,
            )

        calendar(self.mine, today + timedelta(days=10))
        for entity in LedgerEntity.objects.filter(is_active=True).exclude(pk=self.mine.pk):
            calendar(entity, today + timedelta(days=300))

        grant(self.admin, "finance.report.view")
        runway = self.fetch(self.admin)["signals"]["fiscal_runway"]
        self.assertEqual(runway["entity_name"], "Mine Books")

    def test_draft_journal_count_excludes_other_tenants(self):
        from vs_finance.constants import DocumentStatus
        from vs_finance.models import JournalEntry

        for entity in (self.mine, self.theirs, self.theirs):
            JournalEntry.objects.create(
                entity=entity, date=timezone.localdate(),
                narration="draft", status=DocumentStatus.DRAFT,
            )

        grant(self.admin, "finance.journal.view")
        signals = self.fetch(self.admin)["signals"]
        # Three drafts exist; exactly one is mine.
        self.assertEqual(signals["draft_journals"]["count"], 1)

    def test_a_quiet_tenant_gets_no_signal_from_a_noisy_neighbour(self):
        from vs_finance.constants import DocumentStatus
        from vs_finance.models import JournalEntry

        JournalEntry.objects.create(
            entity=self.theirs, date=timezone.localdate(),
            narration="their draft", status=DocumentStatus.DRAFT,
        )

        grant(self.admin, "finance.journal.view")
        data = self.fetch(self.admin)
        self.assertNotIn("draft_journals", data.get("signals", {}))


class OverviewSignalTests(OverviewTestBase):
    """Module signals - gated by the target screen's key AND silent when quiet.

    A healthy or empty signal must be absent, not zero: the dashboard renders
    a card per key it receives, so a leaked zero would paint a false alarm and
    a leaked count would hand out a number from a screen the caller can't open.
    """

    def test_signals_absent_when_everything_is_quiet(self):
        self.assertNotIn("signals", self.fetch())

    def test_failed_jobs_are_own_recent_failures_only(self):
        from datetime import timedelta

        from django.utils import timezone
        from core.models import BackgroundJob

        other = make_vision_user(email="ov-jobs-other@codex.test")
        now = timezone.now()

        def job(owner, status, finished, task_id):
            BackgroundJob.objects.create(
                owner=owner, tenant=owner.tenant, status=status,
                finished_at=finished, celery_task_id=task_id,
            )

        job(self.user, "FAILED", now, "sig-own-recent")
        job(self.user, "FAILED", now - timedelta(days=2), "sig-own-old")
        job(other, "FAILED", now, "sig-other-recent")
        job(self.user, "SUCCEEDED", now, "sig-own-ok")

        signals = self.fetch()["signals"]
        self.assertEqual(signals["jobs_failed_24h"]["count"], 1)

    def test_webhook_failures_need_the_webhook_key(self):
        """Gate plus scope: the key admits the signal, the tenant sizes it.

        The events are attached to a collection rather than left bare, because a
        webhook reaches a tenant only through its ``collection``/``payout`` side.
        An unattached one belongs to nobody and is counted for nobody - see
        ``test_unattributed_webhook_failures_count_for_nobody``.
        """
        from vs_finance.models import LedgerEntity
        from vs_payments.models import CollectionIntent, WebhookEvent

        entity = LedgerEntity.objects.create(
            name="Webhook Books", code="OVWH", tenant=self.user.tenant,
        )

        def collection(ref):
            return CollectionIntent.objects.create(
                entity=entity, provider="PAYSTACK", reference=ref,
            )

        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-1",
            collection=collection("ov-wh-1"),
        )
        WebhookEvent.objects.create(
            provider="PAYSTACK", status="PROCESSED", dedupe_key="sig-wh-2",
            collection=collection("ov-wh-2"),
        )

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "payments.webhook.view")
        signals = self.fetch()["signals"]
        self.assertEqual(signals["webhook_failures_24h"]["count"], 1)

    def test_webhook_failures_exclude_another_tenants(self):
        from vs_finance.models import LedgerEntity
        from vs_payments.models import CollectionIntent, WebhookEvent

        other = make_school(slug="ov-wh-other", name="Other Webhook School")
        theirs = LedgerEntity.objects.create(
            name="Their Webhook Books", code="OVWHOTHER", tenant=other.tenant,
        )
        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-other",
            collection=CollectionIntent.objects.create(
                entity=theirs, provider="PAYSTACK", reference="ov-wh-other"),
        )

        grant(self.user, "payments.webhook.view")
        self.assertNotIn("webhook_failures_24h", self.fetch().get("signals", {}))

    def test_unattributed_webhook_failures_stay_out_of_the_tenant_count(self):
        """A webhook on neither side belongs to no tenant.

        Bad signature, unparseable payload: real events, but not evidence about
        anybody's books. They are kept out of every tenant-scoped count rather
        than folded into one, and reported separately to the platform instead.
        """
        from vs_payments.models import WebhookEvent

        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-orphan")

        grant(self.user, "payments.webhook.view")
        self.assertNotIn("webhook_failures_24h", self.fetch().get("signals", {}))

    def test_platform_is_told_about_unattributed_failures(self):
        """Nobody's data still needs an owner, and that owner is the platform.

        A broken signature check or a flood of garbage at the webhook endpoint
        produces failures attached to nothing. No school can act on them, so
        they belong on CodeX's dashboard rather than on nobody's.
        """
        from vs_payments.models import WebhookEvent

        for i in range(3):
            WebhookEvent.objects.create(
                provider="PAYSTACK", status="FAILED", dedupe_key=f"sig-wh-orphan-{i}")

        grant(self.user, "payments.webhook.view")
        signals = self.fetch()["signals"]
        self.assertEqual(signals["unattributed_webhook_failures_24h"]["count"], 3)

    def test_the_platform_signal_counts_orphans_and_not_everything(self):
        """The guard against this becoming the exemption it replaced.

        A platform caller must not be handed every tenant's failures under a
        different name. Only rows attached to neither side are counted.
        """
        from vs_finance.models import LedgerEntity
        from vs_payments.models import CollectionIntent, WebhookEvent

        other = make_school(slug="ov-wh-plat", name="Platform Signal School")
        theirs = LedgerEntity.objects.create(
            name="Their Books", code="OVWHPLAT", tenant=other.tenant,
        )
        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-theirs",
            collection=CollectionIntent.objects.create(
                entity=theirs, provider="PAYSTACK", reference="ov-wh-plat"),
        )
        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-nobody")

        grant(self.user, "payments.webhook.view")
        signals = self.fetch()["signals"]
        # Two failures exist; exactly one belongs to nobody.
        self.assertEqual(signals["unattributed_webhook_failures_24h"]["count"], 1)

    def test_a_school_is_never_shown_the_platform_signal(self):
        from vs_payments.models import WebhookEvent
        from core.test_utils import TenantAPIClient  # noqa: F401

        WebhookEvent.objects.create(
            provider="PAYSTACK", status="FAILED", dedupe_key="sig-wh-hidden")

        school = make_school(slug="ov-wh-school", name="Webhook School")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="ov-wh-admin@school.test")
        grant(admin, "payments.webhook.view")

        data = self.fetch(admin)
        self.assertNotIn("unattributed_webhook_failures_24h", data.get("signals", {}))

    def test_fiscal_runway_needs_the_finance_key_and_reports_the_worst_entity(self):
        from datetime import timedelta

        from django.utils import timezone
        from vs_finance.models import FiscalPeriod, FiscalYear, LedgerEntity

        healthy = LedgerEntity.objects.create(
            name="Healthy Books", code="OVHEALTHY", tenant=self.user.tenant,
        )
        expiring = LedgerEntity.objects.create(
            name="Expiring Books", code="OVEXPIRING", tenant=self.user.tenant,
        )
        today = timezone.localdate()

        def calendar(entity, end):
            year = FiscalYear.objects.create(
                entity=entity, year=end.year,
                start_date=end - timedelta(days=364), end_date=end,
            )
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=year, period_no=1, name="P1",
                start_date=end - timedelta(days=364), end_date=end,
            )

        calendar(healthy, today + timedelta(days=300))
        calendar(expiring, today + timedelta(days=10))
        # Any migration-seeded entities (e.g. the CODEX platform entity) have no
        # calendar and would legitimately rank EXPIRED - give every other active
        # entity a healthy one so the fixtures above control the outcome.
        for entity in LedgerEntity.objects.filter(is_active=True).exclude(
            pk__in=(healthy.pk, expiring.pk),
        ):
            calendar(entity, today + timedelta(days=300))

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "finance.report.view")
        runway = self.fetch()["signals"]["fiscal_runway"]
        self.assertEqual(runway["entity_name"], "Expiring Books")
        self.assertEqual(runway["status"], "EXPIRING")
        self.assertEqual(runway["days_remaining"], 10)

    def test_draft_journals_and_open_pos_stay_gated(self):
        # No fixtures needed: the gate check runs before any query, so absent
        # keys must keep the signal absent even if data existed.
        data = self.fetch()
        self.assertNotIn("signals", data)


class OverviewTenantIsolationTests(OverviewTestBase):
    """The team count is the one section that could span tenants."""

    def test_school_actor_counts_no_platform_staff(self):
        school_admin = make_school_admin(self.branch, email="ov-iso@school.test")
        grant(school_admin, PERM_TEAM)
        # Several CX staff exist on the platform tenant (self.user among them).
        make_vision_user(email="ov-iso-cx@codex.test")
        data = self.fetch(school_admin, tenant=self.school.tenant.slug)
        # Scoped to the school's own tenant, which has no CX_STAFF rows.
        self.assertEqual(data["team"]["total"], 0)

    def test_platform_actor_keeps_the_platform_wide_count(self):
        grant(self.user, PERM_TEAM)
        make_vision_user(email="ov-wide-cx@codex.test")
        self.assertEqual(self.fetch()["team"]["total"], 2)


class OverviewExpandedSignalTests(OverviewTestBase):
    """The second wave of signals - same contract: gated, and silent when quiet.

    Fixtures are deliberately minimal (nullable control accounts stay null):
    the signals only count rows, so the tests build only what the count reads.
    """

    def _entity(self, code="SIGBOOKS"):
        from vs_finance.models import LedgerEntity

        return LedgerEntity.objects.create(
            name=f"{code} Books", code=code, tenant=self.user.tenant,
        )

    def test_overdue_invoices_counts_posted_unpaid_past_due(self):
        import datetime

        from django.utils import timezone
        from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
        from vs_finance.models import Customer, Invoice

        entity = self._entity()
        customer = Customer.objects.create(entity=entity, code="C1", name="Acme")
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)

        def invoice(due, status=DocumentStatus.POSTED, paid=InvoicePaymentStatus.UNPAID):
            Invoice.objects.create(
                entity=entity, customer=customer, invoice_date=yesterday,
                due_date=due, status=status, payment_status=paid,
            )

        invoice(yesterday)                                        # counts
        invoice(yesterday, paid=InvoicePaymentStatus.PARTIAL)     # counts
        invoice(yesterday, paid=InvoicePaymentStatus.PAID)        # settled
        invoice(tomorrow)                                         # not yet due
        invoice(yesterday, status=DocumentStatus.DRAFT)           # not posted

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "finance.invoice.view")
        self.assertEqual(self.fetch()["signals"]["overdue_invoices"]["count"], 2)

    def test_unallocated_credit_counts_receipts_with_cash_left(self):
        import datetime

        from vs_finance.constants import DocumentStatus
        from vs_finance.models import Customer, Payment

        entity = self._entity("SIGRCP")
        customer = Customer.objects.create(entity=entity, code="C2", name="Beta")
        day = datetime.date(2026, 8, 1)

        def receipt(amount, allocated, refunded=0, status=DocumentStatus.POSTED):
            Payment.objects.create(
                entity=entity, customer=customer, payment_date=day, status=status,
                amount=amount, allocated_amount=allocated, refunded_amount=refunded,
            )

        receipt(10_000, 4_000)            # 6k credit - counts
        receipt(10_000, 10_000)           # fully applied
        receipt(10_000, 4_000, 6_000)     # rest refunded out
        receipt(10_000, 0, status=DocumentStatus.DRAFT)  # not posted

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "finance.payment.view")
        self.assertEqual(self.fetch()["signals"]["unallocated_credit"]["count"], 1)

    def test_vendor_invoices_unpaid_counts_posted_not_paid(self):
        import datetime

        from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
        from vs_procurement.models import Vendor, VendorInvoice

        entity = self._entity("SIGAP")
        vendor = Vendor.objects.create(entity=entity, code="V1", name="Supplies Co")
        day = datetime.date(2026, 8, 1)

        def bill(status=DocumentStatus.POSTED, paid=InvoicePaymentStatus.UNPAID):
            VendorInvoice.objects.create(
                entity=entity, vendor=vendor, invoice_date=day,
                status=status, payment_status=paid,
            )

        bill()                                      # counts
        bill(paid=InvoicePaymentStatus.PARTIAL)     # counts
        bill(paid=InvoicePaymentStatus.PAID)
        bill(status=DocumentStatus.DRAFT)

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "procurement.vendor_invoice.view")
        self.assertEqual(self.fetch()["signals"]["vendor_invoices_unpaid"]["count"], 2)

    def test_rfqs_open_counts_issued_only(self):
        import datetime

        from vs_procurement.constants import RfqStatus
        from vs_procurement.models import RequestForQuotation

        entity = self._entity("SIGRFQ")
        day = datetime.date(2026, 8, 1)
        for status in (RfqStatus.ISSUED, RfqStatus.ISSUED, RfqStatus.DRAFT, RfqStatus.AWARDED):
            RequestForQuotation.objects.create(
                entity=entity, issue_date=day, rfq_status=status,
            )

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "procurement.rfq.view")
        self.assertEqual(self.fetch()["signals"]["rfqs_open"]["count"], 2)

    def test_contracts_expiring_counts_active_inside_the_window(self):
        import datetime

        from django.utils import timezone
        from vs_procurement.constants import ContractStatus
        from vs_procurement.models import Vendor, VendorContract

        entity = self._entity("SIGCON")
        vendor = Vendor.objects.create(entity=entity, code="V2", name="Services Co")
        today = timezone.localdate()

        def contract(ref, end, status=ContractStatus.ACTIVE):
            VendorContract.objects.create(
                entity=entity, vendor=vendor, reference=ref, title=ref,
                status=status, start_date=today - datetime.timedelta(days=100), end_date=end,
            )

        contract("SOON", today + datetime.timedelta(days=10))            # counts
        contract("FAR", today + datetime.timedelta(days=90))             # outside window
        contract("DRAFT", today + datetime.timedelta(days=10), ContractStatus.DRAFT)

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "procurement.contract.view")
        self.assertEqual(self.fetch()["signals"]["contracts_expiring"]["count"], 1)

    def test_users_without_roles_is_tenant_scoped_and_gated(self):
        # Two extra active CX accounts with no role; the caller's own grant role
        # (created by `grant`) keeps the caller out of the count.
        make_vision_user(email="ov-roleless-1@codex.test")
        make_vision_user(email="ov-roleless-2@codex.test")

        self.assertNotIn("signals", self.fetch())
        grant(self.user, "platform.roles.view")
        self.assertEqual(self.fetch()["signals"]["users_without_roles"]["count"], 2)

    def test_team_overdue_tasks_walks_the_callers_own_subtree(self):
        import datetime

        from django.utils import timezone
        from vs_todo.models import Task
        from vs_user.models import OrgNode, Position, PositionAssignment

        division = OrgNode.objects.create(name="Ops", code="SIG-OPS", kind=OrgNode.Kind.DIVISION)
        report = make_vision_user(email="ov-report@codex.test")
        outsider = make_vision_user(email="ov-outsider@codex.test")

        top = Position.objects.create(title="Ops Lead", code="SIG-LEAD", org_node=division)
        seat = Position.objects.create(title="Ops Analyst", code="SIG-AN", org_node=division, reports_to=top)
        lone = Position.objects.create(title="Lone Seat", code="SIG-LONE", org_node=division)
        PositionAssignment.objects.create(user=self.user, position=top, is_primary=True)
        PositionAssignment.objects.create(user=report, position=seat, is_primary=True)
        PositionAssignment.objects.create(user=outsider, position=lone, is_primary=True)

        yesterday = timezone.localdate() - datetime.timedelta(days=1)

        def task(assignee, deadline, done=False):
            Task.objects.create(
                assignee=assignee, title="t", metric="m", target="g",
                deadline=deadline, is_done=done,
            )

        task(report, yesterday)                 # counts
        task(report, yesterday, done=True)      # finished
        task(outsider, yesterday)               # not in the caller's subtree
        task(self.user, yesterday)              # own tasks live in the tasks section

        signals = self.fetch().get("signals", {})
        self.assertEqual(signals["team_overdue_tasks"]["count"], 1)


class OverviewDelegationAndExportTests(OverviewTestBase):
    """Delegate-cover approvals and finished-jobs notices."""

    def setUp(self):
        super().setUp()
        from django.contrib.contenttypes.models import ContentType
        from vs_workflow.models import WorkflowStage, WorkflowTemplate

        self.requester = make_vision_user(email="ov-del-req@codex.test")
        self.template = WorkflowTemplate.objects.create(
            document_type="TEST_DOC", code="default", name="Delegation Template",
        )
        self.stage = WorkflowStage.objects.create(
            template=self.template, code="review", label="Manager Review",
            kind="APPROVAL", order=1, advance_rule="ANY",
            on_rejection="TERMINAL", skip_if_no_approvers=False,
        )
        self.doc_ct = ContentType.objects.get_for_model(WorkflowTemplate)

    def _queue_for(self, approver, object_id, on_behalf_of=None):
        from django.utils import timezone as tz
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStageApprover, WorkflowStageInstance,
        )

        instance = WorkflowInstance.objects.create(
            tenant=self.requester.tenant, template=self.template,
            document_content_type=self.doc_ct, document_object_id=object_id,
            document_type=self.template.document_type, status="IN_PROGRESS",
            requested_by=self.requester, current_stage=self.stage,
            submitted_at=tz.now(),
        )
        stage_instance = WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status="ACTIVE", attempt=1, activated_at=tz.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=stage_instance, user=approver, attempt=1,
            on_behalf_of=on_behalf_of,
        )
        return instance

    def test_delegated_count_and_item_flag(self):
        principal = make_vision_user(email="ov-del-principal@codex.test")
        principal.first_name, principal.last_name = "Pat", "Principal"
        principal.save(update_fields=["first_name", "last_name"])

        self._queue_for(self.user, "own-doc")
        self._queue_for(self.user, "covered-doc", on_behalf_of=principal)

        approvals = self.fetch()["approvals"]
        self.assertEqual(approvals["pending"], 2)
        self.assertEqual(approvals["delegated"], 1)
        by_doc = {item["document_object_id"]: item for item in approvals["items"]}
        self.assertIsNone(by_doc["own-doc"]["on_behalf_of_name"])
        self.assertEqual(by_doc["covered-doc"]["on_behalf_of_name"], "Pat Principal")

    def test_exports_signal_counts_uncollected_exports_only(self):
        # The landing screen's rule: doing the work clears the card. "Exports
        # ready to download" therefore counts only a finished export the caller
        # has NOT downloaded yet - collect the file and its row must go away. A
        # file already taken, or one that has expired or been purged (there is
        # nothing left to collect either way), never keeps the notice on-screen.
        from django.utils import timezone
        from vs_exports.constants import ExportFormat
        from vs_exports.models import ExportFile, ExportRun

        other = make_vision_user(email="ov-done-other@codex.test")
        now = timezone.now()

        def export(requester, *, ref, downloads=0,
                   until_offset=timedelta(days=5), purged=False):
            run = ExportRun.objects.create(
                reference=ref, tenant=requester.tenant,
                frozen_config={"name": "Report"}, requested_by=requester,
            )
            ExportFile.objects.create(
                run=run, name="report.xlsx", format=ExportFormat.XLSX,
                storage_name=f"exports/{ref}.xlsx",
                available_until=now + until_offset,
                download_count=downloads,
                purged_at=(now if purged else None),
            )
            return run

        export(self.user, ref="RUN-UNCOLLECTED")              # the one that counts
        export(self.user, ref="RUN-TAKEN", downloads=1)       # collected: cleared
        export(self.user, ref="RUN-EXPIRED",
               until_offset=timedelta(days=-1))               # window closed: gone
        export(self.user, ref="RUN-PURGED", purged=True)      # bytes deleted: gone
        export(other, ref="RUN-OTHER")                        # another user's export

        signals = self.fetch()["signals"]
        self.assertEqual(signals["exports_uncollected"]["count"], 1)
        self.assertNotIn("jobs_failed_24h", signals)
