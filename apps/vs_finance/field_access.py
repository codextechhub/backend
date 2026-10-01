"""Finance fields an administrator may restrict per role.

A bank account's number, and the figures on a payroll line and a salary row.
All are declared sensitive, so a role reaches none of them until a school turns
it on: reaching the payroll screens is ``finance.payrollrun.view``, and seeing
what each person is paid is a second decision.

What decides ``writable``:

* A bank account's number is set by the create and update endpoints.
* A payroll line's name, gross, PAYE and pension are typed in when a run is
  raised by hand. Its net is worked out by ``compute_payroll`` and its
  components are a snapshot of the salary structure, so neither is writable.
* A salary row's gross, PAYE and pension are stored figures its endpoints
  accept. Its net is derived from them and the structure. The pay breakdown
  is writable: a person's voluntary deductions (their amount and limit), the
  salary structure assigned to them and the lines of a salary structure are
  what shape it, and they are typed in.

Every payroll and salary write checks the caller's write switches before it
writes anything (:func:`assert_pay_writable`), so a role that may read pay but
not change it is refused with the standard field refusal on every path: a
roster row's create and update (and the dated version of its terms an update
writes), its statutory details and PAYE override, its deductions, the salary
structures, and the lines of a payroll run typed by hand. Three body keys are
not registered names but change a registered figure when written, so they
travel under its write switch (:data:`PAY_WRITE_ALIASES`): the state of
residence decides which state's PAYE is charged, the pension fund
administrator where pension is remitted, and the structure the pay breakdown.
Reading them is not reading a figure, so their read side is unchanged.

The statutory figures travel under the switch of the figure they belong to,
so a role that may see a person's PAYE sees how it was worked out and a role
that may not sees neither: on a line, the person's tax ID and pension PIN go
with their name, taxable pay with gross, the PAYE working with PAYE, and the
deduction and contribution items with the pay breakdown; on a salary row, the
tax ID, annual rent and any PAYE override go with PAYE, and the pension PIN
with pension. A person's voluntary deduction (a staff loan, cooperative
savings) is a line of their pay breakdown wherever it is read, so its amount
and limit go with the pay breakdown on the roster too.
"""
from collections.abc import Mapping

from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"

#: Per resource, body keys that are not registered names, mapped to the
#: registered name whose write switch governs them (see the module docstring).
PAY_WRITE_ALIASES: dict[str, dict[str, str]] = {
    "finance.salary": {
        "residence_state": "paye_amount",
        "pfa": "pension_amount",
        "structure": "components",
    },
}


def submitted_names(body, *, rows_key: str = "") -> list[str]:
    """The names a payroll write body submits, including those inside its rows.

    A hand-typed payroll run carries its figures in ``lines``, one mapping
    per person; each name any row carries counts once. A body that is not a
    mapping submits nothing a switch governs, and the view refuses it on its
    own terms.
    """
    if not isinstance(body, Mapping):
        return []
    names = list(body)
    rows = body.get(rows_key) if rows_key else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                names.extend(name for name in row if name not in names)
    return names


def assert_pay_writable(request, resource: str, body, *, creating=False, rows_key: str = ""):
    """Refuse a payroll or salary write naming a field the caller may not change.

    The one check every payroll and salary write path makes before it writes
    (:class:`vs_finance.views_ops.payroll.PayFieldWriteMixin` applies it to
    each), so nothing is half written when it refuses. Mrs Okafor may read
    salaries but not change them: a body setting Tunde's gross, his tax ID,
    his PFA or a deduction's amount is refused with the standard field
    refusal, naming each such field, and his record is untouched.
    """
    from vs_rbac.field_enforcement import assert_writable

    assert_writable(
        request, resource, submitted_names(body, rows_key=rows_key),
        creating=creating, aliases=PAY_WRITE_ALIASES.get(resource),
    )


def register():
    """Publish the finance field declarations to the Field Access registry."""
    register_fields(
        "finance",
        "bankaccount",
        surfaces=("vs_finance.serializers.BankAccountSerializer",),
        fields=(
            FieldSpec("account_number", "Account number", group="Banking",
                      sensitive=True, scope=_TENANT, sort_order=10),
        ),
    )
    register_fields(
        "finance",
        "payrollrun",
        surfaces=("vs_finance.serializers.PayrollLineSerializer",),
        fields=(
            FieldSpec("employee_name", "Employee name", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=10,
                      api_names=("employee_name", "tax_id", "pension_pin")),
            FieldSpec("gross_amount", "Gross pay", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=20, api_names=("gross_amount", "taxable_pay")),
            FieldSpec("paye_amount", "PAYE", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=30, api_names=("paye_amount", "tax_basis")),
            FieldSpec("pension_amount", "Pension", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=40),
            FieldSpec("net_amount", "Net pay", group="Pay", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=50),
            FieldSpec("components", "Pay breakdown", group="Pay", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=60,
                      api_names=("components", "items", "other_deductions_amount",
                                 "employer_contributions_amount")),
        ),
    )
    register_fields(
        "finance",
        "salary",
        surfaces=(
            "vs_finance.serializers.EmployeeSalarySerializer",
            "vs_finance.serializers.EmployeeSalaryVersionSerializer",
            "vs_finance.serializers.EmployeeDeductionSerializer",
        ),
        fields=(
            FieldSpec("gross_amount", "Gross pay", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("paye_amount", "PAYE", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=20,
                      api_names=("paye_amount", "paye_override", "paye_override_reason",
                                 "annual_rent", "tax_id")),
            FieldSpec("pension_amount", "Pension", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=30, api_names=("pension_amount", "pension_pin")),
            FieldSpec("net_amount", "Net pay", group="Pay", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=40),
            FieldSpec("components", "Pay breakdown", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=50,
                      api_names=("components", "amount", "total_limit")),
        ),
    )
