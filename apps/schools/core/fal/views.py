"""The FAL's HTTP surface.

Why this file exists at all
---------------------------
The FAL was built as a Python boundary and had no ``urls.py`` for months, so
``link_term`` and ``generate_cohort_invoices`` were callable from a shell and
from nothing else. Every other part of the fees chain - the bridge, the dry run,
the student-to-customer resolver, the pricing - was already finished. This is
the front door, and deliberately nothing more: no business rule lives here, and
each view is a thin translation between HTTP and a port that already works.

Two decisions worth knowing
---------------------------
**Tenant scoping happens here, before the port is called.** The bridge raises
``CrossTenantError`` and would catch this on its own, but a fee structure
belonging to another school answers **404, never 403**, so a structure id cannot
be used to learn what another school has priced. The port's own check remains as
the second line, not the first.

**A cohort is always named.** ``vs_finance``'s own batch generation bills every
active customer from a structure. That is reasonable for a handful of clients
and catastrophic for a school, so this surface has no "bill everyone" path at
all: the serializer requires a non-empty student list.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import error_response, success_response
from vs_finance.models import FeeStructure
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import branch_q

from .exceptions import (
    CrossTenantError,
    CustomerNotProvisioned,
    EntityNotProvisioned,
    FALError,
    InvalidTermLinkError,
    TermNotLinkedError,
)
from .due_dates import resolve_due_date
from .models import FeeDueBasis, SchoolFeeDuePolicy
from .registry import get_fee_term_bridge
from .serializers import (
    GenerateInvoicesSerializer,
    LinkTermSerializer,
    generation_payload,
    link_payload,
)

#: FAL refusals that are the caller's fault, and the code each answers with.
#: Anything not listed is a genuine 500 and is left to propagate, because a
#: swallowed FALError would report success for a run that never happened.
_REFUSALS = {
    TermNotLinkedError: (status.HTTP_409_CONFLICT, "TERM_NOT_LINKED"),
    InvalidTermLinkError: (status.HTTP_400_BAD_REQUEST, "INVALID_TERM_LINK"),
    CustomerNotProvisioned: (status.HTTP_400_BAD_REQUEST, "CUSTOMER_NOT_PROVISIONED"),
    EntityNotProvisioned: (status.HTTP_409_CONFLICT, "ENTITY_NOT_PROVISIONED"),
}


class _FalView(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def get_structure(self, pk):
        """The fee structure, or 404 if it is not this school's, or not this caller's.

        Cross-tenant reads answer 404 rather than 403 for the same reason the
        student endpoints do: a 403 confirms the row exists.

        The branch narrowing is the same rule and the same answer. A structure is
        the price list, and this surface links it to a term and bills a cohort
        from it, so a bursar pinned to Ikeja must not reach Lekki's - and a
        template the school publishes for every branch has a null branch and stays
        reachable by all of them, which is why the inclusive form is used here and
        the exclusive one is not.
        """
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No school in context.")
        structure = (
            FeeStructure.objects.select_related("entity")
            .filter(branch_q(self.request, include_shared=True))
            .filter(pk=pk, entity__tenant=tenant)
            .first()
        )
        if structure is None:
            raise NotFound("No such fee structure.")
        return structure

    def refuse(self, exc: FALError):
        for kind, (code, slug) in _REFUSALS.items():
            if isinstance(exc, kind):
                return error_response(str(exc), status=code, code=slug)
        if isinstance(exc, CrossTenantError):
            # The port caught what the view's scoping should already have. Say
            # nothing about the other tenant.
            raise NotFound("No such fee structure.")
        raise exc


class LinkTermView(_FalView):
    """Attach a fee structure to an academic term.

    A structure prices exactly one term and cannot be billed until it is linked,
    so this is the first step of the fees chain rather than a setting.
    """

    rbac_permission = "finance.feestructure.edit"

    def post(self, request, pk):
        structure = self.get_structure(pk)
        payload = LinkTermSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            result = get_fee_term_bridge().link_term(
                structure.pk,
                payload.validated_data["session"],
                payload.validated_data.get("term"),
            )
        except FALError as exc:
            return self.refuse(exc)

        if not result.is_available:
            return error_response(
                result.reason or "The link could not be made.",
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="FINANCE_UNAVAILABLE",
            )
        return success_response(
            message="Fee structure linked to the term.",
            data=link_payload(result.value),
        )


class GenerateInvoicesView(_FalView):
    """Bill a named cohort from a fee structure, or preview what it would bill.

    ``dry_run`` runs the real generation inside a transaction that is rolled
    back, so the total shown is priced by the code that posts rather than by a
    second implementation that would quote a pre-tax figure.
    """

    rbac_permission = "finance.feestructure.generate"

    def post(self, request, pk):
        structure = self.get_structure(pk)
        payload = GenerateInvoicesSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        dry_run = payload.validated_data["dry_run"]

        try:
            result = get_fee_term_bridge().generate_cohort_invoices(
                structure.pk,
                tuple(payload.validated_data["students"]),
                dry_run=dry_run,
            )
        except FALError as exc:
            return self.refuse(exc)

        if not result.is_available:
            return error_response(
                result.reason or "The run could not be made.",
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="FINANCE_UNAVAILABLE",
            )
        return success_response(
            message=(
                "This is what the run would bill."
                if dry_run else
                "Bills raised."
            ),
            data=generation_payload(result.value),
            status=status.HTTP_200_OK if dry_run else status.HTTP_201_CREATED,
        )


class FeeDuePolicyView(APIView):
    """GET / PATCH when this school's fee bills fall due.

    A school that has never opened this answers with the default it is already
    billing by, not with an empty body: there is no "unset" state a bursar can
    observe, because billing always has to pick a date and does.

    ``preview`` on the read is what makes the choice legible. "End of the term
    billed" is an abstraction until it says 15 November, and a bursar choosing
    between four rules should not have to raise an invoice to find out what each
    one means.
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "school.fees.manage" if self.request.method == "PATCH" \
            else "school.fees.view"

    def _payload(self, request, row):
        import datetime

        from schools.vs_academics.models import AcademicSession, AcademicTerm

        today = datetime.date.today()
        tenant = request.tenant
        session = (
            AcademicSession.objects.filter(tenant=tenant, status="ACTIVE")
            .order_by("-start_date").first()
        )
        term = None
        if session is not None:
            term = (
                AcademicTerm.objects.filter(
                    session=session, start_date__lte=today, end_date__gte=today,
                ).first()
                or AcademicTerm.objects.filter(session=session).order_by("start_date").first()
            )

        # What each rule would put on a bill raised today, priced by the same
        # function that bills, so the preview cannot drift from the behaviour.
        preview = {
            basis.value: resolve_due_date(
                basis=basis.value, days_after=row.days_after, invoice_date=today,
                term_end=term.end_date if term else None,
                session_end=session.end_date if session else None,
            ).isoformat()
            for basis in FeeDueBasis
        }
        return {
            "basis": row.basis,
            "basis_display": row.get_basis_display(),
            "days_after": row.days_after,
            "options": [
                {"value": b.value, "label": b.label, "due_if_billed_today": preview[b.value]}
                for b in FeeDueBasis
            ],
            "resolved_against": {
                "session": session.name if session else None,
                "term": term.name if term else None,
            },
        }

    def _row(self, request):
        row = SchoolFeeDuePolicy.objects.filter(tenant=request.tenant).first()
        return row or SchoolFeeDuePolicy(tenant=request.tenant)

    def get(self, request):
        return success_response(
            "Fee due policy retrieved.", data=self._payload(request, self._row(request)),
        )

    @transaction.atomic
    def patch(self, request):
        row = self._row(request)
        body = request.data or {}

        if "basis" in body:
            basis = str(body.get("basis") or "").upper()
            if basis not in FeeDueBasis.values:
                return error_response(
                    f"Basis must be one of {', '.join(FeeDueBasis.values)}.",
                    status=status.HTTP_400_BAD_REQUEST, code="INVALID_BASIS",
                )
            row.basis = basis

        if "days_after" in body:
            try:
                days = int(body.get("days_after"))
            except (TypeError, ValueError):
                days = -1
            # Bounded rather than merely non-negative: zero means the bill is due
            # the day it is raised, which a school may genuinely want, while a
            # year of credit on a term's fees is a typo every time.
            if not 0 <= days <= 365:
                return error_response(
                    "Days after the bill must be between 0 and 365.",
                    status=status.HTTP_400_BAD_REQUEST, code="INVALID_DAYS_AFTER",
                )
            row.days_after = days

        row.updated_by = request.user
        row.save()
        return success_response(
            "Fee due policy updated.", data=self._payload(request, row),
        )
