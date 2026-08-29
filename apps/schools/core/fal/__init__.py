"""
schools.core.fal - Finance Abstraction Layer
============================================

A **school-aware** contract over the generic finance, payments and procurement
subsystems (``vs_finance`` / ``vs_payments`` / ``vs_procurement``). School
modules (M9 onboarding, M11 students, M25 dashboards, M26 reports, M28 parent
portal, school procurement) depend on this package's *ports* and never import
finance or procurement models directly.

Where this package belongs
--------------------------
**The school layer, which is where it now is.**

The FAL exists precisely *because* school concepts meet the finance engines, so
school vocabulary (school, student, guardian, session, term) belongs inside it.
Putting it in a domain-neutral app would plant school words where they must never
go. The FAL is the boundary: school words stop here, and the engines below stay
neutral.

Dependency direction, explicitly: **the schools package may import the engines;
the engines may never import the schools package.**

Public surface (accessors, port types, DTOs)::

    from schools.core.fal import (
        # DI accessors, one per port
        get_entity_resolver, get_fee_term_bridge, get_student_customer,
        get_finance_rbac, get_finance_reader, get_parent_payment_bridge,
        get_procurement_reader, get_procurement_actions, get_guardian_link,
        # port types
        EntityResolverPort, FeeTermBridgePort, StudentCustomerPort,
        FinanceRbacPort, FinanceReadPort, ParentPaymentBridgePort,
        ProcurementReadPort, ProcurementActionPort, GuardianLinkPort,
        # envelope / money / refs
        FinanceResult, Availability, Unavailable, Kobo, Ref, LooseRef,
        # ... plus the DTOs in schools.core.fal.contracts
    )

This module deliberately imports nothing from ``models`` or ``adapters``: it is
loaded while the Django app registry is still populating, and it must stay
importable without a configured settings module.

Decision (2026-07-04): ``PaymentPort`` / ``ApplyPaymentCommand`` and the other
payment-application types are **deferred to FAL v1.2** and are intentionally NOT
part of this public surface. Settlement stays inside ``vs_payments``
(``confirm_collection`` -> ``_book_receipt``); the quarantined v1.2 starting
points live in ``ports`` / ``contracts`` under clearly-marked "v1.2 (deferred)"
sections.
"""

from __future__ import annotations

from . import contracts, exceptions, registry
from .contracts import (
    SOURCE_TYPE_STUDENT,
    AgeingBucket,
    AgeingRow,
    ApprovalDecision,
    ApprovalOverride,
    ApprovalSubmission,
    ArAgeingReport,
    Availability,
    BillLine,
    BranchRef,
    CustomerHandle,
    CustomerRef,
    DateRange,
    DebtorRow,
    DocRef,
    EntityHandle,
    EntityRef,
    FeeLiability,
    FeeRow,
    FeeStatus,
    FeeStructureRef,
    FeeTermLink,
    FilterClause,
    FinanceResult,
    GuardianRef,
    InvoiceGenerationResult,
    InvoiceLine,
    InvoiceRef,
    InvoiceStatus,
    InvoiceView,
    Kobo,
    KpiValue,
    LooseRef,
    Page,
    PaymentMethod,
    PaymentRef,
    PaymentRow,
    Period,
    ProcApprovalState,
    ProcDocRef,
    ProcDocType,
    ProcDocument,
    ProcurementRow,
    ProcurementSnapshot,
    Receipt,
    ReceiptLine,
    Ref,
    SchoolRef,
    Series,
    SeriesPoint,
    SessionRef,
    StudentRef,
    TermRef,
    Unavailable,
    Unit,
    UserRef,
    VendorRef,
    WorkflowInstanceRef,
)
from .ports import (
    EntityResolverPort,
    FeeTermBridgePort,
    FinanceRbacPort,
    FinanceReadPort,
    GuardianLinkPort,
    ParentPaymentBridgePort,
    ProcurementActionPort,
    ProcurementReadPort,
    StudentCustomerPort,
)
from .registry import (
    get_entity_resolver,
    get_fee_term_bridge,
    get_finance_rbac,
    get_finance_reader,
    get_guardian_link,
    get_parent_payment_bridge,
    get_procurement_actions,
    get_procurement_reader,
    get_student_customer,
)

#: Correction release, on the same grounds v1.1.1 used: the contract has no
#: consumers yet, so a correction beats a major bump. 1.1.2 reconciles the
#: contract with the backend as it actually stands in August 2026 - Branch lives
#: in vs_tenants, apps/schools/ exists, the academic calendar app exists (so
#: session and term refs are integers and the link table carries real foreign
#: keys), success_response no longer flattens an empty list, and the RBAC
#: evaluator's branch default is the ANY_BRANCH sentinel rather than None. It
#: also adds the GuardianLinkPort seam, because decision 5's ownership check has
#: nothing in the repository to consult. No capability and no decision changes
#: meaning.
FAL_CONTRACT_VERSION = "1.1.2"

default_app_config = "schools.core.fal.apps.FalConfig"

__all__ = [
    "FAL_CONTRACT_VERSION",
    # accessors
    "get_entity_resolver",
    "get_fee_term_bridge",
    "get_student_customer",
    "get_finance_rbac",
    "get_finance_reader",
    "get_guardian_link",
    "get_parent_payment_bridge",
    "get_procurement_reader",
    "get_procurement_actions",
    # ports
    "EntityResolverPort",
    "FeeTermBridgePort",
    "StudentCustomerPort",
    "FinanceRbacPort",
    "FinanceReadPort",
    "GuardianLinkPort",
    "ParentPaymentBridgePort",
    "ProcurementReadPort",
    "ProcurementActionPort",
    # envelope / money
    "FinanceResult",
    "Availability",
    "Unavailable",
    "Kobo",
    # refs
    "Ref",
    "LooseRef",
    "SchoolRef",
    "BranchRef",
    "EntityRef",
    "CustomerRef",
    "InvoiceRef",
    "PaymentRef",
    "FeeStructureRef",
    "UserRef",
    "VendorRef",
    "DocRef",
    "WorkflowInstanceRef",
    "SessionRef",
    "TermRef",
    "StudentRef",
    "GuardianRef",
    "SOURCE_TYPE_STUDENT",
    # scoping
    "Period",
    "DateRange",
    "FilterClause",
    "Page",
    # kpi / dashboard contracts
    "KpiValue",
    "Unit",
    "Series",
    "SeriesPoint",
    "ArAgeingReport",
    "AgeingRow",
    "FeeLiability",
    # rows
    "DebtorRow",
    "AgeingBucket",
    "FeeRow",
    "PaymentRow",
    "PaymentMethod",
    "InvoiceStatus",
    # per-entity
    "InvoiceLine",
    "InvoiceView",
    "FeeStatus",
    "Receipt",
    # procurement reads
    "ProcurementSnapshot",
    "ProcurementRow",
    # procurement actions (component 7)
    "ProcDocType",
    "ProcApprovalState",
    "ProcDocRef",
    "ProcDocument",
    "ApprovalSubmission",
    "ApprovalDecision",
    "ApprovalOverride",
    "ReceiptLine",
    "BillLine",
    # component 1/2/3 handles
    "EntityHandle",
    "FeeTermLink",
    "InvoiceGenerationResult",
    "CustomerHandle",
    # NOTE: the payment-application types (Allocation, ApplyPaymentCommand,
    # AppliedInvoice, PaymentApplication) and PaymentPort are v1.2 (deferred)
    # and are deliberately NOT exported. Decision 2026-07-04.
    # submodules
    "contracts",
    "exceptions",
    "registry",
]
