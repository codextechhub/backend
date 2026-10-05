"""Petty cash funds, their vouchers, and the returns that bank their cash.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from vs_rbac.scoping import WholeTenantWriteMixin, transaction_branch_q

from core.response import success_response
from vs_config.clock import branch_today

from ..constants import DocumentStatus, PettyCashReturnKind
from ..money import format_naira
from ..views import resolve_entity
from ..models import (
    JournalLine,
    PettyCashFund,
    PettyCashReturn,
    PettyCashVoucher,
    PettyCashVoucherLine,
)
from ..serializers import (
    PettyCashFundSerializer,
    PettyCashReturnSerializer,
    PettyCashVoucherSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _dec,
    _filter_by_branch,
    _inherited_branch_id,
    _int,
    _money,
    _transaction_branch,
    _require_lines,
    _resolve_account,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_tax,
)

# --------------------------------------------------------------------------- #
# Petty cash                                                                  #
# --------------------------------------------------------------------------- #

# Support the resolve user workflow.
def _resolve_user(ref, field, entity):
    """Resolve a user of ``entity``'s own tenant by id (or return None for a blank ref).

    A custodian, a spender or a counter is somebody who works at the tenant, so a
    user of another tenant is unknown here exactly as a mistyped id is. Without the
    tenant, naming another school's user id would attach them to this fund and the
    fund's response would print their name and email.
    """
    if ref in (None, ""):
        return None
    from django.contrib.auth import get_user_model

    if not str(ref).isdigit():
        raise ValidationError({field: f"No user '{ref}'."})
    users = get_user_model().objects.filter(pk=int(ref))
    if entity.tenant_id is not None:
        users = users.filter(tenant_id=entity.tenant_id)
    user = users.first()
    if user is None:
        raise ValidationError({field: f"No user '{ref}'."})
    return user


# Group endpoint behavior for Petty Cash Fund List Create View.
class PettyCashFundListCreateView(_FinanceBase):
    """GET (list) / POST (create) petty-cash funds for an entity.

    docstring-name: Petty cash funds
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.pettycash.create" if self.request.method == "POST" \
            else "finance.pettycash.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = PettyCashFund.objects.filter(
            transaction_branch_q(request), entity=entity,
        ).select_related("gl_account", "custodian", "closed_by")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Petty cash funds retrieved.",
            data=PettyCashFundSerializer(qs.order_by("name"), many=True).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if not body.get("name"):
            raise ValidationError({"name": "A fund name is required."})
        fund = PettyCashFund.objects.create(
            entity=entity, name=body["name"],
            # A float is one branch's cash tin.
            branch=_transaction_branch(request, entity, body),
            gl_account=_resolve_account(request, entity, body.get("gl_account"), "gl_account", required=True),
            custodian=_resolve_user(body.get("custodian"), "custodian", entity),
            custodian_name=body.get("custodian_name", ""),
            float_amount=_money(body.get("float_amount", 0), "float_amount"),
            currency=_resolve_currency(body.get("currency")),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            "Petty cash fund created.",
            data=PettyCashFundSerializer(fund).data, status=201,
        )


# Define Petty Cash Fund Action Base values.
class _PettyCashFundActionBase(_FinanceBase):
    # Support the fund workflow.
    def _fund(self, request, pk):
        entity = resolve_entity(request)
        fund = PettyCashFund.objects.filter(
            transaction_branch_q(request), entity=entity, pk=pk,
        ).select_related("custodian", "closed_by").first()
        if fund is None:
            raise NotFound("Petty cash fund not found for this entity.")
        return entity, fund


