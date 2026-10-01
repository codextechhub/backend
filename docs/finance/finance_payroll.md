# finance_payroll

Batch **payroll** on the classic two-step accrual-then-disburse model: **post** a run
to recognise the cost and park each liability (`Dr salary expense and employer
contribution expenses, Cr PAYE / pension / NHF / NSITF / ITF / voluntary deduction /
net-wages payable`), then **pay** it to clear net wages (`Dr net-wages payable, Cr
bank`) and issue each person's payslip. Runs are typed by hand or **generated from an
employee-salary roster**, and a generated line works out its own statutory deductions:
PAYE from the national tax table of the month's tax year, pension, NHF, voluntary
deductions and employer contributions from the tenant's payroll settings.

Routes (mounted at `/v1/finance/`): `payroll-runs/…`, `payroll-runs/{summary,generate}/`,
`payroll-runs/<pk>/{post,pay,cancel}/`, `payroll-runs/<pk>/lines/<line_pk>/payslip/`,
`employee-salaries/…` (with `history/`, `deductions/`, `tax-summary/`),
`employee-deductions/<pk>/`, `salary-structures/…` (with `history/`),
`payroll/deduction-types/…`, `payroll/tax-tables/…`, `payroll/tax-states/…`,
`payroll/pension-fund-administrators/…`, `settings/payroll/`, `my-payslips/…`,
`my-tax-summary/`.

---

## 1. What it is (and what it is NOT)

- A **`PayrollRun`** is a batch of **`PayrollLine`** rows, one per employee:
  `gross`, `paye`, `pension`, `other_deductions` (NHF and voluntary deductions),
  `employer_contributions` (employer pension, NSITF, ITF, a cost on top of gross) and
  `net = gross - paye - pension - other_deductions`. Each deduction and contribution
  is a **`PayrollLineItem`**, which decides the account it posts to.
- An **`EmployeeSalary`** is the roster row a run is generated from: the person's
  current pay terms, their statutory profile (state of residence, tax ID, PFA, pension
  PIN, annual rent) and any explicit PAYE override. Its pay terms keep their history as
  **`EmployeeSalaryVersion`** rows, each effective from a date.
- A **`SalaryStructure`** + **`SalaryComponent`** split a gross into earnings (basic,
  pensionable, taxable). Its lines keep their history too: an edit closes the current
  lines and writes new ones effective from a date.
- **National data, maintained by the platform:** **`PayeTaxTable`** (one per country
  and tax year, with **`PayeTaxBand`** and **`PayeTaxRelief`** rows),
  **`PayrollTaxJurisdiction`** (the states PAYE is remitted to) and
  **`PensionFundAdministrator`**. No tenant can edit them.
- **`FinancePayrollSettings`** is the tenant's payroll policy, every choice with a
  default (section 5).
- **`Payslip`** is issued per line when the line's pay leaves the bank.

**This does NOT:**
- **Apportion a month by days.** A raise, a move or a structure change dated inside a
  month applies to the whole month (the payroll date rule, section 6).
- **Refund PAYE through payroll.** A month whose cumulative tax is below what was
  already withheld deducts nothing; the excess reduces later months.
- **Model reliefs the payroll has no input for** (health insurance, life assurance,
  mortgage interest). The tax table can carry them as rules, but nothing feeds them.
- **One-click undo a *paid* run.** `cancel/` voids a DRAFT or POSTED run; a PAID run is
  refused (reverse the disbursement first).
- **Show individual salaries to everyone.** Every pay figure, the tax ID and the
  pension PIN are Field Access switches (section 11).

## 2. Domain model

