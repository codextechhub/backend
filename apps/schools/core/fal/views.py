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

    def refuse_unseen_students(self, student_refs):
        """404 if the cohort names a child this caller cannot see.

        The structure is scoped above, but a cohort is a list of ids the caller
        typed, and the bridge checks only that each child is this school's. A
        bursar pinned to Ikeja who can read one Lekki pupil's id would otherwise
        bill that child from an Ikeja screen that never listed them. Students
        are read exclusively, as they are everywhere else: a child always
        belongs to one branch.

        Only references that name a student row are checked. A reference that
        is not a student's primary key is an imported receivable the bridge
        resolves by entity, and a child of another school is the bridge's
        ``CrossTenantError``, which also answers 404.
        """
        from schools.vs_students.models import Student
        from schools.vs_students.services.scoping import scope_students

        ids = {int(ref) for ref in student_refs if str(ref).isdigit()}
        if not ids:
            return
        tenant = self.request.tenant
        named = Student.all_objects.filter(tenant=tenant, pk__in=ids)
        visible = scope_students(named, self.request.user, tenant).values("pk")
        if named.exclude(pk__in=visible).exists():
            raise NotFound("No such student.")

    def students_off_the_price_list(self, structure, student_refs):
        """How many named children attend a branch other than the structure's.

        A structure with a branch is that branch's price list: Lekki's JSS 1
        fee is not Ikeja's. A caller who sees every branch can still name any
        child, so the rule is held here rather than left to the screen that
        lists the classes. A school-wide structure (no branch) prices every
        branch and is never refused.
        """
        from schools.vs_students.models import Student

        if not structure.branch_id:
            return 0
        ids = {int(ref) for ref in student_refs if str(ref).isdigit()}
        return (
            Student.all_objects.filter(tenant=self.request.tenant, pk__in=ids)
            .exclude(branch_id=structure.branch_id)
            .count()
        )

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
    """Read or set the academic term a fee structure bills.

    A structure prices exactly one term and cannot be billed until it is linked,
    so this is the first step of the fees chain rather than a setting.

    The read lets a billing screen say which term a run is for, and load that
    year's classes, before anybody is chosen. Without it the only way to learn
    the link was a dry run, which needs a cohort, so a bursar picked classes
    first and was told afterwards that nothing could be billed. An unlinked
    structure answers ``{"linked": false}`` rather than 404, because it exists
    and simply has no term yet.
    """

    @property
    def rbac_permission(self):
        return "finance.feestructure.view" if self.request.method == "GET" \
            else "finance.feestructure.edit"

    def get(self, request, pk):
        from .contracts import FeeTermLink
        from .models import FeeStructureTermLink

        structure = self.get_structure(pk)
        link = (
            FeeStructureTermLink.objects.filter(fee_structure=structure)
            .select_related("session", "term").first()
        )
        if link is None:
            return success_response(
                message="Fee structure is not linked to a term.",
                data={"linked": False},
            )
        return success_response(
            message="Fee structure link retrieved.",
            data={"linked": True, **link_payload(FeeTermLink(
                fee_structure_ref=structure.pk,
                session_ref=link.session_id,
                term_ref=link.term_id,
                entity_ref=structure.entity_id,
                session_label=link.session.name,
                term_label=link.term.name if link.term_id else "",
            ))},
        )

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
        self.refuse_unseen_students(payload.validated_data["students"])
        off_list = self.students_off_the_price_list(
            structure, payload.validated_data["students"],
        )
        if off_list:
            return error_response(
                f"{off_list} of the children named attend another branch. "
                "This fee structure prices one branch only; bill them from "
                "their own branch's structure.",
                status=status.HTTP_409_CONFLICT,
                code="WRONG_BRANCH",
            )

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
        return "school.fees.update" if self.request.method == "PATCH" \
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
