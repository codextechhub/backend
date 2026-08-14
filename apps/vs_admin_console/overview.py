"""One request for the console landing screen (`/overview`).

The screen shows eight numbers that used to be eight endpoints. They were already
issued in parallel, so the cost was never their sum - the win here is elsewhere:

  * one authentication, tenant resolution and permission evaluation instead of
    eight;
  * one browser round trip instead of eight against a connection pool, which is
    what a user on a high-latency link actually feels;
  * two of those endpoints returned an entire dashboard so the screen could read
    a single field. ``/health/overview/`` serialises the service grid, request
    series, deployments, queues and up to ten incidents to display a posture
    label; ``/todo/dashboard/mine/`` serialises every task the person owns to
    show three. Here each section computes only what the screen renders.

## Permissions

The endpoint itself needs nothing but an active account - this is the landing
screen, everybody gets one. Each *section* is then gated by the same permission
its own endpoint enforces, so this cannot become a back door to a number the
caller could not otherwise fetch:

    schools        platform.schools.view      (SchoolStatsView)
    team           platform.team.view         (UserAccountViewSet list)
    tickets        support desk OR tickets.ticket.view
                                              (HasTicketRBACPermission)
    health         platform.health.view       (HealthViewMixin)
    tasks          CX staff only              (IsVisionStaff on vs_todo)
    approvals      own queue, any active user
    submissions    own submissions, any active user
    notifications  own unread count, any active user

A section the caller may not see is **omitted from the response**, never returned
as zero: `0` and "you have no access" must not look the same to the reader, and
the frontend already hides those cards behind the matching permission key.
"""

from __future__ import annotations

from django.db.models import Count, Q

from vs_rbac.evaluator import has_permission

PERM_SCHOOLS_VIEW = "platform.schools.view"
PERM_TEAM_VIEW = "platform.team.view"
PERM_TICKETS_VIEW = "tickets.ticket.view"
PERM_HEALTH_VIEW = "platform.health.view"
PERM_ORGANOGRAM_VIEW = "platform.organogram.view"
# Both vocabularies, matching RoleViewSet's own ROLE_VIEW_KEYS.
PERM_ROLE_VIEW_KEYS = ("platform.roles.view", "school.roles.view")

# The landing screen lists only the next few commitments; the rest live on the
# Tasks screen behind "View all".
MY_TASKS_LIMIT = 3

# Same idea for the worklist: the dashboard shows the few decisions and returned
# submissions a person should act on next; the full queues stay on their screens.
APPROVAL_ITEMS_LIMIT = 5
RETURNED_ITEMS_LIMIT = 3


def _schools() -> dict:
    """Active-school count - the conditional aggregate SchoolStatsView uses."""
    from vs_schools.models import School, SchoolStatus

    row = School.objects.aggregate(
        active=Count("slug", filter=Q(status=SchoolStatus.ACTIVE)),
    )
    return {"active": row["active"] or 0}


def _team(user, tenant) -> dict:
    """Active CX staff, under the same tenant boundary as the user list.

    Platform-kind actors keep the platform-wide view (their RBAC key is the
    gate); everyone else is scoped to the asserted request tenant, so a school
    admin cannot read another tenant's headcount.
    """
    from vs_tenants.models import Tenant
    from vs_user.models import User

    qs = User.objects.filter(
        user_type=User.UserType.CX_STAFF,
        status=User.Status.ACTIVE,
    )
    if getattr(getattr(user, "tenant", None), "kind", None) != Tenant.Kind.PLATFORM:
        qs = qs.filter(tenant=tenant or user.tenant)
    return {"total": qs.count()}


def _tasks(user) -> dict:
    """Own-task headline plus the few the panel lists.

    Ordering mirrors what the screen used to do client-side: overdue first, then
    by priority, then by nearest deadline. Sorted in Python over the list that
    ``stats_for`` already materialised - no second query.
    """
    from vs_todo.serializers import TaskSerializer
    from vs_todo.services.stats import own_tasks_qs, stats_for

    tasks = list(own_tasks_qs(user).select_related("assignee", "assigned_by"))
    stats = stats_for(tasks)

    def rank(task):
        if task.status == "OVERDUE":
            return 0
        return {"HIGH": 1, "MEDIUM": 2}.get(task.priority, 3)

    active = sorted(
        (task for task in tasks if not task.is_done),
        key=lambda task: (rank(task), task.deadline),
    )[:MY_TASKS_LIMIT]

    return {
        "stats": stats,
        "items": TaskSerializer(active, many=True).data,
    }