| Model | Key fields |
|---|---|
| `PayrollRun` | `pay_date`, `period_label`, `run_status`, `branch?`, the four base posting accounts, `gross/paye/pension/other_deductions/employer_contributions/net_total`, `journal`, `disbursement_journal` |
| `PayrollLine` | `salary?`, `employee?`/`employee_name`, `branch`, `gross/paye/pension/other_deductions/employer_contributions/net_amount`, `taxable_pay`, `paye_source` (COMPUTED / OVERRIDE / SUPPLIED / MANUAL), `tax_table?`, `tax_basis` (the PAYE working), `tax_state?`, `pfa?`, `tax_id`, `pension_pin`, `components`, `cost_center` |
| `PayrollLineItem` | `line`, `kind` (DEDUCTION / EMPLOYER), `code` (PAYE, PENSION, NHF, VOLUNTARY, EMPLOYER_PENSION, NSITF, ITF), `amount`, `basis_amount`, `rate_bps`, `deduction_type?`, `employee_deduction?`, `liability_account?`, `expense_account?` (stamped at posting) |
| `PayrollRunBranch` | one branch's share of a central run: its totals, journal, disbursement and bank |
| `EmployeeSalary` | current terms (`branch`, `structure`, `gross_amount`, typed `paye/pension_amount`, `cost_center`, `residence_state`), profile (`tax_id`, `pfa`, `pension_pin`, `annual_rent`), `paye_override` + `paye_override_reason`, `is_active` |
| `EmployeeSalaryVersion` | `salary`, `effective_from`, the terms above, `reason`, `created_by` |
| `SalaryComponent` | `kind`, `calc_method`, `rate_bps`, `amount`, `is_basic`, `is_pensionable`, `is_taxable`, `statutory_type`, `effective_from`, `effective_to` |
| `PayrollDeductionType` | `code`, `name`, `liability_account` (shared config of the books) |
| `EmployeeDeduction` | `salary`, `deduction_type`, `amount` a month, `start_date?`, `end_date?`, `total_limit?` |
| `PayeTaxTable` | `country`, `tax_year`, `minimum_tax_rate_bps`, `exempt_income_threshold`, `revision`, bands, reliefs |
| `PayeTaxRelief` | `kind` (CONTRIBUTION / PERCENT_CAPPED / FIXED), `basis` (PENSION / NHF / ANNUAL_RENT / ANNUAL_GROSS), `rate_bps`, `cap_amount?`, `floor_amount` |
| `PayrollTaxJurisdiction` | `country`, `code` ("LA"), `name`, `authority_name` |
| `PensionFundAdministrator` | `code` ("STANBIC"), `name` |
| `FinancePayrollSettings` | section 5 |
| `Payslip` | `line`, `run`, `salary?`, `employee?`, `branch`, `pay_date`, `email_status`, `email_attachment` (storage key) |

Money is kobo. Default posting accounts: salary `5200`, PAYE `2310`, pension `2320`,
net wages `2330`, NHF `2340`, NSITF `2350` (expense `5220`), ITF `2360` (expense
`5230`), employer pension expense `5210`. Per-state PAYE payables are `2310-<state>`
and per-PFA pension payables `2320-<pfa>`, created on first use (section 8).

## 3. Endpoint map