# Group endpoint behavior for Petty Cash Fund Detail View.
class PettyCashFundDetailView(_PettyCashFundActionBase):
    """docstring-name: Petty cash funds"""
    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.pettycash.update" if self.request.method == "PATCH" \
            else "finance.pettycash.view"

    @staticmethod
    def _counter_line(ln, fund):
        """The line on the other side of the journal that ``ln`` moved against.

        A spend credits petty cash against its expense lines, so the first debit
        names it. A return to the bank holds two pairs in one journal (the cash
        banked, then a count difference), so a line on the other side for the same
        amount wins, the nearest by line number, the earlier on a tie: the banked
        cash pairs with the bank line and a shortage with the over and short line.
        """
        inflow = int(ln.debit or 0)
        others = [
            line for line in ln.entry.lines.all()
            if line.account_id != fund.gl_account_id
            and (int(line.credit or 0) if inflow else int(line.debit or 0))
        ]
        size = inflow or int(ln.credit or 0)
        same = [
            line for line in others
            if (int(line.credit or 0) if inflow else int(line.debit or 0)) == size
        ]
        if same:
            return min(same, key=lambda line: (abs(line.line_no - ln.line_no), line.line_no))
        return others[0] if others else None

    # Support the register workflow.
    def _register(self, fund, *, limit=80):
        """The fund's GL ledger as a movement register, newest first, running balance.

        ``in``/``out`` are the petty-cash debit/credit. ``category`` comes from the
        line it moved against (:meth:`_counter_line`): cash in from a bank is a
        'Top-up' and cash out to one 'Returned to bank'; a count difference is
        'Count over' or 'Count short'; a spend is named by its expense account.
        The running balance walks back from the fund's ``current_balance``, the
        ledger figure, so the lines walked are the same ledger
        (:func:`vs_finance.branch_ledger.ledger_lines`): a reversed voucher and its
        reversal both appear and cancel.
        """
        from ..account_mappings import resolve_mapped_account
        from ..branch_ledger import ledger_lines
        from ..constants import AccountMappingKey
        from ..exceptions import MissingAccountError
        from ..models import BankAccount

        lines = list(
            ledger_lines(fund.entity)
            .filter(account=fund.gl_account)
            .select_related("entry")
            .prefetch_related("entry__lines__account")
            .order_by("-entry__date", "-id")[:limit]
        )
        bank_ledgers = set(
            BankAccount.objects.filter(entity=fund.entity).values_list("gl_account_id", flat=True))
        try:
            over_short_id = resolve_mapped_account(
                fund.entity, AccountMappingKey.CASH_OVER_SHORT).pk
        except MissingAccountError:
            over_short_id = None
        running = fund.current_balance
        out = []
        for ln in lines:
            inflow, outflow = int(ln.debit or 0), int(ln.credit or 0)
            counter = self._counter_line(ln, fund)
            if counter is not None and counter.account_id == over_short_id:
                category = "Count over" if inflow else "Count short"
            elif counter is not None and counter.account_id in bank_ledgers and outflow:
                category = "Returned to bank"
            elif inflow:
                category = "Top-up"
            else:
                category = counter.account.name if counter else "-"
            out.append({
                "id": ln.id, "date": ln.entry.date,
                "description": ln.description or ln.entry.narration or "-",
                "category": category, "in": inflow, "out": outflow,
                "balance": int(running),
            })
            running -= (inflow - outflow)
        return out

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        import datetime

        _, fund = self._fund(request, pk)
        week_ago = branch_today(fund.entity.tenant, fund.branch_id) - datetime.timedelta(days=7)
        spent_week = sum(
            v.total for v in PettyCashVoucher.objects.filter(
                fund=fund, status=DocumentStatus.POSTED, voucher_date__gte=week_ago))
        data = PettyCashFundSerializer(fund).data
        data["spent_this_week"] = int(spent_week)
        data["register"] = self._register(fund)
        return success_response("Petty cash fund retrieved.", data=data)

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        """Edit the fund's name, custodian, float or activity, audited.

        An edit moves no cash, so it refuses (409) what only a return can do:
        lowering the float below the cash the fund holds, deactivating a fund that
        still holds cash, or changing a closed fund's float or activity
        (:func:`vs_finance.petty_cash.change_fund_details`).
        """
        from ..petty_cash import change_fund_details

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        changes = {}
        if "name" in body:
            changes["name"] = body["name"]
        if "custodian" in body:
            custodian = _resolve_user(body.get("custodian"), "custodian", entity)
            changes["custodian_id"] = custodian.pk if custodian else None
        if "custodian_name" in body:
            changes["custodian_name"] = body["custodian_name"]
        if "float_amount" in body:
            changes["float_amount"] = _money(body.get("float_amount", 0), "float_amount")
        if "is_active" in body:
            changes["is_active"] = _bool(body["is_active"])
        fund = change_fund_details(fund, actor_user=request.user, **changes)
        return success_response(
            "Petty cash fund updated.", data=PettyCashFundSerializer(fund).data,
        )


