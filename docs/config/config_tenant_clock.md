# config_tenant_clock

A tenant's own clock: which time zone it keeps its calendar in, what time it is
there, and which calendar day it is there, for the school and for each of its
branches. One configuration value, `display.timezone`, and one small module
every app reads it through, `vs_config.clock`.

The server runs in UTC (`settings.TIME_ZONE = "UTC"`), so `date.today()` and
`timezone.localdate()` answer with the UTC day. For a school in Lagos that day
is wrong for the first hour after every midnight: at 00:30 in Lagos the server
still says it is yesterday. An invoice raised then was dated yesterday, a fee
due yesterday was not yet overdue, and the calendar hub showed the day before.

---

## 1. What it is (and what it is NOT)

- **A day or a wall-clock reading comes from here; an instant does not.** A
  timestamp that is stored (`created_at`, `decided_at`, `posted_at`) stays
  `timezone.now()`, an aware UTC instant. Only "which day is it at this
  tenant" and "what does this tenant's clock say" go through the helper.
- **Every tenant starts on Africa/Lagos.** That is the definition's default
  and the platform's. A school elsewhere sets its own zone on its display
  settings (`docs/schools/school_settings.md`, section on display).
- **Three layers.** `display.timezone` allows `platform`, `school` and
  `branch` scope. The school's zone is the default, and a branch in another
  zone keeps its own: a Lagos school's Nairobi branch turns its day at
  Nairobi's midnight. Anything that belongs to a branch (a student, a posting,
  an invoice, a stock movement, a leave request) reads that branch's day. A
  school whose branches all follow it sees exactly the school's day
  everywhere, so nothing changes until a branch sets a zone.
- **Only real zones are stored.** A write guard registered by `vs_config`
  itself refuses anything that is not in the tz database's own list, at every
  write path (the console's generic value endpoint and the school's display
  endpoint alike). `localtime`, `UTC+1` and `Lagos` are all refused.

## 2. The helper

```python
from vs_config.clock import (
    tenant_zone, tenant_now, tenant_today,
    branch_zone, branch_now, branch_today, branch_zones, branch_day_q,
)

tenant_zone(tenant)            # -> zoneinfo.ZoneInfo, the school's
tenant_now(tenant)             # -> aware datetime in that zone
tenant_today(tenant)           # -> datetime.date in that zone

branch_zone(tenant, branch)    # -> the branch's own zone, else the school's
branch_now(tenant, branch)     # -> tenant_now's instant, in the branch's zone
branch_today(tenant, branch)   # -> the day at the branch
branch_zones(tenant)           # -> {branch id: ZoneInfo}, branches with their own only
branch_day_q(tenant, "branch", lambda day: Q(due_date__lt=day))
                               # -> a Q judging each row on its own branch's day
```

`branch` is a `Branch`, a branch id (or its digits) or `None`. `None` is the
school's zone, as is a branch that is not one of the tenant's. The branch
functions read `tenant_now`'s instant, so there is one "now" for a school and
its branches, and a test that patches `vs_config.clock.tenant_now` moves every
branch with it.

