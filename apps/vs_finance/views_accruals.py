"""REST API for receivables accruals (mounted at ``/v1/finance/``).

Deferred income, the doubtful-debt provision, customer deposits, the recovery of
written-off debt and the policy behind them. Same conventions as the rest of the
finance surface: entity-scoped via ``?entity=``, the ``{success, message, data}``
envelope, one ``finance.<resource>.<action>`` key per route, and thin views over
the services that own every posting.

The runs (release, reverse, provision, forfeit) act for every branch at once and
write one journal per branch, so they are refused to a caller whose reach is not
the whole tenant (:class:`~vs_rbac.scoping.WholeTenantWriteMixin`). Reads narrow to
the caller's branches like every other transaction read.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import SAFE_METHODS

from core.response import success_response
from vs_config.clock import tenant_today
from vs_rbac.scoping import (
    WholeTenantWriteMixin,
    assert_caller_may_change,
    transaction_branch_q,
    transaction_branch_scope,
)

from .constants import DepositStatus, FinanceAuditAction
from .models import (
    CustomerDeposit,
    DoubtfulDebtProvision,
    FiscalPeriod,
    Payment,
    WriteOffRequest,
)
from .serializers import (
    CustomerDepositSerializer,
    DoubtfulDebtProvisionSerializer,
    WriteOffRequestSerializer,
)
from .views import resolve_entity
from .views_ar import _paginate, _resolve_customer
from .views_ops import _FinanceBase, _date
from .views_ops.base import _filter_by_branch
from .views_settings import _FinanceSettingsView, _settings_history


def _release_payload(release):
    return {
        "id": release.pk, "branch_id": release.branch_id,
        "date": release.journal.date.isoformat(), "amount": release.amount,
        "journal_id": release.journal_id,
    }


class _WholeTenantRun(WholeTenantWriteMixin, _FinanceBase):
    """A run for every branch at once: any caller may read, only a whole-tenant caller may run it."""

    shared_subject = "every branch's books at once"


# --------------------------------------------------------------------------- #
# Policy                                                                      #
# --------------------------------------------------------------------------- #

class FinanceReceivablesSettingsView(_FinanceSettingsView):
    """Read or update the entity's receivables policy.

    ``revenue_recognition`` (SPREAD_MONTHLY | AT_PERIOD_START), ``provision_bands``
    (``[{over_days, rate_bps}]``), ``deposits_offset_unpaid_bills`` and
    ``unclaimed_deposit_years``. Whole-tenant writes only.

    docstring-name: Receivables settings
    """

    settings_subject = "the finance receivables settings"

    def _payload(self, entity, policy):
        from .receivables_policy import serialize_receivables_policy
        from .settings_ownership import RECEIVABLES_SETTING_CONSUMERS

        return {
            "settings": serialize_receivables_policy(policy),
            "consumers": RECEIVABLES_SETTING_CONSUMERS,
            "history": _settings_history(
                entity, FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED),
        }

    def get(self, request):
        from .receivables_policy import resolve_receivables_policy

        entity = resolve_entity(request)
        return success_response(
            "Finance receivables settings retrieved.",
            data=self._payload(entity, resolve_receivables_policy(entity)),
        )

    def patch(self, request):
        from .receivables_policy import update_receivables_policy

        entity = resolve_entity(request)
        policy = update_receivables_policy(
            entity=entity, data=request.data or {}, actor_user=request.user)
        return success_response(
            "Finance receivables settings saved.", data=self._payload(entity, policy))


# --------------------------------------------------------------------------- #
# Deferred income                                                             #
# --------------------------------------------------------------------------- #

class DeferredIncomeView(_FinanceBase):
    """GET - deferred income waiting, released, and due by month, in the caller's branches.

    ``?branch=`` narrows it to one branch the caller works in; a branch outside
    their reach is answered like one that does not exist.

    docstring-name: Deferred income
    """

    rbac_permission = "finance.deferredincome.view"

    def get(self, request):
        from .deferred_income import deferred_income_summary
        from .models import DeferredIncomeEntry

        entity = resolve_entity(request)
        rows = _filter_by_branch(DeferredIncomeEntry.objects.all(), request, entity)
        return success_response(
            "Deferred income retrieved.",
            data=deferred_income_summary(
                entity, scope=transaction_branch_scope(request), rows=rows),
        )


class DeferredIncomeReleaseListView(_FinanceBase):
    """GET - the deferred income releases posted, newest first, in the caller's branches.

    One row per release journal: its branch, date and month, the period it
    falls in and whether that period is open, the amount moved to revenue, the
    journal, and whether it has been reversed. ``can_reverse`` is true for a
    release not yet reversed whose period is still open, the ones the undo form
    (``POST deferred-income/reverse/`` with that ``period_id``) would reverse;
    that action is a whole-tenant run and reverses every release of the period.
    Filters: ``?branch=`` (one branch in reach; another answers like an unknown
    one), ``?period=`` (id), ``?month=YYYY-MM`` and ``?reversed=true|false``.
    Paginated.

    docstring-name: Deferred income releases
    """

    rbac_permission = "finance.deferredincome.view"

    def get(self, request):
        from django.db.models import OuterRef, Subquery

        from .models import DeferredIncomeRelease

        entity = resolve_entity(request)
        periods = FiscalPeriod.objects.filter(
            entity=entity, start_date__lte=OuterRef("journal__date"),
            end_date__gte=OuterRef("journal__date"),
        ).order_by("start_date")
        qs = (
            DeferredIncomeRelease.objects
            .filter(transaction_branch_q(request), entity=entity)
            .select_related("branch", "journal")
            .annotate(
                period_id=Subquery(periods.values("pk")[:1]),
                period_name=Subquery(periods.values("name")[:1]),
                period_status=Subquery(periods.values("status")[:1]),
            )
        )
        qs = _filter_by_branch(qs, request, entity)
        params = request.query_params
        if (ref := params.get("period")):
            if not str(ref).isdigit():
                raise ValidationError({"period": "Name the period by id."})
            period = FiscalPeriod.objects.filter(entity=entity, pk=int(ref)).first()
            if period is None:
                raise ValidationError({"period": "No such period in this entity."})
            qs = qs.filter(journal__date__gte=period.start_date,
                           journal__date__lte=period.end_date)
        if (month := params.get("month")):
            try:
                year, number = (int(part) for part in str(month).split("-"))
                qs = qs.filter(journal__date__year=year, journal__date__month=number)
            except ValueError:
                raise ValidationError({"month": "Use YYYY-MM."})
        if (flag := params.get("reversed")) in ("true", "false"):
            qs = qs.filter(reversed_at__isnull=flag == "false")
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(
            qs.order_by("-journal__date", "-id"), request, view=self)
        return paginator.get_paginated_response([_release_row(row) for row in page])


def _release_row(release) -> dict:
    """One release as the release list shows it."""
    from .constants import PeriodStatus

    date = release.journal.date
    return {
        "id": release.pk,
        "branch_id": release.branch_id,
        "branch_name": release.branch.name if release.branch_id else None,
        "date": date.isoformat(),
        "month": f"{date.year:04d}-{date.month:02d}",
        "period_id": release.period_id,
        "period_name": release.period_name,
        "period_status": release.period_status,
        "amount": release.amount,
        "journal_id": release.journal_id,
        "journal_number": release.journal.document_number or None,
        "reversed": release.reversed_at is not None,
        "reversed_at": release.reversed_at.isoformat() if release.reversed_at else None,
        "can_reverse": release.reversed_at is None and release.period_status == PeriodStatus.OPEN,
    }


class DeferredIncomeReleaseView(_WholeTenantRun):
    """POST - release the deferred income due by ``up_to`` (default today). Idempotent.

    ``up_to`` may not be later than today: income is released for months that have
    happened, never ahead of them.

    docstring-name: Release deferred income
    """

    rbac_permission = "finance.deferredincome.run"

    def post(self, request):
        from .deferred_income import release_deferred_income

        entity = resolve_entity(request)
        today = tenant_today(entity.tenant)
        up_to = _date((request.data or {}).get("up_to"), "up_to") or today
        if up_to > today:
            raise ValidationError(
                {"up_to": "Deferred income is released for days that have passed, not ahead."})
        releases = release_deferred_income(entity, up_to=up_to, actor_user=request.user)
        return success_response(
            f"{len(releases)} deferred income release(s) posted.",
            data={"up_to": up_to.isoformat(),
                  "releases": [_release_payload(release) for release in releases]},
        )


class DeferredIncomeReverseView(_WholeTenantRun):
    """POST ``{period}`` - reverse the deferred income releases dated in an open period.

    docstring-name: Reverse deferred income releases
    """

    rbac_permission = "finance.deferredincome.reverse"

    def post(self, request):
        from .deferred_income import reverse_deferred_release

        entity = resolve_entity(request)
        ref = (request.data or {}).get("period")
        if ref in (None, "") or not str(ref).isdigit():
            raise ValidationError({"period": "Name the period by id."})
        period = FiscalPeriod.objects.filter(entity=entity, pk=int(ref)).first()
        if period is None:
            raise NotFound("No such period in this entity.")
        count = reverse_deferred_release(entity, period, actor_user=request.user)
        return success_response(
            f"{count} deferred income release(s) reversed.",
            data={"period": period.pk, "reversed": count},
        )


# --------------------------------------------------------------------------- #
# Doubtful-debt provision                                                     #
# --------------------------------------------------------------------------- #

class DoubtfulDebtProvisionListCreateView(_WholeTenantRun):
    """GET (list) / POST ``{as_of, narration?}`` (raise a draft run with its figures).

    ``?branch=`` keeps the runs with a line for that branch, one the caller
    works in; a branch outside their reach is answered like an unknown one.

    docstring-name: Doubtful-debt provisions
    """

    @property
    def rbac_permission(self):
        return ("finance.provision.view" if self.request.method in SAFE_METHODS
                else "finance.provision.create")

    def get(self, request):
        entity = resolve_entity(request)
        qs = (DoubtfulDebtProvision.objects
              .filter(transaction_branch_q(request), entity=entity)
              .select_related("entity__tenant", "branch")
              .prefetch_related("lines__branch").order_by("-id"))
        # A run names no branch; ``?branch=`` keeps the runs with a line for it.
        qs = _filter_by_branch(qs, request, entity, column="lines__branch")
        return _paginate(request, qs, DoubtfulDebtProvisionSerializer, self)

    def post(self, request):
        from .provisions import prepare_provision

        entity = resolve_entity(request)
        body = request.data or {}
        provision = prepare_provision(
            entity, as_of=_date(body.get("as_of"), "as_of", required=True),
            narration=str(body.get("narration") or "")[:255], actor_user=request.user,
        )
        return success_response(
            f"Provision {provision.document_number} prepared.",
            data=DoubtfulDebtProvisionSerializer(provision).data, status=201,
        )


class _ProvisionActionBase(_WholeTenantRun):
    def _provision(self, request, pk):
        entity = resolve_entity(request)
        provision = (DoubtfulDebtProvision.objects
                     .filter(transaction_branch_q(request), entity=entity, pk=pk)
                     .prefetch_related("lines__branch").first())
        if provision is None:
            raise NotFound("No such provision in this entity.")
        return provision


class DoubtfulDebtProvisionDetailView(_ProvisionActionBase):
    """GET - one provision run.

    docstring-name: Doubtful-debt provisions
    """

    rbac_permission = "finance.provision.view"

    def get(self, request, pk):
        return success_response(
            "Provision retrieved.",
            data=DoubtfulDebtProvisionSerializer(self._provision(request, pk)).data)


class DoubtfulDebtProvisionSubmitView(_ProvisionActionBase):
    """POST - submit a draft provision run for approval.

    docstring-name: Submit a doubtful-debt provision
    """

    rbac_permission = "finance.provision.submit"

    def post(self, request, pk):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        provision = self._provision(request, pk)
        instance = submit_for_approval(provision, requested_by=request.user)
        provision.refresh_from_db()
        return success_response(
            f"Provision {provision.document_number} submitted for approval.",
            data=DoubtfulDebtProvisionSerializer(provision).data
            | {"approval": release_svc.approval_block(instance)},
        )


class DoubtfulDebtProvisionPostView(_ProvisionActionBase):
    """POST - post a draft provision run where no approval route stops it.

    docstring-name: Post a doubtful-debt provision
    """

    rbac_permission = "finance.provision.post"

    def post(self, request, pk):
        from .approvals import guard_direct_post
        from .provisions import post_provision

        provision = self._provision(request, pk)
        guard_direct_post(provision, request, noun="provision")
        post_provision(provision, actor_user=request.user)
        provision.refresh_from_db()
        return success_response(
            f"Provision {provision.document_number} posted.",
            data=DoubtfulDebtProvisionSerializer(provision).data)


# --------------------------------------------------------------------------- #
# Write-off recovery                                                          #
# --------------------------------------------------------------------------- #

class WriteOffRecoverView(_FinanceBase):
    """POST ``{payment, amount?}`` - recover a written-off debt from a later receipt.

    The receipt (id or document number) must be the debtor's, of the bill's branch,
    and still hold the credit. ``amount`` defaults to the smaller of that credit and
    what remains written off.

    docstring-name: Recover a written-off debt
    """

    rbac_permission = "finance.writeoff.reverse"

    def post(self, request, pk):
        from .credit_notes import recover_write_off
        from .views_ops import _money

        entity = resolve_entity(request)
        write_off = WriteOffRequest.objects.filter(
            transaction_branch_q(request), entity=entity, pk=pk).first()
        if write_off is None:
            raise NotFound("Write-off request not found for this entity.")
        body = request.data or {}
        ref = body.get("payment")
        if ref in (None, ""):
            raise ValidationError({"payment": "Name the receipt that paid the debt."})
        payments = Payment.objects.filter(transaction_branch_q(request), entity=entity)
        payment = (payments.filter(pk=int(ref)).first() if str(ref).isdigit()
                   else payments.filter(document_number=str(ref)).first())
        if payment is None:
            raise NotFound(f"No receipt matches '{ref}' for this entity.")
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        recovery = recover_write_off(write_off, payment, amount=amount, actor_user=request.user)
        write_off.refresh_from_db()
        return success_response(
            f"Recovered {recovery.amount} kobo of write-off {write_off.document_number}.",
            data={"recovery": {"id": recovery.pk, "amount": recovery.amount,
                               "journal_id": recovery.journal_id,
                               "payment_id": recovery.payment_id},
                  "write_off": WriteOffRequestSerializer(write_off).data},
        )


# --------------------------------------------------------------------------- #
# Customer deposits                                                           #
# --------------------------------------------------------------------------- #

class CustomerDepositListView(_FinanceBase):
    """GET - refundable deposits in the caller's branches (``?customer=``, ``?status=``, ``?branch=``).

    ``?branch=`` names one branch the caller works in; a branch outside their
    reach is answered like one that does not exist.

    docstring-name: Customer deposits
    """

    rbac_permission = "finance.deposit.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = (CustomerDeposit.objects.filter(transaction_branch_q(request), entity=entity)
              .select_related("customer", "invoice", "branch", "release_note"))
        params = request.query_params
        if params.get("customer"):
            qs = qs.filter(customer=_resolve_customer(request, entity, params["customer"]))
        if params.get("status"):
            status = str(params["status"]).upper()
            if status not in DepositStatus.values:
                raise ValidationError({"status": f"Use one of {', '.join(DepositStatus.values)}."})
            qs = qs.filter(status=status)
        qs = _filter_by_branch(qs, request, entity)
        return _paginate(request, qs.order_by("-id"), CustomerDepositSerializer, self)


class CustomerDepositReleaseView(_FinanceBase):
    """POST ``{customer, offset?}`` - release a customer's held deposits.

    Returns the deposits as credit to refund, or with ``offset`` sets them against
    the customer's unpaid bills first, where the policy allows it. The caller must
    reach every branch that holds one of the deposits.

    docstring-name: Release customer deposits
    """

    rbac_permission = "finance.deposit.settle"

    @transaction.atomic
    def post(self, request):
        from .deposits import release_deposits
        from .serializers import CreditNoteSerializer

        entity = resolve_entity(request)
        body = request.data or {}
        customer = _resolve_customer(request, entity, body.get("customer"))
        branch_ids = set(
            CustomerDeposit.objects.filter(customer=customer, status=DepositStatus.HELD)
            .values_list("branch_id", flat=True))
        if not branch_ids:
            raise ValidationError({"customer": f"{customer.code} holds no deposit."})
        assert_caller_may_change(
            request.user, entity.tenant, branch_ids,
            message="Some of these deposits are held by a branch you do not work in.",
        )
        offset = body.get("offset", False)
        if not isinstance(offset, bool):
            raise ValidationError({"offset": "Use true or false."})
        notes = release_deposits(customer, offset=offset, actor_user=request.user)
        return success_response(
            f"Released {customer.code}'s deposits.",
            data={"credit_notes": CreditNoteSerializer(notes, many=True).data},
        )


class CustomerDepositForfeitView(_WholeTenantRun):
    """POST ``{as_of?}`` - take deposits unclaimed past the policy's limit to income.

    docstring-name: Forfeit unclaimed deposits
    """

    rbac_permission = "finance.deposit.run"

    def post(self, request):
        from .deposits import forfeit_unclaimed_deposits

        entity = resolve_entity(request)
        today = tenant_today(entity.tenant)
        as_of = _date((request.data or {}).get("as_of"), "as_of") or today
        if as_of > today:
            raise ValidationError({"as_of": "Deposits are forfeited as of today or earlier."})
        outcome = forfeit_unclaimed_deposits(entity, as_of=as_of, actor_user=request.user)
        return success_response(
            f"{len(outcome['forfeitures'])} forfeiture journal(s) posted.",
            data={
                "forfeitures": [
                    {"id": f.pk, "branch_id": f.branch_id, "amount": f.amount,
                     "journal_id": f.journal_id}
                    for f in outcome["forfeitures"]
                ],
                "skipped": outcome["skipped"],
            },
        )


__all__ = [
    "CustomerDepositForfeitView",
    "CustomerDepositListView",
    "CustomerDepositReleaseView",
    "DeferredIncomeReleaseView",
    "DeferredIncomeReverseView",
    "DeferredIncomeView",
    "DoubtfulDebtProvisionDetailView",
    "DoubtfulDebtProvisionListCreateView",
    "DoubtfulDebtProvisionPostView",
    "DoubtfulDebtProvisionSubmitView",
    "FinanceReceivablesSettingsView",
    "WriteOffRecoverView",
]
