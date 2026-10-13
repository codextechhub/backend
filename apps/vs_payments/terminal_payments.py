"""Payments made on a branch terminal: booked on arrival, matched to a customer afterwards.

A parent who pays at a branch's terminal names no pupil. The provider reports the
terminal, the amount, the payer's email and, for a transfer, the sender's name and
bank, and nothing else. The money is real the moment the provider confirms it, so
it is booked then, whether or not anyone knows whom it is for.

**Booking.** :func:`book_terminal_collection` runs where an ordinary collection
books its receipt (:func:`vs_payments.services._confirm_collection_atomic`). It posts
one journal in the terminal's branch: Dr gateway clearing, Cr unidentified receipts
(the ``UNIDENTIFIED_RECEIPTS`` mapping, starter code 2195). A payment the platform
holds raises the branch's held balance exactly as a checkout does
(:func:`vs_payments.held.record_collection`), and leaves clearing by the same
settlement run.

**Matching.** A payment is applied to a customer by a receipt that debits
unidentified receipts and credits the customer's receivable, settling the
customer's oldest bills first (:func:`vs_finance.receivables.post_payment`), a
part payment when the amount differs from the bill. Dr unidentified receipts, Cr
receivable is the whole effect, so the branch's suspense falls by what the receipt
carries.

* **Automatic.** When exactly one active customer of the branch has the payer's
  email (compared ignoring case and surrounding spaces), the payment is applied to
  them at once. A customer filed under no branch is every branch's, so counts as
  the branch's. If two customers share the email (a parent with two children), or
  none has it, or the email is missing, the payment stays on the unmatched list
  with a note saying why.
* **By a person.** :func:`match` applies a payment to a customer of the same
  branch; a customer of another branch is refused, since a branch's receipts only
  settle that branch's bills.
* **Undoing.** :func:`unmatch` voids the receipt, which reverses its journals and
  allocations, and puts the payment back on the list. A receipt that cannot be
  voided (its credit was refunded, say) refuses with finance's reason and nothing
  changes. A matched receipt voided from the receipts screen also puts the payment
  back, because it is then once more in unidentified receipts.

**Suggestions.** :func:`suggestions` ranks up to five customers of the payment's
branch for a person to choose from. The order is deterministic: customers with the
payer's email first; then customers whose name resembles the sender's name (or, with
none, the email's local part) by token overlap or string similarity of at least
:data:`NAME_THRESHOLD`, best first; then customers whose open balance in the branch
equals the amount exactly. A customer meeting several tests ranks by the earliest
and carries every reason. Ties break by customer code.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict

from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_finance.money import format_naira

from . import audit
from .constants import (
    CollectionChannel,
    CollectionStatus,
    PaymentAuditAction,
    TerminalMatchMethod,
    TerminalPaymentStatus,
)
from .exceptions import PaymentStateError
from .models import CollectionIntent, TerminalPayment, VirtualTerminal

#: Smallest name similarity (0 to 1) that counts as a resemblance.
NAME_THRESHOLD = 0.6

#: Most customers suggested for one payment.
SUGGESTION_LIMIT = 5

#: Most customers whose names are compared with the sender's for one payment.
_NAME_POOL = 200

_TOKEN = re.compile(r"[a-z]{3,}")


# --------------------------------------------------------------------------- #
# Receiving                                                                   #
# --------------------------------------------------------------------------- #

def terminal_for_code(provider: str, code: str):
    """The terminal the provider calls ``code``, or None when it is not one of ours."""
    if not code:
        return None
    return (VirtualTerminal.objects.select_related("tenant", "branch")
            .filter(provider=provider.upper(), code=code).first())


def record_terminal_payment(*, terminal, reference, amount, provider_reference="",
                            payer_email="", sender_name="", sender_bank="", event_type=""):
    """Create the collection and terminal payment an event describes. ``(intent, created)``, or ``(None, False)``.

    ``reference`` is the provider's own transaction reference and becomes the
    collection's, so a re-delivered or replayed event finds the same row and a
    second is never made. ``amount`` is the event's claim, which the confirm step
    replaces with the amount the provider's API reports before anything is
    booked. The collection is routed as the terminal is now: when a split sends
    its money to a subaccount it records that subaccount, and so is not held by
    the platform. ``(None, False)`` means the terminal's tenant has no books.
    """
    from .terminals import tenant_entity

    entity = tenant_entity(terminal.tenant)
    if entity is None:
        return None, False
    metadata = {"source": "virtual_terminal", "terminal_id": terminal.pk,
                "terminal_code": terminal.code, "webhook_event_type": event_type}
    if terminal.split_subaccount:
        metadata["settlement_subaccount"] = terminal.split_subaccount
    with transaction.atomic():
        intent, created = CollectionIntent.objects.get_or_create(
            reference=reference,
            defaults=dict(
                entity=entity, branch=terminal.branch, provider=terminal.provider,
                channel=CollectionChannel.TERMINAL, provider_reference=provider_reference,
                amount=amount,
                currency=entity.base_currency, payer_email=payer_email[:254],
                payer_name=sender_name[:200], narration="Branch payment terminal.",
                metadata=metadata, status=CollectionStatus.PENDING,
            ),
        )
        if created:
            TerminalPayment.objects.create(
                tenant=terminal.tenant, entity=entity, branch=terminal.branch,
                terminal=terminal, collection=intent, payer_email=payer_email[:254],
                sender_name=sender_name[:200], sender_bank=sender_bank[:120],
            )
    if created:
        audit.record(
            action=PaymentAuditAction.TERMINAL_PAYMENT_RECEIVED, entity=entity,
            provider=terminal.provider, reference=reference,
            message=f"Received a {format_naira(int(amount))} payment on {terminal.branch.name}'s terminal.",
            metadata={"branch_id": terminal.branch_id, "terminal_id": terminal.pk},
        )
    return intent, created


# --------------------------------------------------------------------------- #
# Booking                                                                     #
# --------------------------------------------------------------------------- #

def _payer_facts(raw: dict) -> dict:
    """The payer's email and sender, as the provider's verification reports them."""
    customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}
    auth = raw.get("authorization") if isinstance(raw.get("authorization"), dict) else {}
    return {
        "payer_email": str(customer.get("email", "") or "").strip(),
        "sender_name": str(auth.get("sender_name") or auth.get("account_name") or "").strip(),
        "sender_bank": str(auth.get("sender_bank") or auth.get("bank") or "").strip(),
    }


def _verified_terminal_code(raw: dict) -> str:
    meta = raw.get("metadata")
    if isinstance(meta, dict):
        terminal = meta.get("virtual_terminal")
        if isinstance(terminal, dict):
            return str(terminal.get("code", "") or "").strip()
    return ""


def book_terminal_collection(intent, *, actor_user=None, paid_at=None, verify_raw=None):
    """Book a confirmed terminal payment to its branch, and apply it to a customer if one is certain.

    Runs inside the confirmation's transaction with the collection locked. Posts
    Dr gateway clearing, Cr unidentified receipts in the payment's branch, dated
    the day the payer paid when that day's period is open and the first open day
    after it otherwise (the rule every online receipt follows,
    :func:`vs_payments.services._booking_date`). The provider's own
    verification, when it names a terminal, must name this one; a payer's email
    and sender it reports replace what the event claimed.

    Then tries the automatic match. A match that cannot post (a closed period, a
    customer with no receivable account) is rolled back on its own and leaves the
    payment unmatched with the reason in ``match_note``: the money has arrived and
    stays booked whatever happens to the match.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, JournalSource
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.posting import post_journal, resolve_period

    from .services import _booking_date

    raw = verify_raw if isinstance(verify_raw, dict) else {}
    payment = (TerminalPayment.objects.select_for_update(of=("self",))
               .select_related("terminal", "branch").get(collection=intent))
    claimed = _verified_terminal_code(raw)
    if claimed and claimed != payment.terminal.code:
        raise PaymentStateError(
            "The provider says this payment was made on a different terminal than the one "
            "the event named; booking held for manual review.",
            terminal=payment.terminal.code, reported=claimed)

    entity = intent.entity
    facts = _payer_facts(raw)
    if facts["payer_email"]:
        payment.payer_email = intent.payer_email = facts["payer_email"][:254]
    if facts["sender_name"]:
        payment.sender_name = intent.payer_name = facts["sender_name"][:200]
    if facts["sender_bank"]:
        payment.sender_bank = facts["sender_bank"][:120]

    clearing = resolve_mapped_account(entity, AccountMappingKey.GATEWAY_CLEARING,
                                      label="gateway clearing")
    suspense = resolve_mapped_account(entity, AccountMappingKey.UNIDENTIFIED_RECEIPTS,
                                      label="unidentified receipts")
    booked_on, dating = _booking_date(entity, paid_at, branch=payment.branch_id)
    entry = JournalEntry.objects.create(
        entity=entity, branch_id=payment.branch_id, date=booked_on,
        period=resolve_period(entity, booked_on), source=JournalSource.BANK,
        currency=intent.currency, reference=intent.reference, created_by=actor_user,
        narration=f"Branch terminal payment {intent.reference}"[:255],
    )
    JournalLine.objects.create(
        entry=entry, account=clearing, debit=intent.amount, credit=0, line_no=1,
        description="Gateway clearing")
    JournalLine.objects.create(
        entry=entry, account=suspense, debit=0, credit=intent.amount, line_no=2,
        description="Unidentified receipts")
    post_journal(entry, actor_user=actor_user)

    payment.suspense_journal = entry
    payment.booked_on = booked_on
    payment.paid_at = paid_at or timezone.now()
    payment.save()
    intent.clearing_account = clearing
    intent.branch_id = payment.branch_id
    if dating:
        intent.metadata = {**(intent.metadata or {}), **dating}
    _auto_match(payment, intent, suspense=suspense, actor_user=actor_user)


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #

def _eligible_customers(entity, branch_id):
    """Active customers of ``entity`` that belong to ``branch_id``, or to every branch."""
    from vs_finance.models import Customer

    return Customer.objects.filter(entity=entity, is_active=True).filter(
        Q(branch_id=branch_id) | Q(branch__isnull=True))


def _auto_match(payment, intent, *, suspense, actor_user=None) -> None:
    """Apply the payment to the one customer who has its payer's email, or note why not."""
    email = (payment.payer_email or "").strip().lower()
    if not email:
        payment.match_note = "The provider did not report the payer's email."
        payment.save(update_fields=["match_note", "updated_at"])
        return
    found = list(
        _eligible_customers(payment.entity, payment.branch_id)
        .annotate(_email=Lower(Trim("billing_email"))).filter(_email=email)
        .order_by("pk")[:2]
    )
    if len(found) != 1:
        payment.match_note = (
            "No customer of this branch has the payer's email." if not found else
            "More than one customer has the payer's email, so a person chooses between them.")
        payment.save(update_fields=["match_note", "updated_at"])
        return
    try:
        with transaction.atomic():
            _apply_to_customer(payment, intent, found[0], method=TerminalMatchMethod.AUTO,
                               suspense=suspense, received_on=payment.booked_on,
                               actor_user=actor_user)
    except Exception as exc:  # noqa: BLE001 - the money is booked; only the match failed.
        payment.refresh_from_db()
        payment.match_note = f"The automatic match could not post: {str(getattr(exc, 'message', exc))}"[:255]
        payment.save(update_fields=["match_note", "updated_at"])