# Group endpoint behavior for Petty Cash Fund Establish View.
class PettyCashFundEstablishView(_PettyCashFundActionBase):
    """POST - move cash from a bank account into the tin (Dr petty cash, Cr bank).

    docstring-name: Establish a petty cash fund
    """

    rbac_permission = "finance.pettycash.establish"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..petty_cash import establish_fund

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(
            request, entity, body.get("bank_account"),
            document_branch=fund.branch_id, noun="petty cash fund")
        establish_fund(
            fund, bank_account=bank,
            amount=_money(body.get("amount"), "amount"),
            date=_date(body.get("date"), "date", required=True),
            actor_user=request.user,
        )
        fund.refresh_from_db()
        return success_response(
            f"Established cash into petty cash '{fund.name}'.",
            data=PettyCashFundSerializer(fund).data,
        )


# Group endpoint behavior for Petty Cash Fund Replenish View.
class PettyCashFundReplenishView(_PettyCashFundActionBase):
    """POST - top the tin back up to its float (Dr petty cash, Cr bank).

    docstring-name: Replenish a petty cash fund
    """

    rbac_permission = "finance.pettycash.replenish"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..petty_cash import replenish_fund

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(
            request, entity, body.get("bank_account"),
            document_branch=fund.branch_id, noun="petty cash fund")
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        replenish_fund(
            fund, bank_account=bank,
            date=_date(body.get("date"), "date", required=True),
            amount=amount, actor_user=request.user,
        )
        fund.refresh_from_db()
        return success_response(
            f"Replenished petty cash '{fund.name}'.",
            data=PettyCashFundSerializer(fund).data,
        )


def _return_branch_id(request, entity, fund):
    """The branch a return of ``fund`` is raised for: the fund's own.

    The caller must work in it (:func:`vs_rbac.scoping.inherited_branch_id`). A fund
    not yet given a branch is, at a tenant with one branch, that branch's; at a
    tenant with several nobody can say whose cash it holds, so it is refused (400)
    until it is given one (:func:`vs_finance.banking.money_branch_id`).
    """
    from ..banking import money_branch_id

    branch_id = _inherited_branch_id(request, fund)
    return branch_id if branch_id is not None else money_branch_id(entity, fund, field="fund")


