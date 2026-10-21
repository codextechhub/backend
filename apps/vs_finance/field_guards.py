"""Small checks the finance views share when they enforce Field Access by hand.

A view that builds its response itself, or applies a body without a serializer,
asks the same questions over and over: which submitted keys carry a value
worth judging against a Write switch, whether a list filter names a field the
caller may not read, and whether the caller may read a field a sentence would
name. They live here so each resource's views state the rule once instead of
each carrying its own copy.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import FieldReadDenied, can_read


def submitted(body, *names) -> dict:
    """The named keys of ``body`` that carry a value.

    A form posts every input it has, filled or not, and an empty one asks for
    nothing, so it is not a write worth judging against a Write switch.
    """
    if not isinstance(body, dict):
        return {}
    return {name: body[name] for name in names if body.get(name) not in (None, "")}


def refuse_hidden_filters(request, resource, filters) -> None:
    """Refuse a list filter that names a field the caller may not read.

    ``filters`` maps a query parameter to ``(registered field, label)``, and the
    registered field may be a tuple when the parameter matches on several. A
    filter on a hidden field answers the question the field's absence exists to
    keep unanswered: ``?bank_account=7`` on a list that shows no bank account
    says which rows were paid into it.
    """
    params = request.query_params
    hidden = {}
    for param, (names, label) in filters.items():
        if not params.get(param):
            continue
        names = (names,) if isinstance(names, str) else names
        if not all(can_read(request, f"{resource}.{name}") for name in names):
            hidden[param] = label
    if hidden:
        raise FieldReadDenied(hidden, labels=hidden)


def readable(request, resource, name) -> bool:
    """Whether the caller may read the registered field *name* of *resource*."""
    return can_read(request, f"{resource}.{name}")


def shown_recipients(request, recipients, details) -> list[str]:
    """A send's addresses as *request*'s caller may see them.

    *details* is ``{email: {field, label, ...}}`` for the recipients a product
    named (:func:`vs_finance.document_email.contact_details`). An address its
    Field Access key closes to the caller is shown as its label, so a role that
    may read a customer's billing email but not a guardian's email learns who
    was sent the document and not where.
    """
    details = details or {}
    shown = []
    for email in recipients or []:
        contact = details.get(email)
        if contact and contact.get("field") and not can_read(request, contact["field"]):
            shown.append(contact.get("label") or "another contact")
        else:
            shown.append(email)
    return shown


def customer_balance_readable(request) -> bool:
    """Whether the caller may read a customer's balance, wherever a payload restates it."""
    return can_read(request, "finance.customer.balance")


def customer_aging_readable(request) -> bool:
    """Whether the caller may read how late a customer's balance is.

    An age analysis restates both the balance (how much is owed) and the
    account standing (how late it is), so it is shown only when both are readable.
    """
    return (can_read(request, "finance.customer.balance")
            and can_read(request, "finance.customer.account_status"))
