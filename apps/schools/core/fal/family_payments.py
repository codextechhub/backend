"""How a school asks its families to pay, as the finance engine reads it.

The finance engine knows nothing of pupils or of a school's choice between card
and bank transfer; it asks a provider registered with
:mod:`vs_finance.payer_options`. This is the school's answer, registered in
:meth:`schools.core.fal.apps.FalConfig.ready`.

For a customer that is a pupil's finance account, at a school that has not
switched card payments on (``fees.family_card_payments_enabled``), the answer
is that card is not offered and the family pays into the pupil's own account
number: the invoice prints it, the invoice email names it, and the public pay
link refuses to start a checkout. While the pupil has no account number the
answer carries the sentence a family reads instead. At a school that has
switched card on, and for any customer that is not a pupil's, the answer is
``None`` and the engine behaves as it always has.

The number is the pupil's own, read here from the payments engine's active
virtual account for that finance account. Where it is shown is the engine's
concern (:data:`vs_rbac.field_enforcement.SYSTEM_SURFACES`).
"""
from __future__ import annotations

from vs_finance.payer_options import PayerPaymentOptions, PayerTransferAccount

#: The pay page's sentence when a family opens an old link at a school that takes no card.
CARD_OFF_MESSAGE = (
    "Card payment is not available for this invoice. Pay by bank transfer to "
    "the student's account number on the invoice, or contact the school office."
)

#: Printed and emailed in place of the account while the pupil has none.
NO_ACCOUNT_MESSAGE = (
    "Your payment account is being set up. Please contact the school office "
    "for the account number to pay into."
)


def payer_options(customer):
    """The school's payment options for ``customer``, or ``None`` when it has no opinion."""
    from vs_payments.constants import VirtualAccountStatus
    from vs_payments.models import VirtualAccount

    from .contracts import SOURCE_TYPE_STUDENT
    from .student_payments import family_card_payments_enabled
    from .transfer_accounts import _pupil_customer

    if customer.source_type != SOURCE_TYPE_STUDENT:
        return None
    pupil = _pupil_customer(customer.pk)
    if pupil is None or family_card_payments_enabled(pupil.entity.tenant):
        return None
    account = (
        VirtualAccount.objects.filter(customer=pupil, status=VirtualAccountStatus.ACTIVE)
        .order_by("-created_at", "-pk").first()
    )
    return PayerPaymentOptions(
        card_allowed=False,
        transfer_account=(
            PayerTransferAccount(
                bank_name=account.bank_name, account_number=account.account_number,
                account_name=account.account_name,
            ) if account is not None else None
        ),
        card_off_message=CARD_OFF_MESSAGE,
        no_account_message=NO_ACCOUNT_MESSAGE,
    )