`branch_day_q` is for a filter over rows of several branches ("overdue" across
a school's invoices). Rows at a branch whose day differs from the school's are
judged on theirs; every other row, shared rows with no branch included, on the
school's. Where no branch's day differs (every school whose branches share its
zone, and every school for most of the day) it is exactly
`on_day(tenant_today(tenant))`.

**Which one to call.** A thing that belongs to a branch uses the branch form
with that branch: `branch_today(student.tenant, student.branch_id)`. A screen
looking through a branch filter uses that branch. A school-wide thing (the
books' fiscal calendar, a report or dashboard with one as-of date, a run over
the whole school, the school's subscription) uses `tenant_today`. Never guess a
branch where the thing has none.

| Argument | Layer read |
|---|---|
| A school's (or any business) tenant | its own value, else the platform value, else Africa/Lagos |
| The platform tenant (`kind == "PLATFORM"`) | the platform value, else Africa/Lagos |
| `None` | the same as the platform tenant |

Also in the module: `TIME_ZONE_KEY` (`"display.timezone"`),
`DEFAULT_TIME_ZONE` (`"Africa/Lagos"`), `is_valid_time_zone(name)`,
`guard_time_zone` (the write guard) and `forget_tenant_zone(tenant)`.

A stored value that is somehow not a zone (written around the guard, by a raw
update) reads as Africa/Lagos rather than raising.

### Where the tenant comes from

Use the tenant the rows already belong to: `request.tenant` in a view,
`entity.tenant` in the finance engine, `staff.tenant` in staff services,
`row.tenant` on a model. Where no tenant is in reach, pass `None` and the
platform zone applies. Do not thread a tenant through a call chain only for
this: the platform zone is Lagos until somebody changes it, which is right for
every current school.

### Caching

`get_config` is not cached (two queries), so the zone is memoised on the tenant
instance, which lives for one request (`request.tenant`) or one task, and so is
the map of branch zones (read in the same two queries). Another instance of the
request's own tenant, such as `row.tenant` on each row of a list, shares the
memo held on the request's instance, so a per-row "is this overdue" asks the
configuration once. The platform layer (`None`) is not memoised. After writing
a value, call `forget_tenant_zone(tenant)`, which drops both memos, so the same
request reads the new zone; the display endpoint does.

A row whose `tenant` has not been loaded still costs one query to load it; add
the tenant to `select_related` on list querysets (`vs_todo` does).

## 3. Seeding

- Data migration `vs_config/migrations/0013_seed_display_timezone.py`: STRING,
  default `"Africa/Lagos"`, INTERNAL. Reversible; the reverse drops the
  definition and every value.
- `vs_config/migrations/0014_display_date_format_clock_and_branch_zones.py`
  adds `branch` to its `allowed_scopes` (now `["branch", "platform",
  "school"]`) and declares `display.date_format` and `display.clock`
  (`vs_config.display`, `docs/schools/school_settings.md` section 5).
  Reversible; the reverse removes every branch zone, so each branch follows its
  school again.
- `seed_config_catalogue` carries the same rows in `SCHOOL_SCOPED_DEFINITIONS`,
  with `display.timezone` in `BRANCH_OVERRIDABLE`, so an environment built from
  the catalogue has them too.

## 4. Call sites

Every "today" in `vs_calendar`, `vs_academics`, `vs_staff`, `vs_students`,
`vs_schools`, the FAL, `vs_finance`, `vs_procurement`, `vs_payments`,
`vs_exports`, `vs_admin_console`, `vs_todo`, `vs_user` organogram,
`vs_tenants` document numbering and `vs_import_data` reads the helper. Those
below read a branch's day.

### Read on a branch's day

| Where | Branch |
|---|---|
| `vs_students.serializers`: age, offer expired | the student's |
| `vs_students.serializers._plausible_birth_date` on an edit, `ages.date_of_birth_problem` | the student's (`branch=`) |
| `vs_students.imports.resolve_row` (birth and admission dates in the future, age), `create_student_from_row` (enrolment date, applied on) | the row's, resolved before the dates |
| `vs_students.services.enrolment` (enrolment date, applied on) | the branch joined |
| `vs_students.services.admission` (stage move: entered on, offer last day) | the applicant's |
| `vs_students.services.placement`, `.promotion`, `.status` (effective dates) | the student's |
| `vs_staff.models.LeaveRequest.display_status`, `workflow_handlers` (undoing an approval) | the person's posting |
| `vs_staff.services.leave` on-leave expression, until-expression and set | each person's, through `branch_day_q` on `staff__branch` |
| `vs_staff.services.employment`, `.hire`, `.invitations`, `.setup_invitations`, `.organogram` (event dates, exit dates, appointment start and end) | the person's posting |
| `vs_staff.serializers` tenure, `views.leave` balance session | the person's posting |
| `vs_calendar.views.reads` current, year and overview | the `?branch=` lens, else the school |
| `vs_academics.views.reads` overview | the `?branch=` filter, else the school |
| FAL fee billing run | the structure's pinned branch, else the school (one date for the run, which the preview promises) |
| FAL `ar_ageing`, `debtors` | `branch_ref`, else the school |
| FAL requisition request date, goods receipt date | the raised branch, the order's |
| `vs_finance.fees.generate_invoices` with no invoice date | each customer's; the due date counts from it |
| `vs_finance.views._invoice_bucket`, invoice summary overdue balance; `views_ar._customer_ledger` overdue; customer detail invoice status; dunning summary | each invoice's |
| `vs_finance.receivables.post_opening_balance` | the customer's |
| `PaymentPlanInstallment.is_overdue` | the plan's |
| petty cash fund detail, bank reconciliation | the fund's, the bank account's |
| `vs_finance.dunning` single-invoice reminder | the invoice's |
| `posting.reverse_journal` fallback date, `create_direct_entry` default date | the journal's |
| `vs_procurement.serializers._row_today` (quotation validity, bill overdue) | the row's (a contract has none, so the school's) |
| `vs_procurement.views.receiving` overdue tab and summary | each bill's |
| stock issue and adjustment date, restock requisition | the store's, the raised branch |
| `sourcing` award validity, awarded order date | the quotation's |
| `vendor_portal` quote date, RFQ deadline zone | the RFQ's |
| vendor payment reversal date | the payment's |
| `vs_payments._booking_date` | a receipt's customer's; a payout's paying bank's |

### Deliberately on the school's day

- The books' fiscal calendar (`fiscal_calendar`, `seed`, `posting_window`,
  `fiscal_calendar_runway`, the account's fiscal year, the next year's
  defaults): an entity keeps one calendar, and a period closes for every
  branch at once.
