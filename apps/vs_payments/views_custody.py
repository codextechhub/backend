"""Custody settings, branch subaccounts and settlement matching (``/v1/payments/``).

* ``settings/custody/``: the tenant's custody mode, its pending change and the
  settlement interval, with every branch's collection account and whether it is
  set up with the provider. Read with ``payments.settings.view``; changed with
  ``payments.settings.update`` by a whole-tenant caller only, since the mode binds
  every branch (:class:`vs_rbac.scoping.WholeTenantWriteMixin`).
* ``subaccounts/``: create or refresh the provider subaccount behind one branch's
  collection account. Same key and the same whole-tenant rule: it decides where
  a branch's money is paid.
* ``settlements/``: book a bank statement line as the settlement of the online
  payments it carries (:func:`vs_payments.settlement.settle_collections`). Needs
  ``payments.settlement.create``; the line and every payment must be within the
  caller's branches, read as :class:`vs_payments.reach.PaymentsReach` reads them,
  so a Lekki bursar settles Lekki's lines only and another branch's line answers
  404 like one that does not exist.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import WholeTenantWriteMixin

from . import custody, settlement
from .reach import PaymentsReach


def _bank_row(bank):
    """One collection account as the settings screen lists it; no account number."""
    if bank is None:
        return None
    return {
        "id": bank.id, "name": bank.name, "bank_name": bank.bank_name,
        "subaccount_ready": bool(bank.gateway_subaccount_code),
        "subaccount_provider": bank.gateway_subaccount_provider or None,
    }


def _branch_rows(entity):
    """Every branch of the tenant with its collection account."""
    from vs_tenants.models import Branch

    if not entity.tenant_id:
        return []
    return [
        {"branch": branch.pk, "branch_name": branch.name,
         "collection_account": _bank_row(custody.branch_collection_account(entity, branch.pk))}
        for branch in Branch.all_objects.filter(tenant_id=entity.tenant_id).order_by("name")
    ]


class CustodySettingsView(WholeTenantWriteMixin, APIView):
    """GET / PATCH the tenant's payment custody setting.

    PATCH body (any of): ``mode`` (``DIRECT`` or ``HELD``), taking effect on the
    first day of next month; ``settlement_interval_days`` (1 to 7);
    ``clearing_stale_days`` (1 to 60).

    docstring-name: Payment custody settings
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    shared_subject = "how the school's online payments are held"

    @property
    def rbac_permission(self):
        return ("payments.settings.view" if self.request.method in SAFE_METHODS
                else "payments.settings.update")

    def _payload(self, entity, row):
        tenant = entity.tenant if entity.tenant_id else None
        return {"settings": custody.serialize_custody(row, tenant),
                "branches": _branch_rows(entity)}

    def get(self, request):
        entity = resolve_entity(request)
        row = custody.custody_row(entity.tenant if entity.tenant_id else None)
        return success_response("Payment custody settings retrieved.",
                                data=self._payload(entity, row))

    def patch(self, request):
        entity = resolve_entity(request)
        row = custody.update_custody_settings(
            entity=entity, data=request.data, actor_user=request.user)
        return success_response("Payment custody settings updated.",
                                data=self._payload(entity, row))


class CollectionSubaccountView(WholeTenantWriteMixin, APIView):
    """POST: create or refresh the provider subaccount of a branch's collection account.

    Body: ``bank_account`` (id, required), ``settlement_bank_code`` (the bank's code
    at the provider, required), optional ``business_name`` and ``provider``.

    docstring-name: Collection subaccount
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.settings.update"
    shared_subject = "where a branch's online payments are paid"

    def post(self, request):
        from vs_finance.models import BankAccount

        entity = resolve_entity(request)
        body = request.data or {}
        ref = body.get("bank_account")
        if ref in (None, "") or not str(ref).isdigit():
            raise ValidationError({"bank_account": "Name the bank account by its id."})
        bank = BankAccount.objects.filter(entity=entity, pk=int(ref)).select_related("branch").first()
        if bank is None:
            raise ValidationError({"bank_account": "No such bank account in this entity."})
        bank = custody.save_collection_subaccount(
            entity=entity, bank_account=bank,
            settlement_bank_code=body.get("settlement_bank_code"),
            business_name=body.get("business_name", ""), provider=body.get("provider"),
            actor_user=request.user,
        )
        return success_response("Collection subaccount saved.", data={
            **_bank_row(bank), "branch": bank.branch_id,
        })


class SettlementCreateView(APIView):
    """POST: book a bank statement line as the settlement of online payments.

    Body: ``statement_line`` (id), ``collections`` (ids of the payments it
    carries), optional ``posting_date`` (YYYY-MM-DD). Answers the settlement
    journal's id and figures.

    docstring-name: Settle online payments
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.settlement.create"

    def post(self, request):
        from vs_finance.models import BankStatementLine

        entity = resolve_entity(request)
        reach = PaymentsReach.for_request(request, entity)
        body = request.data or {}
        ref = body.get("statement_line")
        if ref in (None, "") or not str(ref).isdigit():
            raise ValidationError({"statement_line": "Name the statement line by its id."})
        line = reach.bank_lines(
            BankStatementLine.objects.filter(bank_account__entity=entity)
        ).filter(pk=int(ref)).first()
        if line is None:
            raise NotFound("No such statement line.")
        raw_ids = body.get("collections")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValidationError({"collections": "Name the collections this line settles."})
        try:
            ids = {int(value) for value in raw_ids}
        except (TypeError, ValueError) as exc:
            raise ValidationError({"collections": "Collections are named by id."}) from exc
        if reach.collections().filter(pk__in=ids).count() != len(ids):
            raise ValidationError({"collections": "A collection named is not one you can settle."})
        posting_date = None
        if body.get("posting_date"):
            try:
                posting_date = datetime.date.fromisoformat(str(body["posting_date"]))
            except ValueError as exc:
                raise ValidationError({"posting_date": "Expected an ISO date (YYYY-MM-DD)."}) from exc

        entry = settlement.settle_collections(
            line, sorted(ids), actor_user=request.user, posting_date=posting_date)
        amounts = {jl.line_no: (jl.debit, jl.credit) for jl in entry.lines.all()}
        net = amounts.get(1, (0, 0))[0]
        gross = sum(credit for _debit, credit in amounts.values())
        return success_response("Settlement booked.", data={
            "journal_id": entry.pk, "journal_number": entry.document_number,
            "date": entry.date.isoformat(), "statement_line": line.pk,
            "collections": sorted(ids), "gross": gross, "fee": gross - net, "net": net,
        }, status=201)
