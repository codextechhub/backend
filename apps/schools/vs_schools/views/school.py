from __future__ import annotations

from django.db.models import Q
from django.db.models import Prefetch
from rest_framework import generics
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated

from core.mixins import RetrieveModelMixin, CreateModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from core.uploads import LOGO_EXTENSIONS, MAX_LOGO_BYTES, validate_upload
from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
from vs_audit.services import AuditDiffService, emit_audit_event
from ..models import School, SchoolStatus
from vs_tenants.models import Branch
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from ..serializers import (
    SchoolCreateSerializer,
    SchoolDetailSerializer,
    SchoolListSerializer,
    SchoolProfileSerializer,
    SchoolProfileUpdateSerializer,
    SchoolUpdateSerializer,
)


class ActorContextMixin:
    """Adds actor_id into serializer context (for audit/events)."""

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class SchoolListView(ActorContextMixin, generics.ListAPIView):
    """docstring-name: List schools"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"
    serializer_class = SchoolListSerializer
    pagination_class = XVSPagination

    queryset = (
        School.objects.all()
        .select_related("branding",)
        # "tenant__branches", not "branches": School.branches is now a property
        # over the tenant's sites, so the prefetch has to name the real path.
        .prefetch_related("tenant__branches")
    )

    def get_queryset(self):
        qs = super().get_queryset()

        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            qs = qs.filter(status=SchoolStatus.ACTIVE)

        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            qs = qs.filter(status=SchoolStatus.INACTIVE)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(ownership_type__iexact=q)
                | Q(status__iexact=q)
                | Q(tenant__branches__state__icontains=q)
                | Q(tenant__branches__country__icontains=q)
                | Q(tenant__branches__name__icontains=q)
            ).distinct()

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name", "status", "-status"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("-created_at")
        return qs


class SchoolStatsView(generics.GenericAPIView):
    """
    Returns a single summary payload with school counts broken down
    by status. Designed for the School Management dashboard stat cards.

    Response shape - ``all`` plus one lower-cased key per member of
    :class:`SchoolStatus`, so the four counts below grow with the enum:
        {
            "all":       47,
            "active":    32,
            "inactive":   7,
            "pending":    8,
            "suspended":  0
        }

    Built from the choices rather than listed by hand. The hand-written version
    named ACTIVE, PENDING and INACTIVE and omitted SUSPENDED, which had been
    added to the enum afterwards - so the one tab whose figure was missing had to
    fetch its own count with a second request, and any status added next would
    have gone the same way in silence. Deriving it means a new status is counted
    the moment it exists.

    One DB query using conditional aggregation - no N+1.

    docstring-name: School statistics
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"

    def get(self, request, *args, **kwargs):
        from django.db.models import Count, Q

        result = School.objects.aggregate(
            all=Count("slug"),
            **{
                value.lower(): Count("slug", filter=Q(status=value))
                for value in SchoolStatus.values
            },
        )

        return success_response(message="School statistics retrieved.", data=result)
    

class SchoolCreateView(CreateModelMixin, ActorContextMixin, generics.CreateAPIView):
    """docstring-name: Create a school"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.create"
    serializer_class = SchoolCreateSerializer


class SchoolDetailView(ActorContextMixin, generics.RetrieveAPIView):
    """docstring-name: School detail"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"
    serializer_class = SchoolDetailSerializer

    queryset = (
        School.objects.all()
        .select_related(
            "branding",
            "primary_admin",
            "primary_admin__contact",
            "package_setup",
            "package_setup__package_plan",
        )
        .prefetch_related(
            Prefetch(
                "tenant__branches",
                queryset=Branch.objects.select_related(
                    "primary_admin", "primary_admin__contact"
                ),
            )
        )
    )
    lookup_field = "slug"

    def retrieve(self, request, *args, **kwargs):
        """Return one school, letting a missing slug 404 the way it should.

        This used to wrap the whole method in ``except Exception`` and answer with
        a ``DEBUG:`` message plus ``traceback.format_exc()``. Left-over debugging,
        and it did real harm: it swallowed the ``Http404`` that ``get_object``
        raises for an unknown slug, so a school that does not exist came back as a
        500 rather than a 404 - and it tried to hand a full Python stack trace,
        file paths and all, to whoever asked. Every other failure is better served
        by the project's exception handler, which already maps DRF errors to the
        standard envelope and logs the trace server-side instead of shipping it.
        """
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(
            message="Data retrieved successfully.",
            data=serializer.data,
        )


class SchoolUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Separate update endpoint. Returns a full detail payload after update
    so the UI doesn't need to refetch.

    docstring-name: Update a school
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.update"
    serializer_class = SchoolUpdateSerializer

    queryset = (
        School.objects.all()
        .select_related(
            "branding",
            "primary_admin",
            "primary_admin__contact",
            "package_setup",
            "package_setup__package_plan",
        )
        .prefetch_related(
            Prefetch(
                "tenant__branches",
                queryset=Branch.objects.select_related(
                    "primary_admin", "primary_admin__contact"
                ),
            )
        )
    )
    lookup_field = "slug"

    def update(self, request, *args, **kwargs):
        # The primary key is read before the write and the row re-read by it
        # afterwards. ``lookup_field`` is the slug, and the slug is now
        # editable until the school goes live, so re-fetching by the URL key
        # would 404 on exactly the rename that had just succeeded - reporting
        # "no such school" for the one request that moved it.
        school_pk = self.get_object().pk
        super().update(request, *args, **kwargs)
        school = self.get_queryset().get(pk=school_pk)
        return success_response(
            message="School updated successfully.",
            data=SchoolDetailSerializer(school, context=self.get_serializer_context()).data,
        )


class SchoolProfileView(ActorContextMixin, generics.GenericAPIView):
    """GET/PATCH /v1/schools/me/profile/ - the school's own profile.

    THE SCHOOL'S VIEW OF ITSELF, not the platform's. Three things separate it
    from ``SchoolUpdateView`` above and each is deliberate.

    **It is on the pending-tenant surface.** Filling in ownership type, term
    structure and currency is one of the required onboarding steps, and until
    now the only endpoint that could do it was closed to exactly the schools
    that needed it: a PENDING tenant reaching ``/schools/<slug>/update/`` was
    refused with TENANT_NOT_LIVE, so the step could be blocked and never
    cleared. It stays open after go-live too - a school's profile is its own to
    maintain.

    **It takes no identifier.** The school is ``request.tenant``'s, full stop.
    There is no pk and no slug to change, so there is no way to address another
    tenant's row and no 404-versus-403 question to get wrong.

    **It is narrower.** ``SchoolProfileUpdateSerializer`` drops name, slug and
    code. Those are allocated by CodeX when the school is created; the slug in
    particular is the host every one of the school's users signs in at, and
    moving it stays a platform decision made through the platform endpoint.

    docstring-name: My school profile
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pending_tenant_surface = True

    @property
    def rbac_permission(self) -> str:
        """Read and write are different keys, so a bursar can be given one.

        ``HasRBACPermission`` reads this attribute once per request, so a
        property is enough to vary it by method - the same shape the ticket
        viewset uses for its per-action keys.
        """
        method = (getattr(self.request, "method", "") or "").upper()
        return (
            "school.profile.view"
            if method in ("GET", "HEAD", "OPTIONS")
            else "school.profile.update"
        )

    def get_serializer_class(self):
        return (
            SchoolProfileSerializer
            if self.request.method in ("GET", "HEAD", "OPTIONS")
            else SchoolProfileUpdateSerializer
        )

    def get_object(self) -> School:
        """This tenant's school, or a 404 that says the profile is missing.

        ``select_related("branding")`` because the read serializer always asks
        for the logo; without it every GET costs a second query for a row that
        is usually empty.
        """
        from rest_framework.exceptions import NotFound

        tenant = getattr(self.request, "tenant", None)
        school = (
            School.objects.select_related("branding", "tenant")
            .filter(tenant=tenant)
            .first()
        )
        if school is None:
            raise NotFound("This tenant has no school profile.")
        return school

    def _profile_payload(self, school):
        """The whole profile, which is what every write here answers with.

        A save changes more than the field it touched - ``missing_required``
        moves with it - and the caller's next screen is usually the one that
        lists what is left, so returning the record beats returning the field.
        """
        return SchoolProfileSerializer(
            school, context=self.get_serializer_context(),
        ).data

    def get(self, request, *args, **kwargs):
        school = self.get_object()
        return success_response(
            message="School profile retrieved.",
            data=self._profile_payload(school),
        )

    def patch(self, request, *args, **kwargs):
        """Partial update only.

        There is no PUT: a full replace would need name, slug and code in the
        body, and those are the three fields this endpoint refuses to accept.
        """
        school = self.get_object()
        serializer = SchoolProfileUpdateSerializer(
            school,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        school.refresh_from_db()
        return success_response(
            message="School profile updated.",
            data=self._profile_payload(school),
        )



class SchoolLogoView(SchoolProfileView):
    """POST/DELETE /v1/i/me/profile/logo/ - the school's own logo.

    Its own endpoint rather than a field on the profile PATCH, because it is a
    file: a nested ``ImageField`` reached through a JSON body cannot receive
    one, and a serializer field that silently ignores every write is worse than
    no field at all.

    Inherits ``SchoolProfileView`` for the parts that must not differ - the
    pending-tenant surface, the per-method permission keys, and the resolution
    of "which school" from ``request.tenant`` alone. Both verbs are writes, so
    both take ``school.profile.update``: a branch admin who may read the profile
    cannot replace the school's logo.

    docstring-name: Set or clear my school's logo
    """

    parser_classes = [MultiPartParser, FormParser]

    @property
    def rbac_permission(self) -> str:
        # Overrides the parent's read/write split: there is no reading here.
        # Without this, DELETE would be checked against the view key and a
        # branch admin could clear the logo.
        return "school.profile.update"

    def _branding(self, school):
        from ..models import SchoolBranding

        branding, _ = SchoolBranding.objects.get_or_create(school=school)
        return branding

    def _audit(self, school, *, before: str, after: str, summary: str):
        emit_audit_event(
            module_key=AuditModuleKey.SCHOOL,
            action_type=AuditActionType.CONFIG_CHANGED,
            actor_user=getattr(self.request, "user", None),
            tenant=school.tenant,
            entity_type="School",
            # Same entity key as every other school write, so the logo lands on
            # the school's one trail instead of starting a second one.
            entity_id=str(school.pk),
            entity_label=school.name,
            severity=AuditSeverity.INFO,
            summary=summary,
            before_data={"logo": before},
            diff_data=AuditDiffService.diff_dicts(
                before_data={"logo": before}, after_data={"logo": after},
            ),
        )

    def post(self, request, *args, **kwargs):
        """Replace the logo.

        Validated through ``core.uploads.validate_upload``, which checks the
        extension, the size AND the leading bytes - so a script renamed
        ``logo.png`` is refused at the door rather than stored and discovered
        when somebody tries to render it. Storage re-checks both as defence in
        depth, but raises where the caller would receive a 500.
        """
        school = self.get_object()
        upload = request.FILES.get("logo")
        validate_upload(
            upload,
            allowed=LOGO_EXTENSIONS,
            max_bytes=MAX_LOGO_BYTES,
            field="logo",
            size_message="Your logo must be 2MB or smaller.",
            type_message="Upload a PNG, JPG or WEBP image.",
        )

        branding = self._branding(school)
        before = branding.logo.name if branding.logo else ""
        branding.logo = upload
        branding.save()

        self._audit(
            school,
            before=before,
            after=branding.logo.name if branding.logo else "",
            summary=f"Logo updated for {school.name}",
        )

        school.refresh_from_db()
        return success_response(
            message="School logo updated.",
            data=self._profile_payload(school),
        )

    def delete(self, request, *args, **kwargs):
        """Remove the logo, leaving the school with the bundled default.

        Idempotent: clearing a logo that is already absent is a success, not a
        404. There is nothing for the caller to do differently either way.
        """
        school = self.get_object()
        branding = self._branding(school)
        before = branding.logo.name if branding.logo else ""

        if before:
            # ``delete(save=False)`` drops the stored bytes; the column is
            # cleared by the save below. Doing both in one save keeps the row
            # and the file from disagreeing if the second call fails.
            branding.logo.delete(save=False)
            branding.logo = None
            branding.save()
            self._audit(
                school, before=before, after="",
                summary=f"Logo removed for {school.name}",
            )

        school.refresh_from_db()
        return success_response(
            message="School logo removed.",
            data=self._profile_payload(school),
        )
