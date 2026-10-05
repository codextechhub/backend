"""Payroll runs.
"""
from __future__ import annotations

import datetime
from collections.abc import Mapping

from django.db import transaction
from rest_framework.exceptions import NotFound

from core.response import success_response

from rest_framework.exceptions import ValidationError

from django.db.models import Count, Q
from vs_rbac.scoping import transaction_branch_q
from vs_rbac.scoping import (
    assert_caller_may_change,
    caller_branch_ids,
    caller_may_use_branch,
    shared_write_refusal,
)
from vs_rbac.scoping import resolve_branch as _resolve_branch
from vs_tenants.references import BRANCH_NOT_FOUND

from ..constants import SalaryCalcMethod, SalaryComponentKind, StatutoryType
from ..views import resolve_entity
from ..models import (
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
    SalaryComponent,
    SalaryStructure,
)
from ..serializers import (
    EmployeeSalarySerializer,
    PayrollRunSerializer,
    SalaryStructureSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _filter_by_branch,
    _date,
    _money,
    _raised_branch,
    _bank_account_in_reach,
    _require_lines,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _transaction_branch,
)
from vs_rbac.scoping import only_branch_id, transaction_branch_scope

# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #



#: What a refused write to a whole-school run names.
SHARED_RUN = "a payroll run for the whole school"


class PayFieldWriteMixin:
    """Judges a payroll or salary write against the caller's Field Access write switches.

    Every view that writes pay figures carries this. Its handler works out,
    inside its transaction and before it writes anything, what each pay key of
    the body would leave on the record and what the record holds now, and
    passes both to :meth:`judge_pay_write`
    (:func:`vs_finance.field_access.assert_pay_writable`). So a value sent back
    exactly as stored is not a write, and a value that changes a figure the
    caller may not change is refused with nothing written.
    ``pay_field_resource`` names the registered resource the body's fields
    belong to.

    A POST, PUT or PATCH that succeeds without having been judged is a fault in
    the view rather than a permitted write: it raises
    :class:`~django.core.exceptions.ImproperlyConfigured` inside the request's
    transaction, so the unjudged write is rolled back. Reads and deletes submit
    no field and are not judged.
    """

    pay_field_resource = "finance.salary"
    _pay_write_judged = False

    def judge_pay_write(self, submitted, *, current, creating=False):
        """Refuse the write if it changes a figure the caller may not change."""
        from ..field_access import assert_pay_writable

        assert_pay_writable(
            self.request, self.pay_field_resource, submitted,
            current=current, creating=creating,
        )
        self._pay_write_judged = True

    def dispatch(self, request, *args, **kwargs):
        if request.method not in ("POST", "PUT", "PATCH"):
            return super().dispatch(request, *args, **kwargs)
        with transaction.atomic():
            response = super().dispatch(request, *args, **kwargs)
            if response.status_code < 400 and not self._pay_write_judged:
                from django.core.exceptions import ImproperlyConfigured

                raise ImproperlyConfigured(
                    f"{type(self).__name__}.{request.method.lower()} wrote without "
                    f"calling judge_pay_write."
                )
        return response


def _parsed(parse, raw):
    """``parse(raw)``, or :data:`~vs_rbac.field_enforcement.UNPARSED` where the write would refuse it."""
    from vs_rbac.field_enforcement import UNPARSED

    try:
        return parse(raw)
    except (ValidationError, TypeError, ValueError, AttributeError):
        return UNPARSED


def _runs_in_reach(request, entity):
    """The runs the caller may open: their branches' own, and central ones through their share.

    A run is a transaction, read by its own branch exclusively
    (:func:`vs_rbac.scoping.transaction_branch_q`). A central run names no
    branch because it covers every branch's staff and is booked per branch:
    each :class:`PayrollRunBranch` names one, and that share is the branch's
    money. So a branch-bound reader reaches a central run through a share of
    one of their branches, or through its one journal when every line it pays
    is booked to one of their branches, and is shown only that part
    (:class:`vs_finance.serializers.PayrollRunSerializer`). Lagoon View pays
    Ikeja's and Lekki's staff in one January run: Lekki's bursar opens it and
    sees Lekki's staff and Lekki's totals. A central run with no share of
    theirs, and a central draft, which has no shares until it posts, are not
    found for them, as another branch's run is. A whole-tenant reader is not
    narrowed.
    """
    from ..models import PayrollRunBranch

    qs = PayrollRun.objects.filter(entity=entity)
    reach = caller_branch_ids(request)
    if reach is None:
        return qs
    ids = tuple(sorted(reach))
    shared_through = PayrollRunBranch.objects.filter(branch_id__in=ids).values("run_id")
    return qs.filter(
        Q(branch_id__in=ids)
        | Q(branch__isnull=True, pk__in=shared_through)
        | Q(branch__isnull=True, journal__branch_id__in=ids),
    )


def _run_data(request, run):
    """``run`` serialized for the caller, narrowed to their branches' part of a central run."""
    return PayrollRunSerializer(
        run, context={"request": request, "branch_ids": caller_branch_ids(request)},
    ).data


def _run_bank_account(request, entity, ref, run_branch):
    """The bank account a payroll run is paid from, by id or name, or None.

    A run is paid from its own branch's account, the rule every document follows
    (:func:`_resolve_bank_account`). A central run at a school with several
    branches names no account of its own: it posts one journal per branch, and
    each branch's share is paid from that branch's account when it is paid
    (:func:`vs_finance.payroll.pay_payroll`). At a school with one branch a
    central run is that branch's and takes its account like any other.
    """
    if ref in (None, ""):
        return None
    if run_branch is None and only_branch_id(entity.tenant_id) is None and \
            entity.tenant_id is not None:
        raise ValidationError({"bank_account": (
            "A payroll run for every branch is paid from each branch's own account. "
            "Name the accounts when you pay it."
        )})
    return _resolve_bank_account(
        request, entity, ref, required=False,
        document_branch=run_branch, noun="payroll run")