All tenant routes require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`
unless noted.

| Method + path | permission key | what it does |
|---|---|---|
| `GET /payroll-runs/` | `finance.payrollrun.view` | List runs |
| `POST /payroll-runs/` | `finance.payrollrun.create` | Create a DRAFT run by hand. Typed PAYE is audited as an override |
| `POST /payroll-runs/generate/` | `finance.payrollrun.create` | Draft a run from the roster; `skipped` lists people a live run of the month already pays |
| `GET /payroll-runs/summary/` | `finance.payrollrun.view` | KPIs |
| `GET /payroll-runs/<pk>/` | `finance.payrollrun.view` | Run + lines + items |
| `POST /payroll-runs/<pk>/post/` | `finance.payrollrun.post` | Accrue |
| `POST /payroll-runs/<pk>/pay/` | `finance.payrollrun.pay` | Disburse and issue payslips |
| `POST /payroll-runs/<pk>/cancel/` | `finance.payrollrun.post` | Cancel a draft or void a posted run |
| `GET /payroll-runs/<pk>/lines/<line_pk>/payslip/` | `finance.payrollrun.view` + every pay-figure switch | The line's payslip PDF (`?output=json` for content) |
| `GET/POST /employee-salaries/` | `finance.salary.view` / `.create` | Roster list / add (with profile fields and `effective_from`) |
| `PATCH /employee-salaries/<pk>/` | `finance.salary.update` | Profile edited in place; terms as a new dated version; `paye_override` with a reason |
| `DELETE /employee-salaries/<pk>/` | `finance.salary.delete` | Deactivates (the row and history stay) |
| `GET /employee-salaries/<pk>/history/` | `finance.salary.view` | Versions of the pay terms |
| `GET/POST /employee-salaries/<pk>/deductions/` | `finance.salary.view` / `.create` | Voluntary deductions of one person |
| `PATCH/DELETE /employee-deductions/<pk>/` | `finance.salary.update` / `.delete` | Edit or stop one |
| `GET /employee-salaries/<pk>/tax-summary/?year=` | `finance.salary.view` + every pay-figure switch | The person's tax year, JSON or `?output=pdf` |
| `GET/POST /salary-structures/` | `finance.salary.view` / `.create` | Structures with their current lines |
| `GET/PATCH/DELETE /salary-structures/<pk>/` | `finance.salary.view` / `.update` | Edit lines from `effective_from` |
| `GET /salary-structures/<pk>/history/` | `finance.salary.view` | Every line with its dates |
| `GET/POST /payroll/deduction-types/`, `PATCH …/<pk>/` | `finance.salary.view` / `.create` / `.update`, whole-tenant writes | Voluntary deduction types |
| `GET/PATCH /settings/payroll/` | `finance.settings.view` / `.update`, whole-tenant writes | Payroll policy |
| `GET /payroll/tax-tables/`, `GET …/<pk>/` | any finance access | National PAYE tables |
| `POST /payroll/tax-tables/`, `PATCH …/<pk>/` | platform staff + `finance.statutory.create` / `.update` (platform scope) | Add or edit a table |
| `GET/POST /payroll/tax-states/`, `GET/PATCH …/<pk>/` | read: any finance access; write: platform staff + `finance.statutory.*` | PAYE states |
| `GET/POST /payroll/pension-fund-administrators/`, `GET/PATCH …/<pk>/` | as above | PFAs |
| `GET /my-payslips/`, `GET /my-payslips/<pk>/` (`?output=pdf`) | authenticated only | The caller's own payslips, where in-app delivery is on |
| `GET /my-tax-summary/?year=` (`?output=pdf`) | authenticated only | The caller's own tax year |

## 4. Lifecycle

```
Roster (EmployeeSalary + versions) ──generate──▶ DRAFT run
                          (or POST /payroll-runs/ by hand)
DRAFT ──post (accrue)──▶ POSTED ──pay (disburse)──▶ PAID ──▶ payslips issued
  │                         │
cancel                    cancel (reverse accrual)
  ▼                         ▼
