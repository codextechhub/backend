"""Moving a pupil to another branch: the choices, a preview, and the move.

All three are held to ``school.students.change_branch``, and every one finds
the pupil through the caller's own branches first, so a pupil outside them
answers 404 as everywhere in this module. The rules of the move itself live in
:mod:`schools.vs_students.services.branch_move`.

**The money in a move is shown to finance readers only.** A move carries a
pupil's open bills, credit and unearned fees between two branches' books, and
the figures are a family's debt. A caller who also holds
``finance.invoice.view`` sees them, bill by bill; anybody else who may move the
pupil is told how many fee accounts and bills move, and no amount.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_config.clock import branch_today

from ..constants import PERM_CHANGE_BRANCH, PERM_CLASS_ASSIGN
from ..serializers import BranchMovePreviewSerializer, BranchMoveSerializer
from ..services import branch_move as service
from .base import StudentsViewMixin

#: The finance key that reads the bills a move carries.
FINANCE_FIGURES_KEY = "finance.invoice.view"


def _branch_or_400(tenant, raw, field="to_branch"):
    """The branch an id names in this school, or a 400 that reveals nothing else."""
    from vs_tenants.references import resolve_branch_reference

    from rest_framework.exceptions import ValidationError

    branch = resolve_branch_reference(tenant, raw, field)
    if branch is None:
        raise ValidationError({field: "Choose the branch the pupil moves to."})
    return branch


def _account_row(account, *, figures):
    """One fee account's part of a move, with or without its money."""
    row = {
        "name": account.name,
        "from_branch": account.from_branch_ref,
        "from_branch_name": account.from_branch,
        "to_branch": account.to_branch_ref,
        "to_branch_name": account.to_branch,
        "invoice_count": account.invoice_count,
        "debit_note_count": account.debit_note_count,
        "transfer": account.transfer_ref,
        "transfer_number": account.transfer_number,
    }
    if not figures:
        return row
    row.update({
        "amount": account.amount,
        "owed_amount": account.owed_amount,
        "credit_amount": account.credit_amount,
        "deferred_amount": account.deferred_amount,
        "inter_branch_amount": account.inter_branch_amount,
        "bills": [
            {
                "kind": bill.kind, "number": bill.number,
                "amount": bill.amount, "deferred_amount": bill.deferred_amount,
            }
            for bill in account.bills
        ],
    })
    return row


def move_payload(result, *, figures):
    """The answer of a preview and of a move, in one shape.

    ``totals`` sums every account, and holds the four figures the form shows:
    the open bills the new branch now collects, the credit and the unearned
    fees it takes over, and what it owes the old branch for the income the old
    branch already earned (negative when the old branch owes the new one).
    Without ``figures`` the money keys are absent rather than zero, so no
    screen can mistake a withheld balance for a clear one.
    """
    accounts = [_account_row(a, figures=figures) for a in result.accounts]
    payload = {
        "move": getattr(result.move, "pk", None),
        "from_branch": result.from_branch.pk,
        "from_branch_name": result.from_branch.name,
        "to_branch": result.to_branch.pk,
        "to_branch_name": result.to_branch.name,
        "effective_date": result.effective_date,
        "school_class": getattr(result.school_class, "pk", None),
        "school_class_name": getattr(result.school_class, "name", None),
        "over_capacity": result.over_capacity,
        "figures_shown": figures,
        "accounts": accounts,
    }
    if figures:
        payload["totals"] = {
            key: sum(getattr(a, key) for a in result.accounts)
            for key in (
                "owed_amount", "credit_amount", "deferred_amount",
                "inter_branch_amount", "amount",
            )
        }
    return payload


class _BranchMoveBase(StudentsViewMixin, APIView):
    def get_permissions(self):
        self.rbac_permission = PERM_CHANGE_BRANCH
        return super().get_permissions()

    def figures_shown(self):
        from vs_rbac.permissions import has_permission, is_vision_super_admin

        user = self.request.user
        return is_vision_super_admin(user) or has_permission(
            user, FINANCE_FIGURES_KEY, tenant=self.tenant,
        )