def _refuse_central_run_for_branch_caller(request, entity) -> None:
    """A run covering every branch's staff is raised only by a whole-school caller.

    Mrs Bello keeps Ikeja's payroll at Corona. A run for all staff would put
    Lekki's and Yaba's salaries in front of them, and it names no branch, so once
    raised it would be out of their reach anyway. At a school with one branch their
    grant covers the whole school and they are not refused.
    """
    from rest_framework.exceptions import PermissionDenied

    if transaction_branch_scope(request).is_narrowed:
        raise PermissionDenied(
            "A payroll run for all staff is raised by someone who covers the whole "
            "school. Raise one for your branch instead.",
        )


def _line_branch(request, entity, run_branch, ref, where):
    """The branch a hand-typed line of a central run is booked to, or None.

    A branch run's lines are its branch's. A central run's line may name its
    branch; one that does not takes it from the employee's salary row, or the
    school's only branch, when the run posts (:func:`vs_finance.payroll.post_payroll`).
    """
    if run_branch is not None or ref in (None, ""):
        return run_branch
    branch = _resolve_branch(entity.tenant, ref, where)
    if branch is None or not caller_may_use_branch(request, branch):
        raise ValidationError({where: BRANCH_NOT_FOUND})
    return branch


def _line_name(raw):
    """A line's name as the run stores it; anything but text is not a name."""
    if not isinstance(raw, str):
        raise TypeError("An employee name is text.")
    return raw


#: The figures of a hand-typed payroll line, each with how the run view parses
#: it and what a line holds when the key is left out.
_LINE_FIGURES = {
    "employee_name": (_line_name, ""),
    "gross_amount": (lambda raw: _money(raw, "gross_amount"), 0),
    "paye_amount": (lambda raw: _money(raw, "paye_amount"), 0),
    "pension_amount": (lambda raw: _money(raw, "pension_amount"), 0),
}


def _run_line_values(body):
    """``(submitted, current)`` for the pay check of a payroll run typed by hand.

    The figures sit in ``lines``, one mapping per person, and a run is a
    create, so ``current`` holds what a line would carry with the key left
    out. A key is a write when any line sets it to something else: a line
    with no name and nothing typed in writes no figure, and a line with
    Tunde's gross writes his gross. Any other key a line or the body carries
    is judged by its presence.
    """
    from vs_rbac.field_enforcement import UNPARSED

    if not isinstance(body, Mapping):
        return {}, {}
    submitted = dict(body)
    current, changed = {}, set()
    lines = body.get("lines")
    for row in lines if isinstance(lines, list) else ():
        if not isinstance(row, Mapping):
            continue
        for name, raw in row.items():
            figure = _LINE_FIGURES.get(name)
            if figure is None:
                submitted.setdefault(name, raw)
                continue
            parse, default = figure
            value = _parsed(parse, raw)
            current[name] = default
            if value is UNPARSED or type(value) is not type(default) or value != default:
                changed.add(name)
    for name, default in current.items():
        submitted[name] = UNPARSED if name in changed else default
    return submitted, current


