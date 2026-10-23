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

A branch and a document number are never fields. A branch's name always shows,
and which branch a person may choose is decided by their branch reach and
permission keys; a document number identifies the document the way a name
identifies a customer. Neither is registered on any resource, and
``RegistryHoldsNoBranchOrDocumentNumberTests`` in ``vs_rbac`` keeps it so.

The receivables accruals carry four resources of their own, all open to every
role until a school closes a field:

* ``finance.deferredincome``: what is waiting to be released, what has been
  released, what falls due by month, and each release (its accounting
  period, date, amount, journal and whether it can still be undone, with the
  reason it cannot). The release date and the accounting period are the two
  writable fields, because the release run submits ``up_to`` and the undo
  submits ``period``. The sentence saying why an undo is blocked names a branch
  and a month, so it is worded without the month when the period is hidden.
* ``finance.provision``: a doubtful-debt run (its status, date,
  narration, totals, policy snapshot and when it was raised) and each branch's
  line of it (required, current, movement, age bands, journal). Only the
  provision date and the narration are typed in, when a run is raised.
* ``finance.deposit``: a refundable deposit and where it stands. Billing opens a
  deposit and the release and forfeit runs close it, so every field is
  read-only. The release and forfeit responses are rendered through the same
  switches, and a caller who may not read a deposit's customer may not name one
  to release.
* ``finance.settings``: every value on the finance settings screen: account
  mappings, document defaults, banking and cash, the fiscal calendar, the
  receivables policy, payroll and record retention. Each value is stored and
  submitted, so each has a Read and a Write switch, labelled as the settings
  history labels it; the last change (when and by whom) is audit data and
  read-only. An account mapping travels under its role key (``CASH_BANK``). The
  settings history shows a changed value only to a caller who may read that
  setting.

The customer master and the bank account master are open to every role until a
school closes a field, apart from the account number:

* ``finance.customer``: a customer's code and name identify it wherever it is
  picked, printed or named in a message and carry no switch. Its billing email,
  phone and address, its receivable account, opening balance, source
  record (type and reference) and whether it is active are typed in, each under
  its own switch; the date it was created is read-only. What its account holds
  is worked out and read-only: the balance (with the signed figures that add up
  to it, such as each open bill's balance and a statement line's running
  balance), the account status, what it has paid, how many bills are open, how
  many customers it pays for, and the lists of open bills, open debit notes,
  transactions and statement lines. The billing email also travels as the
  recipient of a document email, in the preview, the delivery history and the
  message that says where a send went. A bill-to block printed on an invoice,
  receipt or statement is the customer's own record of who was billed, so it is a
  declared system surface (:data:`vs_rbac.field_enforcement.SYSTEM_SURFACES`).
  The opening import writes bills and not the customer record, and the record a
  school's billing contact is mirrored into on CodeX's own books is written
  without a request, so neither is judged.
* ``finance.bankaccount`` (the master): an account's name identifies it and
  carries no switch. The bank, the bank's code at the payment provider (set by
  the payments subaccount endpoint, so read-only here), the currency, the
  ledger account, whether it is active, the main operating account
  and the fee collection account are typed in, each under its own switch. The
  balance on the books, the bank's statement balance, the reconciliation
  progress, the recent ledger activity and the provider subaccount are worked
  out and read-only. A reconciliation run restates the two balances, so it is
  read under their switches, and the difference between them needs both.

Bank documents, the split of a shared bank account, payments from a payer and
petty cash returns carry resources of their own, all open to every role until a
school closes a field:

* ``finance.banktransaction`` and ``finance.banktransfer``: money in or out of
  a bank account, and money between two of a branch's own accounts. The bank
  account (or the two accounts of a transfer), the amount, the date, the
  narration and the reference are typed in; the status and
  where the document stands with its approval route and the journal are
  produced by the books and read-only. A
  correction of a draft is judged by what it changes: a form that sends a
  figure back exactly as stored writes nothing to it.
* ``finance.bankaccount`` (the branch split): the one-time cutover of a bank
  account shared by several branches into one account per branch. The split
  date, the agreement reference, the way differences are treated, and each
  branch's share (its agreed opening balance and the code and name of
  its new ledger account) are typed in. The balance of the shared ledger, the
  part not yet given a branch, each branch's own balance, the accounts opened,
  their journals and the debts between branches are worked out and read-only.
  A debt between branches is an inter-branch document, so its figures are also
  filtered by the ``finance.interbranch`` switches. The account number keeps
  its switch.
* ``finance.payer_link``: who pays for whom. The payer, the customer paid
  for, whether the link is active, and the kind and reference of the record
  that made it are one switch each. A link is not a customer master record, so
  it has a resource of its own and its names (``customer``, ``is_active``) never
  compete with the customer master's.
* ``finance.payer_payment``: one payment split into a receipt per customer. The
  payer, the receiving bank account, the amount, the date, the method, the
  reference, the narration, the way it is split and each customer's share are
  typed in; the status and what each
  share settled (its kind, credit, bills and receipt) are read-only. A share
  carries the amount that a hidden total withholds, so the credit and bill
  figures that would restate it go with it. An ordinary receipt keeps the
  switches of ``finance.payment``.
* ``finance.pettycash_return``: the count of a tin and the cash banked back
  from it. The cash counted, the new float, the cash banked, the bank account,
  the date, who counted, the reason for a difference, the narration and the
  reference are typed in; the books' balance, the difference and what the tin
  keeps, the status, the journal and
  who raised it are read-only. The fund a return belongs to is an identifying
  label, as a bank account's name is, and has no switch of its own; a fund
  keeps the switches of ``finance.pettycash``.

These three are fields-only resources: they carry switches and no permission
keys of their own. The endpoints that reach them are guarded by
``finance.customer.view`` (links), ``finance.payment.view`` (payer payments) and
``finance.pettycash.view`` (returns), and ``seed_finance_permissions`` mints the
resources.

``finance.expenseclaim`` carries one switch so far, the claimant, open to every
role until a school closes it. Who a claim reimburses is one fact however it is
recorded: a member of staff picked from the staff list (``claimant_id``, with
their name copied into ``claimant_name``) or a name typed for somebody who is
not on it, such as a parent volunteer. Both travel under the one switch, as
does the ``claimant`` body key that picks the person (a write alias), the
claimant's email in the claims export, the claimant named on the claim's
approval card and on the spending dashboard's oldest claims, and a search of the
claims list by claimant. The switch is not open on create: a claim cannot be
raised without a claimant, so a role that may not write it raises no claims and
is not offered the New claim form.

The finance dashboards, the month-end close, the sealed figures and the year
archive present figures the books work out and nobody types. Every field below
is therefore read-only, and open to every role until a school closes it, so a
deployment blanks nothing. Each switch hides one whole card, block, trend or
exception list, with every key the block travels under; a block a role's
permission keys already withhold stays withheld whatever the switch says.

