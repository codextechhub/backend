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