# Group endpoint behavior for Payroll Run List Create View.
class PayrollRunListCreateView(PayFieldWriteMixin, _FinanceBase):
    """GET (list) / POST (create draft) payroll runs for an entity.

    docstring-name: Payroll runs
    """

    pay_field_resource = "finance.payrollrun"

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = _runs_in_reach(request, entity).select_related("branch").prefetch_related(
            "lines__branch", "lines__tax_state", "lines__pfa", "lines__items",
            "branch_shares__branch")
        if (status_val := request.query_params.get("run_status")):
            qs = qs.filter(run_status=status_val)
        qs = _filter_by_branch(qs, request, entity)
        return self.paginate(
            request, qs.order_by("-pay_date", "-id"), PayrollRunSerializer,
            context={"request": request, "branch_ids": caller_branch_ids(request)},
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import compute_payroll, ensure_no_overlapping_run

        entity = resolve_entity(request)
        body = request.data or {}
        submitted, current = _run_line_values(body)
        self.judge_pay_write(submitted, current=current, creating=True)
        lines = _require_lines(body)
        branch = _raised_branch(request, entity, body)
        if branch is None:
            _refuse_central_run_for_branch_caller(request, entity)
        pay_date = _date(body.get("pay_date"), "pay_date", required=True)
        # The same guard as the generated run, at the other door into the same
        # table. Typing the lines by hand rather than drawing them from the roster
        # does not make a second run for the period any less of a double payment.
        # No-ops for a central school, which is not guarded at all.
        ensure_no_overlapping_run(entity, pay_date, branch)
        run = PayrollRun.objects.create(
            entity=entity,
            branch=branch,
            pay_date=pay_date,
            period_label=body.get("period_label", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            bank_account=_run_bank_account(request, entity, body.get("bank_account"), branch),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PayrollLine.objects.create(
                run=run, line_no=i,
                branch=_line_branch(request, entity, branch, ln.get("branch"), f"lines[{i}].branch"),
                employee_name=ln.get("employee_name", ""),
                gross_amount=_money(ln.get("gross_amount", 0), f"lines[{i}].gross_amount"),
                taxable_pay=_money(ln.get("gross_amount", 0), f"lines[{i}].gross_amount"),
                paye_amount=_money(ln.get("paye_amount", 0), f"lines[{i}].paye_amount"),
                pension_amount=_money(ln.get("pension_amount", 0), f"lines[{i}].pension_amount"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        compute_payroll(run)
        run.refresh_from_db()
        typed = run.lines.filter(paye_amount__gt=0).count()
        if typed:
            from ..audit import record
            from ..constants import FinanceAuditAction

            # PAYE typed by hand is an override of the computed figure; say so.
            record(
                entity=entity, action=FinanceAuditAction.PAYE_OVERRIDE_CHANGED,
                actor_user=request.user, target=run, branch=run.branch_id,
                message=(
                    f"Raised payroll run {run.document_number} by hand with PAYE typed on "
                    f"{typed} line(s) instead of computed."
                ),
                lines=typed, paye=run.paye_total,
            )
        return success_response(
            f"Payroll run {run.document_number} created.",
            data=_run_data(request, run), status=201,
        )


# Group endpoint behavior for Payroll Run Summary View.
class PayrollRunSummaryView(_FinanceBase):
    """GET - header KPIs over **all** payroll runs (accurate under pagination).

    ``to_pay`` is the net pay no bank account has paid yet. A run posted one
    journal per branch reads POSTED until its last share is paid, so it counts
    its unpaid shares, not its whole net: once Ikeja's share of January is paid,
    only Lekki's is still awaiting payment.

    A branch-bound reader's figures are their branches' part alone
    (:func:`_runs_in_reach`): the runs they reach, their shares of what is
    unpaid, and their own staff on the latest run. Nothing another branch is
    paid reaches them, even summed into a total.

    docstring-name: Payroll runs
    """

    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Exists, OuterRef, Sum
        from django.db.models.functions import Coalesce

        from ..constants import PayrollRunStatus
        from ..models import PayrollRunBranch

        entity = resolve_entity(request)
        reach = caller_branch_ids(request)
        runs = _runs_in_reach(request, entity)
        posted = runs.filter(run_status=PayrollRunStatus.POSTED)
        unsplit = posted.exclude(Exists(PayrollRunBranch.objects.filter(run=OuterRef("pk"))))
        unpaid_shares = PayrollRunBranch.objects.filter(
            run__in=posted, status=PayrollRunStatus.POSTED)
        if reach is not None:
            unpaid_shares = unpaid_shares.filter(branch_id__in=tuple(sorted(reach)))
        to_pay = (
            unsplit.aggregate(net=Coalesce(Sum("net_total"), 0))["net"]
            + unpaid_shares.aggregate(net=Coalesce(Sum("net_total"), 0))["net"]
        )
        agg = {"runs": runs.count(), "to_pay": to_pay}
        latest = runs.order_by("-pay_date", "-id").first()
        employees, net = 0, 0
        if latest is not None:
            latest_lines = latest.lines.all()
            if reach is not None and latest.branch_id is None:
                latest_lines = latest_lines.filter(branch_id__in=tuple(sorted(reach)))
            figures = latest_lines.aggregate(
                count=Count("id"), net=Coalesce(Sum("net_amount"), 0))
            employees, net = figures["count"], figures["net"]
        from ..payroll import payroll_scope

        return success_response(
            "Payroll summary retrieved.",
            data={
                # How this school runs payroll, so the screen knows whether to
                # ask which branch a new run is for. It is a school setting, but
                # reading it through the config API needs `config.value.view` -
                # a settings key no payroll officer holds - and the alternative
                # was a screen that guesses. Only the scope, never the rest of
                # the school's configuration.
                "payroll_scope": payroll_scope(entity),
                "runs": agg["runs"],
                "employees": employees,
                "net": net,
                "to_pay": agg["to_pay"],
            },
        )


class _PayrollActionBase(_FinanceBase):
    """Resolve one run in the caller's reach; on a write, one they may change.

    A central run is reached by a branch-bound reader through their branch's
    share and shown narrowed to it (:func:`_runs_in_reach`). Posting, paying
    and voiding it act on every branch's share at once, so they need
    whole-tenant reach: a branch-bound caller is refused with a 403
    ``SHARED_RECORD_READ_ONLY`` before anything is posted. A branch run of one
    of the caller's branches is theirs to change.
    """

    def _run(self, request, pk):
        from rest_framework.permissions import SAFE_METHODS

        entity = resolve_entity(request)
        run = _runs_in_reach(request, entity).filter(pk=pk).select_related(
            "branch", "journal").prefetch_related(
            "lines__branch", "lines__tax_state", "lines__pfa", "lines__items",
            "branch_shares__branch").first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        if request.method not in SAFE_METHODS:
            assert_caller_may_change(
                request.user, getattr(request, "tenant", None), (run.branch_id,),
                message=shared_write_refusal(SHARED_RUN),
            )
        return entity, run


# Group endpoint behavior for Payroll Run Detail View.
class PayrollRunDetailView(_PayrollActionBase):
    """docstring-name: Payroll runs"""
    rbac_permission = "finance.payrollrun.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, run = self._run(request, pk)
        return success_response("Payroll run retrieved.", data=_run_data(request, run))


# Group endpoint behavior for Payroll Run Post View.
class PayrollRunPostView(_PayrollActionBase):
    """docstring-name: Post a payroll run"""
    rbac_permission = "finance.payrollrun.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import post_payroll

        _, run = self._run(request, pk)
        post_payroll(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} accrued.",
            data=_run_data(request, run),
        )


# Group endpoint behavior for Payroll Run Pay View.
class PayrollRunPayView(_PayrollActionBase):
    """POST - pay a posted run's net wages.

    A run posted as one journal is paid from ``bank_account``, its branch's own. A
    run posted one journal per branch is paid from ``bank_accounts`` (or a single
    ``bank_account``): each account pays its own branch's share, and the rest
    stay unpaid until their accounts are named.

    docstring-name: Pay a payroll run
    """
    rbac_permission = "finance.payrollrun.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import pay_payroll

        entity, run = self._run(request, pk)
        body = request.data or {}
        bank, banks = None, None
        if run.branch_shares.exists():
            refs = body.get("bank_accounts")
            if refs in (None, ""):
                refs = [body["bank_account"]] if body.get("bank_account") not in (None, "") else []
            if not isinstance(refs, list):
                raise ValidationError({"bank_accounts": "Expected a list of bank accounts."})
            banks = [
                _bank_account_in_reach(request, entity, ref, f"bank_accounts[{i}]")
                for i, ref in enumerate(refs)
            ]
        else:
            branch_id = run.journal.branch_id if run.journal_id else run.branch_id
            bank = _run_bank_account(request, entity, body.get("bank_account"), branch_id)
        pay_payroll(
            run, bank_account=bank, bank_accounts=banks,
            pay_date=_date(body.get("pay_date"), "pay_date"),
            actor_user=request.user,
        )
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} disbursed.",
            data=_run_data(request, run),
        )


# Group endpoint behavior for Payroll Run Cancel View.
class PayrollRunCancelView(_PayrollActionBase):
    """POST - cancel a draft run, or void a posted (un-paid) run by reversing its accrual.

    docstring-name: Cancel a payroll run
    """
    rbac_permission = "finance.payrollrun.post"  # the approver who accrues can void

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..payroll import cancel_payroll_run

        _, run = self._run(request, pk)
        cancel_payroll_run(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} cancelled.",
            data=_run_data(request, run),
        )




# --------------------------------------------------------------------------- #
# Employee salary roster                                                      #
# --------------------------------------------------------------------------- #

def _reader_today(request, entity):
    """The day the caller reads the roster on: their branch's, or the school's.

    A caller working in one branch reads on that branch's clock
    (:func:`vs_config.clock.branch_today`); anybody else on the school's
    (:func:`vs_config.clock.tenant_today`). For a school whose branches share a
    time zone, which is nearly every school, the two are the same day.
    """
    from vs_config.clock import branch_today, tenant_today

    reach = caller_branch_ids(request)
    if reach is not None and len(reach) == 1:
        return branch_today(entity.tenant, next(iter(reach)))
    return tenant_today(entity.tenant)


def _salary_rows(request, entity):
    """The salary rows the caller may read: their own branches' staff only.

    A salary belongs to the branch on the version of its terms in force on the
    reader's today (:meth:`~vs_finance.models.EmployeeSalaryQuerySet.with_branch_on`),
    read exclusively like every money record
    (:func:`vs_rbac.scoping.transaction_branch_q`). Mrs Bello keeps Lekki's
    payroll, so they see Lekki's teachers' pay and never an Ikeja teacher's, nor
    the pay of somebody nobody has given a branch yet: that row could be anyone's,
    and placing it is the whole-school bursar's job, who sees every row.

    A move is dated, and the row changes hands on that date. Tunde is moved
    from Ikeja to Lekki on 10 February, dated 1 April. Until 1 April Ikeja
    still pays him and still opens his record; Lekki finds nothing. From
    1 April Lekki opens it, with his whole year behind it, and Ikeja finds
    nothing.

    Every row carries ``branch_on_id`` and ``branch_on_name``, which the roster's
    serializer reports as the row's branch.
    """
    return (
        EmployeeSalary.objects.filter(entity=entity)
        .with_branch_on(_reader_today(request, entity))
        .filter(transaction_branch_q(request, field="branch_on"))
    )


def _salary_data(request, entity, sal):
    """One roster row as the roster serializer shows it, with its branch as of today.

    Read again rather than serialized from the instance in hand, which carries
    the branch as it stood before a write.
    """
    fresh = (
        EmployeeSalary.objects.with_branch_on(_reader_today(request, entity))
        .select_related("cost_center", "structure", "branch", "residence_state", "pfa")
        .get(pk=sal.pk)
    )
    return EmployeeSalarySerializer(fresh, context={"request": request}).data


# Support the resolve salary workflow.
def _resolve_salary(request, entity, pk):
    """One roster row the caller is entitled to, or 404.

    Narrowed rather than merely entity-scoped, and exclusively, like the list:
    the same rule as :func:`_salary_rows`, for a read and a write alike.
    """
    sal = _salary_rows(request, entity).filter(pk=pk).first()
    if sal is None:
        raise NotFound("Employee salary not found for this entity.")
    return sal


# Support the resolve employee workflow.
def _resolve_employee(entity, raw):
    """Resolve an account id to the person a roster row is for, or None.

    Deliberately a USER id and not a staff-record id. This app is
    domain-neutral: it knows about entities, employees and money, and nothing
    about schools or staff profiles, so a school module cannot be imported here
    to resolve one. A user is the identity every domain already shares, and it
    is what ``EmployeeSalary.employee`` has always pointed at.

    Scoped to the entity's own tenant, so a crafted id cannot attach another
    customer's account to this roster.

    Optional on purpose, and it stays optional. A school may legitimately pay
    somebody who has no account at all - a contractor, a visiting examiner - so
    ``name`` remains the required field and this only ever adds certainty where
    it is available.
    """
    if raw in (None, "", 0, "0"):
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(
        pk=raw, tenant=entity.tenant,
    ).first() if str(raw).isdigit() else None
    if user is None:
        raise ValidationError({"employee": "No such person at this customer."})
    return user


def _resolve_jurisdiction(raw, where="residence_state"):
    """A state of residence by id or code, or None for a blank value."""
    from ..models import PayrollTaxJurisdiction

    if raw in (None, "", 0, "0"):
        return None
    qs = PayrollTaxJurisdiction.objects.filter(is_active=True)
    found = (
        qs.filter(pk=raw).first() if str(raw).isdigit()
        else qs.filter(code__iexact=str(raw).strip()).first()
    )
    if found is None:
        raise ValidationError({where: "No such state."})
    return found


def _resolve_pfa(raw):
    """A pension fund administrator by id or code, or None for a blank value."""
    from ..models import PensionFundAdministrator

    if raw in (None, "", 0, "0"):
        return None
    qs = PensionFundAdministrator.objects.filter(is_active=True)
    found = (
        qs.filter(pk=raw).first() if str(raw).isdigit()
        else qs.filter(code__iexact=str(raw).strip()).first()
    )
    if found is None:
        raise ValidationError({"pfa": "No such pension fund administrator."})
    return found


def _identifier(body, field):
    value = str(body.get(field) or "").strip()
    if len(value) > 32:
        raise ValidationError({field: "Use at most 32 characters."})
    return value


def _override(body):
    """``(amount or None, reason)`` from ``paye_override`` / ``paye_override_reason``.

    Setting an override needs a reason: it replaces the computed PAYE on every
    run until cleared, and the audit trail has to say why.
    """
    raw = body.get("paye_override")
    if raw in (None, ""):
        return None, ""
    amount = _money(raw, "paye_override")
    reason = str(body.get("paye_override_reason") or "").strip()
    if not reason:
        raise ValidationError({"paye_override_reason": "Say why PAYE is overridden."})
    return amount, reason[:255]


# Support the resolve structure workflow.
def _resolve_structure(entity, raw, *, required=False):
    """Resolve a salary-structure id scoped to the entity, or None."""
    if raw in (None, "", 0, "0"):
        if required:
            raise ValidationError({"structure": "A salary structure is required."})
        return None
    structure = SalaryStructure.objects.filter(entity=entity, pk=raw).first()
    if structure is None:
        raise ValidationError({"structure": "Salary structure not found for this entity."})
    return structure


#: What a new salary row holds for each pay key its body leaves out.
_SALARY_PAY_DEFAULTS = {
    "gross_amount": 0, "paye_amount": 0, "pension_amount": 0, "annual_rent": 0,
    "structure": None, "residence_state": None, "pfa": None,
    "tax_id": "", "pension_pin": "", "paye_override": None, "paye_override_reason": "",
}


def _salary_pay_values(entity, body, *, held_reason=""):
    """Each key of a salary body, with every pay key as the row would hold it after the write.

    Parsed by the same functions the write uses, so ``"234567"`` and
    ``234567`` are one gross, a state named by code or by id is one state, and
    a value the write would refuse is
    :data:`~vs_rbac.field_enforcement.UNPARSED`. A PAYE override reason is
    written only with ``paye_override``; sent without it, the row keeps
    *held_reason*. Any other key keeps its raw value and is judged by presence.
    """
    from vs_rbac.field_enforcement import UNPARSED

    if not isinstance(body, Mapping):
        return {}
    parsers = {
        "gross_amount": lambda raw: _money(raw, "gross_amount"),
        "paye_amount": lambda raw: _money(raw, "paye_amount"),
        "pension_amount": lambda raw: _money(raw, "pension_amount"),
        "annual_rent": lambda raw: _money(raw, "annual_rent"),
        "structure": lambda raw: getattr(_resolve_structure(entity, raw), "pk", None),
        "residence_state": lambda raw: getattr(_resolve_jurisdiction(raw), "pk", None),
        "pfa": lambda raw: getattr(_resolve_pfa(raw), "pk", None),
        "tax_id": lambda raw: _identifier({"tax_id": raw}, "tax_id"),
        "pension_pin": lambda raw: _identifier({"pension_pin": raw}, "pension_pin"),
    }
    submitted = dict(body)
    for name, parse in parsers.items():
        if name in body:
            submitted[name] = _parsed(parse, body.get(name))
    if "paye_override" in body:
        override = _parsed(_override, body)
        amount, reason = (UNPARSED, UNPARSED) if override is UNPARSED else override
        submitted["paye_override"] = amount
        if "paye_override_reason" in body:
            submitted["paye_override_reason"] = reason
    elif "paye_override_reason" in body:
        submitted["paye_override_reason"] = held_reason
    return submitted


def _salary_pay_held(sal):
    """What ``sal`` holds for each pay key, in the form :func:`_salary_pay_values` gives.

    The terms are those of its latest version, the ones a change is written
    over (:func:`vs_finance.payroll_statutory.change_terms`).
    """
    terms = sal.terms_on(datetime.date.max) or sal
    return {
        "gross_amount": terms.gross_amount, "paye_amount": terms.paye_amount,
        "pension_amount": terms.pension_amount, "structure": terms.structure_id,
        "residence_state": terms.residence_state_id, "annual_rent": sal.annual_rent,
        "pfa": sal.pfa_id, "tax_id": sal.tax_id, "pension_pin": sal.pension_pin,
        "paye_override": sal.paye_override, "paye_override_reason": sal.paye_override_reason,
    }


# Group endpoint behavior for Employee Salary List Create View.
class EmployeeSalaryListCreateView(PayFieldWriteMixin, _FinanceBase):
    """GET (list) / POST (add) employee salaries - the roster a run is generated from.

    Rows are read by the employee's branch, exclusively (:func:`_salary_rows`).
    A new row names its branch as a transaction does
    (:func:`vs_rbac.scoping.raised_transaction_branch`): a pinned officer's hire
    is theirs, a whole-school bursar at a school with several branches names one,
    and a school with one branch files it there without asking. A row with no
    branch at a school with several stops the payroll run it is on from posting,
    so none is created.

    docstring-name: Employee salaries
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = (
            _salary_rows(request, entity)
            .select_related("cost_center", "structure", "branch")
            .prefetch_related("structure__components")
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("search")):
            qs = qs.filter(name__icontains=search)
        qs = _filter_by_branch(qs, request, entity, column="branch_on")
        return success_response(
            "Employee salaries retrieved.",
            data=EmployeeSalarySerializer(qs.order_by("name"), many=True,
                                          context={"request": request}).data,
        )

    # Handle POST requests for this endpoint.
    @transaction.atomic
    def post(self, request):
        from ..payroll_statutory import (
            assert_one_active_row,
            record_creation,
            record_override_change,
        )

        entity = resolve_entity(request)
        body = request.data or {}
        self.judge_pay_write(
            _salary_pay_values(entity, body), current=_SALARY_PAY_DEFAULTS, creating=True,
        )
        employee = _resolve_employee(entity, body.get("employee"))
        name = str(body.get("name", "")).strip()
        if not name and employee is not None:
            # Taken from the account when one is named, so a roster keyed on a
            # person does not also depend on somebody retyping their name the
            # same way twice.
            name = " ".join(
                part for part in (employee.first_name, employee.last_name) if part
            ).strip()
        if not name:
            raise ValidationError({"name": "An employee name is required."})
        override, override_reason = _override(body)
        sal = EmployeeSalary(
            entity=entity, name=name, employee=employee,
            # Every new hire names a branch; see the view's docstring.
            branch=_transaction_branch(request, entity, body),
            structure=_resolve_structure(entity, body.get("structure")),
            gross_amount=_money(body.get("gross_amount", 0), "gross_amount"),
            paye_amount=_money(body.get("paye_amount", 0), "paye_amount"),
            pension_amount=_money(body.get("pension_amount", 0), "pension_amount"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),
            is_active=_bool(body.get("is_active", True), default=True),
            residence_state=_resolve_jurisdiction(body.get("residence_state")),
            tax_id=_identifier(body, "tax_id"),
            pfa=_resolve_pfa(body.get("pfa")),
            pension_pin=_identifier(body, "pension_pin"),
            annual_rent=_money(body.get("annual_rent", 0), "annual_rent"),
            paye_override=override, paye_override_reason=override_reason,
        )
        assert_one_active_row(sal)
        sal.save()
        record_creation(
            sal, effective_from=_date(body.get("effective_from"), "effective_from"),
            actor_user=request.user,
        )
        if override is not None:
            record_override_change(sal, None, "", actor_user=request.user)
        return success_response(
            f"Employee salary for {name} added.",
            data=_salary_data(request, entity, sal), status=201,
        )


# Group endpoint behavior for Employee Salary Detail View.
class EmployeeSalaryDetailView(PayFieldWriteMixin, _FinanceBase):
    """PATCH / DELETE one employee salary.

    ``branch`` places or moves a person under the same rule a new row is filed
    by: a pinned officer's own branch, or any of the school's for a whole-school
    bursar, and a 403 for anybody else's. Nobody can move a person back to no
    branch at a school with several.

    docstring-name: Employee salaries
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        if self.request.method == "DELETE":
            return "finance.salary.delete"
        if self.request.method == "PATCH":
            return "finance.salary.update"
        return "finance.salary.view"

    # Handle PATCH requests for this endpoint.
    @transaction.atomic
    def patch(self, request, pk):
        """Edit a roster row: profile in place, pay terms as a new dated version.

        The pay terms (``branch``, ``structure``, ``gross_amount``,
        ``paye_amount``, ``pension_amount``, ``cost_center``,
        ``residence_state``) are never overwritten: the change is written as a
        version effective from ``effective_from``, or from the first payroll
        month not yet paid, and audited
        (:func:`vs_finance.payroll_statutory.change_terms`). The profile (name,
        account, tax number, PFA and PIN, annual rent) is edited in place and
        audited; a PAYE override is audited on its own. ``is_active`` false takes
        the person off the payroll.

        Only a pay value that changes the row is checked against the caller's
        write switches (:meth:`PayFieldWriteMixin.judge_pay_write`), with the
        row locked so the values compared are the ones replaced. The edit form
        sends back the structure, state and PFA it was opened with; a bursar
        who may read pay but not change it corrects Tunde's name and saves, and
        is refused only for a figure whose value actually differs.
        """
        from ..payroll_statutory import (
            PROFILE_FIELDS,
            assert_branch_required,
            assert_one_active_row,
            change_terms,
            deactivate_salary,
            record_override_change,
            record_profile_change,
        )

        entity = resolve_entity(request)
        sal = _resolve_salary(request, entity, pk)
        # Locked, so the values judged are the values the write replaces.
        list(EmployeeSalary.objects.select_for_update().filter(pk=sal.pk).values_list("pk"))
        sal.refresh_from_db()
        body = request.data or {}
        self.judge_pay_write(
            _salary_pay_values(entity, body, held_reason=sal.paye_override_reason),
            current=_salary_pay_held(sal),
        )
        profile_before = {k: getattr(sal, k) for k in PROFILE_FIELDS + ("is_active",)}
        override_before = (sal.paye_override, sal.paye_override_reason)
        if "employee" in body:
            # A roster row written before rows named their person is linked here,
            # one row at a time.
            sal.employee = _resolve_employee(entity, body.get("employee"))
            if sal.employee is not None and not str(body.get("name", "")).strip():
                sal.name = " ".join(
                    part for part in (
                        sal.employee.first_name, sal.employee.last_name,
                    ) if part
                ).strip() or sal.name
        if "name" in body:
            sal.name = str(body["name"]).strip()
        for field in ("tax_id", "pension_pin"):
            if field in body:
                setattr(sal, field, _identifier(body, field))
        if "pfa" in body:
            sal.pfa = _resolve_pfa(body.get("pfa"))
        if "annual_rent" in body:
            sal.annual_rent = _money(body.get("annual_rent"), "annual_rent")
        if "paye_override" in body:
            sal.paye_override, sal.paye_override_reason = _override(body)
        deactivate = False
        if "is_active" in body:
            active = _bool(body.get("is_active"), default=sal.is_active)
            deactivate = sal.is_active and not active
            if active and not sal.is_active:
                assert_branch_required(entity, sal.branch_id, True)
                sal.is_active = True
        assert_one_active_row(sal)
        sal.save()

        terms = {}
        if "branch" in body:
            # Placing and moving people is editable; see the view's docstring.
            terms["branch_id"] = getattr(_transaction_branch(request, entity, body), "pk", None)
        if "structure" in body:
            terms["structure_id"] = getattr(
                _resolve_structure(entity, body.get("structure")), "pk", None)
        for field in ("gross_amount", "paye_amount", "pension_amount"):
            if field in body:
                terms[field] = _money(body.get(field), field)
        if "cost_center" in body:
            terms["cost_center_id"] = getattr(
                _resolve_cost_center(entity, body.get("cost_center"), "cost_center"), "pk", None)
        if "residence_state" in body:
            terms["residence_state_id"] = getattr(
                _resolve_jurisdiction(body.get("residence_state")), "pk", None)
        if terms:
            change_terms(
                sal, terms, effective_from=_date(body.get("effective_from"), "effective_from"),
                reason=str(body.get("reason") or ""), actor_user=request.user,
            )
        record_profile_change(sal, profile_before, actor_user=request.user)
        record_override_change(sal, *override_before, actor_user=request.user)
        if deactivate:
            deactivate_salary(sal, actor_user=request.user)
        return success_response(
            "Employee salary updated.", data=_salary_data(request, entity, sal),
        )

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk):
        """Take a person off the payroll. The row and its history stay, audited."""
        from ..payroll_statutory import deactivate_salary

        entity = resolve_entity(request)
        deactivate_salary(_resolve_salary(request, entity, pk), actor_user=request.user)
        return success_response("Employee salary removed.", data={})