def _apply_to_customer(payment, intent, customer, *, method, suspense, received_on,
                       actor_user=None) -> None:
    """Post the receipt that applies ``payment`` to ``customer`` and record the match.

    Dr unidentified receipts, Cr the customer's receivable, settling their
    oldest bills of the payment's branch first as of ``received_on``; whatever
    no bill takes stays as the customer's credit, as for any receipt. Runs in
    the caller's transaction.
    """
    from vs_finance.constants import PaymentMethod
    from vs_finance.models import Payment
    from vs_finance.receivables import post_payment

    receipt = Payment.objects.create(
        entity=payment.entity, customer=customer, branch_id=payment.branch_id,
        payment_date=received_on, currency=intent.currency, method=PaymentMethod.ONLINE,
        amount=intent.amount, deposit_account=suspense, reference=intent.reference,
        narration=f"Branch terminal payment {intent.reference}"[:255],
    )
    post_payment(receipt, actor_user=actor_user, auto_allocate=True, strategy="oldest")
    receipt.refresh_from_db()

    payment.status = TerminalPaymentStatus.MATCHED
    payment.match_method = method
    payment.customer = customer
    payment.match_payment = receipt
    payment.match_note = ""
    payment.matched_by = actor_user if method == TerminalMatchMethod.MANUAL else None
    payment.matched_at = timezone.now()
    payment.save()
    intent.customer = customer
    intent.payment = receipt
    if intent.pk:
        CollectionIntent.objects.filter(pk=intent.pk).update(
            customer=customer, payment=receipt, updated_at=timezone.now())
    audit.record(
        action=PaymentAuditAction.TERMINAL_PAYMENT_MATCHED, entity=payment.entity,
        provider=intent.provider, reference=intent.reference, actor_user=actor_user,
        message=(f"Applied {format_naira(int(intent.amount))} to {customer.code} "
                 f"({'automatically' if method == TerminalMatchMethod.AUTO else 'by a person'})."),
        metadata={"branch_id": payment.branch_id, "method": method,
                  "customer_id": customer.pk, "payment_id": receipt.pk},
    )