class BranchMoveView(_BranchMoveBase):
    """GET and POST /v1/students/<id>/move-branch/

    GET answers what the move form needs: the pupil's branch and class, the
    branches this caller may move them to (each with its own today, which is
    the default day of the move), and whether a class must be chosen. An empty
    ``branches`` is a real answer: the school has one branch, or the caller
    works only at the pupil's.

    POST moves the pupil. The body is ``{"to_branch", "school_class"?,
    "effective_date"?, "reason", "allow_over_capacity"?}``. Refusals: 409
    ``ONE_BRANCH``, 409 ``ALREADY_AT_BRANCH``, 422 ``NOT_ON_ROLL``,
    ``BRANCH_NOT_OPEN``, ``CLASS_REQUIRED``, ``INVALID_EFFECTIVE_DATE``,
    ``BRANCH_SCOPE_CONFLICT``, the class move's capacity refusals, 403 for a
    caller who does not work at both branches, 400 for a branch of another
    school, the finance refusals (409 ``PERIOD_CLOSED`` for a closed month at
    either branch) and 503 ``FINANCE_UNAVAILABLE``. A refused move changes
    nothing.

    Naming a class also needs ``academics.classes.assign``, as every other
    placement does.

    docstring-name: Move a pupil to another branch
    """

    def get(self, request, pk):
        student = self.student(pk)
        _, current = service.current_placement(student)
        targets = service.move_targets(student, request.user)
        return success_response(data={
            "branch": student.branch_id,
            "branch_name": student.branch.name,
            "status": student.status,
            "school_class": getattr(current, "school_class_id", None),
            "school_class_name": (
                current.school_class.name if current is not None else None
            ),
            "class_is_shared": bool(
                current is not None and current.school_class.branch_id is None
            ),
            "needs_class": bool(
                current is not None and current.school_class.branch_id is not None
            ),
            "figures_shown": self.figures_shown(),
            "branches": [
                {
                    "id": branch.pk, "name": branch.name,
                    "today": branch_today(self.tenant, branch.pk),
                }
                for branch in targets
            ],
        })

    def post(self, request, pk):
        writer = BranchMoveSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        student = self.student(pk)
        to_branch = _branch_or_400(self.tenant, data["to_branch"])
        if data.get("school_class"):
            self.assert_holds(PERM_CHANGE_BRANCH, PERM_CLASS_ASSIGN)
        result = service.move_to_branch(
            student, to_branch, actor=request.user, reason=data["reason"],
            effective_date=data.get("effective_date"),
            school_class_id=data.get("school_class"),
            allow_over_capacity=data.get("allow_over_capacity", False),
        )
        message = f"{student.full_name} now attends {to_branch.name}."
        if result.accounts:
            message += " Their fee account moved with them."
        if result.over_capacity:
            message += " The class is now over capacity."
        return success_response(
            message, data=move_payload(result, figures=self.figures_shown()),
        )


class BranchMovePreviewView(_BranchMoveBase):
    """POST /v1/students/<id>/move-branch/preview/

    What the move would carry, with nothing moved: ``{"to_branch",
    "effective_date"?}``. The finance move is run for real and rolled back, so
    the figures are the ones the move books, and a refusal it would meet is
    met here with the same code. A preview names no transfer.

    docstring-name: Preview a pupil's move to another branch
    """

    def post(self, request, pk):
        reader = BranchMovePreviewSerializer(data=request.data)
        reader.is_valid(raise_exception=True)
        data = reader.validated_data
        student = self.student(pk)
        to_branch = _branch_or_400(self.tenant, data["to_branch"])
        result = service.preview_move(
            student, to_branch, user=request.user,
            effective_date=data.get("effective_date"),
        )
        return success_response(data=move_payload(result, figures=self.figures_shown()))