# Group endpoint behavior for Payroll Run Generate View.
class PayrollRunGenerateView(_FinanceBase):
    """POST - raise a draft payroll run from the active employee-salary roster.

    Central or per branch, whichever the school has chosen. Under CENTRAL the run
    covers the whole roster, names no branch, and is raised only by a caller who
    covers the whole school; it posts one journal per branch. Under PER_BRANCH the
    caller's branch decides which roster rows it covers, and it covers that
    branch's staff and nobody else's, so the same person is never on two runs.

    docstring-name: Generate a payroll run
    """

    rbac_permission = "finance.payrollrun.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll import generate_run_from_roster, is_per_branch

        entity = resolve_entity(request)
        body = request.data or {}
        # Under CENTRAL the run covers every branch and names none.
        branch = (
            _raised_branch(request, entity, body) if is_per_branch(entity) else None
        )
        if branch is None:
            _refuse_central_run_for_branch_caller(request, entity)
        run = generate_run_from_roster(
            entity, pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            branch=branch,
            period_label=body.get("period_label", ""), narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")), actor_user=request.user,
        )
        from vs_rbac.field_enforcement import can_read

        data = _run_data(request, run)
        # People a live run of the period already pays, left off this one; the
        # names only for a caller who may read payroll names.
        named = can_read(request, "finance.payrollrun.employee_name")
        data["skipped"] = [
            {"name": name if named else None, "run": other.document_number or other.pk}
            for name, other in getattr(run, "skipped", [])
        ]
        # People paid for the first time this tax year after January with no
        # earlier pay recorded; the names only for a caller who may read them.
        data["previous_pay_missing"] = [
            name if named else None for name in getattr(run, "previous_pay_missing", [])
        ]
        return success_response(
            f"Payroll run {run.document_number} generated from {run.lines.count()} employee(s).",
            data=data, status=201,
        )


