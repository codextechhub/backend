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

from .exceptions import (
    CrossTenantError,
    CustomerNotProvisioned,
    EntityNotProvisioned,
    FALError,
    InvalidTermLinkError,
    TermNotLinkedError,
)
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
        """The fee structure, or 404 if it is not this school's.

        Cross-tenant reads answer 404 rather than 403 for the same reason the
        student endpoints do: a 403 confirms the row exists.
        """
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise NotFound("No school in context.")
        structure = (
            FeeStructure.objects.select_related("entity")
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
