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

**Only this employer's pay is this employer's.** A person who joined mid-year
has their previous employer's figures counted in their PAYE, and both
documents print them, apart, as brought forward; the year to date and the
totals are this employer's alone. A tenant that moved its payroll here
mid-year has its own earlier months counted the other way: they are its pay,
so they are inside the year to date, the totals and the annual return
(:func:`annual_paye_return`), and shown apart as brought forward from before
payroll ran here. No monthly remittance schedule carries them.

Who may open either is decided by the endpoints: the employee, for their own;
a payroll reader who may see every pay figure, for anybody's.
"""
from __future__ import annotations

from vs_finance.wording import counted

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


#: The figures of a previous employer's pay a payslip and tax summary print.
PAYSLIP_EARLIER_FIGURES = ("gross", "taxable_pay", "paye", "pension")


def brought_forward_of(line) -> dict | None:
    """The previous employer's figures ``line``'s PAYE was worked out on, or None.

    Read from the line's PAYE working, not from the record as it stands now,
    so a payslip explains the PAYE it shows: a correction made after April was
    paid changes May's working, and April's payslip still shows what April
    counted. A line whose PAYE was not computed, or that counted no earlier
    pay, has none.
    """
    working = (line.tax_basis or {}).get("brought_forward")
    if not working:
        return None
    return {
        "employer_name": working.get("employer_name", ""),
        **{k: int(working.get(k) or 0) for k in PAYSLIP_EARLIER_FIGURES},
    }


def opening_of(line) -> dict | None:
    """This employer's own pay from before its payroll ran here, as ``line`` recorded it, or None.

    Read from the line's working, where every generated line records it
    whatever the PAYE method, for the same reason as :func:`brought_forward_of`.
    """
    working = (line.tax_basis or {}).get("opening")
    if not working:
        return None
    return {
        "evidence_reference": working.get("evidence_reference", ""),
        **{k: int(working.get(k) or 0) for k in PAYSLIP_EARLIER_FIGURES},
    }


def payslip_context(line) -> dict:
    """Everything a payslip shows, as formatted text and kobo figures.

    ``ytd`` is this employer's year to date and nothing else's, so the payslip
    is honest about what this employer paid and deducted. For a tenant that
    moved its payroll here mid-year it includes the employer's own months from
    before (``opening``, :func:`opening_of`), which are shown again apart, as
    brought forward from before payroll ran here; ``ytd["net"]`` is the net
    paid through this payroll alone, since those months carry no net figure.
    A person who joined mid-year also has ``brought_forward``: the previous
    employer's figures their PAYE counted (:func:`brought_forward_of`),
    printed apart and never added to this employer's year to date.
    """
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
    opening = opening_of(line)
    if opening is not None:
        for key in ("gross", "paye", "pension"):
            ytd[key] += opening[key]

    def money(kobo):
        return format_naira(kobo)

    brought = brought_forward_of(line)
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
        "brought_forward": None if brought is None else {
            "employer_name": brought["employer_name"],
            **{k: money(brought[k]) for k in PAYSLIP_EARLIER_FIGURES},
        },
        "opening": None if opening is None else {
            k: money(opening[k]) for k in PAYSLIP_EARLIER_FIGURES
        },
        "figures": {
            "gross": line.gross_amount, "paye": line.paye_amount,
            "pension": line.pension_amount, "other_deductions": line.other_deductions_amount,
            "employer_contributions": line.employer_contributions_amount,
            "net": line.net_amount,
            "brought_forward": brought,
            "opening": opening,
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

    ``months`` are the months paid through this payroll. ``totals`` are this
    employer's year, the figures its annual return declares: those months plus
    its own months from before its payroll ran here (``opening``), whose gross,
    taxable pay, PAYE, pension and NHF (under other deductions) are added in;
    ``totals["net"]`` is the net paid through this payroll alone, since those
    months carry no net figure. Earlier pay from a previous employer is
    ``brought_forward``, never added to the totals: that employer declares it
    in its own returns. Both are read from the person's records as they stand,
    so the year-end figures carry every correction.
    """
    from .models import EmployeeSalary, PayrollLine

    qs = PayrollLine.objects.filter(
        run__entity=entity, run__run_status__in=_COUNTED, run__pay_date__year=year,
    )
    if salary is not None:
        person = Q(salary=salary)
        if salary.employee_id:
            person |= Q(salary__isnull=True, employee_id=salary.employee_id)
        qs = qs.filter(person)
        name = salary.name
        rows = [salary]
    else:
        qs = qs.filter(employee=employee)
        name = " ".join(p for p in (employee.first_name, employee.last_name) if p) or employee.email
        rows = list(EmployeeSalary.objects.filter(entity=entity, employee=employee))
    previous, opening = earlier_pay(rows, year)
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
    if opening is not None:
        for key in ("gross", "taxable_pay", "paye", "pension"):
            totals[key] += opening[key]
        totals["other_deductions"] += opening["nhf"]
    latest = lines[-1] if lines else None
    return {
        "year": int(year), "employee_name": name, "issuer": _issuer_name(entity),
        "entity": entity.code,
        "tax_id": latest.tax_id if latest else (salary.tax_id if salary is not None else ""),
        "tax_states": sorted({row["tax_state"] for row in months if row["tax_state"]}),
        "months": months, "totals": totals,
        "opening": opening, "brought_forward": previous,
    }


