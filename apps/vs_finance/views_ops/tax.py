"""Tax obligations and filings.
"""
from __future__ import annotations


from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError
from vs_rbac.scoping import (
    WholeTenantWriteMixin,
    assert_caller_may_change,
    caller_branch_ids,
    shared_write_refusal,
)

from core.response import success_response

from ..money import format_naira
from ..views import resolve_entity
from ..models import (
    TaxFiling,
    TaxObligation,
)
from ..serializers import (
    TaxFilingSerializer,
    TaxObligationSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _int,
    _money,
    _resolve_account,
    _bank_account_in_reach,
    _resolve_bank_account,
    _resolve_currency,
)

# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

#: The refusal subject for a branch-bound write to a filing with no branch.
SHARED_FILING = "a school-wide tax filing"

#: What a filing read loads with it: the obligation, the branch shares and the payments.
FILING_PREFETCH = ("shares__branch", "remittances__branch", "remittances__bank_account")


def _filings_in_reach(request, entity):
    """The returns the caller may open: their branches' own, and the tenant's through their shares.

    A return is a transaction, read by its own branch exclusively
    (:func:`vs_rbac.scoping.transaction_branch_q`). The tenant's one return names
    no branch because it is booked per branch: each :class:`TaxFilingShare` names
    one, and that share is the branch's money. So a branch-bound reader reaches a
    tenant return through a share of one of their branches, and is shown only those
    shares (:func:`_filing_data`). Lagoon View's March WHT return has an Ikeja
    share and a Lekki share: Ngozi at Lekki opens it and sees Lekki's N300. A
    return with no share of theirs, and one not yet given a branch or any share,
    is not found for them, as another branch's is. A whole-tenant reader is not
    narrowed.
    """
    from ..models import TaxFilingShare

    qs = TaxFiling.objects.filter(entity=entity)
    reach = caller_branch_ids(request)
    if reach is None:
        return qs
    ids = tuple(sorted(reach))
    shared_through = TaxFilingShare.objects.filter(branch_id__in=ids).values("filing_id")
    return qs.filter(
        Q(branch_id__in=ids) | Q(branch__isnull=True, pk__in=shared_through),
    )


def _filing_branch(entity, ref, field):
    """The tenant branch ``ref`` (an id) names, or ``None`` for a blank ``ref``.

    Refuses a branch of another tenant with the same 404-style message as an
    unknown id, so a caller learns nothing about branches outside their books.
    """
    from vs_tenants.models import Branch

    if ref in (None, ""):
        return None
    branch = None
    if str(ref).isdigit() and entity.tenant_id:
        branch = Branch.all_objects.filter(tenant_id=entity.tenant_id, pk=int(ref)).first()
    if branch is None:
        raise ValidationError({field: f"There is no branch '{ref}'."})
    return branch


class _TaxObligationWriteMixin(WholeTenantWriteMixin):
    """Every write to a tax obligation needs whole-tenant reach.

    An obligation carries no branch: its liability account and filing day
    govern the return every branch's tax is remitted through.
    """

    shared_subject = "the tax obligations"


