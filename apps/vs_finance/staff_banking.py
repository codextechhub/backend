"""A member of staff's bank account for payroll paid through the payment provider.

Staff are paid into :class:`~vs_finance.models.StaffBankAccount`, one per person.
An account is never paid because somebody typed it: when the bank or the account
number is saved the account is asked of the provider (an account-name enquiry),
and what comes back decides whether it is paid.

* **VERIFIED** when the name the bank holds for the account matches the staff
  member's. The rule, kept in :func:`names_match`, is on words rather than on
  spelling: both names are lowercased, stripped of accents and punctuation and
  split into words, and the account matches when the staff member's surname and
  at least one of their other names both appear among the account holder's
  words, in any order, because banks register "ADEYEMI TUNDE OLAWALE" as often
  as "Tunde Adeyemi". The surname is the user account's last name when there is
  one (each part of a hyphenated surname counts), else the last word of the
  roster name. The staff member's names are the user account's first and last
  name and the roster name; the account matches if any of them does.
* **NEEDS_CONFIRMATION** when a holder is named and does not match. Tunde
  Adeyemi's bursar types her own cousin's account number for him: the bank says
  "Bola Okafor", the surname is not there, and a second person must look at
  the account and confirm it before a naira goes to it. That person is neither
  the one who made the change nor the staff member (:func:`confirm_staff_bank_account`).
* **FAILED** when the provider says it cannot resolve the account; a transport
  failure or a provider outage leaves it ``UNVERIFIED`` with a note, to be tried
  again (:func:`reverify_staff_bank_account`).

Any change to the bank or the account number resets the account to
``UNVERIFIED`` (:meth:`~vs_finance.models.StaffBankAccount.save`), so a verified
account cannot be pointed somewhere else and still be paid. Every change and
every confirmation is audited by who and which person, never by the account or its
state.

The provider is reached through ``vs_payments`` (imported where used): this app
decides what a verified account is, and ``vs_payments`` owns how the provider is
asked.
"""
from __future__ import annotations

import re
import unicodedata

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .audit import record
from .constants import FinanceAuditAction, StaffBankState

ACCOUNT_NUMBER_RE = re.compile(r"^\d{10}$")
BANK_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")

#: Provider answers that say nothing about whether the account exists.
_INDETERMINATE_STATUSES = frozenset({408, 425, 429})


def name_words(value) -> list[str]:
    """``value`` as lowercase words without accents or punctuation."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).split()


def names_match(*, surname_words, other_words, holder) -> bool:
    """Whether the account ``holder`` is the staff member: their surname and one other name appear.

    ``surname_words`` and ``other_words`` are the staff member's surname and every
    other word of their name (:func:`name_words`). Both must have a word in the
    holder's name, in any order and ignoring case, accents and punctuation. A
    person with no other word in their name cannot be matched and is confirmed
    by a second person.
    """
    held = set(name_words(holder))
    if not held:
        return False
    surname = set(surname_words)
    others = set(other_words) - surname
    return bool(surname & held) and bool(others & held)


def staff_name_variants(employee, salary=None) -> list[tuple[list[str], list[str]]]:
    """Each way the staff member's name is known, as ``(surname words, other words)``.

    The user account's names, and the roster name when there is a salary row;
    a roster name with no account gives its last word as the surname.
    """
    variants = []
    first, last = (getattr(employee, "first_name", ""), getattr(employee, "last_name", ""))
    if last:
        variants.append((name_words(last), name_words(first)))
    roster = name_words(getattr(salary, "name", "")) if salary is not None else []
    if roster:
        if last:
            variants.append((name_words(last), [w for w in roster if w not in set(name_words(last))]))
        else:
            variants.append((roster[-1:], roster[:-1]))
    return variants


def real_person(user):
    """The real person behind ``user``: the proxy actor under an impersonation, else the user."""
    from vs_tenants.context import get_proxy_actor

    return get_proxy_actor(for_user=user) or user


def _employee_salary(entity, employee):
    from .models import EmployeeSalary

    return (EmployeeSalary.objects.filter(entity=entity, employee=employee)
            .order_by("-is_active", "-id").first())


def _audit_branch(entity, employee):
    salary = _employee_salary(entity, employee)
    return getattr(salary, "branch_id", None)


def _provider():
    from vs_payments.providers.registry import get_provider
    from vs_payments.services import resolve_provider_name

    return get_provider(resolve_provider_name(None))


def _final_rejection(exc) -> bool:
    """True only when the provider definitely could not resolve the account (a clean 4xx)."""
    code = str(getattr(exc, "provider_code", "") or "")
    return code.isdigit() and 400 <= int(code) < 500 and int(code) not in _INDETERMINATE_STATUSES


def _resolve(row, salary) -> None:
    """Ask the provider who holds ``row``'s account and set its state accordingly (unsaved)."""
    from vs_payments.exceptions import PaymentError

    row.resolved_account_name = ""
    row.confirmed_by = None
    row.confirmed_at = None
    try:
        resolution = _provider().resolve_account(
            account_number=row.account_number, bank_code=row.bank_code)
    except PaymentError as exc:
        reason = str(getattr(exc, "message", exc))[:255]
        if _final_rejection(exc):
            row.verification_state = StaffBankState.FAILED
            row.verification_note = f"The provider could not find this account: {reason}"[:255]
        else:
            row.verification_state = StaffBankState.UNVERIFIED
            row.verification_note = f"The provider could not be reached; try again: {reason}"[:255]
        row.verified_at = None
        return
    row.resolved_account_name = resolution.account_name[:200]
    variants = staff_name_variants(row.employee, salary)
    matched = any(
        names_match(surname_words=surname, other_words=others, holder=resolution.account_name)
        for surname, others in variants)
    row.verified_at = timezone.now()
    if matched:
        row.verification_state = StaffBankState.VERIFIED
        row.verification_note = ""
    else:
        row.verification_state = StaffBankState.NEEDS_CONFIRMATION
        row.verification_note = "The account holder's name does not match the staff member's."


