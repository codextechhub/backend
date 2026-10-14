"""Staff bank accounts for payroll paid through the payment provider.

Routes under ``/v1/finance/staff-bank-accounts/``. A member of staff is reached
through the payroll roster row they sit on, read exclusively by branch like every
roster row (:func:`vs_finance.views_ops.payroll._salary_rows`): Lekki's bursar
opens Lekki's teachers' accounts and finds nothing for an Ikeja teacher or for
someone nobody has given a branch. The rules of an account (what verifies it, who
may confirm it) are in :mod:`vs_finance.staff_banking`.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..models import StaffBankAccount
from ..serializers import StaffBankAccountSerializer
from ..staff_banking import (
    confirm_staff_bank_account,
    list_banks,
    reverify_staff_bank_account,
    set_staff_bank_account,
)
from ..views import resolve_entity
from .base import _FinanceBase
from .payroll import _salary_rows

#: Keys a bank account write may carry.
WRITABLE_KEYS = ("bank_code", "bank_name", "account_number")

BANK_LIST_CACHE_KEY = "vs_finance:provider-banks"
BANK_LIST_CACHE_SECONDS = 6 * 60 * 60


def _accounts_in_reach(request, entity):
    """Bank accounts of staff on roster rows the caller reaches, in this entity's books."""
    staff = _salary_rows(request, entity).exclude(employee__isnull=True).values("employee_id")
    return (StaffBankAccount.objects.filter(entity=entity, employee_id__in=staff)
            .select_related("employee", "changed_by", "confirmed_by"))


def _account_data(request, account):
    return StaffBankAccountSerializer(account, context={"request": request}).data


class _StaffBankBase(_FinanceBase):
    def _employee(self, request, entity, employee_id):
        """The staff member on a roster row the caller reaches, or 404."""
        row = (_salary_rows(request, entity).filter(employee_id=employee_id)
               .select_related("employee").first())
        if row is None or row.employee is None:
            raise NotFound("Staff member not found on this payroll.")
        return row.employee

    def _account(self, request, entity, employee_id):
        account = _accounts_in_reach(request, entity).filter(employee_id=employee_id).first()
        if account is None:
            raise NotFound("No bank account is saved for this staff member.")
        return account


class StaffBankAccountListView(_StaffBankBase):
    """GET - staff bank accounts the caller reaches; ``?state=`` filters by verification state.

    docstring-name: Staff bank accounts
    """
    rbac_permission = "finance.staff_bank.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _accounts_in_reach(request, entity)
        if (state := request.query_params.get("state")):
            qs = qs.filter(verification_state=state.upper())
        return self.paginate(request, qs.order_by("employee__last_name", "employee_id"),
                             StaffBankAccountSerializer)


class StaffBankAccountBanksView(_StaffBankBase):
    """GET - the banks the payment provider can pay into, for choosing a staff member's bank.

    docstring-name: Banks for staff accounts
    """
    rbac_permission = "finance.staff_bank.view"

    def get(self, request):
        resolve_entity(request)
        banks = cache.get(BANK_LIST_CACHE_KEY)
        if banks is None:
            banks = list_banks()
            cache.set(BANK_LIST_CACHE_KEY, banks, BANK_LIST_CACHE_SECONDS)
        return success_response("Banks retrieved.", data=banks)


class StaffBankAccountDetailView(_StaffBankBase):
    """GET / PUT one staff member's bank account, by the staff member's user id.

    PUT takes ``bank_code``, ``account_number`` and optionally ``bank_name``.
    Saving a changed bank or account number has the payment provider name the
    account's holder and sets the verification state
    (:func:`vs_finance.staff_banking.set_staff_bank_account`). Each key is judged
    against the caller's write switch of ``finance.staff_bank`` before anything
    is written; a value sent back exactly as stored is not a write.

    docstring-name: Staff bank account
    """

    @property
    def rbac_permission(self):
        return "finance.staff_bank.update" if self.request.method == "PUT" \
            else "finance.staff_bank.view"

    def get(self, request, employee_id):
        entity = resolve_entity(request)
        self._employee(request, entity, employee_id)
        return success_response(
            "Staff bank account retrieved.",
            data=_account_data(request, self._account(request, entity, employee_id)))

    def put(self, request, employee_id):
        from vs_rbac.field_enforcement import assert_writable

        entity = resolve_entity(request)
        employee = self._employee(request, entity, employee_id)
        body = request.data or {}
        unknown = sorted(set(body) - set(WRITABLE_KEYS))
        if unknown:
            raise ValidationError({"detail": f"Unknown fields: {', '.join(unknown)}."})
        existing = StaffBankAccount.objects.filter(entity=entity, employee=employee).first()
        submitted = {key: str(body[key] or "").strip() for key in WRITABLE_KEYS if key in body}
        current = {
            "bank_code": getattr(existing, "bank_code", ""),
            "bank_name": getattr(existing, "bank_name", ""),
            "account_number": getattr(existing, "account_number", ""),
        }
        assert_writable(request, "finance.staff_bank", submitted,
                        creating=existing is None, current=current)
        account = set_staff_bank_account(
            entity=entity, employee=employee,
            bank_code=submitted.get("bank_code", current["bank_code"]),
            account_number=submitted.get("account_number", current["account_number"]),
            bank_name=submitted.get("bank_name", current["bank_name"]),
            actor_user=request.user)
        account = self._account(request, entity, employee_id)
        return success_response(
            "Staff bank account saved.", data=_account_data(request, account),
            status=200 if existing else 201)


class StaffBankAccountVerifyView(_StaffBankBase):
    """POST - ask the provider again about an account it could not be asked about or could not find.

    docstring-name: Verify a staff bank account
    """
    rbac_permission = "finance.staff_bank.update"

    def post(self, request, employee_id):
        entity = resolve_entity(request)
        self._employee(request, entity, employee_id)
        account = self._account(request, entity, employee_id)
        reverify_staff_bank_account(account=account, actor_user=request.user)
        return success_response(
            "Staff bank account checked.",
            data=_account_data(request, self._account(request, entity, employee_id)))


class StaffBankAccountConfirmView(_StaffBankBase):
    """POST - a second person confirms an account whose holder's name did not match the staff member's.

    Refused (403) for the person who made the last change and for the staff
    member themselves.

    docstring-name: Confirm a staff bank account
    """
    rbac_permission = "finance.staff_bank.confirm"

    def post(self, request, employee_id):
        entity = resolve_entity(request)
        self._employee(request, entity, employee_id)
        account = self._account(request, entity, employee_id)
        confirm_staff_bank_account(account=account, actor_user=request.user)
        return success_response(
            "Staff bank account confirmed.",
            data=_account_data(request, self._account(request, entity, employee_id)))
