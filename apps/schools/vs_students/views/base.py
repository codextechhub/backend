"""Wiring every view in this module shares.

Three things, all of which the repo's ship-check asks about by name.

**Tenant scoping is the view's job.** Every queryset is filtered on
``request.tenant`` and a row belonging to another tenant answers **404, never
403**, so a student id cannot be used to learn that a child exists at another
school. ``TenantAwareManager`` already does this eagerly, but the view asserts
it anyway, because the manager is bypassed by ``all_objects`` and by related
traversal.

**No view here declares ``pending_tenant_surface``, and that is deliberate.**
Absence means closed. Enrolling a student, transferring a class and running a
promotion are operations of a live school; a school still onboarding has no
active session to enrol into. The one student-shaped thing a PENDING school
does need - loading its existing roll - happens on the import engine's surface,
which declares the attribute already. A view added to this module later without
thinking about it is closed rather than open by omission, which is the right
default.

**The branch dimension is resolved once per request**, not once per row.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import branch_dimension_applies


class StudentsViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pagination_class = XVSPagination

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed: an unscoped queryset looks like a working endpoint
            # until somebody reads another school's children.
            raise NotFound("No school in context.")
        return tenant

    @property
    def multi_branch(self) -> bool:
        cached = getattr(self, "_multi_branch", None)
        if cached is None:
            cached = branch_dimension_applies(self.tenant)
            self._multi_branch = cached
        return cached

    @property
    def active_session(self):
        """The school year this module works in. There is only ever one.

        Cached per request: eight of these views ask for it, and asking eight
        times is eight queries for a value that cannot change mid-request.
        """
        if "_session" in self.__dict__:
            return self._session
        from ..services.placement import active_session

        self._session = active_session(self.tenant)
        return self._session

    @property
    def session_or_none(self):
        """The active year, or None. For reads that must still answer without one.

        A school between years still has students, and a directory that
        answered NO_ACTIVE_SESSION would be unopenable at exactly the moment a
        registrar needs it.
        """
        from ..exceptions import NoActiveSession
        from ..services.placement import active_session

        try:
            return active_session(self.tenant)
        except NoActiveSession:
            return None

    # ── paginating from a plain APIView ────────────────────────────────────

    def paginate_queryset(self, queryset):
        """Page a list on an ``APIView``, which has no paginator of its own.

        ``pagination_class`` is set on this mixin, so all twenty-two views in
        the module *look* paginated - including the twelve that are plain
        ``APIView``s, where the attribute does nothing because DRF only builds
        a paginator on ``GenericAPIView``. Calling ``paginate_queryset`` there
        raises AttributeError and DRF turns it into a bare 500, which is how
        the profile's History tab answered 500 for every student on every
        school.

        Exactly the trap ``get_serializer_context`` below already documents:
        an APIView reaching for generic machinery it does not have. Fixed here
        rather than in the one view that hit it, so the next list served from
        an APIView pages instead of failing.

        Generic views are untouched - the mixin is first in every MRO, so
        ``super()`` finds DRF's own implementation and defers to it.
        """
        parent = getattr(super(), "paginate_queryset", None)
        if parent is not None:
            return parent(queryset)
        if getattr(self, "_apiview_paginator", None) is None:
            self._apiview_paginator = (
                self.pagination_class() if self.pagination_class else None
            )
        if self._apiview_paginator is None:
            return None
        return self._apiview_paginator.paginate_queryset(
            queryset, self.request, view=self,
        )

    def get_paginated_response(self, data):
        parent = getattr(super(), "get_paginated_response", None)
        if parent is not None:
            return parent(data)
        return self._apiview_paginator.get_paginated_response(data)

    def get_serializer_context(self):
        """The context every serializer here needs, on generic views AND APIView.

        ``APIView`` has no ``get_serializer_context``, so calling ``super()``
        unconditionally raised AttributeError on the twelve action views - and
        DRF turned that into a bare 500 with no field to look at, which made a
        withdrawal look like a server fault rather than a missing method.
        """
        getter = getattr(super(), "get_serializer_context", None)
        context = getter() if getter is not None else {
            "request": self.request, "view": self,
            "format": self.request.parser_context.get("kwargs", {}).get("format"),
        }
        context["multi_branch"] = self.multi_branch
        return context

    # ── the branch lens ────────────────────────────────────────────────────

    @property
    def branch_filter(self):
        """The branch this request asked to be narrowed to, or None.

        Resolved ONCE per request and cached, and read by every list and every
        aggregate in the module. Resolved inline in the student list alone, the
        directory's table narrows to a branch while the summary above it keeps
        answering for the whole school: 87 students printed over 49 rows, with
        nothing on screen saying which is which, and a registrar reporting a
        branch's roll reads the wrong number.

        Ignored at a single-branch school: the dimension has receded from every
        response there, so a branch parameter is meaningless rather than wrong.
        An unknown branch is a validation error, not a silent whole-school
        answer - a filter that quietly does nothing is worse than one that
        refuses.
        """
        if "_branch_filter" in self.__dict__:
            return self._branch_filter
        raw = (self.request.query_params.get("branch") or "").strip()
        if not raw or not self.multi_branch:
            self._branch_filter = None
            return None
        from vs_tenants.references import resolve_branch_reference

        self._branch_filter = resolve_branch_reference(self.tenant, raw, "branch")
        return self._branch_filter

    @property
    def session_filter(self):
        """The year this request is reading, or None for the school's current one.

        A person is not per-session, but their PLACEMENT is, and so is the roll
        itself: Lagoon View had 85 students in 2026/2027 and has 73 in
        2027/2028, and a child in SSS1 A last year is in SSS2 A this year. So
        "which year" is a real question about students even though status,
        guardians and documents carry no year at all.

        None means the module's existing behaviour - the active year, read
        through each enrolment's ``is_active`` flag. A named year means the
        register AS IT WAS: see ``enrolment_for`` for why that cannot use
        ``is_active``.
        """
        if "_session_filter" in self.__dict__:
            return self._session_filter
        raw = (self.request.query_params.get("session") or "").strip()
        if not raw:
            self._session_filter = None
            return None
        from rest_framework.exceptions import ValidationError

        from schools.vs_academics.models import AcademicSession

        row = AcademicSession.objects.filter(tenant=self.tenant, pk=raw).first()
        if row is None:
            raise ValidationError({"session": "No such year at this school."})
        self._session_filter = row
        return row

    def narrow_to_branch(self, queryset, field="branch"):
        """Apply :attr:`branch_filter` to *queryset*, or hand it back whole."""
        branch = self.branch_filter
        if branch is None:
            return queryset
        return queryset.filter(**{field: branch})

    # ── shared resolvers ───────────────────────────────────────────────────

    def student(self, pk):
        from ..services.scoping import get_student_or_404

        return get_student_or_404(self.tenant, self.request.user, pk)

    def guardian(self, pk):
        from ..services.scoping import get_guardian_or_404

        return get_guardian_or_404(self.tenant, pk)

    def assert_holds(self, *keys):
        """Every one of *keys*, not any of them.

        ``rbac_permission`` is deliberately **any-of** so a view can accept
        equivalent grants, which is the wrong shape for the two routes that
        need two different powers at once: enrolling creates a record AND
        seats the child in a class, and a caller who may register a child must
        not thereby be able to place them anywhere they like. Listing both keys
        on ``rbac_permission`` would let either one alone through.
        """
        from rest_framework.exceptions import PermissionDenied

        from vs_rbac.permissions import has_permission, is_vision_super_admin

        user = self.request.user
        if is_vision_super_admin(user):
            return
        for key in keys:
            if not has_permission(user, key, tenant=self.tenant):
                raise PermissionDenied(
                    f"You do not hold {key}, which this action needs as well.",
                )

    def audit(self, action_type, student, summary, **metadata):
        from vs_audit.models import AuditModuleKey
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key=AuditModuleKey.STUDENT, action_type=action_type,
            entity_type="Student", entity_id=str(student.pk),
            entity_label=student.full_name,
            tenant=self.tenant, actor_user=self.request.user,
            summary=summary, metadata=metadata or None,
        )