def _approvals(user, tenant) -> dict:
    """Decisions waiting on the caller - shares the queue screen's own rules.

    The count already materialises the snapshot list (the already-acted and
    stale-attempt filters cannot be pushed into SQL), so the worklist items the
    dashboard renders come from that same list for free. One extra ``in_bulk``
    resolves requester names; everything else is on rows already fetched.
    """
    from vs_user.models import User
    from vs_workflow.services import my_queue as my_queue_svc

    snaps = my_queue_svc.pending_approval_snapshots(user, tenant)
    # The queue screen lists newest first; the worklist is the opposite. It
    # surfaces the decisions that have waited longest, so a lingering item can
    # never hide below the cap while fresh arrivals fill the visible rows.
    top = sorted(
        snaps,
        key=lambda snap: (
            snap.stage_instance.activated_at is None,
            snap.stage_instance.activated_at,
        ),
    )[:APPROVAL_ITEMS_LIMIT]

    # One in_bulk covers requesters and the people delegates are covering for.
    name_ids = {
        snap.stage_instance.instance.requested_by_id
        for snap in top
        if snap.stage_instance.instance.requested_by_id
    } | {snap.on_behalf_of_id for snap in top if snap.on_behalf_of_id}
    people = User.objects.in_bulk(name_ids) if name_ids else {}

    def person_name(pk) -> str:
        person = people.get(pk)
        if person is None:
            return ""
        return person.full_name or person.email

    items = []
    for snap in top:
        instance = snap.stage_instance.instance
        items.append({
            # The workflow-instance id: what APPROVAL_DETAIL(id) opens.
            "id": str(instance.id),
            "document_type": instance.document_type,
            "document_object_id": instance.document_object_id,
            "stage_label": snap.stage_instance.stage.label,
            "awaiting_since": snap.stage_instance.activated_at,
            "requested_by_name": person_name(instance.requested_by_id),
            # Set when the caller is a delegate covering this decision for
            # someone else - the dashboard splits these into their own box.
            "on_behalf_of_name": person_name(snap.on_behalf_of_id) if snap.on_behalf_of_id else None,
        })

    delegated = sum(1 for snap in snaps if snap.on_behalf_of_id)
    return {"pending": len(snaps), "delegated": delegated, "items": items}


def _submissions(user, tenant) -> dict:
    """The caller's own submissions that came back for changes."""
    from vs_workflow.models import WorkflowInstance

    qs = WorkflowInstance.objects.filter(requested_by=user, status="RETURNED")
    if tenant is not None:
        qs = qs.filter(tenant=tenant)

    items = [
        {
            "id": str(row.id),
            "document_type": row.document_type,
            "document_object_id": row.document_object_id,
            "returned_at": row.updated_at,
        }
        for row in qs.order_by("-updated_at")[:RETURNED_ITEMS_LIMIT]
    ]

    return {"returned": qs.count(), "items": items}


def _notifications(user) -> dict:
    """Unread in-app count - the same query behind the bell badge."""
    from vs_notifications.constants import ChannelChoices
    from vs_notifications.models import Notification

    return {
        "unread": Notification.objects.filter(
            recipient=user,
            channel=ChannelChoices.IN_APP,
            is_read=False,
        ).count()
    }


def _tickets(user) -> dict:
    """Unfinished tickets, inside the same visibility boundary as the list.

    Both numbers count live work only (ACTIVE_TICKET_STATUSES): the landing
    screen's contract is that acting on something makes it go away, so a
    resolved or closed ticket may never keep a card on the page.
    """
    from vs_tickets.constants import ACTIVE_TICKET_STATUSES
    from vs_tickets.services import visibility

    row = (
        visibility.visible_tickets_qs(user)
        .filter(status__in=ACTIVE_TICKET_STATUSES)
        .aggregate(
            active=Count("id"),
            assigned_to_me=Count("id", filter=Q(assignee=user)),
        )
    )
    return {"active": row["active"] or 0, "assigned_to_me": row["assigned_to_me"] or 0}


