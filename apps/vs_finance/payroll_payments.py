"""How a payroll run's payment through the payment provider is going.

For a tenant that pays staff through the provider (the payroll payment route
``PAYSTACK``, :class:`~vs_finance.constants.PayrollPaymentRoute`), a posted run
is paid line by line. :func:`payment_progress` reports it per branch and per
line: how many lines are sent, paid, failed, waiting for funds or need bank
details, and the money each branch's lines add up to, so Lekki's bursar sees
Lekki's staff and never Ikeja's.

A caller's Field Access decides what the report carries: the per-state counts
are a view of the payment status (``finance.payrollrun.pay_state``), the money
totals of net pay (``finance.payrollrun.net_amount``), and the lines themselves
are rendered by :class:`~vs_finance.serializers.PayrollLinePaymentSerializer`
behind the same switches. The run's route is not a person's field and is always
shown. ``approved`` says whether the run has an approved request, the evidence
paying through the provider needs (:func:`vs_finance.approvals.payroll_run_approved`):
it belongs to the payment status (``finance.payrollrun.pay_state``) and not to
the approval route's state (``finance.payrollrun.approval``), so a reader who
follows how a run is being paid is told whether it may be paid this way even
when they may not read where it stands with its approvers.
"""
from __future__ import annotations

from .constants import PayrollLinePayState

#: The counts every branch and the run report, by the word the screen uses.
COUNT_KEYS = {
    "": "not_sent",
    PayrollLinePayState.NEEDS_BANK_DETAILS: "needs_bank_details",
    PayrollLinePayState.WAITING_FOR_FUNDS: "waiting_for_funds",
    PayrollLinePayState.SENT: "sent",
    PayrollLinePayState.PAID: "paid",
    PayrollLinePayState.FAILED: "failed",
}


def _empty_counts() -> dict:
    return {key: 0 for key in COUNT_KEYS.values()}


def payment_progress(run, request, *, reach=None) -> dict:
    """The payment progress of ``run`` for ``request``'s caller, narrowed to ``reach``.

    ``reach`` is the caller's branch ids (``None`` for the whole tenant). The
    lines of branches outside it are left out of every figure.
    """
    from vs_rbac.field_enforcement import can_read

    from .approvals import payroll_run_approved
    from .models import PayrollLine
    from .payroll import payment_route
    from .serializers import PayrollLinePaymentSerializer

    lines = list(
        PayrollLine.objects.filter(run=run).select_related("branch")
        .order_by("branch__name", "line_no", "id"))
    if reach is not None:
        lines = [line for line in lines if line.branch_id in reach]
    sees_state = can_read(request, "finance.payrollrun.pay_state")
    sees_money = can_read(request, "finance.payrollrun.net_amount")

    branches: dict = {}
    for line in lines:
        row = branches.setdefault(line.branch_id, {
            "branch_id": line.branch_id,
            "branch_name": line.branch.name if line.branch_id else None,
            "lines": 0, "counts": _empty_counts(), "net_total": 0, "paid_total": 0,
        })
        row["lines"] += 1
        row["counts"][COUNT_KEYS[line.pay_state]] += 1
        row["net_total"] += int(line.net_amount)
        if line.pay_state == PayrollLinePayState.PAID:
            row["paid_total"] += int(line.net_amount)
    batches = _latest_batches(run)
    for branch_id, row in branches.items():
        row["batch"] = batches.get(branch_id)
        if not sees_state:
            row.pop("counts")
            row.pop("batch")
        if not sees_money:
            row.pop("net_total")
            row.pop("paid_total")

    totals = _empty_counts()
    for row in branches.values():
        for key, value in row.get("counts", {}).items():
            totals[key] += value
    data = {
        "run_id": run.pk,
        "run_status": run.run_status,
        "payment_route": payment_route(run.entity),
        "branches": list(branches.values()),
        "lines": PayrollLinePaymentSerializer(
            lines, many=True, context={"request": request}).data,
    }
    if sees_state:
        data["counts"] = totals
        data["approved"] = payroll_run_approved(run)
    return data


def _latest_batches(run) -> dict:
    """Each branch's latest payroll batch for ``run`` as ``{reference, status}`` (imported from ``vs_payments``)."""
    from vs_payments.constants import PayoutPurpose
    from vs_payments.models import PayoutBatch

    out: dict = {}
    for batch in (PayoutBatch.objects.filter(
            entity=run.entity, purpose=PayoutPurpose.PAYROLL, metadata__payroll_run_id=run.pk)
            .order_by("pk")):
        out[batch.branch_id] = {"reference": batch.reference, "status": batch.status}
    return out