# Group endpoint behavior for Tax Obligation List Create View.
class TaxObligationListCreateView(_TaxObligationWriteMixin, _FinanceBase):
    """GET (list) / POST (create) statutory tax obligations for an entity.

    docstring-name: Tax obligations
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.create" if self.request.method == "POST" \
            else "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxObligation.objects.filter(entity=entity).select_related(
            "liability_account", "recoverable_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Tax obligations retrieved.",
            data=TaxObligationSerializer(qs.order_by("code"), many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if not body.get("code"):
            raise ValidationError({"code": "An obligation code is required."})
        if not body.get("obligation_type"):
            raise ValidationError({"obligation_type": "An obligation type is required."})
        obligation = TaxObligation.objects.create(
            entity=entity, code=body["code"], name=body.get("name", body["code"]),
            obligation_type=body["obligation_type"],
            liability_account=_resolve_account(
                request, entity, body.get("liability_account"), "liability_account", required=True),
            recoverable_account=_resolve_account(
                request, entity, body.get("recoverable_account"), "recoverable_account"),
            authority_name=body.get("authority_name", ""),
            frequency=body.get("frequency", "MONTHLY"),
            filing_day=_int(body.get("filing_day", 21), "filing_day", minimum=1),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            "Tax obligation created.",
            data=TaxObligationSerializer(obligation).data, status=201,
        )


# Group endpoint behavior for Tax Obligation Detail View.
class TaxObligationDetailView(_TaxObligationWriteMixin, _FinanceBase):
    """docstring-name: Tax obligations"""
    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.update" if self.request.method == "PATCH" \
            else "finance.tax.view"

    # Support the obligation workflow.
    def _obligation(self, request, pk):
        entity = resolve_entity(request)
        obligation = TaxObligation.objects.filter(entity=entity, pk=pk).first()
        if obligation is None:
            raise NotFound("Tax obligation not found for this entity.")
        return entity, obligation

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, obligation = self._obligation(request, pk)
        return success_response(
            "Tax obligation retrieved.", data=TaxObligationSerializer(obligation).data,
        )

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        entity, obligation = self._obligation(request, pk)
        body = request.data or {}
        if "name" in body:
            obligation.name = body["name"]
        if "liability_account" in body:
            obligation.liability_account = _resolve_account(
                request, entity, body.get("liability_account"), "liability_account", required=True)
        if "recoverable_account" in body:
            obligation.recoverable_account = _resolve_account(
                request, entity, body.get("recoverable_account"), "recoverable_account")
        if "authority_name" in body:
            obligation.authority_name = body["authority_name"]
        if "frequency" in body:
            obligation.frequency = body["frequency"]
        if "filing_day" in body:
            obligation.filing_day = _int(body["filing_day"], "filing_day", minimum=1)
        if "is_active" in body:
            obligation.is_active = _bool(body["is_active"])
        obligation.save()
        return success_response(
            "Tax obligation updated.", data=TaxObligationSerializer(obligation).data,
        )


# Group endpoint behavior for Tax Obligation Outstanding View.
class TaxObligationOutstandingView(_FinanceBase):
    """GET - per-obligation unremitted balance sitting in each control account.

    A branch-bound reader sees only their branches' part of each balance, counted
    by entry branch as a return counts its lines; a whole-tenant reader sees the
    whole balance.

    docstring-name: Outstanding tax obligations
    """

    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..tax_filing import outstanding_obligations

        entity = resolve_entity(request)
        rows = outstanding_obligations(entity, branch_ids=caller_branch_ids(request))
        return success_response(
            "Outstanding tax obligations retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        **r,
                        "payable_balance": {"kobo": r["payable_balance"], "naira": format_naira(r["payable_balance"])},
                        "recoverable_balance": {"kobo": r["recoverable_balance"], "naira": format_naira(r["recoverable_balance"])},
                        "net_outstanding": {"kobo": r["net_outstanding"], "naira": format_naira(r["net_outstanding"])},
                    }
                    for r in rows
                ],
            },
        )


# Group endpoint behavior for Tax Filing Summary View.
class TaxFilingSummaryView(_FinanceBase):
    """GET - header KPIs over **all** tax filings (accurate under pagination).

    docstring-name: Tax filings
    """

    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, F, Q, Sum
        from django.db.models.functions import Coalesce

        from ..constants import TaxFilingStatus

        from ..models import TaxFilingShare

        entity = resolve_entity(request)
        filings = _filings_in_reach(request, entity)
        agg = filings.aggregate(
            outstanding=Coalesce(
                Sum(F("amount_due") - F("amount_paid"),
                    filter=~Q(filing_status=TaxFilingStatus.PAID)), 0),
            open=Count("id", filter=Q(filing_status=TaxFilingStatus.DRAFT)),
            filed=Count("id", filter=Q(filing_status=TaxFilingStatus.FILED)),
            paid=Count("id", filter=Q(filing_status=TaxFilingStatus.PAID)),
        )
        reach = caller_branch_ids(request)
        if reach is not None:
            # A branch-bound reader's outstanding is their branches' shares only.
            agg["outstanding"] = TaxFilingShare.objects.filter(
                filing__in=filings.exclude(filing_status=TaxFilingStatus.PAID),
                branch_id__in=tuple(sorted(reach)),
            ).aggregate(o=Coalesce(Sum(F("amount_due") - F("amount_paid")), 0))["o"]
        return success_response("Tax filing summary retrieved.", data=agg)


# Group endpoint behavior for Tax Filing List Create View.
class TaxFilingListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET (list) / POST (prepare from GL) tax filings for an entity.

    Preparing a filing reads the obligation's accounts across every branch and
    files the draft with no branch, so it needs whole-tenant reach.

    docstring-name: Tax filings
    """

    shared_subject = SHARED_FILING

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.file" if self.request.method == "POST" \
            else "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = _filings_in_reach(request, entity).select_related(
            "obligation__liability_account").prefetch_related(*FILING_PREFETCH)
        if (ob := request.query_params.get("obligation")):
            qs = qs.filter(obligation_id=ob)
        if (status_val := request.query_params.get("filing_status")):
            qs = qs.filter(filing_status=status_val)
        return self.paginate(
            request, qs.order_by("-period_end", "-id"), TaxFilingSerializer,
            context={"request": request, "branch_ids": caller_branch_ids(request)})

    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..tax_filing import prepare_filing

        entity = resolve_entity(request)
        body = request.data or {}
        ref = body.get("obligation")
        if ref in (None, ""):
            raise ValidationError({"obligation": "A tax obligation is required."})
        obligation = TaxObligation.objects.filter(entity=entity, pk=ref).first()
        if obligation is None:
            raise ValidationError({"obligation": f"No tax obligation '{ref}' in this entity."})
        filing = prepare_filing(
            obligation,
            period_start=_date(body.get("period_start"), "period_start", required=True),
            period_end=_date(body.get("period_end"), "period_end", required=True),
            due_date=_date(body.get("due_date"), "due_date"),
            currency=_resolve_currency(body.get("currency")),
            actor_user=request.user,
        )
        return success_response(
            f"Tax filing {filing.document_number} prepared.",
            data=_filing_data(filing), status=201,
        )