def _validated(bank_code, account_number):
    bank_code = str(bank_code or "").strip()
    account_number = "".join(str(account_number or "").split())
    errors = {}
    if not BANK_CODE_RE.match(bank_code):
        errors["bank_code"] = "Choose the staff member's bank."
    if not ACCOUNT_NUMBER_RE.match(account_number):
        errors["account_number"] = "Enter the 10-digit account number."
    if errors:
        raise ValidationError(errors)
    return bank_code, account_number


def set_staff_bank_account(*, entity, employee, bank_code, account_number, bank_name="",
                           actor_user=None, must_be_new=False):
    """Save a staff member's bank account and have the provider name its holder.

    Creates the account or changes the existing one. A changed bank or account
    number always resets and re-checks the account; saving the same two again
    changes nothing but the bank's display name. ``employee`` must belong to
    ``entity``'s tenant (a crafted id for another tenant's user is a 400).
    The provider is asked first and nothing is held open across the call.
    ``must_be_new`` makes the save an add only: an account already on file is
    never replaced and raises
    :class:`~vs_finance.exceptions.StaffBankAccountExistsError`, judged under
    the row lock so two simultaneous adds cannot both succeed.
    """
    from .exceptions import StaffBankAccountExistsError
    from .models import StaffBankAccount

    if employee is None or employee.tenant_id != entity.tenant_id:
        raise ValidationError({"employee": "No such person at this customer."})
    bank_code, account_number = _validated(bank_code, account_number)
    bank_name = str(bank_name or "").strip()[:120]
    salary = _employee_salary(entity, employee)

    with transaction.atomic():
        row = (StaffBankAccount.objects.select_for_update().filter(employee=employee).first())
        if row is not None and row.entity_id != entity.pk:
            raise ValidationError({"employee": "No such person at this customer."})
        if row is not None and must_be_new:
            raise StaffBankAccountExistsError()
        if row is not None and (row.bank_code, row.account_number) == (bank_code, account_number):
            if bank_name and bank_name != row.bank_name:
                row.bank_name = bank_name
                row.save(update_fields=["bank_name", "updated_at"])
            return row
        row = row or StaffBankAccount(entity=entity, employee=employee)
        row.bank_code, row.account_number = bank_code, account_number
        row.bank_name = bank_name or row.bank_name
        row.changed_by = real_person(actor_user) if actor_user else None
        _resolve(row, salary)
        row._state_managed = True
        row.save()
        record(
            entity=entity, action=FinanceAuditAction.STAFF_BANK_CHANGED, actor_user=actor_user,
            target=row, target_type="StaffBankAccount", target_id=str(row.pk),
            branch=_audit_branch(entity, employee),
            message="Changed a staff bank account.",
            employee_id=employee.pk,
        )
    return row


