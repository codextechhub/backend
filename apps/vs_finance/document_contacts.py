"""Who else a customer's documents go to, as the product hosting the books says.

The finance engine knows a customer by its billing email and nothing more. A
product hosting the books may know the people a customer's documents belong
with: a school knows that a pupil's invoices, receipts and statements are for
the pupil's primary guardian, who pays them. Such a product registers a
resolver here, and every document email reaches those people as well as the
billing email (:func:`vs_finance.document_email.resolve_contacts`). The engine
never imports the product.

A contact names the Field Access key that guards its address (``field``), so
a reader whose role may read a customer's billing email but not a guardian's
email is shown the contact's ``label`` instead of the address, wherever a
send's addresses are shown.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentContact:
    """One more person a customer's documents go to.

    ``name`` greets them. ``field`` is the full Field Access key guarding
    ``email`` (``"school.guardians.email"``), and ``label`` says who they are
    to a reader who may not read the address ("Grace's primary guardian").
    """

    email: str
    name: str
    field: str
    label: str


_RESOLVERS: list = []


def register_contact_resolver(resolver) -> None:
    """Add *resolver*, called as ``resolver(customer) -> list[DocumentContact]``. Idempotent."""
    if resolver not in _RESOLVERS:
        _RESOLVERS.append(resolver)


def host_contacts(customer) -> list[DocumentContact]:
    """Every contact the registered resolvers name for *customer*.

    A resolver that fails is logged and passed over, so the billing email is
    still sent to whatever the product could not answer.
    """
    contacts = []
    for resolver in _RESOLVERS:
        try:
            contacts.extend(resolver(customer) or [])
        except Exception:
            logger.exception("A document contact resolver failed for customer %s.", customer.pk)
    return [contact for contact in contacts if (contact.email or "").strip()]
