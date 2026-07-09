"""Bank accounts, statement import, reconciliation.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from django.db import transaction  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..constants import BankLineStatus, DocumentStatus  # Import dependency used by this finance module.
from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    BankAccount,  # Finance processing step.
    BankStatementLine,  # Finance processing step.
    JournalLine,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    BankAccountSerializer,  # Finance processing step.
    BankReconciliationSerializer,  # Finance processing step.
    BankStatementLineSerializer,  # Finance processing step.
    BankStatementSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _bool,  # Finance processing step.
    _date,  # Finance processing step.
    _int,  # Finance processing step.
    _require_lines,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
    _signed_money,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Banking + reconciliation                                                    #
# --------------------------------------------------------------------------- #

class BankAccountListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) bank accounts for an entity.

    docstring-name: Bank accounts
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.bankaccount.create" if self.request.method == "POST" \
            else "finance.bankaccount.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = BankAccount.objects.filter(entity=entity).select_related("gl_account")  # Query finance data from the database.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Bank accounts retrieved.", data=BankAccountSerializer(qs, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A bank account name is required."})  # Surface validation or finance error.
        if BankAccount.objects.filter(entity=entity, name=name).exists():  # Branch when this finance condition is true.
            raise ValidationError({"name": f"A bank account named '{name}' already exists."})  # Surface validation or finance error.
        gl_account = _resolve_account(entity, body.get("gl_account"), "gl_account", required=True)  # Store intermediate finance value.
        is_primary = _bool(body.get("is_primary", False), default=False)  # Store intermediate finance value.
        is_primary_collection = _bool(body.get("is_primary_collection", False), default=False)  # Store intermediate finance value.
        currency = _resolve_currency(body.get("currency"))  # Store intermediate finance value.
        with transaction.atomic():  # Enter scoped finance context.
            if is_primary_collection:  # Branch when this finance condition is true.
                BankAccount.objects.filter(entity=entity, is_primary_collection=True).update(  # Query finance data from the database.
                    is_primary_collection=False)  # Store intermediate finance value.
            bank = BankAccount.objects.create(  # Query finance data from the database.
                entity=entity, name=name,  # Store intermediate finance value.
                bank_name=body.get("bank_name", ""),  # Store intermediate finance value.
                account_number=body.get("account_number", ""),  # Store intermediate finance value.
                gl_account=gl_account,  # Store intermediate finance value.
                currency=currency,  # Store intermediate finance value.
                is_active=_bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
                is_primary=is_primary,  # Store intermediate finance value.
                is_primary_collection=is_primary_collection,  # Store intermediate finance value.
            )  # Continue structured finance payload.
            if is_primary:  # at most one primary per entity
                BankAccount.objects.filter(entity=entity, is_primary=True).exclude(  # Query finance data from the database.
                    pk=bank.pk).update(is_primary=False)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Bank account '{name}' created.",  # Finance processing step.
            data=BankAccountSerializer(bank).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankAccountDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET one bank account (with metrics, transactions, statements, reconciliations)
    or PATCH its settings (name, bank, number, currency, active, primary).

    docstring-name: Bank accounts
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.bankaccount.update" if self.request.method == "PATCH" \
            else "finance.bankaccount.view"  # Finance processing step.

    def _bank(self, request, pk):  # Function handles this finance operation.
        bank = (BankAccount.objects.filter(entity=resolve_entity(request), pk=pk)  # Query finance data from the database.
                .select_related("gl_account").first())  # Finance processing step.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.
        return bank  # Return the computed finance response.

    def _transactions(self, bank, *, book_balance, limit=50):  # Function handles this finance operation.
        """Recent posted GL cash lines, newest first, with a running balance."""
        lines = list(  # Store intermediate finance value.
            JournalLine.objects  # Query finance data from the database.
            .filter(account=bank.gl_account, entry__status=DocumentStatus.POSTED)  # Store intermediate finance value.
            .select_related("entry")  # Finance processing step.
            .prefetch_related("bank_statement_lines")  # Finance processing step.
            .order_by("-entry__date", "-id")[:limit]  # Finance processing step.
        )  # Continue structured finance payload.
        running = book_balance  # Store intermediate finance value.
        out = []  # Store intermediate finance value.
        for ln in lines:  # Iterate through finance records.
            signed = (ln.debit or 0) - (ln.credit or 0)  # Store intermediate finance value.
            out.append({  # Finance processing step.
                "id": ln.id,  # Finance processing step.
                "date": ln.entry.date,  # Finance processing step.
                "description": ln.description or ln.entry.narration or "—",  # Finance processing step.
                "reference": ln.entry.document_number or ln.entry.reference or "",  # Finance processing step.
                "debit": int(ln.debit or 0),  # Finance processing step.
                "credit": int(ln.credit or 0),  # Finance processing step.
                "running_balance": int(running),  # Finance processing step.
                "matched": bool(ln.bank_statement_lines.all()),  # Finance processing step.
            })  # Continue structured finance payload.
            running -= signed  # Store intermediate finance value.
        return out  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        from ..banking import gl_account_balance, statement_balance  # Import dependency used by this finance module.

        bank = self._bank(request, pk)  # Store intermediate finance value.
        book = gl_account_balance(bank.gl_account)  # Store intermediate finance value.
        stmt = statement_balance(bank)  # Store intermediate finance value.
        stmt_val = stmt if stmt is not None else book  # Store intermediate finance value.
        unreconciled = bank.statement_lines.filter(status=BankLineStatus.UNMATCHED).count()  # Store intermediate finance value.
        data = BankAccountSerializer(bank).data  # Store intermediate finance value.
        data["metrics"] = {  # Store intermediate finance value.
            "book_balance": book, "statement_balance": stmt_val,  # Finance processing step.
            "unreconciled_diff": book - stmt_val, "unreconciled_count": unreconciled,  # Finance processing step.
        }  # Continue structured finance payload.
        data["transactions"] = self._transactions(bank, book_balance=book)  # Store intermediate finance value.
        data["statements"] = BankStatementSerializer(  # Store intermediate finance value.
            bank.statements.all()[:50], many=True).data  # Store intermediate finance value.
        data["reconciliations"] = BankReconciliationSerializer(  # Store intermediate finance value.
            bank.reconciliations.all()[:50], many=True).data  # Store intermediate finance value.
        return success_response("Bank account retrieved.", data=data)  # Return the computed finance response.

    def patch(self, request, pk):  # Function handles this finance operation.
        bank = self._bank(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if "name" in body:  # Branch when this finance condition is true.
            new_name = str(body["name"]).strip()  # Store intermediate finance value.
            if BankAccount.objects.filter(entity=bank.entity, name=new_name).exclude(pk=bank.pk).exists():  # Branch when this finance condition is true.
                raise ValidationError({"name": f"A bank account named '{new_name}' already exists."})  # Surface validation or finance error.
        for field in ("name", "bank_name", "account_number"):  # Iterate through finance records.
            if field in body:  # Branch when this finance condition is true.
                setattr(bank, field, str(body[field]).strip())  # Finance processing step.
        if "currency" in body:  # Branch when this finance condition is true.
            bank.currency = _resolve_currency(body.get("currency"))  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            bank.is_active = _bool(body.get("is_active"), default=bank.is_active)  # Store intermediate finance value.
        if "is_primary" in body:  # Branch when this finance condition is true.
            make_primary = _bool(body.get("is_primary"), default=bank.is_primary)  # Store intermediate finance value.
            bank.is_primary = make_primary  # Store intermediate finance value.
            if make_primary:  # at most one primary per entity
                BankAccount.objects.filter(entity=bank.entity, is_primary=True).exclude(  # Query finance data from the database.
                    pk=bank.pk).update(is_primary=False)  # Store intermediate finance value.
        if "is_primary_collection" in body:  # Branch when this finance condition is true.
            make_primary_collection = _bool(  # Store intermediate finance value.
                body.get("is_primary_collection"), default=bank.is_primary_collection)  # Store intermediate finance value.
            bank.is_primary_collection = make_primary_collection  # Store intermediate finance value.
            if make_primary_collection:  # Branch when this finance condition is true.
                with transaction.atomic():  # Enter scoped finance context.
                    BankAccount.objects.filter(  # Query finance data from the database.
                        entity=bank.entity, is_primary_collection=True).exclude(  # Store intermediate finance value.
                        pk=bank.pk).update(is_primary_collection=False)  # Store intermediate finance value.
                    bank.save()  # Finance processing step.
            else:  # Fallback finance branch.
                bank.save()  # Finance processing step.
        else:  # Fallback finance branch.
            bank.save()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Bank account '{bank.name}' updated.", data=BankAccountSerializer(bank).data)  # Store intermediate finance value.


class BankStatementLineView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) statement lines / POST import a batch of statement lines.

    docstring-name: Bank statement lines
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.bankaccount.import" if self.request.method == "POST" \
            else "finance.bankaccount.view"  # Finance processing step.

    def _bank(self, request, pk):  # Function handles this finance operation.
        bank = BankAccount.objects.filter(entity=resolve_entity(request), pk=pk).first()  # Query finance data from the database.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.
        return bank  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        bank = self._bank(request, pk)  # Store intermediate finance value.
        qs = (BankStatementLine.objects.filter(bank_account=bank)  # Query finance data from the database.
              .select_related("matched_line__entry", "adjusting_journal"))  # Finance processing step.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        return self.paginate(request, qs, BankStatementLineSerializer)  # Return the computed finance response.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import import_statement_lines  # Import dependency used by this finance module.

        bank = self._bank(request, pk)  # Store intermediate finance value.
        rows = _require_lines(request.data or {})  # Store intermediate finance value.
        parsed = []  # Store intermediate finance value.
        for i, row in enumerate(rows):  # Iterate through finance records.
            parsed.append({  # Finance processing step.
                "txn_date": _date(row.get("txn_date"), f"lines[{i}].txn_date", required=True),  # Store intermediate finance value.
                "amount": _signed_money(row.get("amount"), f"lines[{i}].amount"),  # Finance processing step.
                "description": row.get("description", ""),  # Finance processing step.
                "reference": row.get("reference", ""),  # Finance processing step.
                "external_id": row.get("external_id", ""),  # Finance processing step.
            })  # Continue structured finance payload.
        body = request.data or {}  # Store intermediate finance value.
        _, created, suspected = import_statement_lines(  # Store intermediate finance value.
            bank, parsed, actor_user=request.user,  # Store intermediate finance value.
            force=_bool(body.get("force", False), default=False),  # Store intermediate finance value.
            statement_date=_date(body.get("statement_date"), "statement_date"),  # Store intermediate finance value.
            period_label=str(body.get("period_label", "")).strip(),  # Store intermediate finance value.
            opening_balance=_signed_money(body.get("opening_balance", 0), "opening_balance"),  # Store intermediate finance value.
            closing_balance=(_signed_money(body.get("closing_balance"), "closing_balance")  # Store intermediate finance value.
                             if body.get("closing_balance") not in (None, "") else None),  # Branch when this finance condition is true.
        )  # Continue structured finance payload.
        message = f"Imported {len(created)} statement line(s)."  # Store intermediate finance value.
        if suspected:  # Branch when this finance condition is true.
            message += (f" {len(suspected)} suspected duplicate(s) held back — "  # Store intermediate finance value.
                        f"re-send with force=true to import them anyway.")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            message,  # Finance processing step.
            data={  # Store intermediate finance value.
                "imported": BankStatementLineSerializer(created, many=True).data,  # Store intermediate finance value.
                "suspected_duplicates": suspected,  # Finance processing step.
            },  # Continue structured finance payload.
            status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankAutoReconcileView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST — auto-match unmatched statement lines to posted cash journal lines.

    docstring-name: Auto-reconcile a bank statement
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import auto_reconcile  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        bank = BankAccount.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.
        body = request.data or {}  # Store intermediate finance value.
        tolerance = _int(body.get("tolerance_days", 4), "tolerance_days", minimum=0) or 4  # Store intermediate finance value.
        group = _bool(body.get("group", True), default=True)  # Store intermediate finance value.
        matched = auto_reconcile(  # Store intermediate finance value.
            bank, tolerance_days=tolerance, group=group, actor_user=request.user)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Auto-matched {len(matched)} statement line(s).",  # Finance processing step.
            data=BankStatementLineSerializer(matched, many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankBookLinesView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — posted cash-account journal lines not yet matched to a statement line.

    The "book" side of the reconciliation workbench. ``amount`` is signed kobo
    (debit − credit) so it lines up with a statement line's signed amount.

    docstring-name: Unmatched book lines
    """

    rbac_permission = "finance.bankaccount.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        from core.pagination import XVSPagination  # Import dependency used by this finance module.
        from ..banking import _unmatched_gl_lines  # Import dependency used by this finance module.
        from ..models import Customer  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()  # Query finance data from the database.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.

        # Posting bakes the customer *code* into line descriptions ("Receipt: CUST-002").
        # Resolve it to the human name for the reconciliation view.
        names = dict(Customer.objects.filter(entity=entity).values_list("code", "name"))  # Query finance data from the database.

        def humanize(desc: str) -> str:  # Function handles this finance operation.
            label, sep, tail = (desc or "").partition(": ")  # Store intermediate finance value.
            return f"{label}: {names[tail]}" if sep and tail in names else (desc or "—")  # Return the computed finance response.

        # Paginate the unmatched book lines (was capped at [:200]); build rows per page.
        paginator = XVSPagination()  # Store intermediate finance value.
        page = paginator.paginate_queryset(_unmatched_gl_lines(bank), request, view=self)  # Store intermediate finance value.
        rows = [{  # Store intermediate finance value.
            "id": ln.id,  # Finance processing step.
            "date": ln.entry.date,  # Finance processing step.
            "description": humanize(ln.description or ln.entry.narration or "—"),  # Finance processing step.
            "reference": ln.entry.document_number or ln.entry.reference or "",  # Finance processing step.
            "amount": int((ln.debit or 0) - (ln.credit or 0)),  # Finance processing step.
        } for ln in page]  # Continue structured finance payload.
        return paginator.get_paginated_response(rows)  # Return the computed finance response.


