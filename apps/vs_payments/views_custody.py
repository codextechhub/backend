"""Custody settings, branch subaccounts and settlement matching (``/v1/payments/``).

* ``settings/custody/``: the tenant's custody mode, its pending change and the
  settlement interval, with each branch's collection account, whether it is set
  up with the provider and what the platform holds for it, for the branches the
  caller reaches. Read with ``payments.settings.view``; changed with
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
* ``held-settlements/``: the platform's settlements of the money it held for the
  tenant's branches, read-only, narrowed to the caller's branches. The tenant
  never submits or approves one.
* ``platform/held-settlements/`` and ``platform/held-settlements/<id>/submit/``:
  the platform operators' list of settlements to act on across every tenant, and
  putting one forward for the platform's two-person approval. Platform staff only
  (``IsVisionStaff``), with ``payments.platform_settlement.view`` / ``.submit``.
* ``platform/held-reconciliations/``: the daily checks of the platform's books
  against its provider balance, for the same platform staff.
* ``platform/provider-settings/``: whether the provider sweeps the platform's
  balance to its bank, read with ``payments.platform_provider.view`` and changed
  with ``.update``, platform staff only.
* ``platform/provider-sweeps/``: the provider's settlements of the platform
  balance that the daily check counted, for the platform settlement readers.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive, IsVisionStaff
from vs_rbac.scoping import WholeTenantWriteMixin

from . import custody, held, settlement
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


def _branch_rows(reach):
    """Each branch in reach with its collection account and what the platform holds for it.

    A branch-bound reader gets their own branches' rows only: another branch's
    collection account and held balance are that branch's money, so they are
    absent, not blanked. A whole-school reader gets every branch.
    """
    if not reach.entity.tenant_id:
        return []
    balances = dict(reach.held_balances().values_list("branch_id", "balance"))
    return [
        {"branch": branch.pk, "branch_name": branch.name,
         "collection_account": _bank_row(
             custody.branch_collection_account(reach.entity, branch.pk)),
         "held_balance": int(balances.get(branch.pk, 0))}
        for branch in reach.branches().order_by("name")
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
        reach = PaymentsReach.for_request(self.request, entity)
        settings = custody.serialize_custody(row, tenant)
        if reach.is_narrowed and entity.tenant_id:
            # A branch reader's note names only their own branches' money.
            settings["pending_note"] = held.pending_note_in_reach(
                row, entity, reach.branches().values_list("pk", flat=True))
        return {"settings": settings, "branches": _branch_rows(reach)}

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


#: Most rows the held-settlements list answers in one page.
_HELD_SETTLEMENT_LIMIT = 200


class HeldSettlementListView(APIView):
    """GET: the platform's settlement runs paying this tenant's branches what it held for them.

    Read with ``payments.report.view``, narrowed to the caller's branches as every
    payments read is (:meth:`vs_payments.reach.PaymentsReach.held_settlements`): a
    Lekki bursar sees Lekki's settlements only. ``?status=`` filters by
    ``PENDING``, ``PAID`` or ``FAILED``; ``?limit=`` caps the page (at most 200).
    Newest first.

    docstring-name: Held settlements
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.report.view"

    def get(self, request):
        from .constants import HeldSettlementStatus

        entity = resolve_entity(request)
        rows = (PaymentsReach.for_request(request, entity).held_settlements()
                .select_related("branch", "bank_account", "batch")
                .order_by("-id"))
        status = str(request.query_params.get("status") or "").strip().upper()
        if status:
            if status not in HeldSettlementStatus.values:
                raise ValidationError({"status": f"Use one of: {', '.join(HeldSettlementStatus.values)}."})
            rows = rows.filter(status=status)
        rows = list(rows[:_limit(request)])
        return success_response("Held settlements retrieved.",
                                data=[_settlement_row(row) for row in rows])


def _limit(request):
    """The page size ``?limit=`` asks for: 50 by default, at least 1, at most 200."""
    try:
        return min(max(int(request.query_params.get("limit") or 50), 1), _HELD_SETTLEMENT_LIMIT)
    except ValueError as exc:
        raise ValidationError({"limit": "Expected a whole number."}) from exc