* ``finance.dashboard``: the three dashboard tabs (overview, receivables and
  collections, cash and spend) in one fields-only resource with a group per tab.
  The tabs share one resource because two of their cards, collections and
  who-owes-and-who-is-late, are the same card on two tabs and one switch hides
  it on both, and because a card on one tab is often restated on another (the
  tax return due appears on the overview's attention list and on the cash tab).
  A list that restates another card shows an item only to a role who may read
  the card it restates: a bank-lines item needs Bank balances, a tax item needs
  Tax owed, and so on, so hiding a card cannot be undone by reading the same
  figure off a neighbouring list.
* ``finance.period``: where a month stands in its close (its status, when it
  closed and each branch's state), the progress of its checklist, and each
  close check's result. A check names the figures it found, so it is read under
  its own switch. The checks that restate a figure another resource guards (due
  deferred income, the sealed figures, what branches owe each other) are shown
  only to a role who may also read that figure. A refusal to close names the
  failed checks the caller may read, and no others.
* ``finance.seal``: the result of verifying the sealed figures of closed
  periods and years: the verdict, each seal's identity, checksum and line
  counts, and the balances that moved.
* ``finance.fiscalyear``: a year's close state, whether it has been put away
  (archived) and the net result its close carried to equity.

``finance.dashboard`` carries no permission key of its own: the dashboards are
open to anyone working in finance or payments, and ``seed_finance_permissions``
mints the resource. ``finance.report.view`` is the key whose scope its fields
follow.
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


def _bank_document_fields(*, account_label, account_name, date_name, date_label, account_api,
                          extra):
    """The fields a bank transaction and a transfer between own accounts share.

    The two documents differ in their accounts (a bank account and the other
    side of the entry, or the sending and the receiving account) and in the name
    of their date; everything else about them is the same. ``extra`` carries the
    fields the document has beyond the common ones.
    """
    return (
        FieldSpec("status", "Status", group="Identity", writable=False, scope=_TENANT,
                  sort_order=20, api_names=("status", "approval_state", "approval_returned")),
        FieldSpec(account_name, account_label, group="Banking", scope=_TENANT,
                  sort_order=410, api_names=account_api),
        FieldSpec("amount", "Amount", group="Money", scope=_TENANT, sort_order=200),
        FieldSpec(date_name, date_label, group="Money", scope=_TENANT, sort_order=220),
        FieldSpec("narration", "Narration", group="Details", scope=_TENANT, sort_order=300),
        FieldSpec("reference", "Reference", group="Details", scope=_TENANT, sort_order=310),
        FieldSpec("journal", "Journal", group="Outcome", writable=False, scope=_TENANT,
                  sort_order=700, api_names=("journal_id",)),
        *extra,
    )


def _figure(name, label, group, sort_order, description="", api_names=()):
    """A read-only, open-by-default figure the books work out.

    Every dashboard card, close check result and verification result is
    server-owned, so it carries a Read switch and no Write switch, and starts
    open to every role.
    """
    return FieldSpec(name, label, group=group, description=description, writable=False,
                     scope=_TENANT, sort_order=sort_order, api_names=api_names)


#: Group names of the three dashboard tabs.
OVERVIEW = "Overview"
RECEIVABLES_TAB = "Receivables and collections"
SPEND_TAB = "Cash, spend and compliance"


def _dashboard_fields():
    """One Read switch per dashboard card, block, trend and exception list."""
    o, r, s = OVERVIEW, RECEIVABLES_TAB, SPEND_TAB
    return (
        _figure("kpi_cash_position", "Cash position card", o, 10,
                "Cash and bank balance with its monthly sparkline.",
                api_names=("cash_position",)),
        _figure("kpi_receivables", "Receivables card", o, 20,
                "What customers owe, with its monthly sparkline.",
                api_names=("receivables",)),
        _figure("kpi_payables", "Payables card", o, 30,
                "What is owed to vendors, with its monthly sparkline.",
                api_names=("payables",)),
        _figure("kpi_net_income", "Net income this year card", o, 40,
                "Net income so far this fiscal year, with its monthly sparkline.",
                api_names=("net_income_ytd",)),
        _figure("collections", "Billed and collected", o, 50,
                "Fees billed and collected in the window and the share collected. "
                "Shown on the overview and on the receivables tab."),
        _figure("channels", "How money was received", o, 60,
                "Money received in the window by how it was paid."),
        _figure("branch_comparison", "Branch comparison", o, 70,
                "Billed, collected and overdue for each branch.",
                api_names=("branches",)),
        _figure("bank_accounts", "Bank balances and reconciliation", o, 80,
                "Each bank account's balance and unmatched statement lines."),
        _figure("revenue_vs_budget", "Revenue against budget", o, 90,
                "Income and spending against the school's plan."),
        _figure("budget_lines", "Budget lines", o, 100,
                "The income line and the three biggest spending lines against the plan. "
                "Shown only to a role who also reads Revenue against budget, because its "
                "first line restates it.",
                api_names=("budget",)),
        _figure("top_payers", "Payers owing the most", o, 110,
                "The payers with the most past their due date."),
        _figure("receivables_summary", "Who owes and who is late", o, 120,
                "Counts of payers who owe, who are overdue and who fall due this week. "
                "Shown on the overview and on the receivables tab."),
        _figure("ar_aging", "Receivables by age", o, 130,
                "What customers owe, grouped by how long it has been due."),
        _figure("trend", "Invoiced and collected by month", o, 140,
                "Twelve months of fees invoiced against money collected."),
        _figure("top_overdue", "Largest overdue invoices", o, 150),
        _figure("vendor_due", "Vendor bills due this week", o, 160),
        _figure("payables_due", "Vendor bills falling due", o, 170,
                "How many vendor bills fall due in the next 30 days and how many are late."),
        _figure("approvals", "Spend approvals waiting", o, 180,
                "Purchase requisitions, orders and vendor bills waiting to be approved."),
        _figure("attention", "Things that need attention", o, 190,
                "The list of things someone should act on. An item is shown only to a "
                "role who may read the card it restates: bank lines need Bank balances, "
                "tax needs Tax owed, payment plans need Payment plans, unapplied receipts "
                "need Credit held for payers and low floats need Petty cash floats."),
        _figure("upcoming", "Money due in the next 30 days", o, 200,
                "Payroll, instalments, vendor bills and tax due. An item is shown only to a "
                "role who may read the card it restates."),
        _figure("close_progress", "Month close progress", o, 210,
                "How far the month is from closing. Each check is shown only to a role who "
                "may read that check's result."),
        _figure("fiscal_runway", "Fiscal calendar runway", o, 220,
                "How long the fiscal calendar lasts before nothing can be posted."),
        _figure("recent_journals", "Recent journals", o, 230),

        _figure("days_to_pay", "Days to pay", r, 300,
                "The usual number of days from invoice to final payment."),
        _figure("credit_held", "Credit held for payers", r, 310,
                "Money payers have with the school that no invoice has used yet.",
                api_names=("credit",)),
        _figure("collection_curve", "Collection curve", r, 320,
                "Week by week share of the window's fees paid, against last window and the target.",
                api_names=("curve",)),
        _figure("payment_plans", "Payment plans", r, 330,
                "Plans on track and behind, and the next instalments.",
                api_names=("plans",)),
        _figure("groups", "Collections by group", r, 340,
                "Billed and collected for each group of payers, such as a class."),
        _figure("dunning", "Reminders and their results", r, 350),
        _figure("concessions", "Concessions given", r, 360),
        _figure("adjustments", "Credit notes, refunds and write-offs", r, 370),
        _figure("largest_balances", "Largest balances", r, 380,
                "The payers owing the most, by age, and the last thing done about it.",
                api_names=("largest",)),

        _figure("runway", "Cash runway", s, 400,
                "Months of cash at the recent average monthly outflow."),
        _figure("cash_movement", "Cash movement", s, 410,
                "Opening cash, each kind of money in and out, and cash today."),
        _figure("operating_spend", "Spending against the last window", s, 420,
                api_names=("spend",)),
        _figure("spending", "Spending breakdown", s, 430,
                "Spending by cost centre, or by expense account."),
        _figure("reconciliation", "Bank reconciliation", s, 440),
        _figure("unmatched", "Unmatched bank lines", s, 450),
        _figure("budgets", "Budgets used", s, 460),
        _figure("payroll", "Payroll run", s, 470,
                "The latest payroll run due and what it is made of."),
        _figure("expense_claims", "Expense claims", s, 480, api_names=("claims",)),
        _figure("petty_cash", "Petty cash floats", s, 490),
        _figure("tax_owed", "Tax owed", s, 500,
                "Everything owed on open returns and the next one due."),
        _figure("tax_calendar", "Tax calendar", s, 510),
        _figure("fixed_assets", "Fixed assets", s, 520,
                "What the register is worth by category and the window's depreciation.",
                api_names=("assets",)),
    )


#: Finance's own close checks, each registered under ``finance.period`` by its
#: machine name. A check another module contributes is read under
#: ``other_checks`` (:mod:`vs_finance.close_access`).
CLOSE_CHECKS = (
    ("trial_balance_balanced", "Trial balance agrees"),
    ("no_draft_journals", "Draft journals left in the month"),
    ("ar_reconciled", "Receivables reconciled"),
    ("depreciation_posted", "Depreciation posted"),
    ("earlier_periods_closed", "Earlier months closed"),
    ("deferred_income_released", "Deferred income released"),
    ("inter_branch_balanced", "Inter-branch balances agree"),
    ("sealed_figures_unchanged", "Closed figures unchanged"),
    ("depreciation_posted_for_year", "The year's depreciation posted"),
)

#: The check names other modules contribute to the close today.
OTHER_CHECK_NAMES = ("ap_reconciled", "grir_explained")


def _period_fields():
    """The close state of a month, its checklist progress and each check's result."""
    checks = tuple(
        _figure(name, label, "Close checks", 100 + 10 * index,
                "What this check found, and whether it passed.")
        for index, (name, label) in enumerate(CLOSE_CHECKS)
    )
    return (
        _figure("close_state", "Close state of the month", "Close state", 10,
                "Whether the month is open, soft-closed, closed or locked, when it closed "
                "and where each branch stands.",
                api_names=("status", "closed_at", "branch_states", "branch_period")),
        _figure("checklist_progress", "Checklist progress", "Close state", 20,
                "Whether the checklist passed and how many checks are done.",
                api_names=("passed", "done", "total")),
        *checks,
        _figure("other_checks", "Checks added by other modules", "Close checks", 300,
                "Payables reconciled and goods received not billed, and any check another "
                "module adds to the close.",
                api_names=OTHER_CHECK_NAMES),
    )


def register():
    """Publish the finance field declarations to the Field Access registry."""
    register_fields(
        "finance", "budget",
        surfaces=(
            "vs_finance.serializers.BudgetSerializer",
            "vs_finance.serializers.BudgetLineSerializer",
            "vs_finance.budget_access.BudgetAccessSerializer",
        ),
        fields=(
            FieldSpec("name", "Budget name", group="Plan", scope=_TENANT,
                      sort_order=10, api_names=("name", "budget_name")),
            FieldSpec("fiscal_year", "Fiscal year", group="Plan", scope=_TENANT,
                      sort_order=20, open_on_create=True),
            FieldSpec("status", "Status", group="Plan", scope=_TENANT,
                      sort_order=30, writable=False, api_names=("status", "is_locked")),
            FieldSpec("approved_at", "Approved on", group="Plan", scope=_TENANT,
                      sort_order=40, writable=False),
            FieldSpec("lines", "Budget lines (account, cost centre, period)",
                      group="Plan", scope=_TENANT, sort_order=50),
            FieldSpec("amount", "Planned amount", group="Figures", scope=_TENANT,
                      sort_order=100, sensitive=True,
                      api_names=("amount", "budget", "budgeted_total", "budget_total",
                                 "total_budget", "plan")),
            FieldSpec("actual", "Actual against the plan", group="Figures",
                      scope=_TENANT, sort_order=110, writable=False,
                      api_names=("actual", "actual_ytd", "actual_total", "total_actual",
                                 "used")),
            FieldSpec("variance", "Variance and share used", group="Figures",
                      scope=_TENANT, sort_order=120, writable=False,
                      api_names=("variance", "total_variance", "consumed_pct",
                                 "pct", "pct_of_plan", "delta_pct")),
        ),
    )
    register_fields(
        "finance", "report",
        surfaces=("vs_finance.report_access.ReportSectionsSerializer",),
        fields=tuple(
            FieldSpec(name, label, group=group, scope=_TENANT, writable=False,
                      sort_order=order)
            for order, (name, label, group) in enumerate((
                ("trial_balance", "Trial balance", "Trial balance"),
                ("income_revenue", "Revenue lines", "Income statement"),
                ("income_expense", "Expense lines", "Income statement"),
                ("income_net", "Net income", "Income statement"),
                ("income_budget", "Budget and variance columns", "Income statement"),
                ("income_prior_year", "Prior-year column", "Income statement"),
                ("balance_assets", "Assets", "Balance sheet"),
                ("balance_liabilities", "Liabilities", "Balance sheet"),
                ("balance_equity", "Equity and current-year earnings", "Balance sheet"),
                ("balance_check", "Whether the balance sheet balances", "Balance sheet"),
                ("cashflow_cash", "Opening and closing cash", "Cash flow"),
                ("cashflow_activities", "Cash by activity", "Cash flow"),
                ("equity_changes", "Changes in equity", "Changes in equity"),
                ("analytics_slice", "Cost and dimension analysis", "Analysis"),
                ("aging_customers", "Amounts owed by each customer", "Receivables"),
                ("aging_totals", "Receivables by age, totals", "Receivables"),
                ("ar_reconciliation", "Receivables reconciliation", "Receivables"),
            ), start=1)
        ),
    )
    register_fields(
        "finance",
        "customer",
        surfaces=(
            "vs_finance.serializers.CustomerSerializer",
            "vs_finance.serializers.CustomerListSerializer",
            "vs_finance.serializers.CustomerAccountSerializer",
            "vs_finance.serializers.CustomerStandingSerializer",
            "vs_finance.serializers.CustomerSummaryTotalsSerializer",
            "vs_finance.serializers.FinanceDocumentDeliverySerializer",
            "vs_finance.serializers.DocumentEmailPreviewSerializer",
        ),
        fields=(
            FieldSpec("billing_email", "Billing email", group="Contact", scope=_TENANT,
                      sort_order=10, api_names=("billing_email", "recipients"),
                      open_on_create=True),
            FieldSpec("billing_phone", "Billing phone", group="Contact", scope=_TENANT,
                      sort_order=20, open_on_create=True),
            FieldSpec("billing_address", "Billing address", group="Contact", scope=_TENANT,
                      sort_order=30),
            FieldSpec("receivable_account", "Receivable account", group="Account",
                      scope=_TENANT, sort_order=110,
                      api_names=("receivable_account_id", "receivable_account_code",
                                 "receivable_account_name")),
            FieldSpec("opening_balance", "Opening balance", group="Account", scope=_TENANT,
                      sort_order=120, api_names=("opening_balance", "opening_balance_naira"),
                      open_on_create=True),
            FieldSpec("source_type", "Source record type", group="Account", scope=_TENANT,
                      sort_order=130),
            FieldSpec("source_id", "Source record reference", group="Account", scope=_TENANT,
                      sort_order=140),
            FieldSpec("is_active", "Active", group="Account", scope=_TENANT, sort_order=150),
            FieldSpec("created_at", "Created", group="Account", writable=False, scope=_TENANT,
                      sort_order=160),
            FieldSpec("balance", "Balance", group="Standing", writable=False, scope=_TENANT,
                      sort_order=200,
                      api_names=("balance", "balance_naira", "current_balance", "receivable",
                                 "on_credit")),
            FieldSpec("account_status", "Account status", group="Standing", writable=False,
                      scope=_TENANT, sort_order=210,
                      api_names=("account_status", "overdue", "status_counts")),
            FieldSpec("lifetime_paid", "Paid to date", group="Standing", writable=False,
                      scope=_TENANT, sort_order=220),
            FieldSpec("open_invoice_count", "Open invoices", group="Standing", writable=False,
                      scope=_TENANT, sort_order=230),
            FieldSpec("pays_for_count", "Customers paid for", group="Standing", writable=False,
                      scope=_TENANT, sort_order=240),
            FieldSpec("open_invoices", "Open invoice list", group="Account history",
                      writable=False, scope=_TENANT, sort_order=300),
            FieldSpec("open_debit_notes", "Open debit note list", group="Account history",
                      writable=False, scope=_TENANT, sort_order=310),
            FieldSpec("transactions", "Transactions", group="Account history", writable=False,
                      scope=_TENANT, sort_order=320),
            FieldSpec("statement", "Statement of account", group="Account history",
                      writable=False, scope=_TENANT, sort_order=330),
        ),
        write_aliases={
            "receivable_account": "receivable_account",
            "opening_date": "opening_balance",
            "opening_branch": "opening_balance",
        },
    )
    register_fields(
        "finance",
        "bankaccount",
        surfaces=(
            "vs_finance.serializers.BankAccountSerializer",
            "vs_finance.serializers.BankAccountMetricsSerializer",
            "vs_finance.serializers.BankAccountActivitySerializer",
            "vs_finance.serializers.BankAccountDetailSerializer",
            "vs_finance.serializers.BankReconciliationSerializer",
            "vs_finance.views_ops.banking._BankAccountBranchSplitSerializer",
            "vs_finance.views_ops.banking._BankBranchAllocationSerializer",
            "vs_finance.views_ops.banking.BankSplitPreviewSerializer",
            "vs_finance.views_ops.banking.BankSplitResultSerializer",
        ),
        fields=(
            FieldSpec("account_number", "Account number", group="Banking",
                      sensitive=True, scope=_TENANT, sort_order=10),
            FieldSpec("bank_name", "Bank", group="Banking", scope=_TENANT, sort_order=20),
            FieldSpec("bank_code", "Bank code at the payment provider", group="Banking",
                      scope=_TENANT, sort_order=30, api_names=("settlement_bank_code",)),
            FieldSpec("currency", "Currency", group="Account setup", scope=_TENANT,
                      sort_order=40),
            FieldSpec("ledger_account", "Ledger account", group="Account setup",
                      scope=_TENANT, sort_order=50, open_on_create=True,
                      api_names=("gl_account", "gl_account_name", "gl_account_id")),
            FieldSpec("is_active", "Active", group="Account setup", scope=_TENANT,
                      sort_order=70),
            FieldSpec("is_primary", "Main operating account", group="Account setup",
                      scope=_TENANT, sort_order=80),
            FieldSpec("is_primary_collection", "Fee collection account", group="Account setup",
                      scope=_TENANT, sort_order=90),
            FieldSpec("gateway_subaccount", "Payment provider subaccount", group="Banking",
                      writable=False, scope=_TENANT, sort_order=35,
                      api_names=("gateway_subaccount_code", "gateway_subaccount_provider")),
            FieldSpec("book_balance", "Book balance", group="Balances", writable=False,
                      scope=_TENANT, sort_order=300,
                      api_names=("book_balance", "book_balance_naira", "running_balance")),
            FieldSpec("statement_balance", "Statement balance", group="Balances",
                      writable=False, scope=_TENANT, sort_order=310,
                      api_names=("statement_balance", "statement_balance_naira",
                                 "unreconciled_diff", "difference", "difference_naira")),
            FieldSpec("reconciliation", "Reconciliation progress", group="Balances",
                      writable=False, scope=_TENANT, sort_order=320,
                      api_names=("unreconciled_count", "last_reconciled_at")),
            FieldSpec("recent_activity", "Recent ledger activity", group="Balances",
                      writable=False, scope=_TENANT, sort_order=330,
                      api_names=("transactions",)),
            FieldSpec("split_date", "Split date", group="Branch split", scope=_TENANT,
                      sort_order=100),
            FieldSpec("agreement_reference", "Agreement reference", group="Branch split",
                      scope=_TENANT, sort_order=110),
            FieldSpec("difference_treatment", "How differences are treated",
                      group="Branch split", scope=_TENANT, sort_order=120),
            FieldSpec("split_opening_balance", "Agreed opening balance", group="Branch split",
                      scope=_TENANT, sort_order=140, api_names=("opening_balance",)),
            FieldSpec("split_ledger_code", "New ledger account code", group="Branch split",
                      scope=_TENANT, sort_order=150, api_names=("ledger_account_code",)),
            FieldSpec("split_ledger_name", "New ledger account name", group="Branch split",
                      scope=_TENANT, sort_order=155, api_names=("ledger_account_name",)),
            FieldSpec("legacy_balance", "Balance of the shared account", group="Split result",
                      writable=False, scope=_TENANT, sort_order=200),
            FieldSpec("unbranched_balance", "Part not yet given a branch", group="Split result",
                      writable=False, scope=_TENANT, sort_order=210),
            FieldSpec("branch_balances", "Each branch's own balance", group="Split result",
                      writable=False, scope=_TENANT, sort_order=220, api_names=("branches",)),
            FieldSpec("branch_accounts", "Branch accounts opened", group="Split result",
                      writable=False, scope=_TENANT, sort_order=230,
                      api_names=("bank_accounts",)),
            FieldSpec("split_journals", "Split journals", group="Split result",
                      writable=False, scope=_TENANT, sort_order=240, api_names=("journal_ids",)),
            FieldSpec("branch_debts", "Debts between branches", group="Split result",
                      writable=False, scope=_TENANT, sort_order=250,
                      api_names=("inter_branch_transfers",)),
        ),
    )
    register_fields(
        "finance",
        "banktransaction",
        surfaces=("vs_finance.views_ops.banking.BankTransactionSerializer",),
        fields=_bank_document_fields(
            account_label="Bank account", account_name="bank_account",
            date_name="transaction_date", date_label="Transaction date",
            account_api=("bank_account_id", "bank_account_name"),
            extra=(
                FieldSpec("direction", "Money in or out", group="Money", scope=_TENANT,
                          sort_order=210),
                FieldSpec("counter_account", "Other side of the entry", group="Banking",
                          scope=_TENANT, sort_order=420,
                          api_names=("counter_account_id", "counter_account_code",
                                     "counter_account_name")),
            ),
        ),
        write_aliases={"bank_account": "bank_account", "counter_account": "counter_account"},
    )
    register_fields(
        "finance",
        "banktransfer",
        surfaces=("vs_finance.views_ops.banking.BankTransferSerializer",),
        fields=_bank_document_fields(
            account_label="Sending account", account_name="from_account",
            date_name="transfer_date", date_label="Transfer date",
            account_api=("from_account_id", "from_account_name"),
            extra=(
                FieldSpec("to_account", "Receiving account", group="Banking", scope=_TENANT,
                          sort_order=420, api_names=("to_account_id", "to_account_name")),
            ),
        ),
        write_aliases={"from_account": "from_account", "to_account": "to_account"},
    )
    register_fields(
        "finance",
        "payrollrun",
        surfaces=(
            "vs_finance.serializers.PayrollRunSerializer",
            "vs_finance.serializers.PayrollLineSerializer",
            "vs_finance.serializers.PayrollLinePaymentSerializer",
        ),
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
            FieldSpec("approval", "Approval status", group="Approval", writable=False,
                      scope=_TENANT, sort_order=65,
                      api_names=("approval_state", "approval_returned")),
            FieldSpec("pay_state", "Payment status", group="Payment", writable=False,
                      scope=_TENANT, sort_order=70,
                      api_names=("pay_state", "pay_state_label", "paid_via", "paid_via_label")),
            FieldSpec("pay_note", "Payment note", group="Payment", writable=False,
                      scope=_TENANT, sort_order=80),
            FieldSpec("payout_reference", "Payment reference", group="Payment",
                      writable=False, scope=_TENANT, sort_order=90),
        ),
    )
    register_fields(
        "finance",
        "staff_bank",
        surfaces=(
            "vs_finance.serializers.StaffBankAccountSerializer",
            "vs_finance.serializers.StaffWithoutBankAccountSerializer",
        ),
        fields=(
            FieldSpec("employee", "Staff member", group="Identity", writable=False,
                      scope=_TENANT, sort_order=10, api_names=("employee_id", "employee_name")),
            FieldSpec("bank", "Bank", group="Banking", sensitive=True, scope=_TENANT,
                      sort_order=20, api_names=("bank_code", "bank_name")),
            FieldSpec("account_number", "Account number", group="Banking", sensitive=True,
                      scope=_TENANT, sort_order=30),
            FieldSpec("resolved_account_name", "Account holder named by the bank",
                      group="Banking", sensitive=True, writable=False, scope=_TENANT,
                      sort_order=40),
            FieldSpec("verification_state", "Verification status", group="Verification",
                      writable=False, scope=_TENANT, sort_order=50,
                      api_names=("verification_state", "verification_state_label",
                                 "verification_note", "verified_at", "is_payable",
                                 "can_confirm")),
            FieldSpec("changed_by", "Last changed by", group="Verification", writable=False,
                      scope=_TENANT, sort_order=60, api_names=("changed_by_name", "updated_at")),
            FieldSpec("confirmation", "Confirmed by and when", group="Verification",
                      writable=False, scope=_TENANT, sort_order=70,
                      api_names=("confirmed_by_name", "confirmed_at")),
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
    register_fields(
        "finance",
        "interbranch",
        surfaces=(
            "vs_finance.views_ops.interbranch.InterBranchTransferSerializer",
            "vs_finance.views_ops.interbranch.HeldReceiptSerializer",
            "vs_finance.views_ops.interbranch.RechargeSerializer",
            "vs_finance.views_ops.interbranch.SharedCostRuleSerializer",
        ),
        fields=(
            FieldSpec("kind", "Transfer kind", group="Identity", writable=False,
                      scope=_TENANT, sort_order=20, api_names=("kind", "kind_label")),
            FieldSpec("status", "Status", group="Identity", writable=False,
                      scope=_TENANT, sort_order=30,
                      api_names=("status", "stage", "approval_state", "approval_returned")),
            FieldSpec("name", "Rule name", group="Shared cost rule", scope=_TENANT,
                      sort_order=40),
            FieldSpec("treatment", "Cost treatment", group="Shared cost rule",
                      scope=_TENANT, sort_order=50),
            FieldSpec("is_active", "Active", group="Shared cost rule", scope=_TENANT,
                      sort_order=60),
            FieldSpec("amount", "Amount", group="Money", scope=_TENANT, sort_order=200),
            FieldSpec("transaction_date", "Transaction date", group="Money",
                      scope=_TENANT, sort_order=210,
                      api_names=("transfer_date", "receipt_date", "recharge_date")),
            FieldSpec("purpose", "Purpose or narration", group="Details", scope=_TENANT,
                      sort_order=300, api_names=("purpose", "narration")),
            FieldSpec("reference", "Reference", group="Details", scope=_TENANT,
                      sort_order=310),
            FieldSpec("repay_by", "Repay by", group="Details", scope=_TENANT,
                      sort_order=320),
            FieldSpec("method", "Payment method", group="Details", scope=_TENANT,
                      sort_order=330),
            FieldSpec("source_bank_account", "Source bank account", group="Banking",
                      scope=_TENANT, sort_order=400,
                      api_names=("from_bank_account_id",
                                 "from_bank_account_name",
                                 "bank_account_id", "bank_account_name")),
            FieldSpec("destination_bank_account", "Destination bank account",
                      group="Banking", scope=_TENANT, sort_order=410,
                      api_names=("to_bank_account_id",
                                 "to_bank_account_name")),
            FieldSpec("customer", "Customer", group="Parties", scope=_TENANT,
                      sort_order=500,
                      api_names=("customer_id", "customer_name")),
            FieldSpec("expense_account", "Expense account", group="Shared cost",
                      scope=_TENANT, sort_order=600,
                      api_names=("expense_account_id", "expense_account_code")),
            FieldSpec("rule", "Shared cost rule", group="Shared cost", scope=_TENANT,
                      sort_order=610, api_names=("rule_id",)),
            FieldSpec("basis", "Sharing basis", group="Shared cost", scope=_TENANT,
                      sort_order=620),
            FieldSpec("shares", "Branch shares", group="Shared cost", scope=_TENANT,
                      sort_order=630),
            FieldSpec("recharge_shares", "Recharge shares", group="Shared cost",
                      writable=False, scope=_TENANT, sort_order=640, api_names=("lines",)),
            FieldSpec("decline_reason", "Decline reason", group="Outcome", scope=_TENANT,
                      sort_order=700),
            FieldSpec("arrival_date", "Arrival date", group="Outcome", scope=_TENANT,
                      sort_order=710),
            FieldSpec("activity_times", "Request and transfer times", group="Outcome",
                      writable=False, scope=_TENANT, sort_order=720,
                      api_names=("requested_at", "sent_at", "received_at", "declined_at")),
            FieldSpec("journals", "Journal links", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=730),
            FieldSpec("moved_items", "Moved documents", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=740),
            FieldSpec("net_owed", "Net owed", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=750),
            FieldSpec("forwarded_by", "Forward transfer", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=760),
            FieldSpec("related_documents", "Related documents", group="Outcome",
                      writable=False, scope=_TENANT, sort_order=770,
                      api_names=("held_receipt_id", "receipt_id", "recharge_id",
                                 "adjustment_entry_id", "journal_id")),
        ),
        write_aliases={
            "from_bank_account": "source_bank_account",
            "bank_account": "source_bank_account",
            "to_bank_account": "destination_bank_account",
            "customer": "customer",
            "expense_account": "expense_account",
            "rule": "rule",
            "weights": "shares",
            "reason": "decline_reason",
            "date": "transaction_date",
            "move_date": "transaction_date",
        },
    )
    register_fields(
        "finance",
        "deferredincome",
        surfaces=(
            "vs_finance.views_accruals.DeferredIncomeSummarySerializer",
            "vs_finance.views_accruals.DeferredIncomeReleaseRowSerializer",
            "vs_finance.views_accruals.DeferredIncomeRunSerializer",
            "vs_finance.views_accruals.DeferredIncomePostedReleaseSerializer",
            "vs_finance.views_accruals.DeferredIncomeReversalSerializer",
        ),
        fields=(
            FieldSpec("pending_balance", "Waiting to be released", group="Balances",
                      writable=False, scope=_TENANT, sort_order=10, api_names=("pending",)),
            FieldSpec("released_total", "Released so far", group="Balances",
                      writable=False, scope=_TENANT, sort_order=20, api_names=("released",)),
            FieldSpec("monthly_releases", "Due by month", group="Balances",
                      writable=False, scope=_TENANT, sort_order=30, api_names=("by_month",)),
            FieldSpec("period", "Accounting period", group="Release", scope=_TENANT,
                      sort_order=110,
                      api_names=("period", "period_id", "period_name", "period_label",
                                 "period_status", "branch_period_status")),
            FieldSpec("release_date", "Release date", group="Release", scope=_TENANT,
                      sort_order=120, api_names=("date", "month", "up_to")),
            FieldSpec("release_amount", "Release amount", group="Release", writable=False,
                      scope=_TENANT, sort_order=130, api_names=("amount",)),
            FieldSpec("journal", "Journal", group="Release", writable=False, scope=_TENANT,
                      sort_order=140, api_names=("journal_id", "journal_number")),
            FieldSpec("reversal_state", "Reversal state", group="Reversal", writable=False,
                      scope=_TENANT, sort_order=200,
                      api_names=("reversed", "reversed_at", "can_reverse")),
            FieldSpec("reverse_blocked_reason", "Why an undo is blocked", group="Reversal",
                      writable=False, scope=_TENANT, sort_order=210),
        ),
    )
    register_fields(
        "finance",
        "provision",
        surfaces=(
            "vs_finance.serializers.DoubtfulDebtProvisionSerializer",
            "vs_finance.serializers.DoubtfulDebtProvisionLineSerializer",
        ),
        fields=(
            FieldSpec("status", "Status", group="Provision run", writable=False,
                      scope=_TENANT, sort_order=20,
                      api_names=("status", "approval_state", "approval_returned",
                                 "approval_required")),
            FieldSpec("as_of", "Provision date", group="Provision run", scope=_TENANT,
                      sort_order=30),
            FieldSpec("narration", "Narration", group="Provision run", scope=_TENANT,
                      sort_order=40),
            FieldSpec("required_total", "Total required", group="Totals", writable=False,
                      scope=_TENANT, sort_order=100),
            FieldSpec("movement_total", "Total movement", group="Totals", writable=False,
                      scope=_TENANT, sort_order=110),
            FieldSpec("policy_snapshot", "Policy snapshot", group="Totals", writable=False,
                      scope=_TENANT, sort_order=120),
            FieldSpec("created_at", "Raised at", group="Provision run", writable=False,
                      scope=_TENANT, sort_order=50),
            FieldSpec("required", "Required", group="Branch lines", writable=False,
                      scope=_TENANT, sort_order=210),
            FieldSpec("current", "Current provision", group="Branch lines", writable=False,
                      scope=_TENANT, sort_order=220),
            FieldSpec("movement", "Movement", group="Branch lines", writable=False,
                      scope=_TENANT, sort_order=230),
            FieldSpec("bands", "Age bands", group="Branch lines", writable=False,
                      scope=_TENANT, sort_order=240),
            FieldSpec("journal", "Journal", group="Branch lines", writable=False,
                      scope=_TENANT, sort_order=250, api_names=("journal_id",)),
        ),
    )
    register_fields(
        "finance",
        "deposit",
        surfaces=(
            "vs_finance.serializers.CustomerDepositSerializer",
            "vs_finance.views_accruals.DepositReleaseNoteSerializer",
            "vs_finance.views_accruals.DepositForfeitureSerializer",
            "vs_finance.views_accruals.DepositSkippedSerializer",
        ),
        fields=(
            FieldSpec("customer", "Customer", group="Deposit", writable=False,
                      scope=_TENANT, sort_order=10,
                      api_names=("customer_id", "customer_code", "customer_name", "customer")),
            FieldSpec("invoice", "Invoice", group="Deposit", writable=False, scope=_TENANT,
                      sort_order=30, api_names=("invoice_id", "invoice_number", "invoice")),
            FieldSpec("amount", "Amount", group="Deposit", writable=False, scope=_TENANT,
                      sort_order=40, api_names=("amount", "amount_naira")),
            FieldSpec("status", "Status", group="Deposit", writable=False, scope=_TENANT,
                      sort_order=50),
            FieldSpec("claim_opened_on", "Claim date", group="Deposit", writable=False,
                      scope=_TENANT, sort_order=60),
            FieldSpec("release_note", "Release note", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=100, api_names=("release_note_number",)),
            FieldSpec("forfeiture", "Forfeiture", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=110,
                      api_names=("forfeiture_id", "journal_id")),
        ),
    )
    register_fields(
        "finance",
        "settings",
        surfaces=(
            "vs_finance.views_accruals.ReceivablesPolicySerializer",
            "vs_finance.views_settings.BankingSettingsSerializer",
            "vs_finance.views_settings.DocumentSettingsSerializer",
            "vs_finance.views_settings.CalendarSettingsSerializer",
            "vs_finance.views_settings.PayrollSettingsSerializer",
            "vs_finance.views_settings.AccountMappingsSerializer",
            "vs_finance.views_records.RetentionSettingsSerializer",
        ),
        fields=(
            FieldSpec("revenue_recognition", "Revenue recognition", group="Receivables policy",
                      scope=_TENANT, sort_order=10,
                      api_names=("revenue_recognition", "revenue_recognition_label",
                                 "revenue_recognition_options")),
            FieldSpec("provision_bands", "Provision bands", group="Receivables policy",
                      scope=_TENANT, sort_order=20),
            FieldSpec("deposits_offset_unpaid_bills", "Deposits offset unpaid bills",
                      group="Receivables policy", scope=_TENANT, sort_order=30),
            FieldSpec("unclaimed_deposit_years", "Unclaimed-deposit years",
                      group="Receivables policy", scope=_TENANT, sort_order=40),
            FieldSpec("payer_payment_split", "Payer payment split", group="Receivables policy",
                      scope=_TENANT, sort_order=50,
                      api_names=("payer_payment_split", "payer_payment_split_options")),
            FieldSpec("payer_payment_surplus", "Payer payment surplus",
                      group="Receivables policy", scope=_TENANT, sort_order=60,
                      api_names=("payer_payment_surplus", "payer_payment_surplus_options")),
            FieldSpec("last_changed", "Last changed", group="Receivables policy",
                      writable=False, scope=_TENANT, sort_order=70,
                      api_names=("updated_at", "updated_by")),
            FieldSpec("default_bank_reconciliation_tolerance_days",
                      "Reconciliation date window", group="Banking and cash",
                      scope=_TENANT, sort_order=110),
            FieldSpec("default_group_reconciliation_matches",
                      "Allow grouped automatic matches", group="Banking and cash",
                      scope=_TENANT, sort_order=120),
            FieldSpec("default_receipt_allocation_strategy", "Receipt allocation order",
                      group="Banking and cash", scope=_TENANT, sort_order=130,
                      api_names=("default_receipt_allocation_strategy",
                                 "default_receipt_allocation_strategy_label",
                                 "default_receipt_allocation_strategy_options")),
            FieldSpec("apply_transfers_automatically",
                      "Apply bank transfers to open bills automatically",
                      group="Banking and cash", scope=_TENANT, sort_order=140),
            FieldSpec("petty_cash_low_balance_threshold_bps", "Petty cash low-balance alert",
                      group="Banking and cash", scope=_TENANT, sort_order=150),
        ) + _settings_section_specs(),
    )
    register_fields(
        "finance",
        "payer_link",
        surfaces=("vs_finance.views_payers.PayerLinkSerializer",),
        fields=(
            FieldSpec("payer", "Payer", group="Payer links", scope=_TENANT, sort_order=10),
            FieldSpec("customer", "Linked customer", group="Payer links", scope=_TENANT,
                      sort_order=20),
            FieldSpec("is_active", "Link active", group="Payer links", scope=_TENANT,
                      sort_order=30),
            FieldSpec("source_type", "Source type", group="Payer links", scope=_TENANT,
                      sort_order=40),
            FieldSpec("source_id", "Source reference", group="Payer links", scope=_TENANT,
                      sort_order=50),
        ),
    )
    register_fields(
        "finance",
        "payer_payment",
        surfaces=(
            "vs_finance.views_payers.PayerPaymentSerializer",
            "vs_finance.views_payers.PayerPaymentShareSerializer",
        ),
        fields=(
            FieldSpec("status", "Status", group="Payer payments", writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("payer", "Payer", group="Payer payments", scope=_TENANT, sort_order=30),
            FieldSpec("bank_account", "Receiving bank account", group="Payer payments",
                      scope=_TENANT, sort_order=40),
            FieldSpec("amount", "Amount", group="Payer payments", scope=_TENANT, sort_order=50),
            FieldSpec("payment_date", "Payment date", group="Payer payments", scope=_TENANT,
                      sort_order=60),
            FieldSpec("method", "Payment method", group="Payer payments", scope=_TENANT,
                      sort_order=70),
            FieldSpec("split", "How the payment is split", group="Payer payments",
                      scope=_TENANT, sort_order=80, api_names=("split", "split_label")),
            FieldSpec("reference", "Reference", group="Payer payments", scope=_TENANT,
                      sort_order=90),
            FieldSpec("narration", "Narration", group="Payer payments", scope=_TENANT,
                      sort_order=100),
            FieldSpec("shares", "Customer shares", group="Payer payments", scope=_TENANT,
                      sort_order=110),
            FieldSpec("allocation_results", "What each share settled", group="Payer payments",
                      writable=False, scope=_TENANT, sort_order=130,
                      api_names=("kind", "credit", "outstanding", "bills", "document",
                                 "forwarded_by")),
        ),
    )
    register_fields(
        "finance",
        "pettycash_return",
        surfaces=("vs_finance.serializers.PettyCashReturnSerializer",),
        fields=(
            FieldSpec("status", "Status", group="Petty cash returns", writable=False,
                      scope=_TENANT, sort_order=20,
                      api_names=("status", "approval_state", "approval_returned")),
            FieldSpec("kind", "Kind of return", group="Petty cash returns", writable=False,
                      scope=_TENANT, sort_order=30, api_names=("kind", "kind_label")),
            FieldSpec("return_date", "Return date", group="Petty cash returns", scope=_TENANT,
                      sort_order=60),
            FieldSpec("counted_amount", "Cash counted", group="The count", scope=_TENANT,
                      sort_order=100),
            FieldSpec("book_balance", "Balance on the books", group="The count",
                      writable=False, scope=_TENANT, sort_order=110),
            FieldSpec("difference", "Over or short", group="The count", writable=False,
                      scope=_TENANT, sort_order=120,
                      api_names=("difference", "shortage", "overage")),
            FieldSpec("difference_reason", "Reason for the difference", group="The count",
                      scope=_TENANT, sort_order=130),
            FieldSpec("counted_by", "Counted by", group="The count", scope=_TENANT,
                      sort_order=140,
                      api_names=("counted_by_id", "counted_by_name", "counted_by_is_exited")),
            FieldSpec("amount", "Cash banked", group="Cash banked", scope=_TENANT,
                      sort_order=200, api_names=("amount", "amount_naira")),
            FieldSpec("bank_account", "Bank account", group="Cash banked", scope=_TENANT,
                      sort_order=210, api_names=("bank_account_id", "bank_account_name")),
            FieldSpec("previous_float_amount", "Float before", group="Cash banked",
                      writable=False, scope=_TENANT, sort_order=220),
            FieldSpec("new_float_amount", "Float after", group="Cash banked", scope=_TENANT,
                      sort_order=230),
            FieldSpec("cash_left", "Cash the tin keeps", group="Cash banked", writable=False,
                      scope=_TENANT, sort_order=240),
            FieldSpec("narration", "Narration", group="Details", scope=_TENANT, sort_order=300),
            FieldSpec("reference", "Reference", group="Details", scope=_TENANT, sort_order=310),
            FieldSpec("journal", "Journal", group="Outcome", writable=False, scope=_TENANT,
                      sort_order=400, api_names=("journal_id",)),
            FieldSpec("raised_by", "Raised by", group="Outcome", writable=False,
                      scope=_TENANT, sort_order=410,
                      api_names=("created_by_id", "created_by_name", "created_by_is_exited")),
        ),
        write_aliases={"bank_account": "bank_account", "counted_by": "counted_by"},
    )
    register_fields(
        "finance",
        "expenseclaim",
        surfaces=("vs_finance.serializers.ExpenseClaimSerializer",),
        fields=(
            FieldSpec("claimant", "Claimant", group="Claim", scope=_TENANT, sort_order=10,
                      description="Who the claim reimburses: a member of staff from the "
                                  "staff list, or a name typed for somebody not on it.",
                      api_names=("claimant_id", "claimant_name")),
        ),
        write_aliases={"claimant": "claimant"},
    )
    register_fields(
        "finance",
        "dashboard",
        surfaces=(
            "vs_finance.dashboard_serializers.FinanceKpisSerializer",
            "vs_finance.dashboard_serializers.FinanceDashboardSerializer",
            "vs_finance.dashboard_serializers.ReceivablesDashboardSerializer",
            "vs_finance.dashboard_serializers.SpendDashboardSerializer",
        ),
        fields=_dashboard_fields(),
    )
    register_fields(
        "finance",
        "period",
        surfaces=(
            "vs_finance.serializers.FiscalPeriodSerializer",
            "vs_finance.close_access.BranchPeriodStateSerializer",
            "vs_finance.close_access.CloseChecksSerializer",
            "vs_finance.close_access.CloseChecklistSerializer",
            "vs_finance.close_access.PeriodChecklistPreviewSerializer",
            "vs_finance.close_access.PeriodCloseResultSerializer",
        ),
        fields=_period_fields(),
    )
    register_fields(
        "finance",
        "seal",
        surfaces=(
            "vs_finance.seal_serializers.SealCheckSerializer",
            "vs_finance.seal_serializers.SealVerificationSerializer",
        ),
        fields=(
            _figure("verdict", "Whether the closed figures match", "Result", 10,
                    "The overall and per-period verdict, and how many periods were checked "
                    "and how many differ.",
                    api_names=("ok", "checked", "mismatches")),
            _figure("chain_breaks", "Seals that do not follow the one before", "Result", 20),
            _figure("checks", "Each period or year checked", "Result", 30,
                    "The list of seals that were checked."),
            _figure("seal_name", "Period or year", "Each seal", 100,
                    api_names=("seal_id", "label", "kind", "fiscal_year", "period_id")),
            _figure("sealed_at", "When it was sealed", "Each seal", 110),
            _figure("seal_checksum", "Seal checksum", "Each seal", 120),
            _figure("line_counts", "Ledger lines sealed and now", "Each seal", 130,
                    api_names=("line_count", "line_count_now")),
            _figure("seal_agreement", "Whether the seal and its lines still match",
                    "Each seal", 140, api_names=("seal_intact", "lines_match")),
            _figure("differences", "Balances that moved", "Each seal", 150,
                    "Each account balance that differs from the sealed figure, by branch, "
                    "with what was sealed and what the ledger says now.",
                    api_names=("differences", "summary")),
        ),
    )
    register_fields(
        "finance",
        "fiscalyear",
        surfaces=(
            "vs_finance.serializers.FiscalYearSerializer",
            "vs_finance.close_access.FiscalYearCloseResultSerializer",
        ),
        fields=(
            _figure("close_state", "Close state of the year", "Fiscal year", 10,
                    "Whether the year is open, closed or locked and where each branch stands.",
                    api_names=("status", "branch_states")),
            _figure("archive_state", "Whether the year is put away", "Fiscal year", 20,
                    "Whether the year has been archived and when.",
                    api_names=("is_archived", "archived_at")),
            _figure("net_income", "Net result carried to equity", "Year close", 100,
                    "The year's result the close rolled into retained earnings."),
        ),
    )


def _settings_section_specs() -> tuple:
    """The ``finance.settings`` fields of the sections other than receivables and banking.

    Labelled from the settings history's own maps, so a switch and a history
    line name a value the same way. A choice carries its label and options,
    and the retention period its effective value, under the same switch.
    """
    from .account_mappings import ACCOUNT_MAPPING_SPECS, PLATFORM_ONLY_MAPPING_KEYS
    from .constants import AccountMappingKey
    from .settings_history import (
        DOCUMENT_SETTING_FIELDS,
        PAYROLL_SETTING_FIELDS,
        RETENTION_SETTING_FIELDS,
        _calendar_fields,
    )

    choice_names = {"paye_method", "payment_route", "next_year_mode"}
    extra_names = {"retention_years": ("effective_retention_years",)}
    specs = []
    sections = (
        ("Documents", 200, {
            name: field for name, field in DOCUMENT_SETTING_FIELDS.items()
            if name != "primary_collection_bank_account"
        }),
        ("Fiscal calendar", 300, _calendar_fields()),
        ("Payroll", 400, PAYROLL_SETTING_FIELDS),
        ("Record retention", 500, RETENTION_SETTING_FIELDS),
    )
    for group, base, fields in sections:
        for offset, (name, field) in enumerate(fields.items()):
            api_names = (name,)
            if name in choice_names:
                api_names += (f"{name}_label", f"{name}_options")
            api_names += extra_names.get(name, ())
            specs.append(FieldSpec(
                name, field.label, group=group, scope=_TENANT,
                sort_order=base + offset * 10,
                **({"api_names": api_names} if len(api_names) > 1 else {}),
            ))
    keys = [key for key in ACCOUNT_MAPPING_SPECS if key not in PLATFORM_ONLY_MAPPING_KEYS]
    for offset, key in enumerate(keys):
        specs.append(FieldSpec(
            mapping_field_name(key), AccountMappingKey(key).label, group="Account mappings",
            scope=_TENANT, sort_order=600 + offset * 10, api_names=(key,),
        ))
    return tuple(specs)


def mapping_field_name(key: str) -> str:
    """The ``finance.settings`` field an account mapping role travels under."""
    return f"mapping_{str(key).lower()}"
