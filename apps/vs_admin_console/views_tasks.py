"""Background-task monitoring for the admin console (engine-room view).

Reads core.BackgroundJob - the single source of truth for every Celery run
(written by core.tasks_base.TrackedTask). The owner-facing slice of the same
table lives at /v1/user/me/tasks/.

Three surfaces, three different answers to "who may see this"
-------------------------------------------------------------
The table holds operational metadata AND, for a failure, the text of what went
wrong - which is routinely made of somebody's personal data. Those are not the
same thing to read, so they are no longer the same thing to reach:

``GET /v1/admin/tasks/``            the triage list. Status, task, owner,
                                    tenant, timings. No result, no error, no
                                    traceback: nobody deciding what is on fire
                                    needs fifty tracebacks at once.
                                    Key: ``platform.tasks.view``.

``GET /v1/admin/tasks/<id>/``       one run, with its REDACTED error and
                                    result. Enough to see the shape of the
                                    failure ("duplicate key on email").
                                    Key: ``platform.tasks.view``.

``GET /v1/admin/tasks/<id>/diagnostics/``
                                    the raw, unredacted error and traceback
                                    out of core.TaskDiagnostic. A deliberate
                                    act, on one named row, that writes an
                                    audit event naming who did it.
                                    Key: ``platform.tasks.view_sensitive``.

Tenant scope
------------
The queryset used to ignore ``BackgroundJob.tenant`` entirely, so every reader
got every customer interleaved and a support agent working a Corona ticket had
to page through Greenfield to find it. Now the list is limited to the tenants
the caller actually holds ``platform.tasks.view`` in, and seeing across all of
them takes the separate ``platform.tasks.view_all`` key. ``?for_tenant=<slug|id>``
narrows within whatever that leaves.

List filters:
    ?status=QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED
    ?task=<substring of the task name>     e.g. ?task=import
    ?kind=import|export|email|system
    ?since=YYYY-MM-DD                      created on/after this date
    ?for_tenant=<slug or numeric id>       one customer's runs. NOT ?tenant=,
                                           which is the caller's own tenant
                                           assertion (see core.tenant_filters)
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError

from core.models import BackgroundJob
from core.pagination import XVSPagination
from core.response import success_response
from core.tenant_filters import narrow as narrow_by_tenant
from vs_rbac.permissions import (
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
    user_has_rbac_permission,
)

# Validate a date filter before it reaches the ORM.
def _as_date(value, field):
    """Parse ``?since=`` into a date, or raise the 400 it actually is.

    The raw string used to go straight into ``created_at__date__gte``, where
    ``?since=yesterday`` raised Django's ``ValidationError`` - not a DRF
    exception, so it fell through the handler as a 500 SERVER_ERROR. A
    malformed filter is the caller's mistake and reads as one now.
    """
    parsed = parse_date(value)
    if parsed is None:
        raise DRFValidationError({field: "Must be a date in YYYY-MM-DD form."})
    return parsed


#: The redacted triage surface.
PERM_VIEW = "platform.tasks.view"
#: Every tenant's runs rather than only the caller's own scope.
PERM_VIEW_ALL = "platform.tasks.view_all"
#: The raw, unredacted failure text.
PERM_VIEW_SENSITIVE = "platform.tasks.view_sensitive"


# Work out which customers' task runs this caller is allowed to see at all.
def visible_tenant_ids(user):
    """Return the tenant ids in scope, or ``None`` for "every tenant".

    ``None`` - the platform-wide view - is returned only for a holder of
    ``platform.tasks.view_all``. Everyone else sees their own tenant, which
    for a CX account means Codex's own system runs and no customer's.

    **Why this is not finer-grained.** The obvious shape is "this support
    operator covers Corona and Greenfield", and this RBAC model cannot express
    it. ``platform.tasks.*`` is PLATFORM-scoped, and
    ``vs_rbac.models.assert_tenant_may_hold`` refuses a platform-scoped key
    granted inside a tenant role - so an operator cannot be given
    ``platform.tasks.view`` "inside Corona" the way a bursar is given a school
    key. Reaching for that shape here would mean either relaxing that guard,
    which exists to stop a school granting itself platform powers, or adding a
    second "which schools does this operator cover" table alongside RBAC,
    which is a feature rather than a fix.

    So the split this function draws is the one the model actually supports:
    platform-wide against own-tenant. It still closes the finding - the
    unscoped queryset showed every customer to every reader - and the
    remaining coarseness is recorded rather than hidden.
    """
    home_tenant = getattr(user, "tenant", None)
    if user_has_rbac_permission(user, PERM_VIEW_ALL, tenant=home_tenant):
        return None
    return {home_tenant.pk} if home_tenant is not None else set()


# Shape background job rows for the triage list: metadata only, never payloads.
class AdminJobListSerializer(serializers.ModelSerializer):
    """The list row.

    ``result``, ``error`` and ``traceback`` are deliberately absent. They were
    on this serializer, which meant one scroll of the monitor rendered every
    failing row's full text - and a Postgres duplicate-key error carries the
    duplicated value, so that text is where guardians' email addresses lived.
    ``has_diagnostic`` replaces them: it says a raw record exists to open,
    without being the record.
    """

    owner_name = serializers.SerializerMethodField()
    runtime_seconds = serializers.SerializerMethodField()
    has_diagnostic = serializers.SerializerMethodField()

    class Meta:
        model = BackgroundJob
        fields = [
            "id", "celery_task_id", "task_name", "kind", "label",
            "owner", "owner_name", "tenant", "status", "progress", "worker",
            "created_at", "started_at", "finished_at", "runtime_seconds",
            "has_diagnostic",
        ]

    def get_owner_name(self, obj):
        # Jobs may be system-owned, so owner display is intentionally nullable.
        return obj.owner.full_name if obj.owner_id and obj.owner else None

    def get_runtime_seconds(self, obj):
        if obj.started_at and obj.finished_at:
            return round((obj.finished_at - obj.started_at).total_seconds(), 3)
        # Queued/running jobs do not have a stable runtime yet.
        return None

    def get_has_diagnostic(self, obj):
        # Answered from the prefetched relation, so the list stays one query.
        return bool(getattr(obj, "diagnostic", None))


# One run in full, with the redacted failure text.
class AdminJobDetailSerializer(AdminJobListSerializer):
    """The detail row: everything the list shows, plus the redacted payloads.

    Redacted, not raw. ``core.tasks_base.TrackedTask._finish`` scrubs these two
    columns on the way in, so what is served here is already
    ``Key (email)=([redacted])`` rather than the address itself. The shape of
    the failure survives; the person in it does not.

    ``traceback`` is still not here. Even redacted it is internal file paths
    and framework versions, which is reconnaissance for anyone who later
    compromises an account and nothing an operator reads to triage. It lives
    on the diagnostics action with the rest of the raw record.
    """

    class Meta(AdminJobListSerializer.Meta):
        fields = AdminJobListSerializer.Meta.fields + ["result", "error"]


# Read-only operational view over tracked Celery jobs.
class TaskMonitorViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    """Read-only window onto task history, scoped and redacted by default.

    docstring-name: Task monitor
    """

    permission_classes = [
        IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission
    ]
    pagination_class = XVSPagination

    def get_permissions(self):
        # Per-action rather than per-view: reading the raw traceback is a
        # different power from reading the queue, and one permission_classes
        # line cannot express two keys.
        self.rbac_permission = (
            PERM_VIEW_SENSITIVE if self.action == "diagnostics" else PERM_VIEW
        )
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminJobDetailSerializer
        return AdminJobListSerializer

    def retrieve(self, request, *args, **kwargs):
        """One run, in the platform's standard envelope.

        ``RetrieveModelMixin`` answers with the bare serializer body, which
        would make this the only endpoint in the console whose response is not
        ``{success, message, data}``.
        """
        serializer = self.get_serializer(self.get_object())
        return success_response(
            message="Task run retrieved.", data=serializer.data,
        )

    def get_queryset(self):
        # Newest-first ordering makes incident triage land on the freshest runs.
        qs = (
            BackgroundJob.objects
            .select_related("owner", "diagnostic")
            .order_by("-created_at")
        )
        qs = self._scope_to_visible_tenants(qs)
        return self._apply_filters(qs)

    def _scope_to_visible_tenants(self, qs):
        """Narrow to the tenants the caller may see, then to ``?for_tenant=``.

        The filter is ``?for_tenant=``, never ``?tenant=``. ``?tenant=`` is the
        assertion the authentication layer requires, naming the tenant the
        caller is acting IN, and this viewset does not set
        ``platform_cross_tenant_param`` - so a CodeX operator must assert
        ``codex`` or be refused. Reading that as "whose rows do you want" meant
        a Super Admin holding ``platform.tasks.view_all`` sent ``?tenant=codex``
        like everybody else and had the platform-wide list they hold a CRITICAL
        key for silently collapse to Codex's own system jobs, showing no school
        at all. The same conflation answered 500 on ``/v1/health/tasks/``;
        ``core.tenant_filters`` carries the full account.
        """
        allowed = visible_tenant_ids(self.request.user)
        if allowed is not None:
            qs = qs.filter(tenant_id__in=allowed)
        # An unknown slug narrows to nothing rather than being ignored:
        # silently returning every tenant for a typo is how a filter becomes a
        # leak. A tenant outside `allowed` is already excluded above, so this
        # can never widen the scope either.
        return narrow_by_tenant(qs, self.request.query_params)

    def _apply_filters(self, qs):
        params = self.request.query_params

        status_param = (params.get("status") or "").strip().upper()
        if status_param:
            # Status values are stored uppercase; normalize user-entered filters.
            qs = qs.filter(status=status_param)

        task = (params.get("task") or "").strip()
        if task:
            qs = qs.filter(task_name__icontains=task)

        kind = (params.get("kind") or "").strip().lower()
        if kind:
            qs = qs.filter(kind=kind)

        since = (params.get("since") or "").strip()
        if since:
            qs = qs.filter(created_at__date__gte=_as_date(since, "since"))

        return qs

    # Serve the raw failure text for one run, and record who asked.
    @action(detail=True, methods=["get"])
    def diagnostics(self, request, pk=None):
        """The unredacted error, traceback and result for one failed run.

        Everything narrow about this endpoint is on purpose. It is addressed
        by a single job id rather than listed, so there is no way to sweep the
        store. It needs ``platform.tasks.view_sensitive``, which the seed gives
        to the Super Admin alone. And it emits an audit event before returning,
        so "who read Corona's failed guardian import, and when" has an answer
        for as long as the audit trail is kept.

        ``get_queryset`` still applies, so a caller cannot reach a tenant here
        that they could not have listed.
        """
        job = self.get_object()
        diagnostic = getattr(job, "diagnostic", None)
        if diagnostic is None:
            # Successful runs keep no raw record, and a failure's record is
            # pruned once its retention window closes.
            raise NotFound(
                "No raw diagnostic is stored for this run. Diagnostics are "
                "written for failures only, and are pruned on expiry."
            )

        self._audit_diagnostic_read(job, diagnostic)
        return success_response(
            message="Raw task diagnostic retrieved.",
            data={
                "job": str(job.pk),
                "task_name": diagnostic.task_name,
                "tenant": diagnostic.tenant_id,
                "raw_error": diagnostic.raw_error,
                "raw_traceback": diagnostic.raw_traceback,
                "raw_result": diagnostic.raw_result,
                "recorded_at": diagnostic.created_at,
                "expires_at": diagnostic.expires_at,
            },
        )

    def _audit_diagnostic_read(self, job, diagnostic):
        """Write the audit event for a raw read.

        The event is attributed to the job's tenant, not the reader's: an
        auditor at Corona Secondary School asking who looked at their data is
        asking a question about Corona's trail, and filing the event under
        Codex would put the answer where they cannot see it.
        """
        from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key=AuditModuleKey.PLATFORM,
            action_type=AuditActionType.TASK_DIAGNOSTIC_VIEWED,
            entity_type="BackgroundJob",
            entity_id=str(job.pk),
            entity_label=job.label or job.task_name or "",
            actor_user=getattr(self.request, "actor_user", None) or self.request.user,
            effective_user=self.request.user,
            tenant=diagnostic.tenant,
            severity=AuditSeverity.WARNING,
            summary=(
                f"Raw task diagnostic read for '{job.task_name or job.label}'."
            ),
            metadata={
                "celery_task_id": job.celery_task_id,
                "task_name": job.task_name,
                "job_status": job.status,
            },
        )

    # Summarize task health for the operations dashboard.
    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Status counts (all-time and last 24h) plus a per-task breakdown.

        Built from ``get_queryset`` rather than from ``BackgroundJob.objects``.
        The counts used to be unscoped, which meant an operator restricted to
        one school still read the platform-wide totals - and "recent_failures"
        listed other customers' job labels outright.
        """
        day_ago = timezone.now() - timedelta(hours=24)
        scoped = self.get_queryset()

        # Pair all-time counts with a 24-hour window so regressions stand out.
        by_status = dict(
            scoped.values_list("status").annotate(n=Count("id")).order_by()
        )
        last_24h = dict(
            scoped.filter(created_at__gte=day_ago)
            .values_list("status").annotate(n=Count("id")).order_by()
        )
        by_task = list(
            scoped.values("task_name")
            .annotate(runs=Count("id"))
            .order_by("-runs")[:20]
        )
        failures = list(
            # Recent failures are capped for dashboard readability.
            scoped.filter(status=BackgroundJob.Status.FAILED)
            .order_by("-finished_at")
            .values("task_name", "label", "finished_at", "celery_task_id")[:5]
        )
        return success_response(
            message="Task statistics retrieved.",
            data={
                "by_status": by_status,
                "last_24h": last_24h,
                "by_task": by_task,
                "recent_failures": failures,
                "total": scoped.count(),
            },
        )

    # Expose scheduler configuration so support can confirm beat wiring.
    @action(detail=False, methods=["get"])
    def schedule(self, request):
        """The beat schedule as configured in code, what mode it runs in, and
        when each entry last actually ran.

        Configuration rather than customer data, so it is not tenant-scoped -
        but it still sits behind ``platform.tasks.view`` because it describes
        the platform's internals.

        ``last_run`` is the point of this endpoint rather than a decoration.
        Listing the schedule proves only that somebody wrote it down; a
        deployment in eager mode has a full, correct-looking schedule and has
        never executed one line of it. The two fields that answer whether the
        scheduler is alive are ``last_run`` per entry and ``scheduler_alive``
        below - and because ``TrackedTask`` is the base of every task, the
        answer is already in ``BackgroundJob`` and needs no heartbeat of its own.
        """
        from apps.celery import app as celery_app

        schedule = celery_app.conf.beat_schedule or {}

        # One grouped query for the whole schedule rather than one per entry.
        # ``created_at`` rather than ``finished_at``: a task that started and
        # died still proves the scheduler fired, which is the question here.
        last_by_task = dict(
            BackgroundJob.objects
            .filter(task_name__in={e["task"] for e in schedule.values()})
            .values_list("task_name")
            .annotate(last=Max("created_at"))
        )

        entries = [
            # Stringify schedule objects because beat schedules are not JSON-native.
            {
                "name": name,
                "task": entry["task"],
                "schedule": str(entry["schedule"]),
                "last_run": last_by_task.get(entry["task"]),
            }
            for name, entry in schedule.items()
        ]

        eager = bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))
        return success_response(
            message="Beat schedule retrieved.",
            data={
                "eager_mode": eager,
                "broker_configured": bool(getattr(settings, "CELERY_BROKER_URL", "")),
                # False whenever nothing on the schedule has ever run. In eager
                # mode that is guaranteed, because eager mode has no beat at all -
                # stated as a fact about observed runs rather than inferred from
                # the mode, so a worker that is configured but wedged reads the
                # same as one that was never started.
                "scheduler_alive": bool(last_by_task),
                "never_run": sorted(
                    entry["task"] for entry in schedule.values()
                    if entry["task"] not in last_by_task
                ),
                "entries": entries,
            },
        )