class BankReconcileCompleteView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST — finalise the reconciliation, recording a snapshot of the current state.

    docstring-name: Complete a bank reconciliation
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import complete_reconciliation  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()  # Query finance data from the database.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.
        recon = complete_reconciliation(bank, actor_user=request.user)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Reconciliation recorded.",  # Finance processing step.
            data=BankReconciliationSerializer(recon).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _StatementLineActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _line(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        line = (  # Store intermediate finance value.
            BankStatementLine.objects  # Query finance data from the database.
            .filter(pk=pk, bank_account__entity=entity)  # Store intermediate finance value.
            .select_related("bank_account").first()  # Finance processing step.
        )  # Continue structured finance payload.
        if line is None:  # Branch when this finance condition is true.
            raise NotFound("Statement line not found for this entity.")  # Surface validation or finance error.
        return entity, line  # Return the computed finance response.


class BankStatementLineMatchView(_StatementLineActionBase):  # Class groups related finance API or service behavior.
    """POST {journal_line} — manually pair a statement line to a cash journal line.

    docstring-name: Match a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import match_line  # Import dependency used by this finance module.

        entity, line = self._line(request, pk)  # Store intermediate finance value.
        ref = (request.data or {}).get("journal_line")  # Store intermediate finance value.
        if ref in (None, ""):  # Branch when this finance condition is true.
            raise ValidationError({"journal_line": "A journal line id is required."})  # Surface validation or finance error.
        jl = JournalLine.objects.filter(pk=ref, entry__entity=entity).first()  # Query finance data from the database.
        if jl is None:  # Branch when this finance condition is true.
            raise ValidationError({"journal_line": f"No journal line '{ref}' in this entity."})  # Surface validation or finance error.
        match_line(line, jl, actor_user=request.user)  # Store intermediate finance value.
        line.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Statement line matched.", data=BankStatementLineSerializer(line).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankStatementLineGroupMatchView(_StatementLineActionBase):  # Class groups related finance API or service behavior.
    """POST {journal_lines:[ids]} — match a statement line to several cash journal lines
    whose signed amounts sum to it (one settlement covering many receipts).

    docstring-name: Group-match a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import group_match  # Import dependency used by this finance module.

        entity, line = self._line(request, pk)  # Store intermediate finance value.
        ids = (request.data or {}).get("journal_lines") or []  # Store intermediate finance value.
        if not isinstance(ids, list) or len(ids) < 2:  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"journal_lines": "Provide a list of at least two journal line ids."})  # Continue structured finance payload.
        jls = list(  # Store intermediate finance value.
            JournalLine.objects.filter(pk__in=ids, entry__entity=entity).select_related("entry"))  # Query finance data from the database.
        missing = set(map(str, ids)) - {str(jl.id) for jl in jls}  # Store intermediate finance value.
        if missing:  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"journal_lines": f"Not found in this entity: {', '.join(sorted(missing))}."})  # Continue structured finance payload.
        group_match(line, jls, actor_user=request.user)  # Store intermediate finance value.
        line.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Statement line group-matched.",  # Finance processing step.
            data=BankStatementLineSerializer(line).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankSplitMatchView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST {journal_line, statement_lines:[ids]} — match one cash journal line to
    several statement lines that sum to it (one ledger movement the bank split).

    docstring-name: Split-match a cash journal line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import split_match  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()  # Query finance data from the database.
        if bank is None:  # Branch when this finance condition is true.
            raise NotFound("Bank account not found for this entity.")  # Surface validation or finance error.
        body = request.data or {}  # Store intermediate finance value.
        jl_ref = body.get("journal_line")  # Store intermediate finance value.
        jl = (JournalLine.objects.filter(pk=jl_ref, entry__entity=entity).select_related("entry").first()  # Query finance data from the database.
              if jl_ref not in (None, "") else None)  # Branch when this finance condition is true.
        if jl is None:  # Branch when this finance condition is true.
            raise ValidationError({"journal_line": "A valid journal line id is required."})  # Surface validation or finance error.
        ids = body.get("statement_lines") or []  # Store intermediate finance value.
        if not isinstance(ids, list) or len(ids) < 2:  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"statement_lines": "Provide a list of at least two statement line ids."})  # Continue structured finance payload.
        slines = list(  # Store intermediate finance value.
            BankStatementLine.objects.filter(pk__in=ids, bank_account=bank).select_related("bank_account"))  # Query finance data from the database.
        missing = set(map(str, ids)) - {str(s.id) for s in slines}  # Store intermediate finance value.
        if missing:  # Branch when this finance condition is true.
            raise ValidationError(  # Surface validation or finance error.
                {"statement_lines": f"Not found on this bank account: {', '.join(sorted(missing))}."})  # Continue structured finance payload.
        split_match(jl, slines, actor_user=request.user)  # Store intermediate finance value.
        rows = BankStatementLine.objects.filter(pk__in=[s.id for s in slines])  # Query finance data from the database.
        return success_response(  # Return the computed finance response.
            "Journal line split-matched.",  # Finance processing step.
            data=BankStatementLineSerializer(rows, many=True).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankStatementLineAdjustView(_StatementLineActionBase):  # Class groups related finance API or service behavior.
    """POST {counter_account?, counter_code?, narration?} — book + match an unrecorded line.

    docstring-name: Post an adjustment from a statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import post_bank_adjustment  # Import dependency used by this finance module.

        entity, line = self._line(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        counter = _resolve_account(entity, body.get("counter_account"), "counter_account")  # Store intermediate finance value.
        post_bank_adjustment(  # Finance processing step.
            line, counter_account=counter,  # Store intermediate finance value.
            counter_code=body.get("counter_code"),  # Store intermediate finance value.
            narration=body.get("narration", ""), actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        line.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Bank adjustment booked and line matched.",  # Finance processing step.
            data=BankStatementLineSerializer(line).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankStatementLineUnmatchView(_StatementLineActionBase):  # Class groups related finance API or service behavior.
    """POST — undo a match (reverses the adjusting journal if the match created one).

    docstring-name: Unmatch a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import unmatch_line  # Import dependency used by this finance module.

        _, line = self._line(request, pk)  # Store intermediate finance value.
        unmatch_line(line, actor_user=request.user)  # Store intermediate finance value.
        line.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Statement line unmatched.", data=BankStatementLineSerializer(line).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class BankStatementLineIgnoreView(_StatementLineActionBase):  # Class groups related finance API or service behavior.
    """POST {ignored?: true, reason?} — mark an unmatched line IGNORED (a known
    duplicate / opening-balance line), or revert it with ``ignored: false``.

    docstring-name: Ignore a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..banking import set_line_ignored  # Import dependency used by this finance module.

        _, line = self._line(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        set_line_ignored(  # Finance processing step.
            line, ignored=_bool(body.get("ignored", True), default=True),  # Store intermediate finance value.
            reason=body.get("reason", ""), actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        line.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Statement line updated.", data=BankStatementLineSerializer(line).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.