def _health() -> dict:
    """Service-derived posture only - not the whole Command Center payload."""
    from vs_health import services as health_svc

    posture = health_svc.overall_posture()
    return {
        "label": posture["label"],
        "overall": posture["overall"],
        "active_incidents": posture["active_incidents"],
    }


PERM_FINANCE_REPORT_VIEW = "finance.report.view"
PERM_FINANCE_JOURNAL_VIEW = "finance.journal.view"
PERM_FINANCE_INVOICE_VIEW = "finance.invoice.view"
PERM_FINANCE_PAYMENT_VIEW = "finance.payment.view"
PERM_PO_VIEW = "procurement.purchase_order.view"
PERM_VENDOR_INVOICE_VIEW = "procurement.vendor_invoice.view"
PERM_RFQ_VIEW = "procurement.rfq.view"
PERM_CONTRACT_VIEW = "procurement.contract.view"
PERM_WEBHOOK_VIEW = "payments.webhook.view"

SIGNAL_WINDOW_HOURS = 24
#: An ACTIVE vendor contract ending inside this window is "expiring".
CONTRACT_EXPIRY_DAYS = 30


def _signals(user, tenant) -> dict:
    """Module signals: conditions someone should act on soon.

    Same contract as every other section, applied twice over: a signal is
    omitted when the caller lacks the key of the screen it points at, AND when
    there is nothing to act on - a healthy signal is silence, not a green card.
    The frontend renders only what arrives, so quiet days cost no screen space.
    """
    from django.utils import timezone

    signals: dict = {}
    since = timezone.now() - timezone.timedelta(hours=SIGNAL_WINDOW_HOURS)

    if has_permission(user, PERM_FINANCE_REPORT_VIEW, tenant=tenant):
        # Worst fiscal runway across active entities - the same computation the
        # finance dashboard banner uses, read entity-by-entity (entities are the
        # org's own few ledgers, so this stays a handful of LIMIT-1 queries).
        from vs_finance.models import LedgerEntity
        from vs_finance.posting import FISCAL_RUNWAY_HEALTHY, fiscal_calendar_runway

        worst = None
        for entity in LedgerEntity.objects.filter(is_active=True):
            runway = fiscal_calendar_runway(entity)
            if runway["status"] == FISCAL_RUNWAY_HEALTHY:
                continue
            # None days_remaining means "no calendar at all" - rank it worst.
            rank = runway["days_remaining"] if runway["days_remaining"] is not None else -(10 ** 6)
            if worst is None or rank < worst["rank"]:
                worst = {
                    "rank": rank,
                    "entity_name": entity.name,
                    "status": runway["status"],
                    "days_remaining": runway["days_remaining"],
                    "calendar_end": runway["calendar_end"],
                }
        if worst is not None:
            worst.pop("rank")
            signals["fiscal_runway"] = worst

    if has_permission(user, PERM_FINANCE_JOURNAL_VIEW, tenant=tenant):
        from vs_finance.constants import DocumentStatus
        from vs_finance.models import JournalEntry

        drafts = JournalEntry.objects.filter(status=DocumentStatus.DRAFT).count()
        if drafts:
            signals["draft_journals"] = {"count": drafts}

    if has_permission(user, PERM_PO_VIEW, tenant=tenant):
        # Issued orders still awaiting (full) delivery - the PO console's own
        # open-orders rule: drafts and in-approval orders are not commitments,
        # and receipt progress is derived from line quantities via the shared
        # stage helper so this number always matches the PO list.
        from django.db.models import Q, Sum

        from vs_finance.constants import DocumentStatus as FinDocStatus
        from vs_procurement.constants import ProcApprovalState
        from vs_procurement.models import PurchaseOrder
        from vs_procurement.purchasing import po_receipt_stage

        rows = (
            PurchaseOrder.objects
            .exclude(status__in=(
                FinDocStatus.CANCELLED, FinDocStatus.REVERSED,
                FinDocStatus.DRAFT, FinDocStatus.PENDING_APPROVAL,
            ))
            .exclude(approval_state=ProcApprovalState.PENDING)
            .annotate(ordered_qty=Sum("lines__quantity"), received_qty=Sum("lines__received_qty"))
            .values_list("ordered_qty", "received_qty")
        )
        awaiting = sum(
            1 for ordered, received in rows
            if po_receipt_stage(ordered, received) != "RECEIVED"
        )
        if awaiting:
            signals["pos_awaiting_receipt"] = {"count": awaiting}

    if has_permission(user, PERM_WEBHOOK_VIEW, tenant=tenant):
        from vs_payments.constants import WebhookStatus
        from vs_payments.models import WebhookEvent

        failures = WebhookEvent.objects.filter(
            status=WebhookStatus.FAILED, created_at__gte=since,
        ).count()
        if failures:
            signals["webhook_failures_24h"] = {"count": failures}

    if has_permission(user, PERM_FINANCE_INVOICE_VIEW, tenant=tenant):
        # Posted invoices past due and not fully settled - dunning pressure.
        from vs_finance.constants import DocumentStatus, InvoicePaymentStatus
        from vs_finance.models import Invoice

        overdue = (
            Invoice.objects.filter(
                status=DocumentStatus.POSTED,
                due_date__lt=timezone.localdate(),
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .count()
        )
        if overdue:
            signals["overdue_invoices"] = {"count": overdue}

    if has_permission(user, PERM_FINANCE_PAYMENT_VIEW, tenant=tenant):
        # Receipts whose cash is still sitting as customer credit (2140):
        # amount beyond what's been allocated to invoices or refunded back out.
        from django.db.models import F

        from vs_finance.constants import DocumentStatus
        from vs_finance.models import Payment

        idle = Payment.objects.filter(
            status=DocumentStatus.POSTED,
            amount__gt=F("allocated_amount") + F("refunded_amount"),
        ).count()
        if idle:
            signals["unallocated_credit"] = {"count": idle}

    if has_permission(user, PERM_VENDOR_INVOICE_VIEW, tenant=tenant):
        # Posted vendor bills not yet fully paid - payables waiting on cash.
        from vs_finance.constants import DocumentStatus as FinStatus
        from vs_finance.constants import InvoicePaymentStatus as PayStatus
        from vs_procurement.models import VendorInvoice

        unpaid = (
            VendorInvoice.objects.filter(status=FinStatus.POSTED)
            .exclude(payment_status=PayStatus.PAID)
            .count()
        )
        if unpaid:
            signals["vendor_invoices_unpaid"] = {"count": unpaid}

    if has_permission(user, PERM_RFQ_VIEW, tenant=tenant):
        # Sourcing sitting open: issued RFQs that have not been awarded/closed.
        from vs_procurement.constants import RfqStatus
        from vs_procurement.models import RequestForQuotation

        open_rfqs = RequestForQuotation.objects.filter(rfq_status=RfqStatus.ISSUED).count()
        if open_rfqs:
            signals["rfqs_open"] = {"count": open_rfqs}

    if has_permission(user, PERM_CONTRACT_VIEW, tenant=tenant):
        from vs_procurement.constants import ContractStatus
        from vs_procurement.models import VendorContract

        horizon = timezone.localdate() + timezone.timedelta(days=CONTRACT_EXPIRY_DAYS)
        expiring = VendorContract.objects.filter(
            status=ContractStatus.ACTIVE, end_date__lte=horizon,
        ).count()
        if expiring:
            signals["contracts_expiring"] = {"count": expiring}

    if any(has_permission(user, key, tenant=tenant) for key in PERM_ROLE_VIEW_KEYS):
        # Access hygiene: active accounts in the caller's tenant holding no
        # active role. Scoped to the caller's own tenant like the roles screen.
        from vs_rbac.models import TenantUserRoleAssignment
        from vs_user.models import User

        scope = tenant or user.tenant
        assigned = TenantUserRoleAssignment.objects.filter(
            tenant=scope,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).values("user_id")
        roleless = (
            User.objects.filter(tenant=scope, is_active=True)
            .exclude(id__in=assigned)
            .count()
        )
        if roleless:
            signals["users_without_roles"] = {"count": roleless}

    if getattr(user, "user_type", None) == "CX_STAFF":
        # A manager's reports with overdue tasks - own subtree, so the gate is
        # the same hierarchy that scopes the team dashboard, not a key.
        from vs_todo.models import Task
        from vs_todo.services.hierarchy import TodoHierarchy

        reports = [u for u in TodoHierarchy.descendant_users(user) if u.pk != user.pk]
        if reports:
            team_overdue = Task.objects.filter(
                assignee__in=reports, is_done=False,
                deadline__lt=timezone.localdate(),
            ).count()
            if team_overdue:
                signals["team_overdue_tasks"] = {"count": team_overdue}

    # Own background jobs that failed recently - own rows, so no key beyond an
    # active account, exactly like approvals/submissions above. Successes are
    # already toasted live by the queues poller; failures are what linger.
    from core.models import BackgroundJob

    failed_jobs = BackgroundJob.objects.filter(
        owner=user, status=BackgroundJob.Status.FAILED, finished_at__gte=since,
    ).count()
    if failed_jobs:
        signals["jobs_failed_24h"] = {"count": failed_jobs}

    # Finished work still waiting to be collected. The landing screen's one rule
    # is that acting on something clears its row, so this may only count outputs
    # the caller has NOT yet downloaded - a plain "SUCCEEDED in the last 24h"
    # count sat on the screen for a full day whether or not the person collected
    # the file, which is precisely the counter-that-cannot-be-cleared this screen
    # forbids. Scoped to exports because they are the one job kind that produces a
    # downloadable artefact (imports, emails and system jobs leave nothing to
    # collect), which is also what the notice's "ready to download" copy promises.
    #
    # A file is countable only while it is genuinely collectable: not yet
    # downloaded, not purged, and not past its availability window. An expired or
    # purged file is dropped rather than nagged about, because there is no longer
    # anything to download - the honest state there is silence, not a dead link.
    # Attribution is by the run's requester (the actor who asked for the export),
    # which is always set, indexed, and independent of whether the async job row
    # still exists.
    #
    # Cost: one COUNT with a two-table join (ExportFile -> ExportRun), driven by
    # the requested_by index and the file's unique run FK. No per-row work, no
    # Python loop, and the result set is bounded by one person's own live exports.
    from vs_exports.models import ExportFile

    uncollected_exports = ExportFile.objects.filter(
        run__requested_by=user,
        download_count=0,
        purged_at__isnull=True,
        available_until__gt=timezone.now(),
    ).count()
    if uncollected_exports:
        signals["exports_uncollected"] = {"count": uncollected_exports}

    return signals


def console_overview(request) -> dict:
    """Assemble every section the caller is allowed to see."""
    user = request.user
    tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)

    data: dict = {}

    if has_permission(user, PERM_SCHOOLS_VIEW, tenant=tenant):
        data["schools"] = _schools()

    if has_permission(user, PERM_TEAM_VIEW, tenant=tenant):
        data["team"] = _team(user, tenant)

    # vs_todo is a CX-staff surface (IsVisionStaff), so school users get no
    # tasks section rather than an empty one.
    if getattr(user, "user_type", None) == "CX_STAFF":
        data["tasks"] = _tasks(user)

    # Own queue and own submissions - no key beyond an active account, matching
    # the dashboard endpoints they replace.
    data["approvals"] = _approvals(user, tenant)
    data["submissions"] = _submissions(user, tenant)
    data["notifications"] = _notifications(user)

    from vs_tickets.services.visibility import is_support_user

    if is_support_user(user) or has_permission(user, PERM_TICKETS_VIEW, tenant=tenant):
        data["tickets"] = _tickets(user)

    if has_permission(user, PERM_HEALTH_VIEW, tenant=tenant):
        data["health"] = _health()

    # Module signals follow the same rule: absent when quiet or not permitted.
    signals = _signals(user, tenant)
    if signals:
        data["signals"] = signals

    return data