# Define Tax Filing Action Base values.
class _TaxFilingActionBase(_FinanceBase):
    """Resolve one filing in the caller's reach; on a write, one they may change.

    A filing with no branch is the tenant's one return, reached by a
    branch-bound reader through a share of one of their branches and shown
    narrowed to those shares (:func:`_filings_in_reach`, :func:`_filing_data`);
    one with no share of theirs is a 404. Filing, un-filing and reversing a remittance change
    the whole return, so they need whole-tenant reach: a branch-bound caller is
    refused with a 403 ``SHARED_RECORD_READ_ONLY`` before anything is posted.
    Paying (``shares_only``) touches one branch's share, and the service refuses
    a share outside the caller's branches. A filing of one of the caller's own
    branches is theirs.
    """

    # Support the filing workflow.
    def _filing(self, request, pk, *, shares_only=False):
        from rest_framework.permissions import SAFE_METHODS

        entity = resolve_entity(request)
        filing = _filings_in_reach(request, entity).filter(pk=pk).select_related(
            "obligation__liability_account").prefetch_related(*FILING_PREFETCH).first()
        if filing is None:
            raise NotFound("Tax filing not found for this entity.")
        if request.method not in SAFE_METHODS and not shares_only:
            assert_caller_may_change(
                request.user, getattr(request, "tenant", None), (filing.branch_id,),
                message=shared_write_refusal(SHARED_FILING),
            )
        return entity, filing


def _filing_data(filing, reach=None):
    """The serialized filing, re-read with its shares and remittances.

    ``reach`` (the caller's branch ids, or ``None`` for the whole tenant) narrows
    the breakdown, the payments and the totals to the caller's branches.
    """
    fresh = TaxFiling.objects.select_related("obligation__liability_account").prefetch_related(
        *FILING_PREFETCH).get(pk=filing.pk)
    return TaxFilingSerializer(fresh, context={"branch_ids": reach}).data


