"""REST API for the Export Centre (mounted at ``/v1/exports/``).

Conventions match the rest of the platform: entity-scoped via ``?entity=<id|code>``
where an entity is meaningful, the ``{success, message, data}`` envelope, RBAC-gated
with ``exports.<resource>.<action>``, and thin views that hand off to
:mod:`vs_exports.services`.

Visibility is the rule worth stating once. A caller sees:

* every definition they own or that has been shared with them;
* every run and file belonging to those definitions, plus any run they triggered;
* everything in their tenant **only** if they hold ``exports.activity.view``, and that
  read is itself written to the audit trail.

Nothing here trusts a pk from the URL: every lookup is filtered by tenant first and
then by the visibility rule, so changing an id in the address bar returns 404 rather
than someone else's export.
"""
from __future__ import annotations

from django.db.models import Q
from django.http import HttpResponse
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from . import analytics, audit, services
from .catalogue import all_datasets, get_dataset, modules
from .constants import (
    AuditAction,
    ExportFormat,
    DownloadOutcome,
    ExportPermission,
    FORMAT_MEDIA,
    RunTrigger,
    ScheduleState,
    Sharing,
    SUCCESSFUL_RUN_STATUSES,
    SYNC_EXPORT_MAX_BYTES,
    TERMINAL_RUN_STATUSES,
    ValuesMode,
)
from .engine import ExportError, estimate, may_export_dataset, plain_sentence, sample_rows
from .models import (
    ExportDefinition,
    ExportDefinitionShare,
    ExportFile,
    ExportRun,
    ExportSchedule,
)
from .serializers import (
    AnalyticsIngestSerializer,
    ExportDefinitionDetailSerializer,
    ExportDefinitionListSerializer,
    ExportDefinitionWriteSerializer,
    ExportDownloadSerializer,
    ExportFileSerializer,
    ExportRunDetailSerializer,
    ExportRunListSerializer,
    ExportScheduleSerializer,
    PreviewSerializer,
    QuickExportSerializer,
    RunRequestSerializer,
    ShareSerializer,
)
from .services import ExportServiceError