def earlier_pay(salaries, year) -> tuple:
    """``(previous employer, this employer before payroll ran here)`` of ``salaries`` for ``year``.

    Each is a dict of kobo figures summed over the rows given (a person has
    one row, but an older inactive one may hold a record too), with the
    previous employer's name and the evidence references, or None where
    neither row holds one.
    """
    from .constants import PayBroughtForwardSource
    from .models import PayBroughtForward

    out = {}
    for record in PayBroughtForward.objects.filter(
        salary__in=[s.pk for s in salaries], tax_year=year,
    ).order_by("pk"):
        sums = out.setdefault(record.source, {
            "gross": 0, "taxable_pay": 0, "paye": 0, "pension": 0, "nhf": 0,
            "employer_name": "", "evidence_reference": "",
        })
        for key, field in (("gross", "gross_amount"), ("taxable_pay", "taxable_pay"),
                           ("paye", "paye_amount"), ("pension", "pension_amount"),
                           ("nhf", "nhf_amount")):
            sums[key] += int(getattr(record, field) or 0)
        sums["employer_name"] = sums["employer_name"] or record.employer_name
        sums["evidence_reference"] = sums["evidence_reference"] or record.evidence_reference
    return (
        out.get(PayBroughtForwardSource.PREVIOUS_EMPLOYER),
        out.get(PayBroughtForwardSource.THIS_EMPLOYER),
    )


def annual_paye_return(entity, *, year, branch_ids=None) -> dict:
    """Each person's year on ``entity``'s books, for the employer's annual PAYE return.

    One row per person paid by a posted or paid run of ``year`` or holding pay
    of their own from before payroll ran here: gross, taxable pay, PAYE and
    pension, each including those earlier months of this employer (shown again
    under ``opening_*``), because they are this employer's pay and tax even
    though no run here paid them. A previous employer's figures are never in
    it: that employer files them. The monthly remittance schedules are
    different: they list only what a run here deducted, so a month this
    payroll did not run is in none of them.

    A person is told apart by the most reliable key their lines carry: user
    account, then salary record, then the name as typed
    (:func:`_return_person_key`). Each row names the ``employee_id`` and
    ``salary_id`` behind it where there is one, so two namesakes can be told
    apart on screen.

    ``branch_ids`` narrows to a branch-bound reader's branches: the months a
    run paid from those branches, and the earlier months of a person whose
    record a branch there owned at the end of the year.
    """
    import datetime

    from .constants import PayBroughtForwardSource
    from .models import EmployeeSalary, PayBroughtForward, PayrollLine

    lines = PayrollLine.objects.filter(
        run__entity=entity, run__run_status__in=_COUNTED, run__pay_date__year=year,
    ).select_related("tax_state", "salary")
    if branch_ids is not None:
        lines = lines.filter(branch_id__in=tuple(sorted(branch_ids)))
    people: dict = {}

    def person(key, name, tax_id, *, salary_id=None, employee_id=None):
        row = people.setdefault(key, {
            "salary_id": None, "employee_id": None,
            "employee_name": name, "tax_id": tax_id, "tax_states": set(),
            "gross": 0, "taxable_pay": 0, "paye": 0, "pension": 0, "months": 0,
            "opening_gross": 0, "opening_paye": 0,
        })
        row["salary_id"] = salary_id or row["salary_id"]
        row["employee_id"] = employee_id or row["employee_id"]
        return row

    for line in lines.order_by("run__pay_date", "id"):
        employee_id = line.employee_id or (line.salary.employee_id if line.salary_id else None)
        row = person(
            _return_person_key(employee_id, line.salary_id, line.employee_name, line.tax_id,
                               line_id=line.pk),
            line.employee_name, line.tax_id, salary_id=line.salary_id, employee_id=employee_id,
        )
        row["tax_id"] = line.tax_id or row["tax_id"]
        if line.tax_state_id:
            row["tax_states"].add(line.tax_state.name)
        row["gross"] += line.gross_amount
        row["taxable_pay"] += line.taxable_pay
        row["paye"] += line.paye_amount
        row["pension"] += line.pension_amount
        row["months"] += 1

    year_end = datetime.date(int(year), 12, 31)
    openings = PayBroughtForward.objects.filter(
        salary__entity=entity, tax_year=year, source=PayBroughtForwardSource.THIS_EMPLOYER,
    ).select_related("salary").prefetch_related("salary__versions")
    for record in openings:
        salary = record.salary
        if branch_ids is not None and salary.branch_on(year_end) not in branch_ids:
            continue
        row = person(
            _return_person_key(salary.employee_id, salary.pk, salary.name, salary.tax_id),
            salary.name, salary.tax_id, salary_id=salary.pk, employee_id=salary.employee_id,
        )
        row["gross"] += record.gross_amount
        row["taxable_pay"] += record.taxable_pay
        row["paye"] += record.paye_amount
        row["pension"] += record.pension_amount
        row["opening_gross"] += record.gross_amount
        row["opening_paye"] += record.paye_amount
    _fold_untaxed_namesakes(people)
    rows = sorted(people.values(), key=lambda r: (
        r["employee_name"], r["salary_id"] or 0, r["employee_id"] or 0, r["tax_id"]))
    for row in rows:
        row["tax_states"] = sorted(row["tax_states"])
    return {
        "year": int(year), "entity": entity.code, "issuer": _issuer_name(entity),
        "rows": rows,
        "totals": {k: sum(r[k] for r in rows) for k in (
            "gross", "taxable_pay", "paye", "pension", "opening_gross", "opening_paye",
        )},
    }