# --------------------------------------------------------------------------- #
# Salary structures (reusable pay templates)                                  #
# --------------------------------------------------------------------------- #

_VALID_KINDS = {SalaryComponentKind.EARNING, SalaryComponentKind.DEDUCTION}
_VALID_METHODS = {
    SalaryCalcMethod.FIXED, SalaryCalcMethod.PERCENT_OF_GROSS, SalaryCalcMethod.PERCENT_OF_BASIC,
}
_VALID_STATUTORY = {StatutoryType.PAYE, StatutoryType.PENSION}


def _parse_components(raw) -> list:
    """Validate a request body's component list into unsaved :class:`SalaryComponent` lines.

    Earnings carry no statutory type and may be flagged basic, pensionable and
    taxable (taxable by default); deductions must be PAYE or pension, and count
    only for a tenant whose PAYE is supplied rather than computed.
    """
    if not isinstance(raw, list):
        raise ValidationError({"components": "Expected a list of components."})

    rows = []
    for i, c in enumerate(raw):
        where = f"components[{i}]"
        name = str(c.get("name", "")).strip()
        if not name:
            raise ValidationError({where: "A component name is required."})
        kind = c.get("kind", SalaryComponentKind.EARNING)
        if kind not in _VALID_KINDS:
            raise ValidationError({f"{where}.kind": "Must be EARNING or DEDUCTION."})
        method = c.get("calc_method", SalaryCalcMethod.PERCENT_OF_GROSS)
        if method not in _VALID_METHODS:
            raise ValidationError({f"{where}.calc_method": "Unknown calc method."})

        statutory = StatutoryType.NONE
        if kind == SalaryComponentKind.DEDUCTION:
            statutory = c.get("statutory_type")
            if statutory not in _VALID_STATUTORY:
                raise ValidationError(
                    {f"{where}.statutory_type": "Deductions must be PAYE or PENSION."},
                )

        rate_bps = int(c.get("rate_bps") or 0)
        amount = _money(c.get("amount", 0), f"{where}.amount")
        if method == SalaryCalcMethod.FIXED and amount <= 0:
            raise ValidationError({f"{where}.amount": "Fixed components need a positive amount."})
        if method != SalaryCalcMethod.FIXED and not (0 < rate_bps <= 1_000_000):
            raise ValidationError({f"{where}.rate_bps": "Percent components need a rate in basis points."})

        earning = kind == SalaryComponentKind.EARNING
        rows.append(SalaryComponent(
            name=name, kind=kind, calc_method=method, rate_bps=rate_bps, amount=amount,
            is_basic=bool(c.get("is_basic", False)) and earning,
            is_pensionable=bool(c.get("is_pensionable", False)) and earning,
            is_taxable=bool(c.get("is_taxable", True)) or not earning,
            statutory_type=statutory, sequence=int(c.get("sequence", i)),
        ))
    return rows


