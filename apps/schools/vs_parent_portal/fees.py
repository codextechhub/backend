"""A parent's view of their children's fees: what is owed, what was paid, and paying.

Everything is read and started through the finance abstraction layer, never the
ledger: ``FinanceReadPort`` for the invoices, ``StudentPaymentPort`` for the
history, the receipts, the invoice PDF and each child's transfer account, and
``ParentPaymentBridgePort`` to start a checkout. The portal holds no money and
books none; a payment it starts is settled by ``vs_payments`` when the provider
confirms it.

**A child whose books cannot be read is unavailable, never zero.** The fees
screen lists the children it could read beside the ones it could not, the family
total is withheld while any child is unreadable, and the pay-all steps say they
are incomplete.

**A parent always pays for their own children online.** No school switch removes
the Pay action from a parent, because paying is what a parent is for; the one
rule a school sets is its smallest part payment
(:func:`schools.core.fal.student_payments.read_student_payment_rules`), and the
whole balance of an invoice may always be paid. The payments engine charges one
child's account per checkout, so "pay all" is a list of steps, one per open
invoice, which the app runs one checkout at a time.

Every invoice, receipt and account is found by asking the parent's own children,
so another family's child is the same 404 as one that does not exist. Money
crosses this boundary as integer kobo; the portal formats it.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from schools.core.fal import registry
from schools.core.fal.exceptions import CrossTenantError, PaymentGatewayError
from schools.core.fal.student_payments import read_student_payment_rules
from schools.vs_student_portal import fees as pupil_fees
from schools.vs_student_portal.exceptions import (
    FinanceUnavailable,
    InvoiceNotPayable,
    PaymentProviderUnavailable,
)

from .links import parent_app_url

#: Where the gateway returns a parent after a card payment, inside the parent
#: app; the gateway adds ``?reference=`` and the page polls the collection's
#: status by it.
PAID_PATH = "fees/paid"

COMBINED_UNAVAILABLE = (
    "The family total cannot be shown while one child's fees cannot be read."
)


def _ref(child) -> str:
    return str(child.pk)


def child_label(child, *, full_name: bool = False) -> dict:
    """The small description of a child that rides on every fees row."""
    label = {"id": child.pk, "first_name": child.first_name}
    if full_name:
        label["full_name"] = child.full_name
    return label


def unavailable_sentence(child) -> str:
    return f"{child.first_name}'s fees cannot be shown right now. Try again in a few minutes."


# ── Reading ───────────────────────────────────────────────────────────────


def read_child(child) -> dict:
    """One child's fees as the fees screen shows them, available or not.

    ``owing`` is what is still to pay on open bills and debit notes, never
    reduced by credit; ``credit`` is the unused credit held for the child's
    accounts; ``balance`` is ``owing`` less ``credit``. Debit notes are listed
    beside the invoices because they count in ``owing``.

    An unreadable child answers ``available: false`` with a sentence, and
    carries no figure at all.
    """
    head = {"child": child_label(child, full_name=True)}
    result = registry.get_finance_reader().fee_status(_ref(child))
    if not result.is_available:
        return {**head, "available": False, "message": unavailable_sentence(child)}
    status = result.value
    return {
        **head,
        "available": True,
        "owing": status.owing,
        "credit": status.credit,
        "balance": status.balance,
        "total_billed": status.total_billed,
        "total_paid": status.total_paid,
        "invoices": [pupil_fees.invoice_row(invoice) for invoice in status.invoices],
        "debit_notes": [pupil_fees.debit_note_row(note) for note in status.debit_notes],
    }


def open_invoices(entry: dict) -> list:
    """A readable child's invoices that still owe something, oldest due date first."""
    return sorted(
        (row for row in entry["invoices"] if row["balance"] > 0),
        key=lambda row: (
            row["due_date"] is None, row["due_date"] or row["invoice_date"], row["invoice_ref"],
        ),
    )


def open_charges(entry: dict) -> list:
    """A readable child's debit notes that still owe something."""
    return [row for row in entry["debit_notes"] if row["balance"] > 0]


