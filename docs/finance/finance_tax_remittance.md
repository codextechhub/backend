# finance_tax_remittance

Statutory tax **returns and remittance**. A `TaxObligation` maps a tax (VAT / WHT /
PAYE / pension) to the GL **payable account** that accumulates it, plus a
**recoverable account** for VAT input. A `TaxFiling` is one return: it **declares
source lines**, is **filed** (lines stamped, per-branch netting and penalty posted),
then **paid** branch share by branch share (`Dr payable, Cr bank`). The source flows
(sales VAT, purchase input VAT, payroll PAYE/pension, vendor WHT) park the tax; this
slice is how it is declared and leaves the books.

Routes (mounted at `/v1/finance/`): `tax-obligations/…`, `tax-obligations/outstanding/`,
`tax-filings/…`, `tax-filings/summary/`, `tax-filings/<pk>/{file,unfile,pay}/`,
`tax-filings/<pk>/remittances/<remittance_pk>/reverse/`, and `tax-codes/`.

---

## 1. What it is (and what it is NOT)

- **A return declares source lines, not a date window.** The source lines of an
  obligation are the ledger lines (POSTED and REVERSED entries, as every money
  reader counts them) on its payable account, and on its recoverable account where
  it has one, that the tax module did not write. Every journal the module posts
  (netting, penalty, remittance) carries `JournalSource.TAX`; those journals, and
  reversals of them, are never source lines.
- **Each source line is declared once.** A return collects every undeclared source
  line dated on or before its `period_end`. Filing stamps each one with a
  `TaxFilingLine` (one-to-one on the journal line, so the database refuses a second
  declaration). Un-filing deletes those rows and releases the lines.
- **Late lines go on the next return, named by month.** A line dated before the
  return's `period_start` (recorded after its own month was filed) is declared by the
  next open return and listed in `late_items` under its month ("from March"). A filed
  return is never amended.
- **One return per tenant, booked per branch.** The return breaks down by the branch
  of each line's entry. Each branch share has its own netting/penalty journal and its
  own remittance journals, all carrying that branch; the return is PAID when every
  share is.
- **Credits carry forward.** When recoverable input and brought-forward credit
  exceed the tax, the return is due nil and the excess is carried to the next return
  of the same obligation.
- **This does NOT accrue tax.** The liability already sits in the control account
  from source postings; `prepare` only reads it.
- **This does NOT apportion input VAT** between taxable and exempt supplies, or
  generate the expected return for each period from `frequency` and `filing_day`.

## 2. Domain model