def _component_rows(components) -> list:
    """Structure lines as comparable rows, in the order they are stored and read.

    By sequence, and in the order given within one sequence, which is the
    order a list written by :func:`_parse_components` is read back in. Given
    the current lines ordered by sequence and id, it is the order they are read
    in now, so two equal results are one set of lines.
    """
    from ..payroll_statutory import component_row

    return [component_row(c) for c in sorted(components, key=lambda c: c.sequence)]


def _structure_pay_values(body):
    """A structure body, with ``components`` as the lines it would write.

    Each line as :func:`_component_rows` compares it, or
    :data:`~vs_rbac.field_enforcement.UNPARSED` for a list the write would
    refuse. Every other key keeps its raw value.
    """
    if not isinstance(body, Mapping):
        return {}
    submitted = dict(body)
    if "components" in body:
        submitted["components"] = _parsed(
            lambda raw: _component_rows(_parse_components(raw)), body.get("components"),
        )
    return submitted


def _structure_data(structure):
    """A structure with its current lines, as the structure screens show it."""
    from django.db.models import Prefetch

    fresh = (
        SalaryStructure.objects.filter(pk=structure.pk)
        .prefetch_related(Prefetch(
            "components", queryset=SalaryComponent.objects.filter(effective_to__isnull=True),
        ))
        .first()
    )
    return SalaryStructureSerializer(fresh).data