CANCELLED ◀─────────────────┘   (PAID can't be cancelled)
```

A run posted one journal per branch is paid a branch at a time; each branch's staff
get their payslips when their branch's share is paid.

## 5. Payroll settings (`settings/payroll/`)

| Setting | Default | Effect |
|---|---|---|
| `paye_method` | `COMPUTED` | COMPUTED: PAYE from the national table, employee pension from the rate. SUPPLIED: both from the structure's PAYE/pension lines or the roster's typed figures |
| `tax_country` | `NG` | Which country's tables price PAYE |
| `employee_pension_enabled` / `_rate_bps` | on / 800 | 8% of pensionable pay withheld (COMPUTED only) |
| `employer_pension_enabled` / `_rate_bps` | on / 1000 | 10% of pensionable pay, expensed and accrued per branch |
| `nhf_enabled` / `nhf_rate_bps` | on / 250 | 2.5% of basic withheld |
| `nsitf_enabled` / `nsitf_rate_bps` | on / 100 | 1% of gross, employer |
| `itf_enabled` / `itf_rate_bps` | on / 100 | 1% of gross, employer |
| `payslip_in_app` | on | Employees see their payslips in the app and get an in-app notice |
| `payslip_email` | on | Employees are emailed their payslip PDF |

Writes need whole-tenant reach (403 `SHARED_RECORD_READ_ONLY` for a branch-bound
caller), are validated per field and audited (`FIN_PAYROLL_SETTINGS_UPDATED`). A
change reaches the next run generated.

## 6. Generating a run

**The payroll date of a month** is the last day of the payroll period containing the
run's pay date (the entity's fiscal period, or that calendar month). Each person's
terms are read as at that date (`EmployeeSalary.terms_on`): gross, structure (its
lines in force then), branch, cost centre and state. One date per month, so every run
of the month reads a person the same way, and **the branch a person belongs to on the
payroll date pays the whole month**.

**Who is on it.** A central run covers every active person whose terms exist on the
payroll date; a branch run covers those whose branch on that date is the run's, read
exclusively. **The person guard**: anybody a live run of the month (draft included)
already pays is left off and listed in `skipped`; the salary rows are locked first so
two concurrent runs cannot take the same person. A central school can therefore raise
a second generated run for a late hire, but never pays the roster twice.

**Each line** (`payroll_statutory.work_out_line`):

```
pensionable = Σ earnings flagged pensionable   (whole gross when none is)
basic       = Σ earnings flagged basic          (whole gross with no structure)
taxable     = gross - Σ earnings flagged not taxable
pension     = pensionable × employee rate        (SUPPLIED: structure or typed figure)
nhf         = basic × NHF rate
voluntary   = each due assignment, capped by what is left of its total_limit
employer    = pensionable × employer pension rate + gross × NSITF rate + gross × ITF rate
paye        = cumulative PAYE (below), or the person's override
net         = gross - paye - pension - nhf - voluntary
```

**PAYE** (`payroll_tax.compute_paye`), month `m` of the tax year:

```
income to date   = Σ taxable pay of earlier months + this month
reliefs to date  = pension and NHF actually contributed to date
                 + percentage reliefs (annual, floored and capped) × m/12
chargeable       = max(income to date - reliefs to date, 0)
tax to date      = Σ over the annual bands scaled by m/12, rounded to the kobo
paye this month  = max(tax to date - PAYE already withheld this year, 0)
```

The line records `tax_table`, `taxable_pay` and `tax_basis` (bands, relief rules,
inputs and every intermediate figure), so the month can be explained and recomputed.
A year with no table refuses to compute and names the year.

**PAYE state**: the person's `residence_state` on the payroll date, else the state of
the branch that pays them (read from the branch's state text), else none (posts to the
base PAYE payable).

**Override**: `paye_override` on the salary row replaces the computed figure on every
run until cleared; setting it needs a reason and is audited (`PAYE_OVERRIDE_CHANGED`),
and the line keeps the computed figure beside it. A hand-typed run's PAYE is audited
the same way.

## 7. Salary and structure history

- An edit to a person's terms writes a new version (`change_terms`), audited
  (`SALARY_CHANGED`, with before and after). Undated, it takes effect from the first
  payroll month not yet paid; a date inside a month already paid is refused.
- A row created before history existed first records its terms as the version "from
  the start", so the months before the change still read what they were paid on.
- **Branch moves under per-branch payroll**: a move that would leave a person on no run
  in a month is refused. Mrs Okafor moved from Lekki to Ikeja from 22 September after
  Ikeja raised September's run without her: no run would pay her September, so the
  move must be dated from October, or Ikeja's run voided and raised again.
- Under per-branch payroll an active person must have a branch.
- `payroll.scope` changes only at the start of a payroll month, before any run of it is
  raised.
- A structure edit closes the current lines (`effective_to`) and writes new ones from
  `effective_from` (default: the first month nobody on it has been paid for), audited
  (`SALARY_STRUCTURE_CHANGED`).
- `DELETE` on a roster row deactivates it (`SALARY_DEACTIVATED`).

## 8. What posting does to the ledger

**Accrual**, one journal per branch whose staff are on the run:
```
Dr  salary expense (5200, by cost centre)                Σ gross
Dr  employer pension / NSITF / ITF expense (by cost centre)  Σ employer items
Cr  PAYE payable of each state (2310-LA, 2310-OG, …; 2310 without a state)
Cr  pension payable of each PFA (2320-STANBIC, …; 2320 without a PFA)
                                         employee and employer pension together
Cr  NHF (2340), NSITF (2350), ITF (2360) payable
Cr  each voluntary deduction type's account
Cr  net wages payable (2330)                              Σ net
```
`gross + employer = deductions + employer liabilities + net`, so each branch's journal
balances on its own. The first posting to a state or PFA creates its payable account
(beside the base one) and its tax obligation (`PAYE-LA` to "Lagos State Internal Revenue
Service", `PENSION-STANBIC` to the PFA), so each authority gets its own return. Each item
is stamped with the accounts it posted to.

**Disbursement**: `Dr 2330, Cr bank`, per branch share from that branch's own bank.

## 9. Payslips and tax summaries

- Paying a run (or a branch's share) creates a `Payslip` per line (`PAYSLIPS_ISSUED`).
  After the payment commits, the delivery task sends the in-app notice
  (`payroll.payslip_ready`) and the email with the PDF attached
  (`payroll.payslip_emailed`), as the settings say. No user account or no email:
  `email_status = NO_ADDRESS`.
- The PDF is rendered from the line each time it is opened: earnings, every deduction,
  employer contributions, totals, year to date, and how PAYE was arrived at.
- The tax summary lists a person's posted and paid months of a year with totals, the
  states and the tax ID.
- An employee reads only their own (`my-payslips`, `my-tax-summary`). Payroll staff
  read anybody's within their branch reach, and only if every pay figure on it is open
  to their role.

## 10. Worked example

Single Site pays Grace Eze N100,000 a month in Lagos under the default policy. January:
pension N8,000, NHF N2,500, PAYE N3,425 (chargeable N89,500 against bands scaled to one
month), net N86,075; employer pension N10,000, NSITF N1,000, ITF N1,000. Posting credits
`2310-LA` N3,425, `2320` N18,000, `2340` N2,500, `2350` N1,000, `2360` N1,000, `2330`
N86,075, and debits `5200` N100,000, `5210` N10,000, `5220` N1,000, `5230` N1,000. Her
raise to N300,000 from July is a new version; the year's PAYE totals exactly the tax on
N2.4m less her pension and NHF.

## 11. Permissions & tenant isolation

- `finance.payrollrun.{view, create, post, pay}` for runs; `finance.salary.{view,
  create, update, delete}` for the roster, structures, deductions and history;
  `finance.settings.{view, update}` for the policy; `finance.tax.view` for schedules.
- `finance.statutory.{create, update}` are **platform-scoped** and granted to the
  platform roles; the views also require a platform account.
- Field Access: the statutory figures travel under the switch of the figure they
  belong to, so no new switch exists and a role keeps seeing what it saw. On a line,
  `tax_id` and `pension_pin` go with the employee name, `taxable_pay` with gross,
  `tax_basis` with PAYE, and `items`, `other_deductions_amount` and
  `employer_contributions_amount` with the pay breakdown. On a salary row, `tax_id`,
  `annual_rent` and `paye_override(_reason)` go with PAYE and `pension_pin` with
  pension. Payslip, tax summary and remittance schedule are refused unless every pay
  figure on them is open.
- Runs, lines, salary rows, deductions and payslips resolve inside the entity and the
  caller's branch reach; another tenant's ids are 404.

## 12. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `PayrollRun`, `PayrollLine`, `PayrollRunBranch`, `SalaryStructure`, `SalaryComponent`, `EmployeeSalary` |
| `models/payroll_statutory.py` | national data, `FinancePayrollSettings`, `EmployeeSalaryVersion`, voluntary deductions, `PayrollLineItem`, `Payslip` |
| `payroll.py` | `apply_structure`, generation and the person guard, posting, paying, cancelling, scope guards |
| `payroll_statutory.py` | payroll date, line working, YTD, accounts per item, salary and structure history, remittance schedules |
| `payroll_tax.py` | tax table lookup and the cumulative PAYE calculation |
| `payroll_settings.py` | the settings service |
| `payslips.py` | payslip content, issue and delivery, tax summaries |
| `pdf.py` | payslip and tax summary PDFs |
| `views_ops/payroll.py`, `views_ops/payroll_statutory.py`, `views_settings.py` | endpoints |

## 13. Tests

`tests_payroll_statutory.py`: the engine (January figure, mid-year raise, rent cap, no
refund, exemption); a two-state multi-branch run (PAYE by residence and branch state,
per-state and per-PFA accounts and obligations, employer costs per branch, the Ogun
return and the PFA return with their schedules, a branch-bound reader's schedule); a
one-branch run; a mid-year raise over twelve runs; the person guard and the scope switch
under central payroll; mid-month moves under per-branch payroll; overrides, versions and
deactivation; supplied figures and a capped voluntary deduction; payslips (issue,
delivery switches, own-only reads, PDF field access, cross-tenant); settings; national
data; structure history. `tests.py`, `tests_payroll_branch.py`,
`tests_payroll_split.py` and `tests_payroll_share_reach.py` cover the run lifecycle,
branch scope and per-branch posting.

## 14. Known limitations

- The 2026 table, the state revenue service names and the PFA list are data seeded for
  an accountant to confirm.
- PAYE is never refunded through payroll; a mid-year joiner with income elsewhere has
  no opening year-to-date input.
