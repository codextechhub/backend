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

import re

from schools.core.fal import registry
from vs_finance.money import format_naira
from schools.core.fal.exceptions import CrossTenantError, PaymentGatewayError
from schools.core.fal.student_payments import read_student_payment_rules
from rest_framework.exceptions import NotFound, ValidationError

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

BILLS_PAGE_SIZE = 8
PAYMENTS_PAGE_SIZE = 10


def _page_number(value) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise ValidationError({"page": "Choose a valid page."}) from None
    if page < 1:
        raise ValidationError({"page": "Choose a valid page."})
    return page


def _year(value) -> str:
    if value in (None, "", "all"):
        return "all"
    if not re.fullmatch(r"\d{4}", value):
        raise ValidationError({"year": "Choose a valid year."})
    return value


def _page(rows, number: int, size: int) -> dict:
    total = len(rows)
    return {
        "items": rows[(number - 1) * size:number * size],
        "pagination": {
            "page": number, "page_size": size, "total": total,
            "total_pages": max(1, (total + size - 1) // size),
        },
    }


def bill_page(student, *, page, year=None, status=None) -> dict:
    """A bounded bill response, with outstanding items before paid history."""
    number = _page_number(page)
    year = _year(year)
    if status not in (None, "", "all", "due", "paid"):
        raise ValidationError({"status": "Choose a valid bill status."})
    summary = fee_summary(student)
    invoices = summary["invoices"]
    rows = [{"kind": "invoice", **invoice} for invoice in invoices]
    rows.extend({"kind": "charge", **charge} for charge in summary["debit_notes"])

    def issued_on(row):
        if row["kind"] == "invoice":
            return row.get("invoice_date") or row.get("due_date")
        return row.get("date")

    def sort_date(row):
        if row["kind"] == "invoice" and row["balance"] > 0:
            return row.get("due_date") or row.get("invoice_date")
        return issued_on(row)

    years = sorted({str(date)[:4] for row in rows if (date := issued_on(row))}, reverse=True)
    next_due = min((invoice["due_date"] for invoice in invoices if invoice["balance"] > 0 and invoice["due_date"]), default=None)
    if year != "all":
        rows = [row for row in rows if str(issued_on(row) or "").startswith(year)]
    if status in ("due", "paid"):
        rows = [row for row in rows if (row["balance"] > 0) == (status == "due")]
    open_rows = [row for row in rows if row["balance"] > 0]
    paid_rows = [row for row in rows if row["balance"] <= 0]
    open_rows.sort(key=lambda row: (not bool(sort_date(row)), str(sort_date(row) or "")))
    paid_rows.sort(key=lambda row: (bool(sort_date(row)), str(sort_date(row) or "")), reverse=True)
    return {
        **{key: value for key, value in summary.items() if key not in ("invoices", "debit_notes")},
        "has_bills": bool(invoices or summary["debit_notes"]),
        "years": years,
        "next_due": next_due,
        **_page(open_rows + paid_rows, number, BILLS_PAGE_SIZE),
    }


def payment_page(student, *, page, year=None) -> dict:
    """The newest confirmed and pending payments for one requested page."""
    number = _page_number(page)
    year = _year(year)
    rows = payment_history(student)
    years = sorted({str(row["paid_on"])[:4] for row in rows if row["paid_on"]}, reverse=True)
    if year != "all":
        rows = [row for row in rows if str(row["paid_on"] or "").startswith(year)]
    return {"years": years, **_page(rows, number, PAYMENTS_PAGE_SIZE)}


def _ref(student) -> str:
    return str(student.pk)


def _status(invoice) -> str:
    return getattr(invoice.status, "value", invoice.status)


def invoice_row(invoice) -> dict:
    """One invoice as the portals show it: number, title, dates, lines, balance, status."""
    status = _status(invoice)
    return {
        "invoice_ref": invoice.invoice_ref,
        "number": invoice.document_number,
        "title": invoice.term_label,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "lines": [{"description": l.description, "amount": l.amount} for l in invoice.lines],
        "total": invoice.amount_due,
        "paid": invoice.amount_paid,
        "balance": invoice.balance,
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
    }


def fee_summary(student) -> dict:
    """The pupil's balance, invoices and debit notes, and whether they may pay.

    A debit note is a charge added after a bill. It counts in the balance and
    the totals like a bill, and it is listed beside the bills so the balance can
    be accounted for. It cannot be paid by card from here (a card payment names
    one invoice); a transfer into the pupil's account number settles it.
    """
    result = registry.get_finance_reader().fee_status(_ref(student))
    if not result.is_available:
        raise FinanceUnavailable()
    status = result.value
    rules = read_student_payment_rules(student.tenant)
    return {
        "balance": status.balance,
        "total_billed": status.total_billed,
        "total_paid": status.total_paid,
        "invoices": [invoice_row(invoice) for invoice in status.invoices],
        "debit_notes": [
            {
                "note_ref": note.note_ref,
                "number": note.document_number,
                "date": note.note_date,
                "reason": note.reason,
                "total": note.amount_due,
                "paid": note.amount_paid,
                "balance": note.balance,
                "status": _status(note),
                "status_label": STATUS_LABEL.get(_status(note), _status(note)),
            }
            for note in status.debit_notes
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


def validated_amount(amount, *, balance, minimum) -> int:
    """The kobo amount a payer may pay on an invoice owing *balance*, or a refusal.

    The whole balance is always payable; a part payment must reach *minimum*.
    An amount that is not a whole number of kobo (a decimal, a boolean, text
    that is not digits) is refused rather than rounded, so what is charged is
    exactly what was typed.
    """
    if balance <= 0:
        raise InvoiceNotPayable("This invoice is already paid.")
    if isinstance(amount, str) and amount.strip().isdigit():
        amount = int(amount)
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise PaymentAmountInvalid("Enter the amount to pay.", field="amount")
    if amount <= 0:
        raise PaymentAmountInvalid("Enter an amount of more than ₦0.00.", field="amount")
    if amount > balance:
        raise AmountAboveBalance(
            f"You can pay at most the balance on this invoice, {format_naira(balance)}.",
            field="amount", balance=balance,
        )
    if amount < balance and amount < minimum:
        raise AmountBelowMinimum(
            f"The smallest part payment your school accepts is "
            f"{format_naira(minimum)}. Enter {format_naira(minimum)} "
            f"or more, or pay the full balance.",
            field="amount", minimum=minimum,
        )
    return amount


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
    amount = validated_amount(amount, balance=invoice["balance"], minimum=rules.min_part_payment)
    balance = invoice["balance"]
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


def account_answer(result) -> dict:
    """A transfer account read or issue, as the portals answer it (ISSUED, NONE or UNAVAILABLE)."""
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
    return account_answer(
        registry.get_student_payment().transfer_account(_ref(student)),
    )


def open_transfer_account(student, *, actor) -> dict:
    """Issue the pupil's account, or return the one they already have."""
    _transfer_rules(student)
    return account_answer(
        registry.get_student_payment().open_transfer_account(_ref(student), actor=actor),
    )


def transfer_deposits(student, since) -> dict:
    """Transfers into the pupil's account since *since* (an ISO time), oldest first.

    No minimum applies: a transfer cannot be refused once it lands. A deposit
    appears as ``PENDING`` as soon as the provider reports it and turns
    ``CONFIRMED`` once its receipt is booked.
    """
    _transfer_rules(student)
    return deposits_since(student, since)


def deposits_since(student, since) -> dict:
    """Transfers into the pupil's account since *since*, with no school rule applied."""
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

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