- Reports and dashboards with one as-of date (`vs_finance.reports`,
  `dashboard*`, `vs_procurement.reports`, `dashboard*`), the KPI strips of
  payments, expenses, orders, requisitions, vendors and contracts, and the
  invoice summary's month series: each is one figure for the school.
- The dunning run and its settlement sweep: one run over the entity whose run
  date makes a re-run idempotent.
- Vendor contracts and vendor assessments: master data with no branch.
- The school's subscription (`vs_schools`), the fee due policy preview, the
  admin console overview, `vs_exports` date windows, `vs_todo` task status (a
  personal task has no branch), and `vs_history` as-at reads (a day the reader
  names, on the school's calendar).
- `vs_tenants` document numbers: one daily series per tenant.
- The enrolment form's birth-date check runs before the service resolves the
  branch, so it uses the school's day; the enrolment itself is dated on the
  branch's.

### Deliberately not on any tenant's day

- `vs_health.tasks.rollup_uptime_daily_task`: uptime rollups are the
  platform's own UTC day buckets over UTC probe instants, not anybody's
  calendar.
- `DateField(default=timezone.localdate)` on `vs_staff` and `vs_user`
  position assignments: a field default cannot see its row's tenant, and
  every service that creates these rows passes a date.
- `vs_finance.export_datasets._translate_invoices`: the screen-translation
  hook takes only the query parameters, so its "overdue" bucket uses the
  platform day.
- `vs_import_data` school-import validation ("a subscription cannot expire in
  the past") uses the platform day on purpose: the rows are schools that do not
  exist yet, and every new school starts on the platform zone.
- `vs_user` position assignments belong to platform staff, so their default
  start and end dates are the platform day.

### Printed dates

Server-rendered dates (invoices, receipts, PDFs, emails, exports, refusal
sentences) are still written in fixed formats and, in some emails, on the
server's UTC clock. They do not read `display.date_format` or `display.clock`
yet; `todo.md` lists them.

## 5. Tests

- `vs_config/tests_clock.py`: Lagos's day at 23:30 UTC, a Nairobi tenant's own
  day and clock, the platform value inherited, the platform tenant reading the
  platform layer, the memo (two queries once, none after, shared with the
  request's tenant), invalid zones refused at the guard, a corrupt stored value
  falling back.
- `schools/vs_calendar/tests/test_school_day.py`: the hub and `current/` turn
  the day at Lagos midnight and at Nairobi midnight; `?on=` still wins.
- `vs_tenants/tests.py` `TenantDocumentNumberTests`: an undated document
  number carries the tenant's day.
- `vs_config/tests_clock.py` `BranchClockTests`: a Nairobi branch of a Lagos
  school turns its day at 21:00 UTC, a branch named by row, id or digits, the
  two-query memo that reads every branch with the school, forgetting, a corrupt branch value, `branch_day_q` per row,
  and a school whose branches share its zone filtering exactly as before.
- `test_branch_clock.py` in `vs_students`, `vs_staff` and `vs_finance`: an
  enrolment and an applicant dated on the branch's day, an offer lapsed at one
  branch only, leave over at one branch and running at the other (set,
  expression and Completed), a fee run dating each family on its branch's day,
  and a bill overdue at one branch only.
