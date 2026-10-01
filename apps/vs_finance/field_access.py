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
  accept. Net and components are derived from them and the structure.

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
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"


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
                      writable=False, scope=_TENANT, sort_order=50,
                      api_names=("components", "amount", "total_limit")),
        ),
    )
