"""Wiring every view in this module shares.

Three things, all of which the ship-check asks about by name.

**Tenant scoping is the view's job.** Every queryset is filtered on
``request.tenant`` and a row belonging to another tenant answers 404, never
403, so tenant identifiers cannot be enumerated. ``TenantAwareManager`` already
does this eagerly, but the view asserts it anyway, because the manager is
bypassed by ``all_objects`` and by related traversal.

**Every view declares ``pending_tenant_surface``.** A school builds its academic
structure while it is still PENDING - it is a required onboarding task - so
without the declaration every call here answers 403 TENANT_NOT_LIVE and the
school can never go live. Absence means closed, deliberately, so a view added
later is not admitted by default; that is why it is declared per view and
asserted by a test that enumerates the URL conf.

**The branch dimension is resolved once per request**, not once per row.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import branch_dimension_applies


class AcademicsViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pending_tenant_surface = True
    pagination_class = XVSPagination

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed: an unscoped queryset looks like a working endpoint
            # until somebody reads another school's rows.
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
    def session(self):
        """The academic year this request is about, or None if the school has none.

        `?session=<id>` when the screen names one, else the school's ACTIVE
        year, else its latest. Never "all years": levels, classes and subjects
        belong to exactly one, so a list without a year is several years' rows
        piled on top of each other.

        None rather than a refusal, because a school that has not created its
        first year still has to be able to open this module - it is where the
        year gets created. Nothing per-year can exist yet, so narrowing by None
        correctly finds nothing, and departments and programmes still list.
        Writes that must land in a year ask for `session_required` instead.

        On a write it must also be a year that may still be written to. Checked
        here rather than in each view: there are eleven write paths and the one
        that forgets is the one that rewrites history.
        """
        if "_session" in self.__dict__:
            return self._session

        from ..models import AcademicSession, SessionStatus
        from ..services.years import assert_year_is_writable

        raw = str(self.request.query_params.get("session") or "").strip()
        if raw:
            found = AcademicSession.objects.filter(tenant=self.tenant, pk=raw).first()
            if found is None:
                raise NotFound("No such session at this school.")
        else:
            found = (
                AcademicSession.objects.filter(
                    tenant=self.tenant, status=SessionStatus.ACTIVE,
                ).first()
                or AcademicSession.objects.filter(tenant=self.tenant)
                .order_by("-start_date").first()
            )
        if found is not None and self.request.method not in SAFE_METHODS:
            assert_year_is_writable(found)
        self._session = found
        return found

    @property
    def session_required(self):
        """The year, refusing when the school has not made one.

        For the writes that cannot mean anything without a year - a level, a
        class, a subject. The refusal names the next step, which is a better
        answer than a validation error about a field the screen never showed.
        """
        found = self.session
        if found is None:
            from ..exceptions import NoSessionYet

            raise NoSessionYet()
        return found

    def get_object(self):
        """The row, refusing to hand it over for a write if its year has closed.

        The archived-year guard on `session` reads the LENS, and a detail view
        does not use the lens - it resolves a row by primary key. So renaming
        or deleting a 2025/2026 level went straight through while creating one
        was refused, and the rule only looked enforced. A row carries its own
        year, and that is the one that decides.

        Departments and programmes have no year and are unaffected.
        """
        obj = super().get_object()
        if self.request.method not in SAFE_METHODS:
            from ..services.years import assert_year_is_writable

            assert_year_is_writable(getattr(obj, "session", None))
        return obj

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["multi_branch"] = self.multi_branch
        return context


class RecordStateView(AcademicsViewMixin, APIView):
    """Archive or restore one academic record.

    Nothing in this module is deleted. A department, a programme, a level, a
    subject and a class are all part of what a school did in a year, and the
    year is now written on three of them - so a delete does not remove a row
    the school no longer wants, it removes part of the record of a year that
    has already happened.

    Archiving is the whole lifecycle: it is reversible, it takes nothing with
    it, and the row keeps its name and its code so the constraints still see
    it. Children are deliberately left alone. Archiving Junior Secondary does
    not archive JSS1 - the school said one thing, and the other is a decision
    they can make separately and undo separately.

    Subclasses provide `resolve()`, the model's own scoped lookup.
    """

    active: bool
    verb: str

    def resolve(self, pk):
        raise NotImplementedError

    def post(self, request, pk):
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_audit.services import emit_audit_event
        from core.response import success_response
        from ..services.years import assert_year_is_writable

        row = self.resolve(pk)
        assert_year_is_writable(getattr(row, "session", None))

        row.is_active = self.active
        row.save(update_fields=["is_active", "updated_at"])
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type=type(row).__name__, entity_id=str(row.pk),
            entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} {self.verb}.",
        )
        return success_response(f"{row.name} {self.verb}.")