class _PettyCashReturnRaiseBase(_PettyCashFundActionBase):
    """Count the tin and raise a return of its cash to the bank.

    The fund is one of the caller's own branches' (404 otherwise) and the return
    takes its branch. The bank account is one the caller can see (404 otherwise)
    and of the fund's own branch (400 otherwise). ``book_balance`` is read from the
    fund's ledger as the return is raised, so the count is held against the books
    of that moment. Approval follows the ``finance.petty_cash_return`` route
    exactly as a bank transaction follows its own: a route with steps holds it
    (201 with an ``approval`` block), an empty route needs
    ``confirm_without_approval``, and no route posts it at once.
    """

    kind = None

    def _amounts(self, body, fund, counted):
        """``(amount banked, new float)`` for this kind of return."""
        raise NotImplementedError

    def post(self, request, pk):
        from ..approvals import approval_required, confirm_unconfigured_post
        from ..petty_cash import (
            gl_cash_on_hand, post_petty_cash_return, validate_petty_cash_return,
        )

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        if body.get("counted_amount") in (None, ""):
            raise ValidationError({"counted_amount": "Count the tin and give the cash found, in kobo."})
        counted = _money(body.get("counted_amount"), "counted_amount")
        amount, new_float = self._amounts(body, fund, counted)
        bank = _resolve_bank_account(
            request, entity, body.get("bank_account"), required=amount > 0,
            document_branch=fund.branch_id, noun="petty cash fund",
        )
        return_date = _date(body.get("return_date"), "return_date", required=True)
        counted_by = _resolve_user(body.get("counted_by"), "counted_by", entity)
        reason = str(body.get("difference_reason") or "").strip()[:255]

        with transaction.atomic():
            fund = PettyCashFund.objects.select_for_update().get(pk=fund.pk)
            ret = PettyCashReturn(
                entity=entity, branch_id=_return_branch_id(request, entity, fund),
                fund=fund, kind=self.kind, bank_account=bank, return_date=return_date,
                counted_amount=counted, book_balance=gl_cash_on_hand(fund),
                amount=amount, previous_float_amount=fund.float_amount,
                new_float_amount=new_float,
                counted_by=counted_by or fund.custodian,
                difference_reason=reason,
                narration=str(body.get("narration") or "").strip()[:255],
                reference=str(body.get("reference") or "").strip()[:64],
                created_by=request.user,
            )
            if ret.difference and not reason:
                raise ValidationError({"difference_reason": (
                    f"The count found {format_naira(counted)} against "
                    f"{format_naira(ret.book_balance)} on the books. Say why they differ."
                )})
            validate_petty_cash_return(ret)
            ret.save()
            if approval_required(ret):
                from vs_workflow.services import release as release_svc
                from vs_workflow.services.submission import submit_for_approval

                instance = submit_for_approval(ret, requested_by=request.user)
                ret.refresh_from_db()
                return success_response(
                    message=(
                        f"Petty cash return {ret.document_number} is waiting for approval. "
                        f"It reaches the books once it is approved."
                    ),
                    data=PettyCashReturnSerializer(ret).data
                    | {"approval": release_svc.approval_block(instance)},
                    status=201,
                )
            confirm_unconfigured_post(ret, request, noun="petty cash return")
            post_petty_cash_return(ret, actor_user=request.user)
            ret.refresh_from_db()
            fund.refresh_from_db()
        return success_response(
            message=f"Petty cash return posted as {ret.document_number}.",
            data=PettyCashReturnSerializer(ret).data
            | {"fund": PettyCashFundSerializer(fund).data},
            status=201,
        )


class PettyCashFundReduceView(_PettyCashReturnRaiseBase):
    """POST /finance/petty-cash-funds/<id>/reduce/?entity= - cut the float and bank the excess.

    Body: ``counted_amount`` (the cash the custodian counted, kobo), ``new_float_amount``
    (lower than today's float, above zero), ``bank_account`` (id or name, the fund's
    own branch's), ``return_date``, and optionally ``amount`` (the cash banked; by
    default the count less the new float), ``counted_by`` (a user id; by default the
    custodian), ``difference_reason`` (required when the count differs from the
    books), ``narration`` and ``reference``. Posts ``Dr bank, Cr petty cash`` and any
    count difference to the cash over and short account.

    docstring-name: Reduce a petty cash float
    """

    rbac_permission = "finance.pettycash.return"
    kind = PettyCashReturnKind.REDUCE

    def _amounts(self, body, fund, counted):
        if body.get("new_float_amount") in (None, ""):
            raise ValidationError({"new_float_amount": "Give the float the fund runs on from now."})
        new_float = _money(body.get("new_float_amount"), "new_float_amount")
        if body.get("amount") not in (None, ""):
            return _money(body.get("amount"), "amount"), new_float
        return max(counted - new_float, 0), new_float