# Group endpoint behavior for Salary Structure List Create View.
class SalaryStructureListCreateView(PayFieldWriteMixin, _FinanceBase):
    """GET (list) / POST (create) reusable salary structures for an entity.

    Lists show each structure's current lines; ``effective_from`` on a create
    dates its first lines (from the start when left out).

    docstring-name: Salary structures
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.create" if self.request.method == "POST" \
            else "finance.salary.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Prefetch

        entity = resolve_entity(request)
        qs = (
            SalaryStructure.objects.filter(entity=entity)
            .prefetch_related(Prefetch(
                "components", queryset=SalaryComponent.objects.filter(effective_to__isnull=True),
            ))
            .annotate(employee_count_annot=Count("employee_salaries", distinct=True))
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Salary structures retrieved.",
            data=SalaryStructureSerializer(qs.order_by("name"), many=True).data,
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..payroll_statutory import replace_components

        entity = resolve_entity(request)
        body = request.data or {}
        self.judge_pay_write(_structure_pay_values(body), current={"components": []}, creating=True)
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A structure name is required."})
        if SalaryStructure.objects.filter(entity=entity, name__iexact=name).exists():
            raise ValidationError({"name": "A structure with this name already exists."})
        structure = SalaryStructure.objects.create(
            entity=entity, name=name,
            description=str(body.get("description", "")).strip(),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        replace_components(
            structure, _parse_components(body.get("components", [])),
            effective_from=_date(body.get("effective_from"), "effective_from"),
            actor_user=request.user, creating=True,
        )
        return success_response(
            f"Salary structure '{name}' created.", data=_structure_data(structure), status=201,
        )


# Group endpoint behavior for Salary Structure Detail View.
class SalaryStructureDetailView(PayFieldWriteMixin, _FinanceBase):
    """GET / PATCH / DELETE one salary structure.

    ``components`` on a PATCH replaces the current lines from ``effective_from``
    (or the first payroll month nobody on the structure has been paid for); the
    old lines are kept for the months they priced, and the change is audited.
    A list equal to the current lines, as an edit form sends back when only the
    name changed, is not a change: nothing is replaced, and the caller needs no
    write switch on the pay breakdown for it.

    docstring-name: Salary structures
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.salary.view" if self.request.method == "GET" \
            else "finance.salary.update"

    # Support the structure workflow.
    def _structure(self, request, pk):
        entity = resolve_entity(request)
        structure = SalaryStructure.objects.filter(entity=entity, pk=pk).first()
        if structure is None:
            raise NotFound("Salary structure not found for this entity.")
        return entity, structure

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, structure = self._structure(request, pk)
        return success_response("Salary structure retrieved.", data=_structure_data(structure))

    @transaction.atomic
    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        from ..payroll_statutory import replace_components

        entity, structure = self._structure(request, pk)
        # Locked, so the lines judged are the lines the write replaces.
        list(SalaryStructure.objects.select_for_update().filter(pk=structure.pk).values_list("pk"))
        body = request.data or {}
        submitted = _structure_pay_values(body)
        held = {"components": _component_rows(
            structure.components.filter(effective_to__isnull=True).order_by("sequence", "id"),
        )}
        self.judge_pay_write(submitted, current=held)
        if "name" in body:
            name = str(body["name"]).strip()
            if not name:
                raise ValidationError({"name": "A structure name is required."})
            if (
                SalaryStructure.objects.filter(entity=entity, name__iexact=name)
                .exclude(pk=structure.pk)
                .exists()
            ):
                raise ValidationError({"name": "A structure with this name already exists."})
            structure.name = name
        if "description" in body:
            structure.description = str(body["description"]).strip()
        if "is_active" in body:
            structure.is_active = _bool(body.get("is_active"), default=structure.is_active)
        structure.save()
        if "components" in body and submitted["components"] != held["components"]:
            replace_components(
                structure, _parse_components(body.get("components", [])),
                effective_from=_date(body.get("effective_from"), "effective_from"),
                actor_user=request.user,
            )
        return success_response("Salary structure updated.", data=_structure_data(structure))

    # Handle DELETE requests for this endpoint.
    def delete(self, request, pk):
        _, structure = self._structure(request, pk)
        if structure.employee_salaries.exists() or structure.salary_versions.exists():
            raise ValidationError(
                {"structure": "This structure is or was assigned to employees; deactivate it instead."},
            )
        structure.delete()
        return success_response("Salary structure removed.", data={})