# --------------------------------------------------------------------------- #
# Base                                                                        #
# --------------------------------------------------------------------------- #
class _ExportBase(APIView):
    """RBAC-gated base with the platform's pagination envelope and scoping helpers."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def tenant(self):
        """The asserted tenant. Every queryset in this module starts from it."""
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No tenant context on this request.")
        return tenant

    # Paginate a queryset through the platform envelope.
    def paginate(self, request, qs, serializer_cls, **kwargs):
        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            serializer_cls(page, many=True, context={"request": request}, **kwargs).data
        )

    # Whether the caller holds one RBAC key in this tenant.
    def can(self, key: str) -> bool:
        """One place for "does this caller hold key X", used by every finer check.

        ``HasRBACPermission`` already gates the route with the view's ``rbac_permission``
        (any-of). This is for the decisions *inside* a route - a list view that also
        accepts POST, or an ownership rule - where the coarse gate is not enough.
        """
        from vs_rbac.evaluator import has_permission
        from vs_rbac.permissions import is_vision_super_admin

        user = self.request.user
        return is_vision_super_admin(user) or has_permission(
            user, key, tenant=self.tenant,
        )

    # Whether the caller may read other people's export activity.
    def is_admin_reader(self) -> bool:
        return self.can(ExportPermission.ACTIVITY_VIEW)

    # Definitions this caller may see.
    def visible_definitions(self):
        qs = ExportDefinition.objects.filter(tenant=self.tenant).select_related(
            "entity", "owner",
        )
        if self.is_admin_reader():
            return qs
        user = self.request.user
        return qs.filter(Q(owner=user) | Q(shares__user=user)).distinct()

    # Runs this caller may see.
    def visible_runs(self):
        qs = ExportRun.objects.filter(tenant=self.tenant).select_related(
            "definition", "definition__owner", "entity", "requested_by", "file",
        )
        if self.is_admin_reader():
            return qs
        user = self.request.user
        return qs.filter(
            Q(requested_by=user)
            | Q(definition__owner=user)
            | Q(definition__shares__user=user)
        ).distinct()

    # One definition by pk, scoped to what this caller may see.
    def get_definition(self, pk, *, for_write=False):
        definition = self.visible_definitions().filter(pk=pk).first()
        if definition is None:
            raise NotFound("No export matches that id.")
        if for_write and definition.owner_id != self.request.user.pk and not self.is_admin_reader():
            # Sharing grants sight, never edit rights - that is what keeps a shared
            # export's data promises stable for everyone it was shared with.
            raise PermissionDenied(
                "Only the owner can change this export. Duplicate it to make your own "
                "version."
            )
        return definition

    # One run by pk, scoped to what this caller may see.
    def get_run(self, pk):
        run = self.visible_runs().filter(pk=pk).first()
        if run is None:
            raise NotFound("No run matches that id.")
        return run

    # One schedule by pk, scoped to the definitions this caller may see.
    def get_schedule(self, pk, *, for_write=False):
        definition_ids = self.visible_definitions().values_list("pk", flat=True)
        schedule = ExportSchedule.objects.filter(
            pk=pk, definition_id__in=definition_ids,
        ).select_related("definition", "last_run").first()
        if schedule is None:
            raise NotFound("No schedule matches that id.")
        if for_write:
            # Changing how someone else's export behaves unattended needs the same
            # ownership rule as changing the export itself.
            self.get_definition(schedule.definition_id, for_write=True)
        return schedule

    # The boundary one dataset should be read inside, for this request.
    def resolve_scope(self, dataset):
        """Build the :class:`~vs_exports.catalogue.ScopeContext` this dataset needs.

        ``?entity=`` is required for an entity-scoped dataset and ignored for a
        tenant-scoped one - asking which set of books an audit export belongs to is a
        question with no answer. Which applies is the dataset's own declaration, so
        publishing a tenant-scoped dataset needs no change here.
        """
        from .catalogue import ScopeContext

        # The caller travels with the scope, so a builder's estimate and preview
        # narrow to the same rows the finished file will contain. Without it the
        # estimate would promise a branch user rows the run then omits.
        user = getattr(self.request, "effective_user", None) or self.request.user
        if not dataset.needs_entity:
            return ScopeContext(tenant=self.tenant, user=user)
        # resolve_entity raises ValidationError when ?entity= is absent and NotFound
        # when it is unknown or belongs to another tenant.
        return ScopeContext(
            tenant=self.tenant, entity=resolve_entity(self.request), user=user,
        )


# --------------------------------------------------------------------------- #
# Catalogue                                                                   #
# --------------------------------------------------------------------------- #
class CatalogueView(_ExportBase):
    """``GET /v1/exports/catalogue/`` - datasets this caller may export.

    Filtered by the caller's own permissions, so the builder never offers a dataset
    that would fail at run time. Modules with nothing available are still listed, with
    an empty dataset list, because "Procurement - no datasets yet" is information.
    """

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def get(self, request):
        tenant = self.tenant
        allowed = [d for d in all_datasets() if may_export_dataset(request.user, d, tenant)]
        include_sensitive = services.capabilities(request.user, tenant)["can_export_sensitive"]
        by_module = {m: [] for m in modules()}
        for dataset in allowed:
            by_module.setdefault(dataset.module, []).append(
                dataset.describe(include_sensitive=include_sensitive)
            )
        return success_response(
            "Dataset catalogue retrieved successfully.",
            {
                "modules": [
                    {"name": name, "datasets": datasets, "available": bool(datasets)}
                    for name, datasets in sorted(by_module.items())
                ],
            },
        )


class CatalogueDetailView(_ExportBase):
    """``GET /v1/exports/catalogue/<key>/`` - one dataset in full."""

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def get(self, request, key):
        dataset = get_dataset(key)
        if dataset is None or not may_export_dataset(request.user, dataset, self.tenant):
            # Unknown and forbidden look identical, so a caller cannot enumerate the
            # datasets they are not allowed to see.
            raise NotFound(f"No dataset matches '{key}'.")
        include_sensitive = services.capabilities(
            request.user, self.tenant,
        )["can_export_sensitive"]
        return success_response(
            "Dataset retrieved successfully.",
            dataset.describe(include_sensitive=include_sensitive),
        )


class CapabilitiesView(_ExportBase):
    """``GET /v1/exports/capabilities/`` - flags so the UI can disable with a reason."""

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def get(self, request):
        return success_response(
            "Capabilities retrieved successfully.",
            services.capabilities(request.user, self.tenant),
        )


# --------------------------------------------------------------------------- #
# Preview and estimate                                                        #
# --------------------------------------------------------------------------- #
class PreviewView(_ExportBase):
    """``POST /v1/exports/preview/?entity=<code>`` - rows, size and sample.

    Called on every column and filter change, so it stays cheap: the count is a
    LIMITed one and the sample is ten rows. Anything the caller may not read is already
    excluded, and the reason comes back in ``warnings`` rather than as an error.
    """

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def post(self, request):
        payload = PreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        config = payload.validated_data

        dataset = get_dataset(config["dataset_key"])
        if not may_export_dataset(request.user, dataset, self.tenant):
            raise PermissionDenied(f"You cannot export the {dataset.name} dataset.")
        scope = self.resolve_scope(dataset)

        try:
            figures = estimate(request.user, dataset, scope, config, self.tenant)
            headers, rows = sample_rows(request.user, dataset, scope, config, self.tenant)
        except ExportError as exc:
            # A configuration problem is the caller's to fix, so it is a 400 with the
            # same user-safe sentence a failed run would show.
            raise ValidationError({"detail": exc.message, "code": exc.code})

        analytics.record(
            analytics.Event.ESTIMATE_VIEWED, tenant=self.tenant, actor=request.user,
            properties={
                "rows_bucket": analytics.bucket_rows(figures["matching_rows"]),
                "size_bucket": analytics.bucket_bytes(figures["estimated_bytes"]),
            },
            session_key=request.headers.get("X-Export-Session", ""),
        )
        return success_response(
            "Preview generated successfully.",
            {
                **figures,
                "sample": {"headers": headers, "rows": rows},
                "reads_as": plain_sentence(
                    dataset, config, scope, rows=figures["matching_rows"],
                ),
            },
        )


# --------------------------------------------------------------------------- #
# Definitions                                                                 #
# --------------------------------------------------------------------------- #
class DefinitionListView(_ExportBase):
    """``GET`` the Saved exports list · ``POST`` a new export."""

    rbac_permission = [ExportPermission.DEFINITION_VIEW, ExportPermission.DEFINITION_CREATE]

    def get(self, request):
        qs = self.visible_definitions()
        if request.query_params.get("module"):
            wanted = request.query_params["module"].lower()
            keys = [d.key for d in all_datasets() if d.module.lower() == wanted]
            qs = qs.filter(dataset_key__in=keys)
        if request.query_params.get("owner") == "me":
            qs = qs.filter(owner=request.user)
        if request.query_params.get("q"):
            term = request.query_params["q"]
            qs = qs.filter(Q(name__icontains=term) | Q(description__icontains=term))
        if request.query_params.get("include_archived") != "true":
            qs = qs.filter(is_archived=False)
        return self.paginate(request, qs, ExportDefinitionListSerializer)

    def post(self, request):
        # The route's any-of gate lets a viewer through; creating needs the create key.
        if not self.can(ExportPermission.DEFINITION_CREATE):
            raise PermissionDenied("You cannot create exports.")
        payload = ExportDefinitionWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        dataset = get_dataset(payload.validated_data["dataset_key"])
        if not may_export_dataset(request.user, dataset, self.tenant):
            raise PermissionDenied(f"You cannot export the {dataset.name} dataset.")
        scope = self.resolve_scope(dataset)

        # tenant/entity/owner come from the request, never from the body.
        definition = payload.save(
            tenant=self.tenant, entity=scope.entity, owner=request.user,
        )
        audit.record(
            AuditAction.DEFINITION_CREATED, actor=request.user, tenant=self.tenant,
            obj=definition, label=definition.name,
            metadata={"dataset": definition.dataset_key, "scope": scope.label},
        )
        return success_response(
            "Export created successfully.",
            ExportDefinitionDetailSerializer(definition, context={"request": request}).data,
            status=201,
        )


class DefinitionDetailView(_ExportBase):
    """``GET`` / ``PATCH`` / ``DELETE`` one export."""

    rbac_permission = [
        ExportPermission.DEFINITION_VIEW, ExportPermission.DEFINITION_UPDATE,
        ExportPermission.DEFINITION_DELETE,
    ]

    def get(self, request, pk):
        definition = self.get_definition(pk)
        return success_response(
            "Export retrieved successfully.",
            ExportDefinitionDetailSerializer(definition, context={"request": request}).data,
        )

    def patch(self, request, pk):
        definition = self.get_definition(pk, for_write=True)
        before = {
            "columns": list(definition.columns or []),
            "filters": list(definition.filters or []),
            "format": definition.format,
        }
        payload = ExportDefinitionWriteSerializer(definition, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        definition = payload.save()
        audit.record(
            AuditAction.DEFINITION_UPDATED, actor=request.user, tenant=self.tenant,
            obj=definition, label=definition.name, metadata={"before": before},
        )
        return success_response(
            "Export updated successfully. Files already produced are unchanged.",
            ExportDefinitionDetailSerializer(definition, context={"request": request}).data,
        )

    def delete(self, request, pk):
        """Archive rather than destroy - the files it produced must keep their history.

        Runs reference the definition with ``SET_NULL``, so a hard delete would orphan
        them; archiving keeps the audit trail intact and takes the export out of the
        list, which is what "delete" means to the person clicking it.
        """
        definition = self.get_definition(pk, for_write=True)
        definition.is_archived = True
        definition.save(update_fields=["is_archived", "updated_at"])
        audit.record(
            AuditAction.DEFINITION_DELETED, actor=request.user, tenant=self.tenant,
            obj=definition, label=definition.name, severity="WARNING",
            metadata={"files_kept": definition.runs.filter(
                status__in=list(SUCCESSFUL_RUN_STATUSES)).count()},
        )
        return success_response(
            "Export archived. Files it already produced stay available until they expire."
        )


class DefinitionDuplicateView(_ExportBase):
    """``POST /definitions/<pk>/duplicate/`` - a private copy, owned by the caller."""

    rbac_permission = ExportPermission.DEFINITION_CREATE

    def post(self, request, pk):
        source = self.get_definition(pk)
        copy = ExportDefinition.objects.create(
            tenant=source.tenant,
            entity=source.entity,
            dataset_key=source.dataset_key,
            name=f"{source.name} (copy)",
            description=source.description,
            columns=list(source.columns or []),
            filters=list(source.filters or []),
            sort=list(source.sort or []),
            format=source.format,
            format_options=dict(source.format_options or {}),
            values_mode=source.values_mode,
            file_name_pattern=source.file_name_pattern,
            owner=request.user,
            sharing=Sharing.PRIVATE,       # a copy is never born shared
            is_draft=source.is_draft,
        )
        audit.record(
            AuditAction.DEFINITION_CREATED, actor=request.user, tenant=self.tenant,
            obj=copy, label=copy.name, metadata={"duplicated_from": source.pk},
        )
        return success_response(
            "Export duplicated successfully.",
            ExportDefinitionDetailSerializer(copy, context={"request": request}).data,
            status=201,
        )


class DefinitionShareView(_ExportBase):
    """``POST /definitions/<pk>/share/`` - replace the share list.

    Sharing shows people the export and its files. It never lends them the owner's
    access: the run executes as the owner and every download is re-authorised against
    the person clicking it.
    """

    rbac_permission = ExportPermission.DEFINITION_SHARE

    def post(self, request, pk):
        definition = self.get_definition(pk, for_write=True)
        payload = ShareSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        from vs_user.models import User

        # Only people in the same tenant - a share must not cross a tenant boundary.
        users = list(User.objects.filter(
            pk__in=payload.validated_data["user_ids"], tenant=self.tenant,
        ).exclude(pk=definition.owner_id))

        definition.shares.all().delete()
        ExportDefinitionShare.objects.bulk_create([
            ExportDefinitionShare(definition=definition, user=u, shared_by=request.user)
            for u in users
        ])
        definition.sharing = Sharing.SHARED if users else Sharing.PRIVATE
        definition.save(update_fields=["sharing", "updated_at"])

        audit.record(
            AuditAction.DEFINITION_SHARED, actor=request.user, tenant=self.tenant,
            obj=definition, label=definition.name,
            metadata={"recipients": [u.pk for u in users]},
        )
        return success_response(
            "Sharing updated successfully.",
            {"shared_with": [{"id": u.pk, "email": u.email} for u in users]},
        )


class DefinitionRunView(_ExportBase):
    """``POST /definitions/<pk>/run/`` - produce a file now."""

    rbac_permission = ExportPermission.RUN_CREATE

    def post(self, request, pk):
        definition = self.get_definition(pk)
        payload = RunRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            run, created = services.trigger_run(
                definition=definition,
                actor=request.user,
                trigger=RunTrigger.MANUAL,
                client_key=payload.validated_data.get("client_key", ""),
            )
        except ExportServiceError as exc:
            raise ValidationError({"detail": str(exc)})
        return success_response(
            "Export queued successfully." if created
            else "This export is already running - showing the run in progress.",
            ExportRunDetailSerializer(run, context={"request": request}).data,
            status=201 if created else 200,
        )


class FromScreenView(_ExportBase):
    """``GET /v1/exports/from-screen/?screen=<key>&<the screen's own filters>``.

    "Export what this table is showing." The caller forwards the list screen's query
    string unchanged and gets back a runnable configuration, an estimate and a sample,
    ready to hand straight to ``/quick/``.

    The translation lives in the module that owns the screen, because only Finance
    knows what ``?bucket=overdue`` means. What this view adds is the honesty:
    ``unmapped`` names every screen filter that could not be carried, and ``exact``
    is false whenever the file would be **wider** than the table the user was looking
    at. Silently dropping a filter is the one outcome worth designing against - the
    user asked for overdue invoices and would get all of them, with nothing on screen
    to say so.
    """

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def get(self, request):
        from .catalogue import all_screens, get_screen, resolve_screen

        key = request.query_params.get("screen")
        binding = get_screen(key) if key else None
        if binding is None:
            raise ValidationError({
                "screen": (
                    f"'{key}' is not a screen that can start an export."
                    if key else "A 'screen' parameter is required."
                ),
                "available": [s.key for s in all_screens()],
            })

        dataset = binding.dataset
        if dataset is None:
            raise NotFound("The dataset behind this screen is no longer published.")
        if not may_export_dataset(request.user, dataset, self.tenant):
            raise PermissionDenied(f"You cannot export the {dataset.name} dataset.")
        scope = self.resolve_scope(dataset)

        include_sensitive = services.capabilities(
            request.user, self.tenant,
        )["can_export_sensitive"]
        resolved = resolve_screen(binding, request.query_params.dict())
        config = {
            "dataset_key": dataset.key,
            "columns": list(dataset.default_columns or dataset.locked_field_ids),
            "filters": resolved["filters"],
            "sort": [],
            "format": ExportFormat.XLSX,
            "values_mode": ValuesMode.PEOPLE,
        }

        try:
            figures = estimate(request.user, dataset, scope, config, self.tenant)
            headers, rows = sample_rows(request.user, dataset, scope, config, self.tenant)
        except ExportError as exc:
            raise ValidationError({"detail": exc.message, "code": exc.code})

        return success_response(
            "Export configuration prepared from the screen.",
            {
                "screen": binding.describe(),
                "config": config,
                # The drawer offers a format picker, so it needs the dataset's
                # own list rather than assuming the two-value default. Every
                # dataset publishes both today; that is not a promise the UI
                # should be hardcoding.
                "supported_formats": [str(f) for f in dataset.formats],
                # Every column the caller could pick, through the same sensitive-field gate
                # the catalogue uses: a column they may not export is never offered rather
                # than offered and then dropped from the file.
                "fields": [
                    f.describe() for f in dataset.fields
                    if include_sensitive or not f.sensitive
                ],
                "carried": resolved["carried"],
                "unmapped": resolved["unmapped"],
                "added": resolved["added"],
                "exact": resolved["exact"],
                "warning": None if resolved["exact"] else (
                    "Some filters on this screen cannot be carried into an export, so "
                    "the file will contain more rows than the table shows. Check the "
                    "filters in the builder before running it."
                ),
                **figures,
                "sample": {"headers": headers, "rows": rows},
                "reads_as": plain_sentence(
                    dataset, config, scope, rows=figures["matching_rows"],
                ),
            },
        )


class QuickExportView(_ExportBase):
    """``POST /v1/exports/quick/?entity=<code>`` - run a configuration without saving it.

    The drawer opened from a module list screen sends the filters already on that
    screen. Nothing is saved: there is no definition, so the run's ``frozen_config`` is
    the only record of what it produced, and only the person who asked for it can see
    or download the result. If they want it again next month, they save it as a proper
    export instead.
    """

    rbac_permission = ExportPermission.RUN_CREATE

    def post(self, request):
        payload = QuickExportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        config = dict(payload.validated_data)
        client_key = config.pop("client_key", "")
        wants_sync = config.pop("sync", False)

        dataset = get_dataset(config["dataset_key"])
        if not may_export_dataset(request.user, dataset, self.tenant):
            raise PermissionDenied(f"You cannot export the {dataset.name} dataset.")
        scope = self.resolve_scope(dataset)
        if not config.get("columns"):
            raise ValidationError({
                "columns": "Choose at least one column before running this export.",
            })

        # The server decides, by re-running its own estimate. The drawer's claim
        # that the file will be small is a hint, not an authorisation, and an
        # inline run holds a web worker for its whole duration.
        run_inline = False
        if wants_sync:
            try:
                figures = estimate(request.user, dataset, scope, config, self.tenant)
            except ExportError as exc:
                raise ValidationError({"detail": exc.message, "code": exc.code})
            run_inline = figures["estimated_bytes"] <= SYNC_EXPORT_MAX_BYTES

        try:
            run, created = services.trigger_quick_run(
                config=config, entity=scope.entity, tenant=self.tenant, actor=request.user,
                client_key=client_key,
                # Not queued when we are about to run it here ourselves; queueing
                # as well would produce the same file twice.
                queue=not run_inline,
            )
        except ExportServiceError as exc:
            raise ValidationError({"detail": str(exc)})

        if run_inline and created:
            # Errors are recorded ON the run by execute_run (status FAILED plus a
            # user-safe failure block), so a failure here is still a 200 carrying
            # a run the drawer can explain - not a 500.
            run = services.execute_run(run.pk) or run

        if not created:
            message = "This export is already running - showing the run in progress."
        elif run_inline and run.status in TERMINAL_RUN_STATUSES:
            message = "Export ready."
        else:
            message = "Export queued successfully."

        return success_response(
            message,
            ExportRunDetailSerializer(run, context={"request": request}).data,
            status=201 if created else 200,
        )


# --------------------------------------------------------------------------- #
# Runs and files                                                              #
# --------------------------------------------------------------------------- #
class RunListView(_ExportBase):
    """``GET /v1/exports/runs/`` - the Files list."""

    rbac_permission = ExportPermission.RUN_VIEW

    def get(self, request):
        qs = self.visible_runs()
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"].upper())
        if request.query_params.get("trigger"):
            qs = qs.filter(trigger=request.query_params["trigger"].upper())
        if request.query_params.get("definition"):
            qs = qs.filter(definition_id=request.query_params["definition"])
        return self.paginate(request, qs, ExportRunListSerializer)


class RunDetailView(_ExportBase):
    """``GET /v1/exports/runs/<pk>/`` - outcome, frozen config and drift."""

    rbac_permission = ExportPermission.RUN_VIEW

    def get(self, request, pk):
        run = self.get_run(pk)
        if run.failure_code:
            # Starts the clock the failure-resolution metric measures against.
            analytics.record(
                analytics.Event.FAILURE_VIEWED, tenant=self.tenant, actor=request.user,
                properties={"reason_code": run.failure_code},
            )
        return success_response(
            "Run retrieved successfully.",
            ExportRunDetailSerializer(run, context={"request": request}).data,
        )


class RunCancelView(_ExportBase):
    """``POST /v1/exports/runs/<pk>/cancel/`` - stop a queued or running export."""

    rbac_permission = ExportPermission.RUN_CANCEL

    def post(self, request, pk):
        run = self.get_run(pk)
        if run.requested_by_id != request.user.pk and not self.is_admin_reader():
            raise PermissionDenied("Only the person who started this run can cancel it.")
        try:
            run = services.request_cancel(run, request.user)
        except ExportServiceError as exc:
            raise ValidationError({"detail": str(exc)})
        return success_response(
            "Cancellation requested. No partial file is kept.",
            ExportRunDetailSerializer(run, context={"request": request}).data,
        )


class RunRetryView(_ExportBase):
    """``POST /v1/exports/runs/<pk>/retry/`` - a fresh attempt of a failed run."""

    rbac_permission = ExportPermission.RUN_CREATE

    def post(self, request, pk):
        run = self.get_run(pk)
        try:
            new_run = services.retry_run(run, request.user)
        except ExportServiceError as exc:
            raise ValidationError({"detail": str(exc)})
        return success_response(
            "Retry queued successfully.",
            ExportRunDetailSerializer(new_run, context={"request": request}).data,
            status=201,
        )


class FileListView(_ExportBase):
    """``GET /v1/exports/files/`` - produced files with their availability."""

    rbac_permission = ExportPermission.RUN_VIEW

    def get(self, request):
        run_ids = self.visible_runs().values_list("pk", flat=True)
        qs = ExportFile.objects.filter(run_id__in=run_ids).select_related("run")
        if request.query_params.get("available") == "true":
            from django.utils import timezone

            qs = qs.filter(available_until__gt=timezone.now(), purged_at__isnull=True)
        return self.paginate(request, qs, ExportFileSerializer)


class FileDownloadView(_ExportBase):
    """``GET /v1/exports/files/<pk>/download/`` - the bytes, if you may have them.

    Authorised against the *downloader* and the run's frozen entity and dataset, then
    logged either way. A refusal is recorded exactly like a success, because "who tried
    and was told no" is a question compliance actually asks.
    """

    rbac_permission = ExportPermission.FILE_DOWNLOAD

    def get(self, request, pk):
        run_ids = self.visible_runs().values_list("pk", flat=True)
        file = ExportFile.objects.filter(
            pk=pk, run_id__in=run_ids,
        ).select_related("run", "run__definition", "run__tenant").first()
        if file is None:
            raise NotFound("No file matches that id.")

        ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or \
            request.META.get("REMOTE_ADDR", "")
        allowed, reason = services.authorise_download(file, request.user, self.tenant)
        if not allowed:
            services.log_download(
                file, request.user, outcome=DownloadOutcome.REFUSED, reason=reason, ip=ip,
            )
            raise PermissionDenied(_refusal_message(reason, file))

        services.log_download(
            file, request.user, outcome=DownloadOutcome.ALLOWED, ip=ip,
        )
        from django.core.files.storage import default_storage

        with default_storage.open(file.storage_name, "rb") as handle:
            body = handle.read()
        content_type = FORMAT_MEDIA.get(file.format, ("bin", "application/octet-stream"))[1]
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{file.name}"'
        return response


# Turn a refusal reason into the sentence the downloader reads.
def _refusal_message(reason, file) -> str:
    from .constants import DownloadRefusal

    return {
        DownloadRefusal.EXPIRED: (
            f"This file passed its availability date on "
            f"{file.available_until:%d %b %Y}. Run the export again to produce a new one."
        ),
        DownloadRefusal.PURGED: (
            "This file has been deleted from storage. The run record is still here; run "
            "the export again to produce a new file."
        ),
        DownloadRefusal.NO_ENTITY_ACCESS:
            "You no longer have access to the entity this file was produced for.",
        DownloadRefusal.NO_DATASET_ACCESS:
            "You no longer have access to the dataset this file came from.",
        DownloadRefusal.NOT_SHARED:
            "This export has not been shared with you.",
    }.get(reason, "You cannot download this file.")


class FileDownloadLogView(_ExportBase):
    """``GET /v1/exports/files/<pk>/downloads/`` - who took it, and who was refused."""

    rbac_permission = ExportPermission.RUN_VIEW

    def get(self, request, pk):
        run_ids = self.visible_runs().values_list("pk", flat=True)
        file = ExportFile.objects.filter(pk=pk, run_id__in=run_ids).first()
        if file is None:
            raise NotFound("No file matches that id.")
        return self.paginate(
            request, file.downloads.select_related("user"), ExportDownloadSerializer,
        )


# --------------------------------------------------------------------------- #
# Deliveries and admin activity                                               #
# --------------------------------------------------------------------------- #
class ActivityView(_ExportBase):
    """``GET /v1/exports/activity/`` - the admin console's all-activity view.

    Defaults to the runs an administrator actually cares about: those that included a
    sensitive field or went to a recipient, newest first. Reading it is itself an audit
    event, which is why the read is recorded before the response is built.
    """

    rbac_permission = ExportPermission.ACTIVITY_VIEW

    def get(self, request):
        audit.record(
            AuditAction.ADMIN_VIEWED_ACTIVITY, actor=request.user, tenant=self.tenant,
            obj=self.tenant, label="Export activity",
            metadata={k: request.query_params.getlist(k) for k in request.query_params},
        )
        qs = ExportRun.objects.filter(tenant=self.tenant).select_related(
            "definition", "entity", "requested_by", "file",
        )
        if request.query_params.get("actor"):
            qs = qs.filter(requested_by_id=request.query_params["actor"])
        if request.query_params.get("dataset"):
            qs = qs.filter(frozen_config__dataset_key=request.query_params["dataset"])
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"].upper())
        if request.query_params.get("since"):
            qs = qs.filter(queued_at__date__gte=request.query_params["since"])
        return self.paginate(request, qs, ExportRunListSerializer)


# --------------------------------------------------------------------------- #
# Analytics                                                                   #
# --------------------------------------------------------------------------- #
class AnalyticsIngestView(_ExportBase):
    """``POST /v1/exports/analytics/`` - the builder funnel, reported by the browser.

    Only events in :data:`vs_exports.analytics.CLIENT_EVENTS` are accepted, and every
    property is filtered through the event's schema, so this cannot become a general
    channel for client-supplied data. Anyone who can open the Export Centre can post
    here: refusing telemetry from ordinary users would bias the very funnel it measures.
    """

    rbac_permission = ExportPermission.CATALOGUE_VIEW

    def post(self, request):
        payload = AnalyticsIngestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        session_key = payload.validated_data.get("session_key", "")

        accepted = 0
        for item in payload.validated_data["events"]:
            written = analytics.record(
                item["name"], tenant=self.tenant, actor=request.user,
                properties=item.get("properties"),
                session_key=item.get("session_key") or session_key,
            )
            accepted += 1 if written is not None else 0
        return success_response(
            "Analytics recorded.", {"accepted": accepted, "received": len(payload.validated_data["events"])},
        )


class AnalyticsSummaryView(_ExportBase):
    """``GET /v1/exports/analytics/summary/`` - the four metrics that judge this feature.

    Reuse and unhappy-run share are counted from the run rows; builder abandonment and
    failure-resolution time come from the events. Admin-only, because it describes how
    the whole organisation works rather than one person's exports.
    """

    rbac_permission = ExportPermission.ACTIVITY_VIEW

    def get(self, request):
        since = None
        if request.query_params.get("days"):
            import datetime

            from django.utils import timezone

            try:
                days = max(1, min(int(request.query_params["days"]), 365))
            except ValueError:
                raise ValidationError({"days": "Give a whole number of days, 1 to 365."})
            since = timezone.now() - datetime.timedelta(days=days)
        return success_response(
            "Export metrics retrieved successfully.",
            analytics.summary(self.tenant, since=since),
        )


# --------------------------------------------------------------------------- #
# Schedules                                                                   #
# --------------------------------------------------------------------------- #
class ScheduleListView(_ExportBase):
    """``GET`` the Schedules list - ``POST`` a new schedule."""

    rbac_permission = [ExportPermission.SCHEDULE_VIEW, ExportPermission.SCHEDULE_CREATE]

    def get(self, request):
        definition_ids = self.visible_definitions().values_list("pk", flat=True)
        qs = ExportSchedule.objects.filter(
            definition_id__in=definition_ids,
        ).select_related("definition", "definition__owner", "last_run")
        if request.query_params.get("state"):
            qs = qs.filter(state=request.query_params["state"].upper())
        if request.query_params.get("definition"):
            qs = qs.filter(definition_id=request.query_params["definition"])
        return self.paginate(request, qs, ExportScheduleSerializer)

    def post(self, request):
        if not self.can(ExportPermission.SCHEDULE_CREATE):
            raise PermissionDenied("You cannot schedule exports.")
        payload = ExportScheduleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        # Only an export you could edit may be scheduled: an unattended run makes its
        # owner answerable for output nobody asked for that morning.
        definition = self.get_definition(
            payload.validated_data["definition"].pk, for_write=True,
        )
        if definition.is_draft:
            raise ValidationError({
                "definition": (
                    "Finish this export before scheduling it - a draft cannot run."
                ),
            })
        if definition.is_archived:
            raise ValidationError({
                "definition": "This export is archived, so it cannot be scheduled.",
            })

        schedule = payload.save(definition=definition)
        schedule.reschedule()
        audit.record(
            AuditAction.SCHEDULE_CREATED, actor=request.user, tenant=self.tenant,
            obj=schedule, label=definition.name,
            metadata={
                "recurrence": schedule.recurrence,
                "timezone": schedule.timezone_name,
                "next_run_at": str(schedule.next_run_at),
            },
        )
        return success_response(
            "Schedule created successfully.",
            ExportScheduleSerializer(schedule, context={"request": request}).data,
            status=201,
        )


class ScheduleDetailView(_ExportBase):
    """``GET`` / ``PATCH`` / ``DELETE`` one schedule."""

    rbac_permission = [ExportPermission.SCHEDULE_VIEW, ExportPermission.SCHEDULE_MANAGE]

    def get(self, request, pk):
        return success_response(
            "Schedule retrieved successfully.",
            ExportScheduleSerializer(
                self.get_schedule(pk), context={"request": request},
            ).data,
        )

    def patch(self, request, pk):
        schedule = self.get_schedule(pk, for_write=True)
        payload = ExportScheduleSerializer(schedule, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        schedule = payload.save()
        schedule.reschedule()
        return success_response(
            "Schedule updated successfully. Files already produced are unchanged.",
            ExportScheduleSerializer(schedule, context={"request": request}).data,
        )

    def delete(self, request, pk):
        """Remove the schedule. The export and its files are untouched."""
        schedule = self.get_schedule(pk, for_write=True)
        schedule.delete()
        return success_response(
            "Schedule removed. The export and the files it produced are unaffected."
        )


class SchedulePauseView(_ExportBase):
    """``POST /schedules/<pk>/pause/`` - stop it until somebody resumes it."""

    rbac_permission = ExportPermission.SCHEDULE_MANAGE
    resume = False

    def post(self, request, pk):
        schedule = self.get_schedule(pk, for_write=True)
        if self.resume:
            if schedule.state == ScheduleState.FINISHED:
                raise ValidationError({
                    "detail": (
                        "This schedule has finished - its last occurrence is in the "
                        "past. Change the dates instead of resuming it."
                    ),
                })
            schedule = services.resume_schedule(schedule, request.user)
            message = "Schedule resumed successfully."
        else:
            schedule = services.pause_schedule(
                schedule, request.user, detail=request.data.get("reason", ""),
            )
            message = "Schedule paused successfully."
        return success_response(
            message,
            ExportScheduleSerializer(schedule, context={"request": request}).data,
        )


class ScheduleResumeView(SchedulePauseView):
    """``POST /schedules/<pk>/resume/`` - clear the pause and recompute the next run."""

    resume = True