def reverify_staff_bank_account(*, account, actor_user=None):
    """Ask the provider again about an account that is unverified or failed.

    For an account the provider could not be reached about, or one that failed
    and has since been corrected at the bank. A verified, confirmed or
    awaiting-confirmation account is left as it is: re-asking could only
    quietly undo a confirmation.
    """
    from .models import StaffBankAccount

    with transaction.atomic():
        row = (StaffBankAccount.objects.select_for_update().select_related("employee")
               .get(pk=account.pk))
        if row.verification_state not in (StaffBankState.UNVERIFIED, StaffBankState.FAILED):
            return row
        _resolve(row, _employee_salary(row.entity, row.employee))
        row._state_managed = True
        row.save()
        record(
            entity=row.entity, action=FinanceAuditAction.STAFF_BANK_CHANGED,
            actor_user=actor_user, target=row, target_type="StaffBankAccount",
            target_id=str(row.pk), branch=_audit_branch(row.entity, row.employee),
            message="Checked a staff bank account again.", employee_id=row.employee_id,
        )
    return row


def confirmer_refusal(account, actor_user):
    """Why ``actor_user`` may not confirm ``account``, or ``None`` when the person is eligible.

    The two rules of a second person, judged by user id with an impersonation
    session counted as the real person behind it: not whoever made the last
    change, and not the staff member. Whether the account is awaiting
    confirmation, and whether the caller holds the confirm key, are separate
    questions (:func:`confirm_staff_bank_account`, :func:`reader_may_confirm`).
    """
    person = real_person(actor_user)
    if person.pk == account.changed_by_id:
        return "A second person must confirm this account: you made the change."
    if person.pk == account.employee_id or actor_user.pk == account.employee_id:
        return "Staff cannot confirm their own bank account."
    return None


def reader_may_confirm(account, user, tenant) -> bool:
    """Whether ``user`` could confirm ``account`` now: the answer a screen shows as a button.

    True only for an account awaiting confirmation, a reader holding
    ``finance.staff_bank.confirm`` in ``tenant`` (as the confirm route's gate
    reads it), and a reader the second-person rules allow
    (:func:`confirmer_refusal`).
    """
    from vs_rbac.evaluator import has_permission
    from vs_rbac.permissions import is_vision_super_admin

    if account.verification_state != StaffBankState.NEEDS_CONFIRMATION:
        return False
    if not (is_vision_super_admin(user)
            or has_permission(user, "finance.staff_bank.confirm", tenant=tenant)):
        return False
    return confirmer_refusal(account, user) is None


def confirm_staff_bank_account(*, account, actor_user):
    """A second person confirms an account whose holder's name did not match.

    Only a ``NEEDS_CONFIRMATION`` account can be confirmed. The confirming
    person must be neither whoever made the last change (compared as real
    people, so an impersonated session cannot confirm its own change) nor the
    staff member themselves; either is a 403. Bola changes Tunde's account and
    the bank names somebody else: Bola cannot confirm it, and nor can Tunde;
    Mr Eze, the bursar, can, after checking with Tunde. Audited.
    """
    from .models import StaffBankAccount

    with transaction.atomic():
        row = (StaffBankAccount.objects.select_for_update().select_related("employee")
               .get(pk=account.pk))
        if row.verification_state != StaffBankState.NEEDS_CONFIRMATION:
            raise ValidationError({"detail": (
                "Only an account whose holder's name did not match needs confirming.")})
        person = real_person(actor_user)
        if (refusal := confirmer_refusal(row, actor_user)) is not None:
            raise PermissionDenied(refusal)
        row.verification_state = StaffBankState.CONFIRMED
        row.confirmed_by = person
        row.confirmed_at = timezone.now()
        row.verification_note = ""
        row._state_managed = True
        row.save()
        record(
            entity=row.entity, action=FinanceAuditAction.STAFF_BANK_CONFIRMED,
            actor_user=actor_user, target=row, target_type="StaffBankAccount",
            target_id=str(row.pk), branch=_audit_branch(row.entity, row.employee),
            message="Confirmed a staff bank account whose holder's name did not match.",
            employee_id=row.employee_id,
        )
    return row


def payable_account(employee_id):
    """The staff member's bank account when payroll may send to it, else ``None``."""
    from .models import StaffBankAccount

    if not employee_id:
        return None
    row = StaffBankAccount.objects.filter(employee_id=employee_id).first()
    return row if row is not None and row.is_payable else None


def list_banks():
    """The provider's banks as ``[{name, code}]``, for choosing a staff member's bank."""
    return _provider().list_banks()
