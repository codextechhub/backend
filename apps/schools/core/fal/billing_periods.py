"""The academic term as finance's billing period.

Registered as ``FINANCE_BILLING_PERIOD_PROVIDER`` (see
:mod:`vs_finance.billing_periods`). For a school's books it answers with the term
the school is in, and the invoices raised from the fee structures linked to that
term (``FEE:<code>`` references, the same rule the FAL's read port uses). Books
that are not a school's get ``None``.

Between terms the answer is the term that last started: in the holiday after
First Term the bursar is still collecting First Term fees, and "This term"
should keep meaning the term those fees were billed for until Second Term
begins. A term in a draft or archived session is not considered, so next
year's calendar being set up early never takes over the dashboard.
"""
from __future__ import annotations

from django.db.models import Q

from vs_finance.billing_periods import BillingPeriod


def current_term(entity, as_of):
    """The school's current term as a :class:`BillingPeriod`, or ``None``."""
    from vs_tenants.models import Tenant

    from schools.vs_academics.models import AcademicTerm

    from .models import FeeStructureTermLink

    tenant = getattr(entity, "tenant", None)
    if tenant is None or tenant.kind != Tenant.Kind.SCHOOL:
        return None
    term = (
        AcademicTerm.all_objects
        .filter(
            tenant=tenant, session__status="ACTIVE", archived_at__isnull=True,
            start_date__lte=as_of,
        )
        .select_related("session")
        .order_by("-start_date")
        .first()
    )
    if term is None:
        return None
    codes = FeeStructureTermLink.objects.filter(
        term=term, fee_structure__entity=entity,
    ).values_list("fee_structure__code", flat=True)
    return BillingPeriod(
        key="term",
        label="This term",
        name=f"{term.name} {term.session.name}",
        start=term.start_date,
        end=term.end_date,
        invoices=Q(reference__in=[f"FEE:{code}" for code in codes]),
    )