def combined(entries: list) -> dict:
    """The family's billed, paid, outstanding and credit, or the reason there is no total.

    ``outstanding`` is the sum of the children's ``owing`` and ``credit`` the
    sum of their credit. They are never netted against each other: one child's
    credit does not pay another child's bill.
    """
    if not all(entry["available"] for entry in entries):
        return {"available": False, "message": COMBINED_UNAVAILABLE}
    return {
        "available": True,
        "billed": sum(entry["total_billed"] for entry in entries),
        "paid": sum(entry["total_paid"] for entry in entries),
        "outstanding": sum(entry["owing"] for entry in entries),
        "credit": sum(entry["credit"] for entry in entries),
    }


def pay_all(children, entries: list) -> dict:
    """One step per open invoice, by child as listed and then oldest due date first.

    ``total`` is the sum of the steps. ``charges_total`` is what the readable
    children owe on open debit notes, which no step covers because a card
    payment names one invoice; a transfer into the child's account settles
    them. ``complete`` is false while a child's books could not be read,
    because their invoices are missing from the list.
    """
    steps = []
    for child, entry in zip(children, entries):
        if not entry["available"]:
            continue
        for row in open_invoices(entry):
            steps.append({
                "child_id": child.pk,
                "first_name": child.first_name,
                "invoice_ref": row["invoice_ref"],
                "number": row["number"],
                "title": row["title"],
                "amount": row["balance"],
            })
    return {
        "total": sum(step["amount"] for step in steps),
        "steps": steps,
        "charges_total": sum(
            row["balance"]
            for entry in entries if entry["available"]
            for row in open_charges(entry)
        ),
        "complete": all(entry["available"] for entry in entries),
    }


def family_fees(guardian, children) -> dict:
    """Every child's fees, the family total, the school's smallest part payment and pay-all."""
    entries = [read_child(child) for child in children]
    return {
        "children": entries,
        "combined": combined(entries),
        "min_part_payment": read_student_payment_rules(guardian.tenant).min_part_payment,
        "pay_all": pay_all(children, entries),
    }


def payment_history(children) -> list:
    """Every child's payments merged, newest first, each naming its child.

    A child whose history cannot be read fails the whole answer with
    ``FINANCE_UNAVAILABLE``: a list that quietly left out one child's payments
    would tell the parent something false.
    """
    rows = []
    for child in children:
        label = child_label(child)
        rows.extend({**row, "child": label} for row in pupil_fees.payment_history(child))
    rows.sort(key=lambda row: (row["paid_on"], row["status"] == "PENDING"), reverse=True)
    return rows


def _ask_each(children, ask, missing: str):
    """``(child, answer)`` from the first child whose answer is not a 404; 404 when none has it.

    A child whose books cannot be read is remembered, so that "not found
    among the children I could read" is told as an outage and not as a 404.
    """
    unreadable = False
    for child in children:
        try:
            return child, ask(child)
        except NotFound:
            continue
        except FinanceUnavailable:
            unreadable = True
    if unreadable:
        raise FinanceUnavailable()
    raise NotFound(missing)


def receipt(children, payment_ref) -> dict:
    """A confirmed payment's receipt, found on whichever child's account holds it."""
    child, body = _ask_each(
        children, lambda c: pupil_fees.receipt(c, payment_ref), "No receipt for that payment.",
    )
    return {**body, "child": child_label(child)}


def receipt_pdf(children, payment_ref) -> bytes:
    """The receipt as a PDF, found on whichever child's account holds it."""
    return _ask_each(
        children, lambda c: pupil_fees.receipt_pdf(c, payment_ref),
        "No receipt for that payment.",
    )[1]


def find_invoice(children, invoice_ref):
    """``(child, InvoiceView)`` for an invoice on one of the parent's children.

    Another family's invoice and one that does not exist are the same 404.
    """
    unreadable = False
    for child in children:
        result = registry.get_finance_reader().invoices_for(_ref(child))
        if not result.is_available:
            unreadable = True
            continue
        for invoice in result.value:
            if str(invoice.invoice_ref) == str(invoice_ref):
                return child, invoice
    if unreadable:
        raise FinanceUnavailable()
    raise NotFound("No such invoice on your children's accounts.")


def invoice_detail(children, invoice_ref) -> dict:
    """One invoice for the screen: its number, dates, lines, balance and child.

    ``document`` is the invoice as its printed copy reads (letterhead, billed-to
    account, lines with their tax, totals and the bank to pay into), so the
    sheet on screen and the PDF the parent downloads are the same document.
    """
    child, invoice = find_invoice(children, invoice_ref)
    result = registry.get_student_payment().invoice_document(_ref(child), invoice.invoice_ref)
    if not result.is_available:
        raise FinanceUnavailable()
    return {
        **pupil_fees.invoice_row(invoice),
        "child": child_label(child),
        "document": _invoice_document_row(result.value),
    }


