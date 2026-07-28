"""One request for the console landing screen (`/overview`).

The screen shows eight numbers that used to be eight endpoints. They were already
issued in parallel, so the cost was never their sum — the win here is elsewhere:

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

The endpoint itself needs nothing but an active account — this is the landing
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

# The landing screen lists only the next few commitments; the rest live on the
# Tasks screen behind "View all".
MY_TASKS_LIMIT = 3


def _schools() -> dict:
    """Active-school count — the conditional aggregate SchoolStatsView uses."""
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
    ``stats_for`` already materialised — no second query.
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
    """Decisions waiting on the caller — shares the queue screen's own rules."""
    from vs_workflow.services import my_queue as my_queue_svc

    return {"pending": my_queue_svc.pending_approval_count(user, school)}


def _submissions(user, school) -> dict:
    """The caller's own submissions that came back for changes."""
    from vs_workflow.models import WorkflowInstance

    qs = WorkflowInstance.objects.filter(requested_by=user, status="RETURNED")
    if school is not None:
        qs = qs.filter(tenant=school.tenant)
    return {"returned": qs.count()}


def _notifications(user) -> dict:
    """Unread in-app count — the same query behind the bell badge."""
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
    """Service-derived posture only — not the whole Command Center payload."""
    from vs_health import services as health_svc

    posture = health_svc.overall_posture()
    return {
        "label": posture["label"],
        "overall": posture["overall"],
        "active_incidents": posture["active_incidents"],
    }


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

    # Own queue and own submissions — no key beyond an active account, matching
    # the dashboard endpoints they replace.
    data["approvals"] = _approvals(user, school)
    data["submissions"] = _submissions(user, school)
    data["notifications"] = _notifications(user)

    from vs_tickets.services.visibility import is_support_user

    if is_support_user(user) or has_permission(user, PERM_TICKETS_VIEW, tenant=tenant):
        data["tickets"] = _tickets(user)

    if has_permission(user, PERM_HEALTH_VIEW, tenant=tenant):
        data["health"] = _health()

    return data
