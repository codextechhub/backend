"""Payslips and yearly tax summaries, and how they reach the employee.

**A payslip is issued when a line's pay leaves the bank** (:func:`issue_payslips`,
called by :func:`vs_finance.payroll.pay_payroll` for each run or branch share it
pays). Its content is always rendered from the payroll line
(:func:`payslip_context`), so it says exactly what the books say: the earnings
split, every deduction and employer contribution, the PAYE working, and the
year to date.

**Delivery is the tenant's choice** (:class:`~vs_finance.models.FinancePayrollSettings`):
shown in the app to the employee (their own payslips endpoint, plus an in-app
notice), and emailed to them with the PDF attached, by default; either can be
turned off. Both go through :mod:`vs_notifications` after the payment commits
(:func:`deliver_payslips`), in-app and email only. A person with no user account
or no email address gets no email, and the payslip says so.

**The yearly tax summary** (:func:`tax_summary`) lists a person's months of a
tax year from the posted and paid runs: gross, taxable pay, PAYE, pension, other
deductions and net, with the year's totals and the state and tax number the
PAYE was remitted under. It is what the person, and the revenue service, need
at the end of the year.

Who may open either is decided by the endpoints: the employee, for their own;
a payroll reader who may see every pay figure, for anybody's.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q

from .constants import (
    FinanceAuditAction,
    PayrollItemKind,
    PayrollRunStatus,
    PayslipEmailStatus,
)
from .money import format_naira

logger = logging.getLogger(__name__)

PAYSLIP_READY_EVENT = "payroll.payslip_ready"
PAYSLIP_EMAIL_EVENT = "payroll.payslip_emailed"

#: Runs whose lines count as paid or owed for a summary: accrued, not drafts.
_COUNTED = (PayrollRunStatus.POSTED, PayrollRunStatus.PAID)


def _issuer_name(entity) -> str:
    from .document_email import _issuer_name as issuer_name

    return issuer_name(entity)


def _person_lines(entity, line):
    """Every line of ``line``'s roster row on ``entity``'s counted runs.

    The row is the person whose year to date a payslip prints, counted exactly
    as the PAYE working counts it (:func:`vs_finance.payroll_statutory.year_to_date`):
    the lines naming the row, and any line of the same account written before
    lines named a row. A person has one active row, which follows them between
    branches, so the year counts every branch's months: Tunde, paid by Ikeja
    from January to March and by Lekki from April, has an April payslip whose
    year to date includes Ikeja's three months, the same figures his April PAYE
    was worked out on, whoever opens it.
    """
    from .models import PayrollLine

    person = Q(pk=line.pk)
    if line.salary_id:
        person |= Q(salary_id=line.salary_id)
    if line.employee_id:
        person |= Q(salary__isnull=True, employee_id=line.employee_id)
    return PayrollLine.objects.filter(person, run__entity=entity, run__run_status__in=_COUNTED)


def payslip_context(line) -> dict:
    """Everything a payslip shows, as formatted text and kobo figures."""
    run = line.run
    entity = run.entity
    tenant = entity.tenant
    from vs_config.display import format_date

    items = list(line.items.all())
    deductions = [i for i in items if i.kind == PayrollItemKind.DEDUCTION]
    employer = [i for i in items if i.kind == PayrollItemKind.EMPLOYER]
    year_lines = _person_lines(entity, line).filter(
        run__pay_date__year=run.pay_date.year, run__pay_date__lte=run.pay_date,
    )
    ytd = {"gross": 0, "paye": 0, "pension": 0, "net": 0}
    for other in year_lines.only("gross_amount", "paye_amount", "pension_amount", "net_amount"):
        ytd["gross"] += other.gross_amount
        ytd["paye"] += other.paye_amount
        ytd["pension"] += other.pension_amount
        ytd["net"] += other.net_amount

    def money(kobo):
        return format_naira(kobo)

    return {
        "issuer": _issuer_name(entity),
        "document_number": f"{run.document_number or run.pk}/{line.line_no}",
        "employee_name": line.employee_name,
        "period_label": run.period_label or format_date(run.pay_date, tenant, month="long"),
        "pay_date": format_date(run.pay_date, tenant),
        "branch": line.branch.name if line.branch_id else "",
        "tax_id": line.tax_id,
        "tax_state": line.tax_state.name if line.tax_state_id else "",
        "pfa": line.pfa.name if line.pfa_id else "",
        "pension_pin": line.pension_pin,
        "earnings": [
            {"name": c["name"], "amount": money(c["amount"])}
            for c in (line.components or []) if c.get("kind") == "EARNING"
        ],
        "deductions": [
            {"name": i.label or i.get_code_display(), "amount": money(i.amount)} for i in deductions
        ],
        "employer": [
            {"name": i.label or i.get_code_display(), "amount": money(i.amount)} for i in employer
        ],
        "gross": money(line.gross_amount),
        "total_deductions": money(line.gross_amount - line.net_amount),
        "net": money(line.net_amount),
        "paye_source": line.get_paye_source_display(),
        "tax_table": line.tax_table.name if line.tax_table_id else "",
        "ytd": {k: money(v) for k, v in ytd.items()},
        "figures": {
            "gross": line.gross_amount, "paye": line.paye_amount,
            "pension": line.pension_amount, "other_deductions": line.other_deductions_amount,
            "employer_contributions": line.employer_contributions_amount,
            "net": line.net_amount,
        },
    }


def payslip_pdf_bytes(line) -> bytes:
    from .pdf import payslip_pdf

    return payslip_pdf(payslip_context(line))


def tax_summary(entity, *, year, salary=None, employee=None) -> dict:
    """One person's tax year on ``entity``'s books, month by month.

    The person is a salary row (with any line of theirs written before lines
    named one, by user account) or a user account. Only posted and paid runs
    count: a draft has paid nobody.
    """
    from .models import PayrollLine

    qs = PayrollLine.objects.filter(
        run__entity=entity, run__run_status__in=_COUNTED, run__pay_date__year=year,
    )
    if salary is not None:
        person = Q(salary=salary)
        if salary.employee_id:
            person |= Q(salary__isnull=True, employee_id=salary.employee_id)
        qs = qs.filter(person)
        name = salary.name
    else:
        qs = qs.filter(employee=employee)
        name = " ".join(p for p in (employee.first_name, employee.last_name) if p) or employee.email
    lines = list(qs.select_related("run", "tax_state").order_by("run__pay_date", "id"))
    months, totals = [], {
        "gross": 0, "taxable_pay": 0, "paye": 0, "pension": 0, "other_deductions": 0, "net": 0,
    }
    for line in lines:
        row = {
            "pay_date": line.run.pay_date.isoformat(),
            "period_label": line.run.period_label,
            "run": line.run.document_number or str(line.run_id),
            "tax_state": line.tax_state.name if line.tax_state_id else "",
            "paye_source": line.paye_source,
            "gross": line.gross_amount, "taxable_pay": line.taxable_pay,
            "paye": line.paye_amount, "pension": line.pension_amount,
            "other_deductions": line.other_deductions_amount, "net": line.net_amount,
        }
        months.append(row)
        for key in totals:
            totals[key] += row[key]
    latest = lines[-1] if lines else None
    return {
        "year": int(year), "employee_name": name, "issuer": _issuer_name(entity),
        "entity": entity.code,
        "tax_id": latest.tax_id if latest else (salary.tax_id if salary is not None else ""),
        "tax_states": sorted({row["tax_state"] for row in months if row["tax_state"]}),
        "months": months, "totals": totals,
    }


def tax_summary_pdf_bytes(summary) -> bytes:
    from .pdf import tax_summary_pdf

    return tax_summary_pdf(summary)


# --------------------------------------------------------------------------- #
# Issue and deliver                                                           #
# --------------------------------------------------------------------------- #

def issue_payslips(run, lines, *, actor_user=None) -> list:
    """Create a payslip for each paid line and queue its delivery once the payment commits.

    Idempotent per line. Audited once per call under the branch the lines were
    paid in.
    """
    from .audit import record
    from .models import Payslip
    from .payroll_statutory import payroll_settings

    policy = payroll_settings(run.entity)
    created = []
    branch_id = None
    for line in lines:
        payslip, made = Payslip.objects.get_or_create(
            line=line,
            defaults={
                "entity": run.entity, "run": run, "salary_id": line.salary_id,
                "employee_id": line.employee_id, "branch_id": line.branch_id,
                "pay_date": run.pay_date, "period_label": run.period_label,
                "email_status": (
                    PayslipEmailStatus.PENDING if policy.payslip_email
                    else PayslipEmailStatus.NOT_REQUESTED
                ),
            },
        )
        if made:
            created.append(payslip)
            branch_id = line.branch_id
    if not created:
        return created
    record(
        entity=run.entity, action=FinanceAuditAction.PAYSLIPS_ISSUED, actor_user=actor_user,
        target=run, branch=branch_id,
        message=f"Issued {len(created)} payslip(s) for payroll run {run.document_number or run.pk}.",
        payslip_count=len(created),
    )
    if policy.payslip_in_app or policy.payslip_email:
        ids = [p.pk for p in created]
        transaction.on_commit(lambda: queue_payslip_delivery(ids))
    return created


def queue_payslip_delivery(payslip_ids) -> bool:
    """Hand delivery to the task queue; a broker that is down never undoes a payment."""
    from .tasks import deliver_payslips_task

    try:
        deliver_payslips_task.delay(list(payslip_ids))
        return True
    except Exception:
        logger.exception("Could not enqueue payslip delivery for %s", payslip_ids)
        return False


def deliver_payslips(payslip_ids) -> dict:
    """Send each payslip's in-app notice and email as the tenant's settings say.

    The notice names the person, the issuer and the period, and no figure. Its
    text comes from a template the tenant may edit and is kept, rendered, in a
    notification history read across every branch, so a pay figure offered to
    the template could be printed there for every member of staff. The pay
    travels only in the PDF attached to the employee's own email.

    The attached PDF is kept in storage under the payslip's
    ``email_attachment`` key for the email task to read; it is never served from
    there.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from vs_notifications.notify import send_notification

    from .models import Payslip
    from .models.payroll_statutory import payslip_attachment_path
    from .payroll_statutory import payroll_settings

    sent = {"in_app": 0, "email": 0}
    payslips = Payslip.objects.filter(pk__in=payslip_ids).select_related(
        "entity__tenant", "employee", "branch", "run", "line",
    )
    for payslip in payslips:
        policy = payroll_settings(payslip.entity)
        user = payslip.employee
        context = {
            "employee_name": payslip.line.employee_name,
            "issuer_name": _issuer_name(payslip.entity),
            "period_label": payslip.period_label or payslip.pay_date.isoformat(),
        }
        if policy.payslip_in_app and user is not None:
            try:
                send_notification(
                    event_key=PAYSLIP_READY_EVENT, context=context, recipients=[user],
                    tenant=payslip.entity.tenant, branch=payslip.branch,
                    metadata={"payslip_id": payslip.pk},
                )
                sent["in_app"] += 1
            except Exception:
                logger.exception("In-app payslip notice failed for payslip %s", payslip.pk)
        if not policy.payslip_email or payslip.email_status != PayslipEmailStatus.PENDING:
            continue
        if user is None or not getattr(user, "email", ""):
            payslip.email_status = PayslipEmailStatus.NO_ADDRESS
            payslip.save(update_fields=["email_status", "updated_at"])
            continue
        try:
            name = default_storage.save(
                payslip_attachment_path(payslip), ContentFile(payslip_pdf_bytes(payslip.line)),
            )
            ids = send_notification(
                event_key=PAYSLIP_EMAIL_EVENT, context=context, recipients=[user],
                tenant=payslip.entity.tenant, branch=payslip.branch,
                metadata={
                    "payslip_id": payslip.pk,
                    "attachments": [{
                        "name": f"payslip-{payslip.pay_date:%Y-%m}.pdf",
                        "storage_name": name, "content_type": "application/pdf",
                    }],
                },
            ) or []
        except Exception:
            logger.exception("Payslip email failed for payslip %s", payslip.pk)
            name, ids = "", []
        payslip.email_attachment = name
        payslip.notification_ids = [str(i) for i in ids]
        payslip.email_status = PayslipEmailStatus.QUEUED if ids else PayslipEmailStatus.FAILED
        payslip.save(update_fields=[
            "email_attachment", "notification_ids", "email_status", "updated_at",
        ])
        sent["email"] += 1 if ids else 0
    return sent