def _suspense_account(entity):
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey

    return resolve_mapped_account(entity, AccountMappingKey.UNIDENTIFIED_RECEIPTS,
                                  label="unidentified receipts")


def is_open(payment) -> bool:
    """Whether the payment is waiting for a customer: unmatched, or matched by a receipt since voided."""
    from vs_finance.constants import DocumentStatus

    if payment.status == TerminalPaymentStatus.UNMATCHED:
        return True
    return payment.match_payment_id is not None and payment.match_payment.status != DocumentStatus.POSTED


def open_payments_q(prefix: str = "") -> Q:
    """The terminal payments waiting for a customer, as a ``Q``; the row form of :func:`is_open`."""
    from vs_finance.constants import DocumentStatus

    return (Q(**{f"{prefix}status": TerminalPaymentStatus.UNMATCHED})
            | (Q(**{f"{prefix}status": TerminalPaymentStatus.MATCHED})
               & ~Q(**{f"{prefix}match_payment__status": DocumentStatus.POSTED})))


def check_customer_for(payment, customer) -> None:
    """Refuse a customer the payment may not be applied to (400 on ``customer``).

    The customer must be of the payment's books, and of its branch or of no
    branch in particular. A customer of another branch is refused: the receipt
    belongs to the payment's branch and would settle none of theirs.
    """
    if customer.entity_id != payment.entity_id:
        raise ValidationError({"customer": "No such customer in these books."})
    if customer.branch_id not in (None, payment.branch_id):
        raise ValidationError({"customer": (
            f"That customer belongs to another branch than {payment.branch.name}, where this "
            f"payment was made. A payment can only be matched to a customer of its own branch.")})


@transaction.atomic
def match(payment, customer, *, actor_user=None):
    """Apply an unmatched terminal payment to ``customer`` of its branch. Returns the payment.

    Dr unidentified receipts, Cr the customer's receivable, dated today at the
    payment's branch, settling the customer's oldest bills first. Refused (409)
    when the payment is already matched or its money is not booked yet, and
    (400) for a customer the payment may not go to.
    """
    from vs_config.clock import branch_today

    locked = (TerminalPayment.objects.select_for_update(of=("self",))
              .select_related("collection", "branch", "match_payment").get(pk=payment.pk))
    if not is_open(locked):
        raise PaymentStateError("This payment is already matched. Unmatch it first to apply it elsewhere.",
                                payment=locked.pk)
    if locked.suspense_journal_id is None or locked.collection.status != CollectionStatus.SUCCEEDED:
        raise PaymentStateError("This payment is not booked yet, so it cannot be matched.",
                                payment=locked.pk)
    check_customer_for(locked, customer)
    _apply_to_customer(
        locked, locked.collection, customer, method=TerminalMatchMethod.MANUAL,
        suspense=_suspense_account(locked.entity),
        received_on=branch_today(locked.entity.tenant, locked.branch_id), actor_user=actor_user)
    return locked


@transaction.atomic
def unmatch(payment, *, actor_user=None):
    """Reverse a match and put the payment back on the list. Returns the payment.

    Voids the receipt, which reverses its journals and allocations, so the money
    is again in unidentified receipts. Refused (409) when the payment is not
    matched, and with finance's reason when the receipt cannot be voided.
    """
    from vs_finance.constants import DocumentStatus
    from vs_finance.voids import void_payment

    locked = (TerminalPayment.objects.select_for_update(of=("self",))
              .select_related("collection", "match_payment", "customer").get(pk=payment.pk))
    if locked.status != TerminalPaymentStatus.MATCHED:
        raise PaymentStateError("This payment is not matched.", payment=locked.pk)
    receipt = locked.match_payment
    customer = locked.customer
    if receipt is not None and receipt.status == DocumentStatus.POSTED:
        void_payment(receipt, actor_user=actor_user)
    locked.status = TerminalPaymentStatus.UNMATCHED
    locked.match_method = ""
    locked.customer = None
    locked.match_payment = None
    locked.match_note = ""
    locked.matched_by = None
    locked.matched_at = None
    locked.unmatched_by = actor_user
    locked.unmatched_at = timezone.now()
    locked.save()
    CollectionIntent.objects.filter(pk=locked.collection_id).update(
        customer=None, payment=None, updated_at=timezone.now())
    audit.record(
        action=PaymentAuditAction.TERMINAL_PAYMENT_UNMATCHED, entity=locked.entity,
        provider=locked.collection.provider, reference=locked.collection.reference,
        actor_user=actor_user,
        message=f"Put a {format_naira(int(locked.collection.amount))} terminal payment back on the unmatched list.",
        metadata={"branch_id": locked.branch_id,
                  "customer_id": getattr(customer, "pk", None),
                  "payment_id": getattr(receipt, "pk", None)},
    )
    return locked


