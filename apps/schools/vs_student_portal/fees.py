"""A pupil's own fees in the portal: what is owed, what was paid, and paying.

Everything is read and started through the finance abstraction layer, never
the ledger: ``FinanceReadPort.fee_status`` for the invoices and
``StudentPaymentPort`` for the history, the receipts and a checkout. The portal
holds no money and books none; a payment it starts is settled by
``vs_payments`` when the provider confirms it.

Paying is the fallback for a family that cannot pay through their own app, so
the school decides whether its pupils may do it at all and the smallest part
payment it takes (:mod:`schools.core.fal.student_payments`). The whole balance
of an invoice may always be paid, however small it is.

Money crosses this boundary as integer kobo; the portal formats it.
"""
from __future__ import annotations

from schools.core.fal import registry
from vs_finance.money import format_naira
from schools.core.fal.exceptions import CrossTenantError, PaymentGatewayError
from schools.core.fal.student_payments import read_student_payment_rules
from rest_framework.exceptions import NotFound

from .links import student_app_url
from .exceptions import (
    DepositSinceInvalid,
    AmountAboveBalance,
    AmountBelowMinimum,
    FinanceUnavailable,
    InvoiceNotPayable,
    PaymentAmountInvalid,
    PaymentProviderUnavailable,
    StudentPaymentOff,
)

#: Where the gateway returns a pupil after a card payment, inside the student
#: app; the gateway adds ``?reference=`` and the page polls the collection's
#: status by it.
PAID_PATH = "fees/paid"

#: Where a payment came from, as the history says it.
SOURCE_LABEL = {
    "TRANSFER_ACCOUNT": "Transfer into your account",
    "ONLINE": "Paid online",
    "SCHOOL": "Recorded by the school",
}

STATUS_LABEL = {"UNPAID": "Owing", "PARTIAL": "Part-paid", "PAID": "Paid"}
METHOD_LABEL = {
    "CARD": "Card", "BANK_TRANSFER": "Bank transfer", "USSD": "USSD", "CASH": "Cash",
    "CHEQUE": "Cheque", "ONLINE": "Online", "OTHER": "Other",
}


def _ref(student) -> str:
    return str(student.pk)


def _status(invoice) -> str:
    return getattr(invoice.status, "value", invoice.status)


def fee_summary(student) -> dict:
    """The pupil's balance and invoices, and whether they may pay."""
    result = registry.get_finance_reader().fee_status(_ref(student))
    if not result.is_available:
        raise FinanceUnavailable()
    status = result.value
    rules = read_student_payment_rules(student.tenant)
    return {
        "balance": status.balance,
        "total_billed": status.total_billed,
        "total_paid": status.total_paid,
        "invoices": [
            {
                "invoice_ref": invoice.invoice_ref,
                "number": invoice.document_number,
                "title": invoice.term_label,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "lines": [{"description": l.description, "amount": l.amount} for l in invoice.lines],
                "total": invoice.amount_due,
                "paid": invoice.amount_paid,
                "balance": invoice.balance,
                "status": _status(invoice),
                "status_label": STATUS_LABEL.get(_status(invoice), _status(invoice)),
            }
            for invoice in status.invoices
        ],
        "can_pay": rules.enabled,
        "min_part_payment": rules.min_part_payment,
    }


def payment_history(student) -> list:
    """The pupil's payments, newest first, confirmed and still being confirmed."""
    result = registry.get_student_payment().payment_history(_ref(student))
    if not result.is_available:
        raise FinanceUnavailable()
    return [
        {
            "payment_ref": row.payment_ref,
            "reference": row.reference,
            "amount": row.amount,
            "status": row.status,
            "status_label": "Confirmed" if row.status == "CONFIRMED" else "Pending",
            "method": row.method,
            "method_label": METHOD_LABEL.get(row.method, row.method),
            "paid_on": row.paid_on,
            "payer_name": row.payer_name,
            "receipt_number": row.receipt_number,
            "invoice_number": row.invoice_number,
            "source": row.source,
            "source_label": SOURCE_LABEL.get(row.source, ""),
            "has_receipt": row.status == "CONFIRMED" and row.payment_ref is not None,
        }
        for row in result.value
    ]


