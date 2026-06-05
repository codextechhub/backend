"""Shared enumerations and constants for the finance engine.

Defined once here so Phase-1 models (Account, FiscalPeriod, JournalEntry …) and the
posting service agree on the same vocabulary without circular imports.
"""
from __future__ import annotations

from django.db import models


class PeriodStatus(models.TextChoices):
    """Lifecycle of a fiscal period.

    OPEN          -> postings allowed.
    SOFT_CLOSED   -> normal users blocked; admins/auto-postings (e.g. depreciation
                     at close) may still post. Reversible.
    CLOSED        -> no postings; reversible only by an explicit re-open with audit.
    LOCKED        -> permanently sealed (e.g. after statutory filing). No postings.
    """
    OPEN = "OPEN", "Open"
    SOFT_CLOSED = "SOFT_CLOSED", "Soft Closed"
    CLOSED = "CLOSED", "Closed"
    LOCKED = "LOCKED", "Locked"


#: Statuses into which an ordinary journal may NOT be posted.
PERIOD_POSTING_BLOCKED = frozenset({
    PeriodStatus.CLOSED,
    PeriodStatus.LOCKED,
})

#: Statuses into which only privileged/system postings (close auto-entries) may go.
PERIOD_POSTING_RESTRICTED = frozenset({
    PeriodStatus.SOFT_CLOSED,
})


class DocumentStatus(models.TextChoices):
    """Generic lifecycle for numbered finance documents.

    Concrete documents (invoices, POs, journals) may use a subset or extend this;
    the abstract :class:`~vs_finance.models.FinanceDocument` defaults to it.
    """
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    APPROVED = "APPROVED", "Approved"
    POSTED = "POSTED", "Posted"
    REVERSED = "REVERSED", "Reversed"
    CANCELLED = "CANCELLED", "Cancelled"


class DocType(models.TextChoices):
    """Document-type tokens used by the numbering sequence.

    The token becomes the middle segment of a document number, e.g. ``INV`` in
    ``CFX-B01-INV-2026-00821``. Keep tokens short, uppercase and stable — they are
    persisted inside human-facing identifiers.
    """
    JOURNAL = "JNL", "Journal Entry"
    INVOICE = "INV", "Sales / AR Invoice"
    RECEIPT = "RCP", "Receipt"
    PAYMENT = "PAY", "Payment"
    CREDIT_NOTE = "CRN", "Credit Note"
    PURCHASE_REQUISITION = "PR", "Purchase Requisition"
    RFQ = "RFQ", "Request for Quotation"
    PURCHASE_ORDER = "PO", "Purchase Order"
    GOODS_RECEIVED = "GRN", "Goods Received Note"
    VENDOR_INVOICE = "VIN", "Vendor Invoice"
    VENDOR_PAYMENT = "VPY", "Vendor Payment"
    EXPENSE_CLAIM = "EXP", "Expense Claim"

#: Document-number prefix for the whole platform's finance documents (Code X Finance).
DOC_NUMBER_PREFIX = "CFX"

#: Reserved code for CodeX's own platform set of books (the operator's entity).
#: An uppercase identifier (like all entity codes); the display name is "CodeX".
PLATFORM_ENTITY_CODE = "CODEX"
