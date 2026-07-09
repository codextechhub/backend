"""Expense claims.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from django.db import transaction  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    ExpenseClaim,  # Finance processing step.
    ExpenseClaimLine,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    ExpenseClaimSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _date,  # Finance processing step.
    _dec,  # Finance processing step.
    _money,  # Finance processing step.
    _require_lines,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
    _resolve_tax,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

class ExpenseClaimListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) expense claims for an entity.

    docstring-name: Expense claims
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.expenseclaim.create" if self.request.method == "POST" \
            else "finance.expenseclaim.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        from ..constants import DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = ExpenseClaim.objects.filter(entity=entity).prefetch_related("lines")  # Query finance data from the database.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (pay := request.query_params.get("payment_status")):  # Branch when this finance condition is true.
            qs = qs.filter(payment_status=pay)  # Store intermediate finance value.
        # The UI collapses (status × payment_status) into display states; translate
        # them to the underlying filters so the list stays filtered server-side.
        disp = request.query_params.get("display_status")  # Store intermediate finance value.
        if disp == "DRAFT":  # Branch when this finance condition is true.
            qs = qs.filter(status=DocumentStatus.DRAFT)  # Store intermediate finance value.
        elif disp == "REJECTED":  # Alternative finance branch.
            qs = qs.filter(status=DocumentStatus.CANCELLED)  # Store intermediate finance value.
        elif disp == "PAID":  # Alternative finance branch.
            qs = qs.filter(status=DocumentStatus.POSTED,  # Store intermediate finance value.
                           payment_status=InvoicePaymentStatus.PAID)  # Store intermediate finance value.
        elif disp == "APPROVED":  # posted but not yet fully reimbursed
            qs = qs.filter(status=DocumentStatus.POSTED).exclude(  # Store intermediate finance value.
                payment_status=InvoicePaymentStatus.PAID)  # Store intermediate finance value.
        if (search := request.query_params.get("q")):  # Branch when this finance condition is true.
            from django.db.models import Q  # Import dependency used by this finance module.
            qs = qs.filter(  # Store intermediate finance value.
                Q(claimant_name__icontains=search)  # Store intermediate finance value.
                | Q(title__icontains=search)  # Store intermediate finance value.
                | Q(document_number__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        return self.paginate(  # Return the computed finance response.
            request, qs.order_by("-claim_date", "-id"), ExpenseClaimSerializer)  # Finance processing step.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from ..expenses import price_expense_claim  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        lines = _require_lines(body)  # Store intermediate finance value.
        claim = ExpenseClaim.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            claimant_name=body.get("claimant_name", ""),  # Store intermediate finance value.
            claim_date=_date(body.get("claim_date"), "claim_date", required=True),  # Store intermediate finance value.
            title=body.get("title", ""),  # Store intermediate finance value.
            narration=body.get("narration", ""),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for i, ln in enumerate(lines, start=1):  # Iterate through finance records.
            ExpenseClaimLine.objects.create(  # Query finance data from the database.
                claim=claim, line_no=i,  # Store intermediate finance value.
                description=ln.get("description", ""),  # Store intermediate finance value.
                expense_account=_resolve_account(  # Store intermediate finance value.
                    entity, ln.get("expense_account"),  # Finance processing step.
                    f"lines[{i}].expense_account", required=True),  # Store intermediate finance value.
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),  # Store intermediate finance value.
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),  # Store intermediate finance value.
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),  # Store intermediate finance value.
                cost_center=_resolve_cost_center(  # Store intermediate finance value.
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
            )  # Continue structured finance payload.
        price_expense_claim(claim)  # Finance processing step.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Expense claim {claim.document_number} created.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _ExpenseClaimActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _claim(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        claim = ExpenseClaim.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if claim is None:  # Branch when this finance condition is true.
            raise NotFound("Expense claim not found for this entity.")  # Surface validation or finance error.
        return entity, claim  # Return the computed finance response.


class ExpenseClaimDetailView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Expense claims"""
    rbac_permission = "finance.expenseclaim.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, claim = self._claim(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Expense claim retrieved.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimPostView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Post an expense claim"""
    rbac_permission = "finance.expenseclaim.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..expenses import post_expense_claim  # Import dependency used by this finance module.

        _, claim = self._claim(request, pk)  # Store intermediate finance value.
        post_expense_claim(claim, actor_user=request.user)  # Store intermediate finance value.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Expense claim {claim.document_number} posted.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimRejectView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """POST — reject (cancel) a draft expense claim. docstring-name: Reject an expense claim"""
    rbac_permission = "finance.expenseclaim.post"  # the approver decides approve OR reject

    def post(self, request, pk):  # Function handles this finance operation.
        from ..constants import DocumentStatus  # Import dependency used by this finance module.
        from rest_framework.exceptions import ValidationError  # Import dependency used by this finance module.

        _, claim = self._claim(request, pk)  # Store intermediate finance value.
        if claim.status != DocumentStatus.DRAFT:  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"status": f"Only a draft claim can be rejected (this is '{claim.status}')."})  # Continue structured finance payload.
        claim.status = DocumentStatus.CANCELLED  # Store intermediate finance value.
        claim.save(update_fields=["status", "updated_at"])  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Expense claim {claim.document_number} rejected.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimReceiptView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """POST (multipart ``file``) attach / DELETE a receipt on a claim line.

    docstring-name: Expense line receipt
    """

    rbac_permission = "finance.expenseclaim.create"  # Store intermediate finance value.

    def _line(self, request, pk, line_id):  # Function handles this finance operation.
        _, claim = self._claim(request, pk)  # Store intermediate finance value.
        line = claim.lines.filter(pk=line_id).first()  # Store intermediate finance value.
        if line is None:  # Branch when this finance condition is true.
            raise NotFound("Line not found on this claim.")  # Surface validation or finance error.
        return claim, line  # Return the computed finance response.

    def post(self, request, pk, line_id):  # Function handles this finance operation.
        from rest_framework.exceptions import ValidationError  # Import dependency used by this finance module.

        claim, line = self._line(request, pk, line_id)  # Store intermediate finance value.
        upload = request.FILES.get("file")  # Store intermediate finance value.
        if upload is None:  # Branch when this finance condition is true.
            raise ValidationError({"file": "A receipt file is required."})  # Surface validation or finance error.
        line.receipt.save(upload.name, upload, save=True)  # Store intermediate finance value.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Receipt attached.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def delete(self, request, pk, line_id):  # Function handles this finance operation.
        claim, line = self._line(request, pk, line_id)  # Store intermediate finance value.
        if line.receipt:  # Branch when this finance condition is true.
            line.receipt.delete(save=True)  # Store intermediate finance value.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Receipt removed.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimSettleView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Settle an expense claim"""
    rbac_permission = "finance.expenseclaim.settle"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..expenses import settle_expense_claim  # Import dependency used by this finance module.

        entity, claim = self._claim(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"))  # Store intermediate finance value.
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None  # Store intermediate finance value.
        settle_expense_claim(  # Finance processing step.
            claim, bank_account=bank,  # Store intermediate finance value.
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),  # Store intermediate finance value.
            amount=amount, actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Expense claim {claim.document_number} reimbursed.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimVoidView(_ExpenseClaimActionBase):  # Class groups related finance API or service behavior.
    """POST — void a posted, un-reimbursed claim (reverses its journal, marks CANCELLED).

    docstring-name: Void an expense claim
    """
    rbac_permission = "finance.expenseclaim.post"  # the approver undoes their approval

    def post(self, request, pk):  # Function handles this finance operation.
        from ..expenses import void_expense_claim  # Import dependency used by this finance module.

        _, claim = self._claim(request, pk)  # Store intermediate finance value.
        void_expense_claim(claim, actor_user=request.user)  # Store intermediate finance value.
        claim.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Expense claim {claim.document_number} voided.",  # Finance processing step.
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ExpenseClaimSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — header KPIs over **all** expense claims (accurate under pagination).

    docstring-name: Expense claims
    """

    rbac_permission = "finance.expenseclaim.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Count, Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.
        from django.utils import timezone  # Import dependency used by this finance module.

        from ..constants import DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        today = timezone.now().date()  # Store intermediate finance value.
        live = ~Q(status=DocumentStatus.CANCELLED)  # Store intermediate finance value.
        awaiting_q = Q(status=DocumentStatus.POSTED) & ~Q(  # Store intermediate finance value.
            payment_status=InvoicePaymentStatus.PAID)  # Store intermediate finance value.

        agg = ExpenseClaim.objects.filter(entity=entity).aggregate(  # Query finance data from the database.
            open=Count("id", filter=Q(status=DocumentStatus.DRAFT) | awaiting_q),  # Store intermediate finance value.
            month_total=Coalesce(Sum("total", filter=live & Q(  # Store intermediate finance value.
                claim_date__year=today.year, claim_date__month=today.month)), 0),  # Store intermediate finance value.
            live_total=Coalesce(Sum("total", filter=live), 0),  # Store intermediate finance value.
            live_count=Count("id", filter=live),  # Store intermediate finance value.
            awaiting_total=Coalesce(Sum("total", filter=awaiting_q), 0),  # Store intermediate finance value.
            awaiting_paid=Coalesce(Sum("amount_paid", filter=awaiting_q), 0),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        avg = agg["live_total"] // agg["live_count"] if agg["live_count"] else 0  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Expense claim summary retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "open": agg["open"],  # Finance processing step.
                "month_total": agg["month_total"],  # Finance processing step.
                "avg": avg,  # Finance processing step.
                "awaiting": agg["awaiting_total"] - agg["awaiting_paid"],  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


