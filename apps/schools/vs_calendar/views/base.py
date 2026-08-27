"""Wiring every view in this module shares.

The same three things ``vs_academics.views.base`` sets out, for the same
reasons, plus one this module adds.

**Tenant scoping is the view's job.** Every queryset is filtered on
``request.tenant`` and a row belonging to another tenant answers 404, never 403,
so tenant identifiers cannot be enumerated. ``TenantAwareManager`` already does
this eagerly, but the view asserts it anyway, because the manager is bypassed by
``all_objects`` and by related traversal.

**Every view declares ``pending_tenant_surface``.** A school builds its calendar
and its bell schedule while it is still PENDING, so without the declaration
every call here answers 403 TENANT_NOT_LIVE. Absence means closed, deliberately,
so a view added later is not admitted by default.

**The branch dimension is resolved once per request**, not once per row.

**And the one this module adds: a warning is not a refusal.** Several writes
here succeed and still have something to say - a clash, a date outside every
term, an overlap. They travel in ``data.warnings`` beside the row that was
written, never as an error, because the write happened.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import branch_dimension_applies, visible_branch_id_set


class CalendarViewMixin:
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
    def visible(self):
        cached = getattr(self, "_visible", "__unset__")
        if cached == "__unset__":
            cached = visible_branch_id_set(self.request.user, self.tenant)
            self._visible = cached
        return cached

    @property
    def session(self):
        """The school year this request is about, or None if there is none.

        ``?session=<id>`` when the screen names one, else the school's ACTIVE
        year, else its latest. Never "all years": an event, a period and a slot
        each belong to exactly one, so a list without a year is several years'
        rows piled on top of each other.

        On a write it must also be a year that may still be written to. Checked
        here rather than in each view, because there are a dozen write paths and
        the one that forgets is the one that rewrites history.
        """
        if "_session" in self.__dict__:
            return self._session

        from schools.vs_academics.models import AcademicSession, SessionStatus
        from schools.vs_academics.services.years import assert_year_is_writable

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

        The refusal names the next step, which is a better answer than a
        validation error about a field the screen never showed.
        """
        found = self.session
        if found is None:
            from schools.vs_academics.exceptions import NoSessionYet

            raise NoSessionYet()
        return found

    def get_object(self):
        """The row, refusing to hand it over for a write if its year has closed.

        The guard on ``session`` reads the LENS, and a detail view does not use
        the lens - it resolves a row by primary key. A row carries its own year,
        and that is the one that decides.
        """
        obj = super().get_object()
        if self.request.method not in SAFE_METHODS:
            from schools.vs_academics.services.years import assert_year_is_writable

            year = getattr(obj, "session", None)
            if year is None:
                year = getattr(getattr(obj, "calendar_event", None), "session", None)
            assert_year_is_writable(year)
        return obj

    def get_serializer_context(self):
        """Works on a plain APIView as well as a generic one.

        Several surfaces here - the grid, the teacher's week, the overview -
        are single documents rather than lists, so they are APIViews, which has
        no ``get_serializer_context``. Calling ``super()`` unconditionally made
        every one of them a 500.
        """
        parent = getattr(super(), "get_serializer_context", None)
        context = parent() if parent is not None else {
            "request": self.request, "view": self,
        }
        context["multi_branch"] = self.multi_branch
        return context


class CalendarActionView(CalendarViewMixin, APIView):
    """Base for the POST-only action routes: publish, duplicate, clear."""