| Model | Key fields |
|---|---|
| `TaxObligation` | `code`, `obligation_type`, `liability_account`, `recoverable_account?` (VAT input, usually 1300), `authority_name`, `frequency`, `filing_day`, `is_active` |
| `TaxFiling` | `obligation`, `period_start/end`, `due_date?`, `filing_status`, `gross_liability`, `recoverable_amount`, `brought_forward_credit`, `carried_forward_credit`, `credit_from?`, `adjustment_amount`(+`adjustment_account`), `amount_due`, `amount_paid`, `payment_status`, `declared_line_count`, `late_line_count`, `late_items`, `filing_reference`, `filed_at`, `filing_journal?` |
| `TaxFilingShare` | `filing`, `branch?`, `branch_pending`, the same figures per branch, `amount_due`, `amount_paid`, `payment_status`, `line_count`, `filing_journal?` (that branch's netting/penalty journal) |
| `TaxFilingLine` | `filing`, `journal_line` (one-to-one), `role` (PAYABLE / RECOVERABLE), `branch?` (the branch it counted under), `is_late` |
| `TaxRemittance` | `filing`, `share`, `branch?`, `bank_account`, `pay_date`, `amount`, `journal` (one-to-one), `reversal_journal?`, `reversed_at?`, `reversal_reason` |
| `TaxCode` | `rate_bps`, `treatment` (STANDARD / ZERO_RATED / EXEMPT), `is_recoverable`, `collected_account?`, `paid_account?` |

Money is kobo. `balance_due = amount_due − amount_paid`; `payment_status` reuses
`InvoicePaymentStatus` and reads PAID when nothing is due.

**Why a link table for declarations.** A posted journal line is immutable, and the
declaration is a fact about the return rather than the line. `TaxFilingLine` keeps
the ledger's busiest table free of tax columns, makes "declared at most once" a
database constraint, and lets un-filing release lines by deleting rows.

**`filing_journal` on `TaxFiling`** is the single netting/penalty journal of a
return filed as a whole (returns that existed before shares). A share's journal lives
on the share; the API's `filing_journal_id` returns the share's journal when the
return has exactly one share.

## 3. Branches

The branch of a line is the branch of its journal entry:

- an entry with a branch keeps it;
- an entry with none counts as the tenant's **only branch** when it has exactly one;
- at a tenant with **several** branches it forms its own "no branch yet" group
  (`branch_pending`), which a draft shows and **filing refuses** (naming the count)
  until the entries are reversed and re-posted with a branch;
- a tenant with **no branch at all** (the platform's own books) files and pays as one
  share with no branch.

`vs_finance.tax_filing.branch_breakdown(lines)` is the one helper that groups a
return's lines by branch; `branch_rule(entity)` decides the branch a line counts
under.

Nothing new is written with a NULL branch at a tenant that has branches: every
netting, penalty and remittance journal carries its share's branch.

## 4. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET/POST /tax-obligations/` | `finance.tax.view` / `.create` | Obligation list / create | `code`, `name`, `obligation_type`, `liability_account`, `recoverable_account?`, `authority_name?`, `frequency?`, `filing_day?` | obligation |
| `GET/PATCH /tax-obligations/<pk>/` | `finance.tax.view` / `.update` | One obligation / edit | - | obligation |
| `GET /tax-obligations/outstanding/` | `finance.tax.view` | Running balance in each control account (all-time GL net, less recoverable) | - | rows |
| `GET/POST /tax-filings/` | `finance.tax.view` / `.file` | Filings list (paginated) / **prepare** a draft | `obligation`, `period_start`, `period_end`, `due_date?` | filing |
| `GET /tax-filings/summary/` | `finance.tax.view` | KPIs over all filings | - | summary |
| `GET /tax-filings/<pk>/` | `finance.tax.view` | One filing | - | filing |
| `POST /tax-filings/<pk>/file/` | `finance.tax.file` | **File**: declare lines, post per-branch netting/penalty | `filed_date`, `filing_reference?`, `adjustment_amount?`, `adjustment_account?`, `adjustment_branch?` | filing |
| `POST /tax-filings/<pk>/unfile/` | `finance.tax.file` | **Un-file**: FILED → DRAFT, journals reversed, lines released | - | filing |
| `POST /tax-filings/<pk>/pay/` | `finance.tax.pay` | **Remit** one or more branch shares | `pay_date`, and either `bank_account`, `branch?`, `amount?` or `shares: [{branch, bank_account, amount?}]` | filing |
| `POST /tax-filings/<pk>/remittances/<remittance_pk>/reverse/` | `finance.tax.pay` | **Reverse a remittance** recorded in error | `reason`, `date?` | filing |

A filing response adds to the earlier fields: `brought_forward_credit`,
`carried_forward_credit`, `credit_from_id`, `declared_line_count`, `late_line_count`,
`late_items` (`[{month, label, gross, recoverable, net, line_count}]`),
`branch_breakdown` (one row per share: `branch_id`, `branch_name`, `branch_pending`,
`label`, the figures, `amount_due`, `amount_paid`, `balance_due`, `payment_status`,
`line_count`, `filing_journal_id`) and `remittances` (`branch_id`, `bank_account_id`,
`pay_date`, `amount`, `journal_id`, `is_reversed`, `reversed_at`,
`reversal_journal_id`, `reversal_reason`).

A school-wide filing needs whole-tenant reach for every write; a branch-bound caller
gets `403 SHARED_RECORD_READ_ONLY`.

## 5. Lifecycle

```
DRAFT ──file──▶ FILED ──pay (per share, partial ok)──▶ PAID
  ▲ ◀──unfile── ┘   ◀──reverse remittance── ┘
```

- **Prepare** (`prepare_filing`): works the figures out from the undeclared source
  lines and writes them, with the shares, onto a draft. Re-running for the same
  `(obligation, period_start, period_end)` refreshes the draft. A new draft whose
  period overlaps any other return of the obligation is refused. Nothing posts, no
  line is claimed, and a draft carries no penalty.
- **File** (`file_filing`): DRAFT only. Re-collects the lines and refuses when the
  tax, input or credit differ from the draft (prepare again, check, then file), and
  when a "no branch yet" group moves money. Stamps the lines, posts one netting/penalty
  journal per share that needs one, records `credit_from`. A **nil return files**:
  nothing is due and nothing is posted unless input is netted.
- **Un-file** (`unfile_filing`): FILED only, and only while nothing stands paid and no
  later filed return uses this one's carried credit. Reverses the share journals,
  deletes the declarations (the lines return to the pool), and refreshes the draft.
- **Pay** (`pay_filing`, `pay_filing_shares`): FILED (or PAID with a balance) only,
  dated on or after `filed_at`. Without `branch`, pays the only unpaid share, else the
  bank account's own branch's share, else (tenant-wide account) every unpaid share,
  each as its own journal. A bank account must be the share's branch's own or
  tenant-wide. `amount` needs a single share. The return is PAID when every share is.
- **Reverse a remittance** (`reverse_remittance`): reverses the remittance journal
  (on its own date where that month is open), keeps the row marked reversed, takes the
  amount off the share and the return, and puts a PAID return back to FILED. The
  journal screen cannot reverse a remittance journal on its own: the `TaxRemittance`
  owns it.

## 6. Calculations

Per branch share, over its source lines:

```
gross        = Σ (credit − debit)  on the payable account
recoverable  = Σ (debit − credit)  on the recoverable account
brought fwd  = that branch's carried credit on the previous filed return
net          = gross − recoverable − brought fwd        (signed)
```

Across the return: branches with surplus credit cover the others' tax, shared in
proportion to what each owes; any surplus left is carried forward, taken from each
surplus branch in proportion to its surplus. A penalty is spread over the branches in
proportion to their tax (or booked to `adjustment_branch`).

```
amount_due            = max(Σ gross − Σ recoverable − brought fwd, 0) + penalty
carried_forward_credit = max(Σ recoverable + brought fwd − Σ gross, 0)
```

The next return of the obligation brings forward the carried credit of the latest
filed return that ends before it starts and whose credit no other filed return has
taken.

**Outstanding** (`outstanding_obligations`): all-time movement of each active
obligation's accounts, "what the control accounts hold now".

## 7. What posting does to the ledger

**Prepare posts nothing.**

**File**, per share with input to net or a penalty, `source=TAX`, dated `filed_date`,
branch = the share's branch:
```
Dr  payable account        recoverable   ← net input tax off the payable
Cr  recoverable account    recoverable
Dr  penalty expense        penalty
Cr  payable account        penalty
```
All of a share's recoverable input is moved onto the payable account, so carried
credit sits as a debit on the payable account, and a branch that covered another's tax
shows a debit there while the covered branch shows the matching credit. For the tenant
the two net to zero.

**Pay**, per share, `source=TAX`, dated `pay_date`, branch = the share's branch:
```
Dr  payable account   pay
Cr  bank (GL cash)    pay
```

## 8. VAT treatment

A `TaxCode` carries a `treatment`. Only a STANDARD code may have a rate; a database
constraint holds ZERO_RATED and EXEMPT codes at zero, so an exempt or zero-rated
line never puts tax on the output account, whichever screen prices it. The starter
codes seeded with the chart are `VAT-STD` (7.5%, 2200/1300), `VAT-ZERO` (zero rated,
2200/1300, recoverable) and `VAT-EXEMPT` (exempt). A fee item with no tax code carries
no VAT. Every tenant can change any item's code.

## 9. Worked example

Harbour Primary, June VAT: output ₦75,000, input ₦20,000. Prepare June → due
₦55,000. File on 5 July → `Dr 2200 20,000 / Cr 1300 20,000` (Main). Pay on 21 July →
`Dr 2200 55,000 / Cr bank 55,000` (Main), PAID. July: output ₦80,000, input ₦10,000.
Prepare July → the June netting and payment are TAX journals and are not read; July
declares ₦80,000 − ₦10,000 = ₦70,000.

Lagoon View, June VAT: Ikeja output ₦60,000; Lekki output ₦40,000, input ₦10,000.
One return, due ₦90,000: Ikeja's share ₦60,000, Lekki's ₦30,000 with its own netting
journal. Ikeja pays from its account, Lekki from its own; PAID after both.

## 10. Permissions & tenant isolation

- Verbs: `finance.tax.{view, create, update, file, pay}`; reversing a remittance uses
  `finance.tax.pay`.
- Entity-scoped resolution everywhere; a remittance is reached only through its
  filing. A bank account outside the caller's branches is a 404; one of another branch
  than the share's is refused.

## 11. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `TaxObligation`, `TaxFiling`, `TaxFilingShare`, `TaxFilingLine`, `TaxRemittance` |
| `models/gl.py` | `TaxCode.treatment` and its rate constraint |
| `tax_filing.py` | `collect_source_lines`, `branch_rule`, `branch_breakdown`, `late_items`, `work_out_return`, `prepare_filing`, `file_filing`, `unfile_filing`, `pay_filing`, `pay_filing_shares`, `reverse_remittance`, `outstanding_obligations` |
| `views_ops/tax.py` | obligation CRUD, outstanding, filing list/prepare/summary/detail/file/unfile/pay, remittance reversal |
| `seed.py` | default obligations and the starter VAT codes |
| `constants.py` | `JournalSource.TAX`, `TaxTreatment`, `TaxSourceRole`, `TaxFilingStatus` |

## 12. Tests

`tests_tax_returns.py`: consecutive VAT months with the first filed and paid inside
the second; PAYE accrued monthly and paid the next month; a late line under "from
March"; carried input VAT; a nil return; per-branch breakdown, own-bank payments and
PAID only when every share is; unbranched lines at one and at two branches; un-filing
releasing lines; reversing a remittance; exempt lines and the seeded codes.
`TaxFilingTests` in `tests.py` covers due dates, overlap, netting, penalties, partial
payment and un-filing.