class TaxFilingDetailView(_TaxFilingActionBase):
    """docstring-name: Tax filings"""
    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, filing = self._filing(request, pk)
        return success_response(
            "Tax filing retrieved.",
            data=TaxFilingSerializer(
                filing, context={"request": request, "branch_ids": caller_branch_ids(request)},
            ).data,
        )


class TaxFilingLinesView(_TaxFilingActionBase):
    """GET /finance/tax-filings/<id>/lines/?entity= - the lines a return declares, paginated.

    Each row is one ledger line: ``date``, ``document`` (``type``, ``id``,
    ``number`` of the invoice, bill, payroll run or journal behind it),
    ``journal_id`` and ``journal_number``, ``account`` (``id``, ``code``,
    ``name``), ``branch_id`` and ``branch_name`` (the branch it counts under),
    ``role`` (PAYABLE or RECOVERABLE), ``amount`` (kobo, signed the way the tax
    reads it) and ``is_late`` (dated before the return's period, declared here
    because its own month was already filed). A filed return lists what it
    declared; a draft what it would declare now
    (:func:`vs_finance.tax_filing.return_lines`).

    Read under ``finance.tax.view`` and narrowed like the return itself: a
    branch-bound reader opens the tenant's return through a share of their
    branch, and is shown only the lines their branches count.

    docstring-name: Tax return lines
    """

    rbac_permission = "finance.tax.view"

    def get(self, request, pk):
        from core.pagination import XVSPagination
        from vs_tenants.models import Branch

        from ..journal_documents import journal_documents
        from ..tax_filing import return_lines

        entity, filing = self._filing(request, pk)
        lines = return_lines(filing, branch_ids=caller_branch_ids(request))
        paginator = XVSPagination()
        paginator.page_size = 25
        page = list(paginator.paginate_queryset(lines, request, view=self))
        documents = journal_documents(line.entry_id for line in page)
        names = dict(Branch.all_objects.filter(
            tenant_id=entity.tenant_id, pk__in={line.counted_branch_id for line in page},
        ).values_list("pk", "name"))
        rows = [
            {
                "id": line.pk,
                "date": line.entry.date,
                "document": documents.get(line.entry_id),
                "journal_id": line.entry_id,
                "journal_number": line.entry.document_number,
                "account": {
                    "id": line.account_id, "code": line.account.code, "name": line.account.name,
                },
                "branch_id": line.counted_branch_id,
                "branch_name": names.get(line.counted_branch_id),
                "role": line.tax_role,
                "amount": int(line.tax_amount),
                "is_late": bool(line.is_late),
            }
            for line in page
        ]
        return paginator.get_paginated_response(rows)


# Group endpoint behavior for Tax Filing File View.
class TaxFilingFileView(_TaxFilingActionBase):
    """POST - submit a draft return: declare its lines, post each branch's netting/penalty.

    ``adjustment_branch`` (optional, a branch id) names the branch that bears a
    penalty; without it the penalty is shared in proportion to each branch's tax.

    docstring-name: File a tax return
    """

    rbac_permission = "finance.tax.file"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import ANY_SHARE, file_filing

        entity, filing = self._filing(request, pk)
        body = request.data or {}
        adjustment = body.get("adjustment_amount")
        adjustment_branch = (
            _filing_branch(entity, body.get("adjustment_branch"), "adjustment_branch")
            if body.get("adjustment_branch") not in (None, "") else ANY_SHARE
        )
        file_filing(
            filing,
            filed_date=_date(body.get("filed_date"), "filed_date", required=True),
            filing_reference=body.get("filing_reference", ""),
            adjustment_amount=_money(adjustment, "adjustment_amount") if adjustment not in (None, "") else 0,
            adjustment_account=_resolve_account(
                request, entity, body.get("adjustment_account"), "adjustment_account"),
            adjustment_branch=adjustment_branch,
            actor_user=request.user,
        )
        return success_response(
            f"Tax filing {filing.document_number} filed.",
            data=_filing_data(filing),
        )


