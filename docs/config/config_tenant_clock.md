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
- `vs_user.PositionAssignment.start_date` (`default=timezone.localdate`):
  platform staff seats, not a school's record, so it keeps its default. The
  school records that had the same default (a student's `enrolment_date`, a
  placement's and a status change's `effective_date`, a staff appointment's
  `start_date`, and a vendor assessment's `assessment_date`) have none: a
  column default can only be the server's UTC day, so every write names the
  day (its branch's, or the tenant's for a record with no branch) and a write
  that names none is refused by the database.
- `vs_finance.export_datasets._translate_invoices`: the screen-translation
  hook takes only the query parameters, so its "overdue" bucket uses the
  platform day.
- `vs_import_data` school-import validation ("a subscription cannot expire in
  the past") uses the platform day on purpose: the rows are schools that do not
  exist yet, and every new school starts on the platform zone.
- `vs_user` position assignments belong to platform staff, so their default
  start and end dates are the platform day.

### Printed dates

Everything the server writes for a person to read (a printed invoice or
receipt, a PDF statement, an email or notification, a file an export produces
for people, an approval card, a refusal or warning sentence) writes its dates
and times through `vs_config.display`, so it reads exactly as the school's
screens do (school-fe `src/lib/dates.ts`). Section 5 is the module.

## 5. Printing a date: `vs_config.display`

```python
from vs_config.display import (
    format_date, format_datetime, format_time, format_month, format_date_range,
)

format_date(value, tenant, *, branch=None, year=True, month="short", weekday=None)
    # "29 Sep 2026" | "29/09/2026" | "2026-09-29"
format_datetime(value, tenant, *, branch=None, with_zone=False, weekday=None)
    # "29 Sep 2026, 2:30 pm" | "29/09/2026, 14:30"; with_zone adds " WAT"
format_time(value, tenant, *, branch=None, with_zone=False)
    # "2:30 pm" | "14:30"
format_month(value, tenant, *, branch=None, month="short")
    # "Sep 2026" in every date format ("September 2026" with month="long")
format_date_range(start, end, tenant, *, branch=None)
    # "27 - 31 Oct 2025", "28 Oct - 2 Nov 2025", "19 Dec 2025 - 2 Jan 2026";
    # the numeric formats print both dates in full
```

- **Two kinds of value.** A calendar date (a `date` or `"YYYY-MM-DD"`) is
  printed as it is and never shifted: a due date is the same day at every
  branch. An instant (a `datetime`, or an ISO timestamp string) is read on the
  wall clock of `branch` when that branch keeps its own zone, else the
  school's, and only then written. A naive `datetime` is UTC, which is what
  the server stores (`USE_TZ = True`, `TIME_ZONE = "UTC"`). A wall time with
  no date (a `time` or `"HH:MM"`: a bell, an exam slot) is only reworded for
  the clock.
- **Which branch.** Pass the branch of the thing being printed (an invoice's,
  a purchase order's, an RFQ's, a person's) whenever the value is an instant.
  Dates need none. A school-wide document (a statement run, an export file, a
  report) passes none and reads the school's zone.
- **The zone's name.** `with_zone=True` appends the zone's abbreviation, for a
  reader outside the school: a vendor reading an RFQ deadline, a person
  reading when a password-reset link dies.
- **Empty and unreadable.** `None` and `""` print as `""`; a value that is not
  a date comes back as written, so a bad value is visible rather than blank.
- **The tenant.** `request.tenant` in a view, `entity.tenant` in the finance
  and procurement engines, `staff.tenant` and so on elsewhere. `None` and the
  platform tenant read the platform's values, so an email to a platform
  operator follows the platform's settings.
- **Caching.** The date format and the clock cost two queries together and
  are memoised on the tenant instance beside its zone, shared with the
  request's own instance; `forget_tenant_zone` drops them with the zone.
- **Templates.** `{% load display_dates %}` gives `display_date`,
  `display_datetime`, `display_time` and `display_month`, whose argument is a
  tenant or a `vs_tenants.Branch`. The library is named for the display
  settings rather than for a school because `vs_config` is an engine app. The
  invoice and receipt templates take pre-written strings from their context
  builders instead, like their money.
- **The pure writers.** `write_date(day, date_format, ...)` and
  `write_time(hour, minute, clock)` for a caller that already holds the
  choices; `display_style(tenant)` returns them.

### What stays ISO

- JSON fields a client formats itself (`"due_date": "2026-09-29"`).
- Export columns in system mode: ISO dates, UTC `YYYY-MM-DDTHH:MM:SS`, and
  `HH:MM:SS` for a wall time, because an importer is written once.
- A download's `{date}` and `{datetime}` file-name tokens (sortable, and a
  slash cannot be in a file name), read on the school's clock
  (`tenant_now`), so a file run at 00:30 in Lagos is named for that day, as
  the builder's preview names it.
- Structured audit metadata, log lines, `__str__`, document numbers
  (`INV-12609291`), management-command output and the instruction "write it
  as YYYY-MM-DD" on an import.
- **Approval cards are stored ISO and written as they are read.** A workflow
  instance snapshots its summary and details at submission. The handlers
  store dates ISO, and `vs_workflow.presentation.summary_for_reader` and
  `details_dates_for_reader` rewrite any value that is a date, a timestamp or
  "date to date" in the tenant's format each time the card is read, so a card
  submitted before a school changes its format reads the new way, and the
  snapshot is never rewritten.

### Issued and stored text

Every printed document is built when it is asked for: an invoice or receipt
page, its PDF, a statement, an email body, an export file. Each follows the
setting in force at that moment. Two things are kept after they are made and
are never rewritten: the PDF attached to a finance document email
(`FinanceDocumentDelivery.pdf_file`) and an export's file with its name
(`ExportFile`). A sentence stored for people (an audit message, a journal's
narration) is written in the format in force when it is written and keeps it,
like an issued document.

## 6. Tests

- `vs_config/tests_display_format.py`: every date format with every clock for
  a date, an ISO string, an instant, a wall time and a month; the 12-hour
  edges (12:00 am, 12:00 pm); a month named in every format; a calendar date
  never shifted at any branch; an instant dated at its Nairobi branch after
  21:00 UTC and a month turning there; naive and `Z` timestamps read as UTC;
  the platform's values for `None` and the platform tenant; ranges; the
  two-query memo, shared with the request's tenant and forgotten with the
  zone; the template filters with a tenant and with a branch.
- `tests_display_dates.py` in `vs_exports`, `vs_finance`, `vs_procurement`
  and `vs_payments`, and `tests/test_display_dates.py` in `vs_workflow`,
  `vs_students`, `vs_staff` and `vs_calendar`: each app's printed dates in a
  school that writes DD/MM/YYYY on a 24-hour clock, and the defaults.

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
