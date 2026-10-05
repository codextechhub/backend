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
structures, and the lines of a payroll run typed by hand. What is judged is
the change, not the key: a form that sends back a figure exactly as stored
writes nothing to it and is not refused for it. Three body keys are
not registered names but change a registered figure when written, so they
travel under its write switch (:data:`PAY_WRITE_ALIASES`): the state of
residence decides which state's PAYE is charged, the pension fund
administrator where pension is remitted, and the structure the pay breakdown.
Reading them is not reading a figure, so their read side is unchanged; they
are registered as the resource's write aliases, so the ``/me`` map and the
roster row's ``_read_only_fields`` list each one as read-only exactly when the
switch it follows may not be written.

The statutory figures travel under the switch of the figure they belong to,
so a role that may see a person's PAYE sees how it was worked out and a role
that may not sees neither: on a line, the person's tax ID and pension PIN go
with their name, taxable pay with gross, the PAYE working with PAYE, and the
deduction and contribution items with the pay breakdown; on a salary row, the
tax ID, annual rent and any PAYE override go with PAYE, and the pension PIN
with pension. A person's voluntary deduction (a staff loan, cooperative
savings) is a line of their pay breakdown wherever it is read, so its amount
and limit go with the pay breakdown on the roster too.

A person's pay brought forward into a tax year, whether a previous
employer's or this employer's own from before its payroll ran here
(:class:`~vs_finance.models.PayBroughtForward`), is read and written under the
switch of the same figure of their pay here: its gross and taxable pay with
gross pay, the PAYE deducted with PAYE, its pension with pension, and its NHF
with the pay breakdown, as NHF is on a payroll line. The body keys carry a
``brought_forward_`` prefix so that neither a form nor the audit trail can
mistake them for the person's current pay. On a payroll line and a payslip
they travel inside the PAYE working, so they go with PAYE there.
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


def assert_pay_writable(request, resource: str, submitted: Mapping, *, current: Mapping,
                        creating: bool = False):
    """Refuse a payroll or salary write that changes a figure the caller may not change.

    The one check every payroll and salary write path makes, inside its
    transaction and before it writes anything, so nothing is half written
    when it refuses. Only a value that changes the record is judged:
    *submitted* maps each body key to what the record would hold after the
    write, parsed as the view writes it, and *current* to what it holds now
    (on a create, what it would hold were the key left out). Mrs Okafor may
    read salaries but not change them. Correcting the spelling of Tunde's name
    while her form sends back his unchanged gross, structure, state and PFA
    succeeds; a body that changes his gross, his tax ID, his PFA or a
    deduction's amount is refused with the standard field refusal, naming each
    such field, and his record is untouched
    (:func:`vs_rbac.field_enforcement.assert_writable`).
    """
    from vs_rbac.field_enforcement import assert_writable

    assert_writable(
        request, resource, submitted, creating=creating,
        aliases=PAY_WRITE_ALIASES.get(resource), current=current,
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
            "vs_finance.serializers.EmployeeSalaryNextTermsSerializer",
            "vs_finance.serializers.EmployeeSalaryVersionSerializer",
            "vs_finance.serializers.EmployeeDeductionSerializer",
            "vs_finance.serializers.PayBroughtForwardSerializer",
        ),
        fields=(
            FieldSpec("gross_amount", "Gross pay", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=10,
                      api_names=("gross_amount", "brought_forward_gross_amount",
                                 "brought_forward_taxable_pay")),
            FieldSpec("paye_amount", "PAYE", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=20,
                      api_names=("paye_amount", "paye_override", "paye_override_reason",
                                 "annual_rent", "tax_id", "brought_forward_paye_amount")),
            FieldSpec("pension_amount", "Pension", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=30,
                      api_names=("pension_amount", "pension_pin", "brought_forward_pension_amount")),
            FieldSpec("net_amount", "Net pay", group="Pay", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=40),
            FieldSpec("components", "Pay breakdown", group="Pay", sensitive=True,
                      scope=_TENANT, sort_order=50,
                      api_names=("components", "amount", "total_limit", "brought_forward_nhf_amount")),
        ),
        write_aliases=PAY_WRITE_ALIASES["finance.salary"],
    )