# Group endpoint behavior for Tax Filing Unfile View.
class TaxFilingUnfileView(_TaxFilingActionBase):
    """POST - revert a filed return to draft (reverse its netting/penalty journal).

    docstring-name: Un-file a tax return
    """

    rbac_permission = "finance.tax.file"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import unfile_filing

        _, filing = self._filing(request, pk)
        unfile_filing(filing, actor_user=request.user)
        return success_response(
            f"Tax filing {filing.document_number} un-filed.",
            data=_filing_data(filing),
        )


class TaxFilingPayView(_TaxFilingActionBase):
    """POST - remit a filed return's branch shares (``Dr payable, Cr bank`` per share).

    One payment: ``bank_account``, ``pay_date``, optional ``amount`` and optional
    ``branch`` (the share it pays). Without ``branch`` it pays the only unpaid
    share, else the bank account's own branch's share. Several payments at once:
    ``shares``, a list of ``{branch, bank_account, amount?}`` sharing the one
    ``pay_date``, all recorded or none. Each share is paid only from its own
    branch's bank account (see :func:`vs_finance.tax_filing.pay_filing`).

    Unlike the other writes to a return, paying does not need whole-tenant reach:
    a branch-bound bursar pays her own branches' shares, and a share outside
    her branches is refused.

    docstring-name: Pay a tax filing
    """

    rbac_permission = "finance.tax.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import ANY_SHARE, pay_filing_shares

        entity, filing = self._filing(request, pk, shares_only=True)
        body = request.data or {}
        pay_date = _date(body.get("pay_date"), "pay_date", required=True)
        rows = body.get("shares")
        if rows in (None, ""):
            rows = [{key: body[key] for key in ("branch", "bank_account", "amount") if key in body}]
            fields = ("branch", "bank_account", "amount")
        elif not isinstance(rows, list) or not rows:
            raise ValidationError({"shares": "Give at least one payment: the branch paying and the bank account it pays from."})
        else:
            fields = None

        payments = []
        for i, row in enumerate(rows):
            names = fields or (f"shares[{i}].branch", f"shares[{i}].bank_account", f"shares[{i}].amount")
            if not isinstance(row, dict):
                raise ValidationError({f"shares[{i}]": "Each payment is an object."})
            named = "branch" in row and row.get("branch") not in ("",)
            branch = _filing_branch(entity, row.get("branch"), names[0]) if named else ANY_SHARE
            if branch is ANY_SHARE and filing.branch_id is None:
                # The service pays the account's own branch's share.
                bank = _bank_account_in_reach(
                    request, entity, row.get("bank_account"), names[1])
            else:
                bank = _resolve_bank_account(
                    request, entity, row.get("bank_account"), names[1],
                    document_branch=(branch.pk if branch not in (None, ANY_SHARE)
                                     else filing.branch_id),
                    noun="tax filing")
            amount = (_money(row["amount"], names[2])
                      if row.get("amount") not in (None, "") else None)
            payments.append((branch, bank, amount))

        reach = caller_branch_ids(request)
        pay_filing_shares(
            filing, payments=payments, pay_date=pay_date, actor_user=request.user, reach=reach,
        )
        return success_response(
            f"Tax filing {filing.document_number} remitted.",
            data=_filing_data(filing, reach),
        )


class TaxRemittanceReverseView(_TaxFilingActionBase):
    """POST - reverse one remittance recorded in error; the return goes back to FILED.

    Body: ``reason`` (required) and optional ``date`` for the reversal (default:
    the remittance's own date, or today once that month is closed).

    docstring-name: Reverse a tax remittance
    """

    rbac_permission = "finance.tax.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk, remittance_pk):
        from ..tax_filing import reverse_remittance

        _, filing = self._filing(request, pk)
        remittance = filing.remittances.filter(pk=remittance_pk).first()
        if remittance is None:
            raise NotFound("Tax remittance not found on this filing.")
        body = request.data or {}
        if not str(body.get("reason") or "").strip():
            raise ValidationError({"reason": "Give the reason the payment is being reversed."})
        reverse_remittance(
            remittance, reason=body["reason"], date=_date(body.get("date"), "date"),
            actor_user=request.user,
        )
        return success_response(
            f"Remittance on tax filing {filing.document_number} reversed.",
            data=_filing_data(filing),
        )