class PettyCashFundCloseView(_PettyCashReturnRaiseBase):
    """POST /finance/petty-cash-funds/<id>/close/?entity= - bank the whole tin and close the fund.

    Body: ``counted_amount``, ``return_date``, ``bank_account`` (required unless the
    count found nothing), and optionally ``counted_by``, ``difference_reason``
    (required when the count differs from the books), ``narration`` and
    ``reference``. Refused (409) while a voucher of the fund is a draft or waiting on
    approval, or another return of it is waiting on approval. Once posted the fund
    is CLOSED, with the return's date and the person who raised it, and takes no
    voucher, top-up or float change until reopened.

    docstring-name: Close a petty cash fund
    """

    rbac_permission = "finance.pettycash.close"
    kind = PettyCashReturnKind.CLOSE

    def _amounts(self, body, fund, counted):
        return counted, 0


class PettyCashFundReopenView(_PettyCashFundActionBase):
    """POST /finance/petty-cash-funds/<id>/reopen/?entity= - bring a closed fund back.

    Body: ``reason`` (required) and optionally ``float_amount``. Moves no cash: the
    fund comes back empty and is funded again with ``establish``. Audited under the
    fund's branch.

    docstring-name: Reopen a petty cash fund
    """

    rbac_permission = "finance.pettycash.reopen"

    def post(self, request, pk):
        from ..petty_cash import reopen_fund

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        float_amount = (
            _money(body.get("float_amount"), "float_amount")
            if body.get("float_amount") not in (None, "") else None
        )
        fund = reopen_fund(
            fund, reason=str(body.get("reason") or ""), float_amount=float_amount,
            actor_user=request.user,
        )
        return success_response(
            f"Petty cash fund '{fund.name}' reopened.",
            data=PettyCashFundSerializer(fund).data,
        )


def _returns_in_reach(request, entity):
    """Petty cash returns read as every transaction is: by their own branch, exclusively."""
    return PettyCashReturn.objects.filter(
        transaction_branch_q(request), entity=entity,
    ).select_related("fund", "bank_account", "branch", "counted_by", "created_by")


class PettyCashReturnListView(_FinanceBase):
    """GET /finance/petty-cash-returns/?entity= - returns of petty cash to the bank.

    Filters: ``fund``, ``kind`` (``REDUCE`` or ``CLOSE``), ``status`` and ``branch``
    (a branch id the caller works in; one they do not is refused exactly as an
    unknown one is, :func:`~vs_finance.views_ops.base._filter_by_branch`). Paginated.

    docstring-name: Petty cash returns
    """

    rbac_permission = "finance.pettycash.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _returns_in_reach(request, entity)
        if (fund := request.query_params.get("fund")) and str(fund).isdigit():
            qs = qs.filter(fund_id=int(fund))
        if (kind := request.query_params.get("kind")):
            qs = qs.filter(kind=str(kind).upper())
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        qs = _filter_by_branch(qs, request, entity)
        return self.paginate(request, qs.order_by("-return_date", "-id"), PettyCashReturnSerializer)


class PettyCashReturnDetailView(_FinanceBase):
    """GET /finance/petty-cash-returns/<id>/?entity= - one return.

    docstring-name: Petty cash returns
    """

    rbac_permission = "finance.pettycash.view"

    def get(self, request, pk):
        ret = _returns_in_reach(request, resolve_entity(request)).filter(pk=pk).first()
        if ret is None:
            raise NotFound("Petty cash return not found for this entity.")
        return success_response("Petty cash return retrieved.", data=PettyCashReturnSerializer(ret).data)


class PettyCashReturnVoidView(_FinanceBase):
    """POST /finance/petty-cash-returns/<id>/void/?entity= - undo a return.

    A posted return is reversed (optional body ``date`` dates the reversal) and the
    fund's cash and float come back, a closure reopening the fund; refused (409)
    while its bank line is matched to a statement line, after a later return of the
    fund, or once the fund has moved on. A draft left by a rejected approval is
    cancelled. The return must be one of the caller's own branches' (404 otherwise).

    docstring-name: Void a petty cash return
    """

    rbac_permission = "finance.pettycash.reverse"

    def post(self, request, pk):
        from ..petty_cash import void_petty_cash_return

        entity = resolve_entity(request)
        ret = _returns_in_reach(request, entity).filter(pk=pk).first()
        if ret is None:
            raise NotFound("Petty cash return not found for this entity.")
        void_petty_cash_return(
            ret, actor_user=request.user,
            date=_date((request.data or {}).get("date"), "date"),
        )
        ret.refresh_from_db()
        return success_response(
            f"Petty cash return {ret.document_number} voided.",
            data=PettyCashReturnSerializer(ret).data,
        )