def _invoice_document_row(document) -> dict | None:
    """An ``InvoiceDocument`` as the parent app reads it; money in kobo."""
    if document is None:
        return None
    bank = document.bank
    return {
        "document_number": document.document_number,
        "invoice_date": document.invoice_date,
        "due_date": document.due_date,
        "reference": document.reference,
        "narration": document.narration,
        "status": document.status.value,
        "letterhead": pupil_fees.letterhead_row(document.letterhead),
        "billed_to": {
            "name": document.billed_to.name,
            "code": document.billed_to.code,
            "email": document.billed_to.email,
            "phone": document.billed_to.phone,
            "address": document.billed_to.address,
        },
        "lines": [
            {
                "description": line.description,
                "detail": line.detail,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "tax_amount": line.tax_amount,
                "tax_label": line.tax_label,
                "amount": line.amount,
            }
            for line in document.lines
        ],
        "subtotal": document.subtotal,
        "tax_total": document.tax_total,
        "total": document.total,
        "amount_paid": document.amount_paid,
        "adjustments": document.adjustments,
        "balance_due": document.balance_due,
        "bank": ({"bank_name": bank.bank_name, "account_name": bank.account_name,
                  "account_number": bank.account_number} if bank is not None else None),
    }


def invoice_pdf(children, invoice_ref) -> bytes:
    """One invoice as the PDF the school sends; 404 when it is not a child's."""
    child, invoice = find_invoice(children, invoice_ref)
    result = registry.get_student_payment().invoice_pdf(_ref(child), invoice.invoice_ref)
    if not result.is_available:
        raise FinanceUnavailable()
    if result.value is None:
        raise NotFound("No such invoice on your children's accounts.")
    return result.value


# ── Paying ────────────────────────────────────────────────────────────────


def start_payment(guardian, children, *, user, invoice_ref, amount) -> dict:
    """Check the payment against the school's rules and hand back a checkout.

    The invoice is found among the parent's children first, so one that is not
    theirs is a 404 before anything is asked of the gateway. The amount is
    judged against that invoice's balance and the school's smallest part
    payment (:func:`schools.vs_student_portal.fees.validated_amount`). The
    checkout is started by the payments bridge with the signed-in account as
    its actor, so the parent who pressed Pay is told when it is confirmed, and
    it returns them to the parent app's paid page.
    """
    child, invoice = find_invoice(children, invoice_ref)
    rules = read_student_payment_rules(guardian.tenant)
    amount = pupil_fees.validated_amount(
        amount, balance=invoice.balance, minimum=rules.min_part_payment,
    )
    try:
        result = registry.get_parent_payment_bridge().start_payment_session(
            guardian_ref=str(guardian.pk), invoice_ref=invoice.invoice_ref, amount=amount,
            payer_email=getattr(user, "email", "") or "",
            callback_url=parent_app_url(guardian.tenant, PAID_PATH), actor=user,
        )
    except CrossTenantError:
        raise NotFound("No such invoice on your children's accounts.") from None
    except PaymentGatewayError as exc:
        raise InvoiceNotPayable() from exc
    if not result.is_available:
        raise PaymentProviderUnavailable()
    checkout = result.value
    return {
        "checkout_url": checkout.checkout_url,
        "reference": checkout.reference,
        "amount": amount,
        "balance_after": invoice.balance - amount,
        "child": child_label(child),
    }


# ── Bank transfer ─────────────────────────────────────────────────────────


def transfer_account(child) -> dict:
    """The child's permanent account for bank transfers, or NONE.

    Offered whatever the school decides about pupils paying for themselves:
    that rule is for a pupil, and a parent may always pay.
    """
    return pupil_fees.account_answer(
        registry.get_student_payment().transfer_account(_ref(child)),
    )


def open_transfer_account(child, *, actor) -> dict:
    """Issue the child's account, or return the one they already have."""
    return pupil_fees.account_answer(
        registry.get_student_payment().open_transfer_account(_ref(child), actor=actor),
    )


def transfer_deposits(child, since) -> dict:
    """Transfers into the child's account since *since*, oldest first."""
    return pupil_fees.deposits_since(child, since)