def _settlement_row(row, *, platform=False):
    """One held settlement as its readers see it; ``platform`` adds the tenant it pays."""
    data = {
        "id": row.pk, "branch": row.branch_id, "branch_name": row.branch.name,
        "status": row.status, "run_on": row.run_on.isoformat(), "final": row.final,
        "gross": int(row.gross), "fees": int(row.fees), "transfer_fee": int(row.transfer_fee),
        "amount": int(row.amount),
        "bank_account": {"id": row.bank_account_id, "name": row.bank_account.name},
        "batch": ({"id": row.batch_id, "reference": row.batch.reference,
                   "status": row.batch.status,
                   "approval_status": (row.batch.metadata or {}).get("approval_status")}
                  if row.batch_id else None),
        "journal_id": row.settlement_journal_id,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "failure_reason": row.failure_reason or None,
    }
    if platform:
        data.update({"tenant": row.tenant.slug, "tenant_name": row.tenant.name,
                     "entity": row.entity.code})
    return data


class PlatformHeldSettlementListView(APIView):
    """GET: every settlement a platform operator has to act on, across tenants.

    By default the pending ones (waiting to be put forward, approved or
    transferred) and any built today; ``?status=`` lists one status instead and
    ``?client=<slug>`` one client tenant (``?tenant=`` is the caller's own, which
    routes the request). Platform staff with
    ``payments.platform_settlement.view`` only.

    docstring-name: Platform held settlements
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_settlement.view"

    def get(self, request):
        from .constants import HeldSettlementStatus
        from .held import settlements_due
        from .models import HeldSettlement

        status = str(request.query_params.get("status") or "").strip().upper()
        if status:
            if status not in HeldSettlementStatus.values:
                raise ValidationError({"status": f"Use one of: {', '.join(HeldSettlementStatus.values)}."})
            rows = (HeldSettlement.objects.filter(status=status)
                    .select_related("tenant", "branch", "entity", "bank_account", "batch")
                    .order_by("-id"))
        else:
            rows = settlements_due()
        if (client := str(request.query_params.get("client") or "").strip()):
            rows = rows.filter(tenant__slug=client)
        return success_response("Held settlements retrieved.", data=[
            _settlement_row(row, platform=True) for row in rows[:_limit(request)]])


class PlatformHeldSettlementSubmitView(APIView):
    """POST: put one settlement forward for the platform's two-person approval.

    The caller becomes its requester and cannot approve it; two other platform
    people in the settlement approver group do. Platform staff with
    ``payments.platform_settlement.submit`` only. Answers the settlement and its
    approval.

    docstring-name: Submit a held settlement
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_settlement.submit"

    def post(self, request, pk):
        from vs_workflow.services import release as release_svc

        from .held import submit_settlement
        from .models import HeldSettlement

        row = (HeldSettlement.objects.select_related(
            "tenant", "branch", "entity", "bank_account", "batch__entity__tenant")
            .filter(pk=pk).first())
        if row is None:
            raise NotFound("No such settlement.")
        instance = submit_settlement(row, requested_by=request.user)
        row.batch.refresh_from_db()
        return success_response("Settlement submitted for approval.", data={
            **_settlement_row(row, platform=True),
            "approval": release_svc.approval_block(instance),
        })