class PettyCashReturnApprovalTemplateView(WholeTenantWriteMixin, _FinanceBase):
    """GET/POST /finance/petty-cash-returns/approval-template/?entity= - the ready-made route.

    GET shows the route a tenant may adopt (its step, the default shortage
    threshold and the approver group it names) and whether this tenant has a route
    for petty cash returns already; once it has, ``threshold`` is the shortage
    figure of the route as the tenant now has it, and the response counts the
    approver group's members. POST adopts it: optional body ``threshold``
    (kobo; above it a short count needs a second person, default ₦5,000). Until a
    tenant adopts it, no return of theirs is stopped
    (:func:`vs_finance.approvals.adopt_petty_cash_return_template`).

    A route binds every branch of the tenant, so adopting needs a caller who
    reaches the whole tenant (403 ``SHARED_RECORD_READ_ONLY`` otherwise), and the
    key that publishes any approval route, ``workflow.template.publish``. A route
    already holding a step is left as configured (200).

    docstring-name: Petty cash return approval route
    """

    shared_subject = "the tenant's approval routes"

    @property
    def rbac_permission(self):
        from vs_workflow.constants import PERM_TEMPLATE_PUBLISH, PERM_TEMPLATE_VIEW

        return PERM_TEMPLATE_PUBLISH if self.request.method == "POST" else PERM_TEMPLATE_VIEW

    @staticmethod
    def _payload(tenant, offered_threshold):
        """The route on offer, or the tenant's own once adopted.

        ``threshold`` is the figure that decides: the adopted route's own
        (:func:`~vs_finance.approvals.petty_cash_return_route_threshold`), which
        is ``None`` when the tenant has edited the shortage test out of it, and
        the offered figure before adoption. ``default_threshold`` is always the
        ready-made figure. ``approver_group_member_count`` counts the membership
        rows of the approver group, as the groups screen does, so a screen can
        say the group is empty only while it is.
        """
        from vs_workflow.models import WorkflowApproverGroup

        from ..approvals import (
            PETTY_CASH_RETURN_DOCUMENT_TYPE,
            PETTY_CASH_RETURN_TEMPLATE_NAME,
            petty_cash_return_route,
            petty_cash_return_route_threshold,
            petty_cash_return_stages,
        )
        from ..constants import (
            WF_DEFAULT_TEMPLATE_CODE,
            WF_PETTY_CASH_RETURN_APPROVER_GROUP,
            WF_PETTY_CASH_SHORTAGE_THRESHOLD,
        )

        route = petty_cash_return_route(tenant)
        adopted = bool(route and route.is_active
                       and route.stages.filter(retired_at__isnull=True).exists())
        threshold = petty_cash_return_route_threshold(route) if adopted else offered_threshold
        group = WorkflowApproverGroup.all_objects.filter(
            tenant=tenant, code=WF_PETTY_CASH_RETURN_APPROVER_GROUP,
        ).first()
        return {
            "document_type": PETTY_CASH_RETURN_DOCUMENT_TYPE,
            "code": WF_DEFAULT_TEMPLATE_CODE,
            "name": PETTY_CASH_RETURN_TEMPLATE_NAME,
            "threshold": threshold,
            "threshold_naira": format_naira(threshold) if threshold is not None else None,
            "default_threshold": WF_PETTY_CASH_SHORTAGE_THRESHOLD,
            "approver_group_code": WF_PETTY_CASH_RETURN_APPROVER_GROUP,
            "approver_group_id": group.pk if group else None,
            "approver_group_member_count": group.members.count() if group else 0,
            "stages": petty_cash_return_stages(
                threshold=offered_threshold,
                approver_group_code=WF_PETTY_CASH_RETURN_APPROVER_GROUP),
            "adopted": adopted,
            "route_id": route.pk if route else None,
        }

    def get(self, request):
        from ..constants import WF_PETTY_CASH_SHORTAGE_THRESHOLD

        entity = resolve_entity(request)
        return success_response(
            "Petty cash return approval route retrieved.",
            data=self._payload(entity.tenant, WF_PETTY_CASH_SHORTAGE_THRESHOLD),
        )

    def post(self, request):
        from ..approvals import adopt_petty_cash_return_template
        from ..audit import record
        from ..constants import FinanceAuditAction, WF_PETTY_CASH_SHORTAGE_THRESHOLD

        entity = resolve_entity(request)
        if entity.tenant_id is None:
            raise ValidationError({"entity": "These books belong to no tenant to route for."})
        body = request.data or {}
        threshold = (
            _money(body.get("threshold"), "threshold")
            if body.get("threshold") not in (None, "") else WF_PETTY_CASH_SHORTAGE_THRESHOLD
        )
        with transaction.atomic():
            template, created = adopt_petty_cash_return_template(
                entity.tenant, threshold=threshold, created_by=request.user)
            if created:
                record(
                    entity=entity, action=FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
                    actor_user=request.user, target_type="WorkflowTemplate",
                    target_id=str(template.pk), branch=None,
                    message=(
                        f"Adopted the ready-made petty cash return route: a count more than "
                        f"{format_naira(threshold)} short, or a closure, needs a second person."
                    ),
                    document_type=template.document_type, threshold=threshold,
                )
        if not created:
            return success_response(
                "This tenant already has its own petty cash return route; it is left as configured.",
                data=self._payload(entity.tenant, threshold),
            )
        return success_response(
            "Petty cash return approval route adopted. Put somebody in the approver group "
            "so the returns it stops can be approved.",
            data=self._payload(entity.tenant, threshold), status=201,
        )