# --------------------------------------------------------------------------- #
# Suggestions                                                                 #
# --------------------------------------------------------------------------- #

def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _name_score(wanted: set[str], name: str) -> float:
    """How closely ``name`` resembles the sender's tokens, 0 to 1: the better of token overlap and string similarity."""
    have = _tokens(name)
    if not wanted or not have:
        return 0.0
    overlap = len(wanted & have) / max(len(wanted), len(have))
    similarity = difflib.SequenceMatcher(
        None, " ".join(sorted(wanted)), " ".join(sorted(have))).ratio()
    return round(max(overlap, similarity), 4)


def _sender_tokens(payment) -> set[str]:
    """The words to look for in customer names: the sender's, else the email's local part."""
    tokens = _tokens(payment.sender_name)
    if tokens:
        return tokens
    return _tokens((payment.payer_email or "").split("@", 1)[0].replace(".", " ")
                   .replace("_", " ").replace("-", " ").replace("+", " "))


def suggestions(payment, *, limit: int = SUGGESTION_LIMIT) -> list[dict]:
    """Up to ``limit`` customers of the payment's branch a person might match it to, best first.

    Ranking is described in the module docstring. Each entry names the customer
    (``customer`` id, ``code``, ``name``), their ``open_balance`` in the
    payment's branch in kobo, ``email_match`` and the ``reasons`` it was
    suggested for (``email``, ``name``, ``amount``). Only active customers of the
    payment's branch, or of every branch, are considered, so a branch-bound reader
    is never shown another branch's customer.
    """
    from vs_finance.constants import DocumentStatus
    from vs_finance.models import Invoice
    from vs_rbac.scoping import transaction_branch_match_q

    entity, branch_id = payment.entity, payment.branch_id
    pool = _eligible_customers(entity, branch_id)
    reasons: dict[int, list[str]] = defaultdict(list)
    scores: dict[int, float] = {}

    email = (payment.payer_email or "").strip().lower()
    if email:
        for pk in (pool.annotate(_email=Lower(Trim("billing_email"))).filter(_email=email)
                   .values_list("pk", flat=True)):
            reasons[pk].append("email")

    wanted = _sender_tokens(payment)
    if wanted:
        name_q = Q()
        for token in sorted(wanted):
            name_q |= Q(name__icontains=token)
        for pk, name in pool.filter(name_q).order_by("code").values_list("pk", "name")[:_NAME_POOL]:
            score = _name_score(wanted, name)
            if score >= NAME_THRESHOLD:
                reasons[pk].append("name")
                scores[pk] = score

    open_invoices = (
        Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
        .filter(transaction_branch_match_q(entity.tenant_id, branch_id))
        .values("customer_id")
        .annotate(due=Sum(F("total") - F("amount_paid") - F("amount_credited"))))
    amount = int(payment.collection.amount)
    exact = [row["customer_id"] for row in open_invoices.filter(due=amount)[:50]]
    if exact:
        for pk in pool.filter(pk__in=exact).values_list("pk", flat=True):
            reasons[pk].append("amount")

    if not reasons:
        return []
    rows = {c.pk: c for c in pool.filter(pk__in=list(reasons))}

    def rank(pk):
        why = reasons[pk]
        tier = 0 if "email" in why else 1 if "name" in why else 2
        return (tier, -scores.get(pk, 0.0), rows[pk].code)

    chosen = sorted((pk for pk in reasons if pk in rows), key=rank)[:limit]
    balances = {row["customer_id"]: int(row["due"] or 0)
                for row in open_invoices.filter(customer_id__in=chosen)}
    return [
        {"customer": pk, "code": rows[pk].code, "name": rows[pk].name,
         "open_balance": balances.get(pk, 0), "email_match": "email" in reasons[pk],
         "reasons": reasons[pk]}
        for pk in chosen
    ]