class PlatformHeldReconciliationListView(APIView):
    """GET: the daily checks of the platform's books against its provider balance, newest first.

    Each row is one day's check (:mod:`vs_payments.held_reconciliation`): what
    the provider reported, what the books say and why, the difference against
    the tolerance, whether it agrees, and the incident a disagreement opened.
    ``?agrees=false`` lists only the disagreements; ``?limit=`` caps the page (at
    most 200). Platform staff with ``payments.platform_settlement.view`` only.

    docstring-name: Held-ledger reconciliations
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_settlement.view"

    def get(self, request):
        from .models import HeldReconciliation

        rows = HeldReconciliation.objects.order_by("-checked_on", "-id")
        agrees = str(request.query_params.get("agrees") or "").strip().lower()
        if agrees in ("true", "false"):
            rows = rows.filter(agrees=agrees == "true")
        return success_response("Held-ledger reconciliations retrieved.", data=[{
            "id": row.pk, "checked_on": row.checked_on.isoformat(), "provider": row.provider,
            "currency": row.currency, "provider_balance": row.provider_balance,
            "books_balance": row.books_balance, "provider_account": row.provider_account,
            "held_total": row.held_total, "own_in_transit": row.own_in_transit,
            "balance_swept": row.balance_swept, "swept_total": row.swept_total,
            "own_swept_settled": row.own_swept_settled,
            "difference": row.difference, "tolerance": row.tolerance, "agrees": row.agrees,
            "error": row.error or None, "incident_code": row.incident_code or None,
        } for row in rows[:_limit(request)]])


def _provider_settings_payload():
    """The platform's provider settings as the console shows them, with what was counted."""
    from django.db.models import Count, Max, Sum

    from vs_config.models import ConfigurationValue

    from . import held_reconciliation
    from .models import ProviderSweep

    stored = (ConfigurationValue.all_objects.filter(
        definition__key=held_reconciliation.SWEPT_KEY, scope_key="platform").first())
    sweeps = ProviderSweep.objects.aggregate(
        count=Count("id"), total=Sum("amount"), latest=Max("settled_at"))
    return {
        "balance_swept": held_reconciliation.balance_swept(),
        "source": "platform" if stored is not None else "default",
        "updated_at": stored.updated_at.isoformat() if stored is not None else None,
        "tolerance_kobo": held_reconciliation.tolerance(),
        "sweeps": {
            "count": sweeps["count"] or 0, "total": int(sweeps["total"] or 0),
            "latest_settled_at": sweeps["latest"].isoformat() if sweeps["latest"] else None,
        },
    }


class PlatformProviderSettingsView(APIView):
    """GET, PATCH: how the platform's own merchant account at the payment provider behaves.

    ``balance_swept`` is the platform setting ``payments.provider_balance_swept``
    (default off): whether the provider settles the platform's balance to its
    bank automatically, which the daily held-ledger check then allows for
    (:mod:`vs_payments.held_reconciliation`). It is a setting of the platform's
    own account, never a tenant's. GET also answers the check's tolerance and
    the settlements counted so far.

    PATCH takes ``balance_swept`` (true or false) and a ``reason``, both
    required, and stores the value at platform scope through the configuration
    catalogue, which audits the change with its actor, before and after values
    and reason (``config.value.updated``). The generic configuration value
    endpoints refuse this key, so this view and its permission are the only
    way to change it. Platform staff only (``IsVisionStaff``), with
    ``payments.platform_provider.view`` to read and ``.update`` to change.

    docstring-name: Platform payment provider settings
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]

    @property
    def rbac_permission(self):
        if self.request.method in SAFE_METHODS:
            return "payments.platform_provider.view"
        return "payments.platform_provider.update"

    def get(self, request):
        return success_response("Payment provider settings retrieved.",
                                data=_provider_settings_payload())

    def patch(self, request):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        from . import held_reconciliation

        swept = request.data.get("balance_swept")
        if not isinstance(swept, bool):
            raise ValidationError({"balance_swept": "Expected true or false."})
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            raise ValidationError({"reason": "Say why the setting is changing."})
        definition = ConfigurationDefinition.objects.filter(
            key=held_reconciliation.SWEPT_KEY, is_active=True).first()
        if definition is None:
            raise NotFound("The setting is not in the configuration catalogue; "
                           "run seed_config_catalogue.")
        set_value(definition=definition, value=swept, actor=request.user, reason=reason)
        return success_response("Payment provider settings saved.",
                                data=_provider_settings_payload())


class PlatformProviderSweepListView(APIView):
    """GET: the provider's settlements of the platform balance that the daily check counted, newest first.

    One row per settlement (:class:`~vs_payments.models.ProviderSweep`): the
    provider's id, the kobo that reached the platform's bank, when the provider
    settled it, and the day the check counted it. The provider's raw record is
    not served. ``?limit=`` caps the page (at most 200). Platform staff with
    ``payments.platform_settlement.view`` only, like the checks themselves.

    docstring-name: Provider balance sweeps
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_settlement.view"

    def get(self, request):
        from .models import ProviderSweep

        rows = ProviderSweep.objects.order_by("-settled_at", "-id")
        return success_response("Provider balance sweeps retrieved.", data=[{
            "id": row.pk, "provider": row.provider, "settlement_id": row.settlement_id,
            "currency": row.currency, "amount": row.amount,
            "settled_at": row.settled_at.isoformat() if row.settled_at else None,
            "recorded_on": row.recorded_on.isoformat(),
        } for row in rows[:_limit(request)]])