def _normalised(text, *, keep=str.isalnum) -> str:
    """``text`` compared loosely: case folded, and only the characters ``keep`` accepts."""
    return "".join(ch for ch in str(text or "").casefold() if keep(ch))


def _return_person_key(employee_id, salary_id, name, tax_id, *, line_id=None) -> tuple:
    """Who a payroll line (or opening record) belongs to on the annual return.

    The most reliable key a line carries decides, in this order:

    1. **The employee's user account.** One person has one account, and it
       outlives their salary records: lines written before lines named a salary
       row, and a person whose old salary row was replaced by a new one, all
       carry it. So it joins them, and keeps apart two people who share a name.
    2. **The salary record**, for a person on the roster with no user account.
    3. **The name as typed, with the tax number when one was typed**, for a
       hand-typed line naming neither. Nothing better identifies the person, so
       their months join by name, compared without case, spacing or punctuation
       ("  tunde   BAKARE " is Tunde Bakare). Two namesakes typed with different
       tax numbers stay apart; a month typed without the number is joined to the
       only namesake who has one by :func:`_fold_untaxed_namesakes`.

    A hand-typed line is never joined to a person identified by account or
    salary record, whatever the name: the same name is weaker evidence than a
    different key, and listing one person twice is a visible, correctable fault
    on a return, where merging two people misstates both. A line with no name
    and no tax number either is its own row (``line_id``): there is nothing to
    join it by.
    """
    if employee_id:
        return ("user", employee_id)
    if salary_id:
        return ("salary", salary_id)
    name, tax_id = _normalised(name, keep=str.isalpha), _normalised(tax_id)
    if not name and not tax_id:
        return ("line", line_id)
    return ("name", name, tax_id)


def _fold_untaxed_namesakes(people) -> None:
    """Join a hand-typed person's months typed without a tax number to their numbered ones.

    Only where exactly one hand-typed person of that name carries a tax number:
    with two (Kemi Ade, TIN-002 and TIN-003), nothing says which of them a
    number-less month was, so it stays a row of its own.
    """
    numbered = {}
    for key in people:
        if key[0] == "name" and key[2]:
            numbered.setdefault(key[1], []).append(key)
    for key in [k for k in people if k[0] == "name" and not k[2]]:
        owners = numbered.get(key[1], [])
        if len(owners) != 1:
            continue
        loose, row = people.pop(key), people[owners[0]]
        for field in ("gross", "taxable_pay", "paye", "pension", "months",
                      "opening_gross", "opening_paye"):
            row[field] += loose[field]
        row["tax_states"] |= loose["tax_states"]


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
        message=f"Issued {counted(len(created), 'payslip')} for payroll run {run.document_number or run.pk}.",
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