# Group endpoint behavior for Petty Cash Status View.
class PettyCashStatusView(_FinanceBase):
    """GET - per-fund cash position + low-balance flags (replenishment alerts).

    docstring-name: Petty cash status
    """

    rbac_permission = "finance.pettycash.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..petty_cash import fund_status

        entity = resolve_entity(request)
        from ..banking_settings import resolve_finance_banking_settings

        policy = resolve_finance_banking_settings(entity)
        threshold = _int(
            request.query_params.get(
                "threshold_bps", policy.petty_cash_low_balance_threshold_bps,
            ),
            "threshold_bps", minimum=0, maximum=10000,
        )
        rows = fund_status(entity, threshold_bps=threshold)
        return success_response(
            "Petty cash status retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        **r,
                        "float_amount": {"kobo": r["float_amount"], "naira": format_naira(r["float_amount"])},
                        "current_balance": {"kobo": r["current_balance"], "naira": format_naira(r["current_balance"])},
                        "shortfall": {"kobo": r["shortfall"], "naira": format_naira(r["shortfall"])},
                        "last_replenished_at": str(r["last_replenished_at"]) if r["last_replenished_at"] else None,
                    }
                    for r in rows
                ],
            },
        )


# Group endpoint behavior for Petty Cash Voucher List Create View.
class PettyCashVoucherListCreateView(_FinanceBase):
    """GET (list) / POST (create draft + lines) petty-cash vouchers.

    docstring-name: Petty cash vouchers
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.pettycashvoucher.create" if self.request.method == "POST" \
            else "finance.pettycashvoucher.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = PettyCashVoucher.objects.filter(
            transaction_branch_q(request), entity=entity,
        ).prefetch_related("lines__expense_account")
        if (fund := request.query_params.get("fund")):
            qs = qs.filter(fund_id=fund)
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return self.paginate(
            request, qs.order_by("-voucher_date", "-id"), PettyCashVoucherSerializer)

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..petty_cash import price_voucher

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        fund_ref = body.get("fund")
        if fund_ref in (None, ""):
            raise ValidationError({"fund": "A petty cash fund is required."})
        fund = PettyCashFund.objects.filter(
            transaction_branch_q(request), entity=entity, pk=fund_ref,
        ).first()
        if fund is None:
            raise ValidationError({"fund": f"No petty cash fund '{fund_ref}' in this entity."})
        if fund.is_closed:
            raise ValidationError({"fund": (
                f"Petty cash fund '{fund.name}' was closed on {fund.closed_on.isoformat()}. "
                f"Reopen it first."
            )})
        voucher = PettyCashVoucher.objects.create(
            entity=entity, fund=fund,
            # A voucher takes its fund's branch.
            branch_id=_inherited_branch_id(request, fund),
            voucher_date=_date(body.get("voucher_date"), "voucher_date", required=True),
            payee=body.get("payee", ""),
            spent_by=_resolve_user(body.get("spent_by"), "spent_by", entity),
            narration=body.get("narration", ""),
            reference=body.get("reference", ""),
            currency=_resolve_currency(body.get("currency")) or fund.currency,
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PettyCashVoucherLine.objects.create(
                voucher=voucher, line_no=i,
                description=ln.get("description", ""),
                expense_account=_resolve_account(
                    request, entity, ln.get("expense_account"),
                    f"lines[{i}].expense_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(
                    entity, ln.get("tax_code"), f"lines[{i}].tax_code",
                    usage="purchase",
                ),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_voucher(voucher)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} created.",
            data=PettyCashVoucherSerializer(voucher).data, status=201,
        )


# Define Petty Cash Voucher Action Base values.
class _PettyCashVoucherActionBase(_FinanceBase):
    # Support the voucher workflow.
    def _voucher(self, request, pk):
        entity = resolve_entity(request)
        voucher = PettyCashVoucher.objects.filter(
            transaction_branch_q(request), entity=entity, pk=pk,
        ).first()
        if voucher is None:
            raise NotFound("Petty cash voucher not found for this entity.")
        return entity, voucher


# Group endpoint behavior for Petty Cash Voucher Detail View.
class PettyCashVoucherDetailView(_PettyCashVoucherActionBase):
    """docstring-name: Petty cash vouchers"""
    rbac_permission = "finance.pettycashvoucher.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, voucher = self._voucher(request, pk)
        return success_response(
            "Petty cash voucher retrieved.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


# Group endpoint behavior for Petty Cash Voucher Post View.
class PettyCashVoucherPostView(_PettyCashVoucherActionBase):
    """docstring-name: Post a petty cash voucher"""
    rbac_permission = "finance.pettycashvoucher.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..petty_cash import post_voucher

        _, voucher = self._voucher(request, pk)
        post_voucher(voucher, actor_user=request.user)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} posted.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


# Group endpoint behavior for Petty Cash Voucher Void View.
class PettyCashVoucherVoidView(_PettyCashVoucherActionBase):
    """POST - void a posted voucher (reverses its journal, returns the cash to the tin).

    docstring-name: Void a petty cash voucher
    """
    rbac_permission = "finance.pettycashvoucher.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..petty_cash import void_voucher

        _, voucher = self._voucher(request, pk)
        void_voucher(voucher, actor_user=request.user)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} voided.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


class PettyCashVoucherCancelView(_PettyCashVoucherActionBase):
    """POST - cancel a draft voucher that will never be paid. Writes no journal.

    Held by whoever may raise vouchers, since a draft has touched no ledger. A posted
    voucher is voided instead.

    docstring-name: Cancel a petty cash voucher
    """
    rbac_permission = "finance.pettycashvoucher.create"

    def post(self, request, pk):
        from ..petty_cash import cancel_voucher

        _, voucher = self._voucher(request, pk)
        cancel_voucher(voucher, actor_user=request.user)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} cancelled.",
            data=PettyCashVoucherSerializer(voucher).data,
        )