def start_payment(student, *, actor, invoice_ref, amount) -> dict:
    """Check the payment against the school's rules and hand back a checkout.

    The rules are read here, at the school's end, and the balance from the
    pupil's own invoices, so an invoice that is not theirs is not found before
    anything is asked of the gateway. The checkout returns the pupil to the
    student app's paid page (``/student/fees/paid``).
    """
    rules = read_student_payment_rules(student.tenant)
    if not rules.enabled:
        raise StudentPaymentOff()
    summary = fee_summary(student)
    invoice = next(
        (row for row in summary["invoices"] if str(row["invoice_ref"]) == str(invoice_ref)),
        None,
    )
    if invoice is None:
        raise NotFound("No such invoice on your account.")
    balance = invoice["balance"]
    if balance <= 0:
        raise InvoiceNotPayable("This invoice is already paid.")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise PaymentAmountInvalid("Enter the amount to pay.", field="amount") from None
    if amount <= 0:
        raise PaymentAmountInvalid("Enter an amount of more than ₦0.00.", field="amount")
    if amount > balance:
        raise AmountAboveBalance(
            f"You can pay at most the balance on this invoice, {format_naira(balance)}.",
            field="amount", balance=balance,
        )
    if amount < balance and amount < rules.min_part_payment:
        raise AmountBelowMinimum(
            f"The smallest part payment your school accepts is "
            f"{format_naira(rules.min_part_payment)}. Enter {format_naira(rules.min_part_payment)} "
            f"or more, or pay the full balance.",
            field="amount", minimum=rules.min_part_payment,
        )
    try:
        result = registry.get_student_payment().start_payment_session(
            student_ref=_ref(student), invoice_ref=invoice["invoice_ref"],
            amount=amount, actor=actor,
            callback_url=student_app_url(student.tenant, PAID_PATH),
        )
    except CrossTenantError:
        raise NotFound("No such invoice on your account.") from None
    except PaymentGatewayError as exc:
        raise InvoiceNotPayable() from exc
    if not result.is_available:
        raise PaymentProviderUnavailable()
    checkout = result.value
    return {
        "checkout_url": checkout.checkout_url,
        "reference": checkout.reference,
        "amount": amount,
        "balance_after": balance - amount,
    }


def receipt(student, payment_ref) -> dict:
    """A confirmed payment's receipt, or 404 when there is none for this pupil."""
    result = registry.get_student_payment().receipt(_ref(student), payment_ref)
    if not result.is_available:
        raise FinanceUnavailable()
    doc = result.value
    if doc is None:
        raise NotFound("No receipt for that payment.")
    return {
        "payment_ref": doc.payment_ref,
        "receipt_number": doc.receipt_number,
        "paid_on": doc.paid_on,
        "amount": doc.amount,
        "method": doc.method,
        "method_label": METHOD_LABEL.get(doc.method, doc.method),
        "reference": doc.reference,
        "payer_name": doc.payer_name,
        "received_from": doc.customer_name,
        "lines": [
            {
                "invoice_number": line.invoice_number,
                "title": line.term_label,
                "amount_applied": line.amount_applied,
                "balance_after": line.balance_after,
            }
            for line in doc.lines
        ],
        "balance_after": doc.balance_after,
        "issuer_name": doc.issuer_name,
    }


def receipt_pdf(student, payment_ref) -> bytes:
    """The receipt as the PDF the school's receipt email attaches; 404 otherwise."""
    result = registry.get_student_payment().receipt_pdf(_ref(student), payment_ref)
    if not result.is_available:
        raise FinanceUnavailable()
    if result.value is None:
        raise NotFound("No receipt for that payment.")
    return result.value


# ── Bank transfer ─────────────────────────────────────────────────────────

TRANSFER_UNAVAILABLE_SENTENCE = (
    "Your school cannot take bank transfers here yet. Pay by card, or ask the "
    "school office how to pay."
)


def _transfer_rules(student):
    if not read_student_payment_rules(student.tenant).enabled:
        raise StudentPaymentOff()


def _account_body(account) -> dict:
    from django.utils import timezone

    if account is None:
        return {"state": "NONE", "as_of": timezone.now()}
    return {
        "state": "ISSUED",
        "bank_name": account.bank_name,
        "account_number": account.account_number,
        "account_name": account.account_name,
        "as_of": timezone.now(),
    }


def _account_result(result) -> dict:
    from schools.core.fal.contracts import Unavailable

    if result.is_available:
        return _account_body(result.value)
    if result.reason == Unavailable.NOT_CONFIGURED:
        return {"state": "UNAVAILABLE", "message": TRANSFER_UNAVAILABLE_SENTENCE}
    raise PaymentProviderUnavailable()


def transfer_account(student) -> dict:
    """The pupil's permanent account for bank transfers, or NONE.

    ``as_of`` is the server's time, which the page sends back to watch for a
    deposit, so a phone whose clock is wrong still sees it arrive.
    """
    _transfer_rules(student)
    return _account_result(
        registry.get_student_payment().transfer_account(_ref(student)),
    )


def open_transfer_account(student, *, actor) -> dict:
    """Issue the pupil's account, or return the one they already have."""
    _transfer_rules(student)
    return _account_result(
        registry.get_student_payment().open_transfer_account(_ref(student), actor=actor),
    )


def transfer_deposits(student, since) -> dict:
    """Transfers into the pupil's account since *since* (an ISO time), oldest first.

    No minimum applies: a transfer cannot be refused once it lands. A deposit
    appears as ``PENDING`` as soon as the provider reports it and turns
    ``CONFIRMED`` once its receipt is booked.
    """
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    _transfer_rules(student)
    moment = parse_datetime(str(since or "").strip().replace(" ", "+"))
    if moment is None:
        raise DepositSinceInvalid()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.utc)
    result = registry.get_student_payment().transfer_deposits(_ref(student), since=moment)
    if not result.is_available:
        raise FinanceUnavailable()
    return {
        "as_of": timezone.now(),
        "deposits": [
            {
                "reference": d.reference,
                "amount": d.amount,
                "status": d.status,
                "status_label": "Received" if d.status == "CONFIRMED" else "Arriving",
                "received_at": d.received_at,
                "payment_ref": d.payment_ref,
                "has_receipt": d.status == "CONFIRMED" and d.payment_ref is not None,
            }
            for d in result.value
        ],
    }
