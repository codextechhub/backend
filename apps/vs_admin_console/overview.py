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
    setup.roles_assigned
                   platform.roles.view OR school.roles.view  (RoleViewSet)
    setup.organogram_built
                   platform.organogram.view   (OrgNodeViewSet)

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


def _approvals(user, school) -> dict:
    """Decisions waiting on the caller - shares the queue screen's own rules.

    The count already materialises the snapshot list (the already-acted and
    stale-attempt filters cannot be pushed into SQL), so the worklist items the
    dashboard renders come from that same list for free. One extra ``in_bulk``
    resolves requester names; everything else is on rows already fetched.
    """
    from vs_user.models import User
    from vs_workflow.services import my_queue as my_queue_svc

    snaps = my_queue_svc.pending_approval_snapshots(user, school)
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

    requester_ids = {
        snap.stage_instance.instance.requested_by_id
        for snap in top
        if snap.stage_instance.instance.requested_by_id
    }
    requesters = User.objects.in_bulk(requester_ids) if requester_ids else {}

    def requester_name(instance) -> str:
        person = requesters.get(instance.requested_by_id)
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
            "requested_by_name": requester_name(instance),
        })

    return {"pending": len(snaps), "items": items}


def _submissions(user, school) -> dict:
    """The caller's own submissions that came back for changes."""
    from vs_workflow.models import WorkflowInstance

    qs = WorkflowInstance.objects.filter(requested_by=user, status="RETURNED")
    if school is not None:
        qs = qs.filter(tenant=school.tenant)

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
    """Open tickets, inside the same visibility boundary as the ticket list."""
    from vs_tickets.constants import TicketStatus
    from vs_tickets.services import visibility

    row = visibility.visible_tickets_qs(user).aggregate(
        open=Count("id", filter=Q(status=TicketStatus.OPEN)),
        assigned_to_me=Count("id", filter=Q(assignee=user)),
    )
    return {"open": row["open"] or 0, "assigned_to_me": row["assigned_to_me"] or 0}


def _health() -> dict:
    """Service-derived posture only - not the whole Command Center payload."""
    from vs_health import services as health_svc

    posture = health_svc.overall_posture()
    return {
        "label": posture["label"],
        "overall": posture["overall"],
        "active_incidents": posture["active_incidents"],
    }


def _setup(user, tenant) -> dict:
    """Whether the structural setup steps behind the checklist are done.

    Booleans, not counts, and deliberately so: the checklist renders a tick, and
    a count would hand the caller a number from a screen they may not be allowed
    to open. Each flag carries the same key as the screen it describes and is
    omitted - not returned False - when the caller lacks it, so "not set up" and
    "not your business" stay distinguishable, exactly like the sections above.

    `.exists()` is a LIMIT 1 on an indexed column; neither flag walks the table.
    """
    from vs_rbac.models import TenantUserRoleAssignment
    from vs_user.models import OrgNode

    setup: dict = {}

    if any(has_permission(user, key, tenant=tenant) for key in PERM_ROLE_VIEW_KEYS):
        # Scoped to the caller's own tenant: whether *another* tenant has
        # assigned roles is not an answer this screen is entitled to.
        setup["roles_assigned"] = TenantUserRoleAssignment.objects.filter(
            tenant=tenant or user.tenant,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).exists()

    if has_permission(user, PERM_ORGANOGRAM_VIEW, tenant=tenant):
        # The CX org tree is platform-wide (OrgNode carries no tenant), so this
        # is unscoped by design, like the organogram screen itself.
        setup["organogram_built"] = OrgNode.objects.filter(is_active=True).exists()

    return setup


def console_overview(request) -> dict:
    """Assemble every section the caller is allowed to see."""
    user = request.user
    tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)
    school = getattr(request, "_cached_school", None)

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
    data["approvals"] = _approvals(user, school)
    data["submissions"] = _submissions(user, school)
    data["notifications"] = _notifications(user)

    from vs_tickets.services.visibility import is_support_user

    if is_support_user(user) or has_permission(user, PERM_TICKETS_VIEW, tenant=tenant):
        data["tickets"] = _tickets(user)

    if has_permission(user, PERM_HEALTH_VIEW, tenant=tenant):
        data["health"] = _health()

    # Only sent when at least one flag is visible, so the screen can treat an
    # absent `setup` the same way it treats every other absent section.
    setup = _setup(user, tenant)
    if setup:
        data["setup"] = setup

    return data
